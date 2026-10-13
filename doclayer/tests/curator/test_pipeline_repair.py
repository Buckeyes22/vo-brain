"""Resume safety: repair stops on a repeated failure set, and repair contexts
saved by an older version are never replayed."""

import hashlib
import json

import pytest
from test_recovery_safety import fake_validation, validation_job

from doclayer.curator import curate, preservation
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.validate import Failure


def legacy_binding(state, change, corrections):
    """The binding exactly as computed by the older version (no repair generation)."""
    material = {
        "version": 2,
        "preservation_version": preservation.VERSION,
        "page": change.path,
        "base_commit": change.base_commit or state.base_commit,
        "base_blob": change.base_blob,
        "intent": state.intent,
        "source_policy": state.source_policy,
        "corrections": corrections,
        "required": change.claim_ids,
        "missing": change.missing_evidence,
        "support_version": curate.SOURCE_SUPPORT_VERSION,
        "records": [],
        "source_revisions": [],
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@pytest.mark.asyncio
async def test_repair_stops_when_a_pass_leaves_the_identical_failure_set(cfg, store, checkout, monkeypatch):
    fake_validation(monkeypatch, [])  # deterministic check always reports 'still wrong'
    state = validation_job(store, checkout)
    passes = []

    async def repair(client, topic, change, failures, *args, progress=None, **kwargs):
        passes.append([item["detail"] for item in failures])
        return {"body": change.draft_body, "frontmatter": {}, "draft_progress": {}}

    monkeypatch.setattr(curate, "repair_draft", repair)
    await Pipeline(cfg, store)._stage_validate(state)
    change = state.changes[0]
    assert passes == [["still wrong"]]
    assert change.status == "failed" and change.repair_passes == 1
    assert "identical failure set" in change.error and "still wrong" in change.error


@pytest.mark.asyncio
async def test_repair_continues_while_each_pass_changes_the_failure_set(cfg, store, checkout, monkeypatch):
    import doclayer.curator.pipeline as module

    fake_validation(monkeypatch, [])
    state = validation_job(store, checkout)
    seen = []

    def deterministic(change, *args):
        seen.append(change.draft_body)
        return [Failure("test", f"failure number {len(seen)}")]

    monkeypatch.setattr(module, "run_deterministic_checks", deterministic)
    passes = []

    async def repair(client, topic, change, failures, *args, progress=None, **kwargs):
        passes.append(failures[0]["detail"])
        return {"body": change.draft_body, "frontmatter": {}, "draft_progress": {}}

    monkeypatch.setattr(curate, "repair_draft", repair)
    await Pipeline(cfg, store)._stage_validate(state)
    assert passes == ["failure number 1", "failure number 2"]
    assert state.changes[0].status == "failed" and state.changes[0].repair_passes == 2
    assert "identical" not in state.changes[0].error


@pytest.mark.asyncio
async def test_repair_context_saved_before_the_fix_is_invalidated_not_replayed(cfg, store, checkout, monkeypatch):
    fake_validation(monkeypatch, [])
    state = validation_job(store, checkout)
    change = state.changes[0]
    change.repair_passes = 1
    runner = Pipeline(cfg, store)
    old = legacy_binding(state, change, [])
    assert old != runner._repair_binding(state, change, [], [])
    change.repair_context = {
        "failures": [{"check": "scope", "detail": "stale pre-fix failure"}],
        "intent": state.intent,
        "binding_hash": old,
        "body_hash": runner._draft_body_hash(change),
    }
    store.save(state)
    seen = []

    async def repair(client, topic, change, failures, *args, progress=None, **kwargs):
        seen.append([item["detail"] for item in failures])
        return {"body": change.draft_body, "frontmatter": {}, "draft_progress": {}}

    monkeypatch.setattr(curate, "repair_draft", repair)
    await runner._stage_validate(state)
    assert seen and all("stale pre-fix failure" not in details for details in seen)
    assert seen[0] == ["still wrong"]
