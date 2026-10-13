"""Linked imports renew permitted deferred work without rewriting history."""

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from doclayer.curator import cli
from doclayer.curator.intake import make_url_source
from doclayer.curator.ops import OpsError, _op_continue
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import Evidence, JobState


def parent_job(cfg, store):
    old = make_url_source("https://docs.test/old")
    old.status = "fetched"
    old.extraction_complete = True
    old.relevance["links_expanded"] = True
    old.content_hash = "old-hash"
    state = store.create(JobState("", "parent-frontier", "Device documentation", status="partial", sources=[old]))[0]
    cache = cfg.cache_dir() / state.job_id / "old.md"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("# Old\n\nThe device supports legacy mode.\n")
    old.extract_path = str(cache)
    record = Evidence(old.id, "Old", "# Old", "quote", "The device supports legacy mode.", claim_id="old-unknown")
    store.append_evidence(state.job_id, [record.to_dict()])
    state.evidence_verification[record.claim_id] = {
        "status": "unknown",
        "reason": "qualification incomplete",
        "verification_pending": False,
        "source_binding": "bound-original",
        "support_version": 12,
    }
    store.save(state)
    return state, cache


def linked(cfg, store, parent, **kwargs):
    reply = _op_continue({"job_id": parent.job_id, **kwargs}, cfg, store)
    return reply, store.load(reply["job_id"])


def fetch_wire(seen):
    def handle(request):
        seen.append(str(request.url))
        return httpx.Response(
            200,
            text="<html><title>Primary manual</title><body><h1>Primary guide</h1><p>"
            + "The documented device supports bounded batch execution. " * 30
            + "</p></body></html>",
            headers={"content-type": "text/html"},
        )

    return httpx.MockTransport(handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("previously_submitted", [False, True])
async def test_deferred_frontier_reactivated_by_later_discovery_and_parent_immutable(cfg, store, previously_submitted):
    parent, _ = parent_job(cfg, store)
    candidate = make_url_source("https://docs.test/next", parent=parent.sources[0])
    candidate.status = "deferred"
    candidate.submitted = previously_submitted
    candidate.error = "discovery deferred for targeted continuation"
    candidate.relevance = {"anchor": "Next guide", "score": 0.8, "priority": "high", "why": "prior relevant judgment"}
    parent.sources.append(candidate)
    parent.usage.sources_fetched = 42
    store.save(parent)
    original = store.state_path(parent.job_id).read_bytes()
    _, deferred = linked(cfg, store, parent, discover=False)
    assert deferred.sources[1].status == "deferred"
    deferred.status = "partial"
    store.save(deferred)
    _, child = linked(cfg, store, deferred, discover=True)
    assert child.sources[1].status == "scored"
    assert child.sources[1].relevance["why"] == candidate.relevance["why"]
    seen = []
    pipeline = Pipeline(cfg, store, fetch_transport=fetch_wire(seen))
    assert await pipeline._stage_discover(child) == "extract"
    assert seen == [candidate.url]
    assert child.sources[1].status == "fetched"
    assert child.usage.sources_fetched == 1
    assert store.load(parent.job_id).usage.sources_fetched == 42
    assert store.state_path(parent.job_id).read_bytes() == original


@pytest.mark.asyncio
async def test_new_primary_seed_intake_owns_old_cache_and_retains_unknown_proof(cfg, store):
    parent, cache = parent_job(cfg, store)
    original = store.state_path(parent.job_id).read_bytes()
    reply, child = linked(cfg, store, parent, urls=["https://docs.test/batch#section"])
    assert child.stage == "intake"
    old, fresh = child.sources
    assert fresh.role == "primary" and fresh.submitted and fresh.status == "pending"
    assert old.id in child.inherited_source_ids and not old.submitted
    assert old.extract_path != str(cache) and Path(old.extract_path).read_bytes() == cache.read_bytes()
    assert child.evidence_verification == parent.evidence_verification
    assert store.read_evidence(child.job_id) == store.read_evidence(parent.job_id)
    cache.unlink()  # Parent cache eviction cannot invalidate the linked input.
    seen = []
    pipeline = Pipeline(cfg, store, fetch_transport=fetch_wire(seen))
    assert await pipeline._stage_intake(child) == "discover"
    assert seen == ["https://docs.test/batch"]
    assert fresh.status == "fetched" and fresh.inspected_at
    assert Path(old.extract_path).is_file()
    assert store.state_path(parent.job_id).read_bytes() == original
    replay, same = linked(cfg, store, parent, urls=["https://docs.test/batch#another", "https://docs.test/batch"])
    assert replay["job_id"] == reply["job_id"] and not replay["created"]
    assert same.evidence_verification == parent.evidence_verification
    other, _ = linked(cfg, store, parent, urls=["https://docs.test/other"])
    assert other["job_id"] != reply["job_id"]


@pytest.mark.parametrize("kind", ["policy", "batch", "historical"])
async def test_excluded_seed_and_deferred_url_never_reactivated(cfg, store, kind):
    parent, _ = parent_job(cfg, store)
    bad = make_url_source("https://docs.test/forbidden", parent=parent.sources[0])
    bad.status = "deferred" if kind != "historical" else "rejected"
    bad.relevance = {"relevant": False, "why": "not relevant"}
    if kind == "policy":
        parent.source_policy = {"exclude": [{"url": bad.url}]}
    elif kind == "batch":
        parent.exclusions = ["forbidden"]
    else:
        bad.exclusions_hit = True
    parent.sources.append(bad)
    store.save(parent)
    _, child = linked(cfg, store, parent, urls=[bad.url])
    retained = child.sources[1]
    assert retained.status == "rejected" or retained.exclusions_hit
    assert child.stage == "discover"
    assert not any(s.status == "pending" for s in child.sources)
    seen = []
    await Pipeline(cfg, store, fetch_transport=fetch_wire(seen))._stage_discover(child)
    assert not seen


def test_explicit_primary_seed_can_supersede_relevance_only_rejection(cfg, store):
    parent, _ = parent_job(cfg, store)
    old = make_url_source("https://docs.test/previously-unrelated", parent=parent.sources[0])
    old.status, old.error = "rejected", "not relevant to old subject"
    old.relevance = {"relevant": False, "why": old.error}
    parent.sources.append(old)
    store.save(parent)
    _, child = linked(cfg, store, parent, urls=[old.url], intent="New batch execution topic")
    seed = child.sources[1]
    assert child.stage == "intake"
    assert seed.id == old.id and seed.status == "pending" and seed.submitted and seed.role == "primary"
    assert seed.relevance["why"] == old.relevance["why"]
    assert seed.relevance["continuation_history"][-1]["status"] == "rejected"
    assert store.load(parent.job_id).sources[1].status == "rejected"


@pytest.mark.parametrize("urls", [["file:///private"], "https://docs.test/new", [123]])
def test_invalid_new_seeds_rejected_without_creating_child(cfg, store, urls):
    parent, _ = parent_job(cfg, store)
    with pytest.raises(OpsError):
        linked(cfg, store, parent, urls=urls)
    assert len(store.list_ids()) == 1


def test_new_seed_no_discover_is_explicit_error(cfg, store):
    parent, _ = parent_job(cfg, store)
    with pytest.raises(OpsError, match="require discovery"):
        linked(cfg, store, parent, urls=["https://docs.test/new"], discover=False)


def test_continue_cli_repeated_urls_forwarded(monkeypatch, cfg, capsys):
    seen = []

    class Transport:
        def __init__(self, cfg):
            pass

        def call(self, op, payload):
            seen.append((op, payload))
            return {"ok": True, "job_id": "job-1234567890"}

    monkeypatch.setattr(cli, "load_config", lambda _: cfg)
    monkeypatch.setattr(cli, "LocalTransport", Transport)
    assert (
        cli.main(
            [
                "--local",
                "continue",
                "--job",
                "job-parent123",
                "--url",
                "https://docs.test/one",
                "--url",
                "https://docs.test/two",
            ]
        )
        == 0
    )
    assert seen[0][0] == "continue" and seen[0][1]["urls"] == ["https://docs.test/one", "https://docs.test/two"]


def test_no_added_urls_preserves_existing_request_idempotency_key(cfg, store):
    parent, _ = parent_job(cfg, store)
    request = {
        "parent": parent.job_id,
        "targets": [],
        "review_omitted": True,
        "discover": True,
        "policy": parent.source_policy,
        "intent": parent.intent,
    }
    original_key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:24]
    child = store.create(JobState("", original_key, parent.intent, status="partial", continuation_of=parent.job_id))[0]
    reply, actual = linked(cfg, store, parent, urls=[])
    assert not reply["created"] and actual.job_id == child.job_id


def test_unexplained_rejection_not_overridden_by_explicit_seed(cfg, store):
    parent, _ = parent_job(cfg, store)
    old = make_url_source("https://docs.test/denied", parent=parent.sources[0])
    old.status, old.error = "rejected", "historical access restriction"
    parent.sources.append(old)
    store.save(parent)
    _, child = linked(cfg, store, parent, urls=[old.url])
    assert child.sources[1].status == "rejected" and child.stage == "discover"


@pytest.mark.asyncio
async def test_pending_deferred_link_uses_real_charged_relevance_protocol(cfg, store):
    parent, _ = parent_job(cfg, store)
    candidate = make_url_source("https://docs.test/pending", parent=parent.sources[0])
    candidate.status = "deferred"
    candidate.relevance = {"anchor": "Bounded execution manual"}
    parent.sources.append(candidate)
    store.save(parent)
    _, child = linked(cfg, store, parent, discover=True)
    calls = []

    def inference(request):
        body = json.loads(request.content)
        calls.append(body)
        assert candidate.url in body["messages"][1]["content"]
        result = {
            "assessments": [
                {"url": candidate.url, "relevant": True, "priority": "high", "why": "primary technical evidence"}
            ]
        }
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(result)}, "finish_reason": "stop"}]}
        )

    seen = []
    pipeline = Pipeline(
        cfg, store, fetch_transport=fetch_wire(seen), inference_transport=httpx.MockTransport(inference)
    )
    await pipeline._stage_discover(child)
    assert len(calls) == child.usage.model_calls == 1
    assert seen == [candidate.url] and child.sources[1].status == "fetched"
