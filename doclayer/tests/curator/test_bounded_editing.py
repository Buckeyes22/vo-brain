"""Whole-page preservation with bounded model outputs, anchors and checkpoints."""

import ast
import copy
import hashlib
import json
import re
from pathlib import Path

import pytest

from doclayer import frontmatter
from doclayer.curator import curate, editing, ops
from doclayer.curator.model import ModelError, OutputTruncated
from doclayer.curator.state import Evidence, JobState, PageChange

LONG_PAGES = Path(__file__).resolve().parents[1] / "fixtures" / "long_pages"


def routing_response(user):
    # Real protocol response: semantic routing still runs through edit_page.
    frame_text, evidence = user.split("\nEvidence:\n", 1)
    frame = json.loads(frame_text.split("\n", 1)[1])
    rows = re.findall(r"^- \[(claim-[^ /]+).*?\): (.+)$", evidence, re.M)
    assert rows
    routes = []
    for identity, claim in rows:
        sections = frame["sections"]
        configurations = [row for row in sections if row["heading_path"].endswith("Configuration")]
        if "late_firmware" in claim:
            target = configurations[0]["section_id"]
        elif "late_calibration" in claim:
            target = configurations[-1]["section_id"]
        elif any(claim[:80] in row["preview"] for row in sections):
            target = next(row["section_id"] for row in sections if claim[:80] in row["preview"])
        else:
            target = max(
                sections,
                key=lambda row: len(
                    set(re.findall(r"\w+", claim.lower())) & set(re.findall(r"\w+", row["heading_path"].lower()))
                ),
            )["section_id"]
        routes.append(
            {
                "id": identity,
                "section_id": target,
                "new_heading": None,
                "rationale": "The fixture fact belongs to this existing section.",
            }
        )
    return {"routes": routes}


def response(user, *, append="", edits=None):
    identities = ast.literal_eval(re.search(r"Claim IDs to integrate or prove already present: (.+)", user)[1])
    edits = edits or []
    view = user.split("Current section view:\n", 1)[1].split("\nClaim IDs", 1)[0]
    claims = dict(re.findall(r"^- \[(claim-[^ /]+).*?\): (.+)$", user, re.M))
    dispositions = []
    for identity in identities:
        claim = claims[identity]
        if view.count(claim) == 1:
            dispositions.append({"id": identity, "status": "already_present", "anchor": claim})
        elif edits:
            assert len(identities) == 1  # This fixture replacement represents one whole fact.
            dispositions.append({"id": identity, "status": "integrated", "anchor": edits[0]["new"]})
        else:
            append += ("\n" if append else "") + claim + " [Source](https://device.example/manual)"
            dispositions.append({"id": identity, "status": "integrated", "anchor": claim})
    return {
        "section_id": re.search(r"Section ID: (.+)", user)[1],
        "edits": edits,
        "append": append,
        "handled_claim_ids": identities,
        "claim_dispositions": dispositions,
        "notes": [],
    }


def supported(text="New qualified device fact", source="s"):
    return Evidence(
        source,
        "device manual",
        "#Manual",
        "data",
        text,
        url="https://device.example/manual",
        quote="Actual original-source context " + text,
        support_status="supported",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "model-system-card",
        "openai-api-tools",
        "openai-responses-api",
        "openai-api-performance-modes",
        "openai-model-lineup",
        "prompting-guidance",
    ],
)
async def test_long_pages_survive_bounded_edits_at_same_input_and_output_limits(name):
    raw = (LONG_PAGES / f"{name}.md").read_text()
    meta, original = frontmatter.parse(raw)
    change = PageChange("g", f"wiki/{name}.md", "update", title=meta["title"])

    class Client:
        edit_calls = 0

        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            self.edit_calls += 1
            assert len(system) + len(user) <= 32768
            assert original not in user and "Never return the complete page" in user
            # A tiny output replaces an impossible full-page response at4096tokens.
            return response(user)

    client = Client()
    existing = next(
        line for line in editing.page_units(original)[0].text.splitlines() if line.strip() and not line.startswith("#")
    )
    result = await editing.edit_page(
        client,
        "Audit model/API qualification",
        change,
        [supported(existing)],
        raw,
        [],
        char_budget=32768,
        today="2026-10-02",
    )
    assert result["body"] == original and result["frontmatter"]["concept_id"] == meta["concept_id"]
    assert client.edit_calls == 1


@pytest.mark.asyncio
async def test_late_fence_and_table_facts_are_edited_without_losing_other_material():
    body = "Introduction operator fact.\n# Device\n## Configuration\nInitial operator fact.\n"
    body += "```python\n" + "unchanged_code = 123\n" * 400 + 'late_firmware = "wrong"\n```\n'
    body += "## Configuration\n| Key | Value |\n| --- | --- |\n"
    body += "".join(f"| key_{i} | operator_value_{i} |\n" for i in range(200)) + "| late_calibration | wrong_value |\n"
    body += "### Nested\nLATE_OPERATOR_SENTINEL\n"
    records = [supported("late_firmware must equal correct"), supported("late_calibration correct_value", "t")]

    class Client:
        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            assert len(system) + len(user) <= 16000
            edits = []
            if 'late_firmware = "wrong"' in user:
                edits = [{"old": 'late_firmware = "wrong"', "new": 'late_firmware = "correct"'}]
            if "| late_calibration | wrong_value |" in user:
                edits = [{"old": "| late_calibration | wrong_value |", "new": "| late_calibration | correct_value |"}]
            assert edits  # late views, not arbitrary first parts, are supplied
            return response(user, edits=edits)

    result = await editing.edit_page(
        Client(),
        "Device procedure",
        PageChange("g", "wiki/device.md", "update"),
        records,
        body,
        [],
        char_budget=16000,
        today="2026-10-02",
    )
    expected = body.replace('late_firmware = "wrong"', 'late_firmware = "correct"').replace(
        "| late_calibration | wrong_value |", "| late_calibration | correct_value |"
    )
    assert result["body"] == expected
    units = editing.page_units(body)
    assert len({unit.id for unit in units}) == len(units)
    assert len([unit for unit in units if unit.label.endswith("Configuration")]) == 2


@pytest.mark.asyncio
async def test_progress_resumes_only_with_matching_body_and_evidence_hash(cfg, store):
    change = PageChange("g", "wiki/device.md", "update")
    records = [supported(f"Device fact {i}.", str(i)) for i in range(13)]
    original = "# Device\nOperator fact.\n"
    checkpoints = []
    state = store.create(JobState(job_id="", idempotency_key="", intent="Device"))[0]
    state.changes = [change]
    store.save(state)

    class Interrupted:
        edit_calls = 0

        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            self.edit_calls += 1
            if self.edit_calls > 1:
                raise ModelError("inference outage")
            return response(user, append="FIRST_BATCH_FACT [Source](https://device.example/manual)")

    def retain(result):
        checkpoints.append(result)
        change.draft_body = result["body"]
        change.draft_meta = result["frontmatter"]
        change.draft_progress = result["draft_progress"]
        store.save(state)

    with pytest.raises(ModelError):
        await editing.edit_page(
            Interrupted(),
            "Device",
            change,
            records,
            original,
            [],
            char_budget=16000,
            today="2026-10-02",
            progress=retain,
        )
    assert len(change.draft_progress["handled_claim_ids"]) == 6
    change = store.load(state.job_id).changes[0]  # real atomic job-state restart

    class Resume:
        def __init__(self):
            self.batches = []

        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            reply = response(user)
            self.batches.append(reply["handled_claim_ids"])
            return reply

    resumed = Resume()
    current = frontmatter.build(change.draft_meta, change.draft_body)
    result = await editing.edit_page(
        resumed, "Device", change, records, current, [], char_budget=16000, today="2026-10-02"
    )
    assert len(resumed.batches) == 2 and result["body"].count("FIRST_BATCH_FACT") == 1
    assert not set(change.draft_progress["handled_claim_ids"]) & set(sum(resumed.batches, []))
    # Lost body / changed evidence cannot silently skip earlier work.
    reset = Resume()
    await editing.edit_page(reset, "Device", change, records, original, [], char_budget=16000, today="2026-10-02")
    assert len(reset.batches) == 3


@pytest.mark.asyncio
async def test_truncation_and_invalid_anchor_receive_bounded_repair_without_page_rewrite():
    body = "# Device\nOperator fact.\n"
    record = supported()

    class Client:
        edit_calls = 0

        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            self.edit_calls += 1
            assert len(system) + len(user) <= 6000
            if self.edit_calls == 1:
                raise OutputTruncated("partial")
            if self.edit_calls == 2:
                return response(user, edits=[{"old": "MISSING_ANCHOR", "new": "Wrong"}])
            return response(user, append="Qualified fact. [Source](https://device.example/manual)")

    client = Client()
    result = await editing.edit_page(
        client,
        "Device",
        PageChange("g", "wiki/device.md", "update"),
        [record],
        body,
        [],
        char_budget=6000,
        today="2026-10-02",
    )
    assert client.edit_calls == 3 and body.rstrip("\n") in result["body"]


def test_unsafe_overlap_heading_fence_and_duplicate_claim_edits_fail_closed():
    unit = editing.page_units("# Device\nExact operator text.\n```py\nx=1\n```\n")[0]
    base = {"section_id": unit.id, "handled_claim_ids": ["c"], "append": ""}
    for edits in [
        [{"old": "Exact operator", "new": "a"}, {"old": "operator text", "new": "b"}],
        [{"old": "# Device", "new": "# Lost"}],
        [{"old": "```py", "new": "~~~py"}],
    ]:
        with pytest.raises(ModelError):
            editing.apply_edits(unit, {**base, "edits": edits}, {"c"}, max_output_chars=6000)
    with pytest.raises(ModelError):
        editing.apply_edits(unit, {**base, "edits": [], "handled_claim_ids": ["c", "c"]}, {"c"}, max_output_chars=6000)


def test_known_legacy_fullpage_guard_can_resume_same_failed_job(cfg, store):
    state = store.create(JobState(job_id="", idempotency_key="", intent="Device"))[0]
    state.status = "failed"
    state.stage = "draft"
    state.error = "inference: full existing page exceeds bounded drafting context; no incomplete update"
    state.changes = [PageChange("g", "wiki/device.md", "update")]
    store.save(state)
    assert ops.handle("resume", {"job_id": state.job_id}, cfg, store)["status"] == "queued"
    assert store.load(state.job_id).changes[0].path == "wiki/device.md"


@pytest.mark.asyncio
async def test_checker_blocks_unsupported_tail_then_proves_whole_page_required_coverage():
    alpha = supported("Alpha firmware procedure", source="a")
    omega = supported("Omega calibration procedure", source="b")
    # Coherent full-page semantics plus source records fit the unchanged 12k bound.
    body = "# Manual\n## Alpha firmware\n" + "Alpha firmware procedure. " * 110
    body += "\n## Omega calibration\n" + "Omega calibration procedure. " * 110 + "\nUNSUPPORTED_OLD_TAIL"
    change = PageChange(
        "g",
        "wiki/manual.md",
        "update",
        draft_body=body,
        claim_ids=[curate.evidence_id(alpha), curate.evidence_id(omega)],
    )

    class Client:
        def __init__(self):
            self.seen = []
            self.required_seen = []

        async def chat_json(self, system, user):
            assert user.startswith("ARTICLE EDITORIAL REVIEW")
            assert len(system) + len(user) <= 12000
            frame = json.loads(user.split("\n", 1)[1])
            self.seen.append(frame)
            failures = []
            if "UNSUPPORTED_OLD_TAIL" in frame["body"]:
                section = next(
                    v for v in frame["sections"] if "UNSUPPORTED_OLD_TAIL" in frame["body"][v["start"] : v["end"]]
                )
                failures.append(
                    {
                        "severity": "blocker",
                        "check": "evidence",
                        "detail": "Unsupported old tail claim",
                        "section_id": section["id"],
                        "anchor": "UNSUPPORTED_OLD_TAIL",
                    }
                )
            for identity in frame["required_claim_ids"]:
                self.required_seen.append(identity)
                record = next(v for v in frame["evidence"] if v["id"] == identity)
                if record["text"] not in frame["body"]:
                    failures.append(
                        {
                            "severity": "blocker",
                            "check": "required-contribution",
                            "detail": "Assigned procedure is absent",
                            "claim_id": identity,
                            "section_id": frame["sections"][-1]["id"],
                            "anchor": None,
                        }
                    )
            return {"complete": True, "failures": failures, "conflicts": []}

    client = Client()
    result = await curate.check_draft(client, "Procedure", change, [alpha, omega], [], char_budget=12000)
    assert any(f["check"] == "evidence" for f in result["failures"])
    assert any("UNSUPPORTED_OLD_TAIL" in f["body"] for f in client.seen)
    change.draft_body = body.replace("UNSUPPORTED_OLD_TAIL", "")
    client = Client()
    result = await curate.check_draft(client, "Procedure", change, [alpha, omega], [], char_budget=12000)
    assert not result["failures"] and set(client.required_seen) == set(change.claim_ids)
    assert change.check_progress["article_review"]["complete"]
    change.draft_body = change.draft_body.replace("Omega calibration procedure. ", "")
    client = Client()
    result = await curate.check_draft(client, "Procedure", change, [alpha, omega], [], char_budget=12000)
    assert len(result["failures"]) == 1 and result["failures"][0]["check"] == "required-contribution"
    assert result["failures"][0]["claim_id"] == curate.evidence_id(omega)


@pytest.mark.asyncio
async def test_bounded_repair_visits_unassigned_single_line_introduction_and_late_fact():
    body = "UNSUPPORTED_INTRODUCTION\n# Device\n## Details\n" + "Operator fact. " * 500 + "\nUNSUPPORTED_LATE_FACT\n"
    change = PageChange(
        "g",
        "wiki/device.md",
        "update",
        draft_body=body,
        draft_meta={"concept_id": "c-preserved", "updated": "2026-10-02"},
    )

    class Client:
        seen = []

        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            self.seen.append(user)
            edits = []
            if "Current section view:\nUNSUPPORTED_INTRODUCTION" in user:
                edits = [{"old": "UNSUPPORTED_INTRODUCTION", "new": "Qualified introduction"}]
            elif "\nUNSUPPORTED_LATE_FACT\n" in user:
                edits = [{"old": "UNSUPPORTED_LATE_FACT", "new": "Qualified late fact"}]
            return response(user, edits=edits)

    client = Client()
    result = await curate.repair_draft(
        client,
        "Repair exact unsupported claims",
        change,
        [{"detail": "Remove unsupported intro and late fact"}],
        [],
        [],
        char_budget=12000,
    )
    assert "UNSUPPORTED_INTRODUCTION" not in result["body"] and "UNSUPPORTED_LATE_FACT" not in result["body"]
    assert result["body"].count("Operator fact.") == 500 and result["frontmatter"]["concept_id"] == "c-preserved"


def test_unclosed_long_fence_view_keeps_final_source_line():
    body = "# Device\n```python\n" + "unchanged_line = 1\n" * 500 + "LATE_FIRMWARE_WRONG"
    pieces = editing.views(editing.page_units(body), 2000)
    assert any("LATE_FIRMWARE_WRONG" in text for _, _, text in pieces)
    assert "".join(unit.text for unit in editing.page_units(body)) == body


@pytest.mark.asyncio
async def test_long_operator_correction_without_new_source_updates_its_exact_region():
    body = (
        "# Device\n## Unrelated\n"
        + "Unrelated operator fact. " * 400
        + "\n## Calibration\nCalibration interval is weekly.\n"
    )
    corrections = [
        {
            "page": "wiki/device.md",
            "claim": "Calibration interval is weekly.",
            "instruction": "Calibration interval is every two weeks.",
        }
    ]

    class Client:
        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            assert "Calibration interval is weekly." in user.split("Current section view:\n", 1)[1]
            assert "every two weeks" in user
            return response(
                user,
                edits=[{"old": "Calibration interval is weekly.", "new": "Calibration interval is every two weeks."}],
            )

    change = PageChange("g", "wiki/device.md", "update")
    result = await editing.edit_page(
        Client(), "Apply operator correction", change, [], body, corrections, char_budget=16000, today="2026-10-02"
    )
    assert result["body"] == body.replace("Calibration interval is weekly.", "Calibration interval is every two weeks.")


@pytest.mark.asyncio
async def test_correction_only_late_code_and_table_in_same_oversized_section():
    body = (
        "# Device\n## Calibration\n```text\n"
        + "operator_line\n" * 400
        + "wrong_calibration\n```\n| Key | Value |\n| --- | --- |\n"
        + "| unchanged | operator |\n" * 200
        + "| calibration | wrong |\n"
    )
    corrections = [
        {"claim": "wrong_calibration", "instruction": "correct_calibration"},
        {"claim": "| calibration | wrong |", "instruction": "| calibration | correct |"},
    ]

    class Client:
        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            view = user.split("Current section view:\n", 1)[1]
            found = [old for old in ["wrong_calibration", "| calibration | wrong |"] if old in view]
            assert found
            return response(user, edits=[{"old": old, "new": old.replace("wrong", "correct")} for old in found])

    result = await editing.edit_page(
        Client(),
        "Apply corrections",
        PageChange("g", "wiki/device.md", "update"),
        [],
        body,
        corrections,
        char_budget=12000,
        today="2026-10-02",
    )
    assert result["body"] == body.replace("wrong_calibration", "correct_calibration").replace(
        "| calibration | wrong |", "| calibration | correct |"
    )


@pytest.mark.asyncio
async def test_repair_checkpoint_skips_completed_regions_after_outage():
    body = "# Device\n## First\nWRONG_FIRST\n## Second\nWRONG_SECOND\n" + "operator. " * 1000
    change = PageChange(
        "g", "wiki/device.md", "update", draft_meta={"concept_id": "c", "updated": "2026-10-02"}, draft_body=body
    )

    class Client:
        edit_calls = 0

        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            self.edit_calls += 1
            if self.edit_calls == 2:
                raise ModelError("inference outage")
            return response(
                user,
                edits=[{"old": "WRONG_FIRST", "new": "RIGHT_FIRST"}]
                if "WRONG_FIRST" in user.split("Current section view:\n", 1)[1]
                else [],
            )

    def retain(result):
        change.draft_body = result["body"]
        change.draft_meta = result["frontmatter"]
        change.draft_progress = result["draft_progress"]

    with pytest.raises(ModelError):
        await curate.repair_draft(Client(), "Device", change, [], [], [], char_budget=12000, progress=retain)
    assert "RIGHT_FIRST" in change.draft_body and change.draft_progress["completed_task_ids"]

    class Resume:
        seen = []

        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            self.seen.append(user)
            assert "Current section view:\n## First\nRIGHT_FIRST" not in user
            return response(user)

    resumed = Resume()
    await curate.repair_draft(resumed, "Device", change, [], [], [], char_budget=12000)
    assert resumed.seen


@pytest.mark.asyncio
async def test_repair_frozen_views_survive_length_shifting_edit_and_outage():
    body = (
        "# Device\n## Details\n"
        + "FIRST_OPERATOR_LINE_TO_SHORTEN\n"
        + "".join(f"line{i:03d} operator calibration fact.\n" for i in range(200))
    )
    change = PageChange("g", "wiki/device.md", "update", draft_meta={"updated": "2026-10-02"}, draft_body=body)
    original_views = editing.views(editing.page_units(body), 3000)
    substantive = [text for _, _, text in original_views if "operator calibration" in text]
    first_unseen = re.search(r"line\d+", substantive[1])[0]

    class Interrupted:
        edit_calls = 0

        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            self.edit_calls += 1
            if self.edit_calls == 2:
                raise ModelError("inference outage")
            return response(user, edits=[{"old": "FIRST_OPERATOR_LINE_TO_SHORTEN", "new": "short"}])

    def retain(result):
        change.draft_body = result["body"]
        change.draft_meta = result["frontmatter"]
        change.draft_progress = result["draft_progress"]

    with pytest.raises(ModelError):
        await curate.repair_draft(Interrupted(), "Device", change, [], [], [], char_budget=12000, progress=retain)

    class Resume:
        seen = []

        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            self.seen.append(user.split("Current section view:\n", 1)[1])
            return response(user)

    resumed = Resume()
    await curate.repair_draft(resumed, "Device", change, [], [], [], char_budget=12000)
    assert any(first_unseen in view for view in resumed.seen)
    assert all("FIRST_OPERATOR_LINE_TO_SHORTEN" not in view for view in resumed.seen)


@pytest.mark.asyncio
async def test_pipeline_same_logical_repair_survives_multiple_outages_without_resetting_cap(
    cfg, store, checkout, monkeypatch
):
    from conftest import run_git

    import doclayer.curator.pipeline as pipeline_module
    from doclayer.curator.pipeline import Pipeline
    from doclayer.curator.validate import Failure

    state = store.create(
        JobState(
            job_id="", idempotency_key="", intent="original intent", base_commit=run_git(checkout, "rev-parse", "HEAD")
        )
    )[0]
    state.stage = "validate"
    state.status = "processing"
    state.limits = {"max_repair_passes": 1}
    change = PageChange("g", "wiki/new.md", "create", draft_body="# Device\nWRONG\n", draft_meta={})
    change.status = "drafted"
    state.changes = [change]
    store.save(state)
    calls = []

    async def verify(*args):
        pass

    async def check(*args, **kwargs):
        return {"failures": [], "conflicts": []}

    async def repair(client, topic, current, failures, *args, progress=None, **kwargs):
        calls.append((topic, failures, current.repair_passes))
        if len(calls) < 3:
            progress(
                {
                    "body": current.draft_body + "partial " + str(len(calls)) + "\n",
                    "frontmatter": {},
                    "draft_progress": {"saved": "task"},
                }
            )
            raise ModelError("inference outage")
        return {"body": "# Device\nRIGHT\n", "frontmatter": {}, "draft_progress": {}}

    monkeypatch.setattr(Pipeline, "_verify_selected", verify)
    monkeypatch.setattr(Pipeline, "_evidence", lambda *args: [])
    monkeypatch.setattr(Pipeline, "_scope_failures", lambda *args: [])
    monkeypatch.setattr(
        pipeline_module,
        "run_deterministic_checks",
        lambda current, *args: [Failure("test", "original failure")] if "WRONG" in current.draft_body else [],
    )
    monkeypatch.setattr(curate, "repair_draft", repair)
    monkeypatch.setattr(curate, "check_draft", check)
    for _ in range(2):
        with pytest.raises(ModelError):
            await Pipeline(cfg, store)._stage_validate(state)
        state = store.load(state.job_id)
        assert state.changes[0].repair_passes == 1 and state.changes[0].repair_context
    await Pipeline(cfg, store)._stage_validate(state)
    assert state.changes[0].status == "validated" and state.changes[0].repair_passes == 1
    assert not state.changes[0].repair_context and len(calls) == 3 and all(call == calls[0] for call in calls)


@pytest.mark.asyncio
async def test_heading_correction_preserves_nested_material_and_resumes_original_regions():
    from doclayer.curator.validate import check_update_scope

    old = "## Provider natively outputs images"
    new = "## Provider requires an external image tool"
    body = (
        "# Device\n"
        + old
        + "\n"
        + ("Operator calibration remains 123.\n" * 250)
        + "### Installation\n```python\n# Preserve this comment\nsetting = 456\n```\n| Key | Value |\n| --- | --- |\n| keep | 789 |\nUNSUPPORTED_LATE\n"
    )
    change = PageChange("g", "wiki/device.md", "update", draft_body=body, draft_meta={"concept_id": "c-12345678"})
    change.check_progress = {
        "claims": {"heading": {"span": old, "claim": "Provider natively outputs images"}},
        "verdicts": {"heading": {"supported": False}},
    }

    class Interrupted:
        edit_calls = 0

        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            self.edit_calls += 1
            if self.edit_calls == 2:
                raise ModelError("inference outage")
            return response(user, edits=[{"old": old, "new": new}])

    def retain(result):
        change.draft_body = result["body"]
        change.draft_meta = result["frontmatter"]
        change.draft_progress = result["draft_progress"]

    with pytest.raises(ModelError):
        await editing.edit_page(
            Interrupted(),
            "Repair unsupported provider capabilities",
            change,
            [],
            body,
            [],
            char_budget=12000,
            today="2026-10-02",
            progress=retain,
            repair=True,
        )
    assert new in change.draft_body and [
        (row["old"], row["new"]) for row in change.draft_progress["heading_renames"]
    ] == [(old, new)]

    class Resume:
        seen = []

        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            self.seen.append(user)
            return response(
                user,
                edits=[{"old": "UNSUPPORTED_LATE", "new": "Qualified supported late fact."}]
                if "UNSUPPORTED_LATE" in user
                else [],
            )

    client = Resume()
    result = await editing.edit_page(
        client,
        "Repair unsupported provider capabilities",
        change,
        [],
        change.draft_body,
        [],
        char_budget=12000,
        today="2026-10-02",
        repair=True,
    )
    expected = body.replace(old, new).replace("UNSUPPORTED_LATE", "Qualified supported late fact.")
    assert result["body"] == expected and any("UNSUPPORTED_LATE" in user for user in client.seen)
    assert not check_update_scope(
        {}, body, {}, result["body"], "wiki/device.md", result["draft_progress"]["heading_renames"]
    )
    assert check_update_scope({}, body, {}, result["body"], "wiki/device.md")


@pytest.mark.asyncio
async def test_pipeline_citation_hydration_preserves_bound_heading_repair_and_frozen_tasks():
    import copy
    import hashlib

    from doclayer.curator.pipeline import finalize_draft
    from doclayer.curator.validate import check_update_scope

    old = "## Provider natively outputs images"
    new = "## Provider requires an external tool [src-1234567890]"
    original = "# Device\n" + old + "\nOperator calibration remains 123.\n"
    change = PageChange("g", "wiki/device.md", "update", draft_body=original)
    change.check_progress = {"claims": {"heading": {"span": old}}, "verdicts": {"heading": {"supported": False}}}

    class Client:
        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            return response(user, edits=[{"old": old, "new": new}] if old in user else [])

    draft = await editing.edit_page(
        Client(),
        "Repair unsupported heading",
        change,
        [],
        original,
        [],
        char_budget=12000,
        today="2026-10-02",
        repair=True,
    )
    frozen = copy.deepcopy(draft["draft_progress"])
    record = supported()
    record.source_id = "src-1234567890"
    finalize_draft(change, draft, [record])
    assert "[Source](<https://device.example/manual>)" in change.draft_body
    assert change.draft_progress["body_hash"] == hashlib.sha256(change.draft_body.encode()).hexdigest()
    assert not check_update_scope(
        {}, original, {}, change.draft_body, change.path, change.draft_progress["heading_renames"]
    )
    for key in ("completed_task_ids", "unit_identity", "repair_views", "repair_views_hash", "selection_hash"):
        assert change.draft_progress[key] == frozen[key]
    assert draft["draft_progress"] == frozen  # no mutation of durable partial checkpoint


def test_pipeline_hydration_never_rebinds_mismatched_edit_proof():
    import hashlib

    from doclayer.curator.pipeline import finalize_draft

    change = PageChange("g", "wiki/device.md", "update")
    draft = {
        "body": "# Device\nFact [src-1234567890].\n",
        "draft_progress": {"body_hash": "wrong", "heading_renames": []},
    }
    record = supported()
    record.source_id = "src-1234567890"
    finalize_draft(change, draft, [record])
    assert change.draft_progress["body_hash"] == "wrong"
    assert change.draft_progress["body_hash"] != hashlib.sha256(change.draft_body.encode()).hexdigest()


def test_hydrated_heading_repair_chain_preserves_canonical_first_anchor():
    import hashlib

    from doclayer.curator.editing import page_units
    from doclayer.curator.pipeline import finalize_draft
    from doclayer.curator.validate import check_update_scope

    old = "## Unsupported native capability"
    intermediate = "## External tool [src-1234567890]"
    final = "## Qualified external tool [src-1234567890]"
    base = "# Device\n" + old + "\nOperator content.\n"
    body = base.replace(old, final)
    identity = page_units(base)[1].id
    progress = {
        "body_hash": hashlib.sha256(body.encode()).hexdigest(),
        "heading_renames": [
            {"unit_id": identity, "old": old, "new": intermediate},
            {"unit_id": identity, "old": intermediate, "new": final},
        ],
    }
    change = PageChange("g", "wiki/device.md", "update")
    record = supported()
    record.source_id = "src-1234567890"
    finalize_draft(change, {"body": body, "draft_progress": progress}, [record])
    assert change.draft_progress["heading_renames"][0]["old"] == old
    assert not check_update_scope(
        {}, base, {}, change.draft_body, change.path, change.draft_progress["heading_renames"]
    )


def test_repair_heading_lane_cannot_remove_relevel_or_reorder_sections():
    unit = editing.page_units("## Unsupported capability\nOperator fact.\n### Nested\nNested fact.\n")[0]
    # A heading text correction is permitted only within the same structural slot.
    valid = {
        "section_id": unit.id,
        "edits": [{"old": "## Unsupported capability", "new": "## Qualified capability"}],
        "append": "",
        "handled_claim_ids": [],
    }
    assert "Qualified capability" in editing.apply_edits(
        unit,
        valid,
        set(),
        max_output_chars=6000,
        allow_heading_correction=True,
        allowed_heading_lines={"## Unsupported capability"},
    )
    for replacement in ("", "# Wrong hierarchy", "## New\n### Injected"):
        invalid = {**valid, "edits": [{"old": "## Unsupported capability", "new": replacement}]}
        with pytest.raises(ModelError, match="structural headings"):
            editing.apply_edits(
                unit,
                invalid,
                set(),
                max_output_chars=6000,
                allow_heading_correction=True,
                allowed_heading_lines={"## Unsupported capability"},
            )


@pytest.mark.asyncio
async def test_later_evidence_batches_see_corrected_heading_without_replaying_anchor():
    old = "## Provider natively outputs images"
    new = "## Provider requires an external image tool"
    body = (
        old
        + "\nOperator fact remains.\n"
        + "\n".join("Provider image capabilities qualified fact " + str(index) + "." for index in range(13))
        + "\n"
    )
    change = PageChange("g", "wiki/device.md", "update", draft_body=body)
    change.check_progress = {
        "claims": {"heading": {"span": old, "claim": "Provider natively outputs images"}},
        "verdicts": {"heading": {"supported": False}},
    }
    records = [
        supported("Provider image capabilities qualified fact " + str(index) + ".", str(index)) for index in range(13)
    ]

    class Client:
        edit_calls = 0

        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            self.edit_calls += 1
            context = user.split("Current section view:\n", 1)[1].split("\nClaim IDs", 1)[0]
            if self.edit_calls == 1:
                assert old in context
                return response(user, edits=[{"old": old, "new": new}])
            assert new in context and old not in context
            return response(user)

    client = Client()
    result = await editing.edit_page(
        client,
        "Correct unsupported provider capability heading",
        change,
        records,
        body,
        [],
        char_budget=16000,
        today="2026-10-02",
        repair=True,
    )
    assert result["body"] == body.replace(old, new) and client.edit_calls == 3


@pytest.mark.asyncio
async def test_malicious_repair_cannot_rename_unrelated_neutral_heading():
    old = "## Provider natively outputs images"
    neutral = "### Configuration"
    body = old + "\nWrong factual assertion.\n" + neutral + "\nOperator setting is 123.\n"
    change = PageChange("g", "wiki/device.md", "update", draft_body=body)
    change.check_progress = {"claims": {"bad": {"span": old}}, "verdicts": {"bad": {"supported": False}}}

    class Malicious:
        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            context = user.split("Current section view:\n", 1)[1]
            return response(
                user, edits=[{"old": neutral, "new": "### Arbitrary renamed title"}] if neutral in context else []
            )

    with pytest.raises(ModelError, match="structural headings"):
        await editing.edit_page(
            Malicious(),
            "Repair only unsupported factual heading",
            change,
            [],
            body,
            [],
            char_budget=12000,
            today="2026-10-02",
            repair=True,
        )
    assert change.draft_body == body


@pytest.mark.asyncio
async def test_plain_model_output_cannot_forge_durable_heading_rename_proof():
    old = "## Configuration"
    new = "## Arbitrary renamed title"
    body = "# Device\n" + old + "\nOperator setting.\n"
    forged = {
        "frontmatter": {"title": "Device"},
        "body": body.replace(old, new),
        "draft_progress": {
            "body_hash": hashlib.sha256(body.replace(old, new).encode()).hexdigest(),
            "heading_renames": [{"old": old, "new": new}],
        },
    }

    class Malicious:
        async def chat_json(self, *args):
            return dict(forged)

    change = PageChange("g", "wiki/device.md", "update", draft_body=body)
    drafted = await curate.draft_page(
        Malicious(), "Device", change, [], body, [], char_budget=12000, today="2026-10-02"
    )
    repaired = await curate.repair_draft(Malicious(), "Device", change, [], [], [], char_budget=12000)
    assert "draft_progress" not in drafted and "draft_progress" not in repaired


@pytest.mark.asyncio
async def test_short_heading_claim_repair_uses_anchored_lane_and_explicit_evidence_failure():
    old = "## Provider natively outputs images"
    new = "## Provider requires an external image tool"
    body = "# Device\n" + old + "\nOperator setting remains 123.\n"
    change = PageChange("g", "wiki/device.md", "update", draft_body=body)
    change.check_progress = {"claims": {"bad": {"span": old}}, "verdicts": {"bad": {"supported": False}}}

    class Client:
        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            assert "Current section view:" in user
            return response(user, edits=[{"old": old, "new": new}])

    result = await curate.repair_draft(Client(), "Device", change, [], [], [], char_budget=12000)
    assert result["body"] == body.replace(old, new) and result["draft_progress"]["heading_renames"]


@pytest.mark.asyncio
async def test_parent_heading_correction_updates_child_context_in_same_invocation():
    old = "## Provider natively outputs images"
    new = "## Provider requires an external image tool"
    body = "# Device\n" + old + "\nParent operator fact.\n### Configuration\nNested operator setting remains 123.\n"
    change = PageChange("g", "wiki/device.md", "update", draft_body=body)
    change.check_progress = {"claims": {"bad": {"span": old}}, "verdicts": {"bad": {"supported": False}}}

    class Client:
        edit_calls = 0

        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            self.edit_calls += 1
            view = user.split("Current section view:\n", 1)[1].split("\nClaim IDs", 1)[0]
            if old in view:
                return response(user, edits=[{"old": old, "new": new}])
            structure = user.split("Complete heading structure:\n", 1)[1].split("\nBinding operator corrections:", 1)[0]
            locator = re.search(r"View locator: (.+)", user)[1]
            assert "natively outputs images" not in structure + locator
            assert "requires an external image tool" in structure + locator
            return response(user)

    client = Client()
    result = await editing.edit_page(
        client, "Repair unsupported heading", change, [], body, [], char_budget=12000, today="2026-10-02", repair=True
    )
    assert result["body"] == body.replace(old, new) and client.edit_calls == 2


@pytest.mark.asyncio
async def test_new_repair_generation_preserves_body_bound_heading_proof_but_rechecks_every_task(cfg, store):
    from doclayer.curator.validate import check_update_scope

    old = "## Device natively generates images"
    new = "## Device uses an external rendering tool"
    original = "# Device\n" + old + "\nOperator calibration remains 123.\n### Nested\nDevice temperature is wrong.\n"
    state = store.create(JobState(job_id="", idempotency_key="", intent="Device"))[0]
    change = PageChange("g", "wiki/device.md", "update", draft_body=original)
    change.check_progress = {"claims": {"h": {"span": old}}, "verdicts": {"h": {"supported": False}}}
    state.changes = [change]

    class First:
        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            return response(user, edits=[{"old": old, "new": new}] if old in user else [])

    first = await editing.edit_page(
        First(),
        "First: incorrect heading",
        change,
        [],
        original,
        [],
        char_budget=12000,
        today="2026-10-02",
        repair=True,
    )
    change.draft_body = first["body"]
    change.draft_progress = first["draft_progress"]
    store.save(state)
    change = store.load(state.job_id).changes[0]
    original_ids = [row["id"] for row in change.draft_progress["unit_identity"]]
    second_calls = []

    class Second:
        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            second_calls.append(user)
            return response(
                user,
                edits=[{"old": "Device temperature is wrong.", "new": "Device temperature is correct."}]
                if "Device temperature is wrong." in user
                else [],
            )

    second = await editing.edit_page(
        Second(),
        "Second: different failure and evidence",
        change,
        [supported("Device temperature is correct.")],
        change.draft_body,
        [],
        char_budget=12000,
        today="2026-10-02",
        repair=True,
    )
    assert second_calls and "Device temperature is correct." in second["body"]
    assert "Operator calibration remains 123." in second["body"] and new in second["body"]
    assert not check_update_scope(
        {}, original, {}, second["body"], change.path, second["draft_progress"]["heading_renames"]
    )
    assert [row["id"] for row in second["draft_progress"]["unit_identity"]] == original_ids
    assert second["draft_progress"]["selection_hash"] != first["draft_progress"]["selection_hash"]


def test_retained_structure_drops_task_progress_and_never_rebinds_changed_body():
    import copy

    body = "# Device\n## Qualified heading\nOperator fact.\n"
    progress = {
        "editor_version": editing.EDITOR_VERSION,
        "body_hash": hashlib.sha256(body.encode()).hexdigest(),
        "unit_identity": [{"id": "original-id", "level": 1}],
        "heading_renames": [{"unit_id": "original-id", "old": "# Old", "new": "# Device"}],
        "selection_hash": "old-selection",
        "completed_task_ids": ["done"],
        "handled_claim_ids": ["old-claim"],
        "repair_views": [{"text": "old context"}],
        "repair_views_hash": "old-view-binding",
    }
    saved = copy.deepcopy(progress)
    result = editing.retain_edit_structure(progress, body)
    assert set(result) == {"editor_version", "body_hash", "unit_identity", "heading_renames"}
    result["heading_renames"][0]["new"] = "# Changed"
    assert progress == saved
    assert editing.retain_edit_structure(progress, body + "external edit") == {}
    progress["editor_version"] = -1
    assert editing.retain_edit_structure(progress, body) == {}


@pytest.mark.asyncio
async def test_public_small_page_required_cache_does_not_change_assertion_lane_on_resume(store):
    rows = [
        supported("Device setting zero is stored locally", source="a"),
        supported("Device setting one is stored locally", source="b"),
    ]
    change = PageChange(
        "g",
        "wiki/manual.md",
        "update",
        draft_body="# Manual\n" + "\n".join(r.text for r in rows),
        claim_ids=[curate.evidence_id(r) for r in rows],
    )
    state = store.create(JobState(job_id="", idempotency_key="", intent="Device"))[0]
    state.changes = [change]

    class Client:
        def __init__(self):
            self.calls = 0

        async def chat_json(self, system, user):
            self.calls += 1
            assert user.startswith("ARTICLE EDITORIAL REVIEW")
            frame = json.loads(user.split("\n", 1)[1])
            assert set(frame["required_claim_ids"]) == set(change.claim_ids)
            assert all(r.text in frame["body"] for r in rows)
            return {"complete": True, "failures": [], "conflicts": []}

    def retain(progress):
        change.check_progress = progress
        store.save(state)

    initial = Client()
    result = await curate.check_draft(initial, "Device", change, rows, [], char_budget=12000, progress=retain)
    assert not result["failures"] and initial.calls == 1
    proofs = copy.deepcopy(change.check_progress["article_review"]["results"])
    assert proofs
    restored = store.load(state.job_id).changes[0]
    resumed = Client()
    result = await curate.check_draft(resumed, "Device", restored, rows, [], char_budget=12000)
    assert not result["failures"] and resumed.calls == 0
    assert restored.check_progress["article_review"]["results"] == proofs
    assert list(restored.check_progress) == ["article_review"]


@pytest.mark.asyncio
async def test_article_review_repair_topic_carries_absence_blocker(monkeypatch):
    change = PageChange(
        "g", "wiki/device.md", "update", draft_body="# Device\nBody.\n", draft_meta={"updated": "2026-10-02"}
    )
    change.check_progress["article_review"] = {"done": True}
    seen = {}

    async def fake_edit_page(client, topic, *args, **kwargs):
        seen["topic"] = topic
        return {"body": "x", "frontmatter": {}}

    monkeypatch.setattr(editing, "edit_page", fake_edit_page)
    failures = [
        {
            "check": "absence",
            "severity": "blocker",
            "detail": "wiki/device.md: new text asserts source absence: The source does not say.",
        },
        {"check": "style", "severity": "advisory", "detail": "minor"},
    ]
    await curate.repair_draft(object(), "Device", change, failures, [], [], char_budget=12000)
    assert "Deterministic blockers to repair: " in seen["topic"]
    assert "asserts source absence" in seen["topic"]
    assert "minor" not in seen["topic"]


@pytest.mark.asyncio
async def test_accepted_disposition_anchors_are_kept_with_the_draft_progress():
    """Publication reconciliation attributes a dropped draft section only to
    claims whose accepted anchor lies literally inside it."""
    records = [supported(f"Device fact {i}.", str(i)) for i in range(3)]

    class Client:
        async def chat_json(self, system, user):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return routing_response(user)
            return response(user)

    result = await editing.edit_page(
        Client(),
        "Device",
        PageChange("g", "wiki/device.md", "update"),
        records,
        "# Device\nOperator fact.\n",
        [],
        char_budget=16000,
        today="2026-10-02",
    )
    anchors = result["draft_progress"]["claim_anchors"]
    assert anchors == {curate.evidence_id(record): record.text for record in records}
    assert all(result["body"].count(anchor) == 1 for anchor in anchors.values())
