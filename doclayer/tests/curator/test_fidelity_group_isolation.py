"""Completed article blockers isolate a page; operational failures keep the job."""

import copy
import json

import httpx
import pytest
from conftest import run_git, valid_meta
from test_required_source_support import seed
from test_source_admission_pipeline import PipelineWire
from test_source_group_isolation import wire_for

from doclayer.curator import curate
from doclayer.curator.model import ModelError
from doclayer.curator.pipeline import Pipeline, SourceSupportUnavailable
from doclayer.curator.publish import Publisher
from doclayer.curator.state import PageChange
from doclayer.curator.validate import Failure


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["validate", "publish"])
async def test_completed_semantic_negative_first_page_does_not_hold_coherent_publication(
    cfg, store, origin, forgejo, boundary
):
    state, records, _ = seed(cfg, store, 1, 1)
    transport, _ = wire_for(records)
    pipeline = Pipeline(cfg, store, inference_transport=transport, publisher=Publisher(cfg, store, forgejo=forgejo))
    await pipeline._stage_draft(state)
    if boundary == "publish":
        await pipeline._stage_validate(state)
    broken = PageChange(
        "bad",
        "wiki/broken.md",
        "create",
        title="Broken",
        status="drafted" if boundary == "validate" else "validated",
        claim_ids=list(state.changes[0].claim_ids),
        source_ids=["manual"],
        draft_meta=valid_meta(cid="c-f1de0010"),
        draft_body="# Broken\nDevice costs5credits. [source: Device manual]\n",
    )
    state.changes.insert(0, broken)
    original = copy.deepcopy(broken.to_dict())
    source_proofs = copy.deepcopy(state.evidence_verification)
    ordinary = PipelineWire().transport()
    negative_frames, repair_requests = [], []

    def handler(request):
        system, user = [m["content"] for m in json.loads(request.content)["messages"]]
        if user.startswith("ARTICLE EDITORIAL REVIEW\n"):
            material = json.loads(user.split("\n", 1)[1])
            if material["page"] != "wiki/broken.md":
                return ordinary.handler(request)
            negative_frames.append(material)
            result = {
                "complete": True,
                "failures": [
                    {
                        "severity": "blocker",
                        "check": "article-grounding",
                        "detail": "The source does not support the five-credit price.",
                        "section_id": material["target_section_ids"][0],
                        "anchor": "Device costs5credits.",
                    }
                ],
                "conflicts": [],
            }
        elif "Current section view:\n" in user and "costs5credits" in user:
            repair_requests.append(user)
            import re

            section_id = re.search(r"Section ID: (.+)", user).group(1)
            result = {
                "section_id": section_id,
                "edits": [],
                "append": "",
                "handled_claim_ids": [],
                "claim_dispositions": [],
                "notes": ["Do not guess a replacement price."],
            }
        else:
            return ordinary.handler(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(result)}, "finish_reason": "stop"}]}
        )

    pipeline._inference_transport = httpx.MockTransport(handler)
    state.stage = boundary
    state.status = "queued"
    result = await pipeline.run(state)
    bad, good = result.changes
    assert result.status == "partial" and result.effective_coverage == "incomplete"
    assert bad.status == "failed" and good.status == "published"
    assert bad.claim_ids == original["claim_ids"] and bad.source_ids == original["source_ids"]
    assert bad.draft_body == original["draft_body"] and not bad.missing_evidence
    assert result.evidence_verification == source_proofs
    assert bad.validation[0]["check"] == "article-grounding"
    assert len(repair_requests) == 1  # identical no-op edit reuses its bounded checkpoint
    assert len(negative_frames) == 1  # unchanged article reuses its bound failure
    assert any("editorial review unresolved" in note for note in result.coverage_gaps)
    assert records[0].text in run_git(origin, "show", "main:wiki/device.md")
    assert "wiki/broken.md" not in run_git(origin, "ls-tree", "-r", "--name-only", "main")
    saved = store.load(result.job_id)
    assert saved.changes[0].check_progress == bad.check_progress
    assert bad.repair_passes == 2
    assert bad.check_progress["article_review"]["failures"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transient", [True, False])
async def test_generic_model_errors_are_not_completed_semantic_negatives(cfg, store, monkeypatch, transient):
    state, records, _ = seed(cfg, store, 1, 1)
    transport, _ = wire_for(records)
    pipeline = Pipeline(cfg, store, inference_transport=transport)
    await pipeline._stage_draft(state)
    state.stage = "validate"
    state.status = "queued"
    before = copy.deepcopy(state.changes[0].to_dict())

    async def fail(*args, **kwargs):
        raise ModelError("operational or schema failure", transient=transient)

    monkeypatch.setattr(curate, "check_draft", fail)
    result = await pipeline.run(state)
    assert result.status == ("retryable" if transient else "failed")
    assert result.changes[0].status == before["status"]
    assert not result.changes[0].validation and not result.publication.merged


def test_unmerged_committed_batch_cannot_drop_a_failed_review_member(cfg, store):
    state, _, _ = seed(cfg, store, 1, 1)
    state.changes.append(PageChange("other", "wiki/other.md", "create", status="validated"))
    state.publication.commit_sha = "a" * 40
    Pipeline(cfg, store)._isolate_review_group(
        state, state.changes[0], [Failure("article-grounding", "completed article blocker")]
    )
    assert all(change.status == "conflict" for change in state.changes)
    assert state.publication.commit_sha == "a" * 40 and not state.publication.merged
    assert not state.changes[0].missing_evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["source", "article"])
@pytest.mark.parametrize("later_status", ["drafted", "failed"])
async def test_recorded_bad_first_group_cannot_revalidate_drafted_peer_later(
    cfg, store, monkeypatch, kind, later_status
):
    state, _, _ = seed(cfg, store, 1, 1)
    first = state.changes[0]
    first.status = "drafted"
    first.draft_body = "# Device\nUnverified saved content. [source: Device manual]"
    first.draft_meta = valid_meta()
    later = copy.deepcopy(first)
    later.path = "wiki/later.md"
    later.group_id = "later"
    later.status = later_status
    state.changes.append(later)
    state.publication.commit_sha = "a" * 40
    state.publication.committed_paths = [first.path, later.path]
    pipeline = Pipeline(cfg, store)
    admissions = []

    async def support(current, change, client):
        admissions.append(change.path)
        if kind == "source":
            pipeline._isolate_source_group(current, change, SourceSupportUnavailable(change.claim_ids))
            return False
        return True

    async def check(*args, **kwargs):
        return {
            "complete": True,
            "failures": [
                {"severity": "blocker", "check": "article-grounding", "detail": "Unsupported saved assertion"}
            ],
            "conflicts": [],
        }

    monkeypatch.setattr(pipeline, "_verify_group_support", support)
    monkeypatch.setattr(curate, "check_draft", check)

    async def unchanged(client, intent, change, *args, **kwargs):
        return {"body": change.draft_body, "frontmatter": change.draft_meta}

    monkeypatch.setattr(curate, "repair_draft", unchanged)
    assert await pipeline._stage_validate(state) == "publish"
    assert admissions and set(admissions) == {first.path}
    assert first.status == later.status == "conflict"
    assert not state.publication.merged and state.publication.commit_sha == "a" * 40


@pytest.mark.parametrize("kind", ["source", "article"])
def test_failed_group_outside_known_commit_does_not_block_its_members(cfg, store, kind):
    state, _, _ = seed(cfg, store, 1, 1)
    good = state.changes[0]
    good.status = "validated"
    bad = copy.deepcopy(good)
    bad.path = "wiki/not-committed.md"
    bad.group_id = "bad"
    state.changes.insert(0, bad)
    state.publication.commit_sha = "a" * 40
    state.publication.committed_paths = [good.path]
    pipeline = Pipeline(cfg, store)
    if kind == "source":
        pipeline._isolate_source_group(state, bad, SourceSupportUnavailable(bad.claim_ids))
    else:
        pipeline._isolate_review_group(state, bad, [Failure("article-grounding", "completed article blocker")])
    assert bad.status == "failed" and good.status == "validated"
    assert state.publication.committed_paths == [good.path]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["validate", "publish", "operator-resume"])
async def test_complete_editorial_context_overflow_isolates_only_its_page(
    cfg, store, origin, forgejo, monkeypatch, boundary
):
    """A complete relevant quote cannot fit; coherent peers still publish."""
    from doclayer.curator.editorial import EditorialContextOverflow
    from doclayer.curator.state import Evidence

    state, records, _ = seed(cfg, store, 1, 1)
    transport, _ = wire_for(records)
    pipeline = Pipeline(cfg, store, inference_transport=transport, publisher=Publisher(cfg, store, forgejo=forgejo))
    await pipeline._stage_draft(state)
    if boundary == "publish":
        await pipeline._stage_validate(state)
    bad = copy.deepcopy(state.changes[0])
    bad.group_id = "oversized"
    bad.path = "wiki/oversized.md"
    bad.status = "validated" if boundary == "publish" else "drafted"
    bad.check_progress = {}
    bad.draft_progress = {}
    bad.draft_meta = valid_meta(cid="c-f1de0020")
    state.changes.insert(0, bad)
    before = copy.deepcopy(bad.to_dict())
    proofs = copy.deepcopy(state.evidence_verification)
    oversized = Evidence(
        "manual",
        "Device manual",
        "# Complete source",
        "quote",
        records[0].text,
        quote="Device complete relevant source. " * 2000,
        claim_id="complete-source",
        support_status="supported",
    )
    ordinary_check = curate.check_draft
    attempted = []

    async def complete_check(client, topic, change, evidence, *args, **kwargs):
        if change.path == bad.path:
            attempted.append(change.path)
            # Feed the actual planner a complete relevant record; neither
            # partial quote dispatch nor a fabricated negative is permitted.
            evidence = [*evidence, oversized]
            calls = state.usage.model_calls
            with pytest.raises(EditorialContextOverflow) as caught:
                await ordinary_check(client, topic, change, evidence, *args, **kwargs)
            assert state.usage.model_calls == calls  # packing fails before dispatch
            raise caught.value
        return await ordinary_check(client, topic, change, evidence, *args, **kwargs)

    monkeypatch.setattr(curate, "check_draft", complete_check)
    state.stage = "validate" if boundary == "operator-resume" else boundary
    state.status = "queued"
    if boundary == "operator-resume":
        from doclayer.curator.ops import _op_resume

        state.status = "failed"
        state.error = "inference: complete editorial section/context/source exceeds bounded input"
        state.stages_done = ["intake", "discover", "extract", "place", "draft"]
        store.save(state)
        saved_before = copy.deepcopy(state.to_dict())
        reply = _op_resume({"job_id": state.job_id}, store)
        assert reply["status"] == "queued" and reply["stage"] == "validate"
        state = store.load(state.job_id)
        assert state.usage.to_dict() == saved_before["usage"]
        assert state.limits == saved_before["limits"]
        assert [c.to_dict() for c in state.changes] == saved_before["changes"]
        assert state.stages_done == saved_before["stages_done"]
    result = await pipeline.run(state)
    failed, published = result.changes
    assert attempted == [bad.path]
    assert result.status == "partial" and result.effective_coverage == "incomplete"
    assert failed.status == "failed" and published.status == "published"
    assert failed.validation[0]["check"] == "editorial-context"
    assert failed.repair_passes == before["repair_passes"]
    assert failed.draft_body == before["draft_body"]
    assert failed.claim_ids == before["claim_ids"] and failed.source_ids == before["source_ids"]
    assert result.evidence_verification == proofs and not failed.missing_evidence
    assert any(bad.path in gap for gap in result.coverage_gaps)
    assert records[0].text in run_git(origin, "show", "main:wiki/device.md")
    assert bad.path not in run_git(origin, "ls-tree", "-r", "--name-only", "main")
    assert oversized.quote == "Device complete relevant source. " * 2000
    assert store.load(result.job_id).changes[0].validation == failed.validation


@pytest.mark.asyncio
async def test_cached_preservation_warning_publishes_without_repeating_reviews(cfg, store, checkout, origin, forgejo):
    from doclayer import frontmatter as fm

    state, records, _ = seed(cfg, store, 1, 1)
    change = state.changes[0]
    old_body = "# Device\nOverview of Device.\n" + records[0].text + " [source: Device manual]\n"
    (checkout / change.path).write_text(fm.build(valid_meta(), old_body))
    run_git(checkout, "add", change.path)
    run_git(checkout, "commit", "-qm", "canonical device facts")
    run_git(checkout, "push", "origin", "main")
    state.base_commit = run_git(checkout, "rev-parse", "HEAD")
    change.action = "update"
    transport, _ = wire_for(records)
    review_modes = []

    def handler(request):
        system, user = [m["content"] for m in json.loads(request.content)["messages"]]
        if user.startswith("ARTICLE EDITORIAL REVIEW\n"):
            frame = json.loads(user.split("\n", 1)[1])
            review_modes.append(frame["mode"])
            result = {"complete": True, "failures": [], "conflicts": []}
            if frame["mode"] == "preservation":
                result["failures"] = [
                    {
                        "severity": "warning",
                        "check": "existing-preservation",
                        "detail": "Nonessential overview wording omitted; the factual detail remains.",
                        "section_id": frame["sections"][0]["id"],
                        "anchor": None,
                    }
                ]
            return httpx.Response(
                200, json={"choices": [{"message": {"content": json.dumps(result)}, "finish_reason": "stop"}]}
            )
        return transport.handler(request)

    pipeline = Pipeline(
        cfg, store, inference_transport=httpx.MockTransport(handler), publisher=Publisher(cfg, store, forgejo=forgejo)
    )
    await pipeline._stage_draft(state)
    await pipeline._stage_validate(state)
    assert change.status == "validated" and change.repair_passes == 0
    warning = copy.deepcopy(change.check_progress["existing_preservation"]["failures"])
    assert warning[0]["severity"] == "warning" and "preservation" in review_modes
    calls, modes = state.usage.model_calls, list(review_modes)
    state.limits["max_model_calls"] = calls  # No remaining call may be admitted.
    state.stage = "publish"
    state.status = "queued"
    result = await pipeline.run(state)
    assert result.status == "published" and result.changes[0].status == "published"
    assert result.usage.model_calls == calls and review_modes == modes
    assert result.changes[0].check_progress["existing_preservation"]["failures"] == warning
    assert records[0].text in run_git(origin, "show", "main:" + change.path)


@pytest.mark.asyncio
@pytest.mark.parametrize("severity", ["blocker", None])
async def test_preservation_blocker_or_legacy_ungraded_failure_still_withholds_publication(
    cfg, store, monkeypatch, severity
):
    state, records, _ = seed(cfg, store, 1, 1)
    transport, _ = wire_for(records)
    pipeline = Pipeline(cfg, store, inference_transport=transport)
    await pipeline._stage_draft(state)
    await pipeline._stage_validate(state)
    failure = {"check": "existing-preservation", "detail": "An original supported fact was lost."}
    if severity is not None:
        failure["severity"] = severity

    async def rejected(*args, **kwargs):
        return {"failures": [failure], "conflicts": []}

    class NoPublisher:
        def publish_changes(self, *args):
            pytest.fail("A blocking preservation failure reached publication")

    monkeypatch.setattr(pipeline, "_preserve_existing", rejected)
    pipeline._publisher = NoPublisher()
    state.stage = "publish"
    assert await pipeline._stage_publish(state) == "validate"
    assert state.changes[0].status == "drafted" and not state.publication.pushed


@pytest.mark.asyncio
async def test_orphan_publication_draft_uses_accepted_checks_before_complete_batch(cfg, store, origin, forgejo):
    state, records, _ = seed(cfg, store, 1, 1)
    transport, wire = wire_for(records)
    pipeline = Pipeline(cfg, store, inference_transport=transport, publisher=Publisher(cfg, store, forgejo=forgejo))
    await pipeline._stage_draft(state)
    other = copy.deepcopy(state.changes[0])
    other.group_id = "other"
    other.path = "wiki/other.md"
    other.draft_meta["concept_id"] = "c-f1de0030"
    other.check_progress = {}
    state.changes.append(other)
    await pipeline._stage_validate(state)
    assert all(c.status == "validated" for c in state.changes)
    checks = [copy.deepcopy(c.check_progress) for c in state.changes]
    calls = state.usage.model_calls
    state.limits["max_model_calls"] = calls
    # Actual interrupted publication transition: one page has been downgraded,
    # although its unchanged-body reviews are already accepted in checkpoints.
    state.changes[0].status = "drafted"
    state.stage = "publish"
    state.status = "queued"
    result = await pipeline.run(state)
    assert result.status == "published" and all(c.status == "published" for c in result.changes)
    assert result.usage.model_calls == calls
    assert set(result.publication.committed_paths) == {"wiki/device.md", "wiki/other.md"}
    for c, original_checks in zip(result.changes, checks):
        assert c.check_progress == original_checks
        assert records[0].text in run_git(origin, "show", "main:" + c.path)
    assert result.changes[0].repair_passes == 0


@pytest.mark.parametrize("correction", [False, True])
@pytest.mark.parametrize("unresolved", ["pending", "drafted", "validated"])
def test_report_cannot_call_unpublished_planned_work_complete(cfg, store, correction, unresolved):
    state, _, _ = seed(cfg, store, 1, 1)
    state.changes[0].status = "published"
    other = copy.deepcopy(state.changes[0])
    other.path = "wiki/owed.md"
    other.status = unresolved
    state.changes.append(other)
    state.status = "publishing"
    state.stage = "report"
    if correction:
        state.correction_of = "job-parent"
    Pipeline(cfg, store)._derive_status(state)
    assert state.status == "partial" and other.status == unresolved


@pytest.mark.asyncio
@pytest.mark.parametrize("status,next_stage", [("pending", "draft"), ("drafted", "validate")])
async def test_uncommitted_publication_without_validated_pages_returns_to_saved_work(
    cfg, store, monkeypatch, status, next_stage
):
    state, _, _ = seed(cfg, store, 1, 1)
    state.changes[0].status = status
    pipeline = Pipeline(cfg, store)
    monkeypatch.setattr(pipeline, "_model", lambda _: pytest.fail("Stage routing invoked inference"))
    assert await pipeline._stage_publish(state) == next_stage
    assert not state.publication.commit_sha and state.changes[0].status == status


@pytest.mark.asyncio
async def test_recorded_merge_recovery_precedes_unfinished_draft_routing(cfg, store, monkeypatch):
    state, _, _ = seed(cfg, store, 1, 1)
    landed = state.changes[0]
    landed.status = "drafted"
    owed = copy.deepcopy(landed)
    owed.path = "wiki/owed.md"
    state.changes.append(owed)
    state.publication.commit_sha = "a" * 40
    state.publication.committed_paths = [landed.path]
    state.stage = "publish"
    state.status = "publishing"

    class RecoveringPublisher:
        def recover_existing_merge(self, current, changes, persist):
            assert changes == []  # Page status is not remote commit authority.
            landed.status = "published"
            current.publication.merged = True
            persist()
            return object()

    pipeline = Pipeline(cfg, store, publisher=RecoveringPublisher())
    monkeypatch.setattr(pipeline, "_model", lambda _: pytest.fail("Merged recovery invoked inference"))
    assert await pipeline._stage_publish(state) == "report"
    assert state.status == "partial" and owed.status == "drafted"
    assert state.publication.commit_sha == "a" * 40 and state.publication.merged
