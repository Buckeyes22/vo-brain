"""A draft that drops a base section is repaired without a model.

Reproduces job-7d8f39d4e9, wiki/gpt-6-prompting-guidance.md: the drafter
retitled the page H1 "GPT-6 Astra Prompting Guidance" to "GPT-6 Prompting
Guidance" and added a "## GPT-6 Astra Prompting Guidance" subsection. The
model repair left the "update drops existing section" blocker unchanged and
the page failed as repair-stalled.
"""

import pytest
from conftest import run_git

from doclayer.curator import curate
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import JobState, PageChange
from doclayer.curator.validate import check_title, check_update_scope, restore_dropped_sections, restore_titles

PAGE = "wiki/gpt-6-prompting-guidance.md"
BLOG = "https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra"

BASE = f"""# GPT-6 Astra Prompting Guidance

GPT-6 Astra requires less handholding and scaffolding than previous models [source: {BLOG}].

## Skills Optimization

Skills are prompts stored as Markdown files [source: {BLOG}].

## AGENTS.md Best Practices

`AGENTS.md` is an open format for guiding coding agents [source: https://agents.md/].

### Structural Guidelines

*   **Placement:** Create the file at the root of the repository [source: https://agents.md/].

### Content Strategy for GPT-6 Astra

*   **Contextual Instructions:** Instructions should be contextual [source: {BLOG}].

## Behavioral Adjustments

*   **Decision Boundaries:** Strong boundary language may stop work prematurely [source: {BLOG}].

## Ecosystem Compatibility

`AGENTS.md` is compatible with a wide ecosystem of AI coding agents [source: https://agents.md/].

## Related Pages

* [[gpt-6-astra]]
"""

# The live draft's structure: H1 retitled, new sections before the kept ones.
DRAFT = f"""# GPT-6 Prompting Guidance

GPT-6 models require less handholding and scaffolding than previous generations [source: {BLOG}].

## GPT-6 Astra Prompting Guidance

GPT-6 Astra benefits from clear assignments stating the desired result [source: {BLOG}].

## Codex Clarification and Steering

Codex asks clarifying questions when a task is ambiguous [source: {BLOG}].

## Skills Optimization

Skills are prompts stored as Markdown files [source: {BLOG}].

## AGENTS.md Best Practices

`AGENTS.md` is an open format for guiding coding agents [source: https://agents.md/].

### Structural Guidelines

*   **Placement:** Create the file at the root of the repository [source: https://agents.md/].

### Content Strategy for GPT-6 Astra

*   **Contextual Instructions:** Instructions should be contextual [source: {BLOG}].

## Behavioral Adjustments

*   **Decision Boundaries:** Strong boundary language may stop work prematurely [source: {BLOG}].

## Computer Use

GPT-6 Astra can operate a computer through screenshots [source: {BLOG}].

## Ecosystem Compatibility

`AGENTS.md` is compatible with a wide ecosystem of AI coding agents [source: https://agents.md/].

## Related Pages

* [[gpt-6-astra]]
"""


def section(body, heading):
    start = body.index(heading)
    end = body.find("\n#", start + 1)
    return body[start:] if end < 0 else body[start : end + 1]


def headings(body):
    return [line for line in body.splitlines() if line.startswith("#")]


def test_draft_reproduces_the_dropped_section_blocker():
    failures = check_update_scope({}, BASE, {}, DRAFT, PAGE)
    assert [f.detail for f in failures] == [
        f"{PAGE}: update drops existing section 'GPT-6 Astra Prompting Guidance' (unrelated removals are not allowed)"
    ]


def test_retitled_h1_gets_its_base_heading_back_in_place():
    body, restored, impossible = restore_dropped_sections(BASE, DRAFT)
    assert restored == ["GPT-6 Astra Prompting Guidance"] and impossible == []
    assert check_update_scope({}, BASE, {}, body, PAGE) == []
    # Only the heading line changes: no second H1 and no duplicated text.
    assert body == DRAFT.replace("# GPT-6 Prompting Guidance\n", "# GPT-6 Astra Prompting Guidance\n", 1)
    assert [h for h in headings(body) if h.startswith("# ")] == ["# GPT-6 Astra Prompting Guidance"]


def test_dropped_section_is_reinserted_verbatim_between_its_neighbours():
    draft = DRAFT.replace(section(DRAFT, "## Behavioral Adjustments"), "")
    body, restored, impossible = restore_dropped_sections(
        BASE, draft.replace("# GPT-6 Prompting Guidance", "# GPT-6 Astra Prompting Guidance")
    )
    assert restored == ["Behavioral Adjustments"] and impossible == []
    assert section(BASE, "## Behavioral Adjustments") in body
    # After its earlier neighbour's subsections, before the new and later sections.
    assert headings(body)[6:9] == [
        "### Content Strategy for GPT-6 Astra",
        "## Behavioral Adjustments",
        "## Computer Use",
    ]
    assert check_update_scope({}, BASE, {}, body, PAGE) == []


def test_dropped_nested_and_last_sections_are_restored_in_base_order():
    draft = DRAFT.replace(section(DRAFT, "### Structural Guidelines"), "")
    draft = draft.replace(section(draft, "## Related Pages"), "").rstrip("\n") + "\n"
    body, restored, impossible = restore_dropped_sections(BASE, draft)
    assert restored == ["GPT-6 Astra Prompting Guidance", "Structural Guidelines", "Related Pages"]
    assert impossible == []
    assert headings(body)[4:7] == [
        "## AGENTS.md Best Practices",
        "### Structural Guidelines",
        "### Content Strategy for GPT-6 Astra",
    ]
    assert body.endswith("## Related Pages\n\n* [[gpt-6-astra]]\n")
    assert check_update_scope({}, BASE, {}, body, PAGE) == []


def test_renamed_section_is_told_from_a_new_one_by_its_text():
    # Behavioral Adjustments renamed (same text) beside an unrelated new section.
    draft = DRAFT.replace("# GPT-6 Prompting Guidance", "# GPT-6 Astra Prompting Guidance").replace(
        "## Behavioral Adjustments", "## Behaviour Changes"
    )
    body, restored, impossible = restore_dropped_sections(BASE, draft)
    assert restored == ["Behavioral Adjustments"] and impossible == []
    assert body == draft.replace("## Behaviour Changes", "## Behavioral Adjustments")
    assert "## Computer Use\n" in body


def test_editor_proven_heading_rename_is_not_undone():
    draft = DRAFT.replace("# GPT-6 Prompting Guidance", "# GPT-6 Astra Prompting Guidance").replace(
        "## Skills Optimization", "## Skills"
    )
    renames = [{"old": "## Skills Optimization", "new": "## Skills"}]
    assert check_update_scope({}, BASE, {}, draft, PAGE, renames) == []
    assert restore_dropped_sections(BASE, draft, renames) == (draft, [], [])


def test_no_surviving_neighbour_means_the_restore_is_impossible_and_says_why():
    draft = "## Something else\n\nAll new [source: x].\n"
    body, restored, impossible = restore_dropped_sections("## One\n\nText.\n\n## Two\n\nMore.\n", draft)
    assert restored == [] and body == draft
    assert impossible == [
        "One: no base section around it survives in the draft",
        "Two: no base section around it survives in the draft",
    ]


def test_a_retitled_page_keeps_one_h1_renamed_in_place_and_its_sections_follow():
    # The drafted H1 is renamed in place, never a second H1 prepended.
    draft = "# Something else\n\nAll new [source: x].\n"
    body, restored, impossible = restore_dropped_sections("# Only\n\nText.\n\n## Two\n\nMore.\n", draft)
    assert restored == ["Only", "Two"] and impossible == []
    assert body == "# Only\n\nAll new [source: x].\n\n## Two\n\nMore.\n"


# --------------------------------------------------------------------------- #
# pipeline: no model pass is spent; the model repairs only what cannot be placed
# --------------------------------------------------------------------------- #


def page_job(cfg, store, checkout, monkeypatch, draft, *, base=BASE, passes=1):
    (checkout / PAGE).write_text(
        "---\nschema_version: 1\nprivacy: internal\nsource_origin: agent\nsource_lineage:\n  - seed:test\n"
        "tags:\n  - model\nupdated: 2026-09-01\ntitle: GPT-6 Astra Prompting Guidance\n---\n\n" + base
    )
    run_git(checkout, "add", PAGE)
    run_git(checkout, "commit", "-qm", "guidance page")
    from doclayer.curator import gittools

    head = run_git(checkout, "rev-parse", "HEAD")
    state = store.create(JobState(job_id="", idempotency_key="", intent="GPT-6 guide", base_commit=head))[0]
    state.stage, state.status = "validate", "processing"
    state.limits = {"max_repair_passes": 2}
    state.changes = [
        PageChange(
            "g",
            PAGE,
            "update",
            title="GPT-6 Prompting Guidance",
            draft_body=draft,
            status="failed",
            base_commit=head,
            base_blob=gittools.blob_hash(checkout, f"{head}:{PAGE}") or "",
            draft_meta={
                "schema_version": 1,
                "privacy": "internal",
                "source_origin": "agent",
                "tags": ["model"],
                "source_lineage": ["seed:test"],
                "updated": "2026-10-04",
                "concept_id": "c-1234abcd",
                "title": "GPT-6 Prompting Guidance",
            },
            repair_passes=passes,
            error="repair-stalled: repair pass 1 left the identical failure set; no further pass spent: scope: "
            f"{PAGE}: update drops existing section 'GPT-6 Astra Prompting Guidance'",
        )
    ]
    store.save(state)

    async def verify(*args):
        pass

    async def clean(*args, **kwargs):
        return {"failures": [], "conflicts": []}

    monkeypatch.setattr(Pipeline, "_verify_selected", verify)
    monkeypatch.setattr(Pipeline, "_evidence", lambda *args: [])
    monkeypatch.setattr(Pipeline, "_preserve_existing", clean)
    monkeypatch.setattr(curate, "check_draft", clean)
    return state


@pytest.mark.asyncio
async def test_page_is_restored_and_validated_without_a_model_pass(cfg, store, checkout, monkeypatch):
    state = page_job(cfg, store, checkout, monkeypatch, DRAFT)

    async def no_model_repair(*args, **kwargs):
        raise AssertionError("a dropped base section must not spend a model repair pass")

    monkeypatch.setattr(curate, "repair_draft", no_model_repair)
    await Pipeline(cfg, store)._stage_validate(state)
    saved = store.load(state.job_id).changes[0]
    assert saved.status == "validated", saved.validation
    assert saved.repair_passes == 1 and saved.error is None
    assert saved.draft_body.startswith("# GPT-6 Astra Prompting Guidance\n")
    assert any("restored dropped base section(s) 'GPT-6 Astra Prompting Guidance'" in n for n in saved.notes)


@pytest.mark.asyncio
async def test_unplaceable_section_goes_to_the_model_with_the_reason(cfg, store, checkout, monkeypatch):
    # No H1 and no kept section: nothing anchors either base section.
    base = "## One\n\nText of the first section here.\n\n## Two\n\nMore text in the second one.\n"
    state = page_job(
        cfg, store, checkout, monkeypatch, "## Else\n\nAll new and unrelated words here.\n", base=base, passes=0
    )
    seen = []

    async def model_repair(client, topic, change, failures, *args, **kwargs):
        seen.append([f["detail"] for f in failures])
        return {"body": base, "frontmatter": {}}

    monkeypatch.setattr(curate, "repair_draft", model_repair)
    await Pipeline(cfg, store)._stage_validate(state)
    saved = store.load(state.job_id).changes[0]
    assert len(seen) == 1
    assert (
        f"{PAGE}: deterministic restore of section One: no base section around it survives in the draft" in seen[0][0]
    )
    assert saved.status == "validated" and saved.repair_passes == 1


# --------------------------------------------------------------------------- #
# concern 3: near-empty sections need matching headings to count as renamed
# --------------------------------------------------------------------------- #

NEAR_EMPTY_BASE = "# Page\n\nIntro text of the page.\n\n## Overview\n\n### Details\n\nThe details of the topic.\n"


def test_new_heading_only_section_is_not_renamed_the_base_one_is_reinserted():
    draft = "# Page\n\nIntro text of the page.\n\n## Deployment Notes\n\n### Details\n\nThe details of the topic.\n"
    body, restored, impossible = restore_dropped_sections(NEAR_EMPTY_BASE, draft)
    assert restored == ["Overview"] and impossible == []
    assert "## Deployment Notes\n" in body and "## Overview\n" in body
    assert check_update_scope({}, NEAR_EMPTY_BASE, {}, body, PAGE) == []


def test_near_empty_section_with_a_similar_heading_is_a_rename():
    draft = NEAR_EMPTY_BASE.replace("## Overview", "## Overview Section")  # headings align 0.67
    body, restored, _ = restore_dropped_sections(NEAR_EMPTY_BASE, draft)
    assert restored == ["Overview"] and body == NEAR_EMPTY_BASE


def test_short_bodies_need_matching_headings_too():
    base = "# Page\n\n## Limits\n\nSee pricing.\n\n## End\n\nLast words of the page here.\n"
    draft = "# Page\n\n## Regions\n\nSee pricing.\n\n## End\n\nLast words of the page here.\n"
    body, restored, _ = restore_dropped_sections(base, draft)
    assert restored == ["Limits"] and "## Regions\n" in body and "## Limits\n\nSee pricing.\n" in body


# --------------------------------------------------------------------------- #
# concern 2: the curator never renames a page
# --------------------------------------------------------------------------- #


def titled(draft, *, title, plan, action="update"):
    return PageChange("g", PAGE, action, title=plan, draft_body=draft, draft_meta={"title": title})


def test_title_check_flags_all_three_and_restore_puts_back_the_base_title():
    meta = {"title": "GPT-6 Astra Prompting Guidance"}
    change = titled(DRAFT, title="GPT-6 Prompting Guidance", plan="GPT-6 Prompting Guidance")
    assert [f.detail.split(" '")[0] for f in check_title(change, meta, BASE)] == [
        f"{PAGE}: page H1",
        f"{PAGE}: frontmatter title",
        f"{PAGE}: plan title",
    ]
    assert restore_titles(change, meta, BASE) == ["page H1", "frontmatter title", "plan title"]
    assert change.draft_body.startswith("# GPT-6 Astra Prompting Guidance\n")
    assert change.draft_meta["title"] == change.title == "GPT-6 Astra Prompting Guidance"
    assert check_title(change, meta, BASE) == []


def test_base_page_whose_title_and_h1_already_differ_keeps_both():
    meta = {"title": "forgejo"}
    base = "# Forgejo (git host)\n\nText.\n"
    change = titled("# Forgejo (git host)\n\nText and more.\n", title="forgejo", plan="forgejo")
    assert check_title(change, meta, base) == []
    renamed = titled("# Forgejo\n\nText.\n", title="Forgejo", plan="Forgejo")
    assert restore_titles(renamed, meta, base) == ["page H1", "frontmatter title", "plan title"]
    assert renamed.draft_body.startswith("# Forgejo (git host)\n") and renamed.title == "forgejo"


def test_new_pages_rename_nothing_and_are_not_checked():
    change = titled("# Drafted\n\nText.\n", title="Other", plan="Plan", action="create")
    assert check_title(change, None, None) == [] and restore_titles(change, None, None) == []


@pytest.mark.asyncio
async def test_pipeline_restores_h1_frontmatter_and_plan_title(cfg, store, checkout, monkeypatch):
    state = page_job(cfg, store, checkout, monkeypatch, DRAFT)

    async def no_model_repair(*args, **kwargs):
        raise AssertionError("no model repair pass")

    monkeypatch.setattr(curate, "repair_draft", no_model_repair)
    await Pipeline(cfg, store)._stage_validate(state)
    saved = store.load(state.job_id).changes[0]
    assert saved.status == "validated", saved.validation
    assert saved.title == saved.draft_meta["title"] == "GPT-6 Astra Prompting Guidance"
    assert saved.draft_body.startswith("# GPT-6 Astra Prompting Guidance\n")
    assert any(note.startswith("title repair: frontmatter title, plan title restored") for note in saved.notes)
