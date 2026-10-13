"""Observe requests rather than accepting a nominal client-side selector."""

import json

import httpx
import pytest

from doclayer import livechecks, resolver
from doclayer.settings import Settings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target", ["", "*", "books_raw", "reddit_raw", "vo_rag_brain_v1,books_raw", " vo_rag_brain_v1 "]
)
async def test_bad_brain_target_has_visible_gap_and_never_opens_http(monkeypatch, target):
    monkeypatch.setenv("DOC_LAYER_BRAIN_COLLECTION", target)
    monkeypatch.setattr(resolver, "settings", Settings())

    def forbidden(**kwargs):
        pytest.fail("invalid brain target opened an HTTP client")

    monkeypatch.setattr(resolver.httpx, "AsyncClient", forbidden)
    hits, error = await resolver.query_vo_rag("What is the brain architecture?")
    assert hits == []
    assert "BrainCorpusConfigurationError" in error
    assert "must be vo_rag_brain_v1" in error
    assert "brain search withheld" in error


@pytest.mark.asyncio
async def test_invalid_target_also_withholds_collection_health_probe(monkeypatch):
    monkeypatch.setenv("DOC_LAYER_BRAIN_COLLECTION", "*")
    monkeypatch.setattr(livechecks, "settings", Settings())

    def forbidden(**kwargs):
        pytest.fail("invalid brain target opened a collection-health HTTP client")

    monkeypatch.setattr(livechecks.httpx, "AsyncClient", forbidden)
    result = await livechecks._brain_collection(1)
    assert not result.checks and "must be vo_rag_brain_v1" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["empty", "timeout", "unavailable"])
async def test_retry_keeps_exact_brain_selection_after_empty_or_down_source(monkeypatch, failure):
    monkeypatch.setenv("DOC_LAYER_BRAIN_COLLECTION", "vo_rag_brain_v1")
    monkeypatch.setattr(resolver, "settings", Settings())
    observed = []

    def handler(request):
        observed.append(json.loads(request.content))
        if failure == "timeout":
            raise httpx.ReadTimeout("test outage", request=request)
        if failure == "unavailable":
            return httpx.Response(503)
        return httpx.Response(200, json={"results": []})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        resolver.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    for _ in range(2):
        hits, error = await resolver.query_vo_rag("How does vault propagation work?")
        assert not hits
        assert bool(error) == (failure != "empty")
    assert len(observed) == 2
    assert all(req["collections"] == ["vo_rag_brain_v1"] and req["top_k"] == 8 for req in observed)
