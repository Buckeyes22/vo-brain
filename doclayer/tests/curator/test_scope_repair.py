"""Scope repair: ordinary jobs, one H1, and moved or renamed sections.

- past corrections never exempt anything in an ordinary job;
- a page keeps one H1; moved and renamed sections are restored by identity.
Citations are not repaired deterministically; they go to the model repair.
"""

import pytest
from test_scope_repair_restores import PAGE, page_job

from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import PageChange
from doclayer.curator.validate import check_title, check_update_scope, restore_dropped_sections, restore_titles

P = PAGE


def headings(body):
    return [line for line in body.splitlines() if line.startswith("#")]


# --------------------------------------------------------------------------- #
# Past corrections never exempt anything in an ordinary job
# --------------------------------------------------------------------------- #

RATE_BASE = (
    "# T\n\n## A\n\nAlpha words one two three four.\n\n## Rate Limits\n\nFive hundred requests per minute "
    "on tier one.\n\n## End\n\nEnd words one two three four.\n"
)
RATE_DROPPED = "# T\n\n## A\n\nAlpha words one two three four.\n\n## End\n\nEnd words one two three four.\n"


def test_a_past_rename_correction_never_exempts_a_later_drop():
    # Ordinary jobs get no own correction: the dropped section is a blocker and is restored.
    assert len(check_update_scope({}, RATE_BASE, {}, RATE_DROPPED, P)) == 1
    assert restore_dropped_sections(RATE_BASE, RATE_DROPPED)[1] == ["Rate Limits"]


def test_a_past_retitle_never_unlocks_a_later_rename():
    change = PageChange(
        "g", P, "update", title="New Name", draft_body="# Other\n\nText.\n", draft_meta={"title": "Other"}
    )
    base = "# New Name\n\nText.\n"
    assert len(check_title(change, {"title": "New Name"}, base)) == 2
    assert restore_titles(change, {"title": "New Name"}, base) == ["page H1", "frontmatter title"]
    assert change.draft_body.startswith("# New Name\n")


# --------------------------------------------------------------------------- #
# Past corrections never exempt anything: end to end
# --------------------------------------------------------------------------- #

RR_BASE = (
    "# Probe Page\n\nIntro words of this page.\n\n## Keep\n\nKept section words one two three.\n\n"
    "## Rate Limits\n\nFive hundred requests per minute on tier one. Batch jobs have a separate queue.\n\n"
    "## End\n\nFinal words of the page here.\n"
)
RR_DROPPED = RR_BASE.replace(
    "## Rate Limits\n\nFive hundred requests per minute on tier one. Batch jobs have a separate queue.\n\n", ""
)


@pytest.mark.asyncio
async def test_rename_in_an_ordinary_job_restores_the_section(cfg, store, checkout, monkeypatch):
    state = page_job(cfg, store, checkout, monkeypatch, RR_BASE, RR_DROPPED)
    store.add_correction(
        {
            "page": PAGE,
            "source_evidence_hash": "",
            "claim": "",
            "instruction": "rename the Limits section to Rate Limits",
        }
    )
    await Pipeline(cfg, store)._stage_validate(state)
    change = state.changes[0]
    assert change.status == "validated" and "## Rate Limits" in change.draft_body and "Batch jobs" in change.draft_body


@pytest.mark.asyncio
async def test_naming_the_page_title_never_unlocks_a_rename(cfg, store, checkout, monkeypatch):
    draft = RR_BASE.replace("# Probe Page", "# Totally Different")
    state = page_job(cfg, store, checkout, monkeypatch, RR_BASE, draft, plan="Totally Different")
    state.changes[0].draft_meta["title"] = "Totally Different"
    store.save(state)
    store.add_correction(
        {"page": PAGE, "source_evidence_hash": "", "claim": "", "instruction": "Probe Page supports 1M context, not 2M"}
    )
    await Pipeline(cfg, store)._stage_validate(state)
    change = state.changes[0]
    assert change.status == "validated"
    assert change.draft_body.startswith("# Probe Page\n") and change.title == change.draft_meta["title"] == "Probe Page"


@pytest.mark.asyncio
async def test_crlf_body_multi_section_retitle_validates_once(cfg, store, checkout, monkeypatch):
    draft = RR_BASE.replace("# Probe Page", "# New Title").replace("\n", "\r\n")
    state = page_job(cfg, store, checkout, monkeypatch, RR_BASE, draft)
    await Pipeline(cfg, store)._stage_validate(state)
    change = state.changes[0]
    assert change.status == "validated" and change.draft_body.startswith("# Probe Page\n")
    assert "\r" not in change.draft_body  # CRLF becomes LF before setext detection
    assert sum("title repair" in note for note in change.notes) <= 1


# --------------------------------------------------------------------------- #
# One H1, moved and renamed sections, code fences
# --------------------------------------------------------------------------- #


def test_a_moved_and_renamed_section_is_matched_once():
    base = (
        "# T\n\n## A\n\nAlpha words here one two three.\n\n## B\n\nBeta words here one two three.\n\n"
        "## C\n\nGamma words here one two three.\n"
    )
    draft = (
        "# T\n\n## C\n\nGamma words here one two three.\n\n## B2\n\nBeta words here one two three.\n\n"
        "## A\n\nAlpha words here one two three.\n"
    )
    body, restored, _ = restore_dropped_sections(base, draft)
    assert restored == ["B"] and headings(body) == ["# T", "## C", "## B", "## A"] and body.count("Beta words") == 1


def test_a_moved_duplicate_heading_and_a_dropped_one():
    base = "# T\n\n## X\n\n### Ex\n\nFirst example text here now.\n\n## Y\n\n### Ex\n\nSecond example text here now.\n"
    draft = "# T\n\n## Y\n\n### Ex\n\nFirst example text here now.\n\n## X\n"
    body, restored, _ = restore_dropped_sections(base, draft)
    assert restored == ["Ex"] and body.count("First example") == 1 and body.count("Second example") == 1
    assert check_update_scope({}, base, {}, body, P) == []


def test_a_reparented_subsection_is_not_dropped():
    base = "# T\n\n## A\n\nAlpha words here one two three.\n\n### a1\n\nSub words here one two three.\n\n## B\n\nBeta words here one two three.\n"
    draft = "# T\n\n## B\n\nBeta words here one two three.\n\n### a1\n\nSub words here one two three.\n\n## A\n\nAlpha words here one two three.\n"
    assert check_update_scope({}, base, {}, draft, P) == []


def test_two_new_h1s_rename_the_first_and_flag_the_second():
    base = "# T\n\nIntro words of the page here.\n\n## A\n\nAlpha words.\n"
    draft = "# X\n\nIntro words of the page here.\n\n# Y\n\nMore.\n\n## A\n\nAlpha words.\n"
    body, restored, _ = restore_dropped_sections(base, draft)
    change = PageChange("g", P, "update", title="T", draft_body=body, draft_meta={"title": "T"})
    assert restored == ["T"] and headings(body) == ["# T", "# Y", "## A"]
    assert [f.detail for f in check_title(change, {"title": "T"}, base)] == [
        f"{P}: the page has more than one H1; a page has exactly one title"
    ]


def test_a_base_without_h1_never_gains_two():
    change = PageChange(
        "g", P, "update", title="A", draft_body="# X\n\n# Y\n\n## A\n\nAlpha words.\n", draft_meta={"title": "A"}
    )
    assert [f.detail for f in check_title(change, {"title": "A"}, "## A\n\nAlpha words.\n")] == [
        f"{P}: the page has more than one H1; a page has exactly one title"
    ]


def test_deleting_only_the_h1_line_restores_it_without_a_second_intro():
    base = "# T\n\nIntro words of the page here.\n\n## A\n\nAlpha words.\n"
    body, restored, _ = restore_dropped_sections(base, "Intro words of the page here.\n\n## A\n\nAlpha words.\n")
    assert restored == ["T"] and body == base and body.count("Intro words") == 1


def test_a_fenced_heading_like_line_is_not_a_section():
    base = "# T\n\n## A\n\nAlpha words here one two three.\n\n## B\n\nBeta words here one two three.\n"
    draft = "# T\n\n## A\n\nAlpha words here one two three.\n\n```\n## B\n```\n"
    body, restored, _ = restore_dropped_sections(base, draft)
    assert restored == ["B"] and body.startswith(draft) and body.endswith("## B\n\nBeta words here one two three.\n")


def test_a_tilde_fenced_h1_is_untouched():
    change = PageChange(
        "g", P, "update", title="Old", draft_body="~~~\n# Old\n~~~\n\n# New\n\nText.\n", draft_meta={"title": "Old"}
    )
    assert restore_titles(change, {"title": "Old"}, "# Old\n\nText.\n") == ["page H1"]
    assert change.draft_body == "~~~\n# Old\n~~~\n\n# Old\n\nText.\n"
