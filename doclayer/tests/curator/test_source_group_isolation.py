"""Completed source uncertainty isolates a group; operational failures do not."""

import copy
import json

import httpx
import pytest
from conftest import run_git, valid_meta
from test_required_source_support import seed
from test_source_admission_pipeline import PipelineWire

from doclayer.curator import curate
from doclayer.curator.degrade import GAP_MARK
from doclayer.curator.pipeline import Pipeline, SourceSupportUnavailable
from doclayer.curator.publish import Publisher
from doclayer.curator.state import Evidence, JobState, PageChange, SourceRecord


def setup_groups(cfg, store):
    state, records, text = seed(cfg, store, 1, 1)
    record = Evidence(
        "ambiguous",
        "Regional manual",
        "# Eligibility",
        "quote",
        "Nimbus supports endpoint A.",
        quote="Nimbus",
    )
    source = SourceRecord(
        "ambiguous",
        "text",
        "Regional manual",
        status="fetched",
        extraction_complete=True,
    )
    ambiguous = "# Eligibility\nRegion one: Nimbus supports A.\n\n# Eligibility\nRegion two: Nimbus supports B."
    state.sources.append(source)
    state.changes.insert(
        0,
        PageChange(
            "unresolved",
            "wiki/lineup.md",
            "create",
            title="Lineup",
            source_ids=[source.id],
            claim_ids=[curate.evidence_id(record)],
        ),
    )
    pipeline = Pipeline(cfg, store)
    pipeline._store_extract(state, source, ambiguous)
    store.append_evidence(state.job_id, [record.to_dict()])
    state.stage = "draft"
    state.status = "queued"
    store.save(state)
    return state, records, record


def wire_for(records, heading_link=""):
    wire = PipelineWire()
    ordinary = wire.transport()

    def handler(request):
        system, user = [row["content"] for row in json.loads(request.content)["messages"]]
        if "Draft the full page." in user:
            wire.requests.append((system, user))
            result = {
                "frontmatter": dict(valid_meta(), title="Device"),
                "body": "# Device" + heading_link + "\n" + records[0].text + " [source: Device manual]",
                "notes": [],
            }
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": json.dumps(result)},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        return ordinary.handler(request)

    return httpx.MockTransport(handler), wire


@pytest.mark.asyncio
async def test_actual_full_pipeline_unknown_first_group_preserved_later_group_published(cfg, store, origin, forgejo):
    state, records, unknown = setup_groups(cfg, store)
    parent = store.create(JobState("", "parent", "Historical immutable parent"))[0]
    state.continuation_of = parent.job_id
    parent_bytes = store.state_path(parent.job_id).read_bytes()
    store.add_correction(
        {
            "page": "wiki/unrelated.md",
            "claim": "An unrelated correction",
            "instruction": "Preserve it",
        }
    )
    correction_bytes = store.corrections_path.read_bytes()
    original = copy.deepcopy(state.changes[0].to_dict())
    evidence_bytes = (store.job_dir(state.job_id) / "evidence.jsonl").read_bytes()
    transport, wire = wire_for(records)
    pipeline = Pipeline(
        cfg,
        store,
        inference_transport=transport,
        publisher=Publisher(cfg, store, forgejo=forgejo),
    )
    result = await pipeline.run(state)
    blocked, published = result.changes
    identity = curate.evidence_id(unknown)
    assert result.status == "partial" and result.coverage == "incomplete"
    assert blocked.status == "failed" and published.status == "published"
    assert blocked.claim_ids == original["claim_ids"] and blocked.source_ids == original["source_ids"]
    assert blocked.missing_evidence == [identity]
    proof = copy.deepcopy(result.evidence_verification[identity])
    assert proof["status"] == "unknown" and not proof.get("verification_pending")
    assert proof["support_version"] == curate.SOURCE_SUPPORT_VERSION and proof["source_binding"]
    assert result.publication.merged
    assert records[0].text in run_git(origin, "show", "main:wiki/device.md")
    assert "wiki/lineup.md" not in run_git(origin, "ls-tree", "-r", "--name-only", "main")
    assert (store.job_dir(state.job_id) / "evidence.jsonl").read_bytes() == evidence_bytes
    assert store.state_path(parent.job_id).read_bytes() == parent_bytes
    assert store.corrections_path.read_bytes() == correction_bytes
    resumed = store.load(state.job_id)
    assert resumed.evidence_verification[identity] == proof and resumed.changes[0].claim_ids == original["claim_ids"]
    assert any("Draft the full page." in user for _, user in wire.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["outage", "parser", "budget", "pending", "unbound", "cancel"])
async def test_operational_failure_remains_checkpoint_not_retired(cfg, store, monkeypatch, failure):
    state, records, unknown = setup_groups(cfg, store)
    # The first group is a normal exact source, not final ambiguity.
    state.changes = [state.changes[1]]
    before = copy.deepcopy(state.changes[0].to_dict())
    if failure == "budget":
        state.limits["max_model_calls"] = 0
    if failure == "cancel":
        state.cancel_requested = True
    if failure in ("pending", "unbound"):

        async def pending(client, batch, texts, *, progress, **kwargs):
            progress(
                {
                    curate.evidence_id(record): {
                        "status": "unknown",
                        "supported": False,
                        "verification_pending": failure == "pending",
                        "support_version": curate.SOURCE_SUPPORT_VERSION,
                        "source_binding": curate.source_support_binding(record, texts)
                        if failure == "pending"
                        else "foreign-binding",
                    }
                    for record in batch
                }
            )

        monkeypatch.setattr(curate, "verify_source_claims", pending)
    cfg.limits.transient_retries = 0

    def handler(request):
        if failure == "outage":
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{broken"}, "finish_reason": "stop"}]},
        )

    pipeline = Pipeline(cfg, store, inference_transport=httpx.MockTransport(handler))
    result = await pipeline.run(state)
    if failure == "parser":
        # A reply that never parses is a rejected reply, not an outage: each
        # unit is dropped as a current gap and the job reports (degrade.py).
        assert result.status != "retryable" and result.stage == "report" and result.error is None
        assert any(GAP_MARK in gap for gap in result.coverage_gaps)
        return
    assert result.status == ("cancelled" if failure == "cancel" else "retryable")
    assert result.stage == "draft" and result.changes[0].status == "pending"
    assert result.changes[0].claim_ids == before["claim_ids"]
    assert not result.changes[0].missing_evidence and not result.changes[0].validation


@pytest.mark.asyncio
async def test_completed_unknown_is_typed_but_pending_is_not(cfg, store):
    state, records, unknown = setup_groups(cfg, store)
    pipeline = Pipeline(cfg, store)
    with pytest.raises(SourceSupportUnavailable) as error:
        await pipeline._verify_selected(state, state.changes[0], pipeline._model(state))
    assert error.value.identities == [curate.evidence_id(unknown)]
    assert state.changes[0].status == "pending"


def test_coherence_transitively_blocks_dependants(cfg, store):
    state, _, _ = setup_groups(cfg, store)
    state.changes[0].status = "failed"
    state.changes[1].status = "validated"
    state.changes[1].draft_body = "[[wiki/middle]]"
    state.changes.append(
        PageChange(
            "middle",
            "wiki/middle.md",
            "create",
            status="validated",
            draft_body="[[wiki/lineup]]",
        )
    )
    Pipeline(cfg, store)._enforce_group_coherence(state)
    assert [change.status for change in state.changes] == [
        "failed",
        "conflict",
        "conflict",
    ]


@pytest.mark.asyncio
async def test_existing_canonical_failed_update_does_not_dangle_link(cfg, store, checkout, origin, forgejo):
    from doclayer import frontmatter

    target = checkout / "wiki/lineup.md"
    target.write_text(
        frontmatter.build(
            dict(valid_meta(), title="Lineup", concept_id="old-lineup"),
            "# Lineup\nOperator's old verified content.\n",
        )
    )
    run_git(checkout, "add", "wiki/lineup.md")
    run_git(checkout, "commit", "-m", "Existing operator lineup")
    run_git(checkout, "push", "origin", "main")
    old = run_git(origin, "show", "main:wiki/lineup.md")
    state, records, _ = setup_groups(cfg, store)
    state.changes[0].action = "update"
    transport, _ = wire_for(records, " [[wiki/lineup]]")
    result = await Pipeline(
        cfg,
        store,
        inference_transport=transport,
        publisher=Publisher(cfg, store, forgejo=forgejo),
    ).run(state)
    assert result.status == "partial", result.summary
    assert [change.status for change in result.changes] == ["failed", "published"]
    assert run_git(origin, "show", "main:wiki/lineup.md") == old
    assert "[[wiki/lineup]]" in run_git(origin, "show", "main:wiki/device.md")


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["validate", "publish"])
async def test_later_boundary_final_unknown_isolates_without_weakening_other_audits(
    cfg, store, origin, forgejo, boundary
):
    state, records, _ = setup_groups(cfg, store)
    transport, _ = wire_for(records)
    pipeline = Pipeline(
        cfg,
        store,
        inference_transport=transport,
        publisher=Publisher(cfg, store, forgejo=forgejo),
    )
    await pipeline._stage_draft(state)
    if boundary == "publish":
        await pipeline._stage_validate(state)
    state.changes[0].status = "drafted" if boundary == "validate" else "validated"
    state.stage = boundary
    state.status = "queued"
    result = await pipeline.run(state)
    assert result.status == "partial" and result.coverage == "incomplete"
    assert [change.status for change in result.changes] == ["failed", "published"]
    assert records[0].text in run_git(origin, "show", "main:wiki/device.md")


@pytest.mark.asyncio
async def test_committed_batch_cannot_publish_around_blocked_source(cfg, store):
    state, _, _ = setup_groups(cfg, store)
    state.stage = "publish"
    for change in state.changes:
        change.status = "validated"
    state.publication.commit_sha = "a" * 40

    class NoMutationPublisher:
        def recover_existing_merge(self, *args):
            return None

        def publish_changes(self, *args):
            raise AssertionError("immutable blocked batch must not merge")

    result = await Pipeline(cfg, store, publisher=NoMutationPublisher()).run(state)
    assert all(change.status == "conflict" for change in result.changes)
    assert result.publication.commit_sha == "a" * 40 and not result.publication.merged
    assert result.changes[0].missing_evidence


def test_qualified_link_does_not_alias_blocked_leaf(cfg, store):
    state, _, _ = setup_groups(cfg, store)
    state.changes[0].status = "failed"
    state.changes[1].status = "validated"
    state.changes[1].draft_body = "[[wiki/another/lineup]]"
    Pipeline(cfg, store)._enforce_group_coherence(state)
    assert state.changes[1].status == "validated"
    state.changes[1].draft_body = "[[lineup]]"
    Pipeline(cfg, store)._enforce_group_coherence(state)
    assert state.changes[1].status == "conflict"
