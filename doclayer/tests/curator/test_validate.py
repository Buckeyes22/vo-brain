"""Deterministic validation: schema, identity, paths, links, scope,
correction replay protection, citations."""

from __future__ import annotations

from datetime import date

import pytest
from conftest import valid_meta

from doclayer.curator.config import CuratorConfig
from doclayer.curator.state import PageChange
from doclayer.curator.validate import (
    KnownPages,
    blockers,
    check_absence_claims,
    check_corrections_replay,
    check_links,
    check_path_allowed,
    check_update_scope,
    normalize_claim,
    run_deterministic_checks,
)


def cfg() -> CuratorConfig:
    c = CuratorConfig()
    c.publish.allowed_prefixes = ["wiki/", "infra/"]
    return c


def change(path: str = "wiki/test-model.md", body: str = "# T\n", action: str = "create") -> PageChange:
    return PageChange(
        group_id="g1",
        path=path,
        action=action,
        draft_meta=valid_meta(),
        draft_body=body,
        source_ids=["src-1"],
    )


def test_links_keep_prospective_batch_pages_when_copying_known_map():
    known = KnownPages(published_paths={"wiki/related-model.md"})
    c = change(body="# T\n\nSee [[wiki/related-model.md]].")
    assert check_links(c, known, set()) == []
    assert check_links(c, KnownPages(), set())[0].check == "links"


# -- destination scope -------------------------------------------------------- #


def test_paths_must_be_in_allowed_prefixes() -> None:
    assert check_path_allowed("wiki/ok.md", cfg()) is None
    assert check_path_allowed("infra/notes.md", cfg()) is None
    failure = check_path_allowed("sources/thing.md", cfg())
    assert failure is not None and failure.check == "path"
    assert check_path_allowed("wiki/index.md", cfg()) is not None
    assert check_path_allowed("log.md", cfg()) is not None
    assert check_path_allowed("../escape.md", cfg()) is not None
    assert check_path_allowed("wiki/Bad_Name.md", cfg()) is not None


# -- frontmatter and identity ------------------------------------------------- #


def test_new_page_needs_fresh_unique_id() -> None:
    known = KnownPages(pages={"wiki/other.md": valid_meta(cid="c-1111aaaa")})
    ch = change(body="# x [source: a]")
    ch.draft_meta = valid_meta(cid="c-1111aaaa")  # reuses existing id
    failures = run_deterministic_checks(ch, cfg(), known, None, None, [])
    assert any("reuses an existing page's id" in f.detail for f in failures)


def test_missing_required_meta_blocks() -> None:
    meta = valid_meta()
    del meta["privacy"]
    bad = change()
    bad.draft_meta = meta
    failures = run_deterministic_checks(bad, cfg(), KnownPages(), None, None, [])
    assert any("privacy" in f.detail for f in failures)


def test_update_must_preserve_concept_id() -> None:
    existing_meta = valid_meta(cid="c-aaaa1111")
    known = KnownPages(pages={"wiki/x.md": existing_meta})
    ch = change(path="wiki/x.md", action="update", body="# x [source: a]")
    ch.draft_meta = valid_meta(cid="c-bbbb2222")
    failures = run_deterministic_checks(ch, cfg(), known, existing_meta, "# x\n", [])
    assert any("preserve concept_id" in f.detail for f in failures)


# -- scope: updates keep existing sections -------------------------------------- #


def test_update_dropping_existing_section_is_blocked() -> None:
    existing_body = "# Model\n\n## Capabilities\n\nstuff\n\n## Safety\n\nnotes\n"
    failures = check_update_scope({}, existing_body, {}, "# Model\n\n## Capabilities\n\nnew stuff\n", "wiki/m.md")
    assert any("drops existing section 'Safety'" in f.detail for f in failures)


# -- links ------------------------------------------------------------------------ #


def test_path_qualified_missing_link_is_blocked_but_bare_dangling_warns() -> None:
    known = KnownPages(pages={"wiki/real.md": {}})
    ch = change(body="See [[wiki/nonexistent-page]] and [[maybe-later]] and [[real]]. [source: a]")
    failures = check_links(ch, known, set())
    hard = blockers(failures)
    assert len(hard) == 1 and "nonexistent-page" in hard[0].detail
    assert any(f.severity == "warning" and "maybe-later" in f.detail for f in failures)


def test_link_to_in_batch_page_resolves() -> None:
    known = KnownPages(pages={})
    ch = change(body="See [[test-model-prompting]]. [source: a]")
    failures = check_links(ch, known, published={"wiki/test-model-prompting.md"})
    assert blockers(failures) == []


# -- correction replay -------------------------------------------------------------- #


def test_corrected_claim_reinstatement_is_blocked() -> None:
    corrected = "the model supports unlimited context on all tiers"
    ch = change(body=f"# Draft\n\n{corrected} per the docs. [source: a]")
    failures = check_corrections_replay(ch, [{"claim": corrected, "correction_id": "corr-1"}])
    assert blockers(failures)


def test_unrelated_correction_does_not_block() -> None:
    ch = change(body="# Draft\n\nSomething entirely different. [source: a]")
    failures = check_corrections_replay(
        ch, [{"claim": "the model supports unlimited context on every tier", "correction_id": "corr-1"}]
    )
    assert not blockers(failures)


def test_normalize_claim_is_whitespace_and_markup_insensitive() -> None:
    assert normalize_claim("The   **model**  works.") == normalize_claim("the model works.")


# -- citations ------------------------------------------------------------------------ #


def test_substantive_draft_without_citation_is_blocked() -> None:
    ch = change(body="# Draft\n\nClaims everywhere, no citation.")
    failures = run_deterministic_checks(ch, cfg(), KnownPages(), None, None, [])
    assert any(f.check == "support" for f in failures)


def test_citation_near_claim_passes() -> None:
    ch = change(body="# Draft\n\nA claim. [source: https://docs.test/x]")
    failures = run_deterministic_checks(ch, cfg(), KnownPages(), None, None, [])
    assert not [f for f in failures if f.check == "support"]


@pytest.mark.parametrize(
    "value,valid",
    [
        ("2026-10-01", True),
        (date(2024, 2, 29), True),
        ("2024-02-29", True),
        ("2026-02-29", False),
        ("20261001", False),
        ("2026-W40-4", False),
        ("2026-1-1", False),
        ("2026-13-01", False),
        (20261001, False),
        (True, False),
    ],
)
def test_updated_is_a_strict_calendar_date(value, valid):
    ch = change(body="# Date [source: a]")
    ch.draft_meta["updated"] = value
    failures = run_deterministic_checks(ch, cfg(), KnownPages(), None, None, [])
    assert (not any(f.check == "frontmatter" for f in failures)) == valid


def _change(body):
    return PageChange("g", "wiki/x.md", "update", draft_meta=valid_meta(), draft_body=body)


@pytest.mark.parametrize(
    "line",
    [
        "The committed fleet-hosts documentation does not specify the following for App1:",
        "The provided source does not document an Onboard version.",
        "Onboard is not documented in the checked sources.",
        "## Committed Documentation Gaps (B15 Replay)",
        "The provided documentation does not contain a version.",
        "The checked sources do not say which host runs it.",
        "The evidence does not confirm the port.",
        "The supplied material contains no mention of Onboard.",
        "The version is not available in the provided sources.",
        "The cited documentation is silent on retention.",
        "The source doesn't say which port.",
        "The source didn't mention the host.",
        "The provided sources fail to mention Onboard.",
        "Documentation does not specify the port.",
        "Sources do not mention Onboard.",
        "No mention of retention appears in the provided sources.",
        "The checked documentation says nothing about retention.",
        "The cited sources do not clearly state the version.",
        "The provided documents do not specify X.",
        "The supplied documents do not mention the port.",
        "the retrieved documents do not include the version.",
        "  - Sources do not mention Onboard.",
        "    Documentation does not specify the port.",
        "* **Note:** Documentation does not specify the port.",
        "- Note: Sources do not mention Onboard.",
        "> **Note:** The provided documents do not specify X.",
        "  1. Sources do not mention Onboard.",
    ],
)
def test_new_absence_claims_block(line):
    failures = check_absence_claims(_change("# X\n" + line + "\n"), "# X\n")
    assert [f.check for f in failures] == ["absence"]
    assert failures[0].severity == "blocker"


@pytest.mark.parametrize(
    "line",
    [
        "pypdf does not perform OCR and cannot extract text from images.",
        "pypdf docs note pypdf does not include OCR.",
        "As open source software, pypdf does not provide OCR.",
        "This page does not cover installation.",
        "Gluetun does not support WireGuard for this provider.",
        "pypdf does not perform OCR; the docs are explicit.",
    ],
)
def test_product_negation_is_not_absence_claim(line):
    assert check_absence_claims(_change("# pypdf\n" + line + "\n"), "# pypdf\n") == []


def test_preexisting_absence_line_is_not_reblocked():
    line = "The committed fleet-hosts documentation does not specify the runtime version."
    body = "# X\n" + line + "\nNew sourced fact.\n"
    assert check_absence_claims(_change(body), "# X\n" + line + "\n") == []


@pytest.mark.parametrize(
    "line",
    [
        "MongoDB documents do not include a schema.",
        "Elasticsearch documents do not specify a type.",
        "Versioned docs do not include the beta API.",
        "The MongoDB documents do not include a schema.",
        "The documents do not include a schema field.",
        "These documents do not specify a type.",
        "  - Embedded documents do not contain references.",
    ],
)
def test_document_nouns_in_product_prose_pass(line):
    assert check_absence_claims(_change("# X\n" + line + "\n"), "# X\n") == []


@pytest.mark.parametrize(
    "line",
    [
        "Sources don’t mention Onboard.",
        "The source didn’t mention the host.",
        "* Documentation does not specify the port.",
        "> Sources do not mention the host.",
        "**Note:** Documentation does not state the version.",
        "- Sources do not mention Onboard.",
        "1. Documentation does not specify the port.",
        "Note: Documentation does not specify the port.",
    ],
)
def test_absence_claims_in_list_quote_and_curly_forms_block(line):
    failures = check_absence_claims(_change("# X\n" + line + "\n"), "# X\n")
    assert [f.check for f in failures] == ["absence"]
