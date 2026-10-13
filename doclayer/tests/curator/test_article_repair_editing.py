"""Real reviewed locations replace synthetic repair assertions."""

import hashlib
import json
import re
import subprocess

import pytest

from doclayer.curator import editing
from doclayer.curator.model import ModelError
from doclayer.curator.state import Evidence, PageChange


def change_for(body, locations):
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
                "check": "article-evidence",
                "detail": detail,
                "section_id": sections[index]["id"],
                "anchor": anchor,
            }
            for index, anchor, detail in locations
        ],
    }
    return PageChange("g", "wiki/device.md", "update", draft_body=body, check_progress={"article_review": review})


def result(user, edits=(), append=""):
    return {
        "section_id": re.search(r"Section ID: (.+)", user)[1],
        "edits": [{"old": old, "new": new} for old, new in edits],
        "append": append,
        "handled_claim_ids": [],
        "claim_dispositions": [],
        "notes": [],
    }


async def run(client, change, body, evidence=(), progress=None, budget=12000):
    return await editing.edit_page(
        client,
        "Device",
        change,
        list(evidence),
        body,
        [],
        char_budget=budget,
        today="2026-10-03",
        repair=True,
        progress=progress,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["", "\n"])
async def test_reviewed_heading_only_parent_preserves_nested_content(ending):
    body = "# Device\n## Available to everyone\n### Installation\nOperator command stays here.\n"
    change = change_for(body, [(1, "## Available to everyone" + ending, "Only enterprise edition is supported.")])

    class Client:
        async def chat_json(self, system, user):
            assert not user.startswith("BOUNDED EDIT CLAIM ROUTING")
            assert "Only enterprise edition is supported." in user
            assert "Claim IDs to integrate or prove already present: []" in user
            return result(user, [("## Available to everyone", "## Enterprise edition only")])

    repaired = await run(Client(), change, body)
    assert repaired["body"] == body.replace("## Available to everyone", "## Enterprise edition only")
    assert repaired["draft_progress"]["heading_renames"][0]["old"] == "## Available to everyone"
    assert "claims" not in change.check_progress and "verdicts" not in change.check_progress


@pytest.mark.asyncio
async def test_review_location_evidence_is_support_only_without_forced_claim_ids():
    body = "# Device\n## Unrelated\nKeep dated operator note.\n## Retention\nEnterprise retention is 3 days.\n"
    change = change_for(body, [(2, "Enterprise retention is 3 days.", "Correct enterprise retention to 7 days.")])
    evidence = Evidence(
        "source",
        "Manual",
        "#Retention",
        "quote",
        "Enterprise retention is 7 days.",
        quote="Enterprise retention is 7 days.",
        url="https://device.example/manual",
        support_status="supported",
    )
    calls = []

    class Client:
        async def chat_json(self, system, user):
            calls.append(user)
            assert "support-only for correcting existing assertions" in user
            assert "Enterprise retention is 7 days." in user
            assert "Claim IDs to integrate or prove already present: []" in user
            assert "Current section view:\n## Retention" in user
            return result(
                user,
                [
                    (
                        "Enterprise retention is 3 days.",
                        "Enterprise retention is 7 days. [Manual](https://device.example/manual)",
                    )
                ],
            )

    repaired = await run(Client(), change, body, [evidence])
    assert len(calls) == 1 and "Keep dated operator note." in repaired["body"]
    assert repaired["draft_progress"]["handled_claim_ids"] == []


@pytest.mark.asyncio
async def test_section_omission_appends_at_explicit_location():
    body = "# Device\n## Setup\nExisting setup.\n## Limitations\nExisting limitation.\n"
    change = change_for(body, [(2, None, "Add documented offline limitation.")])

    class Client:
        async def chat_json(self, system, user):
            assert "Current section view:\n## Limitations" in user
            return result(user, append="Offline updates are unsupported. [Manual](https://device.example/manual)")

    repaired = await run(Client(), change, body)
    assert repaired["body"].startswith(body) and repaired["body"].count("Offline updates") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("malformation", ["stale_body", "foreign_section", "wrong_offsets", "foreign_anchor"])
async def test_invalid_review_location_rejected_before_model(malformation):
    body = "# Device\n## Retention\nRetention is 3 days.\n"
    change = change_for(body, [(1, "Retention is 3 days.", "Correct retention.")])
    record = change.check_progress["article_review"]
    if malformation == "stale_body":
        record["body_sha256"] = "0" * 64
    elif malformation == "foreign_section":
        record["failures"][0]["section_id"] = "other-section"
    elif malformation == "wrong_offsets":
        record["sections"][1]["start"] += 1
    else:
        record["failures"][0]["anchor"] = "Not in article."

    class Client:
        async def chat_json(self, *args):
            pytest.fail("invalid review cannot invoke inference")

    with pytest.raises(ModelError, match="article repair"):
        await run(Client(), change, body)


@pytest.mark.asyncio
async def test_heading_permissions_do_not_allow_neutral_rename_or_level_change():
    body = "# Device\n## Wrong scope\nOld value.\n"
    change = change_for(body, [(1, "Old value.", "Correct value, preserve headings.")])
    # Archived clause diagnostics cannot authorize an unrelated heading edit.
    change.check_progress.update(
        {"claims": {"old": {"span": "## Wrong scope"}}, "verdicts": {"old": {"supported": False}}}
    )

    class Client:
        async def chat_json(self, system, user):
            return result(user, [("## Wrong scope", "## New heading")])

    with pytest.raises(ModelError, match="structural headings"):
        await run(Client(), change, body)
    authorized = change_for(body, [(1, "## Wrong scope", "Correct factual scope.")])

    class LevelClient:
        async def chat_json(self, system, user):
            return result(user, [("## Wrong scope", "### New heading")])

    with pytest.raises(ModelError, match="structural headings"):
        await run(LevelClient(), authorized, body)


@pytest.mark.asyncio
async def test_partial_repair_restart_keeps_original_locations_without_replaying_heading():
    body = "# Device\n## Incorrect heading\n## Retention\nRetention is 3 days.\n"
    change = change_for(
        body,
        [
            (1, "## Incorrect heading", "Correct factual heading."),
            (2, "Retention is 3 days.", "Correct retention to 7 days."),
        ],
    )

    def retain(value):
        change.draft_body = value["body"]
        change.draft_progress = value["draft_progress"]

    class First:
        async def chat_json(self, system, user):
            if "Current section view:\n## Incorrect heading" in user:
                return result(user, [("## Incorrect heading", "## Correct heading")])
            raise ModelError("transport unavailable", transient=True)

    with pytest.raises(ModelError, match="transport unavailable"):
        await run(First(), change, body, progress=retain)
    assert "## Correct heading" in change.draft_body and len(change.draft_progress["completed_task_ids"]) == 1
    calls = []

    class Resumed:
        async def chat_json(self, system, user):
            calls.append(user)
            assert "Current section view:\n## Retention" in user
            return result(user, [("Retention is 3 days.", "Retention is 7 days.")])

    final = await run(Resumed(), change, change.draft_body, progress=retain)
    assert len(calls) == 1
    assert final["body"] == body.replace("## Incorrect heading", "## Correct heading").replace("3 days", "7 days")
    assert len(final["draft_progress"]["heading_renames"]) == 1 and "claims" not in change.check_progress


@pytest.mark.asyncio
async def test_oversized_complete_feedback_fails_before_inference_without_clipping():
    body = "# Device\n## Retention\nRetention is 3 days.\n"
    change = change_for(body, [(1, None, "Preserve every qualification. " * 1000)])

    class Client:
        async def chat_json(self, *args):
            pytest.fail("oversized frame cannot invoke inference")

    with pytest.raises(ModelError, match="bounded edit task exceeds input context"):
        await run(Client(), change, body, budget=4500)


@pytest.mark.asyncio
async def test_modified_saved_locations_do_not_authorize_restart_changes():
    body = "# Device\n## Incorrect heading\n## Retention\nRetention is 3 days.\n"
    change = change_for(
        body,
        [(1, "## Incorrect heading", "Correct factual heading."), (2, "Retention is 3 days.", "Correct retention.")],
    )

    def retain(value):
        change.draft_body = value["body"]
        change.draft_progress = value["draft_progress"]

    class First:
        async def chat_json(self, system, user):
            if "Current section view:\n## Incorrect heading" in user:
                return result(user, [("## Incorrect heading", "## Correct heading")])
            raise ModelError("transport unavailable", transient=True)

    with pytest.raises(ModelError):
        await run(First(), change, body, progress=retain)
    change.draft_progress["article_repair_locations"][1]["anchor"] = "## Retention"

    class Client:
        async def chat_json(self, *args):
            pytest.fail("altered checkpoint cannot invoke inference")

    with pytest.raises(ModelError, match="saved article repair locations binding mismatch"):
        await run(Client(), change, change.draft_body)


@pytest.mark.asyncio
async def test_fresh_review_after_parent_rename_maps_current_sections_to_saved_identities():
    body = "# Wrong scope\n## Retention\nRetention is 3 days.\n"
    change = change_for(body, [(0, "# Wrong scope", "Correct the factual parent heading.")])

    class First:
        async def chat_json(self, system, user):
            return result(user, [("# Wrong scope", "# Correct scope")])

    first = await run(First(), change, body)
    change.draft_body = first["body"]
    change.draft_progress = first["draft_progress"]
    fresh = change_for(first["body"], [(1, "Retention is 3 days.", "Correct retention to 7 days.")])
    change.check_progress = fresh.check_progress
    original_child_id = editing.page_units(body)[1].id

    class Second:
        async def chat_json(self, system, user):
            assert re.search(r"Section ID: (.+)", user)[1] == original_child_id
            assert "Current section view:\n## Retention" in user
            return result(user, [("Retention is 3 days.", "Retention is 7 days.")])

    final = await run(Second(), change, first["body"])
    assert final["body"] == "# Correct scope\n## Retention\nRetention is 7 days.\n"
    assert final["draft_progress"]["heading_renames"] == first["draft_progress"]["heading_renames"]


@pytest.mark.asyncio
async def test_preservation_only_failure_restores_actual_canonical_fact_without_new_evidence(checkout):
    original = "# Device\nEnterprise edition only.\n## Metrics\nUnder laboratory conditions, Device found 17 severe defects and three previously unknown defects.\n"
    candidate = original.replace(
        "Under laboratory conditions, Device found 17 severe defects and three previously unknown defects.",
        "Device reported defects.",
    )
    path = checkout / "wiki" / "device.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(original)
    subprocess.run(["git", "add", "wiki/device.md"], cwd=checkout, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Canonical metrics fixture"], cwd=checkout, check=True, capture_output=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    blob = subprocess.check_output(["git", "rev-parse", commit + ":wiki/device.md"], cwd=checkout, text=True).strip()
    change = change_for(candidate, [])
    preservation = change_for(
        candidate, [(1, "Device reported defects.", "Restore the old quantities and laboratory conditions.")]
    ).check_progress["article_review"]
    preservation.update(
        original_body=original,
        original_meta={"title": "Device", "edition": "Enterprise"},
        canonical_binding={"commit": commit, "blob": blob, "path": "wiki/device.md"},
    )
    change.check_progress["existing_preservation"] = preservation
    calls = []

    class Client:
        async def chat_json(self, system, user):
            calls.append(user)
            context = json.loads(user.split("Canonical retention context: ", 1)[1].split("\n", 1)[0])[
                "existing_preservation"
            ]
            assert context["canonical_binding"] == {"commit": commit, "blob": blob, "path": "wiki/device.md"}
            assert context["original_body_sha256"] == hashlib.sha256(original.encode()).hexdigest()
            assert context["original_meta"]["edition"] == "Enterprise"
            assert "".join(row["text"] for row in context["selected_original_units"]) == original
            assert "not new factual source authority" in context["role"]
            assert "review_namespace" in user and "existing_preservation" in user
            assert "Claim IDs to integrate or prove already present: []" in user
            return result(
                user,
                [
                    (
                        "Device reported defects.",
                        "Under laboratory conditions, Device found 17 severe defects and three previously unknown defects.",
                    )
                ],
            )

    repaired = await run(Client(), change, candidate)
    assert len(calls) == 1 and repaired["body"] == original
    assert change.check_progress["article_review"]["failures"] == []
    assert "claims" not in change.check_progress and "verdicts" not in change.check_progress
    assert repaired["draft_progress"]["handled_claim_ids"] == []


@pytest.mark.asyncio
async def test_stale_preservation_locations_are_not_used_beside_current_review():
    body = "# Device\n## Retention\nRetention is 3 days.\n"
    change = change_for(body, [(1, "Retention is 3 days.", "Correct retention.")])
    stale = change_for(body, [(0, "# Device", "Must not apply stale heading repair.")]).check_progress["article_review"]
    stale["body_sha256"] = "0" * 64
    change.check_progress["existing_preservation_fresh"] = stale

    class Client:
        async def chat_json(self, system, user):
            assert "Must not apply stale heading repair." not in user
            assert "existing_preservation_fresh" not in user
            return result(user, [("Retention is 3 days.", "Retention is 7 days.")])

    repaired = await run(Client(), change, body)
    assert repaired["body"] == body.replace("3 days", "7 days")
    assert change.check_progress["existing_preservation_fresh"] == stale


@pytest.mark.asyncio
async def test_preservation_location_without_actual_baseline_fails_before_model():
    body = "# Device\n## Metrics\nReported defects.\n"
    change = change_for(body, [])
    change.check_progress["existing_preservation"] = change_for(
        body, [(1, None, "Restore old metrics.")]
    ).check_progress["article_review"]

    class Client:
        async def chat_json(self, *args):
            pytest.fail("missing original baseline cannot be fabricated")

    with pytest.raises(ModelError, match="canonical retention baseline unavailable"):
        await run(Client(), change, body)
