"""A paragraph's bold lead-in label names its topic the way a section heading does.

gap-d8ef18d93f7a2efe5fe8: the answering paragraph opens with a bold label that
names the question ("Backing filesystem and WAL headroom for a preserved
app_bulk generation") under a generic "Current reading" heading. Its words
never say "storage", so the requested-aspect check dropped it while table rows
under a heading that names the question were kept.
"""

from __future__ import annotations

import re

from test_resolver_answer_sections import _rank
from test_resolver_selection import page

from doclayer import resolver

GAP_Q = "App1 app-db backing filesystem and WAL storage headroom for preserved app_bulk generation"

LEAD = (
    "**Backing filesystem and WAL headroom for a preserved `app_bulk` generation, as of "
    "2026-10-06:** the `app-db` data directory is on `/dev/sdb1` mounted at `/mnt/fast` "
    "(ext4), which had 23,361,626,112 bytes available (98% used) at 01:57 EDT; `pg_wal` held "
    "989,855,744 bytes in 59 files; `app_bulk` itself is 7165 MB. Headroom here means free "
    "bytes on `/mnt/fast` now; no allocation for a new generation is recorded as approved, and "
    "existing data was preserved (nothing was deleted or written). "
    "[source: live check App1 2026-10-06]"
)

WAL_PAGE = page(
    "wiki/app-db-wal-filesystem-observation.md",
    f"""# app-db WAL Filesystem Observation

This page records dated, read-only observations of the backing filesystem and WAL storage settings for the `app-db` PostgreSQL container and the `app_bulk` database on the App1 host.

## Current reading (2026-10-06)

{LEAD}

## Earlier observation (2026-10-04): backing filesystem

The `app-data` Docker volume is backed by `/dev/sdb1`, mounted at `/mnt/fast` [source: src-be72cad4c7]. At the time of observation, the filesystem had:

- **Available:** 33,073,586,176 bytes

The Docker volume path `/mnt/fast/docker/volumes/app-data_pgdata/_data` is mounted at `/var/lib/postgresql/data` inside the `app-db` container [source: src-be72cad4c7].

## Earlier observation (2026-10-04): WAL and retention settings (app_bulk)

The following WAL-related settings were observed via `psql -U app_read -d app_bulk` [source: src-be72cad4c7].

| Setting | Value | Source |
|---|---|---|
| `wal_level` | `replica` | default [source: src-be72cad4c7] |
| `wal_keep_size` | `0 MB` | default [source: src-be72cad4c7] |
| `min_wal_size` | `80 MB` | configuration file [source: src-be72cad4c7] |
| `max_wal_size` | `1024 MB` | configuration file [source: src-be72cad4c7] |
| `max_slot_wal_keep_size` | `-1 MB` (no cap) | default [source: src-be72cad4c7] |
""",
    "app-db WAL Filesystem Observation",
    tags=["app-db", "app_bulk", "wal", "filesystem", "app-1"],
)


def _is_lead(fact):
    return fact["value"].startswith("**Backing filesystem and WAL headroom")


def test_bold_lead_in_paragraph_answering_the_question_is_delivered():
    facts = _rank([WAL_PAGE], GAP_Q)
    assert any(_is_lead(fact) for fact in facts), [fact["value"][:60] for fact in facts]


def test_lead_in_paragraph_outranks_table_rows_matching_fewer_terms():
    facts = _rank([WAL_PAGE], GAP_Q)
    order = [fact["value"] for fact in facts]
    lead = next(index for index, fact in enumerate(facts) if _is_lead(fact))
    rows = [index for index, value in enumerate(order) if value.startswith("`") and "|" in value]
    assert rows and all(lead < index for index in rows), order


def test_lead_in_label_sharing_one_term_does_not_admit_an_aspectless_statement():
    body = (
        "# Disk notes\n\nGeneral notes.\n\n## Current reading\n\n**Backing note:** the volume was renamed last week.\n"
    )
    facts = _rank([page("wiki/disk-notes.md", body, "Disk notes")], "backing storage size of the archive volume")
    assert not any("renamed" in fact["value"] for fact in facts)


def test_lead_in_label_is_read_only_from_the_start_of_a_statement():
    assert resolver._lead_in_label("**Backing filesystem and WAL headroom, as of 2026-10-06:** the value") == (
        "Backing filesystem and WAL headroom, as of 2026-10-06"
    )
    assert resolver._lead_in_label("- **Available:** 33 bytes") == "Available"
    assert resolver._lead_in_label("**Available**: 33 bytes") == "Available"
    # Emphasis inside a sentence, or bold without a colon, is not a label.
    assert resolver._lead_in_label("The slots were **0 replication slots** total.") == ""
    assert resolver._lead_in_label("**bw CLI.** Installed as the snap.") == ""


CONTINUED = page(
    "wiki/app-db-wal-filesystem-observation.md",
    f"""# app-db WAL Filesystem Observation

This page records dated, read-only observations of the backing filesystem and WAL storage settings for the `app-db` PostgreSQL container.

## Current reading (2026-10-06)

{LEAD}

Read-only reading through `psql-remote App1 app-db app_bulk` and `df` on App1:

- `pg_ls_waldir()` lists 54 WAL files totalling 905,969,664 bytes (864 MB) in the `pg_wal` directory, which sits on `/mnt/fast` (`/dev/sdb1`, ext4).
- `app_bulk` is 7165 MB (2678 MB on 2026-10-05).

**Retention note:** checked once.

- The WAL directory then held 61 files totalling 1,023,410,176 bytes.

The note above was taken during the BK2 preflight.

- A later `pg_wal` listing held 63 files totalling 1,056,964,608 bytes.

## Earlier observation (2026-10-04)

- `pg_wal` held 40 WAL files totalling 671,088,640 bytes.
""",
    "app-db WAL Filesystem Observation",
    tags=["app-db", "app_bulk", "wal", "filesystem", "app-1"],
)


def _values(question=GAP_Q):
    return " ".join(fact["value"] for fact in _rank([CONTINUED], question))


def test_bullets_continuing_an_admitted_lead_in_are_admitted():
    values = _values()
    assert "905,969,664 bytes" in values
    assert "7165 MB (2678 MB" in values


def test_bullets_after_the_next_lead_in_heading_or_prose_break_are_not_admitted():
    values = _values()
    # The next lead-in ("Retention note") does not name the question.
    assert "1,023,410,176" not in values
    # Ordinary prose after a blank line ends the block.
    assert "1,056,964,608" not in values
    # So does the next heading.
    assert "671,088,640" not in values


def test_continuation_is_tracked_per_statement():
    answer = resolver.Answer(GAP_Q, None)
    resolver._append_page_facts(answer, CONTINUED, GAP_Q)
    admitted = [fact["value"] for fact in answer.git_facts]
    assert any("905,969,664" in value for value in admitted)
    assert not any(
        marker in value for value in admitted for marker in ("1,023,410,176", "1,056,964,608", "671,088,640")
    )


# Review round: subject link, block ends and nesting.

RUNTIME_PAGE = page(
    "infra/agent-runtime-gateway.md",
    """# Agent-runtime Gateway

Agent-runtime Gateway runs on worker1; its source repository is mirrored on Forgejo, see [[infra/forgejo]].

## Services

**Control API:** `worker1:8898` (`gateway-control`) accepts control commands from the console,
queues control jobs for the fleet profiles,
reports control job status to the operator,
and exposes control health checks.
""",
    "Agent-runtime Gateway",
)

FORGEJO_Q = "What control API does Forgejo expose?"


def test_label_on_a_page_about_something_else_neither_admits_nor_bypasses_the_cap():
    facts = _rank([RUNTIME_PAGE], FORGEJO_Q)
    wrapped = ("queues control jobs", "reports control job status", "exposes control health")
    assert not any(line in fact["value"] for fact in facts for line in wrapped), [f["value"][:60] for f in facts]
    answer = resolver.Answer(FORGEJO_Q, None)
    resolver._append_page_facts(answer, RUNTIME_PAGE, FORGEJO_Q)
    assert not any(fact["_lead_in_heading"] for fact in answer.git_facts)


def _labels(body):
    lines = body.splitlines()
    return {
        lines[index].strip(): tuple(label for label, _ in labels)
        for index, labels in resolver._lead_in_blocks(body).items()
    }


def test_bold_list_item_without_a_label_starts_its_own_block():
    blocks = _labels(
        "- **Backing filesystem:** 23 GB free.\n- **Kernel ring buffer** dropped messages.\n- plain item.\n"
    )
    assert blocks["- **Kernel ring buffer** dropped messages."] == ()
    assert blocks["- plain item."] == ()


def test_sibling_list_item_does_not_inherit_a_sibling_label():
    blocks = _labels("- **Backing filesystem:** 23 GB free.\n- an unrelated item.\n")
    assert blocks["- an unrelated item."] == ()


def test_labelled_sub_bullets_stay_in_the_parent_block():
    blocks = _labels(
        "**Backing filesystem and WAL headroom:** as of today.\n\n"
        "- **Available:** 23 GB\n- **pg_wal:** 989 MB\n  - nested detail\n- plain sibling\n"
    )
    assert blocks["- **Available:** 23 GB"] == ("Backing filesystem and WAL headroom", "Available")
    assert blocks["- **pg_wal:** 989 MB"] == ("Backing filesystem and WAL headroom", "pg_wal")
    assert blocks["- nested detail"] == ("Backing filesystem and WAL headroom", "pg_wal")
    assert blocks["- plain sibling"] == ("Backing filesystem and WAL headroom",)


def test_numbered_list_label_is_recognised():
    assert resolver._lead_in_label("1. **Backing filesystem:** 23 GB") == "Backing filesystem"
    assert _labels("1. **Backing filesystem:** 23 GB\n   - sda1\n")["- sda1"] == ("Backing filesystem",)


def test_prose_straight_after_a_code_fence_does_not_inherit():
    blocks = _labels("**Backing filesystem:** measured with\n```\ndf -B1\n```\nUnrelated prose.\n")
    assert blocks["Unrelated prose."] == ()


def test_indented_paragraph_and_html_comment_continue_the_block():
    blocks = _labels(
        "**Backing filesystem:** measured.\n\n- WAL was 989 MB.\n\n  An indented continuation.\n\n"
        "  - nested bullet\n\n<!-- note -->\n- after the comment\n"
    )
    assert blocks["An indented continuation."] == ("Backing filesystem",)
    assert blocks["- nested bullet"] == ("Backing filesystem",)
    assert blocks["- after the comment"] == ("Backing filesystem",)


def test_a_table_under_a_label_belongs_to_it_with_or_without_a_blank_line():
    table = "| Setting | Value |\n|---|---|\n| `max_wal_size` | `1024 MB` |\n"
    for gap in ("\n", "\n\n"):
        blocks = _labels("**Backing filesystem:** settings follow." + gap + table)
        assert blocks["| `max_wal_size` | `1024 MB` |"] == ("Backing filesystem",), repr(gap)


def test_table_rows_under_a_naming_label_are_admitted_in_both_forms():
    table = "| Setting | Value |\n|---|---|\n| `max_wal_size` | `1024 MB` |\n"
    for gap in ("\n", "\n\n"):
        body = "# app-db WAL Filesystem Observation\n\nIntro.\n\n## Current reading\n\n" + LEAD + gap + table
        answer = resolver.Answer(GAP_Q, None)
        resolver._append_page_facts(
            answer, page("wiki/app-db-wal-filesystem-observation.md", body, "app-db WAL Filesystem Observation"), GAP_Q
        )
        assert any("max_wal_size" in fact["value"] for fact in answer.git_facts), repr(gap)


def test_ordinary_prose_after_a_blank_line_ends_a_naming_block():
    body = (
        "# app-db WAL Filesystem Observation\n\nIntro.\n\n## Current reading\n\n"
        + LEAD
        + "\n\n- `pg_wal` held 989,855,744 bytes in 59 files.\n\n"
        "The team reviewed the dashboards afterwards.\n\n"
        "- A later `pg_wal` listing held 1,056,964,608 bytes in 63 WAL files.\n"
    )
    answer = resolver.Answer(GAP_Q, None)
    resolver._append_page_facts(
        answer, page("wiki/app-db-wal-filesystem-observation.md", body, "app-db WAL Filesystem Observation"), GAP_Q
    )
    admitted = " ".join(fact["value"] for fact in answer.git_facts)
    assert "989,855,744" in admitted
    assert "1,056,964,608" not in admitted


def test_a_label_only_line_is_not_delivered_as_a_fact():
    body = (
        "# app-db WAL Filesystem Observation\n\nIntro.\n\n## Current reading\n\n"
        "- **Backing filesystem and WAL headroom for a preserved app_bulk generation:**\n"
        "  - the `/mnt/fast` filesystem had 23,361,626,112 bytes available.\n"
    )
    facts = _rank([page("wiki/app-db-wal-filesystem-observation.md", body, "app-db WAL Filesystem Observation")], GAP_Q)
    assert all(re.sub(r"[*:\s-]", "", fact["value"]) and not resolver._label_only(fact["value"]) for fact in facts)
    assert any("23,361,626,112" in fact["value"] for fact in facts)


FORGEJO = page(
    "infra/forgejo.md",
    """# Forgejo

The fleet's self-hosted git host.

## Repos

- **Canonical repos:**
  - `Example-org/brain` — this vault, the memory-of-record.
  - `Example-org/Gateway` — the Gateway system.
- **Auth:** git credential store on each host.
""",
    "Forgejo",
)


def test_a_bare_label_is_delivered_with_the_items_of_its_block():
    facts = _rank([FORGEJO], "What are the canonical repos on Forgejo?")
    label = next(fact["value"] for fact in facts if fact["value"].startswith("**Canonical repos:**"))
    assert "Example-org/brain" in label and "Example-org/Gateway" in label
    assert "credential store" not in label
    assert not any(resolver._label_only(fact["value"]) for fact in facts)


# Bare-label admission per item, citation lines, long items, duplicates.

SPLIT = page(
    "infra/forgejo.md",
    """# Forgejo

The fleet's self-hosted git host.

## Repos

- **Canonical repos:**
  - `Example-org/brain` — this vault, the memory-of-record.
  - `Example-org/vo-rag` — the RAG pipeline and portal.
""",
    "Forgejo",
)


def test_a_bare_label_is_not_admitted_on_terms_spread_across_items():
    question = "What memory provider does vo-rag use?"
    answer = resolver.Answer(question, "vo-rag")
    resolver._append_page_facts(answer, SPLIT, question)
    assert not any("Canonical repos" in fact["value"] for fact in answer.git_facts)


def test_a_bare_label_admitted_on_one_item_takes_that_items_terms():
    question = "What are the canonical repos on Forgejo?"
    answer = resolver.Answer(question, None)
    resolver._append_page_facts(answer, SPLIT, question)
    joined = [fact for fact in answer.git_facts if fact["value"].startswith("**Canonical repos:**")]
    assert len(joined) == 1
    assert "Example-org/brain" in joined[0]["value"] and "Example-org/vo-rag" in joined[0]["value"]


def test_a_line_without_a_label_is_never_a_bare_label():
    assert not resolver._label_only("[source: src-be72cad4c7]")
    assert not resolver._label_only("---")
    assert resolver._label_only("- **Canonical repos:**")
    body = (
        "# Forgejo\n\nThe git host.\n\n## Repos\n\n[source: src-1]\n\n## Steps\n\n"
        "- the canonical repos are mirrored nightly.\n- the canonical repos are backed up weekly.\n"
    )
    question = "What are the canonical repos on Forgejo?"
    answer = resolver.Answer(question, None)
    resolver._append_page_facts(answer, page("infra/forgejo.md", body, "Forgejo"), question)
    assert not any("src-1" in fact["value"] for fact in answer.git_facts)


def test_a_first_item_over_the_cap_is_cut_not_dropped():
    item = "`Example-org/brain` " + "x" * 1300
    value, delivered = resolver._bare_label_value("- **Canonical repos:**", [item, "`Example-org/vo-rag`"])
    assert len(delivered) == 1 and item.startswith(delivered[0])
    assert value.startswith("- **Canonical repos:** `Example-org/brain` xxx") and value.endswith("…")
    assert len(value) <= resolver.BARE_LABEL_CHARS


def test_joined_items_are_not_repeated_and_sentences_join_without_semicolons():
    facts = _rank([SPLIT], "What are the canonical repos on Forgejo?")
    values = [fact["value"] for fact in facts]
    assert sum("Example-org/brain" in value for value in values) == 1
    joined = next(value for value in values if value.startswith("**Canonical repos:**"))
    assert ".;" not in joined
    assert "memory-of-record. `Example-org/vo-rag`" in joined
    value, _ = resolver._bare_label_value("**Pages:**", ["`a.md`", "`b.md`"])
    assert value == "**Pages:** `a.md`; `b.md`"


def test_a_cut_first_item_is_admitted_on_its_kept_text_only():
    item = "`Example-org/brain` " + "x " * 700 + "zebrafish"
    body = "# Forgejo\n\nThe git host.\n\n## Repos\n\n- **Canonical repos:**\n  - " + item + "\n"
    question = "Which zebrafish are recorded?"
    answer = resolver.Answer(question, None)
    resolver._append_page_facts(answer, page("infra/forgejo.md", body, "Forgejo"), question)
    assert not any("Canonical repos" in fact["value"] for fact in answer.git_facts)


def test_the_ellipsis_fits_inside_the_cap():
    exact = resolver.BARE_LABEL_CHARS - len("**Label:** ")  # first item fills the cap exactly
    for first in [exact, exact - 1, exact - 2, *range(1, 1300, 7)]:
        items = ["a" * first, "b" * 50, "c" * 50]
        value, delivered = resolver._bare_label_value("**Label:**", items)
        assert len(value) <= resolver.BARE_LABEL_CHARS, first
        assert delivered
