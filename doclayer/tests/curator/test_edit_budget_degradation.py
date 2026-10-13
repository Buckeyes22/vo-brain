"""An over-budget bounded edit task degrades instead of failing the job.

Shape of job-32623087f9 (GPT-6 guide import): one section's edit task packed
expected claims/support evidence, the section view, article-review repair rows
and canonical retention contexts, and failed non-transiently at validate.
"""

import ast
import hashlib
import json
import re

import pytest

from doclayer.curator import curate, editing
from doclayer.curator.model import ModelError
from doclayer.curator.state import Evidence, PageChange

RESERVE = 1000  # actionable repair-feedback reserve every edit task keeps


def record(text, locator="API contract"):
    return Evidence(
        "manual",
        "manual",
        locator,
        "quote",
        text,
        url="https://manual.test/api",
        quote=text,
        support_status="supported",
        applicability={"provider": "ProviderZ"},
    )


def claim_page():
    return "# Platform\nOperator introduction.\n## Containers\nContainers process images and masks.\n## Scripts\nScript runtime details.\n"


class ClaimEditor:
    """Routes every claim to Containers and integrates each listed claim exactly."""

    def __init__(self, records):
        self.records = {curate.evidence_id(item): item for item in records}
        self.prompts = []
        self.edit_claims = []

    async def chat_bounded_json(self, system, user, *, char_budget):
        assert len(system) + len(user) <= char_budget
        self.prompts.append(system + "\x00" + user)
        if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
            frame = json.loads(user.split("\n", 1)[1].split("\nEvidence:\n", 1)[0])
            section = next(row["section_id"] for row in frame["sections"] if row["heading_path"].endswith("Containers"))
            ids = re.findall(r"^- \[([^ ]+) /", user, re.M)
            return {
                "routes": [
                    {"id": identity, "section_id": section, "new_heading": None, "rationale": "Container behaviour."}
                    for identity in ids
                ]
            }
        assert len(system) + len(user) + RESERVE <= char_budget
        ids = ast.literal_eval(user.split("Claim IDs to integrate or prove already present: ", 1)[1].split("\n", 1)[0])
        self.edit_claims.append(ids)
        # Each task carries only its own claims' evidence records.
        evidence = user.split("Selected supported evidence:\n", 1)[1].split("\nEvidence role:", 1)[0]
        assert sorted(re.findall(r"^- \[([^ ]+) /", evidence, re.M)) == sorted(ids)
        section_id = user.split("Section ID: ", 1)[1].split("\n", 1)[0]
        context = user.split("Current section view:\n", 1)[1].split("\nClaim IDs ", 1)[0]
        additions, dispositions = [], []
        for identity in ids:
            text = self.records[identity].text
            status = "already_present" if text in context else "integrated"
            if status == "integrated":
                additions.append(text + " [Source](https://manual.test/api)")
            dispositions.append({"id": identity, "status": status, "anchor": text})
        return {
            "section_id": section_id,
            "edits": [],
            "append": "\n".join(additions),
            "handled_claim_ids": ids,
            "claim_dispositions": dispositions,
            "notes": [],
        }


def review_for(body, rows, namespace_extra=None):
    sections = []
    offset = 0
    for unit in editing.page_units(body):
        sections.append({"id": unit.id, "label": unit.label, "start": offset, "end": offset + len(unit.text)})
        offset += len(unit.text)
    review = {
        "editorial_version": 1,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "sections": sections,
        "failures": [
            {
                "severity": "blocker",
                "check": check,
                "detail": detail,
                "section_id": sections[index]["id"],
                "anchor": anchor,
            }
            for index, anchor, detail, check in rows
        ],
    }
    review.update(namespace_extra or {})
    return review


def preservation_change(body, original, rows, article_rows=()):
    preservation = review_for(
        body,
        rows,
        {
            "original_body": original,
            "original_meta": {"title": "Lineup", "tags": ["models"]},
            "canonical_binding": {"commit": "c" * 40, "blob": "b" * 40, "path": "wiki/lineup.md"},
        },
    )
    progress = {"existing_preservation": preservation}
    if article_rows:
        progress["article_review"] = review_for(body, article_rows)
    return PageChange("g", "wiki/lineup.md", "update", draft_body=body, check_progress=progress)


class RepairEditor:
    def __init__(self, replacements=()):
        self.replacements = dict(replacements)
        self.prompts = []

    async def chat_json(self, system, user):
        self.prompts.append(system + "\x00" + user)
        context = user.split("Current section view:\n", 1)[1].split("\nClaim IDs ", 1)[0]
        edits = [{"old": old, "new": new} for old, new in self.replacements.items() if context.count(old) == 1]
        return {
            "section_id": re.search(r"Section ID: (.+)", user)[1],
            "edits": edits,
            "append": "",
            "handled_claim_ids": [],
            "claim_dispositions": [],
            "notes": [],
        }


def lineup(intro, children):
    return "# Lineup\n" + intro + "\n" + "".join(f"## Model {index}\n{text}\n" for index, text in enumerate(children))


def digest(prompts):
    return hashlib.sha256("\x01".join(prompts).encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Under budget: the prompt is byte-identical to the pre-degradation editor.
# --------------------------------------------------------------------------- #

# Re-pinned 2026-10-10: the only change is the stated reply bound, now the output token target x chars_per_token (10240, was 6000).
UNDER_BUDGET_CLAIMS_DIGEST = "2e7db895c6de868301a430235733a3d70ea18ac93a435228de7b45f8667ca293"
UNDER_BUDGET_REPAIR_DIGEST = "c37ef17c1d945657f0e01c1f8e500c79615405c5f68a65ceb2ae4169a5baf8f5"


@pytest.mark.asyncio
async def test_under_budget_claim_prompts_are_byte_identical():
    records = [
        record(f"The enterprise media endpoint {index} requires alpha masks below {index + 10} MB.")
        for index in range(3)
    ]
    change = PageChange(
        "g", "wiki/platform.md", "update", title="Platform", claim_ids=[curate.evidence_id(r) for r in records]
    )
    model = ClaimEditor(records)
    result = await editing.edit_page(
        model, "Add endpoint requirements", change, records, claim_page(), [], char_budget=12000, today="2026-10-02"
    )
    assert all(r.text in result["body"] for r in records)
    assert len(model.edit_claims) == 1
    assert digest(model.prompts) == UNDER_BUDGET_CLAIMS_DIGEST


@pytest.mark.asyncio
async def test_under_budget_article_repair_prompts_are_byte_identical():
    original = lineup(
        "All models accept text and image input. Client SDKs are available.", ["Astra is the largest.", "Luna is fast."]
    )
    body = original.replace(" Client SDKs are available.", "")
    change = preservation_change(
        body,
        original,
        [
            (
                0,
                "All models accept text and image input.",
                "Original intro stated Client SDKs availability; restore it.",
                "preservation",
            )
        ],
    )
    support = record("Latest models are available via the Responses API and Client SDKs.", "Overview")
    model = RepairEditor(
        {
            "All models accept text and image input.": "All models accept text and image input. Client SDKs are available."
        }
    )
    repaired = await editing.edit_page(
        model, "Lineup", change, [support], body, [], char_budget=12000, today="2026-10-03", repair=True
    )
    assert repaired["body"] == original
    assert len(model.prompts) == 1
    assert digest(model.prompts) == UNDER_BUDGET_REPAIR_DIGEST


# --------------------------------------------------------------------------- #
# Over budget: degrade in order, fail only at a single claim's minimum.
# --------------------------------------------------------------------------- #


def claim_records(count, size=520):
    return [
        record(f"Media endpoint {index} contract: " + " ".join(f"w{index}x{word}" for word in range(size // 7)))
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_over_budget_claim_batch_is_split_and_every_claim_integrated():
    records = claim_records(3)
    change = PageChange(
        "g", "wiki/platform.md", "update", title="Platform", claim_ids=[curate.evidence_id(r) for r in records]
    )
    model = ClaimEditor(records)
    topic = "Add endpoint requirements. " + "Keep provider qualifications. " * 170
    result = await editing.edit_page(
        model, topic, change, records, claim_page(), [], char_budget=12000, today="2026-10-02"
    )
    assert len(model.edit_claims) >= 2 and all(len(ids) < 3 for ids in model.edit_claims)
    assert sorted(i for ids in model.edit_claims for i in ids) == sorted(curate.evidence_id(r) for r in records)
    assert all(r.text in result["body"] for r in records)
    assert "Operator introduction." in result["body"] and "Script runtime details." in result["body"]
    assert sorted(result["draft_progress"]["handled_claim_ids"]) == sorted(curate.evidence_id(r) for r in records)


@pytest.mark.asyncio
async def test_over_budget_single_claim_narrows_view_with_partial_anchor_rule():
    records = claim_records(1, 400)
    long_section = "".join(f"Container rule {index} keeps masks aligned for tenant {index}.\n" for index in range(70))
    body = (
        "# Platform\nOperator introduction.\n## Containers\n" + long_section + "## Scripts\nScript runtime details.\n"
    )
    change = PageChange("g", "wiki/platform.md", "update", title="Platform", claim_ids=[curate.evidence_id(records[0])])
    model = ClaimEditor(records)
    topic = "Add endpoint requirements. " + "Keep provider qualifications. " * 170
    result = await editing.edit_page(model, topic, change, records, body, [], char_budget=12000, today="2026-10-02")
    edits = [p for p in model.prompts if "Current section view:" in p]
    shown = edits[0].split("Current section view:\n", 1)[1].split("\nClaim IDs ", 1)[0]
    assert (
        len(edits) == 1 and len(shown) <= 1500 and shown in "## Containers\n" + long_section
    )  # planned view was 3000 characters
    assert "This view is partial" not in edits[0] and "unique within the current section view" in edits[0]
    assert records[0].text in result["body"] and long_section in result["body"]


@pytest.mark.asyncio
async def test_over_budget_root_preservation_repair_keeps_only_own_retention_context():
    """Job-32623087f9: a root-section preservation repair carried the whole canonical subtree."""
    children = [" ".join(f"Model {index} fact {word}." for word in range(110)) for index in range(6)]
    original = lineup("All models accept text and image input. Client SDKs are available.", children)
    body = original.replace(" Client SDKs are available.", "")
    change = preservation_change(
        body,
        original,
        [
            (
                0,
                "All models accept text and image input.",
                "Original intro stated Client SDKs availability; restore it.",
                "preservation",
            )
        ],
    )
    model = RepairEditor(
        {
            "All models accept text and image input.": "All models accept text and image input. Client SDKs are available."
        }
    )
    budget = 9000
    repaired = await editing.edit_page(
        model, "Lineup", change, [], body, [], char_budget=budget, today="2026-10-03", repair=True
    )
    assert repaired["body"] == original and len(model.prompts) == 1
    user = model.prompts[0].split("\x00", 1)[1]
    assert len(model.prompts[0]) - 1 + RESERVE <= budget
    context = json.loads(user.split("Canonical retention context: ", 1)[1].split("\n", 1)[0])["existing_preservation"]
    root = editing.page_units(original)[0]
    assert context["selected_original_units"] == [{"id": root.id, "label": root.label, "text": root.text}]
    assert context["original_body_sha256"] == hashlib.sha256(original.encode()).hexdigest()


@pytest.mark.asyncio
async def test_over_budget_article_rows_are_split_and_all_repaired():
    original = lineup("Alpha is documented. Beta is documented.", ["Astra."])
    body = original.replace("Alpha is documented.", "Alpha.").replace("Beta is documented.", "Beta.")
    detail = "Restore the documented qualification exactly. " * 45
    change = preservation_change(
        body,
        original,
        [(0, "Alpha.", "Alpha: " + detail, "preservation"), (0, "Beta.", "Beta: " + detail, "preservation")],
    )
    model = RepairEditor({"Alpha.": "Alpha is documented.", "Beta.": "Beta is documented."})
    budget = 7400
    repaired = await editing.edit_page(
        model, "Lineup", change, [], body, [], char_budget=budget, today="2026-10-03", repair=True
    )
    assert len(model.prompts) == 2 and all(len(p) - 1 + RESERVE <= budget for p in model.prompts)
    assert "Alpha: " in model.prompts[0] and "Beta: " not in model.prompts[0]
    assert "Beta: " in model.prompts[1] and "Alpha: " not in model.prompts[1]
    assert repaired["body"] == original


@pytest.mark.asyncio
async def test_single_claim_still_over_budget_names_claim_and_sizes():
    records = [record("Media endpoint contract: " + "qualified detail " * 118)]
    identity = curate.evidence_id(records[0])
    change = PageChange("g", "wiki/platform.md", "update", title="Platform", claim_ids=[identity])

    class RouteOnly(ClaimEditor):
        async def chat_bounded_json(self, system, user, *, char_budget):
            if not user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                pytest.fail("an over-budget edit task cannot invoke inference")
            return await super().chat_bounded_json(system, user, char_budget=char_budget)

    topic = "Add endpoint requirements. " + "Keep provider qualifications. " * 150
    with pytest.raises(ModelError) as raised:
        await editing.edit_page(
            RouteOnly(records), topic, change, records, claim_page(), [], char_budget=12000, today="2026-10-02"
        )
    message = str(raised.value)
    assert not raised.value.transient
    assert message.startswith("bounded edit task exceeds input context including actionable repair reserve")
    assert f"claim {identity}" in message and "budget 12000" in message
    assert (
        re.search(r"needs \d+ characters", message)
        and re.search(r"evidence \d+", message)
        and re.search(r"view \d+", message)
    )


# --------------------------------------------------------------------------- #
# Audit: routing previews, single large records, page structure, anchors.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_routing_narrows_section_previews_before_refusing_claims():
    sections = "".join(f"## Topic {index}\n" + f"Topic {index} operational detail. " * 12 + "\n" for index in range(40))
    body = "# Platform\nOperator introduction.\n" + sections + "## Containers\nContainers process images and masks.\n"
    records = claim_records(1, 200)
    change = PageChange("g", "wiki/platform.md", "update", title="Platform", claim_ids=[curate.evidence_id(records[0])])
    model = ClaimEditor(records)
    result = await editing.edit_page(
        model, "Add endpoint requirements", change, records, body, [], char_budget=12000, today="2026-10-02"
    )
    route = next(p for p in model.prompts if "BOUNDED EDIT CLAIM ROUTING" in p).split("\x00", 1)[1]
    frame = json.loads(route.split("\n", 1)[1].split("\nEvidence:\n", 1)[0])
    assert len(frame["sections"]) == len(editing.page_units(body))  # every section ID stays listed
    assert max(len(row["preview"]) for row in frame["sections"]) < 200
    assert records[0].text in result["body"]


@pytest.mark.asyncio
async def test_single_record_too_large_beside_full_view_runs_alone_at_minimal_view():
    records = claim_records(1, 1350)
    long_section = "".join(f"Container rule {index} keeps masks aligned for tenant {index}.\n" for index in range(70))
    body = (
        "# Platform\nOperator introduction.\n## Containers\n" + long_section + "## Scripts\nScript runtime details.\n"
    )
    change = PageChange("g", "wiki/platform.md", "update", title="Platform", claim_ids=[curate.evidence_id(records[0])])
    assert len(records[0].text) <= 1500  # one whole literal disposition anchor
    model = ClaimEditor(records)
    result = await editing.edit_page(
        model, "Add endpoint requirements", change, records, body, [], char_budget=9000, today="2026-10-02"
    )
    edits = [p for p in model.prompts if "Current section view:" in p]
    shown = edits[0].split("Current section view:\n", 1)[1].split("\nClaim IDs ", 1)[0]
    assert len(edits) == 1 and len(shown) <= editing.MIN_VIEW_BUDGET
    assert records[0].text in result["body"] and long_section in result["body"]


@pytest.mark.asyncio
async def test_page_structure_over_limit_is_shown_locally_per_task():
    groups = "".join(
        f"## Group {group}\n"
        + "".join(
            f"### {'Containers' if (group, item) == (3, 4) else f'Item {group}.{item}'}\nItem detail {group}.{item}.\n"
            for item in range(10)
        )
        for group in range(8)
    )
    body = "# Platform\nOperator introduction.\n" + groups
    records = claim_records(1, 200)
    change = PageChange("g", "wiki/platform.md", "update", title="Platform", claim_ids=[curate.evidence_id(records[0])])
    model = ClaimEditor(records)
    result = await editing.edit_page(
        model, "Add endpoint requirements", change, records, body, [], char_budget=16000, today="2026-10-02"
    )
    edit = [p for p in model.prompts if "Current section view:" in p][0]
    structure = edit.split("Local (ancestors, siblings and subsections of this section) heading structure:\n", 1)[
        1
    ].split("\nBinding", 1)[0]
    assert "Group 3 / Containers" in structure and "Group 3 / Item 3.9" in structure
    assert "Group 5 / Item 5.1" not in structure and "other sections not shown" in structure
    assert records[0].text in result["body"]


@pytest.mark.asyncio
async def test_article_anchor_across_a_view_boundary_is_repaired_not_refused():
    lines = [f"Retention clause {index} keeps operator data for {index} days.\n" for index in range(70)]
    body = "# Device\n## Retention\n" + "".join(lines)
    retention = editing.page_units(body)[1]
    chunks = [text for _, _, text in editing.views([retention], 3000)]
    assert len(chunks) == 2
    boundary = next(line for line in lines if chunks[0].endswith(line))
    following = lines[lines.index(boundary) + 1]
    anchor = boundary + following
    assert all(anchor not in chunk for chunk in chunks)
    original = body.replace(following, following.replace("days", "calendar days"))
    change = preservation_change(
        body, original, [(1, anchor, "Restore the calendar-day qualification.", "preservation")]
    )
    model = RepairEditor({following: following.replace("days", "calendar days")})
    repaired = await editing.edit_page(
        model, "Device", change, [], body, [], char_budget=12000, today="2026-10-03", repair=True
    )
    assert repaired["body"] == original
    assert len(model.prompts) == 1  # the row is sent once, with a view that holds the whole anchor
    assert anchor in model.prompts[0].split("Current section view:\n", 1)[1].split("\nClaim IDs ", 1)[0]


@pytest.mark.asyncio
async def test_short_page_draft_with_one_oversized_record_uses_bounded_editing():
    records = claim_records(1, 1420)
    assert len(records[0].text) <= 1500
    assert curate.pack_evidence(records, max(1000, 8400 // 2 - 1500))[1]  # beyond the short-page batch bound
    change = PageChange("g", "wiki/platform.md", "update", title="Platform", claim_ids=[curate.evidence_id(records[0])])
    model = ClaimEditor(records)
    result = await curate.draft_page(
        model, "Add endpoint requirements", change, records, claim_page(), [], char_budget=8400, today="2026-10-02"
    )
    assert records[0].text in result["body"] and "Operator introduction." in result["body"]
    assert len([p for p in model.prompts if "Current section view:" in p]) == 1


# --------------------------------------------------------------------------- #
# Narrowed views of large pages, split batches and anchors.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_root_task_on_a_181_section_page_shows_only_direct_children():
    groups = "".join(
        f"## Group {g} covering the extended configuration area {g}\nGroup {g} summary.\n"
        + "".join(
            f"### Item {g}.{i} with an extended descriptive heading for {g}.{i}\nItem detail {g}.{i}.\n"
            for i in range(8)
        )
        for g in range(20)
    )
    original = "# Lineup\nAll models accept text input. Client SDKs are available.\n" + groups
    body = original.replace(" Client SDKs are available.", "")
    units = editing.page_units(body)
    assert len(units) == 181
    budget = 32768
    assert len("\n".join(f"{u.id}: {u.label}" for u in units)) > max(500, budget // 5)
    change = preservation_change(
        body, original, [(0, "All models accept text input.", "Restore Client SDKs.", "preservation")]
    )
    model = RepairEditor({"All models accept text input.": "All models accept text input. Client SDKs are available."})
    repaired = await editing.edit_page(
        model, "Lineup", change, [], body, [], char_budget=budget, today="2026-10-06", repair=True
    )
    assert repaired["body"] == original and len(model.prompts) == 1
    structure = model.prompts[0].split("heading structure:\n", 1)[1].split("\nBinding", 1)[0]
    assert model.prompts[0].split("\x00", 1)[1].count("Local (") == 1
    assert structure.count("\n") == 1 + 20  # root, its 20 direct children, the count line
    assert structure.endswith(f"160 of them deeper subsections of {units[0].id}")


@pytest.mark.asyncio
async def test_interrupted_split_resumes_without_restoring_finished_rows_twice():
    original = lineup("Alpha is documented. Beta is documented.", ["Astra."])
    body = original.replace("Alpha is documented.", "Alpha.").replace("Beta is documented.", "Beta.")
    detail = "Restore the documented qualification exactly. " * 45
    change = preservation_change(
        body,
        original,
        [(0, "Alpha.", "Alpha: " + detail, "preservation"), (0, "Beta.", "Beta: " + detail, "preservation")],
    )

    def retain(value):
        change.draft_body = value["body"]
        change.draft_progress = value["draft_progress"]

    class Restorer:
        def __init__(self, fail_on=None):
            self.prompts = []
            self.fail_on = fail_on

        async def chat_json(self, system, user):
            self.prompts.append(user)
            rows = json.loads(user.split("Article review repairs for this exact section: ", 1)[1].split("\n", 1)[0])
            names = [row["detail"].split(":", 1)[0] for row in rows]
            if self.fail_on in names:
                raise ModelError("transport unavailable", transient=True)
            return {
                "section_id": re.search(r"Section ID: (.+)", user)[1],
                "edits": [],
                "append": " ".join(f"Restored {name}." for name in names),
                "handled_claim_ids": [],
                "claim_dispositions": [],
                "notes": [],
            }

    first = Restorer(fail_on="Beta")
    with pytest.raises(ModelError, match="transport unavailable"):
        await editing.edit_page(
            first, "Lineup", change, [], body, [], char_budget=7400, today="2026-10-06", repair=True, progress=retain
        )
    assert "Restored Alpha." in change.draft_body and change.draft_progress["split_task_ids"]
    resumed = Restorer()
    final = await editing.edit_page(
        resumed,
        "Lineup",
        change,
        [],
        change.draft_body,
        [],
        char_budget=20000,
        today="2026-10-06",
        repair=True,
        progress=retain,
    )  # the whole task now fits
    assert len(resumed.prompts) == 1 and "Beta: " in resumed.prompts[0] and "Alpha: " not in resumed.prompts[0]
    assert final["body"].count("Restored Alpha.") == 1 and final["body"].count("Restored Beta.") == 1


def test_narrowed_view_keeps_an_anchor_that_straddles_its_chunk_boundary():
    unit = editing.page_units(
        "# Device\n## Retention\n"
        + "".join(f"Retention clause {index} keeps operator data for {index} days.\n" for index in range(40))
    )[1]
    chunks = [text for _, _, text in editing.views([unit], editing.MIN_VIEW_BUDGET)]
    boundary = chunks[0].splitlines(keepends=True)[-1]
    anchor = boundary + chunks[1].splitlines(keepends=True)[0]
    assert all(anchor not in chunk for chunk in chunks)
    _, _, view = editing._narrow_view(unit, unit.text, editing.MIN_VIEW_BUDGET, tokens=[anchor])
    assert anchor in view and unit.text.count(view) == 1 and len(view) <= editing.MIN_VIEW_BUDGET


@pytest.mark.asyncio
async def test_wide_record_does_not_flush_the_batch_in_progress():
    small = [record(f"Small media contract {index}: masks under {index + 5} MB.") for index in range(2)]
    wide = claim_records(1, 1350)[0]
    records = [small[0], wide, small[1]]
    change = PageChange(
        "g", "wiki/platform.md", "update", title="Platform", claim_ids=[curate.evidence_id(r) for r in records]
    )
    model = ClaimEditor(records)
    result = await editing.edit_page(
        model, "Add endpoint requirements", change, records, claim_page(), [], char_budget=9000, today="2026-10-02"
    )
    assert sorted(map(sorted, model.edit_claims)) == sorted(
        [sorted(curate.evidence_id(r) for r in small), [curate.evidence_id(wide)]]
    )
    assert all(r.text in result["body"] for r in records)


class HeadingEditor(ClaimEditor):
    """Routes every claim to a new root-owned peer section."""

    async def chat_bounded_json(self, system, user, *, char_budget):
        if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
            self.prompts.append(system + "\x00" + user)
            frame = json.loads(user.split("\n", 1)[1].split("\nEvidence:\n", 1)[0])
            ids = re.findall(r"^- \[([^ ]+) /", user, re.M)
            return {
                "routes": [
                    {
                        "id": identity,
                        "section_id": frame["root_section_id"],
                        "new_heading": "## Media API",
                        "rationale": "Separate endpoint contract.",
                    }
                    for identity in ids
                ]
            }
        reply = await super().chat_bounded_json(system, user, char_budget=char_budget)
        context = user.split("Current section view:\n", 1)[1].split("\nClaim IDs ", 1)[0]
        if reply["append"] and "## Media API" not in context:
            reply["append"] = "## Media API\n" + reply["append"]
        return reply


@pytest.mark.asyncio
async def test_planned_heading_is_added_once_across_split_claim_parts():
    records = claim_records(3)
    change = PageChange(
        "g", "wiki/platform.md", "update", title="Platform", claim_ids=[curate.evidence_id(r) for r in records]
    )
    model = HeadingEditor(records)
    topic = "Add endpoint requirements. " + "Keep provider qualifications. " * 170
    result = await editing.edit_page(
        model, topic, change, records, claim_page(), [], char_budget=12000, today="2026-10-02"
    )
    assert len(model.edit_claims) >= 2
    assert result["body"].count("## Media API") == 1
    heading = result["body"].index("## Media API")
    assert all(result["body"].index(r.text) > heading for r in records)


@pytest.mark.asyncio
async def test_later_part_cannot_anchor_on_an_earlier_append_outside_its_view():
    records = [
        record("Quokka gateway uploads: " + " ".join(f"q{i}z" for i in range(120))),
        record("Containers expose probes: " + " ".join(f"p{i}y" for i in range(120))),
    ]
    long_section = "".join(f"Container rule {index} keeps masks aligned for tenant {index}.\n" for index in range(70))
    body = (
        "# Platform\nOperator introduction.\n## Containers\n" + long_section + "## Scripts\nScript runtime details.\n"
    )
    change = PageChange(
        "g", "wiki/platform.md", "update", title="Platform", claim_ids=[curate.evidence_id(r) for r in records]
    )
    first_text = None
    attempts = []

    class Duplicating(ClaimEditor):
        async def chat_bounded_json(self, system, user, *, char_budget):
            nonlocal first_text
            reply = await super().chat_bounded_json(system, user, char_budget=char_budget)
            if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
                return reply
            context = user.split("Current section view:\n", 1)[1].split("\nClaim IDs ", 1)[0]
            attempts.append(reply["handled_claim_ids"])
            if first_text is None:
                first_text = self.records[reply["handled_claim_ids"][0]].text
            elif first_text not in context and len(attempts) == 2:
                # Re-append the earlier part's fact (outside this view) as this claim's anchor.
                reply["append"] += "\n" + first_text
                reply["claim_dispositions"][0]["anchor"] = first_text
            return reply

    model = Duplicating(records)
    topic = "Add endpoint requirements. " + "Keep provider qualifications. " * 140  # split, narrowed views
    result = await editing.edit_page(model, topic, change, records, body, [], char_budget=12000, today="2026-10-02")
    assert len(attempts) == 3 and attempts[1] == attempts[2]  # the duplicate anchor was rejected and retried
    assert all(result["body"].count(r.text) == 1 for r in records)


# --------------------------------------------------------------------------- #
# Section/claim identity mismatch (job-04223e4edf).
# --------------------------------------------------------------------------- #


class Scripted(ClaimEditor):
    """ClaimEditor whose n-th edit reply is altered by `alter(n, reply, ids)`."""

    def __init__(self, records, alter):
        super().__init__(records)
        self.alter = alter
        self.edit_prompts = []

    async def chat_bounded_json(self, system, user, *, char_budget):
        reply = await super().chat_bounded_json(system, user, char_budget=char_budget)
        if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
            return reply
        self.edit_prompts.append(user)
        return self.alter(len(self.edit_prompts), reply, self.records)


async def identity_run(alter, count=3, progress=None, change=None):
    records = [record(f"Media contract {index}: masks under {index + 5} MB.") for index in range(count)]
    change = change or PageChange(
        "g", "wiki/platform.md", "update", title="Platform", claim_ids=[curate.evidence_id(r) for r in records]
    )
    model = Scripted(records, alter)
    result = await editing.edit_page(
        model,
        "Add endpoint requirements",
        change,
        records,
        claim_page(),
        [],
        char_budget=12000,
        today="2026-10-06",
        progress=progress,
    )
    return model, result, records


@pytest.mark.asyncio
async def test_duplicate_handled_claim_ids_are_accepted():
    def alter(n, reply, records):
        reply["handled_claim_ids"] = reply["handled_claim_ids"] * 2
        return reply

    model, result, records = await identity_run(alter)
    assert len(model.edit_prompts) == 1 and all(r.text in result["body"] for r in records)
    assert sorted(result["draft_progress"]["handled_claim_ids"]) == sorted(curate.evidence_id(r) for r in records)


@pytest.mark.asyncio
async def test_wrong_section_and_unexpected_ids_fail_with_precise_feedback():
    def alter(n, reply, records):
        if n == 1:
            reply = {
                **reply,
                "section_id": "section-wrong",
                "handled_claim_ids": reply["handled_claim_ids"] + ["claim-foreign"],
            }
        return reply

    model, result, records = await identity_run(alter)
    assert len(model.edit_prompts) == 2
    feedback = model.edit_prompts[1].split("Previous edit failed: ", 1)[1]
    section = re.search(r"Section ID: (.+)", model.edit_prompts[0])[1]
    assert f"section_id must be {section!r}, got 'section-wrong'" in feedback
    assert "unexpected claim IDs ['claim-foreign']" in feedback
    assert all(r.text in result["body"] for r in records)


@pytest.mark.asyncio
async def test_no_handled_ids_names_every_missing_id():
    def alter(n, reply, records):
        if n == 1:
            reply = {**reply, "handled_claim_ids": [], "claim_dispositions": [], "append": ""}
        return reply

    model, result, records = await identity_run(alter)
    feedback = model.edit_prompts[1].split("Previous edit failed: ", 1)[1]
    assert "missing claim IDs " + str(sorted(curate.evidence_id(r) for r in records)) in feedback
    assert all(r.text in result["body"] for r in records)


def drop_last(reply, records):
    """A valid reply for all but the last listed claim."""
    kept = reply["handled_claim_ids"][:-1]
    dropped = reply["handled_claim_ids"][-1]
    reply["handled_claim_ids"] = kept
    reply["claim_dispositions"] = [d for d in reply["claim_dispositions"] if d["id"] != dropped]
    reply["append"] = "\n".join(line for line in reply["append"].split("\n") if records[dropped].text not in line)
    return reply


@pytest.mark.asyncio
async def test_strict_subset_is_accepted_and_the_rest_dispatched_as_a_follow_up():
    snapshots = []

    def alter(n, reply, records):
        return drop_last(reply, records) if n == 1 else reply

    model, result, records = await identity_run(alter, progress=snapshots.append)
    ids = sorted(curate.evidence_id(r) for r in records)
    first = ast.literal_eval(
        model.edit_prompts[0].split("Claim IDs to integrate or prove already present: ", 1)[1].split("\n", 1)[0]
    )
    second = ast.literal_eval(
        model.edit_prompts[1].split("Claim IDs to integrate or prove already present: ", 1)[1].split("\n", 1)[0]
    )
    assert first == ids and second == [ids[-1]] and len(model.edit_prompts) == 2
    after_partial = next(s for s in snapshots if s["draft_progress"].get("partial_task_ids"))
    assert ids[-1] not in after_partial["draft_progress"]["handled_claim_ids"]  # never marked handled early
    assert sorted(result["draft_progress"]["handled_claim_ids"]) == ids
    assert all(result["body"].count(r.text) == 1 for r in records)


@pytest.mark.asyncio
async def test_interrupted_follow_up_resumes_with_only_the_missing_claims():
    records = [record(f"Media contract {index}: masks under {index + 5} MB.") for index in range(3)]
    change = PageChange(
        "g", "wiki/platform.md", "update", title="Platform", claim_ids=[curate.evidence_id(r) for r in records]
    )

    def retain(value):
        change.draft_body = value["body"]
        change.draft_meta = value["frontmatter"]
        change.draft_progress = value["draft_progress"]

    def alter(n, reply, records):
        if n == 1:
            return drop_last(reply, records)
        raise ModelError("transport unavailable", transient=True)

    with pytest.raises(ModelError, match="transport unavailable"):
        await identity_run(alter, progress=retain, change=change)
    missing = sorted(curate.evidence_id(r) for r in records)[-1]
    assert missing not in change.draft_progress["handled_claim_ids"]
    model = Scripted(records, lambda n, reply, records: reply)
    from doclayer import frontmatter

    result = await editing.edit_page(
        model,
        "Add endpoint requirements",
        change,
        records,
        frontmatter.build(change.draft_meta, change.draft_body),
        [],
        char_budget=12000,
        today="2026-10-06",
    )
    asked = [
        ast.literal_eval(p.split("Claim IDs to integrate or prove already present: ", 1)[1].split("\n", 1)[0])
        for p in model.edit_prompts
    ]
    assert asked == [[missing]]
    assert all(result["body"].count(r.text) == 1 for r in records)


def support_only_case():
    original = lineup("All models accept text and image input. Client SDKs are available.", ["Astra is the largest."])
    body = original.replace(" Client SDKs are available.", "")
    change = preservation_change(
        body,
        original,
        [
            (
                0,
                "All models accept text and image input.",
                "Original intro stated Client SDKs availability; restore it.",
                "preservation",
            )
        ],
    )
    support = [
        record(f"Latest models are available via the Responses API and Client SDKs, variant {index}.", "Overview")
        for index in range(6)
    ]
    return original, body, change, support


class Confused(RepairEditor):
    """First reply lists `handled` as handled claims, with dispositions."""

    def __init__(self, handled):
        super().__init__(
            {
                "All models accept text and image input.": "All models accept text and image input. Client SDKs are available."
            }
        )
        self.handled = handled

    async def chat_json(self, system, user):
        reply = await super().chat_json(system, user)
        if len(self.prompts) == 1:
            reply["handled_claim_ids"] = list(self.handled)
            reply["claim_dispositions"] = [
                {"id": i, "status": "integrated", "anchor": "Client SDKs"} for i in self.handled
            ]
        return reply


@pytest.mark.asyncio
async def test_support_only_ids_returned_as_handled_are_ignored_without_retry():
    """Job-04223e4edf: a repair task with no claim obligations got its six support-only IDs back as handled."""
    original, body, change, support = support_only_case()
    ids = sorted(curate.evidence_id(r) for r in support)
    model = Confused(ids)
    repaired = await editing.edit_page(
        model, "Lineup", change, support, body, [], char_budget=32768, today="2026-10-06", repair=True
    )
    assert len(model.prompts) == 1 and all(i in model.prompts[0] for i in ids)  # they were support-only evidence
    assert repaired["body"] == original and repaired["draft_progress"]["handled_claim_ids"] == []


@pytest.mark.asyncio
async def test_ids_outside_the_support_set_still_fail_with_precise_feedback():
    original, body, change, support = support_only_case()
    ids = sorted(curate.evidence_id(r) for r in support)
    model = Confused([ids[0], "claim-foreign"])
    repaired = await editing.edit_page(
        model, "Lineup", change, support, body, [], char_budget=32768, today="2026-10-06", repair=True
    )
    feedback = model.prompts[1].split("\x00", 1)[1].split("Previous edit failed: ", 1)[1]
    assert "unexpected claim IDs " + str(sorted([ids[0], "claim-foreign"])) in feedback
    assert "handled_claim_ids [] and claim_dispositions []" in feedback
    assert repaired["body"] == original and repaired["draft_progress"]["handled_claim_ids"] == []


@pytest.mark.asyncio
async def test_task_among_400_siblings_lists_only_its_nearest_siblings():
    siblings = "".join(
        f"## Endpoint {index} with an extended descriptive heading\nEndpoint {index} detail.\n" for index in range(400)
    )
    original = "# Lineup\nLineup overview.\n" + siblings
    original = original.replace("Endpoint 200 detail.", "Endpoint 200 detail. Client SDKs are available.")
    body = original.replace(" Client SDKs are available.", "")
    units = editing.page_units(body)
    target = next(
        index
        for index, unit in enumerate(units)
        if unit.label.endswith("Endpoint 200 with an extended descriptive heading")
    )
    change = preservation_change(
        body, original, [(target, "Endpoint 200 detail.", "Restore Client SDKs.", "preservation")]
    )
    model = RepairEditor({"Endpoint 200 detail.": "Endpoint 200 detail. Client SDKs are available."})
    repaired = await editing.edit_page(
        model, "Lineup", change, [], body, [], char_budget=32768, today="2026-10-06", repair=True
    )
    assert repaired["body"] == original and len(model.prompts) == 1
    structure = model.prompts[0].split("heading structure:\n", 1)[1].split("\nBinding", 1)[0]
    listed = [line for line in structure.splitlines() if ": Lineup / Endpoint " in line]
    assert len(listed) == 25 and "Endpoint 188 " in listed[0] and "Endpoint 212 " in listed[-1]
    assert "... 188 more sibling sections" in structure and "... 187 more sibling sections" in structure


@pytest.mark.asyncio
async def test_root_task_on_a_400_section_page_windows_its_direct_children():
    sections = "".join(
        f"## Endpoint {index} with an extended descriptive heading\nEndpoint {index} detail.\n" for index in range(400)
    )
    original = "# Lineup\nAll models accept text input. Client SDKs are available.\n" + sections
    body = original.replace(" Client SDKs are available.", "")
    change = preservation_change(
        body, original, [(0, "All models accept text input.", "Restore Client SDKs.", "preservation")]
    )
    model = RepairEditor({"All models accept text input.": "All models accept text input. Client SDKs are available."})
    repaired = await editing.edit_page(
        model, "Lineup", change, [], body, [], char_budget=32768, today="2026-10-06", repair=True
    )
    assert repaired["body"] == original and len(model.prompts) == 1
    structure = model.prompts[0].split("heading structure:\n", 1)[1].split("\nBinding", 1)[0]
    listed = [line for line in structure.splitlines() if ": Lineup / Endpoint " in line]
    assert len(listed) == 25 and "Endpoint 375 " in listed[0] and "Endpoint 399 " in listed[-1]
    assert "... 375 more subsections" in structure and len(structure) <= 32768 // 5


# Outline shapes: every shape fits the budget.
H = "with an extended descriptive heading"
LONG_HEADING = (
    "with a long descriptive heading that names the model family, its context window, its pricing tier, "
    "its regional availability, its rate-limit class and its deprecation schedule for the upcoming release cycle"
)
MID_HEADING = "with an extended descriptive heading naming its family and context window tier"


def outline_probe(name):
    if name == "O0":
        return (
            "# Lineup\nLineup overview. Client SDKs are available.\n"
            + "".join(f"## Endpoint {i} {H}\nEndpoint {i} detail.\n" for i in range(400)),
            "Lineup",
            "Lineup overview.",
            32768,
        )
    if name in ("O1", "O3"):
        heading = H if name == "O1" else MID_HEADING
        groups = "".join(
            f"## Group {g} {heading}\nGroup {g} detail.\n"
            + ("".join(f"### Endpoint {i} {heading}\nEndpoint {i} detail.\n" for i in range(400)) if g == 200 else "")
            for g in range(400)
        )
        original = ("# Lineup\nLineup overview.\n" + groups).replace(
            "Group 200 detail.", "Group 200 detail. Client SDKs are available."
        )
        return original, f"Group 200 {heading}", "Group 200 detail.", 32768
    if name in ("O2", "O2b"):
        count = 400 if name == "O2" else 30
        return (
            "# Lineup\nLineup overview. Client SDKs are available.\n"
            + "".join(f"## Endpoint {i} {LONG_HEADING}\nEndpoint {i} detail.\n" for i in range(count)),
            "Lineup",
            "Lineup overview.",
            32768,
        )
    if name == "O4":
        groups = "".join(
            f"## Group {g} {H}\nGroup {g} detail.\n"
            + ("".join(f"### Endpoint {i} {H}\nEndpoint {i} detail.\n" for i in range(60)) if g == 30 else "")
            for g in range(60)
        )
        original = ("# Lineup\nLineup overview.\n" + groups).replace(
            "Group 30 detail.", "Group 30 detail. Client SDKs are available."
        )
        return original, f"Group 30 {H}", "Group 30 detail.", 16384
    subs = "".join(
        f"### Model {m} {H}\nModel {m} detail.\n"
        + ("".join(f"#### Endpoint {i} {H}\nEndpoint {i} detail.\n" for i in range(100)) if m == 50 else "")
        for m in range(100)
    )
    original = (f"# Lineup\nLineup overview.\n## Family {H}\nFamily detail.\n" + subs).replace(
        "Model 50 detail.", "Model 50 detail. Client SDKs are available."
    )
    return original, f"Model 50 {H}", "Model 50 detail.", 32768


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["O0", "O1", "O2", "O2b", "O3", "O4", "O5"])
async def test_local_outline_windows_shrink_until_the_outline_fits(name):
    original, label, anchor, budget = outline_probe(name)
    body = original.replace(" Client SDKs are available.", "")
    units = editing.page_units(body)
    target = next(index for index, unit in enumerate(units) if unit.label.endswith(label))
    change = preservation_change(body, original, [(target, anchor, "Restore Client SDKs.", "preservation")])
    model = RepairEditor({anchor: anchor + " Client SDKs are available."})
    repaired = await editing.edit_page(
        model, "Lineup", change, [], body, [], char_budget=budget, today="2026-10-06", repair=True
    )
    assert repaired["body"] == original and len(model.prompts) == 1
    structure = model.prompts[0].split("heading structure:\n", 1)[1].split("\nBinding", 1)[0]
    assert len(structure) <= max(500, budget // 5)
    assert f"{units[target].id}: {units[target].label}" in structure  # the selected section keeps its place
    assert all(
        f"{units[a].id}: " in structure
        for a in range(target)
        if units[a].level < units[target].level and units[target].label.startswith(units[a].label + " / ")
    )  # and its ancestors
