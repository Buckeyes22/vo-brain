"""Search locates candidates; fetched official authority is independently judged."""

import json
import ssl
from types import SimpleNamespace

import httpx
import pytest

from doclayer import discovery
from doclayer.coverage import Catalog

GAP = {
    "gap_id": "gap-public",
    "identity": "unknown:widget",
    "representative_question": "How does Widget configure retries?",
}


def catalog():
    return Catalog(status="ok", revision="a" * 40, policy_version=1)


def search(urls, status=200):
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json={"results": [{"url": url} for url in urls]})
        )
    )


def test_search_candidate_reaches_verified_source_and_same_family_links(monkeypatch):
    visited, judged = [], []
    pages = {
        "https://widget.example/docs": '<h1>Widget documentation</h1><a href="/docs/retries">Retries</a><a href="https://mirror.example/widget">Mirror</a>',
        "https://widget.example/docs/retries": "Widget retries preserve bounded backoff.",
    }

    def fetch(request):
        visited.append(str(request.url))
        return httpx.Response(200, text=pages[str(request.url)])

    def authority(url, title, subject, question, body, **kwargs):
        judged.append((url, subject, question, body))
        return True

    monkeypatch.setattr(discovery, "_qwen_available", lambda: True)
    monkeypatch.setattr(discovery, "_qwen_authoritative", authority)
    result = discovery.discover(
        GAP,
        catalog(),
        client=httpx.Client(transport=httpx.MockTransport(fetch)),
        search_client=search(["https://widget.example/docs"]),
    )
    assert result.status == "discovered"
    assert result.seeds[0].origin == "search-candidate"
    assert result.seeds[0].relevance == "semantic-authority"
    assert result.model_calls == 1 and result.requests_used == 3
    assert judged[0][1:3] == ("widget", GAP["representative_question"])
    assert judged[0][3] == discovery.html_to_markdown(pages[visited[0]])
    assert visited == list(pages) and all(len(seed.source_hash) == 64 for seed in result.seeds)


@pytest.mark.parametrize(
    "semantics,available,authority",
    [(False, True, True), (True, False, True), (True, True, False)],
)
def test_search_rank_http_success_and_matching_title_never_certify_authority(
    monkeypatch, semantics, available, authority
):
    monkeypatch.setattr(discovery, "_qwen_available", lambda: available)
    monkeypatch.setattr(discovery, "_qwen_authoritative", lambda *a, **kw: authority)
    result = discovery.discover(
        GAP,
        catalog(),
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text="Widget official retries documentation"))
        ),
        search_client=search(["https://mirror.example/widget"]),
        use_semantics=semantics,
    )
    assert not result.seeds and result.status == "unresolved"


def test_search_authority_attempts_share_existing_three_call_cap(monkeypatch):
    # The cap is 20 since the assumption audit; one search yields at most MAX_SEEDS
    # (15) candidates, so the cap logic is exercised at its former value of 3.
    monkeypatch.setattr(discovery, "MAX_SEMANTIC_CALLS", 3)
    monkeypatch.setattr(discovery, "_qwen_available", lambda: True)
    calls = []
    monkeypatch.setattr(discovery, "_qwen_authoritative", lambda *a, **kw: calls.append(a) or False)
    result = discovery.discover(
        GAP,
        catalog(),
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="Widget guide"))),
        search_client=search([f"https://candidate{i}.example/docs" for i in range(10)]),
    )
    assert result.model_calls == len(calls) == discovery.MAX_SEMANTIC_CALLS
    assert result.requests_used <= discovery.MAX_DISCOVERY_REQUESTS and not result.seeds


def test_failed_catalog_root_falls_back_to_search_and_reports_gateway_status():
    c = catalog()
    from doclayer.coverage import SubjectEntry

    c.subjects["widget"] = SubjectEntry(
        slug="widget",
        kind="product",
        title="Widget",
        public_docs={"base_url": "https://widget.example/"},
    )
    result = discovery.discover(
        dict(GAP, canonical_subject="widget"),
        c,
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        search_client=search([], 504),
        use_semantics=False,
    )
    assert result.requests_used == 2 and not result.seeds
    assert any("HTTP 504" in note for note in result.notes)


def test_legacy_private_ca_compatibility_keeps_chain_hostname_and_mtls(monkeypatch, tmp_path):
    ctx = ssl.create_default_context()
    ctx.verify_flags |= ssl.VERIFY_X509_STRICT

    class Context:
        verify_flags = ctx.verify_flags
        verify_mode = ctx.verify_mode
        check_hostname = ctx.check_hostname

        def load_cert_chain(self, cert, key):
            self.loaded = (cert, key)

    fake = Context()
    monkeypatch.setattr(discovery.ssl, "create_default_context", lambda **kw: fake)
    assert discovery._search_tls_context(tmp_path) is fake
    assert not fake.verify_flags & ssl.VERIFY_X509_STRICT
    assert fake.verify_mode == ssl.CERT_REQUIRED and fake.check_hostname
    assert fake.loaded == (str(tmp_path / "client.crt"), str(tmp_path / "client.key"))


@pytest.mark.parametrize(
    "verdict,expected",
    [
        ({"official": True, "relevant": True}, True),
        ({"official": False, "relevant": True}, False),
        ({"official": "true", "relevant": True}, False),
    ],
)
def test_actual_native_authority_http_is_charged_strict_and_complete(monkeypatch, tmp_path, verdict, expected):
    from doclayer.curator import config
    from doclayer.gap_budget import ResourceBudget, today

    cfg = SimpleNamespace(
        limits=SimpleNamespace(prompt_char_budget=6000),
        inference=SimpleNamespace(
            url="http://native.test/v1",
            model="local-qwen",
            timeout_s=60,
            effective_output_target=4096,
        ),
        inference_api_key=lambda: "fixture-key",
        stages={},
    )
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    real_client = httpx.Client

    def handler(request):
        payload = json.loads(request.content)
        frame = json.loads(payload["messages"][1]["content"])
        assert frame["complete_content"] == "Widget first-party guide: preserve bounded retries."
        assert payload["max_tokens"] == 4096
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["chat_template_kwargs"] == {
            "enable_thinking": True,
            "thinking_token_budget": 1024,
        }
        assert ResourceBudget(tmp_path).load(today())["model_calls"] == 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(verdict)},
                    }
                ]
            },
        )

    monkeypatch.setattr(
        discovery.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    assert (
        discovery._qwen_authoritative(
            "https://widget.example/docs",
            "Widget",
            "widget",
            "retries?",
            "Widget first-party guide: preserve bounded retries.",
            state_dir=tmp_path,
        )
        is expected
    )
    assert ResourceBudget(tmp_path).load(today())["model_calls"] == 1


def test_registry_exact_project_title_bootstraps_observed_documentation_root(
    monkeypatch,
):
    from doclayer import resolver

    async def registry(*args, **kwargs):
        return {"content": [{"text": "Title: Widget\n- Context7-compatible library ID: /maintainer/widget-monorepo"}]}

    monkeypatch.setattr(resolver, "_context7_call", registry)
    pages = {
        "https://raw.githubusercontent.com/maintainer/widget-monorepo/HEAD/README.md": "# Widget\n[Documentation](https://widget.example/docs/)\n[Community](https://forum.example/)",
        "https://widget.example/docs/": "<h1>Widget retries documentation</h1>",
    }
    visited = []

    def handler(request):
        visited.append(str(request.url))
        return httpx.Response(200, text=pages[str(request.url)])

    real_client = httpx.Client
    monkeypatch.setattr(
        discovery.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    result = discovery.discover(GAP, catalog(), use_semantics=False)
    assert result.status == "discovered" and result.model_calls == 0
    assert visited == list(pages)
    assert any(seed.origin == "project-documentation-root" for seed in result.seeds)


def test_registry_website_id_never_invents_github_project(monkeypatch):
    from doclayer import resolver

    async def registry(*args, **kwargs):
        return {"content": [{"text": "Title: Widget\n- Context7-compatible library ID: /websites/widget"}]}

    monkeypatch.setattr(resolver, "_context7_call", registry)
    monkeypatch.setattr(discovery, "_search_candidates", lambda *args, **kwargs: [])
    real_client = httpx.Client
    monkeypatch.setattr(
        discovery.httpx,
        "Client",
        lambda **kw: real_client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(AssertionError("no fabricated root may be fetched"))
            ),
            **kw,
        ),
    )
    result = discovery.discover(GAP, catalog(), use_semantics=False)
    assert result.status == "unresolved" and not result.seeds
