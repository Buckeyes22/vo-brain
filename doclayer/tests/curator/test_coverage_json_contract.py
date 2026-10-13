"""Coverage JSON constraints are explicit, repairable and preserve full authority."""

import copy
import json

import httpx
import pytest

from doclayer.curator import placement
from doclayer.curator.model import ModelClient, ModelError
from doclayer.curator.state import Evidence, Usage


def original_valid(v, text, qualifiers):
    return (
        isinstance(v, dict)
        and type(v.get("covered")) is bool
        and type(v.get("consistent")) is bool
        and isinstance(v.get("anchor"), str)
        and isinstance(v.get("reason"), str)
        and isinstance(v.get("qualifiers"), list)
        and len(v["qualifiers"]) <= 8
        and all(isinstance(q, str) and q and len(q) <= 600 and (q in text or q in qualifiers) for q in v["qualifiers"])
        and (not v["covered"] or bool(v["anchor"] and v["anchor"] in text))
    )


def verdict(**kwargs):
    return dict(
        covered=True,
        consistent=True,
        anchor="Device stores files.",
        qualifiers=[],
        reason="complete scoped support",
        **kwargs,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("covered", "true"),
        ("covered", 1),
        ("consistent", None),
        ("consistent", "false"),
        ("anchor", None),
        ("anchor", ""),
        ("anchor", "foreign"),
        ("reason", 1),
        ("qualifiers", None),
        ("qualifiers", [""]),
        ("qualifiers", [None]),
        ("qualifiers", ["x" * 601]),
        ("qualifiers", ["foreign"]),
        ("qualifiers", ["Condition."] * 9),
        ("qualifiers", ["Condition."]),
        ("qualifiers", ["Prior condition."]),
        ("qualifiers", ["x" * 600]),
    ],
)
def test_coverage_acceptance_identical_to_original(field, value):
    reply = verdict()
    reply[field] = value
    text = "Device stores files. Condition. " + "x" * 600
    previous = ["Prior condition."]
    assert (not placement._coverage_issues(reply, text, previous)) == bool(original_valid(reply, text, previous))


@pytest.mark.asyncio
async def test_actual_http_repair_states_limits_all_failed_fields_and_preserves_whole_material(cfg):
    conditions = [f"Condition {i} applies." for i in range(9)]
    text = "# Device\nDevice stores files.\n" + "\n".join(conditions)
    record = Evidence(
        "manual",
        "Manual",
        "# Device",
        "quote",
        "Device stores files.",
        claim_id="fact",
        quote="Device stores files.",
        support_status="supported",
    )
    requests = []
    invalid = dict(
        covered="true",
        consistent=None,
        anchor="invented anchor",
        reason=42,
        qualifiers=["x" * 601, "foreign condition"] + conditions,
    )
    fixed = dict(
        covered=True,
        consistent=True,
        anchor="Device stores files.",
        reason="complete context",
        qualifiers=["\n".join(conditions)],
    )

    def handler(request):
        frame = json.loads(request.content)
        system, user = [m["content"] for m in frame["messages"]]
        requests.append((system, user))
        assert "at most 8" in system and "600 characters" in system
        assert text in user and record.text in user
        if len(requests) == 2:
            for key in [
                "covered must be a JSON boolean",
                "consistent must be a JSON boolean",
                "reason must be a string",
                "qualifiers count 11",
                "qualifiers[0] length 601",
                "qualifiers[1] must match literal",
            ]:
                assert key in system
            # A rejected literal is named with its bounded value; oversized values are never echoed.
            assert 'qualifiers[1] "foreign condition" is not in the canonical passage' in system
            assert "foreign condition" not in user and "x" * 601 not in user + system
            assert user == requests[0][1], (
                "the complete original claim/source frame must be byte-identical during repair"
            )
        result = invalid if len(requests) == 1 else fixed
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(result)}, "finish_reason": "stop"}]}
        )

    usage = Usage()
    client = ModelClient(cfg, usage, transport=httpx.MockTransport(handler))
    ledger = {"claims": {"fact": {}}}
    checkpoints = []
    assert await placement._covered(
        client,
        record,
        "wiki/device.md",
        {"wiki/device.md": ({}, text)},
        ledger,
        32768,
        lambda: checkpoints.append(copy.deepcopy(ledger)),
    )
    assert usage.model_calls == len(requests) == 2
    assert ledger["claims"]["fact"]["coverage"]["qualifiers"] == fixed["qualifiers"]
    assert len(ledger["claims"]["fact"]["coverage"]["checked"]) == 1
    assert checkpoints and all(
        "coverage" not in p["claims"]["fact"] or p["claims"]["fact"]["coverage"]["qualifiers"] == fixed["qualifiers"]
        for p in checkpoints
    )


@pytest.mark.asyncio
async def test_no_room_for_actionable_feedback_stops_without_clipping_or_extra_call():
    class Client:
        def __init__(self):
            self.calls = []

        async def chat_bounded_json(self, system, user, *, char_budget):
            assert len(system) + len(user) <= char_budget
            self.calls.append((system, user))
            return {}

    client = Client()
    source = "Complete mandatory source region."
    with pytest.raises(ModelError, match="cannot fit complete actionable field feedback") as exc:
        await placement._json(
            client,
            "Strict contract.",
            source,
            len("Strict contract.") + len(source) + 1,
            lambda v: False,
            diagnose=lambda v: {
                "validation_issues": ["anchor must be exact current source bytes"],
                "issue_count": 1,
                "omitted_issue_count": 0,
            },
        )
    assert client.calls == [("Strict contract.", source)]
    assert exc.value.diagnostic["validation_issues"] == ["anchor must be exact current source bytes"]


@pytest.mark.asyncio
async def test_existing_current_coverage_cache_reused_without_inference():
    record = Evidence(
        "manual",
        "Manual",
        "# Device",
        "quote",
        "Device stores files.",
        claim_id="fact",
        quote="Device stores files.",
        support_status="supported",
    )
    text = "# Device\nDevice stores files."

    class Client:
        async def chat_bounded_json(self, system, user, *, char_budget):
            return verdict()

    ledger = {"claims": {"fact": {}}}
    assert await placement._covered(
        Client(), record, "wiki/device.md", {"wiki/device.md": ({}, text)}, ledger, 32768, lambda: None
    )

    class NoCalls:
        async def chat_bounded_json(self, *args, **kwargs):
            raise AssertionError("verified unchanged cache must remain usable")

    ledger["claims"]["fact"]["status"] = "pending"
    assert await placement._covered(
        NoCalls(), record, "wiki/device.md", {"wiki/device.md": ({}, text)}, ledger, 32768, lambda: None
    )
    assert ledger["claims"]["fact"]["status"] == "covered"


@pytest.mark.asyncio
async def test_large_malformed_list_has_bounded_diagnostics_but_all_rows_counted():
    bad = dict(covered=True, consistent="wrong type", anchor="missing anchor", reason=False, qualifiers=[None] * 10000)
    diagnostic = placement._coverage_diagnostic(bad, "Actual authority.", [])
    assert diagnostic["issue_count"] == 10004
    assert diagnostic["omitted_issue_count"] == 9992
    assert len(diagnostic["validation_issues"]) == 12
    assert any("anchor must" in issue for issue in diagnostic["validation_issues"])
    assert any("count 10000" in issue for issue in diagnostic["validation_issues"])
    assert len(json.dumps(diagnostic)) < 2000

    class Client:
        def __init__(self):
            self.calls = []

        async def chat_bounded_json(self, system, user, *, char_budget):
            self.calls.append(user)
            if len(self.calls) > 1:
                assert "9992 omitted from diagnostics" in system
                assert "10004 validation issues total" in system
                assert "not only those listed" in system
                assert len(system) + len(user) <= char_budget
            return bad

    client = Client()
    with pytest.raises(ModelError) as exc:
        await placement._json(
            client,
            "Strict contract.",
            "Entire original source authority.",
            4500,
            lambda value: not placement._coverage_issues(value, "Actual authority.", []),
            diagnose=lambda value: placement._coverage_diagnostic(value, "Actual authority.", []),
        )
    assert len(client.calls) == 3
    assert exc.value.diagnostic["issue_count"] == 10004
    assert exc.value.diagnostic["omitted_issue_count"] == 9992
    assert len(exc.value.diagnostic["validation_issues"]) == 12
