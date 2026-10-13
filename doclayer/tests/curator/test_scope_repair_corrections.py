"""Scope repair for correction jobs, preambles and the title.

- A correction job gets none of the deterministic repairs and is validated
    with the base rules (in-order section check, no title check): every
    correction phrasing reaches the model with base-style blockers.
- The preamble is never section-restored; an H1 restore re-inserts only the
    H1 line.
- An ordinary job restores the H1 and the frontmatter title together.
"""

import pytest
from test_scope_repair_restores import PAGE, page_job

from doclayer.curator import curate
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import PageChange
from doclayer.curator.validate import (
    check_update_scope,
    restore_dropped_sections,
    restore_titles,
    run_deterministic_checks,
)

BASE = (
    "# Probe Page\n\nIntro words of this page.\n\n## Limits\n\nFive hundred requests per minute on tier one. "
    "Bursts are allowed.\n\n## Notes\n\nGeneral notes about the model family and its history.\n\n"
    "## Pricing\n\nTen dollars per million tokens.\n\n## End\n\nFinal words of the page here.\n"
)
LIMITS = "## Limits\n\nFive hundred requests per minute on tier one. Bursts are allowed.\n\n"
NOTES = "## Notes\n\nGeneral notes about the model family and its history.\n\n"
PRICING = "## Pricing\n\nTen dollars per million tokens.\n\n"


def without(*sections):
    body = BASE
    for section in sections:
        body = body.replace(section, "")
    return body


RETITLED = BASE.replace("# Probe Page", "# Totally Different")

# (instruction, claim, draft, sections the base in-order check reports dropped)
CORRECTION_CASES = [
    ("remove the notes from the Limits section", "", without(NOTES).replace(" Bursts are allowed.", ""), ["Notes"]),
    ("remove the Limits and Pricing sections", "", without(LIMITS, PRICING), ["Limits", "Pricing"]),
    ("remove the Limits section and the Pricing section", "", without(LIMITS, PRICING), ["Limits", "Pricing"]),
    ("remove the Limits section and remove the Pricing section", "", without(LIMITS, PRICING), ["Limits", "Pricing"]),
    ("remove Limits, Pricing", "", without(LIMITS, PRICING), ["Limits", "Pricing"]),
    ("rename the Limits section to Rate Limits", "", without(LIMITS), ["Limits"]),  # deleted, not renamed
    (
        "rename this page's Limits section to Rate Limits",
        "",
        RETITLED.replace("## Limits", "## Rate Limits"),
        ["Probe Page", "Limits"],
    ),
    ("retitle the page to Totally Different", "", RETITLED, ["Probe Page"]),
    ("rename the page to Totally Different", "", RETITLED, ["Probe Page"]),
    (
        "Rename the 'Limits' heading on this page to 'Rate Limits'",
        "",
        BASE.replace("## Limits", "## Rate Limits"),
        ["Limits"],
    ),
    ("remove the Wrong Section", "", without(NOTES), ["Notes"]),  # probe_corr shape
    (
        "the tier one limit is wrong",
        "Five hundred requests per minute on tier one",
        without(LIMITS),
        ["Limits"],
    ),  # partial claim
    ("In Limits, tier one is 600 per minute", "", without(LIMITS), ["Limits"]),  # partial name
    ("fix the table title in the Limits section", "", RETITLED, ["Probe Page"]),
    ("remove the note about usage limits", "", without(NOTES), ["Notes"]),
    ("remove the Notes section", "", without(NOTES, PRICING), ["Notes", "Pricing"]),  # named plus unrelated
    ("fix a figure", "", BASE.replace(PRICING, "").replace(LIMITS, PRICING + LIMITS), ["Pricing"]),  # moved: in order
]


@pytest.mark.parametrize(
    "instruction, claim, draft, dropped", CORRECTION_CASES, ids=[case[0][:40] for case in CORRECTION_CASES]
)
@pytest.mark.asyncio
async def test_a_correction_job_keeps_the_base_rules_for_every_phrasing(
    cfg, store, checkout, monkeypatch, instruction, claim, draft, dropped
):
    state = page_job(
        cfg,
        store,
        checkout,
        monkeypatch,
        BASE,
        draft,
        title="Probe Page",
        plan=f"Correction: {instruction[:40]}",
        correction={"claim": claim, "instruction": instruction},
    )
    if draft.startswith("# Totally Different"):
        state.changes[0].draft_meta["title"] = "Totally Different"
        store.save(state)

    def no_deterministic_repair(*args, **kwargs):
        raise AssertionError("a correction job never gets the deterministic repairs")

    monkeypatch.setattr(Pipeline, "_restore_structure", no_deterministic_repair)
    seen = []

    async def model_repair(client, topic, change, failures, *args, **kwargs):
        seen.append(([f["detail"] for f in failures], [f["check"] for f in failures], change.draft_body, change.title))
        return {"body": change.draft_body, "frontmatter": change.draft_meta}

    monkeypatch.setattr(curate, "repair_draft", model_repair)
    await Pipeline(cfg, store)._stage_validate(state)
    details, checks, body, title = seen[0]
    assert body == draft and title == f"Correction: {instruction[:40]}"  # nothing restored, nothing retitled
    assert "title" not in checks
    assert details == [
        f"{PAGE}: update drops existing section '{name}' (unrelated removals are not allowed)" for name in dropped
    ]


def test_a_correction_job_keeps_the_in_order_section_check_and_no_title_check(cfg):
    moved = BASE.replace(PRICING, "").replace(LIMITS, PRICING + LIMITS)
    assert len(check_update_scope({}, BASE, {}, moved, PAGE, correction_job=True)) == 1
    assert check_update_scope({}, BASE, {}, moved, PAGE) == []  # an ordinary job: a moved section is kept
    change = PageChange(
        "g",
        PAGE,
        "update",
        title="Correction: x",
        draft_body=RETITLED,
        draft_meta={
            "schema_version": 1,
            "privacy": "internal",
            "source_origin": "agent",
            "tags": ["x"],
            "source_lineage": ["seed:test"],
            "updated": "2026-10-04",
            "concept_id": "c-1234abcd",
            "title": "Totally Different",
        },
    )
    from doclayer.curator.validate import KnownPages

    ordinary = run_deterministic_checks(change, cfg, KnownPages(), {"title": "Probe Page"}, BASE, [])
    correction = run_deterministic_checks(
        change, cfg, KnownPages(), {"title": "Probe Page"}, BASE, [], correction_job=True
    )
    assert "title" in [f.check for f in ordinary] and "title" not in [f.check for f in correction]


@pytest.mark.asyncio
async def test_past_corrections_reach_a_later_job_only_through_the_replay_check(cfg, store, checkout, monkeypatch):
    draft = without(LIMITS).replace(
        "Intro words of this page.", "Intro words. Five hundred requests per minute on tier one."
    )
    state = page_job(cfg, store, checkout, monkeypatch, BASE, draft)
    store.add_correction(
        {
            "page": PAGE,
            "source_evidence_hash": "",
            "claim": "Five hundred requests per minute on tier one",
            "instruction": "remove the Limits section",
        }
    )
    seen = []

    async def model_repair(client, topic, change, failures, *args, **kwargs):
        seen.append([f["check"] for f in failures])
        return {"body": change.draft_body, "frontmatter": change.draft_meta}

    monkeypatch.setattr(curate, "repair_draft", model_repair)
    await Pipeline(cfg, store)._stage_validate(state)
    change = state.changes[0]
    assert "## Limits" in change.draft_body  # the past removal exempts nothing; the section is restored
    assert seen and "corrections" in seen[0]  # the replayed corrected claim is still blocked


# --------------------------------------------------------------------------- #
# The preamble is never section-restored; H1 restore is the H1 line only
# --------------------------------------------------------------------------- #

H1_BASE = "# T\n\nIntro words of the page here describing the model.\n\n## A\n\nAlpha words.\n"


@pytest.mark.parametrize(
    "draft",
    [
        "This page describes the model in fresh wording now.\n\n## A\n\nAlpha words.\n",  # reworded intro
        "Intro words of this page here describing the new model family.\n\n## A\n\nAlpha words.\n",  # lightly edited
        "## A\n\nAlpha words.\n\nIntro words of the page here describing the model.\n",  # intro moved under A
        "Intro words of the page here describing the model. It also covers pricing, limits, tools and a long "
        "list of extra topics.\n\n## A\n\nAlpha words.\n",  # kept and extended
        "## A\n\nAlpha words.\n",  # H1 and intro both deleted: the intro is not restored either
    ],
    ids=["H1a", "H1b", "H1c", "H1d", "H1-and-intro-deleted"],
)
def test_an_h1_restore_reinserts_only_the_h1_line(draft):
    body, restored, impossible = restore_dropped_sections(H1_BASE, draft)
    assert restored == ["T"] and impossible == []
    assert body == "# T\n\n" + draft
    assert body.count("Intro words of the page here describing the model.") <= 1


def test_a_renamed_h1_keeps_the_drafted_preamble():
    draft = "# New\n\nA rewritten preamble.\n\n## A\n\nAlpha words.\n"
    assert restore_dropped_sections(H1_BASE, draft)[0] == draft.replace("# New", "# T")


# --------------------------------------------------------------------------- #
# An ordinary job restores the H1 and the frontmatter title together
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_ordinary_job_restores_h1_and_frontmatter_title_together(cfg, store, checkout, monkeypatch):
    state = page_job(cfg, store, checkout, monkeypatch, BASE, RETITLED, title="Probe Page")
    state.changes[0].draft_meta["title"] = "Totally Different"
    store.save(state)
    await Pipeline(cfg, store)._stage_validate(state)
    change = state.changes[0]
    assert change.status == "validated"
    assert change.draft_body.startswith("# Probe Page\n") and change.draft_meta["title"] == "Probe Page"


def test_restore_titles_puts_h1_and_frontmatter_back_in_one_call():
    change = PageChange(
        "g", PAGE, "update", title="Probe Page", draft_body=RETITLED, draft_meta={"title": "Totally Different"}
    )
    assert restore_titles(change, {"title": "Probe Page"}, BASE) == ["page H1", "frontmatter title"]
    assert change.draft_body.startswith("# Probe Page\n") and change.draft_meta["title"] == "Probe Page"


# --------------------------------------------------------------------------- #
# Setext headings are normalised to ATX in drafts
# --------------------------------------------------------------------------- #

from doclayer.curator.validate import check_title, normalize_setext  # noqa: E402


@pytest.mark.parametrize(
    "body, expected",
    [
        ("Title\n=====\n\nText.\n", "# Title\n\nText.\n"),
        ("# P\n\nSub\n---\n\nX.\n", "# P\n\n## Sub\n\nX.\n"),
        ("# P\n\nPara.\n\n---\n\nMore.\n", "# P\n\nPara.\n\n---\n\nMore.\n"),  # thematic break after a blank line
        ("---\ntitle: x\n---\n\n# T\n", "---\ntitle: x\n---\n\n# T\n"),  # frontmatter delimiters
        ("```\na\n===\n```\n", "```\na\n===\n```\n"),  # fenced code
        ("line one\nline two\n---\n", "line one\nline two\n---\n"),  # a multi-line paragraph is left alone
        ("- item\n---\n", "- item\n---\n"),  # a list item is not a heading
    ],
    ids=["setext-h1", "setext-h2", "thematic-break", "frontmatter", "fence", "multi-line", "list-item"],
)
def test_setext_normalisation(body, expected):
    assert normalize_setext(body) == expected


def test_a_setext_h1_next_to_an_atx_h1_counts_as_two_h1s():
    draft = normalize_setext("Title X\n=======\n\n# Y\n\n## A\n\nAlpha words.\n")
    change = PageChange("g", PAGE, "update", title="A", draft_body=draft, draft_meta={"title": "A"})
    assert [f.detail for f in check_title(change, {"title": "A"}, "## A\n\nAlpha words.\n")] == [
        f"{PAGE}: the page has more than one H1; a page has exactly one title"
    ]


@pytest.mark.asyncio
async def test_a_setext_h1_draft_counts_as_the_one_h1_and_validates(cfg, store, checkout, monkeypatch):
    draft = BASE.replace("# Probe Page\n", "Probe Page\n==========\n")
    state = page_job(cfg, store, checkout, monkeypatch, BASE, draft, title="Probe Page")
    await Pipeline(cfg, store)._stage_validate(state)
    change = state.changes[0]
    assert change.status == "validated", change.validation
    assert change.draft_body == BASE  # one ATX H1, nothing restored or duplicated
    assert "normalised setext heading(s) to ATX" in change.notes
    assert not any("restored dropped base section" in note for note in change.notes)


@pytest.mark.asyncio
async def test_a_thematic_break_in_a_draft_is_untouched(cfg, store, checkout, monkeypatch):
    draft = BASE.replace("## End", "---\n\n## End")
    state = page_job(cfg, store, checkout, monkeypatch, BASE, draft, title="Probe Page")
    await Pipeline(cfg, store)._stage_validate(state)
    change = state.changes[0]
    assert change.status == "validated" and change.draft_body == draft
    assert "normalised setext heading(s) to ATX" not in change.notes
