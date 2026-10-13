"""Required draft facts do not force verification of an optional shortlist."""

import json

import httpx
import pytest
from conftest import run_git, valid_meta
from test_source_admission_pipeline import PipelineWire

from doclayer.curator import curate
from doclayer.curator.model import ModelError
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.publish import Publisher
from doclayer.curator.state import Evidence, JobState, PageChange, SourceRecord


def seed(cfg, store, count, required):
    records = [
        Evidence(
            "manual",
            "Device manual",
            f"# Fact {number}",
            "quote",
            f"Device factual detail {number} is {number + 10}.",
            quote=f"Device factual detail {number} is {number + 10}.",
        )
        for number in range(count)
    ]
    text = "\n\n".join(record.locator + "\n" + record.text for record in records)
    source = SourceRecord("manual", "text", "Device manual", status="fetched", extraction_complete=True)
    change = PageChange(
        "g",
        "wiki/device.md",
        "create",
        title="Device",
        source_ids=["manual"],
        claim_ids=[curate.evidence_id(record) for record in records[:required]],
    )
    state = store.create(
        JobState(
            "",
            "",
            "Device",
            sources=[source],
            changes=[change],
            limits={"max_model_calls": 32},
        )
    )[0]
    pipeline = Pipeline(cfg, store)
    pipeline._store_extract(state, source, text)
    store.append_evidence(state.job_id, [record.to_dict() for record in records])
    # Legacy positive editorial markers cannot become current source support.
    state.evidence_verification = {
        curate.evidence_id(record): {
            "status": "supported",
            "supported": True,
            "support_version": 2,
            "source_binding": "obsolete",
            "excerpt": record.quote,
        }
        for record in records
    }
    state.base_commit = run_git(cfg.paths.brain_checkout, "rev-parse", "HEAD")
    return state, records, text


@pytest.mark.asyncio
async def test_actual_861_claim_job_verifies_required8_then_drafts_checks_and_publishes(cfg, store, origin, forgejo):
    state, records, text = seed(cfg, store, 861, 8)
    wire = PipelineWire()
    ordinary = wire.transport()

    def transport(request):
        messages = json.loads(request.content)["messages"]
        system, user = [row["content"] for row in messages]
        if "Draft the full page." in user:
            wire.requests.append((system, user))
            assert "Device factual detail 860" not in user
            reply = {
                "frontmatter": dict(valid_meta(), title="Device"),
                "body": "# Device\n" + "\n".join(record.text + " [source: Device manual]" for record in records[:8]),
                "notes": [],
            }
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": json.dumps(reply)},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        return ordinary.handler(request)

    pipeline = Pipeline(
        cfg,
        store,
        publisher=Publisher(cfg, store, forgejo=forgejo),
        inference_transport=httpx.MockTransport(transport),
    )
    await pipeline._stage_draft(state)
    assert state.changes[0].status == "drafted" and not state.changes[0].missing_evidence
    selection = state.evidence_selection["wiki/device.md"]
    assert len(selection["selected"]) == 8 and len(selection["pending_optional"]) == 853
    assert set(selection["pending_optional_proofs"]) == set(selection["pending_optional"])
    source_requests = []
    for system, user in wire.requests:
        if user.startswith("SOURCE ANCESTOR"):
            source_requests.append(json.loads(user.split("\n", 1)[1])["claim"])
        elif "Verify selected claims" in system:
            source_requests.extend(row["claim"] for row in json.loads(user.split("\n", 1)[1]))
    assert len(source_requests) == 8 and set(source_requests) == {record.text for record in records[:8]}
    assert all(
        state.evidence_verification[curate.evidence_id(record)]["support_version"] == 2 for record in records[8:]
    )
    await pipeline._stage_validate(state)
    assert state.changes[0].status == "validated", state.changes[0].validation
    await pipeline._stage_publish(state)
    assert state.changes[0].status == "published" and state.publication.merged
    assert state.usage.model_calls <= 32
    published = run_git(origin, "show", "main:wiki/device.md")
    assert all(record.text in published for record in records[:8])
    assert "Device factual detail 860" not in published
    assert len(store.read_evidence(state.job_id)) == 861
    # Source byte changes revoke positives, including optional current proofs.
    pipeline._store_extract(state, state.sources[0], text + "\nChanged source context.")
    assert all(record.support_status == "unknown" for record in pipeline._evidence(state))


@pytest.mark.asyncio
async def test_mandatory_source_outage_preserves_pending_only_required_and_resumes(cfg, store):
    state, records, _ = seed(cfg, store, 861, 8)
    cfg.inference.retries = 0

    def unavailable(request):
        if request.method == "GET":  # reachability probe: the server answers, only the POST drops
            return httpx.Response(404)
        raise httpx.ConnectError("actual source inference outage")

    pipeline = Pipeline(cfg, store, inference_transport=httpx.MockTransport(unavailable))
    client = pipeline._model(state)
    with pytest.raises(ModelError):
        await pipeline._verify_selected(state, state.changes[0], client)
    assert state.usage.model_calls == 1
    pending = {identity for identity, value in state.evidence_verification.items() if value.get("verification_pending")}
    assert pending == set(state.changes[0].claim_ids)
    assert all(
        state.evidence_verification[curate.evidence_id(record)]["support_version"] == 2 for record in records[8:]
    )
    saved = store.load(state.job_id)
    assert saved.evidence_verification == state.evidence_verification
    pipeline = Pipeline(cfg, store, inference_transport=PipelineWire().transport())
    await pipeline._verify_selected(saved, saved.changes[0], pipeline._model(saved))
    assert saved.usage.model_calls > 1 and not saved.changes[0].missing_evidence
    assert len(saved.evidence_selection["wiki/device.md"]["pending_optional"]) == 853


def test_more_than80_required_whole_records_are_preserved_and_missing_id_blocks(cfg, store):
    state, records, text = seed(cfg, store, 100, 81)
    pipeline = Pipeline(cfg, store)
    for record in records:
        state.evidence_verification[curate.evidence_id(record)] = {
            "supported": True,
            "status": "supported",
            "excerpt": record.quote,
            "support_version": curate.SOURCE_SUPPORT_VERSION,
            "source_binding": curate.source_support_binding(record, {"manual": text}),
        }
    selected = pipeline._page_evidence(state, state.changes[0], pipeline._evidence(state))
    assert len(selected) == 81 and not state.changes[0].missing_evidence
    state.changes[0].claim_ids.append("missing-source-claim")
    pipeline._page_evidence(state, state.changes[0], pipeline._evidence(state))
    assert state.changes[0].missing_evidence == ["missing-source-claim"]


@pytest.mark.asyncio
async def test_rejected_required_claim_remains_missing_instead_of_optional_replacement(cfg, store):
    state, records, _ = seed(cfg, store, 3, 2)
    pipeline = Pipeline(cfg, store, inference_transport=PipelineWire().transport())
    pipeline._store_extract(
        state,
        state.sources[0],
        "# Fact 0\nUnrelated statement.\n"
        + records[1].locator
        + "\n"
        + records[1].text
        + "\n"
        + records[2].locator
        + "\n"
        + records[2].text,
    )
    await pipeline._verify_selected(state, state.changes[0], pipeline._model(state))
    assert state.changes[0].missing_evidence == [curate.evidence_id(records[0])]
    assert state.evidence_verification[curate.evidence_id(records[0])]["status"] == "unsupported"
    assert state.evidence_selection[state.changes[0].path]["selected"] == [curate.evidence_id(records[1])]
    assert curate.evidence_id(records[2]) in state.evidence_selection[state.changes[0].path]["pending_optional"]


def test_recomputed_optional_frontier_retains_supported_unplaced_facts(cfg, store):
    state, records, text = seed(cfg, store, 4, 1)
    pipeline = Pipeline(cfg, store)
    change = state.changes[0]
    pipeline._page_evidence(state, change, pipeline._evidence(state))
    before = state.evidence_selection[change.path]
    expected = {curate.evidence_id(record) for record in records[1:]}
    assert set(before["optional_claims"]) == set(before["pending_optional"]) == expected
    for record in records[:2]:
        state.evidence_verification[curate.evidence_id(record)] = {
            "supported": True,
            "status": "supported",
            "excerpt": record.quote,
            "source_binding": curate.source_support_binding(record, {"manual": text}),
            "support_version": curate.SOURCE_SUPPORT_VERSION,
        }
    # Reconciliation callers may pass only supported records. The complete
    # optional editorial frontier is still recomputed from actual retained data.
    supported = [record for record in pipeline._evidence(state) if record.support_status == "supported"]
    pipeline._page_evidence(state, change, supported)
    after = state.evidence_selection[change.path]
    verified_optional = curate.evidence_id(records[1])
    assert set(after["optional_claims"]) == set(after["optional_proofs"]) == expected
    assert verified_optional not in after["pending_optional"]
    assert after["optional_proofs"][verified_optional] == {
        "source_binding": state.evidence_verification[verified_optional]["source_binding"],
        "support_version": curate.SOURCE_SUPPORT_VERSION,
    }
    assert after["frontier_context"] == before["frontier_context"]
    assert not state.evidence_selection.get("placement"), "verification must not manufacture editorial disposition"
    assert not change.missing_evidence


@pytest.mark.asyncio
async def test_actual_placement_checkpoints_bind_coverage_to_fresh_canonical_commit(cfg, store, checkout, monkeypatch):
    from test_placement import Planner

    from doclayer import frontmatter

    state, records, text = seed(cfg, store, 2, 1)
    target = checkout / "wiki/device.md"
    target.write_text(
        frontmatter.build(
            dict(valid_meta(cid="c-af123123"), title="Device"),
            "# Device\n" + "\n".join(record.text for record in records),
        )
    )
    run_git(checkout, "add", str(target))
    run_git(checkout, "commit", "-qm", "canonical scoped device facts")
    run_git(checkout, "push", "origin", "main")
    for record in records:
        state.evidence_verification[curate.evidence_id(record)] = {
            "supported": True,
            "status": "supported",
            "excerpt": record.quote,
            "source_binding": curate.source_support_binding(record, {"manual": text}),
            "support_version": curate.SOURCE_SUPPORT_VERSION,
        }
    model = Planner(covered=True)
    pipeline = Pipeline(cfg, store)
    monkeypatch.setattr(pipeline, "_model", lambda current: model)
    checkpoints = []
    original_save = store.save

    def save(current):
        ledger = current.evidence_selection.get("placement", {}).get("ledger")
        if ledger:
            checkpoints.append(ledger.get("canonical_commit"))
        return original_save(current)

    monkeypatch.setattr(store, "save", save)
    await pipeline._stage_place(state)
    first = run_git(checkout, "rev-parse", "origin/main")
    assert state.base_commit == first and checkpoints and set(checkpoints) == {first}
    assert state.evidence_selection["placement"]["ledger"]["canonical_commit"] == first
    assert all(row["status"] == "covered" for row in state.evidence_selection["placement"]["ledger"]["claims"].values())
    target.write_text(target.read_text() + "\nIndependent operator addition.\n")
    run_git(checkout, "add", str(target))
    run_git(checkout, "commit", "-qm", "later canonical snapshot")
    run_git(checkout, "push", "origin", "main")
    checkpoints.clear()
    await pipeline._stage_place(state)
    fresh = run_git(checkout, "rev-parse", "origin/main")
    assert fresh != first and state.base_commit == fresh
    assert checkpoints and set(checkpoints) == {fresh}
    assert store.load(state.job_id).evidence_selection["placement"]["ledger"]["canonical_commit"] == fresh
