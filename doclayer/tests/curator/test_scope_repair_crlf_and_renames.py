"""Scope repair for correction jobs, CRLF drafts and proven renames.

- Correction jobs follow the base rules: no stale-draft redraft, no error/gap clearing.
- CRLF becomes LF before setext detection.
- A proven rename survives it.
"""

import hashlib

import pytest
from test_scope_repair_restores import PAGE, page_job

from doclayer.curator import curate
from doclayer.curator.editing import EDITOR_VERSION
from doclayer.curator.editorial import EditorialContextOverflow
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.validate import check_title, normalize_setext

# --------------------------------------------------------------------------- #
# Correction jobs: no redraft, no error/gap clearing
# --------------------------------------------------------------------------- #

BASE = (
    "# Probe Page\n\nIntro words of this page.\n\n## Notes\n\nGeneral notes about the model family.\n\n"
    "## End\n\nFinal words of the page here.\n"
)


@pytest.mark.asyncio
async def test_a_correction_jobs_stale_draft_fails_instead_of_redrafting(cfg, store, checkout, monkeypatch):
    state = page_job(cfg, store, checkout, monkeypatch, BASE, BASE, correction={"claim": "", "instruction": "fix it"})

    def stale(self, state, change, records):
        raise EditorialContextOverflow(
            "redraft required probe",
            transient=False,
            diagnostic={"operation": "redraft-required", "page": change.path, "records": 43},
        )

    monkeypatch.setattr(Pipeline, "_review_evidence", stale)
    assert await Pipeline(cfg, store)._stage_validate(state) != "draft"
    change = state.changes[0]
    assert change.status == "failed" and "redraft required" in change.error and change.draft_body == BASE
    assert not any("redrafted automatically" in note for note in change.notes)


@pytest.mark.asyncio
async def test_a_resumed_correction_page_keeps_its_old_error_and_gap_when_it_validates(
    cfg, store, checkout, monkeypatch
):
    state = page_job(cfg, store, checkout, monkeypatch, BASE, BASE, correction={"claim": "", "instruction": "fix it"})
    gap = PAGE + ": editorial review unresolved; drafts, source evidence and obligations retained for follow-up"
    state.changes[0].error = "scope: old failure"
    state.coverage_gaps.append(gap)
    store.save(state)
    await Pipeline(cfg, store)._stage_validate(state)
    assert state.changes[0].status == "validated"
    assert state.changes[0].error == "scope: old failure" and gap in state.coverage_gaps


@pytest.mark.asyncio
async def test_an_ordinary_resumed_page_still_clears_its_old_error_and_gap(cfg, store, checkout, monkeypatch):
    state = page_job(cfg, store, checkout, monkeypatch, BASE, BASE)
    gap = PAGE + ": editorial review unresolved; drafts, source evidence and obligations retained for follow-up"
    state.changes[0].error = "scope: old failure"
    state.coverage_gaps.append(gap)
    store.save(state)
    await Pipeline(cfg, store)._stage_validate(state)
    assert state.changes[0].error is None and gap not in state.coverage_gaps


# --------------------------------------------------------------------------- #
# CRLF before setext; a proven rename survives normalisation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body, expected",
    [
        ("Title\r\n=====\r\n\r\nText.\r\n", "# Title\n\nText.\n"),  # Se3
        ("# T\r\n\r\nSection\r\n-------\r\n\r\nText.\r\n", "# T\n\n## Section\n\nText.\n"),  # Se4
        ("---\n\nTitle\n=====\n\n---\n\nText.\n", "---\n\n# Title\n\n---\n\nText.\n"),  # Se10: a --- block, not YAML
    ],
    ids=["Se3", "Se4", "Se10"],
)
def test_crlf_setext_is_normalised(body, expected):
    assert normalize_setext(body) == expected


@pytest.mark.asyncio
async def test_a_crlf_setext_h1_beside_an_atx_h1_is_two_h1s(cfg, store, checkout, monkeypatch):
    draft = BASE.replace("# Probe Page\n", "Other Title\n===========\n\n# Probe Page\n").replace("\n", "\r\n")
    state = page_job(cfg, store, checkout, monkeypatch, BASE, draft)
    seen = []

    async def model_repair(client, topic, change, failures, *args, **kwargs):
        seen.append([f["detail"] for f in failures])
        return {"body": BASE, "frontmatter": change.draft_meta}

    monkeypatch.setattr(curate, "repair_draft", model_repair)
    await Pipeline(cfg, store)._stage_validate(state)
    assert any("more than one H1" in detail for detail in seen[0])
    assert "normalised CRLF line endings to LF" in state.changes[0].notes


@pytest.mark.asyncio
async def test_a_proven_rename_survives_setext_normalisation(cfg, store, checkout, monkeypatch):
    draft = BASE.replace("## Notes", "## Model Notes").replace("## End\n", "End\n---\n")
    proof = {
        "editor_version": EDITOR_VERSION,
        "body_hash": hashlib.sha256(draft.encode()).hexdigest(),
        "heading_renames": [{"old": "## Notes", "new": "## Model Notes"}],
    }
    state = page_job(cfg, store, checkout, monkeypatch, BASE, draft, progress=proof)
    await Pipeline(cfg, store)._stage_validate(state)
    change = state.changes[0]
    assert change.status == "validated", change.validation
    assert "## Model Notes" in change.draft_body and "## Notes\n" not in change.draft_body
    assert "## End\n" in change.draft_body and change.draft_progress["heading_renames"] == proof["heading_renames"]
    assert not any("restored dropped base section" in note for note in change.notes)


def test_two_h1s_check_counts_a_crlf_setext_h1():
    from doclayer.curator.state import PageChange

    draft = normalize_setext("Title X\r\n=======\r\n\r\n# Y\r\n\r\n## A\r\n\r\nAlpha words.\r\n")
    change = PageChange("g", PAGE, "update", title="A", draft_body=draft, draft_meta={"title": "A"})
    assert any("more than one H1" in f.detail for f in check_title(change, {"title": "A"}, "## A\n\nAlpha words.\n"))
