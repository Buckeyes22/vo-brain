"""Discovery tests: bounded requests/seeds, junk rejection,
revalidation, no fabrication, deterministic-first with optional local Qwen."""

from __future__ import annotations

import httpx

from doclayer import discovery
from doclayer.coverage import Catalog, SubjectEntry


def _catalog() -> Catalog:
    catalog = Catalog(status="ok", revision="a" * 40, policy_version=1)
    entry = SubjectEntry(
        slug="widget-prod",
        kind="product",
        title="Widget Prod",
        aliases=["widget prod"],
        public_docs={"base_url": "https://docs.widget.example/"},
    )
    catalog.subjects[entry.slug] = entry
    return catalog


def _transport(pages: dict[str, str]):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in pages:
            return httpx.Response(200, text=pages[url])
        return httpx.Response(404)

    return httpx.MockTransport(handler)


GAP = {
    "gap_id": "gap-test",
    "identity": "unknown:widget prod",
    "canonical_subject": "widget-prod",
    "representative_question": "How do I configure widget prod retries?",
}


def test_attached_urls_come_first_and_are_revalidated():
    gap = dict(GAP, source_candidates=[{"url": "https://docs.widget.example/retries"}])
    pages = {"https://docs.widget.example/retries": "<html><body>retries guide</body></html>"}
    result = discovery.discover(gap, _catalog(), client=httpx.Client(transport=_transport(pages)))
    assert result.status == "discovered"
    assert result.seeds[0].url == "https://docs.widget.example/retries"
    assert result.seeds[0].origin == "attached"


def test_bounds_requests_and_seeds():
    # a huge root page linking far more than the seed cap
    links = "\n".join(f'<a href="https://docs.widget.example/page{i}">widget prod page {i}</a>' for i in range(50))
    pages = {"https://docs.widget.example/": f"<html>{links}</html>"}
    for i in range(50):
        pages[f"https://docs.widget.example/page{i}"] = f"<html>widget prod page {i}</html>"
    client = httpx.Client(transport=_transport(pages))
    result = discovery.discover(GAP, _catalog(), client=client)
    assert len(result.seeds) <= discovery.MAX_SEEDS
    assert result.requests_used <= discovery.MAX_DISCOVERY_REQUESTS


def test_junk_links_rejected_and_scope_kept():
    links = (
        '<a href="https://accounts.widget.example/login">sign in</a>'
        '<a href="https://docs.widget.example/retries">widget prod retries</a>'
        '<a href="https://unrelated.example/other">other</a>'
    )
    pages = {
        "https://docs.widget.example/": f"<html>{links}</html>",
        "https://docs.widget.example/retries": "<html>widget prod retries guide</html>",
    }
    result = discovery.discover(GAP, _catalog(), client=httpx.Client(transport=_transport(pages)))
    urls = [seed.url for seed in result.seeds]
    assert all("unrelated.example" not in u and "login" not in u for u in urls)
    assert "https://docs.widget.example/retries" in urls


def test_candidates_that_fail_revalidation_are_dropped():
    gap = dict(GAP, source_candidates=[{"url": "https://docs.widget.example/gone"}])
    result = discovery.discover(gap, _catalog(), client=httpx.Client(transport=_transport({})))
    assert result.status == "unresolved"
    assert not result.seeds
    assert any("revalidation" in note for note in result.notes)


def test_no_seeds_never_fabricates():
    result = discovery.discover(GAP, _catalog(), client=httpx.Client(transport=_transport({})))
    assert result.status == "unresolved"
    assert result.seeds == []
    assert any("never fabricated" in note for note in result.notes)


def test_semantic_route_unavailable_is_reported_not_faked(monkeypatch):
    monkeypatch.setattr(discovery, "_qwen_available", lambda: False)
    links = '<a href="https://docs.widget.example/mystery">mystery</a>'
    pages = {
        "https://docs.widget.example/": f"<html>{links}</html>",
        "https://docs.widget.example/mystery": "<html>mystery</html>",
    }
    result = discovery.discover(GAP, _catalog(), client=httpx.Client(transport=_transport(pages)), use_semantics=True)
    assert result.model_calls == 0
    # the deterministically-irrelevant link was not silently adopted
    assert all(seed.relevance != "semantic" for seed in result.seeds)


def test_subject_terms_from_catalog():
    terms = discovery._subject_terms(_catalog(), "widget-prod")
    assert "widget" in terms and "prod" in terms
