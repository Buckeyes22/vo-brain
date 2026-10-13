"""Delivery keeps every answer section of a page the question lands on.

Built from real retrieval_defect gaps: the landed page was cited, but the
2-per-page cap dropped the sections that answered the question.
"""

from __future__ import annotations

import pytest
from test_resolver_selection import Repo, page, sources  # noqa: F401

from doclayer import resolver
from doclayer.resolver_context import ANSWER_EVIDENCE_CHARS


def _rank(pages, question, cap=8):
    answer = resolver.Answer(question, None)
    for p in pages:
        resolver._append_page_facts(answer, p, question)
    return resolver._rank_facts(answer.git_facts, question, cap)


BITWARDEN = page(
    "wiki/bitwarden-vaultwarden-clients-ws1.md",
    """# Bitwarden and Vaultwarden clients on ws1
Sam's workstation ws1 has three Bitwarden clients installed (CLI, desktop app, browser extension).

## Installed clients
**bw CLI.** Installed as the snap `bw`, version 2026.9.1, installation channel stable.

**Desktop app.** Installed as the snap `bitwarden`, installation channel stable.

## Configured server
The bw CLI is configured for `https://vault.example.org`, the Vaultwarden server instance.

## Login method
Login is manual: Vaultwarden is locked by default, so the interactive login needs Sam.

## Current session state
The bw CLI reports status `locked` for the interactive login session.
""",
    "Bitwarden and Vaultwarden clients on ws1",
)
STATUS = page(
    "doclayer/STATUS.md",
    "# Status\nNo plan task yet: Bitwarden bw CLI and Vaultwarden server login on the workstation.\n",
    "Status",
)
BITWARDEN_Q = "Bitwarden bw CLI installation, configured Vaultwarden server, and interactive login on Sam's workstation"


def test_configured_server_section_is_delivered_with_the_landed_page():
    facts = _rank([BITWARDEN, STATUS], BITWARDEN_Q)
    values = " ".join(f["value"] for f in facts)
    assert "vault.example.org" in values
    assert "Login is manual" in values


@pytest.mark.asyncio
@pytest.mark.usefixtures("sources")
async def test_landed_page_answer_sections_are_cited_in_the_delivered_answer(tmp_path):
    answer = await resolver.resolve(BITWARDEN_Q, repo=Repo(tmp_path, [BITWARDEN, STATUS]), live=False, capture=False)
    prose = answer._prose()
    assert "https://vault.example.org" in prose
    assert any(
        f["source"].startswith("wiki/bitwarden-vaultwarden-clients-ws1.md@") and "vault.example.org" in f["value"]
        for f in answer.git_facts
    )


OSK = page(
    "wiki/ws1-orca-accessibility-state.md",
    """# Orca Accessibility State on ws1
Read-only checks on ws1 on 2026-10-05.

## Installed OSK applications
No on-screen keyboard application is installed on the local workstation.

GNOME's own on-screen keyboard switch is off on the local workstation.

## Apt candidates
`matchbox-keyboard` candidate for an on-screen keyboard is available.

Unprivileged extraction route for the keyboard packages is documented.

## Isolated Xvfb test route
`Xvfb` is present at `/usr/bin/Xvfb` for an isolated keyboard test route.

`xdotool` is not installed, so no on-screen keyboard test route can type.

Consequence: an isolated Xvfb display exists but no installed on-screen keyboard can run there.
""",
    "Orca Accessibility State on ws1",
)
OSK_Q = "Which on-screen keyboard applications and isolated on-screen keyboard test routes are installed on Sam's local workstation?"


def test_isolated_test_route_section_is_delivered_for_a_facetless_question():
    facts = _rank([OSK], OSK_Q)
    values = " ".join(f["value"] for f in facts)
    assert "Xvfb" in values and "xdotool" in values


DEDUP = page(
    "wiki/brain-curator-runbook.md",
    """# Brain Curator Runbook
How the brain curator and its knowledge-gap worker run: budgets and selection of pending gaps.

## Dedup and selection
Records with the same identity are merged into one gap by the worker.

Only pending gaps with a verified subject are submitted each cycle.

Deduplicated records keep the earliest question as the representative.

## Budgets
The worker limits model calls per job.
""",
    "Brain Curator Runbook",
)
DEDUP_Q = "How does the brain knowledge-gap worker deduplicate records and decide which pending gaps to submit to the curator?"


def test_categorical_aspect_is_credited_from_the_section_heading():
    facts = _rank([DEDUP], DEDUP_Q)
    values = " ".join(f["value"] for f in facts)
    assert "merged into one gap" in values
    assert "submitted each cycle" in values


def test_facts_matching_no_facet_keep_the_two_per_page_cap():
    body = "# Nimbus Notes\n" + "".join(
        f"## Part {n}\nThe nimbus gadget note number {n} mentions gadgets.\n\n" for n in range(6)
    )
    facts = _rank([page("wiki/nimbus-notes.md", body, "Nimbus Notes")], "What do the nimbus gadget notes say?")
    assert 1 <= len(facts) <= 2


def test_facet_matching_facts_stay_within_the_delivery_budget():
    sections = "".join(
        f"## Storage layout {n}\nStorage layout {n} lives on volume {n} " + "x" * 3000 + "\n\n" for n in range(40)
    )
    facts = _rank(
        [page("wiki/big.md", "# Big\nBig storage layout notes.\n\n" + sections, "Big")],
        "What is the Big storage layout?",
    )
    assert 2 < len(facts) <= 8 * (ANSWER_EVIDENCE_CHARS // resolver.EVIDENCE_CHARS)
    contexts = {(f["source"], f["evidence_context"]): f["evidence_context"] for f in facts}
    used = sum(len(context) for context in contexts.values())
    # two original facts plus at most 16,000 characters of expansion
    assert used <= 2 * 3200 + resolver.EXPANSION_CHARS
    assert used <= ANSWER_EVIDENCE_CHARS


def test_working_notes_outside_canonical_roots_are_not_expanded():
    body = "# Notes\nBitwarden vault notes.\n\n" + "".join(
        f"## Configured server {n}\nConfigured Vaultwarden server note {n}.\n\n" for n in range(6)
    )
    facts = _rank([page("doclayer/notes.md", body, "Notes")], BITWARDEN_Q)
    assert len(facts) <= 2
