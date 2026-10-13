"""Bounded edits disclose exact limits without weakening source/structure gates."""

import copy
import json
from types import SimpleNamespace

import pytest
from test_bounded_editing import response, routing_response, supported

from doclayer.curator import editing
from doclayer.curator.model import ModelError
from doclayer.curator.state import PageChange


def base(unit):
    return {
        "section_id": unit.id,
        "edits": [],
        "append": "",
        "handled_claim_ids": [],
        "claim_dispositions": [],
    }


@pytest.mark.parametrize(
    "change,field",
    [
        ({"section_id": "foreign"}, "section_id"),
        (
            {"edits": [{"old": "SECRET_UNSHOWN_SOURCE", "new": "SECRET_REPLACEMENT"}]},
            "edits[0].old",
        ),
        ({"edits": [{"old": "word", "new": 4}]}, "edits[0].new"),
        ({"edits": [{"old": "x" * 1501, "new": "a"}]}, "edits[0].old"),
        (
            {"edits": [{"old": "word", "new": "x"}, {"old": "word here", "new": "y"}]},
            "edits[1].old",
        ),
        ({"handled_claim_ids": ["foreign"]}, "handled_claim_ids"),
        ({"append": "z" * 7000}, "response"),
        ({"append": "# New root"}, "append"),
    ],
)
def test_strict_rejections_return_source_free_field_index_and_bounds(change, field):
    unit = editing.page_units("# Device\nword here\n")[0]
    candidate = {**base(unit), **change}
    untouched = copy.deepcopy(candidate)
    with pytest.raises(ModelError) as caught:
        editing.apply_edits(unit, candidate, set(), max_output_chars=6000)
    diagnostic = caught.value.diagnostic
    assert any(issue["field"] == field for issue in diagnostic["issues"])
    assert "SECRET_" not in json.dumps(diagnostic)
    assert candidate == untouched and unit.text == "# Device\nword here\n"
    assert len(diagnostic["issues"]) <= 8 and diagnostic["max_output_chars"] == 6000


def test_independent_schema_failures_and_serialized_unicode_cap_all_visible():
    unit = editing.page_units("# Device\ntext\n")[0]
    reply = {
        "section_id": "wrong",
        "edits": False,
        "append": 4,
        "handled_claim_ids": [],
        "claim_dispositions": [],
    }
    with pytest.raises(ModelError, match="invalid bounded-edit response") as caught:
        editing.apply_edits(unit, reply, {"claim-a"}, max_output_chars=10)
    fields = {row["field"] for row in caught.value.diagnostic["issues"]}
    assert {
        "section_id",
        "edits",
        "append",
        "handled_claim_ids",
        "claim_dispositions",
        "response",
    } <= fields
    reply = {**base(unit), "append": "é" * 100}
    cap = len(json.dumps(reply, ensure_ascii=False))
    assert editing.apply_edits(unit, reply, set(), max_output_chars=cap)
    with pytest.raises(ModelError, match="material limit") as caught:
        editing.apply_edits(unit, reply, set(), max_output_chars=cap - 1)
    assert caught.value.diagnostic["serialized_chars"] == cap


@pytest.mark.parametrize(
    "status,anchor,append",
    [
        ("integrated", "word", ""),
        ("already_present", "fresh", "fresh"),
        ("invented-status", "fresh", "fresh"),
    ],
)
def test_disposition_diagnostics_preserve_exact_material_semantics(status, anchor, append):
    unit = editing.page_units("# Device\nword\n")[0]
    reply = {
        **base(unit),
        "append": append,
        "handled_claim_ids": ["claim-a"],
        "claim_dispositions": [{"id": "claim-a", "status": status, "anchor": anchor}],
    }
    with pytest.raises(ModelError) as caught:
        editing.apply_edits(unit, reply, {"claim-a"}, max_output_chars=6000)
    assert any(row["field"].startswith("claim_dispositions[0]") for row in caught.value.diagnostic["issues"])


@pytest.mark.asyncio
async def test_actual_editor_exposes_current_cap_and_repairs_precise_anchor_preserving_whole_inputs():
    record = supported("The device supports bounded mode only on enterprise firmware 3.")
    change = PageChange("g", "wiki/device.md", "update")
    calls = []

    class Client:
        cfg = SimpleNamespace(inference=SimpleNamespace(effective_output_target=900))

        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            calls.append((system, user))
            assert "serialized JSON response must be at most 2250 characters" in user
            assert record.text in user and record.quote in user
            if len(calls) == 1:
                return response(user, edits=[{"old": "absent literal", "new": record.text}])
            assert "edits[0].old" in user and "occurrences" in user
            assert calls[0][1] in user
            return response(user)

    result = await editing.edit_page(
        Client(),
        "Whole qualification",
        change,
        [record],
        "# Device\nOperator fact.\n",
        [],
        char_budget=6000,
        today="2026-10-02",
    )
    assert len(calls) == 2 and record.text in result["body"] and "Operator fact." in result["body"]
    assert all(len(system) + len(user) <= 6000 for system, user in calls)
    assert result["draft_progress"]["editor_version"] == editing.EDITOR_VERSION


def test_feedback_boundary_fails_actionably_instead_of_silently_losing_guidance():
    error = ModelError(
        "bounded edit anchors overlap",
        diagnostic={
            "operation": "bounded-edit",
            "issues": [{"field": "edits[1].old", "reason": "overlap"}],
        },
    )
    with pytest.raises(ModelError, match="actionable feedback") as caught:
        editing._edit_feedback(error, 20, 1800)
    assert caught.value.diagnostic["feedback_chars_available"] == 20
    assert caught.value.diagnostic["issues"] == error.diagnostic["issues"]


@pytest.mark.asyncio
async def test_charged_http_invalid_edits_then_outage_preserves_restart_task(cfg, store):
    import httpx

    from doclayer.curator.model import ModelClient
    from doclayer.curator.state import JobState

    original = "# Device\nOperator-owned fact.\n"
    record = supported("The device supports bounded mode only on firmware 3.")
    state = store.create(JobState("", "edit-contract-restart", "Device"))[0]
    state.changes = [PageChange("g", "wiki/device.md", "update")]
    calls = []
    stopped = False
    cfg.inference.output_target = 900
    cfg.limits.transient_retries = 0

    def handler(request):
        nonlocal stopped
        system, user = [message["content"] for message in json.loads(request.content)["messages"]]
        calls.append((system, user))
        if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
            reply = routing_response(user)
        elif not stopped:
            stopped = True
            reply = response(user, edits=[{"old": "nonexistent", "new": record.text}])
        else:
            assert "edits[0].old" in user and "occurrences" in user
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}]},
        )

    def retain(result):
        change = state.changes[0]
        change.draft_body = result["body"]
        change.draft_meta = result["frontmatter"]
        change.draft_progress = result["draft_progress"]
        store.save(state)

    client = ModelClient(cfg, state.usage, transport=httpx.MockTransport(handler))
    with pytest.raises(ModelError):
        await editing.edit_page(
            client,
            state.intent,
            state.changes[0],
            [record],
            original,
            [],
            char_budget=6000,
            today="2026-10-02",
            progress=retain,
        )
    saved = store.load(state.job_id)
    assert saved.changes[0].draft_body == original
    assert not saved.changes[0].draft_progress["completed_task_ids"]
    assert saved.changes[0].draft_progress["claim_routes"]
    # Model usage lives on the real job, rather than a detached probe budget.
    state = saved
    state.usage = client.usage
    store.save(state)
    before_calls = state.usage.model_calls
    resumed = []

    def valid(request):
        system, user = [message["content"] for message in json.loads(request.content)["messages"]]
        assert not user.startswith("BOUNDED EDIT CLAIM ROUTING")
        resumed.append((system, user))
        assert record.text in user and record.quote in user
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps(response(user))},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    resumed_client = ModelClient(cfg, state.usage, transport=httpx.MockTransport(valid))
    result = await editing.edit_page(
        resumed_client,
        state.intent,
        state.changes[0],
        [record],
        state.changes[0].draft_body,
        [],
        char_budget=6000,
        today="2026-10-02",
        progress=retain,
    )
    assert state.usage.model_calls == before_calls + 1 and len(resumed) == 1
    assert "Operator-owned fact." in result["body"] and record.text in result["body"]
    assert len(result["draft_progress"]["handled_claim_ids"]) == 1
