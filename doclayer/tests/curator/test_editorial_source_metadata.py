"""Lossless repeated-source packing preserves complete bounded reviews."""

import copy
import json

import httpx
import pytest

from doclayer.curator import editorial
from doclayer.curator.model import ModelClient
from doclayer.curator.state import Evidence, PageChange, Usage


def records():
    return [
        Evidence(
            "s",
            "Public documentation " + "qualified source " * 50,
            "## Scope",
            "quote",
            f"Device fact {i} applies only when configured.",
            claim_id=f"c{i}",
            quote=f"Exact source fact {i}; condition must remain.",
            support_status="supported",
            url="https://official.example/" + "source/" * 40,
            publisher="Official project",
            published_info="2026",
            applicability={"mode": "configured"},
        )
        for i in range(12)
    ]


def reconstruct(frame):
    result = []
    for row in frame["evidence"]:
        row = copy.deepcopy(row)
        if "source_ref" in row:
            row.update(frame["source_metadata"][row.pop("source_ref")])
        if "quote_ref" in row:
            row["quote"] = frame["source_excerpts"][row.pop("quote_ref")]
        result.append(row)
    return result


@pytest.mark.asyncio
async def test_native_complete_records_reconstruct_and_fit(cfg):
    evidence = records()
    target = PageChange(
        "g",
        "wiki/device.md",
        "create",
        title="Device",
        draft_body="# Device\n\nDevice fact0 applies only when configured.\n",
        draft_meta={"title": "Device"},
        claim_ids=[e.claim_id for e in evidence],
    )
    frames = []
    expected = editorial._records(evidence)

    def handler(request):
        payload = json.loads(request.content)
        system, user = [v["content"] for v in payload["messages"]]
        assert len(system) + len(user) <= 11000
        frame = json.loads(user.split("\n", 1)[1])
        assert editorial.SOURCE_METADATA_GUIDANCE in system
        assert sorted(reconstruct(frame), key=lambda r: r["id"]) == sorted(expected, key=lambda r: r["id"])
        assert frame["body"] == target.draft_body
        assert frame["required_claim_ids"] == sorted(target.claim_ids)
        frames.append(frame)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps({"complete": True, "failures": [], "conflicts": []})},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    usage = Usage()
    result = await editorial.review(
        ModelClient(cfg, usage, transport=httpx.MockTransport(handler)),
        "Device",
        target,
        evidence,
        [],
        char_budget=11000,
    )
    assert result["complete"] and len(frames) == usage.model_calls == 1


@pytest.mark.asyncio
async def test_small_task_keeps_existing_wire_and_grounding_policy(cfg):
    evidence = records()[:1]
    target = PageChange(
        "g",
        "wiki/device.md",
        "create",
        draft_body="# Device\n\nDevice fact0 applies only when configured.\n",
    )

    def handler(request):
        payload = json.loads(request.content)
        assert payload["messages"][0]["content"] == editorial.GROUNDING_SYSTEM
        frame = json.loads(payload["messages"][1]["content"].split("\n", 1)[1])
        assert "source_metadata" not in frame and "source_ref" not in frame["evidence"][0]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps({"complete": True, "failures": [], "conflicts": []})},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    await editorial.review(
        ModelClient(cfg, Usage(), transport=httpx.MockTransport(handler)),
        "Device",
        target,
        evidence,
        [],
        char_budget=11000,
    )


@pytest.mark.asyncio
async def test_genuine_complete_source_overflow_still_zero_dispatch(cfg):
    evidence = records()
    for e in evidence:
        e.quote += "Full source qualification. " * 1000
    # The sentence cites its source, so its complete records are what must fit
    # (an uncited new sentence would instead be reported as lacking a citation).
    target = PageChange(
        "g",
        "wiki/device.md",
        "create",
        draft_body="# Device\n\nDevice fact0 applies only when configured [source: " + evidence[0].url + "].\n",
    )
    usage = Usage()

    def handler(request):
        raise AssertionError("Overflow must not dispatch")

    with pytest.raises(editorial.EditorialContextOverflow):
        await editorial.review(
            ModelClient(cfg, usage, transport=httpx.MockTransport(handler)),
            "Device",
            target,
            evidence,
            [],
            char_budget=11000,
        )
    assert usage.model_calls == 0


@pytest.mark.asyncio
async def test_split_sections_keep_complete_records_and_parent_context(cfg):
    evidence = records()
    root = "# Device\n\nOnly configured mode applies.\n\n"
    first = "## First\n\n" + "Device fact0 applies only when configured. " * 40 + "\n\n"
    second = "## Second\n\n" + "Device fact1 applies only when configured. " * 40 + "\n"
    target = PageChange("g", "wiki/device.md", "create", draft_body=root + first + second)
    shown = []
    expected = editorial._records(evidence)

    def handler(request):
        payload = json.loads(request.content)
        system, user = [v["content"] for v in payload["messages"]]
        assert len(system) + len(user) <= 11000
        frame = json.loads(user.split("\n", 1)[1])
        assert sorted(reconstruct(frame), key=lambda r: r["id"]) == sorted(expected, key=lambda r: r["id"])
        shown.append(frame["body"])
        if "##" in frame["body"] and root not in frame["body"]:
            assert any(item["text"] == root for item in frame["governing_context"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps({"complete": True, "failures": [], "conflicts": []})},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    usage = Usage()
    await editorial.review(
        ModelClient(cfg, usage, transport=httpx.MockTransport(handler)),
        "Device",
        target,
        evidence,
        [],
        char_budget=11000,
    )
    assert len(shown) > 1 and "".join(shown) == target.draft_body
    assert usage.model_calls == len(shown)


def test_compact_refs_are_frame_local_deterministic_and_lossless():
    rows = editorial._records(records())
    rows[1]["role"] = "supplementary"
    original = copy.deepcopy(rows)
    frame = {"evidence": rows}
    editorial._compress_source_metadata(frame)
    assert set(frame["source_metadata"]) == {"s0", "s1"}
    assert frame["evidence"][0]["source_ref"] == "s0"
    assert frame["evidence"][1]["source_ref"] == "s1"
    assert all(r["source_ref"] == "s0" for r in frame["evidence"][2:])
    for before, after in zip(original, frame["evidence"], strict=True):
        recovered = {k: v for k, v in after.items() if k != "source_ref"}
        recovered.update(frame["source_metadata"][after["source_ref"]])
        assert recovered == before
    repeated = {"evidence": copy.deepcopy(original)}
    editorial._compress_source_metadata(repeated)
    assert repeated == frame
    compact_chars = len(json.dumps(frame))
    legacy = copy.deepcopy(frame)
    for row in legacy["evidence"]:
        row["source_ref"] = "source-" + "a" * 64
    assert len(json.dumps(legacy)) - compact_chars > 800


@pytest.mark.asyncio
async def test_required_batches_send_exact_bounded_prepared_representation(cfg):
    evidence = records()
    for index, item in enumerate(evidence):
        item.quote = f"Exact complete qualified evidence {index}. " * 40
    expected = {row["id"]: row for row in editorial._records(evidence)}
    target = PageChange(
        "g",
        "wiki/device.md",
        "create",
        draft_body="# Device\n\nDevice facts apply only when configured.\n",
        claim_ids=[item.claim_id for item in evidence],
    )
    admitted = []
    compressed = []

    def handler(request):
        payload = json.loads(request.content)
        system, user = [item["content"] for item in payload["messages"]]
        assert len(system) + len(user) <= 11000
        frame = json.loads(user.split("\n", 1)[1])
        assert frame["mode"] == "required" and frame["body"] == target.draft_body
        recovered = reconstruct(frame)
        assert recovered == [expected[row["id"]] for row in recovered]
        admitted.extend(frame["required_claim_ids"])
        compressed.append("source_metadata" in frame)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps({"complete": True, "failures": [], "conflicts": []})},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    usage = Usage()
    await editorial.review(
        ModelClient(cfg, usage, transport=httpx.MockTransport(handler)),
        "Device",
        target,
        evidence,
        [],
        char_budget=11000,
        mode="required",
    )
    assert usage.model_calls > 1 and any(compressed)
    assert sorted(admitted) == sorted(target.claim_ids)
