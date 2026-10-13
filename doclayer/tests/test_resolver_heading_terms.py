"""Heading-named sections reach the pool; heading matching tolerates suffixes."""

from __future__ import annotations

import pytest
from test_resolver_answer_sections import _rank
from test_resolver_selection import page

from doclayer import resolver

WAL_Q = (
    "Where does App1's app-db PostgreSQL Docker data directory actually live "
    "(mount/backing filesystem), and what declared WAL/retention settings affect a fresh "
    "isolated app_bulk generation while preserving corrupt old pop? Narrow read-only BK2 "
    "capacity preflight; no DB writes or disk scans."
)

WAL_PAGE = page(
    "wiki/app-db-wal-filesystem-observation.md",
    """# app-db WAL Filesystem Observation

This page records dated, read-only observations of the backing filesystem and WAL storage settings for the `app-db` PostgreSQL container.

## Backing Filesystem

The `app-data` Docker volume is backed by `/dev/sdb1`, mounted at `/mnt/fast` [source: src-be72cad4c7].

The Docker volume path `/mnt/fast/docker/volumes/app-data_pgdata/_data` is mounted at `/var/lib/postgresql/data` inside the `app-db` container.

## WAL and Retention Settings (app_bulk)

The following WAL-related settings were observed via `psql -U app_read -d app_bulk` [source: src-be72cad4c7].

| Setting | Value | Source |
|---|---|---|
| `wal_level` | `replica` | default [source: src-be72cad4c7] |
| `max_wal_size` | `1024 MB` | configuration file [source: src-be72cad4c7] |
| `max_slot_wal_keep_size` | `-1 MB` (no cap) | default [source: src-be72cad4c7] |

## Unanswered Questions

- What is the projected WAL demand for a fresh isolated `app_bulk` generation?
""",
    "app-db WAL Filesystem Observation",
)


def test_wal_and_retention_settings_section_is_delivered():
    facts = _rank([WAL_PAGE], WAL_Q)
    values = " ".join(f["value"] for f in facts)
    assert "max_wal_size" in values
    assert "max_slot_wal_keep_size" in values


@pytest.mark.parametrize(
    ("heading", "question"),
    [
        ("Doc / Installed CLI clients", "Bitwarden CLI installation"),
        ("Doc / Installation steps", "Which steps install it?"),
        ("Doc / Retention settings", "What settings apply to retained data?"),
        ("Doc / Retained data settings", "What is the retention policy for settings?"),
    ],
)
def test_heading_terms_match_across_suffix_variants(heading, question):
    assert resolver._heading_facets(heading, resolver._terms(question), set())


def test_unrelated_heading_terms_still_do_not_match():
    assert not resolver._heading_facets(
        "Doc / Installed clients", resolver._terms("What is the retail price of clients?"), set()
    )


def test_installed_section_is_delivered_for_an_installation_question():
    body = (
        "# Bitwarden clients\nBitwarden bw CLI clients on the workstation.\n\n"
        "## Changelog\nThe Bitwarden bw CLI changelog is long.\n\n"
        "The Bitwarden bw CLI changelog has many entries.\n\n"
        "The Bitwarden bw CLI changelog is kept upstream.\n\n"
        "## Installed CLI clients\nThe bw package ships as a snap.\n"
    )
    facts = _rank([page("wiki/bw.md", body, "Bitwarden clients")], "Bitwarden bw CLI installation")
    assert any("ships as a snap" in f["value"] for f in facts)


def test_a_heading_term_shared_by_every_section_does_not_expand_the_page():
    body = "# Prometheus\nPrometheus gadget notes.\n\n" + "".join(
        f"## Prometheus part {n}\nThe prometheus gadgets note number {n}.\n\n" for n in range(6)
    )
    facts = _rank([page("wiki/prometheus.md", body, "Prometheus")], "What does Prometheus say about gadgets?")
    assert len(facts) <= 2
