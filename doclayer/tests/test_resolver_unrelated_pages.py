"""Heading-driven expansion never pulls unrelated pages into an answer.

Fixtures are shaped on real vault pages.
"""

from __future__ import annotations

from test_resolver_answer_sections import BITWARDEN, BITWARDEN_Q, STATUS, _rank
from test_resolver_expansion_budget import Q_7DE, _pages
from test_resolver_heading_terms import WAL_PAGE, WAL_Q
from test_resolver_selection import page

from doclayer import resolver


def sources(facts):
    return {f["source"].split("@", 1)[0] for f in facts}


def text(facts):
    return " ".join(f["value"] for f in facts)


ACCESS = page(
    "wiki/app-db-app-1-access.md",
    """# app-db App 1 Access

## Deployment location

app-db is the container `app-db` (image `postgres:17`) on App1, published as `192.0.2.228:5437->5432/tcp`. It runs from compose directory `/opt/stacks/app-data`.

Databases on the server (non-template): `meetpro`, `postgres`, `app`, `app_bulk`, `app_stage`. `app_bulk` is 2678 MB.

## Read-only access route

Routes that were executed on 2026-10-05:

- Direct from ws1 as the read-only role: `psql -h 192.0.2.228 -p 5437 -U app_read -d app` connected and returned `current_user = app_read`.

- Through the house helper: `PGREMOTE_USER=app_owner psql-remote App1 app-db <db>` with SHOW or SELECT statements.

Roles: `app_owner` (superuser, createdb; used by the loader), `app_read` (no createdb, not superuser; for analysis and other projects), `app_analyst` (receives `SELECT` on `app_bulk.pop`).

Passwords live in `/opt/stacks/app-data/.env` on App1 (root-only) and `~/.pgpass` on the workstation.

## Data directory and backing filesystem

Inside the container `data_directory` is `/var/lib/postgresql/data`, the bind mount from `/mnt/fast/docker/volumes/app-data_pgdata/_data`.

## Backups

`/opt/stacks/app-data/backup.sh` runs from cron at 03:15 and appends to `/data/backups/app-data/backup.log`.

## app_bulk

`app_bulk` exists on app-db with owner `app_owner`. The BK2 bulk-build bundle created it per the operator's decision.

The BK2 text says to run `CREATE DATABASE app_bulk OWNER app_owner` only if `app_owner` cannot create databases.
""",
    "app-db App 1 Access",
)

CORPUS_AUDIT = page(
    "wiki/vo-rag-corpus-audit-capability-and-evidence-sources.md",
    """# vo-rag Corpus Audit Capability and Evidence Sources

## Read-only access routes

- **Qdrant REST (collection roster and counts).** `GET http://localhost:6400/collections` on App1 returned HTTP 200 without credentials.

- **`GET /api/v1/collections/stats`** (vo-rag API, port 8001) returns exact point counts for every Qdrant collection.

- **`ops/corpus_audit.py`** is the corpus-wide chunk-quality profiler; its docstring says it is read-only and scans every book.
""",
    "vo-rag Corpus Audit Capability and Evidence Sources",
)

VORAG_TOPOLOGY = page(
    "wiki/vo-rag-production-deployment-topology.md",
    """# vo-rag Production Deployment Topology

## Brain doc layer components on App1

The brain indexer `/home/user/vo-rag-brain-sync` and the doc fetcher timer run on App1; the resolver is declared to run on the operator workstation.
""",
    "vo-rag Production Deployment Topology",
)

FLEET = page(
    "infra/app1-containers.md",
    """# App1 containers

## Docker containers on App1

Docker containers running on App1: `app-db`, `nimbus-api`, `vo-rag-qdrant`.

Each Docker container on App1 is declared in its own compose directory under `/opt/stacks`.
""",
    "App1 containers",
)

DELL = page(
    "wiki/laptop-1-gpu-verification-runtime.md",
    """# laptop-1 GPU Verification Runtime

## SSH access

- Use the ssh config alias `laptop-1` (user `user`). Key-based login works non-interactively.

- The host reports hostname `laptop1` over ssh.
""",
    "laptop-1 GPU Verification Runtime",
)

RUNBOOK = page(
    "wiki/brain-curator-runbook.md",
    """# Brain Curator Runbook

How the brain curator and its knowledge-gap worker run: budgets and selection of pending gaps.

## Dedup and selection

Records with the same identity are merged into one gap by the worker.

Only pending gaps with a verified subject are submitted each cycle.
""",
    "Brain Curator Runbook",
)

ASTRA = page(
    "wiki/gpt-6-astra.md",
    """# GPT-6 Astra

## Model Selection Context

*   **GPT-6 Astra:** Highest capability; use it to decide ambiguous problems and ambitious deliverables.

*   **GPT-6 Luna:** Smart and efficient model; use it to decide scoped tasks, triage and frequent automations.
""",
    "GPT-6 Astra",
)
DEDUP_Q = "How does the brain knowledge-gap worker deduplicate records and decide which pending gaps to submit to the curator?"


def test_wal_rows_delivered_without_the_read_only_routes_of_an_unrelated_page():
    facts = _rank([WAL_PAGE, ACCESS, CORPUS_AUDIT], WAL_Q)
    assert "max_wal_size" in text(facts) and "max_slot_wal_keep_size" in text(facts)
    assert "wiki/vo-rag-corpus-audit-capability-and-evidence-sources.md" not in sources(facts)


def test_release_and_rollback_delivered_without_an_unrelated_ssh_access_section():
    facts = _rank([_pages()[0], DELL], Q_7DE)
    assert "Rollback of a production release" in text(facts)
    assert "wiki/laptop-1-gpu-verification-runtime.md" not in sources(facts)


def test_dedup_and_selection_kept_but_not_an_unrelated_model_selection_section():
    facts = _rank([RUNBOOK, ASTRA], DEDUP_Q)
    assert "merged into one gap" in text(facts) and "submitted each cycle" in text(facts)
    assert "wiki/gpt-6-astra.md" not in sources(facts)


def test_bitwarden_answer_keeps_server_and_login_and_adds_no_unrelated_page():
    facts = _rank([BITWARDEN, STATUS, VORAG_TOPOLOGY, CORPUS_AUDIT], BITWARDEN_Q)
    assert "vault.example.org" in text(facts)
    assert sources(facts) <= {BITWARDEN.path, STATUS.path}


def test_installed_clients_heading_with_one_shared_term_is_not_an_answer_section():
    # "Installed clients" shares only "install" with the question ("clients" is not "cli").
    assert not resolver._heading_facets("Doc / Installed clients", resolver._terms(BITWARDEN_Q), set())


def _on_topic_pages(phrase, count=5):
    """Enough strongly on-topic pages that the cap-8 pass lands only on them."""
    return [
        page(f"wiki/topic-{n}.md", f"# Topic {n}\n{phrase} variant {n}.\n\n{phrase} detail {n}.\n", f"Topic {n}")
        for n in range(count)
    ]


def test_docker_containers_question_has_no_unrelated_pages():
    question = "Which Docker containers run on App1?"
    pages = [FLEET, *_on_topic_pages("Docker containers run on App1 as compose services"), VORAG_TOPOLOGY, DELL]
    facts = _rank(pages, question)
    assert "app-db" in text(facts)
    assert not sources(facts) & {VORAG_TOPOLOGY.path, DELL.path}


def test_track_db_access_route_keeps_on_topic_statements_and_no_unrelated_pages():
    question = "What is the read-only access route for the app-db database on App1?"
    pages = [
        ACCESS,
        *_on_topic_pages("The app-db database on App1 is reached through psql", 3),
        CORPUS_AUDIT,
        VORAG_TOPOLOGY,
        DELL,
        FLEET,
    ]
    facts = _rank(pages, question)
    assert "Direct from ws1 as the read-only role" in text(facts)
    assert "Through the house helper" in text(facts)
    assert "Roles: `app_owner`" in text(facts)
    assert not sources(facts) & {CORPUS_AUDIT.path, VORAG_TOPOLOGY.path, DELL.path}


def test_function_and_generic_words_do_not_name_a_section():
    q = resolver._terms("What is the read-only access route for the data on App1?")
    for heading in ("Doc / Read-only access routes", "Doc / Data access on", "Doc / Overview and details of notes"):
        assert not resolver._heading_facets(heading, q, set())


def test_generic_words_count_on_a_landed_page_but_not_off_it():
    q = resolver._terms("What is the read-only access route for the app-db database on App1?")
    heading = "app-db App 1 Access / Read-only access route"
    assert not resolver._heading_facets(heading, q, set())
    assert resolver._heading_facets(heading, q, set(), landed=True)
    # A heading made only of question words qualifies even as a single word.
    assert resolver._heading_facets("Doc / Database", q, set(), landed=True)
    # One shared word among other words does not.
    assert not resolver._heading_facets("Doc / Backups and database dumps", q, set(), landed=True)
    # Function words alone never qualify.
    assert not resolver._heading_facets("Doc / On the", q, set(), landed=True)


def test_categorical_heading_credit_needs_a_page_about_the_question():
    audit = page(
        "wiki/vo-rag-corpus-audit-capability-and-evidence-sources.md",
        "# vo-rag Corpus Audit\n\n## Approved source artifacts\n\n`docs/architecture/collections.md` explains each collection's purpose and code layout.\n",
        "vo-rag Corpus Audit",
    )
    facts = _rank([audit], "Which model should I choose for code review?")
    assert audit.path not in sources(facts)
