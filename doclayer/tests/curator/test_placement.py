"""Whole-record placement and actual canonical coverage across bounded passes."""

import copy
import json

import pytest

from doclayer.curator import curate, placement
from doclayer.curator.model import ModelError
from doclayer.curator.state import Evidence


def records(count=1):
    return [
        Evidence(
            "manual",
            "Manual",
            f"## Fact {i}",
            "quote",
            f"Device scoped source fact {i} is {i + 100}.",
            applicability={"model": "Device", "version": "v1"},
            quote=f"Device scoped source fact {i} is {i + 100}.",
            support_status="supported",
        )
        for i in range(count)
    ]


class Planner:
    def __init__(self, covered=False):
        self.calls = []
        self.covered = covered

    async def chat_bounded_json(self, system, user, *, char_budget):
        assert len(system) + len(user) <= char_budget
        self.calls.append(user)
        if user.startswith("CANONICAL CLAIM COVERAGE BATCH"):
            selected = json.loads(user.split("Claims JSON:\n")[1].split("\nPage metadata:")[0])
            body = user.split("Canonical passage:\n")[1]
            return {
                "results": [
                    {
                        "id": record["id"],
                        "covered": record["claim"] in body,
                        "consistent": True,
                        "qualifiers": [],
                        "anchor": record["claim"] if record["claim"] in body else "",
                        "reason": "actual whole scoped anchor",
                    }
                    for record in selected
                ]
            }
        if user.startswith("CANONICAL CLAIM COVERAGE"):
            record = json.loads(user.split("Claim JSON:\n")[1].split("\nPage metadata:")[0])
            body = user.split("Canonical passage:\n")[1]
            covered = record["claim"] in body
            return {
                "covered": covered,
                "consistent": True,
                "qualifiers": [],
                "anchor": record["claim"] if covered else "",
                "reason": "actual whole scoped anchor",
            }
        selected = json.loads(user.split("Claims JSON:\n")[1])
        if self.covered and "FAILED independent" not in system:
            return {
                "pages": [],
                "covered": [{"claim_id": item["id"], "path": "wiki/device.md"} for item in selected],
            }
        return {
            "pages": [
                {
                    "action": "update",
                    "path": "wiki/device.md",
                    "title": "Device",
                    "subject_type": "product",
                    "rationale": "whole source-backed facts",
                    "source_ids": ["manual"],
                    "claim_ids": [item["id"] for item in selected],
                    "related": [],
                }
            ],
            "covered": [],
        }


@pytest.mark.asyncio
async def test_more_than_120_claims_progress_without_relevance_prefix_starvation():
    evidence = records(137)
    client = Planner()
    ledger = {}
    seen = []
    # The old pipeline always selected the exact same top120.
    old_first = [curate.evidence_id(e) for e in curate.relevant_evidence(evidence, "Device", limit=120)]
    assert len(old_first) == 120 and len(set(old_first)) < len(evidence)
    for _ in range(50):
        result = await placement.plan_batch(client, "Device", [], evidence, [], [], char_budget=7000, ledger=ledger)
        seen += [identity for page in result["pages"] for identity in page["claim_ids"]]
        if result["plan_id"]:
            ledger = placement.acknowledge(result["ledger"], result["plan_id"])
        else:
            ledger = result["ledger"]
        if result["complete"]:
            break
    assert len(seen) == len(set(seen)) == 137
    assert set(seen) == {curate.evidence_id(e) for e in evidence}
    assert result["remaining"] == [] and not result["already_covered"]
    assert all(len(json.loads(call.split("Claims JSON:\n")[1])) <= 12 for call in client.calls)


@pytest.mark.asyncio
async def test_unacknowledged_plan_is_durable_replay_not_silently_skipped():
    client = Planner()
    saved = []
    first = await placement.plan_batch(
        client,
        "Device",
        [],
        records(15),
        [],
        [],
        char_budget=7000,
        progress=saved.append,
    )
    calls = len(client.calls)
    replay = await placement.plan_batch(client, "Device", [], records(15), [], [], char_budget=7000, ledger=saved[-1])
    assert replay["pages"] == first["pages"] and replay["plan_id"] == first["plan_id"]
    assert len(client.calls) == calls and replay["remaining"] == first["remaining"]


@pytest.mark.asyncio
async def test_no_change_requires_actual_whole_canonical_anchor_not_model_boolean():
    client = Planner(covered=True)
    evidence = records(2)
    pages = [
        (
            "wiki/device.md",
            {"title": "Device", "version": "v1"},
            "# Device\n" + evidence[0].text,
        )
    ]
    result = await placement.plan_batch(client, "Device", [], evidence, pages, [], char_budget=7000)
    assert result["covered"] == [curate.evidence_id(evidence[0])]
    assert result["pages"][0]["claim_ids"] == [curate.evidence_id(evidence[1])]
    assert not result["already_covered"] and result["complete"]
    assert any("CANONICAL CLAIM COVERAGE" in call for call in client.calls)


@pytest.mark.asyncio
async def test_covered_binding_invalidates_changed_canonical_and_applicability():
    evidence = records()
    page = ("wiki/device.md", {"title": "Device", "version": "v1"}, evidence[0].text)
    client = Planner(covered=True)
    first = await placement.plan_batch(client, "Device", [], evidence, [page], [], char_budget=7000)
    assert first["already_covered"]
    calls = len(client.calls)
    replay = await placement.plan_batch(
        client,
        "Device",
        [],
        evidence,
        [page],
        [],
        char_budget=7000,
        ledger=first["ledger"],
    )
    assert replay["already_covered"] and len(client.calls) == calls
    changed = await placement.plan_batch(
        client,
        "Device",
        [],
        evidence,
        [(page[0], page[1], "# Device\nRelated but absent fact.")],
        [],
        char_budget=7000,
        ledger=first["ledger"],
    )
    assert not changed["already_covered"] and changed["pages"]
    altered = copy.deepcopy(evidence)
    altered[0].applicability["version"] = "v2"
    again = await placement.plan_batch(
        client,
        "Device",
        [],
        altered,
        [page],
        [],
        char_budget=7000,
        ledger=first["ledger"],
    )
    assert (
        len(client.calls) > calls
        and again["ledger"]["claims"][curate.evidence_id(evidence[0])]["binding"]
        != first["ledger"]["claims"][curate.evidence_id(evidence[0])]["binding"]
    )


@pytest.mark.asyncio
async def test_continuation_preserves_parent_failed_retries_and_checks_actual_publication():
    evidence = records(15)
    first = await placement.plan_batch(Planner(), "Device", [], evidence, [], [], char_budget=7000)
    parent = copy.deepcopy(first["ledger"])
    published_id = first["pages"][0]["claim_ids"][0]
    child = placement.continuation(parent, {published_id: "wiki/device.md"})
    assert parent == first["ledger"] and not child["plans"]
    assert all(row["status"] == "pending" for row in child["claims"].values())
    pages = [
        (
            "wiki/device.md",
            {"title": "Device"},
            evidence[next(i for i, e in enumerate(evidence) if curate.evidence_id(e) == published_id)].text,
        )
    ]
    result = await placement.plan_batch(Planner(), "Device", [], evidence, pages, [], char_budget=7000, ledger=child)
    assert published_id in result["covered"]
    assert set(result["planned"]) | set(result["remaining"]) | set(result["covered"]) == {
        curate.evidence_id(e) for e in evidence
    }


@pytest.mark.asyncio
async def test_coverage_progress_resume_does_not_repeat_acknowledged_views():
    evidence = records()
    body = "# Device\n" + "".join(f"Unrelated background material {i}.\n" for i in range(200)) + evidence[0].text
    client = Planner(covered=True)
    saved = []

    def interrupt(ledger):
        saved.append(ledger)
        row = ledger["claims"][curate.evidence_id(evidence[0])]
        if row.get("coverage_attempts"):
            raise RuntimeError("restart")

    with pytest.raises(RuntimeError, match="restart"):
        await placement.plan_batch(
            client,
            "Device",
            [],
            evidence,
            [("wiki/device.md", {}, body)],
            [],
            char_budget=7000,
            progress=interrupt,
        )
    original_calls = list(client.calls)
    result = await placement.plan_batch(
        client,
        "Device",
        [],
        evidence,
        [("wiki/device.md", {}, body)],
        [],
        char_budget=7000,
        ledger=saved[-1],
    )
    assert result["already_covered"]
    assert all(call not in original_calls for call in client.calls[len(original_calls) :])


@pytest.mark.asyncio
async def test_unverified_records_and_oversized_whole_fact_fail_honestly():
    evidence = records()
    evidence[0].support_status = "unknown"
    with pytest.raises(ModelError, match="source-backed"):
        await placement.plan_batch(Planner(), "Device", [], evidence, [], [], char_budget=7000)
    evidence[0].support_status = "supported"
    evidence[0].text = "unique fact " * 2000
    with pytest.raises(ModelError, match="whole placement claim"):
        await placement.plan_batch(Planner(), "Device", [], evidence, [], [], char_budget=7000)


@pytest.mark.asyncio
async def test_schema_rejects_missing_foreign_duplicate_ids_and_unbound_coverage_anchor():
    class Invalid(Planner):
        async def chat_bounded_json(self, system, user, *, char_budget):
            return {
                "pages": [],
                "covered": [{"claim_id": "foreign", "path": "wiki/device.md"}],
            }

    with pytest.raises(ModelError, match="invalid bounded placement"):
        await placement.plan_batch(Invalid(), "Device", [], records(), [], [], char_budget=7000)

    class Invented(Planner):
        async def chat_bounded_json(self, system, user, *, char_budget):
            if user.startswith("CANONICAL"):
                return {
                    "covered": True,
                    "consistent": True,
                    "qualifiers": [],
                    "anchor": "invented absent passage",
                    "reason": "trust me",
                }
            return await super().chat_bounded_json(system, user, char_budget=char_budget)

    result = await placement.plan_batch(
        Invented(True),
        "Device",
        [],
        records(),
        [("wiki/device.md", {}, "# Device\nOther fact.")],
        [],
        char_budget=7000,
    )
    assert not result["covered"] and not result["already_covered"]
    assert result["pages"] and result["planned"]
    row = result["ledger"]["claims"][result["planned"][0]]
    assert "coverage" not in row and row["unproven_coverage_attempts"]


@pytest.mark.asyncio
async def test_explicit_canonical_version_conflict_cannot_claim_whole_coverage():
    evidence = records()
    client = Planner(covered=True)
    result = await placement.plan_batch(
        client,
        "Device",
        [],
        evidence,
        [
            (
                "wiki/device.md",
                {"title": "Device", "applicability": {"version": "v2"}},
                evidence[0].text,
            )
        ],
        [],
        char_budget=7000,
    )
    assert not result["covered"] and result["pages"]
    assert not any(call.startswith("CANONICAL") for call in client.calls)


@pytest.mark.asyncio
async def test_changed_one_claim_invalidates_entire_unacknowledged_plan_without_stranding_peers():
    evidence = records(3)
    first = await placement.plan_batch(Planner(), "Device", [], evidence, [], [], char_budget=7000)
    changed = copy.deepcopy(evidence)
    changed[0].quote += " corrected source"
    result = await placement.plan_batch(
        Planner(),
        "Device",
        [],
        changed,
        [],
        [],
        char_budget=7000,
        ledger=first["ledger"],
    )
    assert result["plan_id"] != first["plan_id"]
    assert set(result["pages"][0]["claim_ids"]) == {curate.evidence_id(record) for record in evidence}


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction_first", [True, False])
async def test_positive_child_never_bypasses_far_governing_installation_condition(
    restriction_first,
):
    evidence = records()
    evidence[0].text = evidence[0].quote = "Device outputs images."
    condition = "Only laboratory installations support all features."
    padding = "".join(f"Background material {i}.\n" for i in range(300))
    body = (
        "# Restrictions\n" + condition + "\n" + padding + "## Features\nDevice outputs images."
        if restriction_first
        else "# Features\nDevice outputs images.\n" + padding + "\nOnly laboratory installations support all features."
    )

    class Scoped(Planner):
        async def chat_bounded_json(self, system, user, *, char_budget):
            if user.startswith("CANONICAL"):
                assert len(system) + len(user) <= char_budget
                self.calls.append(user)
                text = user.split("Canonical passage:\n")[1]
                previous = json.loads(user.split("Governing qualification quotes: ")[1].split("\nSection ancestry:")[0])
                present = condition in text or condition in previous
                covered = evidence[0].text in text
                return {
                    "covered": covered,
                    "consistent": not present,
                    "anchor": evidence[0].text if covered else "",
                    "qualifiers": [condition] if condition in text else [],
                    "reason": "necessary installation qualification omitted",
                }
            return await super().chat_bounded_json(system, user, char_budget=char_budget)

    client = Scoped(covered=True)
    result = await placement.plan_batch(
        client,
        "Device",
        [],
        evidence,
        [("wiki/device.md", {}, body)],
        [],
        char_budget=7000,
    )
    assert not result["already_covered"] and not result["covered"] and result["pages"]
    assert len([call for call in client.calls if call.startswith("CANONICAL")]) > 2


@pytest.mark.asyncio
async def test_continued_cached_coverage_sets_claim_status_and_does_not_replan():
    evidence = records()
    page = ("wiki/device.md", {}, evidence[0].text)
    client = Planner(covered=True)
    first = await placement.plan_batch(client, "Device", [], evidence, [page], [], char_budget=7000)
    identity = curate.evidence_id(evidence[0])
    continued = placement.continuation(first["ledger"], {identity: page[0]})
    assert continued["claims"][identity]["status"] == "pending"
    count = len(client.calls)
    result = await placement.plan_batch(client, "Device", [], evidence, [page], [], char_budget=7000, ledger=continued)
    assert result["already_covered"] and result["covered"] == [identity] and not result["pages"]
    assert len(client.calls) == count


def frontier_for(evidence, **extra):
    return {
        **{
            curate.evidence_id(record): {
                "status": "supported",
                "binding": "source-revision-one",
                "reason": "current verified source",
            }
            for record in evidence
        },
        **extra,
    }


@pytest.mark.asyncio
async def test_unknown_frontier_does_not_starve_supported_records_or_claim_coverage():
    evidence = records(2)
    frontier = frontier_for(
        evidence, unknown={"status": "pending", "binding": "missing-source", "reason": "source extract unavailable"}
    )
    client = Planner()
    result = await placement.plan_batch(client, "Device", [], evidence, [], [], char_budget=7000, frontier=frontier)
    assert set(result["planned"]) == {curate.evidence_id(record) for record in evidence}
    assert result["remaining"] == ["unknown"] and not result["complete"] and not result["already_covered"]
    assert all("missing-source" not in call and "source extract unavailable" not in call for call in client.calls)
    assert result["ledger"]["claims"]["unknown"]["disposition"]["reason"] == "source extract unavailable"


@pytest.mark.asyncio
async def test_unknown_only_frontier_never_calls_model_or_reports_already_covered():
    client = Planner()
    result = await placement.plan_batch(
        client,
        "Device",
        [],
        [],
        [],
        [],
        char_budget=7000,
        frontier={"unknown": {"status": "pending", "binding": "source-unavailable", "reason": "source unavailable"}},
    )
    assert not client.calls and result["remaining"] == ["unknown"]
    assert not result["complete"] and not result["already_covered"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["excluded", "rejected"])
async def test_current_bound_disposition_retires_old_plan_then_reeligibility_reopens(status):
    evidence = records()
    identity = curate.evidence_id(evidence[0])
    first = await placement.plan_batch(
        Planner(), "Device", [], evidence, [], [], char_budget=7000, frontier=frontier_for(evidence)
    )
    client = Planner()
    retired = await placement.plan_batch(
        client,
        "Device",
        [],
        [],
        [],
        [],
        char_budget=7000,
        ledger=first["ledger"],
        frontier={
            identity: {
                "status": status,
                "binding": "policy-or-source-revision-two",
                "reason": "current proven disposition",
            }
        },
    )
    assert retired["excluded"] == [identity] and not retired["remaining"] and not retired["pages"] and not client.calls
    assert retired["ledger"]["claims"][identity]["disposition"]["binding"] == "policy-or-source-revision-two"
    reopened = await placement.plan_batch(
        client,
        "Device",
        [],
        evidence,
        [],
        [],
        char_budget=7000,
        ledger=retired["ledger"],
        frontier=frontier_for(evidence),
    )
    assert reopened["planned"] == [identity] and reopened["pages"]


@pytest.mark.asyncio
async def test_missing_old_claim_is_pending_without_current_rejection_proof():
    evidence = records()
    first = await placement.plan_batch(Planner(), "Device", [], evidence, [], [], char_budget=7000)
    identity = curate.evidence_id(evidence[0])
    for frontier in ({}, None):
        result = await placement.plan_batch(
            Planner(), "Device", [], [], [], [], char_budget=7000, ledger=first["ledger"], frontier=frontier
        )
        assert result["remaining"] == [identity] and not result["complete"]
        assert not result["pages"] and not result["already_covered"]


@pytest.mark.asyncio
async def test_supported_frontier_without_material_is_not_silently_complete():
    result = await placement.plan_batch(
        Planner(),
        "Device",
        [],
        [],
        [],
        [],
        char_budget=7000,
        frontier={"missing": {"status": "supported", "binding": "source-one", "reason": "verified but record absent"}},
    )
    assert result["remaining"] == ["missing"] and not result["complete"]
    assert result["ledger"]["claims"]["missing"]["disposition"]["status"] == "pending"


@pytest.mark.asyncio
async def test_frontier_bindings_invalidate_unacknowledged_plan_after_source_change():
    evidence = records()
    identity = curate.evidence_id(evidence[0])
    client = Planner()
    first = await placement.plan_batch(
        client, "Device", [], evidence, [], [], char_budget=7000, frontier=frontier_for(evidence)
    )
    old_calls = len(client.calls)
    changed = frontier_for(evidence)
    changed[identity]["binding"] = "actual-source-revision-two"
    second = await placement.plan_batch(
        client, "Device", [], evidence, [], [], char_budget=7000, ledger=first["ledger"], frontier=changed
    )
    assert len(client.calls) == old_calls + 1
    assert first["ledger"]["claims"][identity]["binding"] != second["ledger"]["claims"][identity]["binding"]


@pytest.mark.asyncio
async def test_frontier_checkpoint_real_store_restart_preserves_unknown_and_plan_replay(store):
    from doclayer.curator.state import JobState

    evidence = records()
    client = Planner()
    state = store.create(JobState(job_id="", idempotency_key="", intent="Device"))[0]
    frontier = frontier_for(
        evidence, unknown={"status": "pending", "binding": "missing-revision", "reason": "missing source"}
    )

    def persist(ledger):
        state.evidence_selection["placement"] = {"ledger": ledger}
        store.save(state)

    first = await placement.plan_batch(
        client, "Device", [], evidence, [], [], char_budget=7000, frontier=frontier, progress=persist
    )
    calls = len(client.calls)
    state = store.load(state.job_id)
    second = await placement.plan_batch(
        client,
        "Device",
        [],
        evidence,
        [],
        [],
        char_budget=7000,
        frontier=frontier,
        ledger=state.evidence_selection["placement"]["ledger"],
        progress=persist,
    )
    assert second["plan_id"] == first["plan_id"] and len(client.calls) == calls
    assert second["remaining"] == ["unknown"] and not second["already_covered"]
    restored = store.load(state.job_id)
    assert (
        restored.evidence_selection["placement"]["ledger"]["claims"]["unknown"]["disposition"]["binding"]
        == "missing-revision"
    )


@pytest.mark.asyncio
async def test_append_only_recovered_claim_uses_latest_record_once():
    current = records()[0]
    older = copy.deepcopy(current)
    older.support_status = "unknown"
    older.quote = "obsolete unverified quote"
    client = Planner()
    result = await placement.plan_batch(
        client, "Device", [], [older, current], [], [], char_budget=7000, frontier=frontier_for([current])
    )
    requests = [json.loads(call.split("Claims JSON:\n")[1]) for call in client.calls]
    assert len(requests) == 1 and len(requests[0]) == 1
    assert requests[0][0]["quote"] == current.quote
    assert result["planned"] == [curate.evidence_id(current)]
