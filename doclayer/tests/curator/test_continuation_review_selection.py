"""Continuation extraction honors omitted-review choices without losing history."""

import copy
from pathlib import Path

import httpx
import pytest
from test_continuation_frontier import linked, parent_job

from doclayer.curator import curate
from doclayer.curator.intake import make_url_source
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import Evidence


def retained_parent(cfg, store):
    parent, cache = parent_job(cfg, store)
    old = parent.sources[0]
    old.reviewed_sections = ["# Old"]
    record = Evidence(
        old.id,
        "Old",
        "# Old",
        "quote",
        "The device supports legacy mode.",
        quote="The device supports legacy mode.",
        claim_id="retained-known",
        url=old.url,
    )
    (store.job_dir(parent.job_id) / "evidence.jsonl").write_text("")
    store.append_evidence(parent.job_id, [record.to_dict()])
    parent.evidence_verification = {
        record.claim_id: {
            "status": "supported",
            "supported": True,
            "support_version": curate.SOURCE_SUPPORT_VERSION,
            "source_binding": curate.source_support_binding(record, {old.id: cache.read_text()}),
            "excerpt": record.quote,
        }
    }
    for label in ("First", "Second"):
        source = make_url_source("https://docs.test/" + label.lower(), parent=old)
        source.status = "fetched"
        source.extraction_complete = False
        source.reviewed_sections = []
        source.omitted_sections = ["# " + label]
        source.omitted_categories = {"source-budget": "Parent stopped before this source"}
        source.relevance["links_expanded"] = True
        path = cache.parent / (source.id + ".md")
        path.write_text("# " + label + "\nDevice supports bounded execution.\n")
        source.extract_path = str(path)
        parent.sources.append(source)
    parent.usage.model_calls = 50
    store.save(parent)
    return parent


@pytest.mark.asyncio
async def test_no_review_omitted_skips_inherited_incomplete_sources_and_reuses_real_proof(cfg, store):
    parent = retained_parent(cfg, store)
    original = store.state_path(parent.job_id).read_bytes()
    proofs = copy.deepcopy(parent.evidence_verification)
    reply, child = linked(cfg, store, parent, discover=False, review_omitted=False)
    before = [copy.deepcopy(s.to_dict()) for s in child.sources]
    calls = []

    def reject(request):
        calls.append(request)
        pytest.fail("No omitted source review was requested")

    pipeline = Pipeline(cfg, store, inference_transport=httpx.MockTransport(reject))
    assert await pipeline._stage_extract(child) == "place"
    restored = store.load(child.job_id)
    assert await pipeline._stage_extract(restored) == "place"  # durable scheduling
    assert not calls and restored.usage.model_calls == 0
    assert restored.evidence_verification == proofs
    admitted = pipeline._evidence(restored)
    assert admitted[0].claim_id == "retained-known" and admitted[0].support_status == "supported"
    assert store.read_evidence(child.job_id) == store.read_evidence(parent.job_id)
    for source, prior in zip(restored.sources[1:], before[1:]):
        assert not source.extraction_complete and source.status == "fetched"
        assert source.reviewed_sections == prior["reviewed_sections"]
        assert source.omitted_sections == prior["omitted_sections"]
        assert source.omitted_categories["source-budget"] == prior["omitted_categories"]["source-budget"]
        assert "continuation-review" in source.omitted_categories
        assert (
            Path(source.extract_path).read_bytes()
            == Path(parent.sources[restored.sources.index(source)].extract_path).read_bytes()
        )
    assert store.state_path(parent.job_id).read_bytes() == original
    again, _ = linked(cfg, store, parent, discover=False, review_omitted=False)
    assert again["job_id"] == reply["job_id"] and not again["created"]


@pytest.mark.asyncio
@pytest.mark.parametrize("targeted", [False, True])
async def test_default_reviews_pending_but_explicit_source_selection_does_not_open_peers(
    cfg, store, monkeypatch, targeted
):
    parent = retained_parent(cfg, store)
    targets = [parent.sources[1].id] if targeted else []
    _, child = linked(cfg, store, parent, discover=False, source_ids=targets)
    visited = []

    async def review(client, source, text, *args, **kwargs):
        visited.append(source.id)
        assert text == Path(source.extract_path).read_text()
        source.reviewed_sections = list(source.omitted_sections)
        source.omitted_sections = []
        return []  # Model may find no relevant additional facts.

    monkeypatch.setattr(curate, "extract_claims", review)
    assert await Pipeline(cfg, store)._stage_extract(child) == "place"
    assert visited == (targets if targeted else [s.id for s in parent.sources[1:]])
    assert child.evidence_verification == parent.evidence_verification
    assert child.sources[1].extraction_complete
    assert child.sources[2].extraction_complete is (not targeted)
    if targeted:
        assert child.sources[2].omitted_sections and "continuation-review" in child.sources[2].omitted_categories


@pytest.mark.asyncio
async def test_explicit_refresh_is_new_review_even_when_old_omitted_work_is_disabled(cfg, store, monkeypatch):
    from test_continuation_frontier import fetch_wire

    parent = retained_parent(cfg, store)
    identity = parent.sources[0].id
    _, child = linked(cfg, store, parent, review_omitted=False, refresh_source_ids=[identity])
    seen = []
    pipeline = Pipeline(cfg, store, fetch_transport=fetch_wire(seen))
    assert await pipeline._stage_intake(child) == "discover"
    visited = []

    async def review(client, source, text, *args, **kwargs):
        visited.append(source.id)
        return []

    monkeypatch.setattr(curate, "extract_claims", review)
    assert await pipeline._stage_extract(child) == "place"
    assert visited == [identity] and len(seen) == 1
    assert not child.sources[1].extraction_complete and not child.sources[2].extraction_complete
    assert pipeline._evidence(child)[0].support_status != "supported"  # Changed source is not old proof.
