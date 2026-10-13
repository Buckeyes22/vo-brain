"""Declared scope is not a literal source quote; the model must repair it."""

import json

import pytest

from doclayer.curator import curate, source_constraints
from doclayer.curator.state import Evidence


def material():
    fact = "Function tools are available in the Responses API."
    text = (
        "# Manual\nIn the Responses API, the application chooses tool availability.\n"
        + "Unrelated setup guidance.\n" * 120
        + "## Supported tools\n"
        + fact
        + "\nProviderA publication note.\n"
    )
    record = Evidence(
        "s",
        "manual",
        "Supported tools",
        "quote",
        fact,
        quote=fact,
        applicability={"provider": "ProviderA", "scope": "Responses API"},
    )
    return record, text, fact


@pytest.mark.asyncio
async def test_model_repairs_metadata_label_without_dropping_literal_scope():
    record, text, fact = material()
    snapshots = []

    class Client:
        def __init__(self):
            self.calls = 0
            self.repaired = False

        async def chat_verdicts(self, system, user, *, char_budget):
            assert len(system) + len(user) <= char_budget
            assert "Metadata may identify scope but never supply missing factual support." in system
            request = json.loads(user.split("\n", 1)[1])
            body = request["ancestor_context"]
            self.calls += 1
            if self.calls == 1:
                return {
                    "verdicts": [
                        dict(
                            id=request["id"],
                            supported=False,
                            consistent=True,
                            reason="Scope only.",
                            qualifiers=["Responses API", "ProviderA"],
                            support_quotes=[],
                        )
                    ]
                }
            if self.calls == 2:
                assert "qualifiers[1] was not shown" in user
                assert "applicability.provider metadata" in user
                assert "Preserve every actual source condition" in system
                self.repaired = True
                return {
                    "verdicts": [
                        {"id": request["id"], "edits": [{"field": "qualifiers", "index": 1, "replacement": None}]}
                    ]
                }
            supported = fact in body or fact in request["previous_support_quotes"]
            return {
                "verdicts": [
                    dict(
                        id=request["id"],
                        supported=supported,
                        consistent=True,
                        reason="Literal source facts only.",
                        qualifiers=["Responses API"] if "Responses API" in body else [],
                        support_quotes=[fact] if fact in body else [],
                    )
                ]
            }

    client = Client()
    result = await curate.verify_source_claims(
        client,
        [record],
        {"s": text},
        char_budget=6000,
        progress=lambda values: snapshots.append(json.loads(json.dumps(values))),
    )
    proof = result[curate.evidence_id(record)]
    assert client.repaired and proof["supported"]
    assert "Responses API" in proof["excerpt"] and "ProviderA" not in proof["excerpt"]
    for snapshot in snapshots:
        for verdict in snapshot[curate.evidence_id(record)].get("ancestor_verdicts", {}).values():
            assert "ProviderA" not in verdict["qualifiers"]


@pytest.mark.asyncio
async def test_repeated_metadata_quote_remains_unresolved_and_never_auto_admitted():
    record, text, _ = material()
    snapshots = []

    class Client:
        calls = 0

        async def chat_verdicts(self, system, user, *, char_budget):
            self.calls += 1
            request = json.loads(user.split("\n", 1)[1])
            return {
                "verdicts": [
                    dict(
                        id=request["id"],
                        supported=False,
                        consistent=True,
                        reason="Scope only.",
                        qualifiers=["Responses API", "ProviderA"],
                        support_quotes=[],
                    )
                ]
            }

    client = Client()
    result = await curate.verify_source_claims(
        client,
        [record],
        {"s": text},
        char_budget=6000,
        progress=lambda values: snapshots.append(json.loads(json.dumps(values))),
    )
    failed = result[curate.evidence_id(record)]
    assert failed["status"] == "unknown" and failed["supported"] is False and failed["excerpt"] == ""
    assert client.calls == 3
    assert "applicability.provider metadata" in failed["review_error"]["diagnostic"]["validation_issues"][0]
    assert not any(v.get("ancestor_verdicts") for s in snapshots for v in s.values())
    assert snapshots[-1][curate.evidence_id(record)] == failed
    reused = await curate.verify_source_claims(client, [record], {"s": text}, char_budget=6000, known=result)
    assert reused == result and client.calls == 3


def test_metadata_matching_literal_condition_valid_but_metadata_only_fact_not():
    actual = "Only ProviderA enterprise deployments may invoke tools."
    value = dict(
        id="id",
        supported=True,
        consistent=True,
        reason="Actual qualified fact.",
        qualifiers=[actual],
        support_quotes=[actual],
    )
    assert not source_constraints._validation_issues(
        [value], "id", [("Manual", actual)], actual, "", [], [], metadata={"applicability": {"provider": "ProviderA"}}
    )
    invalid = {**value, "qualifiers": ["ProviderA"], "support_quotes": ["ProviderA"]}
    issues = source_constraints._validation_issues(
        [invalid],
        "id",
        [("Manual", "Generic tool API.")],
        "Generic tool API.",
        "",
        [],
        [],
        metadata={"applicability": {"provider": "ProviderA"}},
    )
    assert len(issues) == 2 and all("not a literal source span" in issue for issue in issues)
    assert all("applicability.provider metadata" in issue for issue in issues)
