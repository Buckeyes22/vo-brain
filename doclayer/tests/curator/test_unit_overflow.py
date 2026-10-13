"""A unit whose complete input does not fit the bounded context is dropped, not a job stop.

A "cannot fit" / "exceeds bounded input"
refusal raised for ONE unit (a section, claim, chunk, move or batch) goes
through degrade_unit like a rejected reply: the unit is dropped, a current
gap names the sizes, and the page and the job continue. Only job-level limits
(model calls, active time, bytes) and transport errors stop a job. Each test
below exercises one converted site.
"""

import json
import re

import pytest
from test_bounded_editing import supported
from test_introduced_section_repair import BODY, INTRO, prepared
from test_introduced_section_repair import run as run_structural
from test_placement import records
from test_unit_degrade import Structural, noop_edit

from doclayer import frontmatter
from doclayer.curator import (
    curate,
    degrade,
    editing,
    placement,
    reconciliation,
    source_constraints,
)
from doclayer.curator.degrade import GAP_MARK
from doclayer.curator.editorial import EditorialContextOverflow
from doclayer.curator.model import ModelClient, UnitTooLarge
from doclayer.curator.state import Evidence, JobState, PageChange, SourceRecord, Usage

TOO_LARGE = "input exceeds bounded context"


def overflow_gaps(gaps, unit):
    return [gap for gap in gaps if GAP_MARK in gap and TOO_LARGE in gap and unit in gap]


def has_sizes(gap):
    return re.search(r"\d+ (?:characters|chars)", gap) and "budget" in gap


class NoCalls:
    async def chat_json(self, *args, **kwargs):
        raise AssertionError("an oversized unit is never dispatched")

    chat_bounded_json = chat_verdicts = chat_json


# --------------------------------------------------------------------------- #
# generic bounded dispatch and the placement task frame
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_bounded_dispatch_overflow_is_a_droppable_unit_with_sizes(cfg):
    client = ModelClient(cfg, Usage())
    with pytest.raises(UnitTooLarge, match=r"101 characters with budget 50"):
        await client.chat_bounded_json("s", "u" * 100, char_budget=50)
    gaps = []
    with degrade.recording(gaps):
        try:
            await client.chat_bounded_json("s", "u" * 100, char_budget=50)
        except UnitTooLarge as exc:
            degrade.degrade_unit("wiki/x.md: some unit", exc)
    assert overflow_gaps(gaps, "wiki/x.md: some unit") and has_sizes(gaps[0])


@pytest.mark.asyncio
async def test_placement_task_frame_overflow_is_droppable():
    with pytest.raises(
        UnitTooLarge, match="whole placement task exceeds bounded context: 200 characters with budget 50"
    ):
        await placement._json(NoCalls(), "s" * 100, "u" * 100, 50, lambda value: True)


# --------------------------------------------------------------------------- #
# placement coverage and plan
# --------------------------------------------------------------------------- #


def ledger_for(evidence):
    return {"claims": {curate.evidence_id(item): {"status": "pending"} for item in evidence}}


@pytest.mark.asyncio
async def test_single_claim_coverage_frame_overflow_drops_the_check():
    evidence = records(1)
    ledger = ledger_for(evidence)
    gaps = []
    with degrade.recording(gaps):
        await placement._covered_many(
            NoCalls(),
            evidence,
            "wiki/device.md",
            {"wiki/device.md": ({}, "# Device\n")},
            ledger,
            len(placement._coverage_system()) + 1500,
            lambda: None,
        )
    assert ledger["claims"][curate.evidence_id(evidence[0])]["status"] == "pending"
    found = overflow_gaps(gaps, f"coverage check of claim {curate.evidence_id(evidence[0])}")
    assert found and has_sizes(found[0])


@pytest.mark.asyncio
async def test_single_claim_coverage_passage_overflow_drops_the_check(monkeypatch):
    monkeypatch.setattr(placement, "views", lambda units, budget: [(0, "Huge", "x" * 20000)])
    evidence = records(1)
    ledger = ledger_for(evidence)
    gaps = []
    with degrade.recording(gaps):
        await placement._covered_many(
            NoCalls(), evidence, "wiki/device.md", {"wiki/device.md": ({}, "# Device\n")}, ledger, 12000, lambda: None
        )
    assert ledger["claims"][curate.evidence_id(evidence[0])]["status"] == "pending"
    found = overflow_gaps(gaps, "coverage check of claim")
    assert found and "governing canonical qualifications" in found[0] and has_sizes(found[0])


@pytest.mark.asyncio
async def test_coverage_batch_frame_overflow_drops_the_batch():
    evidence = records(2)
    ledger = ledger_for(evidence)
    gaps = []
    with degrade.recording(gaps):
        await placement._covered_many(
            NoCalls(),
            evidence,
            "wiki/device.md",
            {"wiki/device.md": ({}, "# Device\n")},
            ledger,
            len(placement._coverage_system(batch=True)) + 1500,
            lambda: None,
            view_budget=500,
        )
    assert all(row["status"] == "pending" for row in ledger["claims"].values())
    found = overflow_gaps(gaps, "coverage batch on wiki/device.md")
    assert len(found) == 2 and all(has_sizes(gap) for gap in found)


@pytest.mark.asyncio
async def test_coverage_batch_passage_overflow_drops_the_batch(monkeypatch):
    monkeypatch.setattr(placement, "views", lambda units, budget: [(0, "Huge", "x" * 20000)])
    evidence = records(2)
    ledger = ledger_for(evidence)
    gaps = []
    with degrade.recording(gaps):
        await placement._covered_many(
            NoCalls(), evidence, "wiki/device.md", {"wiki/device.md": ({}, "# Device\n")}, ledger, 12000, lambda: None
        )
    for row in ledger["claims"].values():
        assert row["status"] == "pending"
        assert [a["diagnostic"]["unit_dropped"] for a in row["unproven_coverage_attempts"].values()] == [True]
    found = overflow_gaps(gaps, "coverage batch on wiki/device.md")
    assert found and has_sizes(found[0])


@pytest.mark.asyncio
async def test_claim_too_large_to_plan_alone_is_dropped_and_the_rest_is_placed():
    huge = Evidence(
        "manual",
        "Manual",
        "## Zebra",
        "quote",
        "Zebra fact " + "z" * 6000,
        applicability={"model": "Device"},
        quote="Zebra fact",
        support_status="supported",
    )
    evidence = [huge] + records(2)

    class Planner:
        async def chat_bounded_json(self, system, user, *, char_budget):
            ids = [item["id"] for item in json.loads(user.split("Claims JSON:\n")[-1])]
            return {
                "pages": [
                    {
                        "action": "update",
                        "path": "wiki/device.md",
                        "title": "Device",
                        "subject_type": "product",
                        "rationale": "r",
                        "source_ids": ["manual"],
                        "claim_ids": ids,
                        "related": [],
                    }
                ],
                "covered": [],
            }

    with pytest.raises(UnitTooLarge, match="whole placement claim cannot fit bounded context"):
        await placement.plan_batch(Planner(), "Zebra", [], evidence, [], [], char_budget=6000)
    gaps = []
    with degrade.recording(gaps):
        result = await placement.plan_batch(Planner(), "Zebra", [], evidence, [], [], char_budget=6000)
    statuses = {identity: row["status"] for identity, row in result["ledger"]["claims"].items()}
    assert statuses[curate.evidence_id(huge)] == "failed"
    assert result["ledger"]["claims"][curate.evidence_id(huge)]["reason"] == placement.TOO_LARGE_ALONE
    assert sorted(statuses.values()).count("planned") == 2
    found = overflow_gaps(gaps, f"placement of claim {curate.evidence_id(huge)}")
    assert found and has_sizes(found[0])


# --------------------------------------------------------------------------- #
# editing: routing, structural repair, article rows, section edits
# --------------------------------------------------------------------------- #

PAGE = "# Device\n\n## Manual\n\nKnown original manual fact.\n"


def device_page(body=PAGE):
    meta = {"title": "Device", "schema_version": 1, "privacy": "internal"}
    return PageChange("g", "wiki/device.md", "update", title="Device"), frontmatter.build(meta, body)


@pytest.mark.asyncio
async def test_page_routing_frame_overflow_drops_every_claim_of_the_routing():
    body = "# Device\n\n" + "".join(f"## Section {i} " + "h" * 60 + "\n\nText.\n\n" for i in range(60))
    change = PageChange("g", "wiki/device.md", "update", title="Device")
    units = editing.page_units(body)
    evidence = [supported("Device scoped fact one"), supported("Device scoped fact two")]
    unrouted, gaps = {}, []
    with degrade.recording(gaps):
        routes = await editing.route_claims(
            NoCalls(), "Device", change, evidence, units, {}, char_budget=3000, progress=lambda: None, unrouted=unrouted
        )
    assert routes == {} and set(unrouted) == {curate.evidence_id(item) for item in evidence}
    assert all(row["reason"] == TOO_LARGE for row in unrouted.values())
    found = overflow_gaps(gaps, "section routing of claims")
    assert found and has_sizes(found[0])


@pytest.mark.asyncio
async def test_claim_evidence_too_large_to_route_drops_that_claim_only():
    change, page = device_page()
    units = editing.page_units(frontmatter.parse(page)[1])
    huge, small = supported("Device huge fact " + "y" * 9000), supported("Device scoped small fact")

    class Router:
        async def chat_json(self, system, user):
            from test_bounded_editing import routing_response

            return routing_response(user)

    unrouted, gaps = {}, []
    with degrade.recording(gaps):
        routes = await editing.route_claims(
            Router(),
            "Device",
            change,
            [huge, small],
            units,
            {},
            char_budget=8000,
            progress=lambda: None,
            unrouted=unrouted,
        )
    assert set(routes) == {curate.evidence_id(small)} and set(unrouted) == {curate.evidence_id(huge)}
    found = overflow_gaps(gaps, f"section routing of claim {curate.evidence_id(huge)}")
    assert found and has_sizes(found[0])


@pytest.mark.asyncio
async def test_structural_task_too_large_is_dropped_and_anchored_repair_follows():
    change = prepared(BODY.replace("An exact qualified runtime fact.", "Complete unchanged added content. " * 400))
    client = Structural(fail=AssertionError("the oversized structural task is never dispatched"))
    gaps = []
    with degrade.recording(gaps):
        await run_structural(client, change, budget=6000)
    found = overflow_gaps(gaps, "wiki/device.md: structural repair task")
    assert found and has_sizes(found[0])


@pytest.mark.asyncio
async def test_article_repair_row_whose_anchor_fits_no_view_is_dropped(monkeypatch):
    change = prepared()
    section = next(u for u in editing.page_units(change.draft_body) if u.text == INTRO)
    original = editing._article_repair_locations

    def locations(review, body, units, previous):
        digest, rows = original(review, body, units, previous)
        return digest, [{**rows[0], "section_id": section.id, "anchor": "anchor text absent from every view"}]

    monkeypatch.setattr(editing, "_article_repair_locations", locations)

    class Client:
        async def chat_json(self, system, user):
            if user.startswith("BOUNDED INTRODUCED SECTION REPAIR\n"):
                return {"operations": [], "notes": []}
            return noop_edit(user)

    gaps = []
    with degrade.recording(gaps):
        result = await run_structural(Client(), change)
    assert result["body"] == BODY
    found = overflow_gaps(gaps, f"article repair row in section {section.id}")
    assert found and has_sizes(found[0])


def routed(monkeypatch, section_id):
    async def route(client, topic, change, records, units, routes, **kwargs):
        for record in records:
            routes[curate.evidence_id(record)] = {
                "id": curate.evidence_id(record),
                "section_id": section_id,
                "new_heading": None,
                "rationale": "r",
            }
        return routes

    monkeypatch.setattr(editing, "route_claims", route)


@pytest.mark.asyncio
async def test_claim_evidence_too_large_for_any_edit_view_is_dropped(monkeypatch):
    change, page = device_page()
    manual = next(u for u in editing.page_units(PAGE) if u.label.endswith("Manual"))
    routed(monkeypatch, manual.id)
    huge = supported("Device huge fact " + "y" * 4000)
    gaps = []
    with degrade.recording(gaps):
        result = await editing.edit_page(
            NoCalls(), "Device", change, [huge], page, [], char_budget=6000, today="2026-10-06"
        )
    assert result["body"] == PAGE
    assert result["draft_progress"]["not_integrated"][curate.evidence_id(huge)]["reason"] == TOO_LARGE
    found = overflow_gaps(gaps, f"section edit {manual.id} (claim {curate.evidence_id(huge)})")
    assert found and has_sizes(found[0])


@pytest.mark.asyncio
async def test_section_whose_local_page_structure_cannot_fit_is_dropped(monkeypatch):
    body = "# Device\n\n## Manual " + "w" * 1500 + "\n\nKnown original manual fact.\n"
    change, page = device_page(body)
    manual = next(u for u in editing.page_units(body) if u.level == 2)
    routed(monkeypatch, manual.id)
    record = supported("Device scoped small fact")
    gaps = []
    with degrade.recording(gaps):
        result = await editing.edit_page(
            NoCalls(), "Device", change, [record], page, [], char_budget=6000, today="2026-10-06"
        )
    assert result["body"] == body
    assert result["draft_progress"]["not_integrated"][curate.evidence_id(record)]["reason"] == TOO_LARGE
    found = overflow_gaps(gaps, f"section edit {manual.id}")
    assert found and "local structure needs" in found[0] and has_sizes(found[0])


@pytest.mark.asyncio
async def test_edit_task_too_large_even_at_its_minimum_is_dropped(monkeypatch):
    change, page = device_page()
    manual = next(u for u in editing.page_units(PAGE) if u.label.endswith("Manual"))
    routed(monkeypatch, manual.id)
    record = supported("Device scoped small fact")
    gaps = []
    with degrade.recording(gaps):
        result = await editing.edit_page(
            NoCalls(), "Device " + "t" * 7000, change, [record], page, [], char_budget=8000, today="2026-10-06"
        )
    assert result["body"] == PAGE
    assert result["draft_progress"]["not_integrated"][curate.evidence_id(record)]["reason"] == TOO_LARGE
    found = overflow_gaps(gaps, f"section edit {manual.id}")
    assert found and "bounded edit task exceeds input context" in found[0] and has_sizes(found[0])


# --------------------------------------------------------------------------- #
# extraction and source support
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_extraction_chunk_frame_overflow_is_droppable():
    with pytest.raises(
        UnitTooLarge, match=r"exceed bounded context; material retained unchanged: \d+ characters with budget 100"
    ):
        await curate._extract_json(NoCalls(), "s" * 100, "u" * 100, 100)


@pytest.mark.asyncio
async def test_many_section_groups_are_all_extracted_without_a_selection_step(monkeypatch):
    """The former group maximum (3 primary / 2 supplementary) and its selection
    request are gone: a source with many groups has every group extracted."""
    chunks = [[(f"#S{i}-{j}", "body text") for j in range(20)] for i in range(6)]
    monkeypatch.setattr(curate, "map_source_structure", lambda source, text, char_budget: chunks)
    source = SourceRecord("s", "url", "https://docs.test/big", status="fetched", role="primary")
    users = []

    class Extractor:
        async def chat_json(self, system, user, **kwargs):
            users.append(user)
            return {"claims": []}

        chat_bounded_json = chat_verdicts = chat_json

    gaps = []
    with degrade.recording(gaps):
        claims = await curate.extract_claims(Extractor(), source, "text", "Device", [], char_budget=8000)
    assert claims == [] and len(users) == 6 and not gaps
    assert not any("Section groups:" in user for user in users)
    assert len(source.reviewed_sections) == 120 and source.omitted_sections == []


@pytest.mark.asyncio
async def test_source_support_batch_too_large_leaves_its_claims_unknown(monkeypatch):
    monkeypatch.setattr(source_constraints, "material", lambda record, text, budget: None)
    monkeypatch.setattr(curate, "support_excerpt", lambda record, text, budget=2000: "x" * 40000)
    evidence = records(2)
    gaps = []
    with degrade.recording(gaps):
        results = await curate.verify_source_claims(NoCalls(), evidence, {"manual": "x"}, char_budget=32768)
    for item in evidence:
        verdict = results[curate.evidence_id(item)]
        assert verdict["status"] == "unknown" and verdict["unit_dropped"]
        assert verdict["reason"] == "source-support batch dropped: " + TOO_LARGE
    found = overflow_gaps(gaps, "source-support review of claim")
    assert len(found) == 2
    assert found and has_sizes(found[0])


@pytest.mark.asyncio
async def test_source_record_review_too_large_leaves_that_claim_unknown(monkeypatch):
    monkeypatch.setattr(
        source_constraints, "material", lambda record, text, budget: ("local", "excerpt", [("loc", "body")])
    )
    item = records(1)[0]
    with pytest.raises(UnitTooLarge, match="complete ancestor constraint check cannot fit bounded context: frame"):
        await source_constraints.verify(NoCalls(), item, "text", char_budget=2000, excerpt_budget=500, binding="b")
    gaps = []
    with degrade.recording(gaps):
        results = await curate.verify_source_claims(NoCalls(), [item], {"manual": "text"}, char_budget=2000)
    verdict = results[curate.evidence_id(item)]
    assert verdict["reason"] == "source record review exceeds bounded context" and verdict["unit_dropped"]
    found = overflow_gaps(gaps, f"source-support review of claim {curate.evidence_id(item)}")
    assert found and has_sizes(found[0])


# --------------------------------------------------------------------------- #
# whole-page drafting, reconciliation views, page capacity
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_existing_page_too_large_for_a_whole_draft_is_droppable_to_bounded_editing():
    change = PageChange("g", "wiki/device.md", "update", title="Device", rationale="r")
    page = frontmatter.build({"title": "Device"}, "# Device\n\n" + "Fact. " * 210)
    with pytest.raises(UnitTooLarge, match="full existing page exceeds bounded drafting context"):
        from doclayer.curator.curate import draft_page

        # Small enough for the whole-draft writer's length gate, larger than its remaining budget.
        await draft_page(NoCalls(), "Device", change, [], page, [], char_budget=4600, today="2026-10-06")


@pytest.mark.asyncio
async def test_reconciliation_view_too_large_keeps_fresh_text_and_drops_the_view():
    current = "# Device\nOperator qualification is retained.\n"
    change = PageChange("group", "wiki/device.md", "update")
    views, gaps = [], []
    corrections = [{"page": "wiki/device.md", "instruction": "keep " + "c" * 9000}]
    with degrade.recording(gaps):
        result = await reconciliation._merge_material(
            NoCalls(),
            "topic",
            change,
            current,
            "Supported contribution.\n",
            corrections,
            7000,
            "binding",
            {},
            None,
            "overlap",
            protected=current,
            dropped=views,
        )
    assert result == current and [row["text"] for row in views] == ["Supported contribution.\n"]
    found = overflow_gaps(gaps, "reconciliation of draft view")
    assert found and has_sizes(found[0])


def test_assigned_claim_too_large_for_its_page_leaves_the_page_not_the_job(cfg, store, monkeypatch):
    from doclayer.curator.pipeline import Pipeline

    state = store.create(JobState("", "", "Device"))[0]
    pipeline = Pipeline(cfg, store)
    change = PageChange("g", "wiki/device.md", "update", claim_ids=["claim-big", "claim-ok"])

    class Capacity:
        def require_assigned(self, change, original, records):
            if "claim-big" in change.claim_ids:
                raise EditorialContextOverflow(
                    "wiki/device.md: assigned claim claim-big cannot fit the 32768-char context budget on its own: "
                    "its claim text (40000 chars) alone overflows",
                    transient=False,
                    diagnostic={"operation": "page-capacity", "claim_id": "claim-big"},
                )

    monkeypatch.setattr(pipeline, "_capacity", lambda state: Capacity())
    monkeypatch.setattr(pipeline, "_base_page", lambda state, change: ({}, ""))
    with pytest.raises(EditorialContextOverflow):
        pipeline._require_page_capacity(state, PageChange("g", "wiki/device.md", "update", claim_ids=["claim-big"]), [])
    gaps = []
    with degrade.recording(gaps):
        pipeline._require_page_capacity(state, change, [])
    assert change.claim_ids == ["claim-ok"]
    found = overflow_gaps(gaps, "wiki/device.md: assigned claim claim-big")
    assert found and "40000 chars" in found[0] and "32768-char context budget" in found[0]


@pytest.mark.asyncio
async def test_source_quotation_repair_and_semantic_recheck_frames_too_large_are_droppable():
    request = {"claim": "x" * 5000}
    with pytest.raises(
        UnitTooLarge, match=r"whole source quotation repair cannot fit bounded input: \d+ characters with budget 1000"
    ):
        await source_constraints._repair_quote_fields(
            NoCalls(),
            {"id": "x", "supported": True, "consistent": True, "reason": "r"},
            [],
            request,
            None,
            "",
            "",
            [],
            [],
            char_budget=1000,
            attempts=1,
            metadata={},
        )
    with pytest.raises(
        UnitTooLarge, match=r"repaired-source semantic check cannot fit bounded input: \d+ characters with budget 1000"
    ):
        await source_constraints._reverify_repaired_semantics(
            NoCalls(),
            {"qualifiers": [], "support_quotes": []},
            request,
            None,
            "",
            "",
            [],
            [],
            char_budget=1000,
            metadata={},
        )


@pytest.mark.asyncio
async def test_single_claim_support_excerpt_too_large_is_dropped_as_a_unit(monkeypatch):
    monkeypatch.setattr(source_constraints, "material", lambda record, text, budget: None)
    # Support exists, but not within the bounded excerpt budget.
    monkeypatch.setattr(
        curate, "support_excerpt", lambda record, text, budget=2000: "" if budget < 10**6 else "support"
    )
    item = records(1)[0]
    unbound = await curate.verify_source_claims(NoCalls(), [item], {"manual": "source"}, char_budget=32768)
    assert "unit_dropped" not in unbound[curate.evidence_id(item)]  # no job bound: prior unknown, no drop
    gaps = []
    with degrade.recording(gaps):
        results = await curate.verify_source_claims(NoCalls(), [item], {"manual": "source"}, char_budget=32768)
    verdict = results[curate.evidence_id(item)]
    assert verdict["status"] == "unknown" and verdict["unit_dropped"]
    assert verdict["reason"] == "selected source support exceeds bounded context"
    found = overflow_gaps(gaps, f"source-support review of claim {curate.evidence_id(item)}")
    assert found and "excerpt budget" in found[0] and has_sizes(found[0])


@pytest.mark.asyncio
async def test_ambiguous_support_is_not_a_unit_overflow_and_still_isolates(monkeypatch):
    monkeypatch.setattr(source_constraints, "material", lambda record, text, budget: None)
    monkeypatch.setattr(curate, "support_excerpt", lambda record, text, budget=2000: "")
    item = records(1)[0]
    gaps = []
    with degrade.recording(gaps):
        results = await curate.verify_source_claims(NoCalls(), [item], {"manual": "source"}, char_budget=32768)
    assert "unit_dropped" not in results[curate.evidence_id(item)] and gaps == []
