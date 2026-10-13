"""Real charged same-page review shares context, not fact verdicts or qualifiers."""

import copy
import json

import httpx
import pytest
from test_placement import records

from doclayer.curator import curate, placement
from doclayer.curator.model import ModelClient, ModelError
from doclayer.curator.state import Usage


def client_for(cfg, usage, responder):
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        system, user = [message["content"] for message in payload["messages"]]
        requests.append((system, user))
        value = responder(system, user)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(value)}, "finish_reason": "stop"}]}
        )

    return ModelClient(cfg, usage, limits={"max_model_calls": 50}, transport=httpx.MockTransport(respond)), requests


def batch_material(user):
    return json.loads(user.split("Claims JSON:\n")[1].split("\nPage metadata:")[0])


def verdict(identity, *, covered=False, consistent=True, anchor="", qualifiers=None):
    return {
        "id": identity,
        "covered": covered,
        "consistent": consistent,
        "anchor": anchor,
        "qualifiers": qualifiers or [],
        "reason": "literal canonical review",
    }


@pytest.mark.asyncio
async def test_twelve_claims_six_regions_use_eight_actual_calls_with_original_fifty_cap(cfg):
    evidence = records(12)
    # Actual failed native job started placement after 23 source calls. Its
    # twelve claims had no coverage in six canonical units of one page.
    usage = Usage(model_calls=23)
    coverage_inputs = []

    def respond(system, user):
        if user.startswith("CANONICAL CLAIM COVERAGE BATCH"):
            material = batch_material(user)
            assert len(material) == 12
            coverage_inputs.append(user)
            return {"results": [verdict(item["id"]) for item in material]}
        selected = json.loads(user.split("Claims JSON:\n")[1])
        if "FAILED independent" not in system:
            return {"pages": [], "covered": [{"claim_id": item["id"], "path": "wiki/device.md"} for item in selected]}
        return {
            "pages": [
                {
                    "action": "update",
                    "path": "wiki/device.md",
                    "title": "Device",
                    "subject_type": "product",
                    "rationale": "missing real facts",
                    "source_ids": ["manual"],
                    "claim_ids": [item["id"] for item in selected],
                    "related": [],
                }
            ],
            "covered": [],
        }

    client, requests = client_for(cfg, usage, respond)
    body = "".join(f"## Canonical region {i}\n\nUnrelated existing facts {i}.\n\n" for i in range(6))
    result = await placement.plan_batch(
        client, "Device", [], evidence, [("wiki/device.md", {"version": "v1"}, body)], [], char_budget=32768
    )
    assert len(coverage_inputs) == 6 and len(requests) == 8
    assert usage.model_calls == 31 and client.max_calls == 50
    assert result["covered"] == [] and not result["already_covered"]
    assert set(result["pages"][0]["claim_ids"]) == {curate.evidence_id(item) for item in evidence}
    assert all(len(row["coverage_attempts"]) == 6 for row in result["ledger"]["claims"].values())
    replay = await placement.plan_batch(
        client,
        "Device",
        [],
        evidence,
        [("wiki/device.md", {"version": "v1"}, body)],
        [],
        char_budget=32768,
        ledger=result["ledger"],
    )
    assert replay["pages"] == result["pages"] and len(requests) == 8 and usage.model_calls == 31


@pytest.mark.asyncio
async def test_batch_preserves_each_claim_governing_quotes_and_late_contradiction(cfg):
    evidence = records(2)
    ids = [curate.evidence_id(item) for item in evidence]
    body = (
        "# Device\n\nOnly edition v1.\n\n## Facts\n\n"
        + "\n".join(item.text for item in evidence)
        + "\n\n## Exception\n\nFact zero is not valid on this platform.\n"
    )
    ledger = {"claims": {identity: {"status": "pending"} for identity in ids}}

    def respond(system, user):
        material = batch_material(user)
        quotes = json.loads(
            user.split("Governing qualification quotes by claim ID: ")[1].split("\nSection ancestry:")[0]
        )
        text = user.split("Canonical passage:\n")[1]
        rows = []
        for item in material:
            identity = item["id"]
            if "## Facts" in text:
                assert quotes[identity] == ["Only edition v1."]
            rows.append(
                verdict(
                    identity,
                    covered=item["claim"] in text,
                    anchor=item["claim"] if item["claim"] in text else "",
                    consistent=not ("## Exception" in text and identity == ids[0]),
                    qualifiers=["Only edition v1."] if "Only edition v1." in text else [],
                )
            )
        return {"results": rows}

    client, requests = client_for(cfg, Usage(), respond)
    await placement._covered_many(
        client, evidence, "wiki/device.md", {"wiki/device.md": ({"version": "v1"}, body)}, ledger, 12000, lambda: None
    )
    assert ledger["claims"][ids[0]]["status"] == "pending" and "coverage" not in ledger["claims"][ids[0]]
    assert ledger["claims"][ids[1]]["status"] == "covered"
    assert ledger["claims"][ids[1]]["coverage"]["qualifiers"] == ["Only edition v1."]
    assert len(requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["foreign-id", "duplicate-id", "nonliteral-anchor", "invented-qualifier", "mixed-type"]
)
async def test_batch_does_not_accept_malformed_or_foreign_fact_credit(cfg, failure):
    evidence = records(2)
    ids = [curate.evidence_id(item) for item in evidence]
    body = "# Device\n\n" + "\n".join(item.text for item in evidence)
    ledger = {"claims": {identity: {"status": "pending"} for identity in ids}}

    def respond(system, user):
        rows = [verdict(identity, covered=True, anchor=evidence[i].text) for i, identity in enumerate(ids)]
        if failure == "foreign-id":
            rows[0]["id"] = "foreign"
        if failure == "duplicate-id":
            rows[0]["id"] = ids[1]
        if failure == "nonliteral-anchor":
            rows[0]["anchor"] = "invented supporting quote"
        if failure == "invented-qualifier":
            rows[0]["qualifiers"] = ["not in source"]
        if failure == "mixed-type":
            rows[0]["consistent"] = "true"
        return {"results": rows}

    client, requests = client_for(cfg, Usage(), respond)
    if failure in ("nonliteral-anchor", "invented-qualifier"):  # literal-only faults prove no coverage
        await placement._covered_many(
            client, evidence, "wiki/device.md", {"wiki/device.md": ({}, body)}, ledger, 12000, lambda: None
        )
        assert all(row.get("unproven_coverage_attempts") for row in ledger["claims"].values())
    else:
        with pytest.raises(ModelError, match="invalid bounded placement response"):
            await placement._covered_many(
                client, evidence, "wiki/device.md", {"wiki/device.md": ({}, body)}, ledger, 12000, lambda: None
            )
    assert len(requests) == 3
    assert all(row["status"] == "pending" and "coverage" not in row for row in ledger["claims"].values())


@pytest.mark.asyncio
async def test_current_page_change_invalidates_batch_receipts_without_rewriting_old_proof(cfg):
    evidence = records(2)
    ids = [curate.evidence_id(item) for item in evidence]
    body = "# Device\n\n" + "\n".join(item.text for item in evidence)
    ledger = {"claims": {identity: {"status": "pending"} for identity in ids}}

    def respond(system, user):
        text = user.split("Canonical passage:\n")[1]
        return {
            "results": [
                verdict(
                    item["id"], covered=item["claim"] in text, anchor=item["claim"] if item["claim"] in text else ""
                )
                for item in batch_material(user)
            ]
        }

    client, requests = client_for(cfg, Usage(), respond)
    await placement._covered_many(
        client, evidence, "wiki/device.md", {"wiki/device.md": ({}, body)}, ledger, 12000, lambda: None
    )
    old = copy.deepcopy(ledger)
    for row in ledger["claims"].values():
        row["status"] = "pending"
    await placement._covered_many(
        client,
        evidence,
        "wiki/device.md",
        {"wiki/device.md": ({}, "# Device\n\nChanged source with no facts.\n")},
        ledger,
        12000,
        lambda: None,
    )
    assert len(requests) == 2
    assert all(row["status"] == "pending" for row in ledger["claims"].values())
    for identity in ids:
        assert (
            old["claims"][identity]["coverage_attempts"].items()
            <= ledger["claims"][identity]["coverage_attempts"].items()
        )


@pytest.mark.asyncio
async def test_batch_checkpoint_restart_reuses_actual_charged_region_and_complete_qualifiers(cfg):
    evidence = records(2)
    ids = [curate.evidence_id(item) for item in evidence]
    body = "# Device\n\nOnly edition v1.\n\n## Facts\n\n" + "\n".join(item.text for item in evidence)
    ledger = {"claims": {identity: {"status": "pending"} for identity in ids}}
    usage = Usage(model_calls=34)

    def respond(system, user):
        text = user.split("Canonical passage:\n")[1]
        if "## Facts" in text:
            qualifiers = json.loads(
                user.split("Governing qualification quotes by claim ID: ")[1].split("\nSection ancestry:")[0]
            )
            assert all(value == ["Only edition v1."] for value in qualifiers.values())
        return {
            "results": [
                verdict(
                    item["id"],
                    covered=item["claim"] in text,
                    anchor=item["claim"] if item["claim"] in text else "",
                    qualifiers=["Only edition v1."] if "Only edition v1." in text else [],
                )
                for item in batch_material(user)
            ]
        }

    client, requests = client_for(cfg, usage, respond)
    snapshots = []

    def interrupt():
        snapshots.append(copy.deepcopy(ledger))
        raise RuntimeError("checkpoint interruption after retained response")

    with pytest.raises(RuntimeError, match="checkpoint interruption"):
        await placement._covered_many(
            client, evidence, "wiki/device.md", {"wiki/device.md": ({}, body)}, ledger, 12000, interrupt
        )
    assert usage.model_calls == 35 and len(requests) == 1
    ledger = snapshots[-1]
    old = copy.deepcopy(ledger)
    await placement._covered_many(
        client, evidence, "wiki/device.md", {"wiki/device.md": ({}, body)}, ledger, 12000, lambda: None
    )
    assert usage.model_calls == 36 and len(requests) == 2
    assert all(row["status"] == "covered" for row in ledger["claims"].values())
    for identity in ids:
        assert (
            old["claims"][identity]["coverage_attempts"].items()
            <= ledger["claims"][identity]["coverage_attempts"].items()
        )


@pytest.mark.asyncio
async def test_partition_keeps_full_records_and_scope_mismatch_never_receives_credit(cfg):
    evidence = records(4)
    for item in evidence:
        item.quote += " literal governing source." * 90
    evidence[-1].applicability["version"] = "foreign-v2"
    ids = [curate.evidence_id(item) for item in evidence]
    body = "# Device\n\n" + "\n".join(item.text for item in evidence)
    ledger = {"claims": {identity: {"status": "pending"} for identity in ids}}
    seen = []

    def respond(system, user):
        if user.startswith("CANONICAL CLAIM COVERAGE BATCH"):
            material = batch_material(user)
            seen.extend(material)
            return {"results": [verdict(item["id"], covered=True, anchor=item["claim"]) for item in material]}
        material = json.loads(user.split("Claim JSON:\n")[1].split("\nPage metadata:")[0])
        seen.append(material)
        return {
            key: value
            for key, value in verdict(material["id"], covered=True, anchor=material["claim"]).items()
            if key != "id"
        }

    client, requests = client_for(cfg, Usage(), respond)
    await placement._covered_many(
        client, evidence, "wiki/device.md", {"wiki/device.md": ({"version": "v1"}, body)}, ledger, 7000, lambda: None
    )
    assert len(requests) > 1
    assert {item["id"] for item in seen} == set(ids[:-1])
    assert all(item["quote"] == next(r.quote for r in evidence if curate.evidence_id(r) == item["id"]) for item in seen)
    assert all(row["status"] == "covered" for identity, row in ledger["claims"].items() if identity != ids[-1])
    assert ledger["claims"][ids[-1]]["status"] == "pending"
