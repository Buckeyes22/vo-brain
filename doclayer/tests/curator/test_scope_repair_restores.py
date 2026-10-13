"""Scope repair: deterministic section and title restores.

A no-op restore ends the repair loop, moved sections are matched by identity,
a page keeps exactly one H1, and code fences are never edited. Citations are
not repaired deterministically; they go to the model repair.
"""

import hashlib

import pytest
from conftest import run_git

from doclayer.curator import curate, gittools
from doclayer.curator.editing import EDITOR_VERSION
from doclayer.curator.pipeline import MAX_DETERMINISTIC_PASSES, Pipeline
from doclayer.curator.state import JobState, PageChange
from doclayer.curator.validate import (
    check_title,
    check_update_scope,
    restore_dropped_sections,
    restore_titles,
)

PAGE = "wiki/probe-page.md"
REJ = "https://ex.com/model"
OK = "https://ex.com/guide"
POLICY = {"include": [{"url": OK}]}


def headings(body):
    return [line for line in body.splitlines() if line.startswith("#")]


def page_job(
    cfg, store, checkout, monkeypatch, base, draft, *, title="Probe Page", plan=None, correction=None, progress=None
):
    (checkout / PAGE).write_text(
        "---\nschema_version: 1\nprivacy: internal\nsource_origin: agent\nsource_lineage:\n  - seed:test\n"
        f"tags:\n  - x\nupdated: 2026-09-01\ntitle: {title}\n---\n\n" + base
    )
    run_git(checkout, "add", PAGE)
    run_git(checkout, "commit", "-qm", "probe page")
    head = run_git(checkout, "rev-parse", "HEAD")
    key, intent = "probe", "probe"
    if correction is not None:
        # As ops._op_correct creates it: keyed by its own correction, intent naming the instruction.
        record = store.add_correction({"page": PAGE, "source_evidence_hash": "", **correction})
        key, intent = (
            f"corr-{record['correction_id']}",
            f"Apply operator correction on {PAGE}: {correction['instruction']}",
        )
    state = store.create(
        JobState(
            job_id="",
            idempotency_key=key,
            intent=intent,
            base_commit=head,
            correction_of="direct" if correction is not None else None,
        )
    )[0]
    state.stage, state.status = "validate", "processing"
    state.limits = {"max_repair_passes": 2}
    state.changes = [
        PageChange(
            "g1",
            PAGE,
            "update",
            title=plan or title,
            draft_body=draft,
            status="drafted",
            base_commit=head,
            base_blob=gittools.blob_hash(checkout, f"{head}:{PAGE}") or "",
            draft_progress=progress or {},
            draft_meta={
                "schema_version": 1,
                "privacy": "internal",
                "source_origin": "agent",
                "tags": ["x"],
                "source_lineage": ["seed:test"],
                "updated": "2026-10-04",
                "concept_id": "c-1234abcd",
                "title": title,
            },
        )
    ]
    store.save(state)

    async def verify(*args):
        pass

    async def clean(*args, **kwargs):
        return {"failures": [], "conflicts": []}

    async def no_model_repair(*args, **kwargs):
        raise AssertionError("no model repair pass expected")

    monkeypatch.setattr(Pipeline, "_verify_selected", verify)
    monkeypatch.setattr(Pipeline, "_evidence", lambda *args: [])
    monkeypatch.setattr(Pipeline, "_preserve_existing", clean)
    monkeypatch.setattr(curate, "check_draft", clean)
    monkeypatch.setattr(curate, "repair_draft", no_model_repair)
    return state


# --------------------------------------------------------------------------- #
# A no-op restore is not progress; the loop is bounded
# --------------------------------------------------------------------------- #

LOOP_BASE = "# Old Title\n\nSome text of the page here.\n"


@pytest.mark.parametrize("h1", ["# New Title\r\n", "#\xa0New Title\n", "# New Title\n"])
@pytest.mark.asyncio
async def test_h1_on_a_single_section_page_converges(cfg, store, checkout, monkeypatch, h1):
    draft = LOOP_BASE.replace("# Old Title\n", h1)
    progress = {
        "editor_version": EDITOR_VERSION,
        "body_hash": hashlib.sha256(draft.encode()).hexdigest(),
        "heading_renames": [],
    }
    state = page_job(cfg, store, checkout, monkeypatch, LOOP_BASE, draft, title="Old Title", progress=progress)
    calls = []
    original = Pipeline._restore_structure

    def counting(self, *args, **kwargs):
        calls.append(1)
        assert len(calls) <= MAX_DETERMINISTIC_PASSES + 1, "unbounded"
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Pipeline, "_restore_structure", counting)
    await Pipeline(cfg, store)._stage_validate(state)
    change = state.changes[0]
    assert change.status == "validated", change.validation
    assert len(calls) <= 2
    assert change.draft_body.splitlines()[0] == "# Old Title"
    assert change.draft_body.endswith("Some text of the page here.\n")
    assert sum("title repair" in note for note in change.notes) <= 1


@pytest.mark.parametrize("h1", ["# Old Title \n", "# Old\xa0Title\n", "# Old Title\r\n"])
def test_formatting_only_h1_difference_is_neither_a_failure_nor_a_fix(h1):
    change = PageChange(
        "g", PAGE, "update", title="Old Title", draft_body=h1 + "\nText.\n", draft_meta={"title": "Old Title"}
    )
    before = change.draft_body
    assert check_title(change, {"title": "Old Title"}, "# Old Title\n\nText.\n") == []
    assert restore_titles(change, {"title": "Old Title"}, "# Old Title\n\nText.\n") == []
    assert change.draft_body == before


# --------------------------------------------------------------------------- #
# Sections match by identity; a moved section is not dropped
# --------------------------------------------------------------------------- #


def test_reordered_sections_restore_the_dropped_one_without_duplicates():
    base = (
        "# T\n\nIntro text.\n\n## A\n\nAlpha text one two three four five.\n\n## B\n\nBeta text one two three four five.\n\n"
        "## C\n\nGamma text one two three four five.\n"
    )
    draft = "# T\n\nIntro text.\n\n## C\n\nGamma text one two three four five six.\n\n## A\n\nAlpha text one two three four five.\n"
    body, restored, impossible = restore_dropped_sections(base, draft)
    assert restored == ["B"] and impossible == []
    assert headings(body) == ["# T", "## C", "## A", "## B"]  # after its earlier neighbour A, as the draft orders it
    assert check_update_scope({}, base, {}, body, PAGE) == []


def test_swapped_neighbours_never_relabel_a_base_section():
    base = (
        "# T\n\n## A\n\nAlpha words here one two three.\n\n## B\n\nBeta words here one two three.\n\n"
        "## C\n\nGamma words here one two three.\n\n## D\n\nDelta words here one two three.\n"
    )
    draft = (
        "# T\n\n## A\n\nAlpha words here one two three.\n\n## D\n\nDelta words here one two three.\n\n"
        "## C\n\nGamma words here one two three.\n"
    )
    body, restored, _ = restore_dropped_sections(base, draft)
    assert restored == ["B"]
    assert headings(body) == ["# T", "## A", "## B", "## D", "## C"]
    assert "## B\n\nBeta words here one two three.\n" in body
    assert body.count("Delta words") == 1 and body.count("## D") == 1


def test_both_neighbours_dropped_restores_in_base_order():
    base = (
        "# T\n\n## A\n\na1 a2 a3 a4 a5.\n\n## B\n\nb1 b2 b3 b4 b5.\n\n## C\n\nc1 c2 c3 c4 c5.\n\n"
        "## D\n\nd1 d2 d3 d4 d5.\n\n## E\n\ne1 e2 e3 e4 e5.\n"
    )
    body, restored, _ = restore_dropped_sections(base, "# T\n\n## A\n\na1 a2 a3 a4 a5.\n\n## E\n\ne1 e2 e3 e4 e5.\n")
    assert restored == ["B", "C", "D"] and headings(body) == ["# T", "## A", "## B", "## C", "## D", "## E"]


def test_a_moved_section_is_not_dropped():
    base = "# T\n\n## A\n\nAlpha.\n\n## B\n\nBeta.\n"
    assert check_update_scope({}, base, {}, "# T\n\n## B\n\nBeta.\n\n## A\n\nAlpha.\n", PAGE) == []


def test_duplicate_headings_are_matched_by_their_text():
    base = (
        "# T\n\n## X\n\n### Example\n\nFirst example text here.\n\n## Y\n\n### Example\n\nSecond example text here.\n"
    )
    draft = "# T\n\n## X\n\n## Y\n\n### Example\n\nSecond example text here.\n"
    body, restored, _ = restore_dropped_sections(base, draft)
    assert restored == ["Example"] and "First example text here." in body
    assert body.index("First example") < body.index("## Y")


# --------------------------------------------------------------------------- #
# A page has exactly one H1
# --------------------------------------------------------------------------- #


def test_retitled_h1_with_a_rewritten_intro_is_renamed_in_place():
    base = "# Astra Guide\n\nAstra needs less scaffolding than older models do today.\n\n## Skills\n\nSkills are prompts stored as markdown files.\n"
    draft = "# GPT-6 Guide\n\nThis page collects prompting advice for the whole family.\n\n## Skills\n\nSkills are prompts stored as markdown files.\n"
    body, restored, impossible = restore_dropped_sections(base, draft)
    assert restored == ["Astra Guide"] and impossible == []
    assert body == draft.replace("# GPT-6 Guide", "# Astra Guide")
    assert [h for h in headings(body) if h.startswith("# ")] == ["# Astra Guide"]


def test_an_update_with_two_h1s_fails_the_title_check():
    change = PageChange(
        "g", PAGE, "update", title="T", draft_body="# T\n\nA.\n\n# Other\n\nB.\n", draft_meta={"title": "T"}
    )
    assert [f.detail for f in check_title(change, {"title": "T"}, "# T\n\nA.\n")] == [
        f"{PAGE}: the page has more than one H1; a page has exactly one title"
    ]


# --------------------------------------------------------------------------- #
# Code fences are never edited
# --------------------------------------------------------------------------- #


def test_h1_restore_skips_a_heading_like_line_inside_a_code_fence():
    draft = "```\n# New Title\n```\n\n# New Title\n\nText.\n"
    change = PageChange("g", PAGE, "update", title="Old Title", draft_body=draft, draft_meta={"title": "Old Title"})
    assert restore_titles(change, {"title": "Old Title"}, "# Old Title\n\nText.\n") == ["page H1"]
    assert change.draft_body == "```\n# New Title\n```\n\n# Old Title\n\nText.\n"
