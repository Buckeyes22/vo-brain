"""Only invalid literal fields are repaired; unfinished semantics stay pending."""

import json

import httpx
import pytest

from doclayer.curator import curate, source_constraints
from doclayer.curator.model import ModelClient, ModelError
from doclayer.curator.state import BudgetExceeded, Evidence, JobState


def verdict(supported=False, consistent=True):
    return dict(
        id="scope",
        supported=supported,
        consistent=consistent,
        reason="Scope only.",
        qualifiers=["Only laboratory installations.", "ProviderB"],
        support_quotes=[],
    )


def edits(value):
    return {
        "verdicts": [
            {
                "id": "scope",
                "edits": [{"field": "qualifiers", "index": 1, "replacement": value}],
            }
        ]
    }


def test_literal_repair_preserves_semantics_and_every_valid_quote():
    original = verdict()
    targets = source_constraints._quote_targets([original], "scope", ["qualifiers[1] is not literal"])
    repaired, error = source_constraints._apply_quote_edits(edits(None), original, targets)
    assert error is None and repaired[0]["qualifiers"] == ["Only laboratory installations."]
    assert {k: repaired[0][k] for k in ("id", "supported", "consistent", "reason", "support_quotes")} == {
        k: original[k] for k in ("id", "supported", "consistent", "reason", "support_quotes")
    }
    assert original["qualifiers"] == ["Only laboratory installations.", "ProviderB"]


@pytest.mark.parametrize("supported,consistent", [(True, True), (False, False), (True, False)])
def test_positive_or_contradictory_verdict_needs_full_semantic_reask(supported, consistent):
    assert (
        source_constraints._quote_targets([verdict(supported, consistent)], "scope", ["qualifiers[1] is not literal"])
        is None
    )


@pytest.mark.parametrize("mutation", ["semantic", "valid-index", "wrong-id", "duplicate", "missing"])
def test_model_cannot_edit_semantics_or_valid_entries(mutation):
    original = verdict()
    targets = source_constraints._quote_targets([original], "scope", ["qualifiers[1] is not literal"])
    reply = edits(None)
    row = reply["verdicts"][0]
    if mutation == "semantic":
        row["supported"] = True
    elif mutation == "valid-index":
        row["edits"][0]["index"] = 0
    elif mutation == "wrong-id":
        row["id"] = "different"
    elif mutation == "duplicate":
        row["edits"] *= 2
    else:
        row["edits"] = []
    assert source_constraints._apply_quote_edits(reply, original, targets)[0] is None


@pytest.mark.asyncio
async def test_generic_nonmetadata_quote_requires_model_literal_replacement():
    original = verdict()
    original["qualifiers"][1] = "invented formatting label"
    targets = source_constraints._quote_targets([original], "scope", ["qualifiers[1] is not literal"])
    body = "Only laboratory installations. Access requires approval."
    request = {
        "id": "scope",
        "claim": "The laboratory installation supports the feature with approval.",
        "ancestor_context": body,
        "previous_qualification_quotes": [],
        "previous_support_quotes": [],
    }

    class Client:
        async def chat_verdicts(self, system, user, *, char_budget):
            assert len(system) + len(user) <= char_budget
            assert json.loads(user.split("\n", 1)[1])["ancestor_context"] == body
            return edits("Access requires approval.")

    values = await source_constraints._repair_quote_fields(
        Client(),
        original,
        targets,
        request,
        [("Manual", body)],
        body,
        "",
        [],
        [],
        char_budget=6000,
        attempts=2,
        metadata={},
    )
    assert values[0]["qualifiers"] == [
        "Only laboratory installations.",
        "Access requires approval.",
    ]

    class Invalid:
        async def chat_verdicts(self, *args, **kwargs):
            return edits("Another invented label")

    with pytest.raises(ModelError):
        await source_constraints._repair_quote_fields(
            Invalid(),
            original,
            targets,
            request,
            [("Manual", body)],
            body,
            "",
            [],
            [],
            char_budget=6000,
            attempts=2,
            metadata={},
        )


@pytest.mark.asyncio
async def test_quote_repair_never_clips_source_to_fit():
    original = verdict()
    targets = source_constraints._quote_targets([original], "scope", ["qualifiers[1] is not literal"])

    class Never:
        async def chat_verdicts(self, *args, **kwargs):
            pytest.fail("Cannot send truncated source")

    with pytest.raises(ModelError, match="cannot fit bounded input"):
        await source_constraints._repair_quote_fields(
            Never(),
            original,
            targets,
            {"ancestor_context": "x" * 6000},
            [],
            "",
            "",
            [],
            [],
            char_budget=6000,
            attempts=2,
            metadata={},
        )


@pytest.mark.asyncio
async def test_actual_admitted_budget_restart_keeps_semantic_stage_and_prior_regions(cfg, store):
    condition = "Only laboratory installations."
    fact = "The laboratory device outputs text."
    text = "# Manual\n" + condition + "\n" + "Unrelated setup guidance.\n" * 170 + "## Device\n" + fact + "\n"
    record = Evidence(
        "s",
        "manual",
        "Device",
        "quote",
        fact,
        quote=fact,
        applicability={"provider": "ProviderB"},
    )
    identity = curate.evidence_id(record)
    state, _ = store.create(JobState(job_id="job-quote0001", idempotency_key="quote-restart", intent="Device"))
    requests = []

    def handler(request):
        system, user = [message["content"] for message in json.loads(request.content)["messages"]]
        frame = json.loads(user.split("\n", 1)[1])
        requests.append((user.split("\n", 1)[0], frame))
        if user.startswith("SOURCE LITERAL QUOTATION REPAIR"):
            reply = {
                "verdicts": [
                    {
                        "id": frame["id"],
                        "edits": [{"field": "qualifiers", "index": 1, "replacement": None}],
                    }
                ]
            }
        else:
            body = frame["ancestor_context"]
            number = len(requests)
            reply = {
                "verdicts": [
                    dict(
                        id=frame["id"],
                        supported=fact in body,
                        consistent=True,
                        reason="Actual source only.",
                        qualifiers=[condition] if condition in body else [],
                        support_quotes=[fact] if fact in body else [],
                    )
                ]
            }
            if number == 2:
                reply["verdicts"][0]["qualifiers"] = [
                    "Unrelated setup guidance.",
                    "ProviderB",
                ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}]},
        )

    def checkpoint(rows):
        state.evidence_verification.update(rows)
        store.save(state)

    client = ModelClient(
        cfg,
        state.usage,
        limits={"max_model_calls": 2},
        transport=httpx.MockTransport(handler),
        admitted_call=lambda: store.save(state),
    )
    with pytest.raises(BudgetExceeded):
        await source_constraints.verify(
            client,
            record,
            text,
            char_budget=6000,
            excerpt_budget=2000,
            binding="same-source",
            progress=checkpoint,
        )
    restored = store.load(state.job_id)
    known = restored.evidence_verification[identity]
    assert restored.usage.model_calls == 2 and known["verification_pending"] and not known["supported"]
    assert len(known["ancestor_verdicts"]) == 1 and len(known["pending_quote_repairs"]) == 1
    completed_ids = {value["id"] for value in known["ancestor_verdicts"].values()}
    state = restored
    before = len(requests)
    client = ModelClient(
        cfg,
        state.usage,
        limits={"max_model_calls": 20},
        transport=httpx.MockTransport(handler),
        admitted_call=lambda: store.save(state),
    )
    result = await source_constraints.verify(
        client,
        record,
        text,
        char_budget=6000,
        excerpt_budget=2000,
        binding="same-source",
        known=known,
        progress=checkpoint,
    )
    assert requests[before][0] == "SOURCE LITERAL QUOTATION REPAIR"
    assert all(frame["id"] not in completed_ids for _, frame in requests[before:])
    assert result["supported"] and not result["pending_quote_repairs"]
    assert condition in result["excerpt"] and "ProviderB" not in result["excerpt"]
    assert store.load(state.job_id).usage.model_calls == len(requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_supported,first_consistent", [(True, True), (False, False)])
async def test_bad_positive_and_contradictory_quotes_are_semantically_reassessed(first_supported, first_consistent):
    condition = "Only laboratory installations."
    fact = "The device outputs text."
    text = "# Manual\n" + condition + "\n" + "Unrelated setup guidance.\n" * 120 + "## Device\n" + fact + "\n"
    record = Evidence(
        "s",
        "manual",
        "Device",
        "quote",
        fact,
        quote=fact,
        applicability={"provider": "ProviderB"},
    )

    class Client:
        calls = 0

        async def chat_verdicts(self, system, user, *, char_budget):
            self.calls += 1
            request = json.loads(user.split("\n", 1)[1])
            if user.startswith("SOURCE LITERAL QUOTATION REPAIR"):
                return {
                    "verdicts": [
                        {"id": request["id"], "edits": [{"field": "qualifiers", "index": 0, "replacement": None}]}
                    ]
                }
            if user.startswith("SOURCE REPAIRED QUOTATION SEMANTIC CHECK"):
                return {
                    "verdicts": [
                        dict(
                            id=request["id"],
                            supported=False,
                            consistent=False,
                            reason="Missing laboratory condition.",
                            retained_quote_ids=[
                                identity for quotes in request["retained_quotes"].values() for identity in quotes
                            ],
                            additional_qualifiers=[],
                            additional_support_quotes=[],
                        )
                    ]
                }
            if self.calls == 1:
                return {
                    "verdicts": [
                        dict(
                            id=request["id"],
                            supported=first_supported,
                            consistent=first_consistent,
                            reason="Untrustworthy qualification.",
                            qualifiers=["ProviderB"],
                            support_quotes=[condition],
                        )
                    ]
                }
            return {
                "verdicts": [
                    dict(
                        id=request["id"],
                        supported=False,
                        consistent=False,
                        reason="Missing laboratory condition.",
                        qualifiers=[condition] if condition in request["ancestor_context"] else [],
                        support_quotes=[],
                    )
                ]
            }

    result = await source_constraints.verify(
        Client(), record, text, char_budget=6000, excerpt_budget=2000, binding="fresh"
    )
    assert result["status"] == "unsupported" and not result["supported"]


@pytest.mark.asyncio
async def test_pending_quote_stage_invalidates_on_complete_request_material_change():
    fact = "The device outputs text."
    text = "# Manual\n" + "Setup guidance.\n" * 170 + "## Device\n" + fact + "\n"
    record = Evidence("s", "manual", "Device", "quote", fact, quote=fact)
    snapshots = []

    class Outage:
        async def chat_verdicts(self, system, user, *, char_budget):
            frame = json.loads(user.split("\n", 1)[1])
            if user.startswith("SOURCE LITERAL QUOTATION REPAIR"):
                raise ModelError("Inference unavailable", transient=True)
            return {
                "verdicts": [
                    dict(
                        id=frame["id"],
                        supported=False,
                        consistent=True,
                        reason="Scope.",
                        qualifiers=["invented"],
                        support_quotes=[],
                    )
                ]
            }

    with pytest.raises(ModelError, match="Inference unavailable"):
        await source_constraints.verify(
            Outage(),
            record,
            text,
            char_budget=6000,
            excerpt_budget=2000,
            binding="same",
            progress=lambda rows: snapshots.append(json.loads(json.dumps(rows))),
        )
    known = snapshots[-1][curate.evidence_id(record)]
    assert known["verification_pending"] and not known["supported"] and known["pending_quote_repairs"]
    record.applicability = {"edition": "Enterprise"}

    class Changed:
        calls = 0

        async def chat_verdicts(self, system, user, *, char_budget):
            self.calls += 1
            assert not user.startswith("SOURCE LITERAL QUOTATION REPAIR")
            frame = json.loads(user.split("\n", 1)[1])
            assert frame["applicability"] == {"edition": "Enterprise"}
            return {
                "verdicts": [
                    dict(
                        id=frame["id"],
                        supported=False,
                        consistent=True,
                        reason="Enterprise edition not stated.",
                        qualifiers=[],
                        support_quotes=[],
                    )
                ]
            }

    result = await source_constraints.verify(
        Changed(),
        record,
        text,
        char_budget=6000,
        excerpt_budget=2000,
        binding="same",
        known=known,
    )
    assert result["status"] == "unknown" and not result["pending_quote_repairs"]
