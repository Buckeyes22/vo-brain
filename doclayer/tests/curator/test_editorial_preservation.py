"""Bounded paired article review retains old knowledge without claim ledgers."""

import copy
import json

import httpx
import pytest

from doclayer.curator.model import ModelClient
from doclayer.curator.preservation import (
    audit_existing_preservation,
    existing_repair_records,
)
from doclayer.curator.state import Evidence, JobState, PageChange, Usage

OLD = "# Device\n\n## Metrics\nUnder laboratory conditions, Device found 17 severe defects.\nDevice found three previously unknown defects.\n"
META = {"title": "Device", "edition": "Enterprise"}


def fact(text):
    return Evidence(
        "source-device",
        "Actual current source",
        "Metrics",
        "quote",
        text,
        quote=text,
        claim_id="fact-device",
        support_status="supported",
        applicability={"edition": "Enterprise"},
    )


def change(body):
    return PageChange("g", "wiki/device.md", "update", draft_meta=copy.deepcopy(META), draft_body=body)


async def audit(client, target, old=OLD, records=None, corrections=None, progress=None, **kwargs):
    return await audit_existing_preservation(
        client,
        "Device",
        target,
        records or [],
        corrections or [],
        original_meta=META,
        original_body=old,
        canonical_binding={"commit": "original-git", "blob": "actual-blob"},
        char_budget=12000,
        source_binding="actual-current-source-generation",
        source_policy={"exclude": ["unrelated-provider"]},
        progress=progress,
        **kwargs,
    )


def response(frame, detail=None):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "complete": True,
                                "failures": []
                                if detail is None
                                else [
                                    {
                                        "severity": "blocker",
                                        "check": "existing-preservation",
                                        "detail": detail,
                                        "section_id": frame["target_section_ids"][0],
                                        "anchor": None,
                                    }
                                ],
                                "conflicts": [],
                            }
                        )
                    },
                    "finish_reason": "stop",
                }
            ]
        },
    )


def model(cfg, handler, usage=None):
    return ModelClient(cfg, usage or Usage(), transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_exact_untouched_units_keep_metadata_scope_and_need_no_inference(cfg):
    def never(request):
        raise AssertionError("Unchanged original content needs no preservation inference")

    target = change(OLD + "## New\nNew incoming material.\n")
    target.claim_ids = ["required-new"]
    source = fact("Device found three previously unknown defects.")
    result = await audit(model(cfg, never), target, records=[source])
    assert result["complete"] and result["failures"] == []
    assert target.claim_ids == ["required-new"]
    assert result["repair_records"] == [source]
    assert "claims" not in target.check_progress["existing_preservation"]
    assert "verdicts" not in target.check_progress["existing_preservation"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,detail",
    [
        (
            "# Device\n\n## Metrics\nDevice completed the evaluation.\n",
            "Both actual old metrics disappeared.",
        ),
        (
            "# Device\n\n## Metrics\nDevice found 17 severe defects.\nOtherDevice found three previously unknown defects.\n",
            "Laboratory scope and original subject were lost.",
        ),
        (
            "# Device\n\n## Metrics\nNo findings are recorded.\n",
            "Unknown source support is not permission to erase old knowledge.",
        ),
    ],
)
async def test_removed_old_facts_and_scope_are_reviewed_against_complete_original(cfg, body, detail):
    frames = []

    def handler(request):
        payload = json.loads(request.content)
        system, user = [message["content"] for message in payload["messages"]]
        assert user.startswith("ARTICLE EDITORIAL REVIEW\n")
        frame = json.loads(user.split("\n", 1)[1])
        assert frame["mode"] == "preservation" and frame["body"] == body
        original = "".join(section["text"] for section in frame["original_sections"])
        assert "17 severe defects" in original and "three previously unknown defects" in original
        assert frame["original_metadata"]["edition"] == "Enterprise"
        assert frame["context"]["canonical_binding"]["blob"] == "actual-blob"
        assert frame["context"]["source_binding"] == "actual-current-source-generation"
        assert "scope" in system.lower() and "unknown" in system.lower()
        assert len(system) + len(user) <= 12000
        frames.append(frame)
        return response(frame, detail)

    client = model(cfg, handler)
    target = change(body)
    result = await audit(client, target)
    assert not result["complete"] and result["failures"]
    assert frames and client.usage.model_calls >= 1
    assert result["unsupported"] == []
    assert result["pending"]
    baseline = target.check_progress["existing_preservation"]
    assert baseline["original_body"] == OLD
    assert baseline["original_meta"] == META
    assert baseline["canonical_binding"] == {
        "commit": "original-git",
        "blob": "actual-blob",
    }
    assert baseline["repair_records"] == {}


@pytest.mark.asyncio
async def test_source_supported_paraphrase_and_explicit_operator_correction_are_allowed(
    cfg,
):
    body = "# Device\n\n## Metrics\nIn laboratory tests, Device identified seventeen severe defects.\nDevice found three previously unknown defects.\n"
    source = fact("Under laboratory conditions, Device found 17 severe defects.")
    correction = {
        "page": "wiki/device.md",
        "claim": "Device found three previously unknown defects.",
        "instruction": "Retain this fact; correct only wording of the first metric.",
    }

    def handler(request):
        frame = json.loads(json.loads(request.content)["messages"][1]["content"].split("\n", 1)[1])
        assert frame["body"] == body and frame["corrections"] == [correction]
        assert any(frame["source_excerpts"][record["quote_ref"]] == source.quote for record in frame["evidence"])
        assert frame["metadata"]["edition"] == "Enterprise"
        return response(frame)

    target = change(body)
    result = await audit(model(cfg, handler), target, records=[source], corrections=[correction])
    assert result["complete"] and not result["failures"]
    assert existing_repair_records(target) == [source]
    assert not any(record.source_id.startswith("canonical:") for record in result["repair_records"])


@pytest.mark.asyncio
async def test_changed_metadata_scope_requires_review_despite_identical_body(cfg):
    calls = []

    def handler(request):
        frame = json.loads(json.loads(request.content)["messages"][1]["content"].split("\n", 1)[1])
        assert frame["original_metadata"]["edition"] == "Enterprise"
        assert frame["metadata"]["edition"] == "Community"
        calls.append(frame)
        return response(frame, "Existing Enterprise facts cannot become Community facts.")

    target = change(OLD)
    target.draft_meta["edition"] = "Community"
    result = await audit(model(cfg, handler), target)
    assert calls and result["failures"] and not result["complete"]


@pytest.mark.asyncio
async def test_legacy_positive_is_archived_and_cannot_bypass_new_paired_review(cfg):
    old = {
        "version": 1,
        "complete": True,
        "binding": "historical",
        "original_audit": {"claims": {"old": "fact"}},
    }
    target = change("# Device\n\n## Metrics\nOnly a generic evaluation summary.\n")
    target.check_progress["existing_preservation"] = copy.deepcopy(old)
    calls = []

    def handler(request):
        frame = json.loads(json.loads(request.content)["messages"][1]["content"].split("\n", 1)[1])
        calls.append(frame)
        return response(frame, "Old metrics are missing.")

    client = model(cfg, handler)
    first = await audit(client, target)
    second = await audit(client, target)
    assert first["failures"] and second["failures"]
    assert target.check_progress["existing_preservation_legacy_history"] == [old]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_actual_jobstore_restart_reuses_completed_paired_review(cfg, store):
    class PowerLoss(BaseException):
        pass

    state = store.create(JobState("", "paired-preservation", "Device metrics"))[0]
    target = change(
        "# Device\n\n## Metrics\nIn laboratory tests, Device identified seventeen severe defects.\nDevice found three previously unknown defects.\n"
    )
    state.changes = [target]
    calls = []

    def handler(request):
        frame = json.loads(json.loads(request.content)["messages"][1]["content"].split("\n", 1)[1])
        calls.append(frame)
        return response(frame)

    def checkpoint(progress):
        store.save(state)
        proof = progress.get("existing_preservation", {})
        if proof.get("results"):
            raise PowerLoss()

    client = model(cfg, handler, state.usage)
    with pytest.raises(PowerLoss):
        await audit(
            client,
            target,
            records=[fact("Under laboratory conditions, Device found 17 severe defects.")],
            progress=checkpoint,
        )
    saved = store.load(state.job_id)
    assert saved.usage.model_calls == 1

    def never(request):
        raise AssertionError("Completed exact paired task must survive restart")

    result = await audit(
        model(cfg, never, saved.usage),
        saved.changes[0],
        records=[fact("Under laboratory conditions, Device found 17 severe defects.")],
        progress=lambda _: store.save(saved),
    )
    assert result["complete"] and saved.usage.model_calls == 1 and len(calls) == 1


@pytest.mark.asyncio
async def test_changed_source_binding_revokes_current_editorial_positive(cfg):
    calls = []

    def handler(request):
        frame = json.loads(json.loads(request.content)["messages"][1]["content"].split("\n", 1)[1])
        calls.append(frame)
        return response(frame)

    target = change(
        "# Device\n\n## Metrics\nIn laboratory tests, Device identified seventeen severe defects.\nDevice found three previously unknown defects.\n"
    )
    client = model(cfg, handler)
    await audit(client, target)
    await audit(client, target)
    await audit_existing_preservation(
        client,
        "Device",
        target,
        [],
        [],
        original_meta=META,
        original_body=OLD,
        canonical_binding={"commit": "original-git", "blob": "actual-blob"},
        char_budget=12000,
        source_binding="NEW-current-source-generation",
        source_policy={"exclude": ["unrelated-provider"]},
    )
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", [False, True])
async def test_actual_source_correction_or_binding_operator_can_replace_false_old_fact(cfg, operator):
    original = "# Device\n\n## Output\nDevice natively produces images.\n"
    body = "# Device\n\n## Output\nDevice outputs text; an attached image tool produces images.\n"
    source = fact("Device outputs text; an attached image tool produces images.")
    correction = {
        "page": "wiki/device.md",
        "claim": "Device natively produces images.",
        "instruction": "Incorrect: the attached tool produces images; retain that distinction.",
    }

    def handler(request):
        frame = json.loads(json.loads(request.content)["messages"][1]["content"].split("\n", 1)[1])
        assert frame["body"] == body
        assert any("natively produces images" in section["text"] for section in frame["original_sections"])
        if operator:
            assert frame["corrections"] == [correction] and not frame["evidence"]
        else:
            assert frame["corrections"] == []
            assert any(
                frame["source_excerpts"][record["quote_ref"]] == source.quote
                and record["applicability"]["edition"] == "Enterprise"
                for record in frame["evidence"]
            )
        return response(frame)

    client = model(cfg, handler)
    result = await audit(
        client,
        change(body),
        old=original,
        records=[] if operator else [source],
        corrections=[correction] if operator else [],
    )
    assert result["complete"] and result["failures"] == [] and client.usage.model_calls == 1


@pytest.mark.asyncio
async def test_complete_candidate_over_budget_never_clips_or_dispatches(cfg):
    from doclayer.curator.model import ModelError

    def never(request):
        raise AssertionError("Oversized complete candidate must fail before HTTP")

    target = change("# Device\n\n## Metrics\n" + "New unverified material. " * 800)
    client = model(cfg, never)
    with pytest.raises(ModelError, match="bounded"):
        await audit(client, target)
    assert client.usage.model_calls == 0
    assert not target.check_progress["existing_preservation"].get("complete")


@pytest.mark.asyncio
async def test_nested_governing_metadata_change_is_not_literal_retention(cfg):
    old_meta = {**META, "applicability": {"platform": "laboratory firmware"}}
    target = change(OLD)
    target.draft_meta["applicability"] = {"platform": "production firmware"}

    def handler(request):
        frame = json.loads(json.loads(request.content)["messages"][1]["content"].split("\n", 1)[1])
        assert frame["original_metadata"]["applicability"]["platform"] == "laboratory firmware"
        assert frame["metadata"]["applicability"]["platform"] == "production firmware"
        return response(frame, "Changed platform cannot inherit laboratory metrics.")

    client = model(cfg, handler)
    result = await audit_existing_preservation(
        client,
        "Device",
        target,
        [],
        [],
        original_meta=old_meta,
        original_body=OLD,
        canonical_binding={"commit": "old", "blob": "old"},
        char_budget=12000,
    )
    assert not result["complete"] and client.usage.model_calls == 1


@pytest.mark.asyncio
async def test_changed_canonical_snapshot_and_corrections_revoke_cached_result(cfg):
    frames = []

    def handler(request):
        frame = json.loads(json.loads(request.content)["messages"][1]["content"].split("\n", 1)[1])
        frames.append(frame)
        return response(frame)

    target = change(
        "# Device\n\n## Metrics\nIn laboratory tests, Device identified seventeen severe defects.\nDevice found three previously unknown defects.\n"
    )
    client = model(cfg, handler)
    for revision, correction in [
        ("a", []),
        ("a", []),
        ("b", []),
        ("b", [{"instruction": "Keep original laboratory scope."}]),
    ]:
        await audit_existing_preservation(
            client,
            "Device",
            target,
            [],
            correction,
            original_meta=META,
            original_body=OLD,
            canonical_binding={"commit": revision, "blob": revision},
            char_budget=12000,
        )
    assert client.usage.model_calls == 3
    assert [frame["context"]["canonical_binding"]["commit"] for frame in frames] == [
        "a",
        "b",
        "b",
    ]
    assert frames[-1]["corrections"] == [{"instruction": "Keep original laboratory scope."}]


@pytest.mark.asyncio
async def test_omitted_old_fact_restores_from_canonical_baseline_without_new_evidence(
    cfg,
):
    import re

    from doclayer.curator import editing

    body = "# Device\n\n## Metrics\nDevice completed its evaluation.\n"
    target = change(body)
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        user = payload["messages"][1]["content"]
        calls.append(user)
        if user.startswith("ARTICLE EDITORIAL REVIEW\n"):
            frame = json.loads(user.split("\n", 1)[1])
            # Review is independent of repair; initial loss is a blocker.
            return response(frame, "Original laboratory metric was omitted.")
        assert "Canonical retention context:" in user
        assert "Under laboratory conditions, Device found 17 severe defects." in user
        assert "Device found three previously unknown defects." in user
        assert "actual-blob" in user
        assert "canonical retention baseline, not new factual source authority" in user
        assert "Claim IDs to integrate or prove already present: []" in user
        assert len(payload["messages"][0]["content"]) + len(user) <= 12000
        output = {
            "section_id": re.search(r"Section ID: (.+)", user)[1],
            "edits": [],
            "append": (
                "Under laboratory conditions, Device found 17 severe defects.\n"
                "Device found three previously unknown defects."
            ),
            "handled_claim_ids": [],
            "claim_dispositions": [],
            "notes": [],
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps(output)},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    client = model(cfg, handler)
    result = await audit(client, target)
    assert not result["complete"] and result["repair_records"] == []
    repaired = await editing.edit_page(
        client,
        "Device",
        target,
        [],
        body,
        [],
        char_budget=12000,
        today="2026-10-03",
        repair=True,
    )
    assert "Device completed its evaluation." in repaired["body"]
    assert "Under laboratory conditions, Device found 17 severe defects." in repaired["body"]
    assert "Device found three previously unknown defects." in repaired["body"]
    assert repaired["draft_progress"]["handled_claim_ids"] == []
    assert client.usage.model_calls == 2 and len(calls) == 2
    assert "claims" not in target.check_progress
