"""Editor behavior: targeted repair, current-text views, planned-heading tails,
fail-fast retries and resume safety."""

import ast
import json
import re

import pytest

from doclayer import frontmatter
from doclayer.curator import editing
from doclayer.curator.model import ModelError
from doclayer.curator.state import Evidence, PageChange

TODAY = "2026-10-05"
BUDGET = 16000


def record(text, source="s"):
    return Evidence(
        source,
        "manual",
        "#Manual",
        "data",
        text,
        url="https://device.example/manual",
        quote="Actual original-source context " + text,
        support_status="supported",
    )


def claims_in(user):
    return dict(re.findall(r"^- \[(claim-[^ /]+).*?\): (.+)$", user, re.M))


def view_of(user):
    return user.split("Current section view:\n", 1)[1].split("\nClaim IDs", 1)[0]


class Model:
    """Protocol-faithful fake: routes by a rule and edits only what its prompt shows."""

    def __init__(self, route=None, edit=None):
        self.route_rule = route
        self.edit_hook = edit
        self.route_calls = 0
        self.edits = []

    async def chat_json(self, system, user):
        if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
            self.route_calls += 1
            frame = json.loads(user.split("\n", 1)[1].split("\nEvidence:\n", 1)[0])
            rows = []
            for identity, claim in claims_in(user).items():
                rule = self.route_rule(claim, frame) if self.route_rule else {}
                rows.append(
                    {
                        "id": identity,
                        "rationale": "fixture route",
                        **{"section_id": frame["sections"][-1]["section_id"], "new_heading": None},
                        **rule,
                    }
                )
            return {"routes": rows}
        identities = ast.literal_eval(re.search(r"Claim IDs to integrate or prove already present: (.+)", user)[1])
        section_id = re.search(r"Section ID: (.+)", user)[1]
        view = view_of(user)
        self.edits.append({"section_id": section_id, "view": view, "user": user, "ids": identities})
        if self.edit_hook:
            reply = self.edit_hook(user, view, identities, section_id)
            if reply is not None:
                return reply
        claims = claims_in(user)
        append, dispositions = "", []
        for identity in identities:
            claim = claims[identity]
            if view.count(claim) == 1:
                dispositions.append({"id": identity, "status": "already_present", "anchor": claim})
            else:
                append += claim + "\n"
                dispositions.append({"id": identity, "status": "integrated", "anchor": claim})
        return {
            "section_id": section_id,
            "edits": [],
            "append": append,
            "handled_claim_ids": identities,
            "claim_dispositions": dispositions,
            "notes": [],
        }


def section_route(claim, frame):
    name = re.search(r"Section\d+", claim)[0]
    return {"section_id": next(r["section_id"] for r in frame["sections"] if r["heading_path"].endswith(name))}


# --------------------------------------------------------------------------- #
# Repair carries completed work and re-edits only what the failures flag
# --------------------------------------------------------------------------- #


def five_section_page():
    body = "# Device\n\nIntro.\n\n" + "".join(f"## Section{i}\n\nOriginal {i}.\n\n" for i in range(1, 6))
    return frontmatter.build({"schema_version": 1, "privacy": "internal", "title": "Device"}, body)


async def drafted_five_sections():
    records = [record(f"Section{i} fact cites https://s{i}.example/doc.", str(i)) for i in range(1, 6)]
    change = PageChange("g", "wiki/device.md", "update", title="Device")
    model = Model(route=section_route)
    result = await editing.edit_page(
        model, "Device", change, records, five_section_page(), [], char_budget=BUDGET, today=TODAY
    )
    change.draft_meta, change.draft_body, change.draft_progress = (
        result["frontmatter"],
        result["body"],
        result["draft_progress"],
    )
    assert len(model.edits) == 5
    return change, records


@pytest.mark.asyncio
async def test_editing_repair_with_one_flagged_paragraph_calls_only_that_unit():
    change, records = await drafted_five_sections()
    change.repair_context = {
        "failures": [
            {
                "severity": "blocker",
                "check": "scope",
                "detail": "draft cites excluded or semantically restricted source https://s3.example/doc",
            }
        ]
    }
    repair = Model(route=section_route)
    result = await editing.edit_page(
        repair,
        "Device\nValidation failures to repair: []",
        change,
        records,
        frontmatter.build(change.draft_meta, change.draft_body),
        [],
        char_budget=BUDGET,
        today=TODAY,
        repair=True,
    )
    assert repair.route_calls == 0
    assert len(repair.edits) == 1
    assert repair.edits[0]["ids"] == []
    assert "https://s3.example/doc" in repair.edits[0]["view"] and "## Section3" in repair.edits[0]["view"]
    assert result["body"] == change.draft_body
    assert set(result["draft_progress"]["handled_claim_ids"]) == set(change.draft_progress["handled_claim_ids"])


@pytest.mark.asyncio
async def test_editing_repair_without_locatable_failure_audits_every_region_but_keeps_routes():
    change, records = await drafted_five_sections()
    change.repair_context = {
        "failures": [{"severity": "blocker", "check": "scope", "detail": "draft cites an excluded source"}]
    }
    repair = Model(route=section_route)
    await editing.edit_page(
        repair,
        "Device",
        change,
        records,
        frontmatter.build(change.draft_meta, change.draft_body),
        [],
        char_budget=BUDGET,
        today=TODAY,
        repair=True,
    )
    assert repair.route_calls == 0
    assert len(repair.edits) >= 5 and all(edit["ids"] == [] for edit in repair.edits)


@pytest.mark.asyncio
async def test_editing_repair_still_integrates_claims_that_were_never_handled():
    change, records = await drafted_five_sections()
    new = record("Section2 extra fact cites https://s9.example/doc.", "x")
    change.draft_progress["carry_binding"] = None  # evidence set changed: no carry-forward
    change.repair_context = {
        "failures": [{"severity": "blocker", "check": "scope", "detail": "cites https://s3.example/doc"}]
    }
    repair = Model(route=section_route)
    await editing.edit_page(
        repair,
        "Device",
        change,
        records + [new],
        frontmatter.build(change.draft_meta, change.draft_body),
        [],
        char_budget=BUDGET,
        today=TODAY,
        repair=True,
    )
    assert repair.route_calls >= 1 and len(repair.edits) >= 5


# --------------------------------------------------------------------------- #
# Current-text views and uniqueness over what the model saw
# --------------------------------------------------------------------------- #


def one_section_page(extra=""):
    return frontmatter.build(
        {"schema_version": 1, "privacy": "internal", "title": "Device"},
        "# Device\n\n## Steering\n\n- gamma setting is 5.\n" + extra,
    )


def steering_route(claim, frame):
    return {"section_id": next(r["section_id"] for r in frame["sections"] if r["heading_path"].endswith("Steering"))}


@pytest.mark.asyncio
async def test_editing_later_batch_sees_text_added_by_earlier_batch():
    # Claim 7 repeats a fact batch 1 appended; a stale view hides it and the
    # model re-adds it, so its anchor occurs twice ("2 occurrences").
    records = [record(f"Distinct steering fact number {i}.", str(i)) for i in range(1, 7)]
    records.append(record("Distinct steering fact number 6.", "other-source"))
    change = PageChange("g", "wiki/device.md", "update", title="Device")
    model = Model(route=steering_route)
    result = await editing.edit_page(
        model, "Repair", change, records, one_section_page(), [], char_budget=BUDGET, today=TODAY, repair=True
    )
    assert len(model.edits) == 2
    assert "Distinct steering fact number 1." in model.edits[1]["view"]
    assert result["body"].count("Distinct steering fact number 6.") == 1


@pytest.mark.asyncio
async def test_editing_later_batch_does_not_see_text_an_earlier_batch_replaced():
    # Batch 1 rewrites the gamma line. A stale view still offers the old line,
    # so batch 2 quotes it as already_present ("0 occurrences").
    def rewrite_gamma(user, view, identities, section_id):
        if len(identities) == 6 and "- gamma setting is 5." in view:
            claims = claims_in(user)
            first, rest = identities[0], identities[1:]
            new = "- gamma setting is 6 (revised)."
            return {
                "section_id": section_id,
                "edits": [{"old": "- gamma setting is 5.", "new": new}],
                "append": "".join(claims[i] + "\n" for i in rest),
                "handled_claim_ids": identities,
                "claim_dispositions": [{"id": first, "status": "integrated", "anchor": new}]
                + [{"id": i, "status": "integrated", "anchor": claims[i]} for i in rest],
                "notes": [],
            }
        return None

    records = [record(f"Unrelated steering fact number {i}.", str(i)) for i in range(1, 7)]
    records.append(record("gamma setting is 5.", "g"))
    change = PageChange("g", "wiki/device.md", "update", title="Device")
    model = Model(route=steering_route, edit=rewrite_gamma)
    result = await editing.edit_page(
        model, "Repair", change, records, one_section_page(), [], char_budget=BUDGET, today=TODAY, repair=True
    )
    second = model.edits[1]["view"]
    assert "- gamma setting is 6 (revised)." in second and "- gamma setting is 5." not in second
    assert "gamma setting is 5." in result["body"]


@pytest.mark.asyncio
async def test_editing_anchor_unique_in_shown_view_is_accepted_when_the_unit_repeats_it():
    filler = "".join(f"- row {i}: {'x' * 50}\n" for i in range(100))
    lines = filler.splitlines(keepends=True)
    lines[2] = "- alpha: Shared sentence X.\n"
    lines[90] = "- omega: Shared sentence X.\n"
    page = frontmatter.build(
        {"schema_version": 1, "privacy": "internal", "title": "Device"}, "# Device\n\n## Steering\n\n" + "".join(lines)
    )
    unit = next(u for u in editing.page_units(frontmatter.parse(page)[1]) if u.label.endswith("Steering"))
    assert len(editing.views([unit], 4000)) > 1 and unit.text.count("Shared sentence X.") == 2

    def edit_shared(user, view, identities, section_id):
        assert view.count("Shared sentence X.") == 1  # unique in what the model sees
        new = "Shared sentence X. Revised with the alpha detail."
        return {
            "section_id": section_id,
            "edits": [{"old": "Shared sentence X.", "new": new}],
            "append": "",
            "handled_claim_ids": identities,
            "claim_dispositions": [{"id": identities[0], "status": "integrated", "anchor": new}],
            "notes": [],
        }

    change = PageChange("g", "wiki/device.md", "update", title="Device")
    model = Model(route=steering_route, edit=edit_shared)
    result = await editing.edit_page(
        model,
        "Edit",
        change,
        [record("alpha Shared sentence X. detail", "a")],
        page,
        [],
        char_budget=BUDGET,
        today=TODAY,
    )
    assert result["body"].count("Revised with the alpha detail.") == 1
    assert result["body"].count("Shared sentence X.") == 2


def test_editing_apply_edits_view_scope_does_not_change_whole_unit_behaviour():
    unit = editing.Unit("section-x", "Steering", 2, "## Steering\nSame.\nSame.\n")
    result = {
        "section_id": "section-x",
        "edits": [{"old": "Same.", "new": "Changed."}],
        "append": "",
        "handled_claim_ids": ["c"],
        "claim_dispositions": [{"id": "c", "status": "integrated", "anchor": "Changed."}],
    }
    with pytest.raises(ModelError, match="ambiguous"):
        editing.apply_edits(unit, result, {"c"}, max_output_chars=6000)
    with pytest.raises(ModelError, match="ambiguous"):
        editing.apply_edits(unit, result, {"c"}, max_output_chars=6000, view=unit.text)


# --------------------------------------------------------------------------- #
# Appended headings need the section end and an explicit contract
# --------------------------------------------------------------------------- #

HEADING_RULE = "New headings go ONLY in append"


@pytest.mark.asyncio
async def test_editing_new_heading_task_sees_section_tail_and_states_append_only():
    intro = "".join(
        f"Intro line {i} about reasoning effort controls and performance. " + "y" * 40 + "\n" for i in range(120)
    )
    page = frontmatter.build(
        {"schema_version": 1, "privacy": "internal", "title": "Perf"}, "# Perf\n\n" + intro + "TAIL_MARKER_LINE\n"
    )
    root = editing.page_units(frontmatter.parse(page)[1])[0]
    parts = editing.views([root], 4000)
    assert len(parts) > 1 and "TAIL_MARKER_LINE" not in parts[0][2] and "TAIL_MARKER_LINE" in parts[-1][2]

    def new_section(claim, frame):
        return {"section_id": frame["root_section_id"], "new_heading": "## Reasoning Effort"}

    def heading_in_append(user, view, identities, section_id):
        claims = claims_in(user)
        text = "## Reasoning Effort\n\n" + claims[identities[0]] + "\n"
        return {
            "section_id": section_id,
            "edits": [],
            "append": text,
            "handled_claim_ids": identities,
            "claim_dispositions": [{"id": identities[0], "status": "integrated", "anchor": claims[identities[0]]}],
            "notes": [],
        }

    change = PageChange("g", "wiki/perf.md", "update", title="Perf")
    model = Model(route=new_section, edit=heading_in_append)
    result = await editing.edit_page(
        model, "Perf", change, [record("Reasoning effort has three levels.")], page, [], char_budget=BUDGET, today=TODAY
    )
    assert len(model.edits) == 1
    assert "TAIL_MARKER_LINE" in model.edits[0]["view"]
    assert HEADING_RULE in model.edits[0]["user"]
    assert "## Reasoning Effort" in result["body"]


def test_editing_edit_feedback_states_that_new_headings_go_only_in_append():
    exc = ModelError("bounded replacement changes structural headings or fence boundaries", transient=True)
    exc.diagnostic = {"operation": "bounded-edit", "issues": []}
    assert HEADING_RULE in editing._edit_feedback(exc, 4000, 6000)


# --------------------------------------------------------------------------- #
# Identical deterministic failures fail fast
# --------------------------------------------------------------------------- #


def bad_reply(user, anchor):
    identities = ast.literal_eval(re.search(r"Claim IDs to integrate or prove already present: (.+)", user)[1])
    return {
        "section_id": re.search(r"Section ID: (.+)", user)[1],
        "edits": [],
        "append": "",
        "handled_claim_ids": identities,
        "claim_dispositions": [{"id": i, "status": "integrated", "anchor": anchor} for i in identities],
        "notes": [],
    }


@pytest.mark.asyncio
async def test_editing_identical_deterministic_failure_stops_after_second_attempt():
    model = Model(route=steering_route, edit=lambda user, view, ids, sid: bad_reply(user, "text that is absent"))
    change = PageChange("g", "wiki/device.md", "update", title="Device")
    # Bounded at the second identical attempt. An unprovable claim is recorded as not integrated instead of failing the job.
    result = await editing.edit_page(
        model, "Edit", change, [record("A new fact.")], one_section_page(), [], char_budget=BUDGET, today=TODAY
    )
    assert len(model.edits) == 2
    assert result["draft_progress"]["handled_claim_ids"] == []
    assert [row["reason"] for row in result["draft_progress"]["not_integrated"].values()] == ["not-unique"]


@pytest.mark.asyncio
async def test_editing_changing_failure_keeps_retrying_and_can_succeed():
    seen = []

    def evolving(user, view, identities, section_id):
        seen.append(1)
        claim = claims_in(user)[identities[0]]
        if len(seen) == 1:
            return bad_reply(user, "absent text")
        if len(seen) == 2:  # different diagnostic: the edit anchor is now the failing field
            reply = bad_reply(user, claim)
            reply["edits"] = [{"old": "not in view", "new": "x"}]
            return reply
        return {
            "section_id": section_id,
            "edits": [],
            "append": claim + "\n",
            "handled_claim_ids": identities,
            "claim_dispositions": [{"id": identities[0], "status": "integrated", "anchor": claim}],
            "notes": [],
        }

    model = Model(route=steering_route, edit=evolving)
    change = PageChange("g", "wiki/device.md", "update", title="Device")
    result = await editing.edit_page(
        model, "Edit", change, [record("A new fact.")], one_section_page(), [], char_budget=BUDGET, today=TODAY
    )
    assert len(seen) == 3 and "A new fact." in result["body"]


# --------------------------------------------------------------------------- #
# Resume safety: progress written by an older editor is never replayed
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_editing_older_editor_progress_is_not_replayed(monkeypatch):
    assert editing.EDITOR_VERSION > 3
    records = [record(f"Section{i} fact cites https://s{i}.example/doc.", str(i)) for i in range(1, 6)]
    change = PageChange("g", "wiki/device.md", "update", title="Device")
    monkeypatch.setattr(editing, "EDITOR_VERSION", 3)
    old = await editing.edit_page(
        Model(route=section_route), "Device", change, records, five_section_page(), [], char_budget=BUDGET, today=TODAY
    )
    assert old["draft_progress"]["editor_version"] == 3 and old["draft_progress"]["completed_task_ids"]
    monkeypatch.undo()
    change.draft_meta, change.draft_body, change.draft_progress = old["frontmatter"], old["body"], old["draft_progress"]

    for repair in (False, True):
        model = Model(route=section_route)
        change.repair_context = {
            "failures": [{"severity": "blocker", "check": "scope", "detail": "cites https://s3.example/doc"}]
        }
        result = await editing.edit_page(
            model,
            "Device",
            change,
            records,
            frontmatter.build(old["frontmatter"], old["body"]),
            [],
            char_budget=BUDGET,
            today=TODAY,
            repair=repair,
        )
        assert result["draft_progress"]["editor_version"] == editing.EDITOR_VERSION
        # Nothing was carried: every claim was routed and proven again.
        assert model.route_calls >= 1 and len(model.edits) >= 5
        assert result["body"] == old["body"]


@pytest.mark.asyncio
async def test_editing_interrupted_targeted_repair_resumes_without_widening():
    change, records = await drafted_five_sections()
    change.repair_context = {
        "failures": [
            {"severity": "blocker", "check": "scope", "detail": "draft cites excluded source https://s2.example/doc"},
            {"severity": "blocker", "check": "scope", "detail": "draft cites excluded source https://s4.example/doc"},
        ]
    }
    snapshots = []

    def outage_on_second(user, view, identities, section_id):
        if len(snapshots_model.edits) == 2:
            raise ModelError("inference outage")
        return None

    snapshots_model = Model(route=section_route, edit=outage_on_second)

    def retain(result):
        change.draft_body, change.draft_meta, change.draft_progress = (
            result["body"],
            result["frontmatter"],
            result["draft_progress"],
        )
        snapshots.append(result)

    page = frontmatter.build(change.draft_meta, change.draft_body)
    with pytest.raises(ModelError, match="outage"):
        await editing.edit_page(
            snapshots_model,
            "Device\nValidation failures to repair: []",
            change,
            records,
            page,
            [],
            char_budget=BUDGET,
            today=TODAY,
            repair=True,
            progress=retain,
        )
    assert len(snapshots_model.edits) == 2
    resumed = Model(route=section_route)
    await editing.edit_page(
        resumed,
        "Device\nValidation failures to repair: []",
        change,
        records,
        frontmatter.build(change.draft_meta, change.draft_body),
        [],
        char_budget=BUDGET,
        today=TODAY,
        repair=True,
    )
    assert resumed.route_calls == 0 and len(resumed.edits) == 1
    assert "https://s4.example/doc" in resumed.edits[0]["view"]


@pytest.mark.asyncio
async def test_editing_resume_after_flagged_url_was_removed_stays_targeted():
    # The first edit removes the flagged URL, so recomputing the
    # target set from the current draft finds nothing and used to widen to 5 calls.
    change, records = await drafted_five_sections()
    change.repair_context = {
        "failures": [
            {
                "severity": "blocker",
                "check": "scope",
                "detail": "draft cites excluded source https://s2.example/doc (rejected)",
            },
            {
                "severity": "blocker",
                "check": "scope",
                "detail": "draft cites excluded source https://s4.example/doc (rejected)",
            },
        ]
    }

    def fix_then_outage(user, view, identities, section_id):
        if len(first.edits) == 2:
            raise ModelError("inference outage")
        old = "Section2 fact cites https://s2.example/doc."
        assert old in view
        return {
            "section_id": section_id,
            "edits": [{"old": old, "new": "Section2 fact."}],
            "append": "",
            "handled_claim_ids": [],
            "claim_dispositions": [],
            "notes": [],
        }

    first = Model(route=section_route, edit=fix_then_outage)

    def retain(result):
        change.draft_body, change.draft_meta, change.draft_progress = (
            result["body"],
            result["frontmatter"],
            result["draft_progress"],
        )

    with pytest.raises(ModelError, match="outage"):
        await editing.edit_page(
            first,
            "Device\nValidation failures to repair: []",
            change,
            records,
            frontmatter.build(change.draft_meta, change.draft_body),
            [],
            char_budget=BUDGET,
            today=TODAY,
            repair=True,
            progress=retain,
        )
    assert "https://s2.example/doc" not in change.draft_body
    resumed = Model(route=section_route)
    await editing.edit_page(
        resumed,
        "Device\nValidation failures to repair: []",
        change,
        records,
        frontmatter.build(change.draft_meta, change.draft_body),
        [],
        char_budget=BUDGET,
        today=TODAY,
        repair=True,
    )
    assert resumed.route_calls == 0 and len(resumed.edits) == 1
    assert "https://s4.example/doc" in resumed.edits[0]["view"]


@pytest.mark.asyncio
async def test_editing_resume_with_different_failures_recomputes_the_targets():
    change, records = await drafted_five_sections()
    change.repair_context = {
        "failures": [{"severity": "blocker", "check": "scope", "detail": "cites https://s2.example/doc"}]
    }
    done = Model(route=section_route)

    def retain(result):
        change.draft_body, change.draft_meta, change.draft_progress = (
            result["body"],
            result["frontmatter"],
            result["draft_progress"],
        )

    await editing.edit_page(
        done,
        "Device\nValidation failures to repair: []",
        change,
        records,
        frontmatter.build(change.draft_meta, change.draft_body),
        [],
        char_budget=BUDGET,
        today=TODAY,
        repair=True,
        progress=retain,
    )
    assert len(done.edits) == 1
    change.repair_context = {
        "failures": [{"severity": "blocker", "check": "scope", "detail": "cites https://s5.example/doc"}]
    }
    again = Model(route=section_route)
    await editing.edit_page(
        again,
        "Device\nValidation failures to repair: []",
        change,
        records,
        frontmatter.build(change.draft_meta, change.draft_body),
        [],
        char_budget=BUDGET,
        today=TODAY,
        repair=True,
    )
    assert [edit["section_id"] for edit in again.edits] and all(
        "https://s5.example/doc" in e["view"] for e in again.edits
    )


def test_editing_view_scoped_integrated_anchor_cannot_duplicate_a_fact_elsewhere_in_the_section():
    # The append repeats a fact that sits outside the shown slice.
    text = "## S\nA line.\n" + "".join(f"- row {i}\n" for i in range(5)) + "Dup fact.\n"
    unit = editing.Unit("section-x", "S", 2, text)
    view = "## S\nA line.\n"
    result = {
        "section_id": "section-x",
        "edits": [],
        "append": "Dup fact.\n",
        "handled_claim_ids": ["c"],
        "claim_dispositions": [{"id": "c", "status": "integrated", "anchor": "Dup fact."}],
    }
    with pytest.raises(ModelError, match="unique actual claim anchor") as whole:
        editing.apply_edits(unit, result, {"c"}, max_output_chars=6000)
    with pytest.raises(ModelError, match="unique actual claim anchor") as scoped:
        editing.apply_edits(unit, result, {"c"}, max_output_chars=6000, view=view)
    issues = scoped.value.diagnostic["issues"]
    assert any(i["field"] == "claim_dispositions[0].anchor" and i.get("occurrences") == 2 for i in issues)
    assert whole.value.diagnostic["issues"] == issues


def test_editing_view_scoped_already_present_anchor_still_uses_the_shown_view():
    text = "## S\nShared.\n" + "".join(f"- row {i}\n" for i in range(5)) + "Shared.\n"
    unit = editing.Unit("section-x", "S", 2, text)
    result = {
        "section_id": "section-x",
        "edits": [],
        "append": "",
        "handled_claim_ids": ["c"],
        "claim_dispositions": [{"id": "c", "status": "already_present", "anchor": "Shared."}],
    }
    assert editing.apply_edits(unit, result, {"c"}, max_output_chars=6000, view="## S\nShared.\n") == text


# --------------------------------------------------------------------------- #
# Whole-token targeting, full failure identity, intent-bound carry
# --------------------------------------------------------------------------- #


def test_editing_url_targeting_matches_whole_urls_not_prefixes():
    names = editing._names
    assert not names("see https://a.example/xy for details", "https://a.example/x")
    assert not names("see https://a.example/x/deeper", "https://a.example/x")
    assert not names("see https://a.example/x?q=1", "https://a.example/x")
    assert not names("see https://a.example/x.html", "https://a.example/x")
    assert names("see https://a.example/x for details", "https://a.example/x")
    assert names("(https://a.example/x).", "https://a.example/x")
    assert names("end https://a.example/x.", "https://a.example/x")
    assert names("[S](https://a.example/x)", "https://a.example/x")
    assert names("see [[Page|alias]]", "[[Page") and names("see [[Page]]", "[[Page")
    assert not names("see [[Page two]]", "[[Page")
    assert names("a `quoted span` here", "quoted span")


@pytest.mark.asyncio
async def test_editing_targeted_repair_ignores_views_citing_only_a_longer_allowed_url():
    change, records = await drafted_five_sections()
    body = change.draft_body.replace("https://s4.example/doc", "https://s2.example/docs-allowed")
    change.draft_body = body
    change.draft_progress["body_hash"] = editing.hashlib.sha256(body.encode()).hexdigest()
    change.repair_context = {
        "failures": [
            {
                "severity": "blocker",
                "check": "scope",
                "detail": "draft cites excluded source https://s2.example/doc (rejected)",
            }
        ]
    }
    repair = Model(route=section_route)
    await editing.edit_page(
        repair,
        "Device",
        change,
        records,
        frontmatter.build(change.draft_meta, body),
        [],
        char_budget=BUDGET,
        today=TODAY,
        repair=True,
    )
    assert len(repair.edits) == 1 and "## Section2" in repair.edits[0]["view"]


@pytest.mark.asyncio
async def test_editing_failure_that_changed_only_in_occurrences_does_not_trip_fast_fail():
    page = one_section_page("Twin abcd. Twin abcd.\n")
    seen = []

    def evolving(user, view, identities, section_id):
        seen.append(1)
        claim = claims_in(user)[identities[0]]
        if len(seen) == 1:  # same field, reason, type and length: 0 occurrences
            return bad_reply(user, "wxyz")
        if len(seen) == 2:  # only the occurrence count changed (now 2)
            return bad_reply(user, "abcd")
        return {
            "section_id": section_id,
            "edits": [],
            "append": claim + "\n",
            "handled_claim_ids": identities,
            "claim_dispositions": [{"id": identities[0], "status": "integrated", "anchor": claim}],
            "notes": [],
        }

    model = Model(route=steering_route, edit=evolving)
    change = PageChange("g", "wiki/device.md", "update", title="Device")
    result = await editing.edit_page(
        model, "Edit", change, [record("A new fact.")], page, [], char_budget=BUDGET, today=TODAY
    )
    assert len(seen) == 3 and "A new fact." in result["body"]


@pytest.mark.asyncio
async def test_editing_carried_work_is_invalidated_when_the_intent_changes():
    change, records = await drafted_five_sections()
    failures = {"failures": [{"severity": "blocker", "check": "scope", "detail": "cites https://s3.example/doc"}]}
    page = frontmatter.build(change.draft_meta, change.draft_body)
    change.repair_context = failures
    same = Model(route=section_route)
    await editing.edit_page(
        same,
        'Device\nValidation failures to repair: [{"check": "other"}]',
        change,
        records,
        page,
        [],
        char_budget=BUDGET,
        today=TODAY,
        repair=True,
    )
    assert same.route_calls == 0 and len(same.edits) == 1  # failure text alone does not invalidate
    other = Model(route=section_route)
    await editing.edit_page(
        other,
        "A different intent\nValidation failures to repair: []",
        change,
        records,
        page,
        [],
        char_budget=BUDGET,
        today=TODAY,
        repair=True,
    )
    assert other.route_calls >= 1 and len(other.edits) >= 5  # nothing carried


# --------------------------------------------------------------------------- #
# A scope blocker targets the unit holding the offending sentence
# --------------------------------------------------------------------------- #

REJECTED = "https://developers.openai.com/api/docs/guides/tools"
SCOPE_POLICY = {"include": [{"url": "https://developers.openai.com/api/docs/guides/tools-computer-use"}]}


def legacy_cites_page():
    legacy = "".join(f"## Legacy{i}\n\nLegacy fact {i} is described at {REJECTED}.\n\n" for i in range(1, 4))
    body = "# Device\n\nIntro.\n\n" + legacy + "## Changed\n\nOriginal changed.\n\n"
    return frontmatter.build({"schema_version": 1, "privacy": "internal", "title": "Device"}, body)


async def drafted_with_one_new_citation():
    new = record(f"Changed fact is described at {REJECTED}.", "n")
    change = PageChange("g", "wiki/device.md", "update", title="Device")
    model = Model(
        route=lambda claim, frame: {
            "section_id": next(r["section_id"] for r in frame["sections"] if r["heading_path"].endswith("Changed"))
        }
    )
    result = await editing.edit_page(
        model, "Device", change, [new], legacy_cites_page(), [], char_budget=BUDGET, today=TODAY
    )
    change.draft_meta, change.draft_body, change.draft_progress = (
        result["frontmatter"],
        result["body"],
        result["draft_progress"],
    )
    return change, [new]


@pytest.mark.asyncio
async def test_repair_targets_only_the_unit_with_the_new_citing_sentence():
    from doclayer.curator import capacity

    change, records = await drafted_with_one_new_citation()
    original_body = frontmatter.parse(legacy_cites_page())[1]
    failures = capacity.scope_failures(SCOPE_POLICY, [REJECTED], change.draft_body, original_body)
    assert len(failures) == 1
    # The detail names the offending sentence and the unit that holds it.
    assert f"Changed fact is described at {REJECTED}." in failures[0].detail
    assert "Changed" in failures[0].detail and "Legacy" not in failures[0].detail
    change.repair_context = {
        "failures": [{"severity": "blocker", "check": f.check, "detail": f.detail} for f in failures]
    }
    repair = Model(route=lambda claim, frame: {})
    await editing.edit_page(
        repair,
        "Device",
        change,
        records,
        frontmatter.build(change.draft_meta, change.draft_body),
        [],
        char_budget=BUDGET,
        today=TODAY,
        repair=True,
    )
    assert repair.route_calls == 0
    assert len(repair.edits) == 1
    assert "Changed fact is described at" in repair.edits[0]["view"]
    assert "Legacy fact" not in repair.edits[0]["view"]


@pytest.mark.parametrize(
    "text",
    [
        "See https://proxy.example/redirect?u=https://developers.example/doc for details.",
        "See https://developers.example/doc/extra for details.",
        "See https://developers.example/doc-two for details.",
    ],
)
def test_editing_and_scope_check_agree_on_whole_url_matching(text):
    from doclayer.curator import capacity

    url = "https://developers.example/doc"
    assert editing._names(text, url) == capacity.url_cited(text, url) == False  # noqa: E712


@pytest.mark.parametrize(
    "text",
    [
        "See (https://developers.example/doc), or [x](https://developers.example/doc).",
        "See https://developers.example/doc.",
    ],
)
def test_editing_names_a_whole_url_the_way_the_scope_check_does(text):
    from doclayer.curator import capacity

    url = "https://developers.example/doc"
    assert editing._names(text, url) is capacity.url_cited(text, url) is True


@pytest.mark.asyncio
async def test_a_pre_v4_draft_gets_a_full_first_repair_not_a_targeted_one():
    # Handled-claim state from an older editor cannot be trusted, so the first repair
    # after a resume re-audits every region; budget extensions for such jobs must allow it.
    change, records = await drafted_five_sections()
    change.draft_progress["editor_version"] = 3
    change.draft_progress.pop("carry_binding", None)
    change.repair_context = {
        "failures": [
            {
                "severity": "blocker",
                "check": "scope",
                "detail": "draft cites excluded or semantically restricted source https://s3.example/doc (outside source inclusion policy)",
            }
        ]
    }
    repair = Model(route=section_route)
    await editing.edit_page(
        repair,
        "Device",
        change,
        records,
        frontmatter.build(change.draft_meta, change.draft_body),
        [],
        char_budget=BUDGET,
        today=TODAY,
        repair=True,
    )
    assert len(repair.edits) >= 5


# --------------------------------------------------------------------------- #
# The quoted sentence is found however the draft spaces it
# --------------------------------------------------------------------------- #

URL_A, URL_B, URL_C = (
    f"https://developers.openai.com/api/docs/guides/{n}"
    for n in ("reasoning", "deployment-checklist", "model-selection")
)
POLICY_3 = {"include": [{"url": "https://developers.openai.com/api/docs/guides/tools-computer-use"}]}
CHANGED = {
    3: f"*   **GPT-6.1 Sol:** best for hard reasoning, see {URL_A}.",
    7: f"*   **GPT-6 Luna:**  cheap  and fast, see {URL_B}.",
    9: f"Selection   context  is   described at {URL_C}.",
    11: f"- Plain new item cites {URL_A}.",
}


def gpt6_page(changed=False):
    parts = ["# GPT-6 Astra\n\nIntro.\n"]
    for i in range(14):
        body = (
            f"Legacy fact {i} is described at {[URL_A, URL_B, URL_C][i % 3]}.\n"
            if i % 2 == 0
            else f"Original text {i}.\n"
        )
        if changed and i in CHANGED:
            body += "\n" + CHANGED[i] + "\n"
        parts.append(f"## Topic{i}\n\n{body}")
    return frontmatter.build({"schema_version": 1, "privacy": "internal", "title": "GPT-6 Astra"}, "\n".join(parts))


@pytest.mark.asyncio
async def test_real_shape_targets_exactly_the_units_with_the_changed_sentences():
    from doclayer.curator import capacity

    original = gpt6_page()
    drafted = gpt6_page(changed=True)
    original_body, draft_body = frontmatter.parse(original)[1], frontmatter.parse(drafted)[1]
    failures = capacity.scope_failures(POLICY_3, [URL_A, URL_B, URL_C], draft_body, original_body)
    assert len(failures) == 4
    # The sentence is quoted as written, spacing included.
    assert any(
        "*   **GPT-6.1 Sol:**" in f.detail or "*   **GPT-6.1 Sol:**" in __import__("json").dumps(f.detail)
        for f in failures
    )
    change = PageChange("g", "wiki/gpt6.md", "update", title="GPT-6 Astra")
    change.draft_meta, change.draft_body = frontmatter.parse(drafted)[0], draft_body
    seed = Model(route=lambda claim, frame: {})
    base = await editing.edit_page(seed, "GPT", change, [], drafted, [], char_budget=BUDGET, today=TODAY)
    change.draft_meta, change.draft_body, change.draft_progress = (
        base["frontmatter"],
        base["body"],
        base["draft_progress"],
    )
    change.repair_context = {
        "failures": [{"severity": "blocker", "check": f.check, "detail": f.detail} for f in failures]
    }
    tokens = editing._flagged_tokens(change, draft_body)
    assert tokens is not None and len(tokens) == 4
    repair = Model(route=lambda claim, frame: {})
    await editing.edit_page(repair, "GPT", change, [], drafted, [], char_budget=BUDGET, today=TODAY, repair=True)
    shown = "\n".join(edit["view"] for edit in repair.edits)
    assert len(repair.edits) == 4
    for unit in CHANGED:
        assert f"## Topic{unit}" in shown
    for unit in set(range(14)) - set(CHANGED):
        assert f"## Topic{unit}\n" not in shown


@pytest.mark.parametrize(
    ("sentence", "text"),
    [
        ("*   **GPT-6.1 Sol:** best.", "## T\n\n* **GPT-6.1 Sol:** best.\n"),
        ("doubled  internal   spaces here.", "doubled internal spaces here."),
        ("tab\tseparated words.", "tab separated words."),
    ],
)
def test_editing_matches_sentences_whitespace_insensitively(sentence, text):
    assert editing._names(text, sentence) and editing._names(sentence, text.strip().splitlines()[-1].strip())


def test_scope_failure_labels_the_innermost_heading():
    from doclayer.curator import capacity

    base = "# P\n\n## API & Configuration\n\nIntro.\n\n### Reasoning Effort\n\nOld.\n\n### Other\n\nOld two.\n"
    sentence = f"Astra supports `minimal` effort, see {URL_A}."
    separate = base.replace("Old.\n", f"Old.\n\n{sentence}\n")
    same_paragraph = base.replace("### Reasoning Effort\n\nOld.\n", f"### Reasoning Effort\nOld.\n{sentence}\n")
    mid_paragraph = base.replace(
        "Intro.\n\n### Reasoning Effort\n\nOld.\n", f"Intro.\n### Reasoning Effort\nOld.\n{sentence}\n"
    )
    for draft in (separate, same_paragraph, mid_paragraph):
        failures = capacity.scope_failures(POLICY_3, [URL_A], draft, base)
        assert len(failures) == 1 and "unit: Reasoning Effort;" in failures[0].detail
    h2 = base.replace("Intro.\n", f"Intro.\n\n{sentence}\n")
    assert "unit: API & Configuration;" in capacity.scope_failures(POLICY_3, [URL_A], h2, base)[0].detail


def test_heading_above_a_sentence_wrapped_across_lines_ignores_headings_below_it():
    from doclayer.curator import capacity

    sentence = f"Astra supports `minimal`\neffort,   see {URL_A}\nfor details."
    paragraph = f"### Reasoning Effort\nIntro line.\n{sentence}\n### Later heading\nMore text."
    assert capacity._heading_above(paragraph, sentence, "Outer") == "Reasoning Effort"
    # Doubled spaces and the wrap are matched whitespace-insensitively; a heading
    # that starts after the sentence never applies.
    flat = " ".join(sentence.split())
    assert capacity._heading_above(paragraph, flat, "Outer") == "Reasoning Effort"
    assert capacity._heading_above(f"{sentence}\n### Later heading", flat, "Outer") == "Outer"
    assert capacity._heading_above(f"## First\n### Second\n{sentence}\n### Third", flat, "Outer") == "Second"


def test_heading_above_a_sentence_absent_from_the_paragraph_has_no_label():
    from doclayer.curator import capacity

    paragraph = "### Reasoning Effort\nIntro line.\n### Later heading\nMore text."
    # Not the paragraph's last heading, nor the one in force before it: no label,
    # which the caller renders as its no-heading "(untitled)" unit.
    assert capacity._heading_above(paragraph, "A sentence that is not here.", "Outer") == ""
    assert capacity._heading_above(paragraph, "   ", "Outer") == ""
