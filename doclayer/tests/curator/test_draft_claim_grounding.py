"""Complete article review checks substantive text without an enumerated claim map."""

import copy
import json

import pytest

from doclayer.curator import curate, editorial
from doclayer.curator.model import ModelError
from doclayer.curator.state import Evidence, PageChange


def record(i):
    text = f"Device qualified fact {i}."
    return Evidence(
        str(i),
        "manual",
        "#Manual",
        "quote",
        text,
        url="https://manual.test",
        quote=text,
        support_status="supported",
    )


class ArticleModel:
    def __init__(self, fail_after=None):
        self.calls = 0
        self.frames = []
        self.fail_after = fail_after

    async def chat_json(self, system, user):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise ModelError("inference outage", transient=True)
        assert len(system) + len(user) <= 12000
        assert user.startswith("ARTICLE EDITORIAL REVIEW\n")
        frame = json.loads(user.split("\n", 1)[1])
        self.frames.append(frame)
        body = frame["body"]
        failures = []
        known_bad = (
            "Unsupported provider-specific tail assertion.",
            "New unsupported statement.",
            "Unsupported old fact.",
            "Device natively outputs images",
            "Device natively generates images",
            "Device outputs images",
            "# unsupported condition",
            "value = 123",
            "| rate | 99 |",
        )
        for bad in known_bad:
            if frame["mode"] == "grounding" and bad in body:
                failures.append(
                    {
                        "severity": "blocker",
                        "check": "article-grounding",
                        "detail": "Unsupported complete assertion: " + bad,
                        "section_id": next(
                            section["id"]
                            for section in frame["sections"]
                            if bad in body[section["start"] : section["end"]]
                        ),
                        "anchor": bad,
                    }
                )
        if frame["mode"] == "required":
            for row in frame["evidence"]:
                if row["id"] in frame["required_claim_ids"] and row["text"] not in body:
                    failures.append(
                        {
                            "severity": "blocker",
                            "check": "required-contribution",
                            "detail": "Assigned qualified fact missing: " + row["text"],
                            "section_id": frame["sections"][0]["id"],
                            "anchor": None,
                            "claim_id": row["id"],
                        }
                    )
        # Operator correction authorizes its actual statement only.
        if (
            frame["mode"] == "grounding"
            and frame["corrections"]
            and "Device qualified fact 1." in body
            and not frame["evidence"]
        ):
            failures.append(
                {
                    "severity": "blocker",
                    "check": "article-grounding",
                    "detail": "Operator correction does not authorize Device qualified fact 1.",
                    "section_id": frame["target_section_ids"][0],
                    "anchor": "Device qualified fact 1.",
                }
            )
        return {"complete": True, "failures": failures, "conflicts": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", [False, True])
async def test_80_relevant_source_records_overflow_grounding_without_partial_dispatch(
    tail,
):
    records = [record(i) for i in range(80)]
    body = "# Device\n" + "\n".join(r.text for r in records)
    if tail:
        body += "\nUnsupported provider-specific tail assertion."
    change = PageChange(
        "g",
        "wiki/device.md",
        "update",
        draft_body=body,
        claim_ids=[curate.evidence_id(r) for r in records],
    )
    model = ArticleModel()
    # All 80 records overlap this single section. A prefix cannot ground the
    # complete article, including its tail. Uncited split rule: the new,
    # uncited sentences are not grounded against a partial set at all; each,
    # the tail included, is reported as a new fact without a citation.
    result = await curate.check_draft(model, "Device", change, records, [], char_budget=12000)
    assert not [frame for frame in model.frames if frame["mode"] == "grounding"]
    uncited = [f["detail"] for f in result["failures"] if f["detail"].startswith("New fact without a citation")]
    assert len(uncited) == 80 + tail
    assert any("tail assertion" in detail for detail in uncited) == tail
    assert change.claim_ids == [curate.evidence_id(r) for r in records]


@pytest.mark.asyncio
async def test_article_restart_retains_actual_body_evidence_bound_completed_tasks(
    store,
):
    from doclayer.curator.state import JobState

    records = [record(i) for i in range(80)]
    target = PageChange(
        "g",
        "wiki/device.md",
        "update",
        draft_body="# Device\n" + "\n".join(r.text for r in records),
        claim_ids=[curate.evidence_id(r) for r in records],
    )
    state = store.create(JobState("", "", "Device", changes=[target]))[0]

    def retain(value):
        target.check_progress = value
        store.save(state)

    with pytest.raises(ModelError, match="outage"):
        await editorial.review(
            ArticleModel(fail_after=1),
            "Device",
            target,
            records,
            [],
            char_budget=12000,
            mode="required",
            progress=retain,
        )
    old = store.load(state.job_id).changes[0]
    completed = copy.deepcopy(old.check_progress["article_review"]["results"])
    assert completed
    model = ArticleModel()
    result = await editorial.review(model, "Device", old, records, [], char_budget=12000, mode="required")
    assert not result["failures"]
    assert completed.items() <= old.check_progress["article_review"]["results"].items()
    assert len(model.frames) < len(old.check_progress["article_review"]["results"])
    unchanged = ArticleModel()
    await editorial.review(unchanged, "Device", old, records, [], char_budget=12000, mode="required")
    assert unchanged.calls == 0
    old.draft_body = old.draft_body.replace(records[-1].text, "")
    changed = ArticleModel()
    result = await editorial.review(changed, "Device", old, records, [], char_budget=12000, mode="required")
    assert changed.calls and any(f.get("claim_id") == target.claim_ids[-1] for f in result["failures"])
    records[0].quote = "Changed actual source content."
    fresh = ArticleModel()
    await editorial.review(fresh, "Device", old, records, [], char_budget=12000, mode="required")
    assert fresh.calls


@pytest.mark.asyncio
async def test_old_empty_enumeration_shape_cannot_skip_substantive_article():
    class Empty:
        calls = 0

        async def chat_json(self, system, user):
            self.calls += 1
            assert user.startswith("ARTICLE EDITORIAL REVIEW")
            return {"claims": []}

    model = Empty()
    change = PageChange("g", "wiki/device.md", "update", draft_body="# Device\nUnsupported old fact.\n")
    with pytest.raises(ModelError, match="article review"):
        await curate.check_draft(model, "Device", change, [], [], char_budget=6000)
    assert model.calls > 0 and not change.check_progress["article_review"]["complete"]


@pytest.mark.asyncio
async def test_fenced_comments_tables_and_assertive_headings_reach_whole_review():
    body = "# Device\n## Options\nDevice qualified fact 0.\n## Device natively outputs images\n```python\n# unsupported condition\nvalue = 123\n```\n| Key | Value |\n| --- | --- |\n| rate | 99 |\n"
    target = PageChange("g", "wiki/device.md", "update", draft_body=body)
    model = ArticleModel()
    result = await curate.check_draft(model, "Device", target, [record(0)], [], char_budget=12000)
    assert model.frames[0]["body"] == body
    for assertion in (
        "natively outputs images",
        "unsupported condition",
        "value = 123",
        "rate | 99",
    ):
        assert any(assertion in f["detail"] for f in result["failures"])
    assert not any("Options" in f["detail"] for f in result["failures"])


@pytest.mark.asyncio
async def test_missing_source_support_is_unknown_and_recovers_when_actual_source_returns():
    item = Evidence(
        "s",
        "manual",
        "Page 7",
        "quote",
        "Device maximum is 90.",
        quote="Device maximum is 90.",
    )

    class Client:
        calls = 0

        async def chat_json(self, system, user):
            self.calls += 1
            return {
                "verdicts": [
                    {
                        "id": curate.evidence_id(item),
                        "supported": True,
                        "reason": "Actual source supports the claim.",
                    }
                ]
            }

    client = Client()
    missing = await curate.verify_source_claims(client, [item], {}, char_budget=6000)
    identity = curate.evidence_id(item)
    assert missing[identity]["status"] == "unknown" and not missing[identity]["supported"] and client.calls == 0
    again = await curate.verify_source_claims(client, [item], {}, char_budget=6000, known=missing)
    assert again == missing and client.calls == 0
    restored = await curate.verify_source_claims(
        client,
        [item],
        {"s": "# Card\n## Page 7\nDevice maximum is 90.\n"},
        char_budget=6000,
        known=missing,
    )
    assert restored[identity]["status"] == "supported" and client.calls == 1
    wrong = await curate.verify_source_claims(
        client,
        [item],
        {"s": "# Card\n## Page 9\nOther model maximum is 90.\n"},
        char_budget=6000,
        known=restored,
    )
    assert wrong[identity]["status"] == "unknown" and client.calls == 1


@pytest.mark.asyncio
async def test_selection_unknown_support_stops_without_endless_calls_and_keeps_claim(cfg, store):
    from doclayer.curator.pipeline import Pipeline
    from doclayer.curator.state import JobState, SourceRecord

    state = store.create(JobState(job_id="", idempotency_key="", intent="Device"))[0]
    item = Evidence(
        "s",
        "manual",
        "Page 7",
        "quote",
        "Device maximum is 90.",
        quote="Device maximum is 90.",
    )
    state.sources = [SourceRecord(id="s", kind="url", label="manual", url="https://manual.test")]
    store.append_evidence(state.job_id, [item.to_dict()])
    store.save(state)

    class Client:
        async def chat_json(self, *args):
            pytest.fail("missing actual source must not call model")

    change = PageChange("g", "wiki/device.md", "create", claim_ids=[curate.evidence_id(item)])
    runner = Pipeline(cfg, store)
    with pytest.raises(ModelError, match="selected source support unavailable"):
        await runner._verify_selected(state, change, Client())
    assert state.coverage == "incomplete" and runner._evidence(state)[0].support_status == "unknown"
    assert any("source-support unknown" in gap for gap in state.coverage_gaps)


@pytest.mark.asyncio
async def test_relevant_actual_retained_record_beyond_80_is_supplied_not_false_rejected():
    records = [record(i) for i in range(80)] + [record(999)]
    target = PageChange(
        "g",
        "wiki/device.md",
        "update",
        draft_body="# Device\nDevice qualified fact 999.\n",
        claim_ids=[curate.evidence_id(records[-1])],
    )
    # This is required-contribution selection, not grounding against all 81
    # generic same-subject records (which cannot fit this section budget).
    model = ArticleModel()
    result = await editorial.review(model, "Device", target, records, [], char_budget=12000, mode="required")
    assert not result["failures"]
    assert any(r["id"] == target.claim_ids[0] for frame in model.frames for r in frame["evidence"])
    assert all("omitted_evidence_ids" in frame for frame in model.frames)


@pytest.mark.asyncio
async def test_assigned_unknown_source_remains_unresolved_without_editorial_dispatch():
    unknown = record(999)
    unknown.support_status = "unknown"
    target = PageChange(
        "g",
        "wiki/device.md",
        "update",
        draft_body="# Device\nDevice qualified fact 999.\n",
        claim_ids=[curate.evidence_id(unknown)],
    )
    model = ArticleModel()
    result = await curate.check_draft(model, "Device", target, [unknown], [], char_budget=12000)
    assert result["failures"] and model.calls == 0
    assert any(f.get("claim_id") == target.claim_ids[0] for f in result["failures"])
    assert not target.repair_passes and unknown.support_status == "unknown"
    unknown.support_status = "supported"
    result = await curate.check_draft(model, "Device", target, [unknown], [], char_budget=12000)
    assert not result["failures"] and model.calls == 1


@pytest.mark.asyncio
async def test_correction_only_support_does_not_excuse_unrelated_assertions():
    target = PageChange(
        "g",
        "wiki/device.md",
        "update",
        draft_body="# Device\nDevice qualified fact 0.\nDevice qualified fact 1.\n",
    )
    corrections = [{"instruction": "Device qualified fact 0."}]
    model = ArticleModel()
    result = await curate.check_draft(model, "Device", target, [], corrections, char_budget=12000)
    assert len(result["failures"]) == 1 and "fact 1." in result["failures"][0]["detail"]
    assert model.frames[0]["corrections"] == corrections
    assert [frame["mode"] for frame in model.frames] == ["grounding", "correction"]
    cached = ArticleModel()
    assert await curate.check_draft(cached, "Device", target, [], corrections, char_budget=12000) == result
    assert cached.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "# Device\n## Configuration\nDevice qualified fact 0.\n## Device natively generates images\n",
        "# Device outputs images\n## Configuration\nDevice qualified fact 0.\n",
    ],
)
async def test_assertive_heading_cannot_disappear_as_neutral_structure(body):
    target = PageChange("g", "wiki/device.md", "update", draft_body=body)
    model = ArticleModel()
    result = await curate.check_draft(model, "Device", target, [record(0)], [], char_budget=12000)
    assert len(result["failures"]) == 1 and "images" in result["failures"][0]["detail"]
    assert model.frames[0]["body"] == body


@pytest.mark.asyncio
async def test_changed_governing_metadata_revokes_bound_article_review():
    target = PageChange(
        "g",
        "wiki/device.md",
        "update",
        draft_body="# Device\nDevice qualified fact 0.\n",
        draft_meta={"edition": "Enterprise"},
    )
    model = ArticleModel()
    await curate.check_draft(model, "Device", target, [record(0)], [], char_budget=12000)
    binding = target.check_progress["article_review"]["binding"]
    target.draft_meta["edition"] = "Community"
    changed = ArticleModel()
    await curate.check_draft(changed, "Device", target, [record(0)], [], char_budget=12000)
    assert changed.calls and target.check_progress["article_review"]["binding"] != binding
    assert changed.frames[0]["metadata"]["edition"] == "Community"
