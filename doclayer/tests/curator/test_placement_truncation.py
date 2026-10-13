"""Placement splits a batch whose reply hits the output limit (live job-08354886c6).

The e635 continuation went retryable at PLACE after 140 calls with "model
output truncated repeatedly". Its ledger holds 23 claims targeting
wiki/openai-responses-api.md (8 covered, 15 pending, 807 cached coverage
verdicts) and 75 more pending; no plan batch was ever stored. Coverage
batches of up to twelve claims (and plan batches of up to twelve) ask the
model to quote anchors and qualifiers for every claim, and a full reply can
exceed the serving output limit. OutputTruncated used to stop the job.
"""

import copy
import json
import re

import pytest
from test_placement import records

from doclayer.curator import curate, placement
from doclayer.curator.model import OutputTruncated
from doclayer.curator.state import JobState
from doclayer.curator.stopped_task import record_task_stop, task_stop


def batch_material(user):
    return json.loads(user.split("Claims JSON:\n")[1].split("\nPage metadata:")[0])


def passage(user):
    return user.split("Section ancestry: ")[1].split("\n")[0]


class Truncating:
    """Raises OutputTruncated when a request carries more than its limit of claims.

    `always` names claims whose plan never fits; `retry_limit` applies to
    retry-plan requests; `truncate_on` restricts coverage truncation to one
    passage; `crash_after` raises RuntimeError on that coverage call.
    """

    def __init__(
        self,
        coverage_limit=99,
        plan_limit=99,
        retry_limit=99,
        covered=False,
        always=(),
        truncate_on=None,
        crash_after=None,
    ):
        self.coverage_limit, self.plan_limit, self.retry_limit = coverage_limit, plan_limit, retry_limit
        self.covered, self.always, self.truncate_on, self.crash_after = covered, set(always), truncate_on, crash_after
        self.log = []

    async def chat_bounded_json(self, system, user, *, char_budget):
        if user.startswith("CANONICAL CLAIM COVERAGE"):
            batch = user.startswith("CANONICAL CLAIM COVERAGE BATCH")
            material = (
                batch_material(user)
                if batch
                else [json.loads(user.split("Claim JSON:\n")[1].split("\nPage metadata:")[0])]
            )
            self.log.append(("coverage", passage(user), tuple(item["id"] for item in material)))
            if self.crash_after is not None and len([e for e in self.log if e[0] == "coverage"]) > self.crash_after:
                raise RuntimeError("worker interrupted")
            if len(material) > self.coverage_limit and self.truncate_on in (None, passage(user)):
                raise OutputTruncated('{"results": [')
            body = user.split("Canonical passage:\n")[1]
            rows = [
                {
                    "id": item["id"],
                    "covered": item["claim"] in body,
                    "consistent": True,
                    "anchor": item["claim"] if item["claim"] in body else "",
                    "qualifiers": [],
                    "reason": "r",
                }
                for item in material
            ]
            return {"results": rows} if batch else {key: value for key, value in rows[0].items() if key != "id"}
        selected = json.loads(user.split("Claims JSON:\n")[-1])
        ids = [item["id"] for item in selected]
        retry = placement.RETRY_PLAN_SUFFIX in system
        self.log.append(("retry" if retry else "plan", len(ids), tuple(ids)))
        if len(ids) > (self.retry_limit if retry else self.plan_limit) or self.always & set(ids):
            raise OutputTruncated('{"pages": [')
        if self.covered and not retry:
            return {"pages": [], "covered": [{"claim_id": identity, "path": "wiki/device.md"} for identity in ids]}
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

    def sizes(self, kind):
        return [len(entry[2]) for entry in self.log if entry[0] == kind]


def pending_ledger(evidence):
    return {"claims": {curate.evidence_id(item): {"status": "pending"} for item in evidence}}


@pytest.mark.asyncio
async def test_truncated_coverage_batch_is_split_and_the_smaller_size_is_remembered():
    evidence = records(12)
    body = "# Device\n\n" + "\n".join(item.text for item in evidence[:5]) + "\n"
    ledger = pending_ledger(evidence)
    client = Truncating(coverage_limit=3)
    saved = []
    await placement._covered_many(
        client,
        evidence,
        "wiki/device.md",
        {"wiki/device.md": ({}, body)},
        ledger,
        32768,
        lambda: saved.append(copy.deepcopy(ledger)),
    )
    # 12 and the first 6 truncate; after that the remembered size 3 is used directly.
    assert client.sizes("coverage") == [12, 6, 3, 3, 3, 3]
    assert ledger["coverage_batch_limit"] == 3 and saved[-1]["coverage_batch_limit"] == 3
    covered = {identity for identity, row in ledger["claims"].items() if row["status"] == "covered"}
    assert covered == {curate.evidence_id(item) for item in evidence[:5]}
    # A later pass (or a resumed job, which reloads this ledger) starts at the smaller size.
    more = records(18)[12:]
    ledger["claims"].update(pending_ledger(more)["claims"])
    later = Truncating(coverage_limit=3)
    await placement._covered_many(
        later, more, "wiki/device.md", {"wiki/device.md": ({}, body)}, ledger, 32768, lambda: None
    )
    assert later.sizes("coverage") == [3, 3]


SECTIONED = "".join(f"## Region {i}\n\n" + "Unrelated canonical text. " * 40 + "\n\n" for i in range(3))


@pytest.mark.asyncio
async def test_truncation_on_a_later_passage_reuses_the_earlier_passages():
    evidence = records(6)
    ledger = pending_ledger(evidence)
    client = Truncating(coverage_limit=3, truncate_on="Region 1")
    await placement._covered_many(
        client, evidence, "wiki/device.md", {"wiki/device.md": ({}, SECTIONED)}, ledger, 32768, lambda: None
    )
    asked = [(where, identity) for kind, where, ids in client.log if kind == "coverage" for identity in ids]
    # The 6-claim batch answered Region 0 before Region 1 truncated; the halves never ask Region 0 again.
    assert asked.count(("Region 0", curate.evidence_id(evidence[0]))) == 1
    assert {where for where, _ in asked} == {"Region 0", "Region 1", "Region 2"}
    assert all(row["status"] == "pending" and "coverage" not in row for row in ledger["claims"].values())


@pytest.mark.asyncio
async def test_resume_after_an_interrupted_split_reuses_finished_verdicts():
    evidence = records(6)
    body = "# Device\n\n" + evidence[0].text + "\n\n" + SECTIONED
    ledger = pending_ledger(evidence)
    checkpoints = []
    crashing = Truncating(coverage_limit=3, crash_after=4)
    with pytest.raises(RuntimeError):
        await placement._covered_many(
            crashing,
            evidence,
            "wiki/device.md",
            {"wiki/device.md": ({}, body)},
            ledger,
            32768,
            lambda: checkpoints.append(copy.deepcopy(ledger)),
        )
    resumed_ledger = checkpoints[-1]
    # The smaller size is remembered only once the halves have succeeded.
    assert "coverage_batch_limit" not in resumed_ledger
    resumed = Truncating(coverage_limit=3)
    await placement._covered_many(
        resumed, evidence, "wiki/device.md", {"wiki/device.md": ({}, body)}, resumed_ledger, 32768, lambda: None
    )
    assert resumed_ledger["coverage_batch_limit"] == 3
    before = {
        (where, identity)
        for kind, where, ids in crashing.log[:-1]
        if kind == "coverage" and len(ids) <= 3
        for identity in ids
    }
    # Answered requests only (a request of more than 3 claims truncates and answers nothing).
    after = [
        (where, identity)
        for kind, where, ids in resumed.log
        if kind == "coverage" and len(ids) <= 3
        for identity in ids
    ]
    assert not before & set(after) and len(after) == len(set(after))  # nothing answered twice
    statuses = {identity: row["status"] for identity, row in resumed_ledger["claims"].items()}
    assert statuses[curate.evidence_id(evidence[0])] == "covered"
    assert [status for status in statuses.values()].count("covered") == 1  # no verdict counted twice


@pytest.mark.asyncio
async def test_truncated_plan_batch_is_halved_and_remembered():
    evidence = records(12)
    client = Truncating(plan_limit=3)
    first = await placement.plan_batch(client, "Device", [], evidence, [], [], char_budget=32768)
    assert client.sizes("plan") == [12, 6, 3]
    assert len(first["pages"][0]["claim_ids"]) == 3 and first["ledger"]["batch_limit"] == 3
    ledger = placement.acknowledge(first["ledger"], first["plan_id"])
    second = await placement.plan_batch(client, "Device", [], evidence, [], [], char_budget=32768, ledger=ledger)
    assert client.sizes("plan")[3:] == [3]
    assert set(first["planned"]) < set(second["planned"])


@pytest.mark.asyncio
async def test_truncated_retry_plan_is_halved_and_lowers_the_batch_limit():
    evidence = records(8)
    body = "# Device\n\nUnrelated text.\n"
    client = Truncating(retry_limit=2, covered=True)
    result = await placement.plan_batch(
        client, "Device", [], evidence, [("wiki/device.md", {}, body)], [], char_budget=32768
    )
    assert client.sizes("plan") == [8] and client.sizes("retry") == [8, 4, 2]
    planned = set(result["planned"])
    assert len(planned) == 2 and result["ledger"]["batch_limit"] == 2
    left_out = {curate.evidence_id(item) for item in evidence} - planned
    assert all(result["ledger"]["claims"][identity]["status"] == "pending" for identity in left_out)
    # The halved retry replays as it was asked; no new call.
    calls = len(client.log)
    replay = await placement.plan_batch(
        client, "Device", [], evidence, [("wiki/device.md", {}, body)], [], char_budget=32768, ledger=result["ledger"]
    )
    assert set(replay["planned"]) == planned and len(client.log) == calls


@pytest.mark.asyncio
async def test_76_claims_with_one_that_never_fits_place_the_other_75():
    from doclayer.curator.coverage import assess_coverage

    evidence = records(76)
    bad = curate.evidence_id(evidence[40])
    client = Truncating(always={bad})
    ledger = None
    for _ in range(40):
        result = await placement.plan_batch(client, "Device", [], evidence, [], [], char_budget=32768, ledger=ledger)
        ledger = placement.acknowledge(result["ledger"], result["plan_id"]) if result["plan_id"] else result["ledger"]
        if result["complete"]:
            break
    statuses = {identity: row["status"] for identity, row in ledger["claims"].items()}
    assert statuses[bad] == "failed"
    assert sorted(statuses.values()).count("planned") == 75 and result["complete"]
    state = JobState(job_id="j", idempotency_key="k", intent="Device")
    state.evidence_selection = {"placement": {"ledger": ledger}}
    gaps = [gap for gap in assess_coverage(state).current_gaps if "truncated" in gap]
    assert gaps == ["placement claim " + bad + ": " + placement.TRUNCATED_ALONE]
    # A later run retries the failed claim, and once it is placed the gap is gone.
    retry_ledger = placement.continuation(ledger)
    assert retry_ledger["claims"][bad]["status"] == "pending"
    fitting = Truncating()
    again = await placement.plan_batch(
        fitting, "Device", [], [evidence[40]], [], [], char_budget=32768, ledger=retry_ledger
    )
    assert again["planned"] == [bad]
    state.evidence_selection = {"placement": {"ledger": again["ledger"]}}
    assert not [gap for gap in assess_coverage(state).current_gaps if "truncated" in gap]


@pytest.mark.asyncio
async def test_single_claim_coverage_truncation_takes_ordinary_placement():
    evidence = records(1)
    ledger = {"claims": {curate.evidence_id(evidence[0]): {"status": "pending", "target": "wiki/device.md"}}}
    body = "# Device\n\n" + evidence[0].text + "\n"
    client = Truncating(coverage_limit=0)
    result = await placement.plan_batch(
        client, "Device", [], evidence, [("wiki/device.md", {}, body)], [], char_budget=32768, ledger=ledger
    )
    row = result["ledger"]["claims"][curate.evidence_id(evidence[0])]
    assert row["status"] == "planned"
    assert [attempt["diagnostic"] for attempt in row["unproven_coverage_attempts"].values()] == [
        {"operation": "placement-json", "output_truncated": True}
    ]


@pytest.mark.asyncio
async def test_pipeline_place_stage_records_the_gap_and_does_not_stop(cfg, store, monkeypatch):
    from doclayer.curator.coverage import assess_coverage
    from doclayer.curator.pipeline import Pipeline
    from doclayer.curator.state import Evidence, SourceRecord

    facts = [
        Evidence(
            "manual",
            "Device manual",
            f"## Fact {i}",
            "quote",
            f"Device qualified fact {i} is {i + 100}.",
            quote=f"Device qualified fact {i} is {i + 100}.",
        )
        for i in range(3)
    ]
    source_text = "# Device manual\n" + "\n".join(fact.text for fact in facts)
    state = store.create(JobState(job_id="", idempotency_key="", intent="Device qualified facts"))[0]
    source = SourceRecord("manual", "text", "Device manual", status="fetched", extraction_complete=True)
    state.sources, state.stage, state.status = [source], "place", "processing"
    pipeline = Pipeline(cfg, store)
    pipeline._store_extract(state, source, source_text)
    store.append_evidence(state.job_id, [fact.to_dict() for fact in facts])
    for fact in facts:
        state.evidence_verification[curate.evidence_id(fact)] = {
            "supported": True,
            "status": "supported",
            "excerpt": fact.quote,
            "support_version": curate.SOURCE_SUPPORT_VERSION,
            "source_binding": curate.source_support_binding(fact, {"manual": source_text}),
        }
    store.save(state)
    bad = curate.evidence_id(facts[1])
    client = Truncating(always={bad})
    monkeypatch.setattr(Pipeline, "_model", lambda *args: client)
    for _ in range(4):  # each place pass plans one batch, as continuations do
        next_stage = await pipeline._stage_place(state)
        assert next_stage == "draft" and state.status == "processing"
        if state.evidence_selection["placement"]["ledger"]["claims"][bad]["status"] == "failed":
            break
    assert {identity for change in state.changes for identity in change.claim_ids} == {
        curate.evidence_id(facts[0]),
        curate.evidence_id(facts[2]),
    }
    gap = "placement claim " + bad + ": " + placement.TRUNCATED_ALONE
    assert gap in assess_coverage(state).current_gaps
    assert re.search(r"placement claim claim-[0-9a-f]+: placement output truncated even for this claim alone", gap)


def test_named_truncation_marker_still_records_a_subject():
    state = JobState(job_id="j", idempotency_key="k", intent="i")
    state.stage, state.status = "place", "retryable"
    state.error = "model output truncated repeatedly; split the batch or raise the serving output limit"
    record_task_stop(state, kind="output_truncated", subject="placement claim claim-1")
    marker = task_stop(state)
    assert marker["kind"] == "output_truncated" and marker["subject"] == "placement claim claim-1"


# --------------------------------------------------------------------------- #
# A claim that truncates alone never lowers the remembered limits
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("position", [0, 40, 75])
async def test_one_bad_claim_does_not_shrink_the_plan_batch(position):
    evidence = records(76)
    bad = curate.evidence_id(evidence[position])
    client = Truncating(always={bad})
    ledger, passes = None, 0
    for passes in range(1, 20):
        result = await placement.plan_batch(client, "Device", [], evidence, [], [], char_budget=32768, ledger=ledger)
        ledger = placement.acknowledge(result["ledger"], result["plan_id"]) if result["plan_id"] else result["ledger"]
        if result["complete"]:
            break
    statuses = [row["status"] for row in ledger["claims"].values()]
    assert statuses.count("planned") == 75 and statuses.count("failed") == 1
    assert passes <= 8 and ledger.get("batch_limit", 12) == 12  # the normal size, a few passes
    clean = [size for kind, size, ids in client.log if kind == "plan" and bad not in ids]
    assert clean.count(12) >= 5


@pytest.mark.asyncio
async def test_one_bad_claim_does_not_shrink_the_coverage_batch():
    evidence = records(24)
    bad = curate.evidence_id(evidence[3])
    body = "# Device\n\n" + "\n".join(item.text for item in evidence) + "\n"
    ledger = pending_ledger(evidence)

    class OneBad(Truncating):
        async def chat_bounded_json(self, system, user, *, char_budget):
            if user.startswith("CANONICAL CLAIM COVERAGE BATCH") and bad in [
                item["id"] for item in batch_material(user)
            ]:
                self.log.append(("coverage", passage(user), tuple(item["id"] for item in batch_material(user))))
                raise OutputTruncated("{")
            return await super().chat_bounded_json(system, user, char_budget=char_budget)

    client = OneBad()
    pages = {"wiki/device.md": ({}, body)}
    await placement._covered_many(client, evidence[:12], "wiki/device.md", pages, ledger, 32768, lambda: None)
    assert ledger.get("coverage_batch_limit", 12) == 12
    calls = len(client.log)
    await placement._covered_many(client, evidence[12:], "wiki/device.md", pages, ledger, 32768, lambda: None)
    assert client.sizes("coverage")[calls:] == [12]  # 12 clean claims take one call
    statuses = [row["status"] for row in ledger["claims"].values()]
    assert statuses.count("covered") == 23 and ledger["claims"][bad]["status"] == "pending"
