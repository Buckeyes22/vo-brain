"""Bounded editor calls use constrained thinking without changing edit guards."""

import copy
import json

import httpx
import pytest
from test_article_repair_editing import change_for, result, run

from doclayer.curator import curate, editing
from doclayer.curator.model import ModelClient
from doclayer.curator.source_admission import _ReservedClient
from doclayer.curator.state import Evidence, PageChange, Usage


def response(value):
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(value)}, "finish_reason": "stop"}]},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("output_target", [4096, 512])
@pytest.mark.parametrize("invalid", ["identity", "anchor", "heading"])
async def test_native_edit_and_existing_format_retry_keep_strict_guards(cfg, output_target, invalid):
    cfg.inference.enable_thinking = False
    cfg.inference.output_token_target = output_target
    body = "# Device\n## Retention\nDevice retention is 3 days.\n"
    target = change_for(
        body,
        [(1, "Device retention is 3 days.", "Restore supported retention of 7 days.")],
    )
    evidence = Evidence(
        "source",
        "Manual",
        "# Retention",
        "quote",
        "Device retention is 7 days.",
        quote="Device retention is 7 days.",
        support_status="supported",
    )
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        user = payload["messages"][1]["content"]
        assert payload["max_tokens"] == output_target
        assert payload["thinking_token_budget"] == min(2048, output_target // 2)
        assert payload["chat_template_kwargs"] == {"enable_thinking": True}
        assert payload["response_format"] == {"type": "json_object"}
        assert "Claim IDs to integrate or prove already present: []" in user
        valid = result(user, [("Device retention is 3 days.", "Device retention is 7 days.")])
        if len(seen) == 1:
            if invalid == "identity":
                valid["handled_claim_ids"] = ["foreign-claim"]
            elif invalid == "anchor":
                valid["edits"][0]["old"] = "Absent old text."
            else:
                valid["append"] = "## Unauthorized peer heading\n"
        else:
            assert "Previous edit failed:" in user
        return response(valid)

    usage = Usage(model_calls=5)
    native = ModelClient(
        cfg,
        usage,
        limits={"max_model_calls": 7},
        transport=httpx.MockTransport(handler),
    )
    wrapped = _ReservedClient(native, reserve=0)
    edited = await run(wrapped, target, body, [evidence])
    assert edited["body"] == body.replace("3 days", "7 days")
    assert usage.model_calls == 7 and len(seen) == 2
    assert cfg.inference.enable_thinking is False
    assert edited["draft_progress"]["editor_version"] == editing.EDITOR_VERSION
    completed = copy.deepcopy(edited["draft_progress"]["completed_task_ids"])
    assert completed
    # Accepted edits/checkpoints do not become fresh model work after the policy
    # change or restart; the same exhausted Usage remains sufficient to resume.
    target.draft_body = edited["body"]
    target.draft_progress = edited["draft_progress"]
    resumed = await run(wrapped, target, edited["body"], [evidence])
    assert resumed["body"] == edited["body"]
    assert resumed["draft_progress"]["completed_task_ids"] == completed
    assert usage.model_calls == 7 and len(seen) == 2


@pytest.mark.asyncio
async def test_native_section_routing_uses_same_profile(cfg):
    cfg.inference.enable_thinking = False
    cfg.inference.output_token_target = 512
    record = Evidence(
        "s",
        "Manual",
        "# Setup",
        "quote",
        "Device setup is supported.",
        quote="Device setup is supported.",
        support_status="supported",
    )
    target = PageChange("g", "wiki/device.md", "update", draft_body="# Device\n## Setup\nKnown setup.\n")
    units = editing.page_units(target.draft_body)
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        assert payload["max_tokens"] == 512 and payload["thinking_token_budget"] == 256
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["chat_template_kwargs"] == {"enable_thinking": True}
        assert "Use one brief sentence for rationale" in payload["messages"][0]["content"]
        assert "at most 600 characters" in payload["messages"][0]["content"]
        return response(
            {
                "routes": [
                    {
                        "id": curate.evidence_id(record),
                        "section_id": units[1].id,
                        "new_heading": None,
                        "rationale": "This is the actual setup section.",
                    }
                ]
            }
        )

    native = ModelClient(cfg, Usage(), transport=httpx.MockTransport(handler))
    routes = await editing.route_claims(
        native,
        "Device",
        target,
        [record],
        units,
        {},
        char_budget=6000,
        progress=lambda: None,
    )
    assert routes[curate.evidence_id(record)]["section_id"] == units[1].id
    assert len(seen) == native.usage.model_calls == 1
