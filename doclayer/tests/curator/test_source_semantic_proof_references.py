"""Semantic rechecks reference immutable source bytes instead of re-emitting code."""

import json

import pytest

from doclayer.curator import source_constraints
from doclayer.curator.model import ModelError

CODE = '"output": [{\n  "type": "message",\n  "content": [{"type": "refusal", "refusal": "I cannot comply."}]\n}]'
CONDITION = "Only laboratory installations support this interface."


def response(frame, **changes):
    return {
        "verdicts": [
            dict(
                id=frame["id"],
                supported=False,
                consistent=False,
                reason="The claim omits laboratory scope.",
                retained_quote_ids=[identity for quotes in frame["retained_quotes"].values() for identity in quotes],
                additional_qualifiers=[],
                additional_support_quotes=[],
                **changes,
            )
        ]
    }


async def check(client, *, candidate=None, body=None):
    body = body or CONDITION + "\n" + CODE
    candidate = candidate or dict(
        id="scope",
        supported=True,
        consistent=True,
        reason="Provisional.",
        qualifiers=[CONDITION],
        support_quotes=[CODE],
    )
    request = {
        "id": "scope",
        "claim": "All installations return refusal content items.",
        "ancestor_context": body,
        "applicability": {"provider": "MetadataProvider"},
    }
    return await source_constraints._reverify_repaired_semantics(
        client,
        candidate,
        request,
        [("Manual", body)],
        body,
        "",
        [],
        [],
        char_budget=6000,
        metadata={"applicability": request["applicability"]},
    )


@pytest.mark.asyncio
async def test_references_preserve_original_nested_json_and_condition_without_forced_positive():
    class Client:
        async def chat_verdicts(self, system, user, *, char_budget):
            assert len(system) + len(user) <= char_budget
            frame = json.loads(user.split("\n", 1)[1])
            assert frame["retained_quotes"] == {"qualifiers": {"q0": CONDITION}, "support_quotes": {"s0": CODE}}
            assert "untrusted" in system and "WHOLE claim" in system
            return response(frame)

    result = await check(Client())
    assert not result[0]["supported"] and not result[0]["consistent"]
    assert result[0]["qualifiers"] == [CONDITION] and result[0]["support_quotes"] == [CODE]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "foreign",
        "moved",
        "wrong-id",
        "nonboolean",
        "old-schema",
        "metadata",
        "hidden",
        "oversized",
    ],
)
async def test_bad_references_or_new_source_quotes_never_admit_evidence(mutation):
    class Invalid:
        calls = 0

        async def chat_verdicts(self, system, user, *, char_budget):
            self.calls += 1
            frame = json.loads(user.split("\n", 1)[1])
            reply = response(frame)
            row = reply["verdicts"][0]
            if mutation == "missing":
                row["retained_quote_ids"] = ["s0"]
            elif mutation == "duplicate":
                row["retained_quote_ids"] = ["q0", "q0"]
            elif mutation == "foreign":
                row["retained_quote_ids"] = ["q0", "foreign"]
            elif mutation == "moved":
                row["retained_quote_ids"] = ["s0", "s1"]
            elif mutation == "wrong-id":
                row["id"] = "foreign"
            elif mutation == "nonboolean":
                row["supported"] = "true"
            elif mutation == "old-schema":
                row.pop("retained_quote_ids")
                row["support_quotes"] = [CODE]
            elif mutation == "metadata":
                row["additional_qualifiers"] = ["MetadataProvider"]
            elif mutation == "hidden":
                row["additional_support_quotes"] = ["Unknown hidden fact."]
            elif mutation == "oversized":
                row["additional_qualifiers"] = ["x" * 601]
            return reply

    client = Invalid()
    with pytest.raises(ModelError, match="invalid repaired-source semantic response"):
        await check(client)
    assert client.calls == 2


@pytest.mark.asyncio
async def test_new_literal_conditions_are_added_separately_then_revalidated():
    extra = "Only version 3 is qualified."

    class Client:
        async def chat_verdicts(self, system, user, *, char_budget):
            frame = json.loads(user.split("\n", 1)[1])
            reply = response(frame)
            reply["verdicts"][0]["additional_qualifiers"] = [extra]
            return reply

    result = await check(Client(), body=CONDITION + "\n" + CODE + "\n" + extra)
    assert result[0]["qualifiers"] == [CONDITION, extra]


@pytest.mark.asyncio
async def test_no_quote_list_can_exceed_eight_after_reference_expansion():
    candidate = dict(
        id="scope",
        supported=True,
        consistent=True,
        reason="Provisional.",
        qualifiers=[CONDITION] * 8,
        support_quotes=[CODE],
    )

    class Client:
        async def chat_verdicts(self, system, user, *, char_budget):
            frame = json.loads(user.split("\n", 1)[1])
            reply = response(frame)
            reply["verdicts"][0]["additional_qualifiers"] = [CONDITION]
            return reply

    with pytest.raises(ModelError):
        await check(Client(), candidate=candidate)


@pytest.mark.asyncio
async def test_missing_reference_repair_gets_explicit_complete_ids_without_losing_conditions():
    class Client:
        calls = 0

        async def chat_verdicts(self, system, user, *, char_budget):
            self.calls += 1
            frame = json.loads(user.split("\n", 1)[1])
            assert frame["required_retained_quote_ids"] == ["q0", "s0"]
            assert "even for an unsupported or contradictory verdict" in system
            reply = response(frame)
            if self.calls == 1:
                reply["verdicts"][0]["retained_quote_ids"] = ["s0"]
            else:
                assert 'exactly ["q0", "s0"] once' in system
                assert CONDITION in frame["retained_quotes"]["qualifiers"]["q0"]
            return reply

    client = Client()
    result = await check(client)
    assert client.calls == 2 and result[0]["qualifiers"] == [CONDITION]
