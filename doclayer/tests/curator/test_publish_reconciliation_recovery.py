"""Publication audit checkpoints survive a restart only for the bound candidate."""

import copy
import hashlib
import json
import re

import pytest
from conftest import run_git, valid_meta
from test_draft_claim_grounding import ArticleModel
from test_publish import git_show
from test_recovery_safety import validation_job

from doclayer import frontmatter as fm
from doclayer.curator import curate, gittools, ops, reconciliation
from doclayer.curator.coverage import assess_coverage
from doclayer.curator.model import ModelError
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.publish import Publisher, PublishOutcome, ReconcileFailed
from doclayer.curator.state import Evidence, SourceRecord
from doclayer.curator.validate import Failure


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_publication_resumes_same_candidate_audit_and_invalidates_changed_fresh_body(
    cfg, store, checkout, monkeypatch, changed
):
    fact = "Device qualified fact 0."
    added = "Device qualified fact 1."
    original_body = "# Device\n" + fact + "\n"
    original_meta = valid_meta("c-abcdef12", title="Device")
    (checkout / "wiki/existing-model.md").write_text(fm.build(original_meta, original_body))
    run_git(checkout, "add", "wiki/existing-model.md")
    run_git(checkout, "commit", "-qm", "canonical original with supported facts")
    state = validation_job(store, checkout)
    state.stage = "publish"
    state.status = "publishing"
    change = state.changes[0]
    change.status = "validated"
    change.path = "wiki/existing-model.md"
    change.action = "update"
    change.base_blob = run_git(checkout, "rev-parse", f"{state.base_commit}:{change.path}")
    change.source_ids = ["primary"]
    change.draft_meta = copy.deepcopy(original_meta)
    change.draft_body = original_body
    record = Evidence(
        "primary",
        "manual",
        "#Manual",
        "quote",
        fact,
        quote=fact,
        support_status="supported",
    )
    additional = Evidence("primary", "manual", "#Manual", "quote", added, quote=added, support_status="supported")
    records = [record, additional]
    change.claim_ids = [curate.evidence_id(record)]
    state.sources = [SourceRecord("primary", "text", "manual")]
    store.append_evidence(state.job_id, [item.to_dict() for item in records])
    pipeline = Pipeline(cfg, store)
    source_text = "# Manual\n" + fact + "\n" + added
    pipeline._store_extract(state, state.sources[0], source_text)
    for item in records:
        state.evidence_verification[curate.evidence_id(item)] = {
            "supported": True,
            "status": "supported",
            "excerpt": item.text,
            "support_version": curate.SOURCE_SUPPORT_VERSION,
            "source_binding": curate.source_support_binding(item, {"primary": source_text}),
        }
    store.save(state)
    cfg.limits.prompt_char_budget = 12000
    current_body = [original_body + added + "\n"]
    # Prime the exact old-body article and canonical preservation review. Failure injection then reaches the DISTINCT
    # candidate checker rather than spending its allowance on preflight.
    primed = ArticleModel()
    checked = await curate.check_draft(
        primed,
        state.intent + f"\nBinding source/applicability policy: {state.source_policy}",
        change,
        pipeline._page_records(change, pipeline._evidence(state)),
        [],
        char_budget=12000,
        support_resolver=pipeline._support_resolver(state, primed),
        source_policy=state.source_policy,
        **pipeline._editorial_original(state, change),
    )
    assert not checked["failures"]
    await pipeline._preserve_existing(state, change, primed, pipeline._evidence(state), [])
    preflight_binding = change.check_progress["article_review"]["binding"]
    store.save(state)

    async def merge(*args, **kwargs):
        # Independent unit tests exercise the bounded three-way assembler; this
        # integration test exercises actual publication full-body checking.
        return {"body": current_body[0], "frontmatter": copy.deepcopy(original_meta)}

    monkeypatch.setattr(reconciliation, "reconcile_stale", merge)
    captured = []

    class Publisher:
        def publish_changes(self, current, changes, reconciler, revalidate, persist):
            nonlocal preflight_binding
            preflight_binding = changes[0].check_progress["article_review"]["binding"]
            captured.append(reconciler(changes[0], fm.build(original_meta, original_body), False))
            return PublishOutcome()

    class Traced(ArticleModel):
        def __init__(self, interrupt=False):
            super().__init__()
            self.interrupt = interrupt
            self.candidate_reviews = 0

        async def chat_json(self, system, user):
            frame = json.loads(user.split("\n", 1)[1])
            if frame["mode"] == "grounding" and added in frame["body"]:
                self.candidate_reviews += 1
            # The candidate's actual article review has completed and persisted;
            # interrupt its distinct preservation review before publication.
            if self.interrupt and frame["mode"] == "preservation" and added in frame["body"]:
                raise ModelError("inference outage", transient=True)
            result = await super().chat_json(system, user)
            unsupported = "Unsupported operator-specific new assertion."
            if unsupported in frame["body"]:
                result["failures"].append(
                    {
                        "severity": "blocker",
                        "check": "article-grounding",
                        "detail": "The changed candidate adds an unsupported assertion.",
                        "section_id": frame["target_section_ids"][0],
                        "anchor": unsupported,
                    }
                )
            return result

    models = [Traced(interrupt=True)]
    monkeypatch.setattr(Pipeline, "_model", lambda *args: models[-1])
    pipeline._publisher = Publisher()
    with pytest.raises(ModelError, match="inference outage"):
        await pipeline._stage_publish(state)
    saved = store.load(state.job_id)
    checkpoint = copy.deepcopy(saved.changes[0].reconcile_check_progress)
    assert checkpoint["article_review"]["results"]
    assert checkpoint["existing_preservation"]["results"] == {}
    assert checkpoint["article_review"]["binding"] != preflight_binding
    assert saved.changes[0].check_progress["article_review"]["binding"] == preflight_binding
    assert models[0].candidate_reviews == 1
    assert saved.changes[0].draft_body == change.draft_body and not captured
    if changed:
        current_body[0] += "Unsupported operator-specific new assertion.\n"
    resumed = Traced()
    models.append(resumed)
    if changed:
        with pytest.raises(ReconcileFailed, match="evidence/applicability"):
            await pipeline._stage_publish(saved)
        assert resumed.candidate_reviews > 0 and not captured
        assert (
            saved.changes[0].reconcile_check_progress["article_review"]["binding"]
            != checkpoint["article_review"]["binding"]
        )
    else:
        await pipeline._stage_publish(saved)
        assert resumed.candidate_reviews == 0 and len(captured) == 1
        assert saved.changes[0].check_progress["article_review"]["binding"] == preflight_binding
        assert captured[0]["body"] == current_body[0]
        assert captured[0]["check_progress"]["article_review"]["binding"] == checkpoint["article_review"]["binding"]
        assert captured[0]["check_progress"]["article_review"]["complete"]
        assert not captured[0]["check_progress"]["article_review"]["failures"]
        assert captured[0]["check_progress"]["existing_preservation"]["complete"]


KEPT, LOST = "Device qualified fact 0.", "Device qualified fact 1."
CITE = " [source: manual #Manual]"
GAP_TAIL = "superseded by fresh publication; continue to re-draft against current page"


def overlap_job(cfg, store, checkout, origin, *, rest_valid=True):
    """A validated update whose Notes section overlaps a fresh operator edit
    already on origin/main, plus a non-overlapping intro addition."""
    original = "# Device\nIntro.\n\n## Notes\nOriginal note.\n"
    fresh = "# Device\nIntro.\n\n## Notes\nOperator note.\n" + (
        "" if rest_valid else "\n## Operator extra\nNew unsupported statement.\n"
    )
    meta = valid_meta("c-abcdef12", title="Device")
    path = "wiki/existing-model.md"
    (checkout / path).write_text(fm.build(meta, original))
    run_git(checkout, "commit", "-qam", "canonical original")
    run_git(checkout, "push", "-q", "origin", "main")
    state = validation_job(store, checkout)
    (checkout / path).write_text(fm.build(meta, fresh))
    run_git(checkout, "commit", "-qam", "operator edit after drafting")
    run_git(checkout, "push", "-q", "origin", "main")
    state.stage, state.status = "publish", "publishing"
    change = state.changes[0]
    change.status, change.path, change.action = "validated", path, "update"
    change.base_blob = run_git(checkout, "rev-parse", f"{state.base_commit}:{path}")
    change.source_ids = ["primary"]
    change.draft_meta = copy.deepcopy(meta)
    change.draft_body = original.replace("Intro.", "Intro. " + KEPT + CITE).replace("Original note.", LOST + CITE)
    records = [
        Evidence("primary", "manual", "#Manual", "quote", text, quote=text, support_status="supported")
        for text in (KEPT, LOST)
    ]
    ids = [curate.evidence_id(record) for record in records]
    change.claim_ids = list(ids)
    # Accepted drafting disposition anchors: only LOST lies in the dropped section.
    change.draft_progress = {"claim_anchors": {ids[0]: KEPT, ids[1]: LOST}}
    state.sources = [SourceRecord("primary", "text", "manual")]
    store.append_evidence(state.job_id, [item.to_dict() for item in records])
    pipeline = Pipeline(cfg, store)
    source_text = "# Manual\n" + KEPT + "\n" + LOST
    pipeline._store_extract(state, state.sources[0], source_text)
    for item in records:
        state.evidence_verification[curate.evidence_id(item)] = {
            "supported": True,
            "status": "supported",
            "excerpt": item.text,
            "support_version": curate.SOURCE_SUPPORT_VERSION,
            "source_binding": curate.source_support_binding(item, {"primary": source_text}),
        }
    store.save(state)
    cfg.limits.prompt_char_budget = 12000
    return state, ids


class OverlapModel(ArticleModel):
    """Article reviews as usual; every reconciliation reply rewrites the fresh
    operator line, which the protected-material check refuses."""

    def __init__(self):
        super().__init__()
        self.merges = 0

    async def chat_json(self, system, user):
        if system.startswith("Reconcile one bounded draft contribution"):
            self.merges += 1
            return {
                "section_id": re.search(r"Section ID: (\S+)", user).group(1),
                "edits": [{"old": "Operator note.", "new": LOST}],
                "append": "",
                "handled_claim_ids": [],
                "claim_dispositions": [],
            }
        return await super().chat_json(system, user)


class WorkerKilled(Exception):
    """A simulated worker death at a chosen publication step."""


def real_publisher(cfg, store, forgejo, monkeypatch, crash_at=None):
    """The actual publisher against the bare origin; `crash_at` kills the
    worker once, right before that step (simulated restart)."""
    publisher = Publisher(cfg, store, forgejo=forgejo)
    pushed = Publisher._ensure_pushed

    def push(self, state, branch, persist_fn):
        if crash_at == "push" and not crashed:
            crashed.append(True)
            raise WorkerKilled("worker killed after the reconciled commit was saved")
        pushed(self, state, branch, persist_fn)
        forgejo.branches.add(branch)

    commit = gittools.commit_all

    def commit_all(*args, **kwargs):
        if crash_at == "commit" and not crashed:
            crashed.append(True)
            raise WorkerKilled("worker killed before the reconciled body was saved")
        return commit(*args, **kwargs)

    crashed: list[bool] = []
    monkeypatch.setattr(Publisher, "_ensure_pushed", push)
    monkeypatch.setattr(gittools, "commit_all", commit_all)
    return publisher


def superseded_gaps(gaps):
    return [gap for gap in gaps if GAP_TAIL in gap]


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_at", [None, "commit", "push"])
async def test_overlap_only_editing_fresh_material_falls_back_and_rest_publishes(
    cfg, store, checkout, origin, forgejo, monkeypatch, crash_at
):
    """A draft section that can only be merged by editing fresh operator
    material keeps the fresh section exactly and drops its contribution; the
    rest publishes, and the job reports the section as a CURRENT coverage gap.
    A restart before or after the reconciled body is saved neither repeats nor
    contradicts the gap."""
    state, ids = overlap_job(cfg, store, checkout, origin)
    model = OverlapModel()
    monkeypatch.setattr(Pipeline, "_model", lambda *args: model)
    pipeline = Pipeline(cfg, store)
    pipeline._publisher = real_publisher(cfg, store, forgejo, monkeypatch, crash_at)
    if crash_at:
        with pytest.raises(WorkerKilled):
            await pipeline._stage_publish(state)
        state = store.load(state.job_id)  # restart: only durable state survives
        pipeline = Pipeline(cfg, store)
        pipeline._publisher = real_publisher(cfg, store, forgejo, monkeypatch)
    await pipeline._stage_publish(state)
    assert model.merges == 3  # exhausted once; a restart reuses the checkpointed fallback
    change = store.load(state.job_id).changes[0]
    assert change.status == "published", change.error
    landed = git_show(origin, "main:wiki/existing-model.md")
    assert fm.parse(landed)[1] == "# Device\nIntro. " + KEPT + CITE + "\n\n## Notes\nOperator note.\n"
    assert change.claim_ids == [ids[0]]  # only the claim anchored in the dropped section left
    assert [row["claim_ids"] for row in change.superseded_by_fresh] == [[ids[1]]]
    saved = store.load(state.job_id)
    gaps = superseded_gaps(assess_coverage(saved).current_gaps)
    digest = hashlib.sha256(f"## Notes\n{LOST}{CITE}\n".encode()).hexdigest()[:16]
    expected = (
        f"wiki/existing-model.md: draft section 'Notes' (draft text sha256 {digest}) with claims {ids[1]} {GAP_TAIL}"
    )
    assert gaps == [expected]
    assert saved.effective_coverage == "incomplete"
    result = ops.handle("result", {"job_id": state.job_id, "include_digest": False}, cfg, store)
    assert result["job"]["coverage"] == "incomplete"
    assert superseded_gaps(result["job"]["coverage_gaps"]) == gaps
    assert not superseded_gaps(saved.coverage_gaps)  # derived from the page record only


@pytest.mark.asyncio
async def test_overlap_fallback_with_failing_rest_fails_as_before_without_gap(
    cfg, store, checkout, origin, forgejo, monkeypatch
):
    """When the rest of the page does not validate, publication fails exactly
    as before: no superseded gap and the page keeps every assigned claim."""
    state, ids = overlap_job(cfg, store, checkout, origin, rest_valid=False)
    model = OverlapModel()
    monkeypatch.setattr(Pipeline, "_model", lambda *args: model)
    pipeline = Pipeline(cfg, store)
    pipeline._publisher = real_publisher(cfg, store, forgejo, monkeypatch)
    await pipeline._stage_publish(state)
    change = store.load(state.job_id).changes[0]
    assert model.merges == 3 and change.status == "conflict"
    assert "evidence/applicability" in change.error
    assert change.claim_ids == ids and change.superseded_by_fresh == []
    assert not superseded_gaps(assess_coverage(store.load(state.job_id)).current_gaps)


def test_failed_publish_clears_recorded_superseded_sections(cfg, store, checkout, origin, forgejo):
    """A reconciled page that then fails publication supersedes nothing."""
    path = "wiki/existing-model.md"
    state = validation_job(store, checkout)
    change = state.changes[0]
    change.status, change.path, change.action = "validated", path, "update"
    change.base_blob = "0" * 40  # canonical changed since drafting: reconcile
    change.draft_meta = valid_meta("c-1111aaaa")
    change.claim_ids = ["claim-a", "claim-b"]
    change.superseded_by_fresh = [{"section": "Old", "digest": "d0", "claim_ids": ["claim-c"]}]
    row = {"section": "Notes", "digest": "d1", "claim_ids": ["claim-b"]}

    def reconciler(change, fresh_page, collision):
        return {"meta": change.draft_meta, "body": "# merged\n", "superseded": [row]}

    def revalidate(change, fresh_meta, fresh_body):
        # The reconciled body and its record were applied together.
        assert change.superseded_by_fresh[-1] == row and change.claim_ids == ["claim-a"]
        return [Failure("scope", "operator content removed")]

    Publisher(cfg, store, forgejo=forgejo).publish_changes(
        state, [change], reconciler, revalidate, lambda: store.save(state)
    )
    saved = store.load(state.job_id).changes[0]
    assert saved.status == "conflict"
    assert saved.superseded_by_fresh == []
    assert saved.claim_ids == ["claim-a", "claim-c", "claim-b"]
    assert not superseded_gaps(assess_coverage(store.load(state.job_id)).current_gaps)


def test_staging_rejection_clears_recorded_superseded_sections(cfg, store, checkout, origin, forgejo):
    """A page rejected at staging after reconciliation published nothing."""
    state = validation_job(store, checkout)
    change = state.changes[0]
    change.status, change.path, change.action = "validated", "index.md", "update"
    change.base_blob = "0" * 40  # canonical changed since drafting: reconcile
    change.draft_meta = valid_meta("c-1111aaaa")
    change.claim_ids = ["claim-a", "claim-b"]
    row = {"section": "Notes", "digest": "d1", "claim_ids": ["claim-b"]}

    def reconciler(change, fresh_page, collision):
        return {"meta": change.draft_meta, "body": "# merged\n", "superseded": [row]}

    Publisher(cfg, store, forgejo=forgejo).publish_changes(
        state, [change], reconciler, lambda *args: [], lambda: store.save(state)
    )
    saved = store.load(state.job_id).changes[0]
    assert saved.status == "conflict" and "rejected at staging" in saved.error
    assert saved.superseded_by_fresh == [] and saved.claim_ids == ["claim-a", "claim-b"]


@pytest.mark.parametrize(
    "anchor, attributed",
    [
        ("Device qualified fact 1.", True),  # >= 20 characters, once in the section
        ("fact 1.", False),  # too short to attribute
        ("Device qualified fact", False),  # occurs twice in the section
    ],
)
def test_claim_attribution_needs_a_long_unique_anchor(cfg, store, checkout, anchor, attributed):
    state = validation_job(store, checkout)
    change = state.changes[0]
    change.claim_ids = ["claim-a"]
    change.draft_progress = {"claim_anchors": {"claim-a": anchor}}
    text = "## Notes\nDevice qualified fact 1. Device qualified fact 2.\n"
    rows = Pipeline(cfg, store)._superseded_sections(
        state, change, {"dropped": [{"view": "Notes", "text": text, "digest": "d"}]}
    )
    assert rows == [{"section": "Notes", "digest": "d", "claim_ids": ["claim-a"] if attributed else []}]
