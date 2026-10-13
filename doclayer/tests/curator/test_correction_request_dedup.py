"""Native correction material stays complete without repeated task copies."""

import copy
import json
import re

import httpx
import pytest

from doclayer.curator import curate, editorial, ops
from doclayer.curator.model import ModelClient
from doclayer.curator.state import Usage


def test_topic_dedup_requires_exact_registered_instruction_and_preserves_suffix():
    page = "wiki/cms.md"
    instruction = "Correct the branch.\nComplete source: dev, with its qualifications."
    record = {"page": page, "instruction": instruction, "correction_id": "corr-id"}
    generated = f"Apply operator correction on {page}: {instruction}"
    suffix = "\nBinding source/applicability policy: {'platform': 'RealHost'}"
    assert curate.compact_correction_topic(generated + suffix, [record], page) == (
        "Apply operator correction corr-id on wiki/cms.md" + suffix
    )
    for topic in ["Unrelated intent " + generated, generated[:-1], "Document CMS"]:
        assert curate.compact_correction_topic(topic, [record], page) == topic
    assert curate.compact_correction_topic(generated, [record], "wiki/other.md") == generated
    assert record["instruction"] == instruction


@pytest.mark.asyncio
async def test_long_native_correction_uses_one_complete_record_and_preserves_history(cfg, store):
    cfg.limits.prompt_char_budget = 10000
    instruction = (
        "Add the observation without removing known facts.\n"
        + ("Complete original source line with unchanged qualifications.\n" * 85)
        + "END_OF_COMPLETE_SOURCE"
    )
    submitted = ops.handle(
        "correct", {"page": "wiki/existing-model.md", "claim": "Known facts.", "instruction": instruction}, cfg, store
    )
    job = store.load(submitted["job_id"])
    corrections = store.corrections(page="wiki/existing-model.md")
    change = job.changes[0]
    original = "# Existing model\n\n## Capabilities\n\nKnown facts.\n\n## Safety\n\nKnown safety notes.\n"
    frozen = copy.deepcopy((job.intent, change.rationale, corrections))
    assert len(job.intent) + len(change.rationale) > cfg.limits.prompt_char_budget
    usage = Usage()

    def handler(request):
        payload = json.loads(request.content)
        system, user = (message["content"] for message in payload["messages"])
        assert len(system) + len(user) + 1000 <= cfg.limits.prompt_char_budget
        assert user.startswith("Topic/intent: Apply operator correction corr-")
        encoded = user.split("Binding operator corrections: ", 1)[1].split("\n", 1)[0]
        assert json.loads(encoded) == corrections
        assert user.count("END_OF_COMPLETE_SOURCE") == 1
        reply = {
            "section_id": re.search(r"Section ID: (.+)", user)[1],
            "edits": [],
            "append": "Observed branch is dev. [source: operator correction]",
            "handled_claim_ids": [],
            "claim_dispositions": [],
            "notes": [],
        }
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}]}
        )

    result = await curate.draft_page(
        ModelClient(cfg, usage, transport=httpx.MockTransport(handler)),
        job.intent,
        change,
        [],
        original,
        corrections,
        char_budget=10000,
        today="2026-10-04",
    )
    assert usage.model_calls == 1
    assert "Known facts." in result["body"] and "Known safety notes." in result["body"]
    assert "Observed branch is dev." in result["body"]
    assert (job.intent, change.rationale, corrections) == frozen
    restored = store.load(job.job_id)
    assert restored.intent == frozen[0] and restored.changes[0].rationale == frozen[1]


@pytest.mark.asyncio
async def test_required_correction_review_keeps_complete_instruction_once(cfg):
    from doclayer.curator.state import PageChange

    page = "wiki/cms.md"
    instruction = "Restore the observed branch.\n" + "Original source qualification.\n" * 180 + "END_SOURCE"
    corrections = [{"page": page, "claim": "wrong branch", "instruction": instruction, "correction_id": "corr-id"}]
    topic = f"Apply operator correction on {page}: {instruction}"
    change = PageChange("g", page, "update", draft_body="# CMS\n\nObserved branch is dev.\n")
    usage = Usage()
    frames = []

    def handler(request):
        payload = json.loads(request.content)
        system, user = (message["content"] for message in payload["messages"])
        assert len(system) + len(user) <= 10000
        frame = json.loads(user.split("\n", 1)[1])
        assert frame["corrections"] == corrections
        assert user.count("END_SOURCE") == 1
        if frame["mode"] == "correction":
            assert frame["topic"] == "Apply operator correction corr-id on wiki/cms.md"
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

    result = await editorial.review(
        ModelClient(cfg, usage, transport=httpx.MockTransport(handler)),
        topic,
        change,
        [],
        corrections,
        char_budget=10000,
    )
    assert result["complete"] and any(frame["mode"] == "correction" for frame in frames)
    assert usage.model_calls == len(frames)
    assert corrections[0]["instruction"] == instruction
