"""Actual operator IDs belong only to the completion task namespace."""

import copy
import json

import httpx
import pytest
from test_editorial import change, issue, source

from doclayer.curator import editorial
from doclayer.curator.model import ModelClient, ModelError
from doclayer.curator.state import PageChange, Usage


def response(value):
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value)}, "finish_reason": "stop"}]})


def frame_of(request):
    return json.loads(json.loads(request.content)["messages"][1]["content"].split("\n", 1)[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["grounding", "correction"])
@pytest.mark.parametrize("identity", ["corr-owned", "corr-foreign"])
async def test_actual_ledger_identity_is_completion_only(cfg, mode, identity):
    target = change("# Device\nNew assertion.\n")
    corrections = [{"correction_id": "corr-owned", "instruction": "Restore the documented condition."}]
    seen = []

    def handler(request):
        frame = frame_of(request)
        seen.append(frame)
        assert frame["required_claim_ids"] == (["corr-owned"] if frame["mode"] == "correction" else [])
        if frame["mode"] != mode:
            return response({"complete": True, "failures": [], "conflicts": []})
        return response(issue(frame, None, "correction-completion" if mode == "correction" else "grounding", identity))

    usage = Usage()
    client = ModelClient(cfg, usage, limits={"max_model_calls": 10}, transport=httpx.MockTransport(handler))
    if mode == "correction" and identity == "corr-owned":
        result = await editorial.review(client, "Device", target, [], corrections, char_budget=12000)
        assert result["failures"][0]["claim_id"] == identity
        assert result["failures"][0]["severity"] == "blocker"
        restored = PageChange.from_dict(target.to_dict())
        assert await editorial.review(client, "Device", restored, [], corrections, char_budget=12000) == result
        assert usage.model_calls == 2
    else:
        with pytest.raises(ModelError, match="failure location"):
            await editorial.review(client, "Device", target, [], corrections, char_budget=12000)
        assert usage.model_calls == (3 if mode == "correction" else 2)


@pytest.mark.asyncio
async def test_old_completion_format_failure_keeps_grounding_and_usage(cfg):
    target = change("# Device\nDevice supports the documented feature.\n")
    records = [source("Device supports the documented feature.")]
    corrections = [{"correction_id": "corr-owned", "instruction": "Retain feature and its source condition."}]
    frames = []

    def first(request):
        frame = frame_of(request)
        frames.append(frame)
        if frame["mode"] == "grounding":
            return response({"complete": True, "failures": [], "conflicts": []})
        return response(issue(frame, None, "correction-completion", "corr-foreign"))

    usage = Usage()
    client = ModelClient(cfg, usage, limits={"max_model_calls": 10}, transport=httpx.MockTransport(first))
    with pytest.raises(ModelError):
        await editorial.review(client, "Device", target, records, corrections, char_budget=12000)
    assert usage.model_calls == 3
    saved = copy.deepcopy(target.check_progress["article_review"])
    accepted = copy.deepcopy(saved["results"])
    assert len(accepted) == 1
    # Reconstruct the old emitted completion frame, which assigned no IDs.
    old_frame = {k: v for k, v in frames[1].items() if k != "format_repair"}
    old_frame["required_claim_ids"] = []
    old_key = editorial._hash(old_frame)
    repair = next(iter(saved["format_repairs"].values()))
    saved["format_repairs"] = {old_key: repair}
    saved["active_task"] = old_key
    target.check_progress["article_review"] = saved
    restored = PageChange.from_dict(target.to_dict())
    resumed = []

    def second(request):
        frame = frame_of(request)
        resumed.append(frame)
        assert frame["mode"] == "correction"
        assert frame["required_claim_ids"] == ["corr-owned"]
        return response(issue(frame, None, "correction-completion", "corr-owned"))

    fresh = ModelClient(cfg, usage, limits={"max_model_calls": 10}, transport=httpx.MockTransport(second))
    result = await editorial.review(fresh, "Device", restored, records, corrections, char_budget=12000)
    assert result["failures"][0]["claim_id"] == "corr-owned"
    assert usage.model_calls == 4 and len(resumed) == 1
    state = restored.check_progress["article_review"]
    assert state["format_repairs"][old_key] == repair
    for key, value in accepted.items():
        assert state["results"][key] == value
