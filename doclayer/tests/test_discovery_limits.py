import httpx

from doclayer import discovery
from doclayer.coverage import Catalog, SubjectEntry


def catalog():
    c = Catalog(status="ok", revision="a" * 40, policy_version=1)
    c.subjects["widget"] = SubjectEntry(
        slug="widget",
        kind="product",
        title="Widget",
        public_docs={"base_url": "https://docs.widget.example/"},
    )
    return c


GAP = {
    "gap_id": "gap-one",
    "canonical_subject": "widget",
    "representative_question": "How do I configure Widget retries?",
}


def test_redirects_count_and_downgrade_is_never_requested():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://docs.widget.example/plain"})

    result = discovery.discover(GAP, catalog(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert calls == ["https://docs.widget.example/"]
    assert result.requests_used == 1 and not result.seeds


def test_cross_host_redirect_is_not_authority():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://unrelated.example/widget"})

    result = discovery.discover(GAP, catalog(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(calls) == 1 and not result.seeds


def test_untrusted_attachment_is_not_official_by_http_success():
    c = Catalog(status="ok", revision="a" * 40, policy_version=1)
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, text="Widget claims, buy here")

    result = discovery.discover(
        dict(GAP, source_candidates=[{"url": "https://random.example/widget"}]),
        c,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert not result.seeds and not requests
    assert any("authority unverified" in n for n in result.notes)


class LargeStream(httpx.SyncByteStream):
    def __init__(self):
        self.read = 0

    def __iter__(self):
        for _ in range(100):
            self.read += 1
            yield b"x" * 65536


def test_byte_limit_stops_consumption_before_full_buffer():
    stream = LargeStream()
    transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
    result = discovery.discover(GAP, catalog(), client=httpx.Client(transport=transport))
    assert not result.seeds
    assert stream.read <= discovery.MAX_BYTES // 65536 + 1


def test_irrelevant_links_cannot_spend_unbounded_model_calls(monkeypatch):
    links = "".join(f'<a href="/unknown-{i}">unknown</a>' for i in range(120))

    def handler(request):
        return httpx.Response(200, text=links if request.url.path == "/" else "unrelated material")

    attempted = []
    monkeypatch.setattr(discovery, "_qwen_available", lambda: True)

    def failed(*args, **kwargs):
        attempted.append(args)
        return None

    monkeypatch.setattr(discovery, "_qwen_relevant", failed)
    result = discovery.discover(GAP, catalog(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(attempted) == discovery.MAX_SEMANTIC_CALLS
    assert result.model_calls == len(attempted)
    assert result.requests_used <= discovery.MAX_DISCOVERY_REQUESTS


def test_pricing_and_official_blog_are_allowed_material():
    for url in (
        "https://docs.widget.example/pricing",
        "https://docs.widget.example/blog/guide",
    ):
        assert not discovery._JUNK_URL.search(url)


def test_frame_that_cannot_fit_even_without_content_never_reaches_http(tmp_path, monkeypatch):
    # Was test_complete_semantic_frame_must_fit_configured_model_budget: an oversized
    # body is now excerpted (assumption audit 2026-10-10), so only a budget too small
    # for the instructions themselves still refuses the call.
    from types import SimpleNamespace

    from doclayer.curator import config

    cfg = SimpleNamespace(
        limits=SimpleNamespace(prompt_char_budget=200),
        inference_api_key=lambda: "fixture-key",
        stages={},
    )
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.setattr(
        discovery.httpx,
        "Client",
        lambda **kw: (_ for _ in ()).throw(AssertionError("oversized request must not reach HTTP")),
    )
    assert discovery._qwen_relevant("https://docs.example/x", "guide", "need", "x" * 600, state_dir=tmp_path) is None
    assert not (tmp_path / "budgets").exists()


def test_page_larger_than_the_prompt_budget_is_judged_on_an_excerpt(tmp_path, monkeypatch):
    """The 16 KB skip is gone: a long page is judged on its head plus heading outline."""
    import json
    from types import SimpleNamespace

    from doclayer.curator import config

    budget = 6000
    cfg = SimpleNamespace(
        limits=SimpleNamespace(prompt_char_budget=budget),
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
    body = (
        "# Widget retries\nWidget retries use bounded backoff.\n"
        + "".join(f"## Section {n}\n" + 'Retry "detail" text. ' * 60 + "\n" for n in range(40))
        + "## Late heading about retry limits\nThe tail.\n"
    )
    assert len(body) > 16_384
    frames = []

    def handler(request):
        payload = json.loads(request.content)
        system, user = (m["content"] for m in payload["messages"])
        frames.append((system, user))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps({"official": True, "relevant": True})}}
                ]
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        discovery.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    assert (
        discovery._qwen_authoritative(
            "https://widget.example/docs", "Widget", "widget", "retries?", body, state_dir=tmp_path
        )
        is True
    )
    [(system, user)] = frames
    assert len(system) + len(user) <= budget
    frame = json.loads(user)
    assert "complete_content" not in frame and "excerpt" in system
    excerpt = frame["content_excerpt"]
    assert excerpt.startswith("# Widget retries\nWidget retries use bounded backoff.")
    assert "## Late heading about retry limits" in excerpt  # the outline reaches the end of the page


def test_page_excerpt_keeps_short_pages_whole():
    assert discovery._page_excerpt("short", 100) == "short"
    long = "# A\n" + "x" * 1000 + "\n## B\n"
    excerpt = discovery._page_excerpt(long, 200)
    assert len(excerpt) <= 200 and excerpt.startswith("# A\nxxx") and excerpt.endswith("# A\n## B")


def test_discovery_bounds_are_the_raised_values():
    assert (discovery.MAX_DISCOVERY_REQUESTS, discovery.MAX_SEEDS, discovery.MAX_SEMANTIC_CALLS) == (40, 15, 20)
    assert discovery.MAX_BYTES == 4 * 1024 * 1024


def test_unknown_project_follows_documentation_root_from_primary_readme(monkeypatch):
    from doclayer import resolver

    async def registry(*args, **kwargs):
        return {"content": [{"text": "Title: Widget\n- Context7-compatible library ID: /actual-owner/widget"}]}

    monkeypatch.setattr(resolver, "_context7_call", registry)
    pages = {
        "https://raw.githubusercontent.com/actual-owner/widget/HEAD/README.md": "# Widget\n[Documentation](https://widget.example/manual/)\n[Community](https://other.example/)",
        "https://widget.example/manual/": '<h1>Widget retries</h1><a href="/manual/retry">Widget retries</a>',
        "https://widget.example/manual/retry": "Widget retries use bounded backoff.",
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
    result = discovery.discover(
        dict(
            GAP,
            canonical_subject=None,
            identity="unknown:widget",
            representative_question="Official Widget documentation: retry configuration",
        ),
        Catalog(status="ok", revision="a" * 40, policy_version=1),
        use_semantics=False,
    )
    assert result.status == "discovered"
    assert any(s.origin == "project-documentation-root" for s in result.seeds)
    assert "https://widget.example/manual/retry" in visited
    assert not any("other.example" in url for url in visited)
    assert all(len(s.source_hash) == 64 for s in result.seeds)


def test_a_rate_limited_judgment_names_its_http_status_in_the_note(tmp_path, monkeypatch):
    """A 429 is not a negative verdict: discovery's note says the request failed."""
    from types import SimpleNamespace

    from doclayer.curator import config

    cfg = SimpleNamespace(
        limits=SimpleNamespace(prompt_char_budget=60000),
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

    def pages(request):
        return httpx.Response(200, text="unrelated material")

    page_client = httpx.Client(transport=httpx.MockTransport(pages))
    real_client = httpx.Client
    monkeypatch.setattr(
        discovery.httpx,
        "Client",
        lambda **kw: real_client(
            transport=httpx.MockTransport(lambda _: httpx.Response(429, text="slow down")),
            **kw,
        ),
    )
    assert discovery._qwen_relevant("https://docs.example/x", "guide", "need", "body", state_dir=tmp_path) is None
    assert discovery.last_judgment_failure() == "HTTP 429"
    result = discovery.discover(GAP, catalog(), client=page_client, state_dir=tmp_path)
    assert result.model_calls >= 1
    assert any(
        "candidate relevance unresolved" in note and "model request failed: HTTP 429" in note for note in result.notes
    ), result.notes
