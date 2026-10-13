"""Literal interning preserves complete record-scoped evidence and qualifiers."""

import copy
import json

import httpx
import pytest

from doclayer.curator import editorial
from doclayer.curator.model import ModelClient
from doclayer.curator.state import Evidence, PageChange, Usage


def reconstruct(frame):
    rows = []
    for original in frame["evidence"]:
        row = copy.deepcopy(original)
        row.update(frame["source_metadata"][row.pop("source_ref")])
        excerpt = frame["source_excerpts"][row.pop("quote_ref")]
        row["quote"] = "".join(frame["source_lines"][index] for index in excerpt)
        rows.append(row)
    return rows


def evidence():
    lines = [f"Line {i}: café 条件 applies only when configured; preserve this qualification.\r\n" for i in range(40)]
    lines[4:7] = ["```python\r\n", "\r\n", "```\n"]
    return [
        Evidence(
            f"s{i % 3}",
            f"Distinct source {i % 3}",
            "## Scoped facts",
            "quote",
            f"Device fact {i} applies only in mode {i % 5}.",
            claim_id=f"c{i}",
            quote=f"Source {i % 3}\n" + "".join(lines[i % 10 : i % 10 + 25]),
            support_status="supported",
            applicability={"mode": str(i % 5)},
            url=f"https://official.example/source/{i % 3}",
        )
        for i in range(31)
    ]


def test_lossless_literal_lines_and_all_record_fields_without_model_calls():
    expected = editorial._records(evidence())
    frame = {"evidence": copy.deepcopy(expected), "source_excerpts": {}}
    for i, row in enumerate(frame["evidence"]):
        ref = f"q{i}"
        frame["source_excerpts"][ref] = row.pop("quote")
        row["quote_ref"] = ref
    editorial._compress_source_metadata(frame)
    editorial._compress_source_lines(frame)
    assert reconstruct(frame) == expected
    assert len(frame["evidence"]) == 31
    assert all("applicability" in row for row in frame["evidence"])
    assert len({r["source_id"] for r in reconstruct(frame)}) == 3
    snapshot = copy.deepcopy(frame)
    editorial._compress_source_lines(frame)
    assert frame == snapshot


@pytest.mark.parametrize("quote", ["", "no final newline", "\r\n\r\n", "é\u2028条\r\n```\n\n```"])
def test_exact_empty_and_mixed_line_endings(quote):
    frame = {"evidence": [], "source_metadata": {}, "source_excerpts": {"q": quote}}
    editorial._compress_source_lines(frame)
    assert "".join(frame["source_lines"][i] for i in frame["source_excerpts"]["q"]) == quote


@pytest.mark.asyncio
async def test_overflow_review_roundtrip_bound_and_restart(cfg, monkeypatch):
    records = evidence()
    target = PageChange(
        "g",
        "wiki/device.md",
        "create",
        title="Device",
        draft_body="# Device\n\nDevice facts apply only when configured.\n",
        draft_meta={"title": "Device"},
    )
    expected = editorial._records(records)
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        system, user = [m["content"] for m in payload["messages"]]
        assert len(system) + len(user) + editorial.FORMAT_REPAIR_RESERVE <= 32768
        frame = json.loads(user.split("\n", 1)[1])
        assert "source_lines" in frame
        assert editorial.SOURCE_LINES_GUIDANCE in system
        assert sorted(reconstruct(frame), key=lambda r: r["id"]) == sorted(expected, key=lambda r: r["id"])
        calls.append(frame)
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
    model = ModelClient(cfg, usage, transport=httpx.MockTransport(handler))
    result = await editorial.review(model, "Device", target, records, [], char_budget=32768)
    assert result["complete"] and usage.model_calls == len(calls) == 1
    replay = await editorial.review(model, "Device", target, records, [], char_budget=32768)
    assert replay == result and usage.model_calls == 1
    monkeypatch.setattr(
        editorial, "SOURCE_LINES_GUIDANCE", editorial.SOURCE_LINES_GUIDANCE + " Preserve ordered references."
    )
    refreshed = await editorial.review(model, "Device", target, records, [], char_budget=32768)
    assert refreshed == result and usage.model_calls == len(calls) == 2
