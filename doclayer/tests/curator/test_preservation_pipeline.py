"""Old-fact obligations survive actual stage transitions and restart checkpoints."""

import copy

import pytest
from conftest import run_git, valid_meta
from test_existing_preservation import Model, fact, resolver

from doclayer import frontmatter as fm
from doclayer.curator import curate, preservation
from doclayer.curator.model import ModelError
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import JobState, PageChange


@pytest.fixture
def setup(cfg, store, checkout, monkeypatch):
    metrics = [
        "Under laboratory conditions, Device found 17 severe defects.",
        "Under laboratory conditions, Device found three previously unknown defects.",
    ]
    summary = "Device completed the June evaluation."
    original = "# Device\n## Benchmark\n" + "\n".join(metrics) + "\n"
    meta = valid_meta("c-abcdef12", title="Device")
    path = "wiki/device.md"
    (checkout / path).write_text(fm.build(meta, original))
    run_git(checkout, "add", path)
    run_git(checkout, "commit", "-qm", "existing supported metrics")
    revision = run_git(checkout, "rev-parse", "HEAD")
    change = PageChange(
        "g",
        path,
        "update",
        draft_meta=meta,
        draft_body="# Device\n## Benchmark\n" + summary + "\n",
        base_commit=revision,
        base_blob=run_git(checkout, "rev-parse", revision + ":" + path),
        status="drafted",
    )
    state = store.create(JobState("", "", "Device", base_commit=revision, stage="validate", changes=[change]))[0]
    records = [fact(text) for text in metrics + [summary]]
    store.append_evidence(state.job_id, [record.to_dict() for record in records])
    change.claim_ids = [curate.evidence_id(records[-1])]
    change.draft_progress = {"handled_claim_ids": change.claim_ids}
    store.save(state)
    pipeline = Pipeline(cfg, store)
    model = Model()
    monkeypatch.setattr(pipeline, "_model", lambda _: model)
    monkeypatch.setattr(pipeline, "_evidence", lambda _: records)
    monkeypatch.setattr(pipeline, "_page_records", lambda *_: records)
    monkeypatch.setattr(pipeline, "_page_evidence", lambda *_: records[-1:])
    monkeypatch.setattr(pipeline, "_support_resolver", lambda *_: resolver())

    async def verified(*_):
        return None

    monkeypatch.setattr(pipeline, "_verify_selected", verified)
    return pipeline, state, change, records, original, model


@pytest.mark.asyncio
async def test_validate_repairs_lost_old_metrics_using_actual_old_source_records(setup, monkeypatch):
    pipeline, state, change, records, original, model = setup
    repairs = []

    async def repair(client, topic, target, failures, support, corrections, **kw):
        repairs.append(copy.deepcopy(failures))
        assert {r.text for r in support} >= {r.text for r in records}
        assert all(not r.source_id.startswith("canonical:") for r in support)
        assert sum(f["check"] == "existing-preservation" for f in failures) == 2
        return {
            "frontmatter": target.draft_meta,
            "body": original + records[-1].text + "\n",
        }

    monkeypatch.setattr(curate, "repair_draft", repair)
    assert await pipeline._stage_validate(state) == "publish"
    assert change.status == "validated" and len(repairs) == 1
    assert change.repair_passes == 1
    assert all(r.text in change.draft_body for r in records)
    assert state.changes[0].check_progress["existing_preservation"]["complete"]


@pytest.mark.asyncio
async def test_legacy_validated_job_cannot_publish_without_old_fact_audit(setup):
    pipeline, state, change, records, original, model = setup

    class NoPublisher:
        def publish_changes(self, *_):
            pytest.fail("publication ran before preservation repair")

    pipeline._publisher = NoPublisher()
    change.status = "validated"
    state.stage = "publish"
    assert await pipeline._stage_publish(state) == "validate"
    assert change.status == "drafted"
    assert change.check_progress["existing_preservation"]["complete"]  # Completed review, failed preservation.
    assert not change.check_progress["existing_preservation"]["preservation_complete"]
    assert pipeline.store.load(state.job_id).changes[0].status == "drafted"


@pytest.mark.asyncio
async def test_candidate_check_reset_retains_independently_bound_original_review(setup):
    pipeline, state, change, records, original, model = setup
    await pipeline._preserve_existing(state, change, model, records, [])
    saved = copy.deepcopy(change.check_progress["existing_preservation"])
    change.draft_body += "\n"  # Candidate generation changes; original proof remains bound.
    await curate.check_draft(
        model,
        state.intent,
        change,
        records,
        [],
        char_budget=12000,
        support_resolver=resolver(),
    )
    assert change.check_progress["existing_preservation"] == saved
    await pipeline._preserve_existing(state, change, model, records, [])
    assert change.check_progress["existing_preservation"]["binding"] != saved["binding"]
    assert change.check_progress["existing_preservation"]["original_body"] == saved["original_body"]
    assert change.check_progress["existing_preservation"]["canonical_binding"] == saved["canonical_binding"]


@pytest.mark.asyncio
async def test_reconciliation_audit_outage_persists_candidate_proof_on_owned_change(setup, monkeypatch):
    pipeline, state, change, records, original, model = setup
    candidate = copy.deepcopy(change)

    async def partial(client, topic, target, *_, progress, namespace, **kw):
        target.check_progress[namespace] = {
            "binding": "fresh-canonical-source",
            "partial": True,
        }
        progress(target.check_progress)
        raise ModelError("inference outage", transient=True)

    monkeypatch.setattr(preservation, "audit_existing_preservation", partial)
    with pytest.raises(ModelError):
        await pipeline._preserve_existing(
            state,
            candidate,
            model,
            records,
            [],
            original_raw=fm.build(change.draft_meta, original),
            canonical_binding={"fresh": "actual-blob"},
            namespace="existing_preservation_fresh",
            checkpoint_change=change,
        )
    persisted = pipeline.store.load(state.job_id).changes[0]
    assert persisted.reconcile_check_progress["existing_preservation_fresh"]["partial"]
    assert persisted.draft_body == change.draft_body  # No speculative reconciled body persisted.


@pytest.mark.asyncio
async def test_old_validated_create_with_newly_missing_required_source_never_publishes(setup, monkeypatch):
    pipeline, state, change, records, original, model = setup
    change.action = "create"
    change.status = "validated"
    change.missing_evidence = ["withdrawn-required-claim"]

    class NoPublisher:
        def publish_changes(self, *_):
            pytest.fail("missing required source reached publisher")

    pipeline._publisher = NoPublisher()
    assert await pipeline._stage_publish(state) == "validate"
    assert change.status == "drafted"


@pytest.mark.asyncio
async def test_old_validated_create_is_rechecked_against_current_required_claims(setup):
    pipeline, state, change, records, original, model = setup
    change.action = "create"
    change.status = "validated"
    change.claim_ids = [curate.evidence_id(records[0])]

    # The factual current summary is grounded but its assigned metric is absent.
    class NoPublisher:
        def publish_changes(self, *_):
            pytest.fail("unrepresented required metric reached publisher")

    pipeline._publisher = NoPublisher()
    assert await pipeline._stage_publish(state) == "validate"
    assert change.status == "drafted"
    assert any(
        failure.get("claim_id") == change.claim_ids[0] and failure["severity"] == "blocker"
        for failure in change.check_progress["article_review"]["failures"]
    )
    assert "claims" not in change.check_progress["article_review"]


@pytest.mark.asyncio
async def test_verified_already_merged_receipt_recovers_before_any_model_admission(setup, monkeypatch):
    pipeline, state, change, records, original, model = setup
    change.status = "validated"

    def no_model(_):
        pytest.fail("already merged recovery depends on inference")

    monkeypatch.setattr(pipeline, "_model", no_model)

    class RecoveringPublisher:
        def recover_existing_merge(self, current, changes, persist):
            changes[0].status = "published"
            persist()
            return object()

        def publish_changes(self, *_):
            pytest.fail("already recovered publication was repeated")

    pipeline._publisher = RecoveringPublisher()
    assert await pipeline._stage_publish(state) == "report"
    assert pipeline.store.load(state.job_id).changes[0].status == "published"


@pytest.mark.asyncio
@pytest.mark.parametrize("pushed", [False, True])
async def test_open_immutable_remote_commit_with_missing_claim_cannot_merge_or_repair_under_old_identity(setup, pushed):
    pipeline, state, change, records, original, model = setup
    change.status = "validated"
    change.missing_evidence = ["withdrawn-required-claim"]
    state.publication.pushed = pushed
    state.publication.commit_sha = "a" * 40

    class NoPublisher:
        def recover_existing_merge(self, *_):
            return None

        def publish_changes(self, *_):
            pytest.fail("blocked old remote commit reached merge")

    pipeline._publisher = NoPublisher()
    assert await pipeline._stage_publish(state) == "report"
    assert change.status == "conflict"
    assert state.publication.commit_sha == "a" * 40


@pytest.mark.asyncio
async def test_already_merged_recovery_and_report_survive_exhausted_active_budget(setup, monkeypatch):
    pipeline, state, change, records, original, model = setup
    state.stage = "publish"
    state.status = "retryable"
    change.status = "validated"
    state.limits = {"max_active_seconds": 1, "max_model_calls": 1}
    state.usage.active_seconds = 2
    state.usage.model_calls = 1

    def no_model(_):
        pytest.fail("receipt recovery admitted inference beyond budget")

    monkeypatch.setattr(pipeline, "_model", no_model)

    class RecoveringPublisher:
        def recover_existing_merge(self, current, changes, persist):
            changes[0].status = "published"
            current.publication.merged = True
            current.publication.merge_commit = "a" * 40
            persist()
            return object()

    pipeline._publisher = RecoveringPublisher()
    done = await pipeline.run(state)
    assert done.status == "published"
    assert done.usage.model_calls == 1
    assert done.usage.active_seconds >= 2
    assert pipeline.store.load(state.job_id).publication.merged
