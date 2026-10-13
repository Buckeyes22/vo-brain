"""Known location namespaces survive bounded format repair and profile upgrades."""

import copy
import json

import httpx
import pytest
from test_editorial import change, source

from doclayer.curator import editorial
from doclayer.curator.model import ModelClient, ModelError
from doclayer.curator.state import Usage


def reply(frame, bad=None):
    result = {
        "complete": True,
        "conflicts": [],
        "failures": [
            {
                "severity": "blocker",
                "check": "grounding",
                "detail": "Keep the supported qualifier",
                "section_id": frame["sections"][0]["id"],
                "anchor": None,
            }
        ],
    }
    if bad == "section":
        result["failures"][0]["section_id"] = "private fabricated value"
    if bad == "claim":
        result["failures"][0]["claim_id"] = "private fabricated value"
    return result


@pytest.mark.parametrize(
    "bad,field,reason",
    [("section", "section_id", "unknown-ID"), ("claim", "claim_id", "unassigned-ID")],
)
def test_location_diagnostics_are_source_free_and_validation_stays_strict(bad, field, reason):
    frame = {"sections": [{"id": "section-real"}]}
    with pytest.raises(ModelError) as caught:
        editorial._validate(reply(frame, bad), [{"id": "section-real", "text": "fact"}], set())
    diag = caught.value.diagnostic
    assert diag["location_issues"] == [{"index": 0, "field": field, "reason": reason, "type": "str"}]
    assert "private fabricated value" not in json.dumps(diag)
    feedback = editorial._format_feedback(diag, "shape")
    assert field in feedback and reason in feedback
    assert "sections[].id/required_claim_ids" in feedback
    assert len(json.dumps({"format_repair": feedback}, separators=(",", ":"))) <= editorial.FORMAT_REPAIR_RESERVE


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["section", "claim"])
async def test_real_http_location_repair_preserves_findings_and_charges_both_calls(cfg, bad):
    target = change("# Device\nDevice sometimes lowers costs.\n")
    frames = []

    def handler(request):
        payload = json.loads(request.content)
        system, user = [x["content"] for x in payload["messages"]]
        assert len(system) + len(user) <= 6000
        frame = json.loads(user.split("\n", 1)[1])
        frames.append(frame)
        if len(frames) == 2:
            assert ("section_id" if bad == "section" else "claim_id") in frame["format_repair"]
            assert {k: v for k, v in frame.items() if k != "format_repair"} == frames[0]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps(reply(frame, bad if len(frames) == 1 else None))},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    usage = Usage(model_calls=5)
    result = await editorial.review(
        ModelClient(
            cfg,
            usage,
            limits={"max_model_calls": 7},
            transport=httpx.MockTransport(handler),
        ),
        "Device",
        target,
        [source()],
        [],
        char_budget=6000,
    )
    assert usage.model_calls == 7 and len(frames) == 2
    assert result["failures"][0]["severity"] == "blocker"


@pytest.mark.asyncio
@pytest.mark.parametrize("upgrade_valid", [False, True])
async def test_old_exhausted_feedback_upgrade_keeps_accepted_results_usage_and_history(cfg, store, upgrade_valid):
    # Force two section tasks, then preserve the first accepted task while the
    # second task remains an exhausted old generic-format checkpoint.
    target = change("## First\n" + "Device fact. " * 180 + "\n## Second\n" + "Device fact. " * 180)
    usage = Usage()
    frames = []

    def handler(request):
        frame = json.loads(json.loads(request.content)["messages"][1]["content"].split("\n", 1)[1])
        frames.append(frame)
        result = {"complete": True, "conflicts": [], "failures": []} if len(frames) == 1 else reply(frame, "section")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps(result)},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    with pytest.raises(ModelError, match="failure location"):
        await editorial.review(
            ModelClient(cfg, usage, transport=httpx.MockTransport(handler)),
            "Device",
            target,
            [source("Device fact.")],
            [],
            char_budget=6000,
        )
    progress = target.check_progress["article_review"]
    accepted = copy.deepcopy(progress["results"])
    assert len(accepted) == 1 and usage.model_calls == 3
    key = progress["active_task"]
    progress["format_repairs"][key].pop("profile")
    old = copy.deepcopy(progress["format_repairs"][key])
    from doclayer.curator.state import JobState

    state = store.create(JobState("", "locations", "Device", changes=[target], usage=usage))[0]
    restored = store.load(state.job_id)
    target = restored.changes[0]
    calls = []

    def malformed(request):
        frame = json.loads(json.loads(request.content)["messages"][1]["content"].split("\n", 1)[1])
        calls.append(frame)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                reply(
                                    frame,
                                    None if upgrade_valid and len(calls) == 2 else "section",
                                )
                            )
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    async def run():
        return await editorial.review(
            ModelClient(
                cfg,
                restored.usage,
                limits={"max_model_calls": 5},
                transport=httpx.MockTransport(malformed),
            ),
            "Device",
            target,
            [source("Device fact.")],
            [],
            char_budget=6000,
            progress=lambda _: store.save(restored),
        )

    if upgrade_valid:
        result = await run()
        assert result["failures"][0]["severity"] == "blocker"
    else:
        with pytest.raises(ModelError, match="failure location"):
            await run()
    current = target.check_progress["article_review"]
    assert accepted.items() <= current["results"].items()
    assert len(current["results"]) == len(accepted) + int(upgrade_valid)
    assert current["format_repair_history"][key] == [old]
    assert current["format_repairs"][key]["profile"] == editorial.FORMAT_REPAIR_PROFILE
    assert current["format_repairs"][key].get("exhausted", False) is (not upgrade_valid)
    assert restored.usage.model_calls == 5 and len(calls) == 2
    restored = store.load(state.job_id)
    target = restored.changes[0]
    if upgrade_valid:
        await run()
    else:
        with pytest.raises(ModelError, match="format repair exhausted"):
            await run()
    assert restored.usage.model_calls == 5 and len(calls) == 2


@pytest.mark.parametrize("index", [0, 100, 9223372036854775807])
@pytest.mark.parametrize("typename", ["NoneType", "str", "int", "float", "dict", "list", "bool"])
def test_longest_location_feedback_fits_reserved_actual_frame_overhead(index, typename):
    feedback = editorial._format_feedback(
        {
            "location_issues": [
                {
                    "index": index,
                    "field": "claim_id",
                    "reason": "missing-assigned-ID",
                    "type": typename,
                }
            ]
        },
        "shape",
    )
    original = {"sections": [{"id": "supplied"}], "body": "complete source and article"}
    repaired = {**original, "format_repair": feedback}

    def packed(x):
        return json.dumps(x, ensure_ascii=False, separators=(",", ":"))

    assert len(packed(repaired)) - len(packed(original)) <= editorial.FORMAT_REPAIR_RESERVE
    assert packed(original)[1:-1] in packed(repaired)
