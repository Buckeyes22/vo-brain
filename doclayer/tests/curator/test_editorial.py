"""Article judgments remain coherent, source-grounded, located and restartable."""

import copy
import json

import httpx
import pytest

from doclayer.curator import curate, editorial
from doclayer.curator.model import BudgetExceeded, ModelClient, ModelError
from doclayer.curator.state import Evidence, JobState, PageChange, Usage


def source(
    text="Device often achieves better results using fewer tokens, lowering estimated cost per task.",
    identity="incoming",
):
    return Evidence(
        "s",
        "Device",
        "## Costs",
        "quote",
        text,
        claim_id=identity,
        quote=text,
        support_status="supported",
    )


def change(body, ids=None):
    return PageChange(
        "g",
        "wiki/device.md",
        "update",
        title="Device",
        draft_body=body,
        draft_meta={"title": "Device"},
        claim_ids=ids or [],
    )


def issue(frame, anchor, check="scope", claim_id=None):
    item = {
        "severity": "blocker",
        "check": check,
        "detail": "Restore the source qualification",
        "section_id": frame["sections"][-1]["id"],
        "anchor": anchor,
    }
    if claim_id:
        item["claim_id"] = claim_id
    return {"complete": True, "failures": [item], "conflicts": []}


class Client:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def chat_bounded_json(self, system, user, *, char_budget):
        assert len(system) + len(user) <= char_budget
        assert "NEW or CHANGED" in system and "faithful paraphrase" in system
        frame = json.loads(user.split("\n", 1)[1])
        self.calls.append(copy.deepcopy(frame))
        return self.reply(frame)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,blocked",
    [
        (
            "# Device\n\nDevice often lowers estimated costs by producing better results with fewer tokens.",
            False,
        ),
        ("# Device\n\nDevice always lowers costs.", True),
    ],
)
async def test_qualification_editorial_judgment_and_exact_location(body, blocked):
    client = Client(
        lambda frame: (
            issue(frame, "Device always lowers costs.")
            if blocked
            else {"complete": True, "failures": [], "conflicts": []}
        )
    )
    result = await curate.check_draft(client, "Device", change(body), [source()], [], char_budget=12000)
    assert bool(result["failures"]) is blocked and len(client.calls) == 1
    frame = client.calls[0]
    assert frame["source_excerpts"][frame["evidence"][0]["quote_ref"]] == source().quote
    assert not any(k in frame for k in ("claims", "verdicts", "catalogue"))


@pytest.mark.asyncio
async def test_unsupported_new_tail_targeted_without_reproving_retained_article():
    original = "# Device\n\n## Known\n\nUnrelated retained knowledge.\n\n## Costs\n\nKnown costs.\n"
    body = original + "Device natively creates images without tools.\n"
    client = Client(
        lambda frame: (
            issue(frame, "Device natively creates images without tools.", "evidence")
            if frame["mode"] == "grounding"
            else {"complete": True, "failures": [], "conflicts": []}
        )
    )
    target = change(body)
    result = await curate.check_draft(
        client,
        "Device",
        target,
        [source()],
        [],
        char_budget=12000,
        original_body=original,
        original_meta=target.draft_meta,
    )
    assert result["failures"][0]["check"] == "evidence"
    assert "Unrelated retained knowledge" not in client.calls[0]["body"]
    assert client.calls[0]["original_sections"][0]["text"].endswith("Known costs.\n")


@pytest.mark.asyncio
async def test_changed_parent_scope_cannot_exempt_unchanged_child():
    original = "# Device\n\nOnly firmware three.\n\n## Retention\n\nRecords remain seven days.\n"
    body = original.replace("Only firmware three.", "Every firmware.")
    client = Client(lambda frame: {"complete": True, "failures": [], "conflicts": []})
    target = change(body)
    await editorial.review(
        client,
        "Device",
        target,
        [source()],
        [],
        char_budget=12000,
        original_body=original,
        original_meta=target.draft_meta,
    )
    child = next(s for s in target.check_progress["article_review"]["sections"] if s["label"].endswith("Retention"))
    assert child["id"] not in client.calls[0]["retained_canonical_sections"]


@pytest.mark.asyncio
async def test_missing_incoming_is_not_handled_id_bookkeeping():
    client = Client(lambda frame: issue(frame, None, "required-contribution", "incoming"))
    target = change("# Device\n\nNo cost discussion.\n", ["incoming"])
    result = await curate.check_draft(client, "Device", target, [source()], [], char_budget=12000)
    assert result["failures"][0]["claim_id"] == "incoming"
    assert client.calls[0]["required_claim_ids"] == ["incoming"]
    assert "required_coverage" not in target.check_progress


@pytest.mark.asyncio
async def test_unknown_required_support_stops_without_model():
    record = source()
    record.support_status = "unknown"
    client = Client(lambda _: pytest.fail("unknown support cannot be certified"))
    result = await editorial.review(
        client,
        "Device",
        change("# Device\n", ["incoming"]),
        [record],
        [],
        char_budget=12000,
    )
    assert result["failures"][0]["check"] == "required-evidence" and not client.calls


@pytest.mark.asyncio
async def test_complete_corrections_budget_stop_and_invalid_schema_retryable():
    client = Client(lambda _: {"complete": True, "failures": [], "conflicts": []})
    corrections = [{"claim": "x" * 12000, "instruction": "keep the full correction"}]
    with pytest.raises(ModelError, match="bounded"):
        await editorial.review(
            client,
            "Device",
            change("# Device\n"),
            [source()],
            corrections,
            char_budget=4500,
        )
    assert not client.calls
    bad = Client(lambda _: {"failures": [], "conflicts": []})
    with pytest.raises(ModelError) as exc:
        await editorial.review(bad, "Device", change("# Device\n"), [source()], [], char_budget=12000)
    assert exc.value.transient


@pytest.mark.asyncio
async def test_section_restart_preserves_actual_calls_and_completed_tasks(cfg, store):
    body = "".join(f"## Section {i}\n\n" + (f"Device fact {i}. " * 90) + "\n" for i in range(3))
    records = [source("Device fact " + str(i) + ". " + "Evidence " * 8, str(i)) for i in range(3)]
    saved = store.create(JobState("", "article", "Review all sections"))[0]
    saved.changes = [change(body)]
    frames = []

    def handler(request):
        payload = json.loads(request.content)
        system, user = [m["content"] for m in payload["messages"]]
        assert len(system) + len(user) <= 6000
        frame = json.loads(user.split("\n", 1)[1])
        assert {r["id"] for r in frame["evidence"]} == {r.claim_id for r in records}
        assert set(frame["source_excerpts"].values()) == {r.quote for r in records}
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

    async def run(state, cap):
        native = ModelClient(
            cfg,
            state.usage,
            limits={"max_model_calls": cap},
            transport=httpx.MockTransport(handler),
        )
        native._admitted_call = lambda: store.save(state)
        return await editorial.review(
            native,
            "Device",
            state.changes[0],
            records,
            [],
            char_budget=6000,
            progress=lambda _: store.save(state),
        )

    with pytest.raises(BudgetExceeded):
        await run(saved, 1)
    saved = store.load(saved.job_id)
    assert saved.usage.model_calls == 1
    old = copy.deepcopy(saved.changes[0].check_progress["article_review"]["results"])
    result = await run(saved, 10)  # explicit fixture quota; usage is not reset
    assert result["complete"] and not result["failures"]
    assert saved.usage.model_calls == len(frames)
    assert len(frames) > 1 and all(
        saved.changes[0].check_progress["article_review"]["results"][k] == v for k, v in old.items()
    )
    reviewed = {i for f in frames for i in f["target_section_ids"]}
    assert reviewed == {s["id"] for s in saved.changes[0].check_progress["article_review"]["sections"]}
    before = saved.usage.model_calls
    assert await run(saved, 10) == result and saved.usage.model_calls == before


@pytest.mark.asyncio
async def test_required_batches_each_see_complete_article_and_metadata_is_not_repeated():
    body = "# Device\n\n" + "Retained description. " * 105
    records = [source(f"Distinct source {i}. " + "Qualified source detail " * 30, f"incoming-{i}") for i in range(3)]
    target = change(body, [r.claim_id for r in records])
    target.draft_meta["source_lineage"] = ["https://source.invalid/" + "x" * 200 for _ in range(80)]
    client = Client(lambda frame: {"complete": True, "failures": [], "conflicts": []})
    result = await editorial.review(client, "Device", target, records, [], char_budget=7000, mode="required")
    assert result["complete"]
    batches = [frame for frame in client.calls if frame["mode"] == "required"]
    assert len(batches) > 1
    assert {i for f in batches for i in f["required_claim_ids"]} == set(target.claim_ids)
    assert all(f["body"] == body and "source_lineage" not in f["metadata"] for f in batches)
    assert not any("text" in section for f in client.calls for section in f["sections"])


@pytest.mark.asyncio
@pytest.mark.parametrize("output_target,thinking_budget", [(4096, 2048), (512, 256)])
async def test_null_unassigned_claim_id_preserves_native_semantic_blocker(cfg, output_target, thinking_budget):
    cfg.inference.output_token_target = output_target
    target = change("# Device\n\nUnsupported new assertion.\n")

    def handler(request):
        payload = json.loads(request.content)
        assert payload["chat_template_kwargs"]["enable_thinking"] is True
        assert payload["thinking_token_budget"] == thinking_budget
        assert payload["response_format"] == {"type": "json_object"}
        frame = json.loads(payload["messages"][1]["content"].split("\n", 1)[1])
        result = issue(frame, "Unsupported new assertion.", "unsupported")
        result["failures"][0]["claim_id"] = None
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

    client = ModelClient(cfg, Usage(), transport=httpx.MockTransport(handler))
    result = await editorial.review(client, "Device", target, [source()], [], char_budget=12000)
    assert result["failures"][0]["check"] == "unsupported"
    assert "claim_id" not in result["failures"][0]


@pytest.mark.asyncio
async def test_over_twelve_assigned_records_late_omission_is_reviewed_in_fullpage_batches():
    body = "# Device\n\n" + "Canonical background. " * 65
    records = [
        source(
            f"Distinct incoming fact {i}. " + "Complete qualification. " * 8,
            f"incoming-{i}",
        )
        for i in range(15)
    ]
    target = change(body, [r.claim_id for r in records])

    def reply(frame):
        if frame["mode"] == "required" and "incoming-14" in frame["required_claim_ids"]:
            return issue(frame, None, "required-contribution", "incoming-14")
        return {"complete": True, "failures": [], "conflicts": []}

    client = Client(reply)
    result = await editorial.review(client, "Device", target, records, [], char_budget=6000, mode="required")
    batches = [f for f in client.calls if f["mode"] == "required"]
    assert len(batches) > 1
    assert {v for f in batches for v in f["required_claim_ids"]} == set(target.claim_ids)
    assert all(f["body"] == body for f in batches)
    assert any(f.get("claim_id") == "incoming-14" for f in result["failures"])


@pytest.mark.asyncio
async def test_changed_section_wire_offsets_index_shown_body_but_checkpoint_is_fullpage():
    original = "# Device\n\nCanonical description.\n## New facts\n\nOld detail.\n"
    body = original.replace("Old detail.", "New supported detail.")
    target = change(body)

    def reply(frame):
        assert "Canonical description." not in frame["body"]
        for section in frame["sections"]:
            selected = frame["body"][section["start"] : section["end"]]
            assert selected and "New supported detail." in selected
        return {"complete": True, "failures": [], "conflicts": []}

    result = await editorial.review(
        Client(reply),
        "Device",
        target,
        [source("New supported detail.")],
        [],
        char_budget=12000,
        original_body=original,
        original_meta=target.draft_meta,
    )
    assert result["complete"]
    section = target.check_progress["article_review"]["sections"][-1]
    assert "New supported detail." in body[section["start"] : section["end"]]
    assert section["start"] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_parent", ["## General use", ""])
async def test_split_preservation_keeps_removed_original_parent_scope(candidate_parent):
    parent = "## Restricted mode\nOnly available during calibration.\n"
    leaves = "".join(f"### Detail {i}\n" + f"Device original detail {i}. " * 18 + "\n" for i in range(4))
    original = "# Device\n" + parent + leaves
    candidate = "# Device\n" + candidate_parent + "\n" + leaves
    target = change(candidate)
    seen = []

    def reply(frame):
        assert frame["mode"] == "preservation"
        assert frame["body"] == candidate
        seen.append(frame)
        for old in frame["original_sections"]:
            if old["label"].endswith(tuple(f"Detail {i}" for i in range(4))):
                context = frame["original_sections"] + frame["original_governing_context"]
                assert sum(row["text"] == parent for row in context) == 1
                assert "Only available during calibration." in "".join(row["text"] for row in context)
        return {"complete": True, "failures": [], "conflicts": []}

    await editorial.review(
        Client(reply),
        "Device",
        target,
        [],
        [],
        char_budget=6500,
        mode="preservation",
        original_body=original,
        original_meta=target.draft_meta,
    )
    assert len(seen) > 1
    target_ids = {row["id"] for frame in seen for row in frame["original_sections"]}
    old_sections = editorial._sections(original)
    required_ids = {
        row["id"]
        for row in old_sections
        if row["label"].endswith(tuple(f"Detail {i}" for i in range(4))) or row["text"] == parent
    }
    assert required_ids <= target_ids <= {row["id"] for row in old_sections}
    assert any(frame["original_governing_context"] for frame in seen)


@pytest.mark.asyncio
async def test_native_missing_assigned_warning_requires_repair_while_precision_stays_warning(
    cfg,
):
    target = change("# Device\n\nNo cost procedure is described.\n", ["incoming"])
    usage = Usage()

    def handler(request):
        payload = json.loads(request.content)
        assert "Missing material MUST" in payload["messages"][0]["content"]
        frame = json.loads(payload["messages"][1]["content"].split("\n", 1)[1])
        result = issue(frame, None, "required-contribution", "incoming")
        result["failures"][0]["severity"] = "warning"
        precision = {
            **result["failures"][0],
            "check": "precision",
            "detail": "Optional nonessential precision",
        }
        result["failures"].append(precision)
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

    client = ModelClient(cfg, usage, transport=httpx.MockTransport(handler))
    result = await curate.check_draft(client, "Device", target, [source()], [], char_budget=12000)
    assert usage.model_calls == 1
    assert result["failures"][0]["severity"] == "blocker"
    assert result["failures"][0]["claim_id"] == "incoming"
    assert result["failures"][1]["severity"] == "warning"
    assert (
        next(iter(target.check_progress["article_review"]["results"].values()))["failures"][0]["severity"] == "blocker"
    )
    assert await curate.check_draft(client, "Device", target, [source()], [], char_budget=12000) == result
    assert usage.model_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_id", [None, "unassigned", [], {}, 5])
async def test_required_contribution_must_name_valid_assigned_id(claim_id):
    target = change("# Device\n\nNo cost discussion.\n", ["incoming"])

    def reply(frame):
        result = issue(frame, None, "required-contribution")
        result["failures"][0]["claim_id"] = claim_id
        result["failures"][0]["severity"] = "warning"
        return result

    with pytest.raises(ModelError) as exc:
        await curate.check_draft(Client(reply), "Device", target, [source()], [], char_budget=12000)
    assert exc.value.transient and exc.value.diagnostic["operation"] == "article-review"
    assert target.check_progress["article_review"]["results"] == {}


@pytest.mark.asyncio
async def test_changed_required_severity_contract_invalidates_old_pass_cache(
    monkeypatch,
):
    target = change("# Device\n\nNo cost discussion.\n", ["incoming"])
    current = editorial.SYSTEM
    client = Client(lambda frame: {"complete": True, "failures": [], "conflicts": []})
    with monkeypatch.context() as patch:
        patch.setattr(
            editorial,
            "SYSTEM",
            current.replace("Missing material MUST", "Legacy optional contribution"),
        )
        await curate.check_draft(client, "Device", target, [source()], [], char_budget=12000)
    old_binding = target.check_progress["article_review"]["binding"]
    client.reply = lambda frame: issue(frame, None, "required-contribution", "incoming")
    result = await curate.check_draft(client, "Device", target, [source()], [], char_budget=12000)
    assert len(client.calls) == 2
    assert old_binding != target.check_progress["article_review"]["binding"]
    assert result["failures"][0]["severity"] == "blocker"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["required", "preservation"])
async def test_partial_batch_mode_keeps_unrelated_candidate_facts_outside_grounding(
    mode,
):
    original = "# Device\n\nCanonical retained knowledge.\n"
    unrelated = "A separately grounded API accepts stable instructions."
    body = original + "\n" + unrelated + "\nDevice often improves results using fewer tokens.\n"
    record = source("Device often improves results using fewer tokens.")
    target = change(body, ["incoming"])

    class ModeClient:
        async def chat_json(self, system, user):
            frame = json.loads(user.split("\n", 1)[1])
            assert "Apply ONLY the task's mode" in system
            assert (
                "grounding ONLY" in system
                and "asserted fact lacking supplied supporting evidence is a blocker" in system
            )
            assert frame["mode"] == mode and frame["body"] == body
            assert unrelated not in frame["evidence"][0]["text"]
            if mode == "required":
                assert "not grounding of unrelated candidate facts" in system
                assert frame["required_claim_ids"] == ["incoming"]
                assert record.text in frame["body"]
            else:
                assert "Do NOT ground new candidate assertions" in system
                assert frame["original_sections"] and original in "".join(
                    row["text"] for row in frame["original_sections"]
                )
            return {"complete": True, "failures": [], "conflicts": []}

    result = await editorial.review(
        ModeClient(),
        "Device",
        target,
        [record],
        [],
        char_budget=7000,
        mode=mode,
        original_body=original,
        original_meta={"title": "Prior Device"},
    )
    assert result == {"complete": True, "failures": [], "conflicts": []}


@pytest.mark.asyncio
async def test_mode_isolation_still_checks_required_omission_and_original_loss():
    original = "# Device\n\nExisting calibration procedure.\n"
    target = change("# Device\n\nUnrelated separately grounded addition.\n", ["incoming"])

    class ModeClient:
        async def chat_json(self, system, user):
            frame = json.loads(user.split("\n", 1)[1])
            assert "Apply ONLY the task's mode" in system
            if frame["mode"] == "required":
                assert frame["required_claim_ids"] == ["incoming"]
                return issue(frame, None, "required-contribution", "incoming")
            assert frame["mode"] == "preservation"
            assert "Existing calibration procedure." in "".join(row["text"] for row in frame["original_sections"])
            return issue(frame, None, "preservation")

    for mode in ("required", "preservation"):
        result = await editorial.review(
            ModeClient(),
            "Device",
            target,
            [source()],
            [],
            char_budget=7000,
            mode=mode,
            original_body=original,
            original_meta=target.draft_meta,
            namespace=mode,
        )
        assert result["failures"][0]["severity"] == "blocker"
        assert result["failures"][0]["check"] == ("required-contribution" if mode == "required" else "preservation")


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_condition", [False, True])
async def test_grounding_unchanged_fact_with_added_neighbor_keeps_original_applicability(
    changed_condition,
):
    old_parent = "## Conditions\nOnly laboratory installations.\n"
    old_fact = "### Existing fact\nDevice records traces.\n"
    original = "# Device\n" + old_parent + old_fact
    new_parent = (
        old_parent.replace("laboratory", "production")
        if changed_condition
        else old_parent + "New unrelated tool detail.\n"
    )
    body = "# Device\n" + new_parent + old_fact
    target = change(body)
    frames = []

    class ContextClient:
        async def chat_json(self, system, user):
            frame = json.loads(user.split("\n", 1)[1])
            frames.append(frame)
            assert len(system) + len(user) <= 6000
            assert frame["mode"] == "grounding"
            assert "unrelated additions alone are not scope changes" in system
            assert "absent original context requires grounding" in system
            old_rows = frame["original_sections"] + frame["original_governing_context"]
            assert any(row["text"] == old_parent for row in old_rows)
            assert any(row["text"] == old_fact for row in frame["original_sections"])
            current = frame["body"] + "".join(row["text"] for row in frame["governing_context"])
            assert new_parent in current and old_fact in current
            assert "Device records traces." not in frame["evidence"][0]["text"]
            if changed_condition:
                return issue(frame, "Device records traces.", "applicability")
            return {"complete": True, "failures": [], "conflicts": []}

    result = await editorial.review(
        ContextClient(),
        "Device",
        target,
        [source("New unrelated tool detail.")],
        [],
        char_budget=6000,
        original_body=original,
        original_meta=target.draft_meta,
    )
    assert bool(result["failures"]) == changed_condition
    # The conservative shortcut still withholds exemption for any changed
    # ancestor bytes; the model sees old/current context instead.
    child = next(
        row for row in target.check_progress["article_review"]["sections"] if row["label"].endswith("Existing fact")
    )
    assert frames and all(child["id"] not in frame["retained_canonical_sections"] for frame in frames)
    assert any(frame["original_governing_context"] for frame in frames)


@pytest.mark.asyncio
@pytest.mark.parametrize("malformation", ["shape", "unknown-section"])
async def test_native_schema_repair_retains_complete_task_and_blocker(cfg, store, malformation):
    state = store.create(JobState("", "format-review", "Device"))[0]
    target = change("# Device\n\nDevice always lowers costs.\n")
    state.changes = [target]
    frames = []

    def handler(request):
        payload = json.loads(request.content)
        system, user = [item["content"] for item in payload["messages"]]
        assert system == editorial.GROUNDING_SYSTEM and len(system) + len(user) <= 6000
        frame = json.loads(user.split("\n", 1)[1])
        frames.append(frame)
        result = issue(frame, "Device always lowers costs.", "scope")
        if len(frames) == 1:
            if malformation == "shape":
                result.pop("complete")
            else:
                result["failures"][0]["section_id"] = "unknown-section"
        else:
            assert "preserve findings" in frame["format_repair"]
            assert {key: value for key, value in frame.items() if key != "format_repair"} == frames[0]
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

    client = ModelClient(cfg, state.usage, transport=httpx.MockTransport(handler))
    client._admitted_call = lambda: store.save(state)
    result = await editorial.review(
        client,
        "Device",
        target,
        [source()],
        [],
        char_budget=6000,
        progress=lambda _: store.save(state),
    )
    assert state.usage.model_calls == len(frames) == 2
    assert result["failures"][0]["severity"] == "blocker"
    restored = store.load(state.job_id)
    resumed = ModelClient(cfg, restored.usage, transport=httpx.MockTransport(handler))
    assert await editorial.review(resumed, "Device", restored.changes[0], [source()], [], char_budget=6000) == result
    assert restored.usage.model_calls == 2


@pytest.mark.asyncio
async def test_native_two_bad_schemas_park_without_cached_acceptance_or_restart_retry(cfg, store):
    state = store.create(JobState("", "invalid-review", "Device"))[0]
    target = change("# Device\n\nDevice always lowers costs.\n")
    state.changes = [target]
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps({"findings": []})},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    client = ModelClient(cfg, state.usage, transport=httpx.MockTransport(handler))
    client._admitted_call = lambda: store.save(state)
    with pytest.raises(ModelError) as exc:
        await editorial.review(
            client,
            "Device",
            target,
            [source()],
            [],
            char_budget=6000,
            progress=lambda _: store.save(state),
        )
    assert exc.value.transient and exc.value.diagnostic["reply_keys"] == ["findings"]
    assert state.usage.model_calls == len(requests) == 2
    assert target.check_progress["article_review"]["results"] == {}
    restored = store.load(state.job_id)
    with pytest.raises(ModelError, match="format repair exhausted"):
        await editorial.review(
            ModelClient(cfg, restored.usage, transport=httpx.MockTransport(handler)),
            "Device",
            restored.changes[0],
            [source()],
            [],
            char_budget=6000,
        )
    assert len(requests) == restored.usage.model_calls == 2


@pytest.mark.asyncio
async def test_valid_unsupported_blocker_is_not_format_retried(cfg):
    target = change("# Device\n\nDevice always lowers costs.\n")
    usage = Usage()

    def handler(request):
        frame = json.loads(json.loads(request.content)["messages"][1]["content"].split("\n", 1)[1])
        assert "format_repair" not in frame
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps(issue(frame, "Device always lowers costs.", "unsupported"))},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    result = await editorial.review(
        ModelClient(cfg, usage, transport=httpx.MockTransport(handler)),
        "Device",
        target,
        [source()],
        [],
        char_budget=6000,
    )
    assert usage.model_calls == 1 and result["failures"][0]["severity"] == "blocker"


@pytest.mark.asyncio
async def test_format_repair_budget_interruption_resumes_pending_call_without_initial_replay(cfg, store):
    state = store.create(JobState("", "format-budget", "Device"))[0]
    state.changes = [change("# Device\n\nDevice always lowers costs.\n")]
    frames = []

    def handler(request):
        payload = json.loads(request.content)
        system, user = [v["content"] for v in payload["messages"]]
        assert len(system) + len(user) <= 6000
        frame = json.loads(user.split("\n", 1)[1])
        frames.append(frame)
        result = issue(frame, "Device always lowers costs.", "unsupported")
        if len(frames) == 1:
            result.pop("complete")
            assert "format_repair" not in frame
        else:
            assert "format_repair" in frame
            assert {k: v for k, v in frame.items() if k != "format_repair"} == frames[0]
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

    async def run(current, cap):
        client = ModelClient(
            cfg,
            current.usage,
            limits={"max_model_calls": cap},
            transport=httpx.MockTransport(handler),
        )
        client._admitted_call = lambda: store.save(current)
        return await editorial.review(
            client,
            "Device",
            current.changes[0],
            [source()],
            [],
            char_budget=6000,
            progress=lambda _: store.save(current),
        )

    with pytest.raises(BudgetExceeded):
        await run(state, 1)
    restored = store.load(state.job_id)
    assert restored.usage.model_calls == 1 and len(frames) == 1
    progress = restored.changes[0].check_progress["article_review"]
    assert progress["results"] == {} and progress["format_repairs"]
    result = await run(restored, 2)  # explicit fixture allowance; actual usage retained
    assert result["failures"][0]["severity"] == "blocker"
    assert restored.usage.model_calls == len(frames) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [6000, 7000])
async def test_grounding_groups_keep_complete_relevant_sources_and_real_unsupported_fact(
    budget,
):
    records = [
        source("Device audio and video are not supported.", "modalities"),
        source("Device cache writes cost 1.25 times input rates.", "pricing"),
        source("Device has a Responses endpoint.", "endpoint"),
    ]
    body = "".join(
        f"## Device section {i}\n" + "Device unchanged contextual description. " * 25 + "\n" for i in range(5)
    )
    body += "## Device additions\nDevice audio and video are not supported. Device cache writes cost 1.25 times input rates. Device has an unsupported operational Batch route.\n"
    target = change(body)

    def reply(frame):
        assert frame["mode"] == "grounding"
        assert {r["id"] for r in frame["evidence"]} == {r.claim_id for r in records}
        assert set(frame["source_excerpts"].values()) == {r.quote for r in records}
        assert not frame["omitted_evidence_ids"]
        if "unsupported operational Batch route" in frame["body"]:
            return issue(frame, "Device has an unsupported operational Batch route.", "grounding")
        return {"complete": True, "failures": [], "conflicts": []}

    client = Client(reply)
    result = await editorial.review(client, "Device", target, records, [], char_budget=budget)
    assert len(client.calls) > 1
    assert [f["anchor"] for f in result["failures"]] == ["Device has an unsupported operational Batch route."]
    assert {s for f in client.calls for s in f["target_section_ids"]} == {
        s["id"] for s in target.check_progress["article_review"]["sections"]
    }


@pytest.mark.asyncio
async def test_grounding_complete_relevant_evidence_overflow_is_not_a_partial_negative():
    records = [source("Device qualification. " * 90, "large")]
    client = Client(lambda _: pytest.fail("incomplete relevant sources must not be dispatched"))
    # Uncited split rule: no partial dispatch; the new uncited sentence is reported
    # as a new fact without a citation instead of being judged against trimmed evidence.
    result = await editorial.review(
        client,
        "Device",
        change("# Device\nDevice fact.\n"),
        records,
        [],
        char_budget=4500,
    )
    assert not client.calls
    assert [f["detail"] for f in result["failures"]] == [
        "New fact without a citation: 'Device fact.'. Cite the source that states it, or remove it."
    ]


@pytest.mark.asyncio
async def test_grounding_chunks_do_not_receive_global_intent_but_required_sees_whole_page():
    intent = "Replace the misleading Vision tool sentence; keep the rest intact."
    record = source("Device uses a separate image tool.", "vision")
    original = "".join(f"## Detail {i}\n" + "Device contextual detail. " * 13 + "\n" for i in range(5))
    body = original.replace("contextual", "updated contextual") + "## Vision\nDevice uses a separate image tool.\n"
    target = change(body, ["vision"])

    def reply(frame):
        if frame["mode"] == "grounding":
            assert "topic" not in frame and not frame["required_claim_ids"]
        else:
            assert frame["mode"] == "required" and frame["topic"] == intent
            assert frame["body"] == body and frame["required_claim_ids"] == ["vision"]
        return {"complete": True, "failures": [], "conflicts": []}

    client = Client(reply)
    result = await editorial.review(
        client, intent, target, [record], [], original_body=original, original_meta=target.draft_meta, char_budget=7000
    )
    grounding = [frame for frame in client.calls if frame["mode"] == "grounding"]
    assert len(grounding) > 1 and "## Vision" not in grounding[0]["body"]
    assert any(frame["mode"] == "required" for frame in client.calls)
    assert result["complete"] and not result["failures"]


@pytest.mark.asyncio
async def test_known_section_nonliteral_anchor_keeps_blocker_and_durable_section_location(cfg, store):
    state = store.create(JobState("", "section-review", "Device"))[0]
    target = change("# Device\nDevice always lowers costs.\n")
    state.changes = [target]
    frames = []

    def handler(request):
        frame = json.loads(json.loads(request.content)["messages"][1]["content"].split("\n", 1)[1])
        frames.append(frame)
        result = issue(frame, "Model paraphrase not literal in the draft", "grounding")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(result)}, "finish_reason": "stop"}]}
        )

    client = ModelClient(cfg, state.usage, transport=httpx.MockTransport(handler))
    result = await editorial.review(
        client, "Device", target, [source()], [], char_budget=6000, progress=lambda _: store.save(state)
    )
    failure = result["failures"][0]
    assert failure["severity"] == "blocker" and failure["anchor"] is None
    assert failure["detail"] == "Restore the source qualification"
    assert failure["section_id"] == frames[0]["sections"][0]["id"]
    assert len(frames) == state.usage.model_calls == 1
    restored = store.load(state.job_id)
    assert await editorial.review(client, "Device", restored.changes[0], [source()], [], char_budget=6000) == result
    assert len(frames) == 1
    from doclayer.curator.editing import (
        _article_repair_input,
        _article_repair_locations,
        page_units,
    )

    review, _ = _article_repair_input(restored.changes[0].check_progress, target.draft_body, {})
    _, rows = _article_repair_locations(review, target.draft_body, page_units(target.draft_body), {})
    assert rows[0]["section_id"] == failure["section_id"] and rows[0]["anchor"] is None


@pytest.mark.asyncio
async def test_operator_completion_reviews_unchanged_article_and_reuses_completed_cache():
    body = "# Device\n\n## Runtime\n\nExisting runtime.\n"
    target = change(body)
    corrections = [
        {
            "correction_id": "operator-1",
            "instruction": "Keep runtime and add CI retention.",
        }
    ]
    client = Client(lambda frame: issue(frame, None, "correction-completion"))
    result = await editorial.review(
        client,
        "Device",
        target,
        [],
        corrections,
        char_budget=12000,
        original_body=body,
        original_meta=target.draft_meta,
    )
    assert [f["mode"] for f in client.calls] == ["correction"]
    assert client.calls[0]["body"] == body
    assert client.calls[0]["corrections"] == corrections
    assert result["failures"][0]["severity"] == "blocker"
    restored = PageChange.from_dict(target.to_dict())
    assert (
        await editorial.review(
            client,
            "Device",
            restored,
            [],
            corrections,
            char_budget=12000,
            original_body=body,
            original_meta=target.draft_meta,
        )
        == result
    )
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_operator_completion_warning_is_blocking_and_foreign_claim_id_rejected():
    target = change("# Device\nExisting facts.\n")
    corrections = [{"instruction": "Add runtime and CI retention."}]

    def response(frame):
        result = issue(frame, None, "correction-completion")
        result["failures"][0]["severity"] = "warning"
        return result

    result = await editorial.review(Client(response), "Device", target, [], corrections, char_budget=12000)
    assert all(f["severity"] == "blocker" for f in result["failures"])

    def invalid(frame):
        return issue(frame, None, "correction-completion", "not-an-assigned-claim")

    with pytest.raises(ModelError, match="failure location"):
        await editorial.review(
            Client(invalid),
            "Device",
            change(target.draft_body),
            [],
            corrections,
            char_budget=12000,
        )


@pytest.mark.asyncio
async def test_operator_completion_full_article_overflow_has_no_dispatch():
    body = "# Device\n" + "Old canonical fact. " * 700
    target = change(body)
    client = Client(lambda frame: {"complete": True, "failures": [], "conflicts": []})
    with pytest.raises(editorial.EditorialContextOverflow, match="operator corrections"):
        await editorial.review(
            client,
            "Device",
            target,
            [],
            [{"instruction": "Add runtime."}],
            char_budget=6000,
            original_body=body,
            original_meta=target.draft_meta,
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_factual_operator_authority_is_exact_and_not_topic_research():
    target = change("# Device\nExact operator fact.\nInvented details.\n")
    corrections = [{"instruction": "Exact operator fact. Also research optional interfaces."}]

    class AuthorityClient(Client):
        async def chat_bounded_json(self, system, user, *, char_budget):
            frame = json.loads(user.split("\n", 1)[1])
            if frame["mode"] == "grounding":
                assert system == editorial.CORRECTION_GROUNDING_SYSTEM
                assert "ONLY the exact supplied facts" in system
                assert "not supporting evidence for invented details or citations" in system
                assert frame["evidence"] == [] and frame["corrections"] == corrections
                return issue(frame, "Invented details.", "evidence")
            assert system == editorial.CORRECTION_SYSTEM
            return {"complete": True, "failures": [], "conflicts": []}

    result = await editorial.review(AuthorityClient(None), "Device", target, [], corrections, char_budget=12000)
    assert len(result["failures"]) == 1 and result["failures"][0]["anchor"] == "Invented details."

    class OrdinaryClient(Client):
        async def chat_bounded_json(self, system, user, *, char_budget):
            assert system == editorial.GROUNDING_SYSTEM
            return {"complete": True, "failures": [], "conflicts": []}

    await editorial.review(OrdinaryClient(None), "Device", change("# Device\nFact."), [], [], char_budget=12000)
