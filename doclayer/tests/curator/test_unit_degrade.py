"""One policy for rejected model replies: drop just the unit, record a current gap, continue.

Live jobs kept stopping on ever-new per-task reply rejections (most recently
job-3cc6978aa6 at validate: "inference: structural move selects invalid
destination or level"). A per-unit task whose reply is still rejected after
its own bounded retries now drops only that unit: its content stays as it
was, a CURRENT coverage gap names the unit and the rejection, and the page
and the job continue. Transport/outage errors, limits and safety validators
keep their behavior.
"""

import copy
import json
import re

import httpx
import pytest
from test_bounded_editing import response, routing_response, supported
from test_introduced_section_repair import BODY, DUPLICATE, INTRO, operations, prepared
from test_introduced_section_repair import run as run_structural
from test_pipeline import LONG_BODY, git_show, run_pipeline, submit_payload_job
from test_placement import records

from doclayer import frontmatter
from doclayer.curator import (
    curate,
    degrade,
    editing,
    editorial,
    placement,
    preservation,
    reconciliation,
    source_constraints,
)
from doclayer.curator.coverage import assess_coverage
from doclayer.curator.degrade import GAP_MARK
from doclayer.curator.model import ModelError, ReplyRejected
from doclayer.curator.state import JobState, PageChange, SourceRecord

UNPARSEABLE = "model did not return parseable JSON"


def noop_edit(user):
    return {
        "section_id": re.search(r"Section ID: (.+)", user)[1],
        "edits": [],
        "append": "",
        "handled_claim_ids": [],
        "claim_dispositions": [],
        "notes": [],
    }


def dropped(gaps, unit):
    return [gap for gap in gaps if GAP_MARK in gap and unit in gap]


# --------------------------------------------------------------------------- #
# structural move (job-3cc6978aa6)
# --------------------------------------------------------------------------- #


class Structural:
    def __init__(self, mutate=None, fail=None):
        self.mutate, self.fail, self.edits = mutate, fail, 0

    async def chat_json(self, system, user):
        if user.startswith("BOUNDED INTRODUCED SECTION REPAIR\n"):
            if self.fail is not None:
                raise self.fail
            frame = json.loads(user.split("\n", 1)[1])
            reply = operations(frame)
            self.mutate(reply, frame)
            return reply
        self.edits += 1
        return noop_edit(user)


def intro_id(change):
    return next(u.id for u in editing.page_units(change.draft_body) if u.text == INTRO)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["wrong_destination", "wrong_level"])
async def test_invalid_structural_move_is_skipped_and_its_section_stays(bad):
    change = prepared()
    moved = intro_id(change)

    def mutate(reply, frame):
        if bad == "wrong_destination":
            reply["operations"][0]["after_section_id"] = "unknown"
        else:
            reply["operations"][0]["level"] = True

    gaps = []
    with degrade.recording(gaps):
        result = await run_structural(Structural(mutate), change)
    # The valid removal still applies; the invalid move is skipped, so Runtime stays where it was.
    assert result["body"] == BODY.replace(DUPLICATE, "")
    assert result["body"].index(INTRO) < result["body"].index("## Architecture")
    assert dropped(gaps, f"wiki/device.md: structural move of section {moved}")
    assert "invalid destination or level" in gaps[0]
    saved = result["draft_progress"]["structural_repair"]
    assert saved["status"] == "accepted" and [op["action"] for op in saved["operations"]] == ["remove"]
    assert saved["skipped"][0]["reason"] == "structural move selects invalid destination or level"


@pytest.mark.asyncio
async def test_every_structural_operation_invalid_leaves_the_page_and_anchored_repair_continues():
    change = prepared()

    def mutate(reply, frame):
        reply["operations"][0]["after_section_id"] = "unknown"
        reply["operations"][1]["level"] = 3  # a removal must have a null level

    client = Structural(mutate)
    gaps = []
    with degrade.recording(gaps):
        result = await run_structural(client, change)
    assert result["body"] == BODY and client.edits >= 1
    assert len(dropped(gaps, "wiki/device.md: structural")) == 2
    assert result["draft_progress"]["structural_repair"]["status"] == "no_operations"


@pytest.mark.asyncio
async def test_unparseable_structural_reply_drops_the_whole_structural_task():
    change = prepared()
    client = Structural(fail=ReplyRejected(UNPARSEABLE, transient=True))
    gaps = []
    with degrade.recording(gaps):
        result = await run_structural(client, change)
    assert result["body"] == BODY and client.edits >= 1
    assert dropped(gaps, "wiki/device.md: structural repair task")


@pytest.mark.asyncio
async def test_structural_transport_error_is_not_degraded():
    change = prepared()
    gaps = []
    with degrade.recording(gaps), pytest.raises(ModelError, match="inference unreachable"):
        await run_structural(Structural(fail=ModelError("inference unreachable: ConnectError")), change)
    assert gaps == [] and change.draft_body == BODY


@pytest.mark.asyncio
async def test_without_a_job_to_record_on_nothing_is_dropped_silently():
    change = prepared()

    def mutate(reply, frame):
        reply["operations"][0]["after_section_id"] = "unknown"

    with pytest.raises(ReplyRejected, match="invalid destination or level"):
        await run_structural(Structural(mutate), change)
    assert change.draft_body == BODY


# --------------------------------------------------------------------------- #
# section edit and claim routing
# --------------------------------------------------------------------------- #

PAGE = "# Device\n\n## Manual\n\nKnown original manual fact.\n"


def device_page():
    meta = {"title": "Device", "schema_version": 1, "privacy": "internal"}
    return PageChange("g", "wiki/device.md", "update", title="Device"), frontmatter.build(meta, PAGE)


@pytest.mark.asyncio
async def test_persistently_rejected_section_edit_keeps_the_section_and_records_its_claims():
    change, page = device_page()
    record = supported("Device scoped fact needs integration")

    class Client:
        edits = 0

        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            self.edits += 1
            return {"bogus": True}

    client = Client()
    gaps = []
    with degrade.recording(gaps):
        result = await editing.edit_page(
            client, "Device", change, [record], page, [], char_budget=12000, today="2026-10-06"
        )
    assert result["body"] == PAGE
    assert client.edits == 2  # the same deterministic rejection twice ends the bounded attempts
    identity = curate.evidence_id(record)
    assert result["draft_progress"]["not_integrated"][identity]["reason"] == "model reply rejected"
    assert dropped(gaps, "wiki/device.md: section edit") and identity in gaps[0]


@pytest.mark.asyncio
async def test_rejected_claim_routing_is_isolated_to_one_claim_and_the_others_integrate():
    change, page = device_page()
    good, bad = supported("Device scoped fact routes fine"), supported("Device unroutable fact")

    class Client:
        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return {"routes": "nope"} if "unroutable" in user else routing_response(user)
            return response(user)

    gaps = []
    with degrade.recording(gaps):
        result = await editing.edit_page(
            Client(), "Device", change, [good, bad], page, [], char_budget=12000, today="2026-10-06"
        )
    assert good.text in result["body"] and bad.text not in result["body"]
    missing = result["draft_progress"]["not_integrated"]
    assert set(missing) == {curate.evidence_id(bad)} and missing[curate.evidence_id(bad)]["section_id"] == "(unrouted)"
    assert dropped(gaps, f"section routing of claim {curate.evidence_id(bad)}")


# --------------------------------------------------------------------------- #
# review chunk: dropped, and the safety review still blocks
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rejected_review_chunk_is_dropped_but_still_blocks_publication():
    change = PageChange(
        "g",
        "wiki/device.md",
        "update",
        title="Device",
        draft_body="# Device\n\nDevice always lowers costs.\n",
        draft_meta={"title": "Device"},
    )

    class Client:
        async def chat_bounded_json(self, system, user, *, char_budget):
            raise ReplyRejected(UNPARSEABLE, transient=True)

    gaps = []
    with degrade.recording(gaps):
        result = await curate.check_draft(Client(), "Device", change, [supported()], [], char_budget=12000)
    blockers = preservation.blocking_failures(result)
    assert [f["check"] for f in blockers] == ["incomplete-review"] and "unverified" in blockers[0]["detail"]
    assert dropped(gaps, "wiki/device.md: article_review review chunk")


@pytest.mark.asyncio
async def test_review_format_repair_exhausted_is_dropped_as_a_blocker():
    change = PageChange(
        "g",
        "wiki/device.md",
        "update",
        title="Device",
        draft_body="# Device\n\nDevice often lowers costs.\n",
        draft_meta={"title": "Device"},
    )

    class Client:
        calls = 0

        async def chat_bounded_json(self, system, user, *, char_budget):
            self.calls += 1
            return {"complete": True, "failures": [{"severity": "nope"}], "conflicts": []}

    client = Client()
    gaps = []
    with degrade.recording(gaps):
        result = await editorial.review(client, "Device", change, [supported()], [], char_budget=12000)
    assert client.calls == 2  # the reply and its one bounded format repair
    assert result["failures"][0]["check"] == "incomplete-review" and dropped(gaps, "review chunk")


# --------------------------------------------------------------------------- #
# placement: claim plan and coverage batches
# --------------------------------------------------------------------------- #


class Rejecting:
    """Placement replies that are always invalid while `bad` is in the request."""

    def __init__(self, bad=(), coverage_invalid=False):
        self.bad, self.coverage_invalid, self.log = set(bad), coverage_invalid, []

    async def chat_bounded_json(self, system, user, *, char_budget):
        if user.startswith("CANONICAL CLAIM COVERAGE"):
            self.log.append("coverage")
            if self.coverage_invalid:
                return {"results": "nope"} if user.startswith("CANONICAL CLAIM COVERAGE BATCH") else {"covered": "nope"}
            raise AssertionError("no coverage expected")
        ids = [item["id"] for item in json.loads(user.split("Claims JSON:\n")[-1])]
        self.log.append(tuple(ids))
        if self.bad & set(ids):
            return {"pages": "nope"}
        return {
            "pages": [
                {
                    "action": "update",
                    "path": "wiki/device.md",
                    "title": "Device",
                    "subject_type": "product",
                    "rationale": "r",
                    "source_ids": ["manual"],
                    "claim_ids": ids,
                    "related": [],
                }
            ],
            "covered": [],
        }


@pytest.mark.asyncio
async def test_rejected_claim_placement_is_isolated_and_dropped_while_the_rest_is_placed():
    evidence = records(4)
    bad = curate.evidence_id(evidence[0])
    client = Rejecting(bad={bad})
    gaps = []
    snapshots = []
    with degrade.recording(gaps):
        result = await placement.plan_batch(
            client, "Device", [], evidence, [], [], char_budget=32768, progress=snapshots.append
        )
    # The isolation size is persisted (a resume continues it) and restored once the claim is dropped.
    assert [snap.get("batch_limit") for snap in snapshots if "batch_limit_base" in snap][:2] == [2, 1]
    assert result["ledger"]["batch_limit"] == 12 and "batch_limit_base" not in result["ledger"]
    # Halved down to the rejected claim alone (three attempts each), then the others are planned.
    assert [len(entry) for entry in client.log] == [4] * 3 + [2] * 3 + [1] * 3 + [3]
    assert set(client.log[-1]) == {curate.evidence_id(item) for item in evidence[1:]}
    statuses = {identity: row["status"] for identity, row in result["ledger"]["claims"].items()}
    assert statuses[bad] == "failed" and sorted(statuses.values()).count("planned") == 3
    assert result["ledger"]["claims"][bad]["reason"] == placement.REJECTED_ALONE
    assert dropped(gaps, f"placement of claim {bad}")
    state = JobState(job_id="j", idempotency_key="k", intent="Device")
    state.evidence_selection = {"placement": {"ledger": result["ledger"]}}
    assert f"placement claim {bad}: {placement.REJECTED_ALONE}" in assess_coverage(state).current_gaps


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 3])
async def test_rejected_coverage_check_gives_no_proof_and_ordinary_placement_follows(count):
    evidence = records(count)
    ledger = {"claims": {curate.evidence_id(item): {"status": "pending"} for item in evidence}}
    body = "# Device\n\nUnrelated canonical text.\n"
    client = Rejecting(coverage_invalid=True)
    gaps = []
    with degrade.recording(gaps):
        await placement._covered_many(
            client, evidence, "wiki/device.md", {"wiki/device.md": ({}, body)}, ledger, 32768, lambda: None
        )
    for row in ledger["claims"].values():
        assert row["status"] == "pending"
        assert [
            attempt["diagnostic"].get("unit_dropped") for attempt in row["unproven_coverage_attempts"].values()
        ] == [True]
    unit = "coverage check of claim" if count == 1 else "coverage batch on wiki/device.md"
    assert dropped(gaps, unit)
    # A resumed pass never asks the dropped coverage unit again.
    asked = len(client.log)
    with degrade.recording(gaps):
        await placement._covered_many(
            client, evidence, "wiki/device.md", {"wiki/device.md": ({}, body)}, ledger, 32768, lambda: None
        )
    assert len(client.log) == asked


# --------------------------------------------------------------------------- #
# source support: batch verdicts and record reviews
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rejected_source_support_batch_leaves_its_claims_unknown(monkeypatch):
    from doclayer.curator import source_constraints

    monkeypatch.setattr(source_constraints, "material", lambda record, text, budget: None)
    monkeypatch.setattr(curate, "support_excerpt", lambda record, text, budget=2000: "Actual excerpt.")
    evidence = records(2)

    class Client:
        calls = 0

        async def chat_verdicts(self, system, user, *, char_budget):
            self.calls += 1
            return {"verdicts": []}

    client = Client()
    gaps, retained = [], {}
    with degrade.recording(gaps):
        results = await curate.verify_source_claims(
            client, evidence, {"manual": "Actual excerpt."}, char_budget=32768, progress=retained.update
        )
    assert client.calls == 3
    for item in evidence:
        verdict = results[curate.evidence_id(item)]
        assert verdict["status"] == "unknown" and verdict["supported"] is False and verdict["unit_dropped"]
        assert retained[curate.evidence_id(item)] == verdict
    assert [gap for gap in gaps if GAP_MARK in gap and "source-support review of claim" in gap] and len(gaps) == 2


@pytest.mark.asyncio
async def test_rejected_source_record_review_leaves_that_claim_unknown(monkeypatch):
    from doclayer.curator import source_constraints

    monkeypatch.setattr(source_constraints, "material", lambda record, text, budget: "material")

    async def verify(*args, **kwargs):
        raise ReplyRejected(UNPARSEABLE, transient=True)

    monkeypatch.setattr(source_constraints, "verify", verify)
    item = records(1)[0]
    gaps = []
    with degrade.recording(gaps):
        results = await curate.verify_source_claims(object(), [item], {"manual": "text"}, char_budget=32768)
    verdict = results[curate.evidence_id(item)]
    assert verdict["status"] == "unknown" and verdict["reason"] == "source record review reply rejected"
    assert verdict["unit_dropped"] and dropped(gaps, f"source-support review of claim {curate.evidence_id(item)}")


def support_job(cfg, store):
    from doclayer.curator.pipeline import Pipeline

    source = SourceRecord("manual", "text", "Manual", status="fetched", role="primary")
    state = store.create(JobState("", "", "Device", sources=[source]))[0]
    pipeline = Pipeline(cfg, store)
    pipeline._store_extract(state, source, "Device scoped source fact 0 is 100. Device scoped source fact 1 is 101.")
    good, bad = records(2)
    store.append_evidence(state.job_id, [good.to_dict(), bad.to_dict()])
    return state, pipeline, good, bad


def fake_verification(bad, *, dropped):
    async def verify(client, batch, texts, *, char_budget, progress, known):
        results = {}
        for record in batch:
            rejected = curate.evidence_id(record) == curate.evidence_id(bad)
            results[curate.evidence_id(record)] = {
                "supported": not rejected,
                "status": "unknown" if rejected else "supported",
                "reason": "source-support reply rejected" if rejected else "ok",
                "excerpt": record.quote,
                "support_version": curate.SOURCE_SUPPORT_VERSION,
                "source_binding": curate.source_support_binding(record, texts),
                **({"unit_dropped": True} if rejected and dropped else {}),
            }
        progress(results)
        return results

    return verify


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", ["pending", "drafted-elsewhere"])
async def test_required_claim_with_dropped_support_review_leaves_an_undrafted_page(cfg, store, monkeypatch, draft):
    from doclayer.curator.pipeline import SourceSupportUnavailable

    state, pipeline, good, bad = support_job(cfg, store)
    change = PageChange("g", "wiki/device.md", "update", claim_ids=[curate.evidence_id(good), curate.evidence_id(bad)])
    if draft == "drafted-elsewhere":  # drafted, but the dropped claim is not on the page
        change.draft_body = "# Device\n\n" + good.text + "\n"
        change.draft_progress = {"claim_anchors": {curate.evidence_id(good): good.text}}
    monkeypatch.setattr(curate, "verify_source_claims", fake_verification(bad, dropped=False))
    with pytest.raises(SourceSupportUnavailable):  # an ordinary unknown still isolates the page
        await pipeline._verify_selected(state, copy.deepcopy(change), None)
    state.evidence_verification = {}
    monkeypatch.setattr(curate, "verify_source_claims", fake_verification(bad, dropped=True))
    await pipeline._verify_selected(state, change, None)
    assert change.claim_ids == [curate.evidence_id(good)]


@pytest.mark.asyncio
@pytest.mark.parametrize("carried", ["text", "anchor"])
async def test_drafted_claim_with_dropped_support_review_still_isolates_its_page(cfg, store, monkeypatch, carried):
    """A dropped source-support re-check never lets the drafted sentence publish."""
    from doclayer.curator.pipeline import SourceSupportUnavailable

    state, pipeline, good, bad = support_job(cfg, store)
    change = PageChange(
        "g", "wiki/device.md", "update", claim_ids=[curate.evidence_id(good), curate.evidence_id(bad)], status="drafted"
    )
    if carried == "text":
        change.draft_body = "# Device\n\n" + good.text + "\n" + bad.text.replace(" is ", "  is ") + "\n"
    else:  # the drafted wording differs, but the editor recorded the claim's anchor
        change.draft_body = "# Device\n\n" + good.text + "\nA paraphrase of the second fact.\n"
        change.draft_progress = {"claim_anchors": {curate.evidence_id(bad): "A paraphrase of the second fact."}}
    monkeypatch.setattr(curate, "verify_source_claims", fake_verification(bad, dropped=True))
    with pytest.raises(SourceSupportUnavailable) as error:
        await pipeline._verify_selected(state, change, None)
    assert error.value.identities == [curate.evidence_id(bad)]
    assert curate.evidence_id(bad) in change.claim_ids


@pytest.mark.asyncio
async def test_exhausted_record_schema_review_keeps_its_prior_unknown_without_a_drop(monkeypatch):
    monkeypatch.setattr(source_constraints, "material", lambda record, text, budget: "material")
    item = records(1)[0]

    async def verify(*args, **kwargs):
        raise ReplyRejected(
            "invalid source ancestor constraint response",
            transient=True,
            diagnostic={
                "operation": "source-ancestor-constraints",
                "attempts": 3,
                "expected_ids": [curate.evidence_id(item)],
                "validation_issues": ["verdicts[0].qualifiers: not literal"],
            },
        )

    monkeypatch.setattr(source_constraints, "verify", verify)
    for bound in (False, True):
        gaps = []
        with degrade.recording(gaps if bound else None):
            results = await curate.verify_source_claims(object(), [item], {"manual": "text"}, char_budget=32768)
        verdict = results[curate.evidence_id(item)]
        assert verdict["reason"] == "bounded source record schema review unresolved"
        assert "unit_dropped" not in verdict and gaps == []


@pytest.mark.asyncio
async def test_dropped_review_chunk_gap_is_retired_when_the_chunk_is_later_reviewed():
    change = PageChange(
        "g",
        "wiki/device.md",
        "update",
        title="Device",
        draft_body="# Device\n\nDevice always lowers costs.\n",
        draft_meta={"title": "Device"},
    )

    class Failing:
        async def chat_bounded_json(self, system, user, *, char_budget):
            raise ReplyRejected(UNPARSEABLE, transient=True)

    class Passing:
        async def chat_bounded_json(self, system, user, *, char_budget):
            return {"complete": True, "failures": [], "conflicts": []}

    gaps = []
    with degrade.recording(gaps):
        await curate.check_draft(Failing(), "Device", change, [supported()], [], char_budget=12000)
        assert dropped(gaps, "review chunk")
        change.draft_body = "# Device\n\nDevice often lowers costs.\n"  # repaired: same sections, new text
        result = await curate.check_draft(Passing(), "Device", change, [supported()], [], char_budget=12000)
    assert not preservation.blocking_failures(result) and gaps == []


# --------------------------------------------------------------------------- #
# whole-page repair pass and reconciliation views
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rejected_whole_page_repair_pass_keeps_the_draft():
    change = PageChange(
        "g",
        "wiki/device.md",
        "create",
        title="Device",
        draft_body="# Device\n\nShort draft.\n",
        draft_meta={"title": "Device"},
    )

    class Client:
        async def chat_json(self, system, user):
            raise ReplyRejected(UNPARSEABLE, transient=True)

    gaps = []
    with degrade.recording(gaps):
        result = await curate.repair_draft(
            Client(), "Device", change, [{"check": "evidence", "detail": "x"}], [], [], char_budget=12000
        )
    assert result["body"] == change.draft_body and result["frontmatter"] == change.draft_meta
    assert dropped(gaps, "wiki/device.md: whole-page repair pass")


@pytest.mark.asyncio
async def test_rejected_reconciliation_view_keeps_fresh_text_and_drops_the_view():
    current = "# Device\nOperator qualification is retained.\n"
    change = PageChange("group", "wiki/device.md", "update")

    class Client:
        calls = 0

        async def chat_bounded_json(self, system, user, *, char_budget):
            self.calls += 1
            return {
                "section_id": re.search(r"Section ID: (\S+)", user)[1],
                "edits": [{"old": "absent anchor", "new": "x"}],
                "append": "",
                "handled_claim_ids": [],
            }

    client, views, completed = Client(), [], {}
    gaps = []
    with degrade.recording(gaps):
        result = await reconciliation._merge_material(
            client,
            "topic",
            change,
            current,
            "Supported contribution.\n",
            [],
            7000,
            "binding",
            completed,
            None,
            "overlap",
            protected=current,
            dropped=views,
        )
    assert result == current and client.calls == 3
    assert [row["text"] for row in views] == ["Supported contribution.\n"]
    assert any('"superseded_by_fresh": true' in value for value in completed.values())
    assert dropped(gaps, "wiki/device.md: reconciliation of draft view")


# --------------------------------------------------------------------------- #
# whole jobs: the job continues; transport errors stay retryable; safety blocks
# --------------------------------------------------------------------------- #


def test_job_continues_past_rejected_extraction_chunk_and_link_scoring(cfg, store, qwen, checkout, origin, forgejo):
    pages = {
        "https://docs.test/good": (
            200,
            (
                "<html><head><title>G</title></head><body><article><h1>G</h1>"
                f"<p>{LONG_BODY}</p><p><a href='https://docs.test/linked'>more</a></p>"
                "</article></body></html>"
            ),
        ),
        "https://docs.test/bad": (
            200,
            f"<html><head><title>B</title></head><body><article><h1>B</h1><p>{LONG_BODY}</p></article></body></html>",
        ),
    }

    def extract(user):
        return (
            {"doc_kind": "other", "claims": "nope"} if "docs.test/bad" in user else qwen.default_reply("extract", user)
        )

    qwen.overrides["extract"] = [extract] * 20
    qwen.overrides["relevance"] = ["not json"] * 4
    state = submit_payload_job(
        store, checkout, urls=["https://docs.test/good", "https://docs.test/bad"], intent="build test-model knowledge"
    )
    state.limits["max_model_calls"] = 80
    store.save(state)
    state = run_pipeline(cfg, store, qwen, pages, state, forgejo)
    assert state.status == "published", state.error
    assert git_show(origin, "main:wiki/test-model.md")
    gaps = assess_coverage(state).current_gaps
    assert [gap for gap in gaps if GAP_MARK in gap and "docs.test/bad" in gap and "extraction chunk" in gap]
    assert [gap for gap in gaps if GAP_MARK in gap and "discovery link scoring" in gap]
    bad = next(source for source in state.sources if source.url == "https://docs.test/bad")
    assert bad.omitted_sections  # its unreviewed sections stay recorded
    linked = next(source for source in state.sources if source.url == "https://docs.test/linked")
    assert linked.status == "rejected"


def test_transport_error_inside_a_unit_task_is_still_retryable(cfg, store, qwen, checkout):
    def outage(user):
        raise httpx.ConnectError("inference down")

    qwen.overrides["plan"] = [outage] * (cfg.inference.retries + 1)
    state = submit_payload_job(store, checkout, texts=[("n", LONG_BODY)], intent="outage during placement")
    state = run_pipeline(cfg, store, qwen, {}, state, forgejo=None)
    assert state.status == "retryable" and state.stage == "place"
    assert state.error.startswith("inference: inference unreachable")
    assert not [gap for gap in state.coverage_gaps if GAP_MARK in gap]


def test_safety_review_that_cannot_be_completed_still_blocks_and_the_job_reports(
    cfg, store, qwen, checkout, origin, forgejo
):
    qwen.overrides["check"] = [{"bogus": True}] * 40
    state = submit_payload_job(store, checkout, texts=[("n", LONG_BODY)], intent="build test-model knowledge")
    state.limits["max_model_calls"] = 80
    store.save(state)
    state = run_pipeline(cfg, store, qwen, {}, state, forgejo)
    assert state.stage == "report" and state.error is None
    assert [change.status for change in state.changes] == ["failed"]
    assert git_show(origin, "main:wiki/test-model.md") is None and not forgejo.prs
    assert [gap for gap in state.coverage_gaps if GAP_MARK in gap and "review chunk" in gap]
