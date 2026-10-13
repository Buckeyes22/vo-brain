"""Expansion beyond the 2-per-page cap is relevance-gated and capped at 16,000 chars."""

from __future__ import annotations

import pytest
from test_resolver_answer_sections import _rank
from test_resolver_heading_terms import WAL_PAGE, WAL_Q
from test_resolver_selection import Repo, page, sources  # noqa: F401

from doclayer import resolver

EXPANSION_CHARS = 16_000
Q_7DE = (
    "Where is Nimbus production deployed, and what existing service/API/SSH access and "
    "release procedure should be used for the TAF selected-station nowcast/live workflows?"
)


def _pages():
    pad = " Details are kept in the runbook." * 10
    app = page(
        "wiki/nimbus-metar-taf-application-app-1.md",
        "# Nimbus METAR/TAF application on App1\n"
        "Nimbus production is deployed on App1; TAF selected-station nowcast/live workflows use the installed checkout.\n\n"
        "## Release procedure\n"
        "Release the TAF selected-station workflow with the deployment-validation gate before the next deploy.\n\n"
        "Rollback of a production release stops the supervisor service and checks out the previous tag.\n\n"
        "Service order for a production release is migrations, API, then workflows.\n\n"
        "## SSH access\n"
        "Production SSH access for the TAF workflows uses the App1 alias.\n",
        "Nimbus METAR/TAF application on App1",
    )
    sources_body = "# Nimbus data sources\nNimbus TAF selected-station data sources.\n\n" + "".join(
        f"## Source {n} for TAF nowcast workflows\nTAF selected-station nowcast workflow source {n} feeds the model.{pad}\n\n"
        for n in range(30)
    )
    return [app, page("wiki/nimbus-data-sources.md", sources_body, "Nimbus data sources")]


@pytest.mark.asyncio
@pytest.mark.usefixtures("sources")
async def test_release_question_stays_under_20k_chars_and_keeps_the_release_answer(tmp_path):
    answer = await resolver.resolve(Q_7DE, repo=Repo(tmp_path, _pages()), live=False, capture=False)
    prose = answer._prose()
    assert len(prose) < 20_000
    assert "Rollback of a production release" in prose
    assert "Service order for a production release" in prose


def test_expansion_chars_are_capped_across_all_pages():
    facts = _rank(_pages(), Q_7DE)
    contexts = {(f["source"], f["evidence_context"]): len(f["evidence_context"]) for f in facts}
    # The first two facts per page are the original selection; everything after
    # them is expansion and shares one 16,000-character allowance.
    originals = {}
    for fact in facts:
        originals.setdefault(fact["source"], []).append(fact)
    original_slots = {(f["source"], f["evidence_context"]) for group in originals.values() for f in group[:2]}
    expansion = sum(size for slot, size in contexts.items() if slot not in original_slots)
    assert expansion <= EXPANSION_CHARS


def test_original_selection_is_never_displaced_by_expansion(monkeypatch):
    answer = resolver.Answer(Q_7DE, None)
    for p in _pages():
        resolver._append_page_facts(answer, p, Q_7DE)
    with_expansion = [f["value"] for f in resolver._rank_facts(answer.git_facts, Q_7DE, 8)]
    monkeypatch.setattr(resolver, "EXPANSION_CHARS", 0)
    original = [f["value"] for f in resolver._rank_facts(answer.git_facts, Q_7DE, 8)]
    assert original and len(with_expansion) > len(original)
    # Expansion only appends: the original selection keeps its facts and order.
    assert with_expansion[: len(original)] == original


def test_wal_rows_are_still_delivered_under_the_expansion_cap():
    facts = _rank([WAL_PAGE], WAL_Q)
    values = " ".join(f["value"] for f in facts)
    assert "max_wal_size" in values and "max_slot_wal_keep_size" in values


def test_a_single_shared_heading_term_does_not_qualify_a_section():
    assert not resolver._heading_facets("Doc / Release notes", resolver._terms("Which release is deployed?"), set())
    assert resolver._heading_facets(
        "Doc / Release procedure", resolver._terms("What release procedure applies?"), set()
    )
