"""Unit tests for the resolver's pure parts: fact extraction, flags, rendering.

Network sources (vo-rag, Context7, live checks) are faked; their live
behaviour is covered by the acceptance scenarios.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from doclayer.brainrepo import BrainPage, fact_rows
from doclayer.livechecks import LiveResult
from doclayer.resolver import (
    Answer,
    _clip,
    _context7_useful,
    _doc_copy_facts,
    _row_value,
    route_facts,
    unknown_flags,
)
from doclayer.settings import settings

# --------------------------------------------------------------------------- #
# Facts tables
# --------------------------------------------------------------------------- #


def _page(body: str, path: str = "wiki/x.md") -> BrainPage:
    return BrainPage(path=path, meta={"title": "x"}, body=body, commit="a" * 40)


def test_fact_rows_parses_three_columns():
    page = _page(
        "## Facts\n\n"
        "| Fact | Declared | Observed |\n|---|---|---|\n"
        "| Base URL | `https://x.test/apps` | — |\n"
        "| Models | `a`, `b` | 2026-09-30: same |\n"
    )
    rows = fact_rows(page)
    assert [(r.fact, r.declared, r.observed) for r in rows] == [
        ("Base URL", "`https://x.test/apps`", "—"),
        ("Models", "`a`, `b`", "2026-09-30: same"),
    ]


def test_fact_rows_ignores_other_tables():
    page = _page("## Pieces\n\n| Piece | Where | What |\n|---|---|---|\n| Resolver | doclayer | answers |\n")
    assert fact_rows(page) == []


def test_doc_copy_facts_models_and_urls():
    page = BrainPage(
        path="sources/prod/overview.md",
        meta={
            "url": "https://docs.test/overview",
            "fetched_at": "2026-09-30T00:00:00Z",
        },
        body=(
            "# Overview\n\n"
            "| Model | Features |\n|---|---|\n"
            "| `qwen3.8-max` | reasoning |\n"
            "| `qwen3.8-flash` | fast |\n"
            "- **Monthly quota**: A 30-day subscription cycle.\n"
            "Some prose that is not a fact line.\n"
        ),
        commit="b" * 40,
    )
    facts = _doc_copy_facts(page)
    names = {f["fact"] for f in facts}
    assert "qwen3.8-max" in names and "Monthly quota" in names
    assert all(f["source"] == "https://docs.test/overview@2026-09-30T00:00:00Z" for f in facts)


def test_row_value_table_and_bullet():
    assert _row_value("| `m` | reasoning |") == "reasoning"
    assert _row_value("- **Quota**: 30-day cycle.").startswith("Quota")


# --------------------------------------------------------------------------- #
# Context7 usefulness
# --------------------------------------------------------------------------- #


def test_context7_captcha_page_is_not_useful():
    assert not _context7_useful("### Speech Synthesis\n\nPlease slide to verify\n")
    assert not _context7_useful("short")
    assert _context7_useful("Real documentation " * 50)


# --------------------------------------------------------------------------- #
# Route facts + config split
# --------------------------------------------------------------------------- #


def test_route_facts_flag_config_split(tmp_path, monkeypatch):
    config = tmp_path / "tier-aliases.json"
    config.write_text(
        json.dumps(
            {
                "tier_aliases": {
                    "S": {
                        "providers": [
                            {
                                "provider": "zai",
                                "model": "glm-5.1",
                                "api_base": "https://z.ai",
                            },
                            {
                                "provider": "alibaba-token-plan",
                                "model": "qwen3.8-max",
                                "api_base": "https://token-plan.test/apps/anthropic",
                                "note": "live 2026-09-09: enrolled",
                            },
                        ]
                    }
                }
            }
        )
    )
    monkeypatch.setattr(settings, "route_config", config)
    # Committed version: only zai -> the alibaba provider is a config split.
    import subprocess

    def fake_git(*args, **kwargs):
        proc = subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps({"tier_aliases": {"S": {"providers": [{"provider": "zai"}]}}}),
        )
        return proc

    monkeypatch.setattr("doclayer.resolver.subprocess.run", fake_git)
    facts, flags = route_facts()
    assert any("qwen3.8-max" in f["fact"] for f in facts)
    assert any("config split" in flag and "UNCOMMITTED" in flag for flag in flags)


# --------------------------------------------------------------------------- #
# Declared vs running
# --------------------------------------------------------------------------- #


def test_declared_vs_running_port_mismatch(tmp_path, monkeypatch):
    repo_root = tmp_path / "brain"
    repo_root.mkdir()
    (repo_root / "infra").mkdir()
    (repo_root / "infra" / "fleet-hosts.md").write_text("# Fleet\n\nthe vo-rag backend (`:8000`).\n")
    monkeypatch.chdir(tmp_path)

    from doclayer import resolver

    class FakeRepo:
        root = repo_root

        def markdown_paths(self):
            return ["infra/fleet-hosts.md"]

    check = LiveResult(
        name="vo-rag-api",
        checked_at="2026-09-30T12:00:00Z",
        checks=[
            {"what": "port :8001", "value": "answers (HTTP 401)"},
            {"what": "port :8000", "value": "no answer"},
        ],
    )
    flags = resolver.declared_vs_running_flags(FakeRepo(), [check])
    assert any("declared vs running" in flag and ":8001" in flag for flag in flags)


# --------------------------------------------------------------------------- #
# Subject guess + answer rendering
# --------------------------------------------------------------------------- #


def test_guess_subject_prefers_matching_words(tmp_path):
    (tmp_path / "sources" / "alibaba-model-studio").mkdir(parents=True)
    (tmp_path / "sources" / "other-prod").mkdir()

    from doclayer import resolver

    class FakeRepo:
        root = tmp_path

        def markdown_paths(self):
            return []

        def read_page(self, path):
            raise AssertionError("not called when no product pages exist")

    assert resolver.guess_subject(FakeRepo(), "which Model Studio models can I use?", None) == "alibaba-model-studio"
    assert resolver.guess_subject(FakeRepo(), "unrelated question", None) is None


def test_answer_markdown_flags_first_and_tags_sources():
    answer = Answer(question="q", subject="prod")
    answer.flags = ["config split: …"]
    answer.git_facts = [
        {
            "fact": "Models (My Token Plan)",
            "value": "`qwen3.8-max`",
            "source": "wiki/my-token-plan.md@ab12cd34 · declared",
        }
    ]
    answer.doc_facts = [
        {
            "fact": "qwen3.8-flash",
            "value": "fast",
            "source": "https://d@2026-09-30T00:00:00Z",
        }
    ]
    answer.unknowns = ["quota ceiling"]
    text = answer.markdown()
    assert text.index("**Flags**") < text.index("## Answer")
    assert "wiki/my-token-plan.md@ab12cd34" in text
    assert "https://d@2026-09-30T00:00:00Z" in text
    assert "Unknown" in text and "unsupported" in text


# --------------------------------------------------------------------------- #
# Stale doc copies
# --------------------------------------------------------------------------- #


def test_stale_doc_copy_flag(tmp_path, monkeypatch):
    from doclayer import resolver
    from doclayer.brainrepo import BrainRepo

    old = (datetime.now(UTC) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sources = tmp_path / "sources" / "prod"
    sources.mkdir(parents=True)
    (sources / "overview.md").write_text(
        "---\ntitle: overview\nurl: https://d/overview\n"
        f"fetched_at: '{old}'\nproduct: prod\n---\n\n# Overview\n\n| `Model qwen3.8-max` | reasoning |\n"
    )
    monkeypatch.setattr(settings, "stale_copy_days", 60)
    facts, flags = resolver.doc_copy_facts(BrainRepo(tmp_path), "prod", question="which models can I use?")
    assert facts and any("stale doc copy" in flag and "90 days ago" in flag for flag in flags)


def test_unknown_flags_ignore_framing_words_and_word_forms():
    # The source says "serve" and names the host; "served"/"machine"/"setup"/"tool"
    # are how the question is phrased, not facts that are missing.
    answer = Answer(
        question="On what machine and port is the vo-rag API served in my setup? Name the tool you used.",
        subject=None,
        git_facts=[
            {"fact": "vo-rag", "value": "api on App1 (vo-rag-api, port :8001); release symlink still serve 954e445"}
        ],
    )
    assert unknown_flags(answer, []) == []


def test_unknown_flags_still_report_a_real_gap():
    answer = Answer(
        question="What quota does the vo-rag API enforce?",
        subject=None,
        git_facts=[{"fact": "vo-rag", "value": "api on App1 :8001"}],
    )
    flags = unknown_flags(answer, [])
    assert flags and "quota" in flags[0] and "enforce" in flags[0]


def test_clip_marks_cuts_at_word_boundary():
    assert _clip("short line", 400) == "short line"
    text = "the api release symlink still serves 954e445 and brain_sync does not run from it"
    clipped = _clip(text, 40)
    assert clipped.endswith("…")
    assert clipped[:-1] == "the api release symlink still serves"  # whole words only
    assert len(clipped) <= 41


def test_unknown_flags_ignore_instruction_and_qualifier_words():
    # "change anything" is an instruction to the agent and "supported existing"
    # qualifies the route; an SSH fact that names the route covers "access".
    facts = [{"fact": "fleet-hosts", "value": "**Route.** Use the SSH alias `App1`; it is read-only"}]
    for question in (
        "What is the documented read-only SSH access route to App1? Do not run live checks or change anything.",
        "What is the supported existing read-only SSH access route to App1?",
    ):
        assert unknown_flags(Answer(question=question, subject=None, git_facts=facts), []) == []


def test_unknown_flags_report_access_without_a_route_fact():
    answer = Answer(
        question="Which accounts have access to the App1 backups?",
        subject=None,
        git_facts=[{"fact": "fleet-hosts", "value": "App1 backups run nightly"}],
    )
    flags = unknown_flags(answer, [])
    assert flags and "access" in flags[0]


def test_unknown_flags_keep_a_question_that_starts_with_dont():
    answer = Answer(
        question="Don't I have a backup quota on App1?",
        subject=None,
        git_facts=[{"fact": "fleet-hosts", "value": "App1 backups run nightly"}],
    )
    flags = unknown_flags(answer, [])
    assert flags and "quota" in flags[0]
