"""Legacy absent IDs require fresh exact placement, never fuzzy correction."""

import copy
import json

import pytest
from conftest import run_git

from doclayer import frontmatter
from doclayer.curator import curate, gittools
from doclayer.curator.model import ModelError
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.plan_readmission import readmit_invalid_saved
from doclayer.curator.state import Evidence, JobState, PageChange, SourceRecord

FACT = "The enterprise device retains audit records for seven days."
IDENTITY = "claim-source-backed"
BAD = "claim-phantom"
PATH = "wiki/existing-model.md"


class Planner:
    def __init__(self, *, target=PATH, covered=False, malformed=False):
        self.target = target
        self.covered = covered
        self.malformed = malformed
        self.calls = []

    async def chat_bounded_json(self, system, user, *, char_budget):
        assert len(system) + len(user) <= char_budget
        self.calls.append(user)
        if user.startswith("CANONICAL CLAIM COVERAGE"):
            record = json.loads(user.split("Claim JSON:\n")[1].split("\nPage metadata:")[0])
            body = user.split("Canonical passage:\n")[1]
            present = record["claim"] in body
            return {
                "covered": present,
                "consistent": True,
                "anchor": record["claim"] if present else "",
                "qualifiers": [],
                "reason": "Exact complete contextual fact.",
            }
        selected = json.loads(user.split("Claims JSON:\n")[1])
        assert BAD not in [r["id"] for r in selected]
        if self.covered:
            return {"pages": [], "covered": [{"claim_id": r["id"], "path": PATH} for r in selected]}
        return {
            "pages": [
                {
                    "path": self.target,
                    "action": "update",
                    "title": "Existing model",
                    "subject_type": "product",
                    "rationale": "Scoped real facts.",
                    "source_ids": ["manual"],
                    "claim_ids": [BAD if self.malformed else r["id"] for r in selected],
                    "related": [],
                }
            ],
            "covered": [],
        }


def setup(cfg, store, *, missing=True):
    pipe = Pipeline(cfg, store)
    state = store.create(JobState("", "readmission-proof", "Audit the enterprise device"))[0]
    state.stage = "draft"
    state.stages_done = ["extract", "place"]
    state.base_commit = run_git(cfg.paths.brain_checkout, "rev-parse", "HEAD")
    source = SourceRecord("manual", "text", "Operator manual", role="primary", status="fetched")
    state.sources = [source]
    text = "# Device\n" + FACT + "\n"
    pipe._store_extract(state, source, text)
    record = Evidence(
        "manual", "Operator manual", "# Device", "quote", FACT, quote=FACT, claim_id=IDENTITY, role="primary"
    )
    store.append_evidence(state.job_id, [record.to_dict()])
    state.evidence_verification[IDENTITY] = {
        "status": "supported",
        "supported": True,
        "support_version": curate.SOURCE_SUPPORT_VERSION,
        "source_binding": curate.source_support_binding(record, {"manual": text}),
        "excerpt": FACT,
    }
    state.changes = [
        PageChange(
            "g1",
            PATH,
            "update",
            title="Existing model",
            source_ids=["manual"],
            claim_ids=[IDENTITY] + ([BAD] if missing else []),
            base_commit=state.base_commit,
        ),
        PageChange(
            "g2",
            "wiki/other-page.md",
            "update",
            title="Other page",
            status="drafted",
            draft_body="# Other page\nOperator draft intact.\n",
            draft_meta={"concept_id": "c-2222bbbb"},
            draft_progress={"checkpoint": "unchanged"},
            check_progress={"validated-cache": "unchanged"},
            base_commit=state.base_commit,
        ),
    ]
    store.save(state)
    return pipe, state, record


@pytest.mark.asyncio
async def test_absent_id_replanned_from_real_scope_sibling_and_publication_unchanged(cfg, store):
    pipe, state, _ = setup(cfg, store)
    sibling = copy.deepcopy(state.changes[1].to_dict())
    publication = copy.deepcopy(state.publication.to_dict())
    base = state.base_commit
    model = Planner()
    await readmit_invalid_saved(pipe, state, model)
    change = state.changes[0]
    saved = state.evidence_selection["plan_readmission"]["g1"]
    assert len(model.calls) == 1 and change.claim_ids == [IDENTITY] and change.status == "pending"
    assert saved["original"]["claim_ids"] == [IDENTITY, BAD] and BAD in saved["invalid_claims"]
    assert saved["phase"] == "admitted" and saved["required_targets"] == {IDENTITY: PATH}
    assert (
        state.changes[1].to_dict() == sibling
        and state.base_commit == base
        and state.publication.to_dict() == publication
    )
    assert store.load(state.job_id).changes[0].claim_ids == [IDENTITY]


@pytest.mark.asyncio
async def test_known_rejected_identity_never_triggers_missing_id_migration(cfg, store):
    pipe, state, _ = setup(cfg, store, missing=False)
    state.evidence_verification[IDENTITY].update(
        status="unsupported", supported=False, reason="Source rejects the assertion"
    )
    model = Planner()
    await readmit_invalid_saved(pipe, state, model)
    assert not model.calls and "plan_readmission" not in state.evidence_selection
    assert state.changes[0].claim_ids == [IDENTITY]


@pytest.mark.asyncio
async def test_absent_plus_known_rejected_keeps_real_obligation_blocked(cfg, store):
    pipe, state, _ = setup(cfg, store)
    state.evidence_verification[IDENTITY].update(
        status="unsupported", supported=False, reason="Actual source contradiction"
    )
    model = Planner()
    with pytest.raises(ModelError, match="known required source obligations unavailable"):
        await readmit_invalid_saved(pipe, state, model)
    assert not model.calls and state.changes[0].claim_ids == [IDENTITY, BAD]
    assert state.evidence_selection["plan_readmission"]["g1"]["phase"] == "known-obligation-unavailable"


@pytest.mark.asyncio
async def test_immutable_commit_never_replanned(cfg, store):
    pipe, state, _ = setup(cfg, store)
    state.publication.commit_sha = "f" * 40
    model = Planner()
    with pytest.raises(ModelError, match="immutable publication"):
        await readmit_invalid_saved(pipe, state, model)
    assert not model.calls and state.changes[0].claim_ids == [IDENTITY, BAD]


@pytest.mark.asyncio
async def test_exact_current_placement_rejects_new_invented_ids(cfg, store):
    pipe, state, _ = setup(cfg, store)
    model = Planner(malformed=True)
    with pytest.raises(ModelError, match="placement"):
        await readmit_invalid_saved(pipe, state, model)
    assert len(model.calls) == 3 and state.changes[0].claim_ids == [IDENTITY, BAD]
    assert state.evidence_selection["plan_readmission"]["g1"]["phase"] == "replanning"


@pytest.mark.asyncio
async def test_saved_model_batch_recovers_without_replaying_request(cfg, store):
    pipe, state, _ = setup(cfg, store)
    model = Planner()
    original_persist = pipe._persist
    interrupted = False

    def persist(current, **kw):
        nonlocal interrupted
        original_persist(current, **kw)
        ledger = current.evidence_selection.get("plan_readmission", {}).get("g1", {}).get("ledger", {})
        if not interrupted and ledger.get("batch"):
            interrupted = True
            raise ModelError("simulated worker outage", transient=True)

    pipe._persist = persist
    with pytest.raises(ModelError, match="worker outage"):
        await readmit_invalid_saved(pipe, state, model)
    assert len(model.calls) == 1
    resumed = store.load(state.job_id)
    again = Planner()
    await readmit_invalid_saved(Pipeline(cfg, store), resumed, again)
    assert not again.calls and resumed.changes[0].claim_ids == [IDENTITY]


@pytest.mark.asyncio
async def test_target_change_is_explicit_and_original_target_still_audited(cfg, store):
    pipe, state, _ = setup(cfg, store)
    model = Planner(target="wiki/device-audit.md")
    old = gittools.read_blob(cfg.paths.brain_checkout, state.base_commit, PATH)
    await readmit_invalid_saved(pipe, state, model)
    saved = state.evidence_selection["plan_readmission"]["g1"]
    original = state.changes[0]
    replacement = state.changes[2]
    assert original.status == "drafted" and original.draft_body == frontmatter.parse(old)[1]
    assert original.claim_ids == [] and replacement.claim_ids == [IDENTITY]
    assert replacement.path == "wiki/device-audit.md" and replacement.action == "create"
    assert saved["required_targets"] == {IDENTITY: replacement.path} and saved["original_target_preserved_for_audit"]
    assert saved["original"]["claim_ids"] == [IDENTITY, BAD]


@pytest.mark.asyncio
async def test_all_covered_still_retains_full_original_target_for_validation(cfg, store):
    pipe, state, _ = setup(cfg, store)
    raw = gittools.read_blob(cfg.paths.brain_checkout, state.base_commit, PATH)
    meta, body = frontmatter.parse(raw)
    checkout = cfg.paths.brain_checkout
    from pathlib import Path

    Path(checkout, PATH).write_text(frontmatter.build(meta, body + "\n" + FACT + "\n"))
    run_git(checkout, "add", PATH)
    run_git(checkout, "commit", "-qm", "Operator fact")
    run_git(checkout, "push", "-q", "origin", "main")
    model = Planner(covered=True)
    await readmit_invalid_saved(pipe, state, model)
    change = state.changes[0]
    assert change.status == "drafted" and change.claim_ids == [IDENTITY] and FACT in change.draft_body
    assert state.evidence_selection["plan_readmission"]["g1"]["ledger"]["claims"][IDENTITY]["status"] == "covered"
    assert change.base_commit != state.base_commit and change.base_blob == gittools.blob_hash(
        checkout, f"{change.base_commit}:{PATH}"
    )


@pytest.mark.asyncio
async def test_known_missing_source_remains_pending_not_replaced(cfg, store):
    pipe, state, _ = setup(cfg, store)
    pipe._cache_file(state, state.sources[0]).unlink()
    state.evidence_verification = {}
    model = Planner()
    with pytest.raises(ModelError, match="selected source support unavailable"):
        await readmit_invalid_saved(pipe, state, model)
    assert not model.calls and state.changes[0].claim_ids == [IDENTITY, BAD]
    assert state.evidence_verification[IDENTITY]["status"] == "unknown"


@pytest.mark.asyncio
async def test_changed_semantic_policy_is_known_unavailable_not_silent_retirement(cfg, store):
    pipe, state, _ = setup(cfg, store)
    state.source_policy = {"global": {"forbidden_claims": ["audit records"]}}
    model = Planner()
    with pytest.raises(ModelError, match="known required source obligations unavailable"):
        await readmit_invalid_saved(pipe, state, model)
    assert not model.calls and state.changes[0].claim_ids == [IDENTITY, BAD]


@pytest.mark.asyncio
async def test_replacement_collision_never_mutates_other_existing_group(cfg, store):
    pipe, state, _ = setup(cfg, store)
    sibling = copy.deepcopy(state.changes[1].to_dict())
    original = state.changes[0].claim_ids[:]
    with pytest.raises(ModelError, match="conflicts with another preserved group"):
        await readmit_invalid_saved(pipe, state, Planner(target=state.changes[1].path))
    assert state.changes[0].claim_ids == original and state.changes[1].to_dict() == sibling


@pytest.mark.asyncio
async def test_real_admitted_call_is_durable_and_cached_plan_needs_no_budget(cfg, store):
    import httpx

    from doclayer.curator.model import ModelClient

    pipe, state, _ = setup(cfg, store)
    requests = []

    def handler(request):
        requests.append(request)
        selected = json.loads(json.loads(request.content)["messages"][1]["content"].split("Claims JSON:\n")[1])
        payload = {
            "pages": [
                {
                    "path": PATH,
                    "action": "update",
                    "title": "Existing model",
                    "subject_type": "product",
                    "rationale": "Actual scoped source fact.",
                    "source_ids": ["manual"],
                    "claim_ids": [r["id"] for r in selected],
                    "related": [],
                }
            ],
            "covered": [],
        }
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}, "finish_reason": "stop"}]}
        )

    client = ModelClient(
        cfg,
        state.usage,
        limits={"max_model_calls": 1},
        transport=httpx.MockTransport(handler),
        admitted_call=lambda: store.save(state),
    )
    normal = pipe._persist
    interrupted = False

    def persist(current, **kw):
        nonlocal interrupted
        normal(current, **kw)
        if not interrupted and current.evidence_selection.get("plan_readmission", {}).get("g1", {}).get(
            "ledger", {}
        ).get("batch"):
            interrupted = True
            raise ModelError("worker stopped after model checkpoint", transient=True)

    pipe._persist = persist
    with pytest.raises(ModelError, match="worker stopped"):
        await readmit_invalid_saved(pipe, state, client)
    resumed = store.load(state.job_id)
    assert resumed.usage.model_calls == 1 and len(requests) == 1
    fresh = ModelClient(cfg, resumed.usage, limits={"max_model_calls": 1}, transport=httpx.MockTransport(handler))
    await readmit_invalid_saved(Pipeline(cfg, store), resumed, fresh)
    assert resumed.usage.model_calls == 1 and len(requests) == 1 and resumed.changes[0].claim_ids == [IDENTITY]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["drafted", "failed", "validated"])
async def test_uncommitted_later_stage_invalid_plan_is_readmitted_without_manual_reset(cfg, store, status):
    pipe, state, _ = setup(cfg, store)
    change = state.changes[0]
    change.status = status
    change.draft_body = "# Existing model\nPartial generated fact.\n"
    change.draft_progress = {"editor_version": 3, "handled_claim_ids": [BAD]}
    change.repair_passes = 2
    model = Planner()
    await readmit_invalid_saved(pipe, state, model)
    assert change.status == "pending" and change.claim_ids == [IDENTITY]
    saved = state.evidence_selection["plan_readmission"]["g1"]
    assert saved["original"]["draft_body"] == "# Existing model\nPartial generated fact.\n"
    assert saved["original"]["draft_progress"]["handled_claim_ids"] == [BAD]
    assert change.repair_passes == 2


@pytest.mark.asyncio
async def test_validate_stage_routes_replanned_group_through_normal_draft(cfg, store):
    pipe, state, _ = setup(cfg, store)
    state.stage = "validate"
    state.changes[0].status = "drafted"
    state.changes[0].draft_body = "# Existing model\nUnvalidated draft.\n"
    model = Planner()
    pipe._model = lambda current: model
    assert await pipe._stage_validate(state) == "draft"
    assert state.changes[0].status == "pending" and state.changes[0].claim_ids == [IDENTITY]
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_publish_stage_readmission_occurs_after_readonly_receipt_recovery(cfg, store):
    pipe, state, _ = setup(cfg, store)
    state.stage = "publish"
    state.changes[0].status = "validated"

    class Publisher:
        queried = False

        def recover_existing_merge(self, current, validated, persist):
            self.queried = True
            return None

    publisher = Publisher()
    pipe._publisher = publisher
    model = Planner()
    pipe._model = lambda current: model
    sibling = copy.deepcopy(state.changes[1].to_dict())
    assert await pipe._stage_publish(state) == "validate"
    assert publisher.queried and not model.calls
    assert state.changes[1].to_dict() == sibling
    # Saved drafts finish ordinary validation before any new publication batch;
    # validation still readmits invalid saved assignments through normal draft.
    assert await pipe._stage_validate(state) == "draft"
    assert state.changes[0].status == "pending" and len(model.calls) == 1
    assert state.changes[1].to_dict() == sibling


@pytest.mark.asyncio
async def test_already_merged_receipt_never_needs_invalid_plan_model_repair(cfg, store):
    pipe, state, _ = setup(cfg, store)
    state.stage = "publish"
    state.changes[0].status = "validated"

    class Publisher:
        def recover_existing_merge(self, current, validated, persist):
            current.publication.merged = True
            current.publication.merge_commit = "f" * 40
            for change in validated:
                change.status = "published"
            persist()
            return {"merged": True}

    pipe._publisher = Publisher()

    def no_model(current):
        pytest.fail("Read-only actual merged receipt precedes editorial replan")

    pipe._model = no_model
    assert await pipe._stage_publish(state) == "report"
    assert state.changes[0].claim_ids == [IDENTITY, BAD] and state.publication.merged
    assert "plan_readmission" not in state.evidence_selection


@pytest.mark.asyncio
async def test_multiple_bounded_batches_merge_only_real_new_obligations(cfg, store):
    pipe, state, first = setup(cfg, store)
    records = [first] + [
        Evidence(
            "manual",
            "Operator manual",
            "# Device",
            "quote",
            f"Enterprise retention setting {i} is {i + 3} days.",
            quote=f"Enterprise retention setting {i} is {i + 3} days.",
            claim_id=f"claim-setting-{i}",
            role="primary",
        )
        for i in range(12)
    ]
    text = "# Device\n" + "\n".join(r.text for r in records) + "\n"
    pipe._store_extract(state, state.sources[0], text)
    store.append_evidence(state.job_id, [r.to_dict() for r in records[1:]])
    state.evidence_verification = {
        curate.evidence_id(r): {
            "status": "supported",
            "supported": True,
            "support_version": curate.SOURCE_SUPPORT_VERSION,
            "source_binding": curate.source_support_binding(r, {"manual": text}),
            "excerpt": r.quote,
        }
        for r in records
    }
    real = [curate.evidence_id(r) for r in records]
    state.changes[0].claim_ids = real + [BAD]
    model = Planner()
    await readmit_invalid_saved(pipe, state, model)
    assert len(model.calls) == 2 and set(state.changes[0].claim_ids) == set(real)
    assert len(state.evidence_selection["plan_readmission"]["g1"]["replacement_pages"]) == 1
    assert set(state.evidence_selection["plan_readmission"]["g1"]["required_targets"]) == set(real)


@pytest.mark.asyncio
async def test_draft_reads_fresh_group_base_while_other_group_baseline_stays_old(cfg, store):
    pipe, state, _ = setup(cfg, store)
    base = state.base_commit
    sibling = copy.deepcopy(state.changes[1].to_dict())
    from pathlib import Path

    old = gittools.read_blob(cfg.paths.brain_checkout, base, PATH)
    meta, body = frontmatter.parse(old)
    Path(cfg.paths.brain_checkout, PATH).write_text(frontmatter.build(meta, body + "\nFresh operator paragraph.\n"))
    run_git(cfg.paths.brain_checkout, "add", PATH)
    run_git(cfg.paths.brain_checkout, "commit", "-qm", "Fresh operator edit")
    run_git(cfg.paths.brain_checkout, "push", "-q", "origin", "main")

    class DraftPlanner(Planner):
        async def chat_json(self, system, user):
            assert "Fresh operator paragraph." in user
            return {
                "frontmatter": json.loads(json.dumps(meta, default=str)),
                "body": body + "\nFresh operator paragraph.\n" + FACT + "\n",
                "notes": [],
            }

    model = DraftPlanner()
    pipe._model = lambda current: model
    assert await pipe._stage_draft(state) == "validate"
    assert "Fresh operator paragraph." in state.changes[0].draft_body
    assert state.changes[0].base_commit != base and state.base_commit == base
    assert state.changes[1].to_dict() == sibling


@pytest.mark.asyncio
async def test_model_cannot_expand_replacement_source_scope(cfg, store):
    pipe, state, _ = setup(cfg, store)

    class ExtraSource(Planner):
        async def chat_bounded_json(self, *args, **kwargs):
            reply = await super().chat_bounded_json(*args, **kwargs)
            reply["pages"][0]["source_ids"].append("unsubmitted-hosted-provider")
            return reply

    model = ExtraSource()
    with pytest.raises(ModelError, match="placement"):
        await readmit_invalid_saved(pipe, state, model)
    assert len(model.calls) == 3 and state.changes[0].source_ids == ["manual"]
    assert state.changes[0].claim_ids == [IDENTITY, BAD]
