"""Assigned facts need semantic placement and actual integration anchors."""

import ast
import copy
import hashlib
import json
import re

import pytest

from doclayer import frontmatter
from doclayer.curator import curate, editing
from doclayer.curator.model import ModelError
from doclayer.curator.state import Evidence, PageChange


def evidence(text):
    return Evidence(
        "manual",
        "manual",
        "API contract",
        "quote",
        text,
        url="https://manual.test/api",
        quote=text,
        support_status="supported",
        applicability={"provider": "ProviderZ", "edition": "enterprise"},
    )


class SemanticEditor:
    def __init__(self, records, *, outage_after=None):
        self.records = {curate.evidence_id(record): record for record in records}
        self.route_calls = 0
        self.edit_calls = 0
        self.outage_after = outage_after
        self.requests = []

    async def chat_bounded_json(self, system, user, *, char_budget):
        assert len(system) + len(user) <= char_budget
        self.requests.append(user)
        if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
            self.route_calls += 1
            frame = json.loads(user.split("\n", 1)[1].split("\nEvidence:\n", 1)[0])
            ids = re.findall(r"^- \[([^ ]+) /", user, re.M)
            assert all(self.records[identity].text in user and self.records[identity].quote in user for identity in ids)
            return {
                "routes": [
                    {
                        "id": identity,
                        "section_id": frame["root_section_id"],
                        "new_heading": "## Media API",
                        "rationale": "The source is a separate endpoint contract, not container behavior.",
                    }
                    for identity in ids
                ]
            }
        self.edit_calls += 1
        if self.outage_after is not None and self.edit_calls > self.outage_after:
            raise ModelError("inference outage", transient=True)
        ids = ast.literal_eval(user.split("Claim IDs to integrate or prove already present: ", 1)[1].split("\n", 1)[0])
        section_id = user.split("Section ID: ", 1)[1].split("\n", 1)[0]
        context = user.split("Current section view:\n", 1)[1].split("\nClaim IDs ", 1)[0]
        addition = []
        dispositions = []
        for identity in ids:
            text = self.records[identity].text
            status = "already_present" if text in context else "integrated"
            if status == "integrated":
                addition.append(text + " [Source](https://manual.test/api)")
            dispositions.append({"id": identity, "status": status, "anchor": text})
        if addition and "## Media API" not in context:
            addition.insert(0, "## Media API")
        return {
            "section_id": section_id,
            "edits": [],
            "append": "\n".join(addition),
            "handled_claim_ids": ids,
            "claim_dispositions": dispositions,
            "notes": [],
        }


def page():
    return "# Platform\nOperator introduction.\n## Containers\nContainers process images and masks.\n## Scripts\nScript runtime details.\n"


@pytest.mark.asyncio
async def test_semantic_route_adds_new_peer_preserving_operator_sections():
    record = evidence("The enterprise media endpoint requires alpha masks below 12 MB.")
    change = PageChange("g", "wiki/platform.md", "update", title="Platform", claim_ids=[curate.evidence_id(record)])
    model = SemanticEditor([record])
    result = await editing.edit_page(
        model, "Add endpoint requirements", change, [record], page(), [], char_budget=12000, today="2026-10-02"
    )
    assert result["body"].count("## Media API") == 1
    assert result["body"].index("## Media API") < result["body"].index("## Containers")
    assert record.text in result["body"]
    assert (
        "## Containers\nContainers process images and masks.\n## Scripts\nScript runtime details.\n" in result["body"]
    )
    assert model.route_calls == 1 and model.edit_calls == 1
    assert result["draft_progress"]["claim_routes"][curate.evidence_id(record)]["new_heading"] == "## Media API"


@pytest.mark.parametrize(
    "disposition",
    [
        [],
        [{"id": "claim", "status": "already_present", "anchor": "Missing fact."}],
        [{"id": "claim", "status": "integrated", "anchor": "Operator introduction."}],
        [{"id": "foreign", "status": "already_present", "anchor": "Operator introduction."}],
        [{"id": "claim", "status": "unresolved", "anchor": "Operator introduction."}],
    ],
)
def test_noop_cannot_consume_required_claim_without_actual_anchor(disposition):
    unit = editing.page_units(page())[0]
    response = {
        "section_id": unit.id,
        "edits": [],
        "append": "",
        "handled_claim_ids": ["claim"],
        "claim_dispositions": disposition,
    }
    with pytest.raises(ModelError):
        editing.apply_edits(unit, response, {"claim"}, max_output_chars=6000)


def test_noop_existing_anchor_is_allowed_but_is_not_semantic_proof():
    unit = editing.page_units(page())[0]
    response = {
        "section_id": unit.id,
        "edits": [],
        "append": "",
        "handled_claim_ids": ["claim"],
        "claim_dispositions": [{"id": "claim", "status": "already_present", "anchor": "Operator introduction."}],
    }
    assert editing.apply_edits(unit, response, {"claim"}, max_output_chars=6000) == unit.text


@pytest.mark.asyncio
async def test_route_checkpoint_survives_outage_before_edit_without_replanning():
    record = evidence("The enterprise media endpoint requires alpha masks below 12 MB.")
    change = PageChange("g", "wiki/platform.md", "update", title="Platform")
    snapshots = []

    def retain(result):
        snapshots.append(copy.deepcopy(result))
        change.draft_body = result["body"]
        change.draft_meta = result["frontmatter"]
        change.draft_progress = result["draft_progress"]

    failed = SemanticEditor([record], outage_after=0)
    with pytest.raises(ModelError, match="outage"):
        await editing.edit_page(
            failed,
            "Add endpoint requirements",
            change,
            [record],
            page(),
            [],
            char_budget=12000,
            today="2026-10-02",
            progress=retain,
        )
    assert snapshots[-1]["draft_progress"]["claim_routes"] and not snapshots[-1]["draft_progress"]["handled_claim_ids"]
    resumed = SemanticEditor([record])
    result = await editing.edit_page(
        resumed,
        "Add endpoint requirements",
        change,
        [record],
        frontmatter.build(change.draft_meta, change.draft_body),
        [],
        char_budget=12000,
        today="2026-10-02",
    )
    assert resumed.route_calls == 0 and resumed.edit_calls == 1 and record.text in result["body"]


@pytest.mark.asyncio
async def test_version_two_handled_noop_is_not_reused_but_structure_remains():
    record = evidence("The enterprise media endpoint requires alpha masks below 12 MB.")
    change = PageChange("g", "wiki/platform.md", "update", title="Platform")
    units = editing.page_units(page())
    change.draft_progress = {
        "editor_version": 2,
        "body_hash": hashlib.sha256(page().encode()).hexdigest(),
        "selection_hash": "old-version-signature",
        "handled_claim_ids": [curate.evidence_id(record)],
        "unit_identity": [{"id": unit.id, "level": unit.level} for unit in units],
        "heading_renames": [],
    }
    model = SemanticEditor([record])
    result = await editing.edit_page(
        model, "Add endpoint requirements", change, [record], page(), [], char_budget=12000, today="2026-10-02"
    )
    assert model.route_calls == 1 and model.edit_calls == 1 and record.text in result["body"]
    assert result["draft_progress"]["editor_version"] == editing.EDITOR_VERSION


@pytest.mark.asyncio
async def test_same_peer_multiple_batches_resume_preserves_identity_and_order():
    records = [evidence(f"The enterprise media endpoint supports operation {i}.") for i in range(6)]
    for record in records:
        record.quote = record.text + "\n" + "Qualified operating constraints. " * 30
    change = PageChange("g", "wiki/platform.md", "update", title="Platform")
    snapshots = []

    def retain(result):
        snapshots.append(copy.deepcopy(result))
        change.draft_body = result["body"]
        change.draft_meta = result["frontmatter"]
        change.draft_progress = result["draft_progress"]

    interrupted = SemanticEditor(records, outage_after=1)
    with pytest.raises(ModelError, match="outage"):
        await editing.edit_page(
            interrupted,
            "Add endpoint requirements",
            change,
            records,
            page(),
            [],
            char_budget=12000,
            today="2026-10-02",
            progress=retain,
        )
    assert change.draft_body.count("## Media API") == 1
    original_ids = {row["id"] for row in change.draft_progress["unit_identity"]}
    resumed = SemanticEditor(records)
    result = await editing.edit_page(
        resumed,
        "Add endpoint requirements",
        change,
        records,
        frontmatter.build(change.draft_meta, change.draft_body),
        [],
        char_budget=12000,
        today="2026-10-02",
    )
    assert resumed.route_calls == 0 and result["body"].count("## Media API") == 1
    assert all(record.text in result["body"] for record in records)
    assert original_ids <= {row["id"] for row in result["draft_progress"]["unit_identity"]}
    assert (
        "## Containers\nContainers process images and masks.\n## Scripts\nScript runtime details.\n" in result["body"]
    )


@pytest.mark.parametrize("fault", ["duplicate", "foreign", "section", "heading", "missing_heading_field"])
@pytest.mark.asyncio
async def test_invalid_route_cannot_consume_claim_or_material(fault):
    record = evidence("The media endpoint requires alpha masks.")
    identity = curate.evidence_id(record)
    change = PageChange("g", "wiki/platform.md", "update", title="Platform")
    snapshots = []

    class Invalid:
        calls = 0

        async def chat_bounded_json(self, system, user, *, char_budget):
            self.calls += 1
            frame = json.loads(user.split("\n", 1)[1].split("\nEvidence:\n", 1)[0])
            row = {"id": identity, "section_id": frame["root_section_id"], "new_heading": None, "rationale": "test"}
            if fault == "foreign":
                row["id"] = "foreign"
            elif fault == "section":
                row["section_id"] = "unknown"
            elif fault == "heading":
                row["new_heading"] = "# Replacement root"
            elif fault == "missing_heading_field":
                row.pop("new_heading")
            return {"routes": [row, row] if fault == "duplicate" else [row]}

    client = Invalid()
    with pytest.raises(ModelError, match="routing"):
        await editing.edit_page(
            client,
            "Add endpoint requirements",
            change,
            [record],
            page(),
            [],
            char_budget=12000,
            today="2026-10-02",
            progress=snapshots.append,
        )
    assert client.calls == 3 and not snapshots and not change.draft_progress


@pytest.mark.asyncio
async def test_changed_nested_page_scope_invalidates_route_and_completed_edits():
    record = evidence("The enterprise media endpoint requires alpha masks.")
    change = PageChange("g", "wiki/platform.md", "update", title="Platform")
    first = await editing.edit_page(
        SemanticEditor([record]),
        "Add endpoint requirements",
        change,
        [record],
        frontmatter.build({"applicability": {"platform": {"edition": "enterprise", "versions": [3]}}}, page()),
        [],
        char_budget=12000,
        today="2026-10-02",
    )
    change.draft_meta = first["frontmatter"]
    change.draft_body = first["body"]
    change.draft_progress = first["draft_progress"]
    changed_meta = copy.deepcopy(change.draft_meta)
    changed_meta["applicability"]["platform"]["versions"] = [4]

    class Scoped(SemanticEditor):
        async def chat_bounded_json(self, system, user, *, char_budget):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                self.route_calls += 1
                frame = json.loads(user.split("\n", 1)[1].split("\nEvidence:\n", 1)[0])
                assert frame["page_metadata"]["applicability"]["platform"]["versions"] == [4]
                section = next(row for row in frame["sections"] if row["heading_path"].endswith("Media API"))
                return {
                    "routes": [
                        {
                            "id": curate.evidence_id(record),
                            "section_id": section["section_id"],
                            "new_heading": None,
                            "rationale": "Existing endpoint section.",
                        }
                    ]
                }
            return await super().chat_bounded_json(system, user, char_budget=char_budget)

    model = Scoped([record])
    result = await editing.edit_page(
        model,
        "Add endpoint requirements",
        change,
        [record],
        frontmatter.build(changed_meta, change.draft_body),
        [],
        char_budget=12000,
        today="2026-10-02",
    )
    assert model.route_calls == 1 and result["body"] == first["body"]


@pytest.mark.asyncio
async def test_whole_support_never_clipped_to_fit_route_and_unknown_records_refused():
    record = evidence("The media endpoint requires alpha masks.")
    record.quote = "z" * 12000

    class Never:
        async def chat_bounded_json(self, *args, **kwargs):
            pytest.fail("Cannot submit clipped support")

    with pytest.raises(ModelError, match="whole required evidence"):
        await editing.edit_page(
            Never(),
            "Endpoint",
            PageChange("g", "wiki/p.md", "update"),
            [record],
            page(),
            [],
            char_budget=6000,
            today="2026-10-02",
        )
    record.support_status = "unknown"
    with pytest.raises(ModelError, match="lacks current source support"):
        await editing.edit_page(
            Never(),
            "Endpoint",
            PageChange("g", "wiki/p.md", "update"),
            [record],
            page(),
            [],
            char_budget=6000,
            today="2026-10-02",
        )


@pytest.mark.asyncio
async def test_admitted_budget_restart_retains_route_in_real_jobstore(cfg, store):
    import httpx

    from doclayer.curator.model import ModelClient
    from doclayer.curator.state import BudgetExceeded, JobState

    record = evidence("The enterprise media endpoint requires alpha masks below 12 MB.")
    identity = curate.evidence_id(record)
    state = JobState("job-editorproof123", "editor-budget-proof", "Add endpoint requirements")
    state.changes = [PageChange("g", "wiki/platform.md", "update", title="Platform")]
    store.create(state)
    requests = []

    def handler(request):
        user = json.loads(request.content)["messages"][1]["content"]
        requests.append(user)
        if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
            frame = json.loads(user.split("\n", 1)[1].split("\nEvidence:\n", 1)[0])
            payload = {
                "routes": [
                    {
                        "id": identity,
                        "section_id": frame["root_section_id"],
                        "new_heading": "## Media API",
                        "rationale": "Separate endpoint contract.",
                    }
                ]
            }
        else:
            section = user.split("Section ID: ", 1)[1].split("\n", 1)[0]
            payload = {
                "section_id": section,
                "edits": [],
                "append": "## Media API\n" + record.text,
                "handled_claim_ids": [identity],
                "claim_dispositions": [{"id": identity, "status": "integrated", "anchor": record.text}],
            }
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}, "finish_reason": "stop"}]}
        )

    def retain(result):
        change = state.changes[0]
        change.draft_body = result["body"]
        change.draft_meta = result["frontmatter"]
        change.draft_progress = result["draft_progress"]
        store.save(state)

    client = ModelClient(
        cfg,
        state.usage,
        limits={"max_model_calls": 1},
        transport=httpx.MockTransport(handler),
        admitted_call=lambda: store.save(state),
    )
    with pytest.raises(BudgetExceeded):
        await editing.edit_page(
            client,
            state.intent,
            state.changes[0],
            [record],
            page(),
            [],
            char_budget=12000,
            today="2026-10-02",
            progress=retain,
        )
    state = store.load(state.job_id)
    assert state.usage.model_calls == 1 and len(requests) == 1
    assert identity in state.changes[0].draft_progress["claim_routes"]
    assert not state.changes[0].draft_progress["handled_claim_ids"]
    client = ModelClient(
        cfg,
        state.usage,
        limits={"max_model_calls": 2},
        transport=httpx.MockTransport(handler),
        admitted_call=lambda: store.save(state),
    )
    change = state.changes[0]
    result = await editing.edit_page(
        client,
        state.intent,
        change,
        [record],
        frontmatter.build(change.draft_meta, change.draft_body),
        [],
        char_budget=12000,
        today="2026-10-02",
        progress=retain,
    )
    assert len(requests) == 2 and not requests[-1].startswith("BOUNDED EDIT CLAIM ROUTING")
    assert store.load(state.job_id).usage.model_calls == 2
    assert record.text in result["body"] and result["body"].count("## Media API") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actual_meta",
    [
        {},
        {
            "title": "Current platform",
            "subject_type": "deployment",
            "applicability": {"provider": "ProviderCurrent", "edition": "community", "versions": [7]},
        },
    ],
)
async def test_routing_uses_actual_page_metadata_not_stale_change_metadata(actual_meta):
    record = evidence("The enterprise media endpoint requires alpha masks.")
    change = PageChange(
        "g",
        "wiki/platform.md",
        "update",
        title="Platform",
        draft_meta={"applicability": {"provider": "StaleProvider", "edition": "enterprise", "versions": [3]}},
    )

    class Inspect(SemanticEditor):
        async def chat_bounded_json(self, system, user, *, char_budget):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                frame = json.loads(user.split("\n", 1)[1].split("\nEvidence:\n", 1)[0])
                assert all(frame["page_metadata"].get(key) == value for key, value in actual_meta.items())
                if not actual_meta:
                    assert "applicability" not in frame["page_metadata"]
                assert "StaleProvider" not in user
            return await super().chat_bounded_json(system, user, char_budget=char_budget)

    await editing.edit_page(
        Inspect([record]),
        "Add endpoint requirements",
        change,
        [record],
        frontmatter.build(actual_meta, page()),
        [],
        char_budget=12000,
        today="2026-10-02",
    )


@pytest.mark.asyncio
async def test_actual_yaml_dates_routing_binding_is_stable_and_caller_unmodified():
    import datetime

    record = evidence("The media endpoint requires alpha masks.")
    change = PageChange("g", "wiki/platform.md", "update", title="Platform")
    meta = {
        "published": datetime.date(2026, 9, 2),
        "applicability": {"release": {"date": datetime.date(2026, 9, 1), "edition": "enterprise"}},
    }
    original = copy.deepcopy(meta)

    class Dated(SemanticEditor):
        async def chat_bounded_json(self, system, user, *, char_budget):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                frame = json.loads(user.split("\n", 1)[1].split("\nEvidence:\n", 1)[0])
                assert frame["page_metadata"]["published"] == "2026-09-02"
                assert frame["page_metadata"]["applicability"]["release"]["date"] == "2026-09-01"
            return await super().chat_bounded_json(system, user, char_budget=char_budget)

    first = await editing.edit_page(
        Dated([record]),
        "Endpoint",
        change,
        [record],
        frontmatter.build(meta, page()),
        [],
        char_budget=12000,
        today="2026-10-02",
    )
    change.draft_meta = first["frontmatter"]
    change.draft_body = first["body"]
    change.draft_progress = first["draft_progress"]
    resumed = Dated([record])
    result = await editing.edit_page(
        resumed,
        "Endpoint",
        change,
        [record],
        frontmatter.build(change.draft_meta, change.draft_body),
        [],
        char_budget=12000,
        today="2026-10-02",
    )
    assert resumed.route_calls == resumed.edit_calls == 0 and result["body"] == first["body"]
    assert meta == original and isinstance(meta["published"], datetime.date)


@pytest.mark.asyncio
async def test_unassigned_repair_region_receives_witness_without_new_obligations():
    record = evidence("The Model Z API analyzes images and returns text only.")
    identity = curate.evidence_id(record)
    original = "# Model Z\nOperator notes.\n## Specifications\nTechnical specifications.\n## Vision\nThe Model Z API natively generates images.\nKeep this operator paragraph.\n"
    change = PageChange("g", "wiki/platform.md", "update", title="Model Z")

    class Witness:
        vision_checked = False

        async def chat_bounded_json(self, system, user, *, char_budget):
            assert len(system) + len(user) <= char_budget
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                frame = json.loads(user.split("\n", 1)[1].split("\nEvidence:\n", 1)[0])
                section = next(row for row in frame["sections"] if row["heading_path"].endswith("Specifications"))
                return {
                    "routes": [
                        {
                            "id": identity,
                            "section_id": section["section_id"],
                            "new_heading": None,
                            "rationale": "API specifications.",
                        }
                    ]
                }
            section = user.split("Section ID: ", 1)[1].split("\n", 1)[0]
            ids = ast.literal_eval(
                user.split("Claim IDs to integrate or prove already present: ", 1)[1].split("\n", 1)[0]
            )
            if "View locator: Model Z / Vision" in user:
                self.vision_checked = True
                assert not ids and record.text in user and record.quote in user
                assert "support-only for correcting existing assertions" in user
                return {
                    "section_id": section,
                    "edits": [{"old": "The Model Z API natively generates images.", "new": record.text}],
                    "append": "",
                    "handled_claim_ids": [],
                    "claim_dispositions": [],
                }
            return {
                "section_id": section,
                "edits": [],
                "append": record.text if ids else "",
                "handled_claim_ids": ids,
                "claim_dispositions": [{"id": i, "status": "integrated", "anchor": record.text} for i in ids],
            }

    model = Witness()
    result = await editing.edit_page(
        model,
        "Correct existing source applicability",
        change,
        [record],
        original,
        [],
        char_budget=12000,
        today="2026-10-02",
        repair=True,
    )
    assert model.vision_checked and "natively generates images" not in result["body"]
    assert "Keep this operator paragraph." in result["body"]
    assert result["draft_progress"]["handled_claim_ids"] == [identity]


@pytest.mark.asyncio
async def test_route_repair_identifies_required_markdown_heading_prefix():
    record = evidence("The media endpoint requires alpha masks.")

    class PlainTitle(SemanticEditor):
        attempts = 0

        async def chat_bounded_json(self, system, user, *, char_budget):
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                self.attempts += 1
                assert "COMPLETE Markdown heading line" in system and "'## '" in system
                if self.attempts == 1:
                    frame = json.loads(user.split("\n", 1)[1].split("\nEvidence:\n", 1)[0])
                    return {
                        "routes": [
                            {
                                "id": curate.evidence_id(record),
                                "section_id": frame["root_section_id"],
                                "new_heading": "Media API",
                                "rationale": "Separate contract.",
                            }
                        ]
                    }
                assert "complete Markdown heading beginning '## '" in user and "Plain titles are invalid" in user
            return await super().chat_bounded_json(system, user, char_budget=char_budget)

    model = PlainTitle([record])
    result = await editing.edit_page(
        model,
        "Endpoint",
        PageChange("g", "wiki/platform.md", "update"),
        [record],
        page(),
        [],
        char_budget=12000,
        today="2026-10-02",
    )
    assert model.attempts == 2 and result["body"].count("## Media API") == 1
