"""Tests for the selective UserPromptSubmit nudge hook."""

from __future__ import annotations

import io
import json

import pytest

from doclayer import nudge, routing
from doclayer.coverage import Catalog, CatalogPage, SubjectEntry


def _entry(slug, kind="product", aliases=()):
    return SubjectEntry(
        slug=slug,
        kind=kind,
        title=slug,
        aliases=list(aliases),
        pages=[CatalogPage(path=f"wiki/{slug}.md", concept_id=None)],
        page_status="committed",
    )


@pytest.fixture
def catalog():
    cat = Catalog(status="ok", revision="a" * 40, policy_version=1)
    for entry in (
        _entry("alibaba-model-studio", aliases=["model studio"]),
        _entry("litellm-hub", kind="service", aliases=["litellm"]),
    ):
        cat.subjects[entry.slug] = entry
    return cat


@pytest.fixture
def env(tmp_path, monkeypatch, catalog):
    monkeypatch.setattr(nudge.routing, "load_catalog", lambda *a, **k: catalog)
    monkeypatch.setattr("doclayer.coverage.ssh_host_aliases", lambda *a, **k: ["worker 1"])
    monkeypatch.setenv("DOC_LAYER_NEGATIVE_CACHE", str(tmp_path / "nc.json"))
    monkeypatch.setenv("DOC_LAYER_GAPS_DIR", str(tmp_path / "gaps"))
    monkeypatch.setenv("DOC_LAYER_GAP_CAPTURE", "1")
    monkeypatch.setenv("DOC_LAYER_CATALOG_CACHE_DIR", str(tmp_path / "catalog"))
    return tmp_path


def _run(monkeypatch, capsys, stdin: str):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    with pytest.raises(SystemExit) as exc:
        nudge.main()
    assert exc.value.code == 0
    return capsys.readouterr().out


def test_covered_subject_gets_reasoned_suggestion(monkeypatch, capsys, env):
    out = _run(monkeypatch, capsys, json.dumps({"prompt": "which models on model studio?"}))
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "alibaba-model-studio" in ctx
    assert "resolve" in ctx
    assert "brain_git" in ctx  # the route is named, not just a command


def test_code_question_gets_no_nudge(monkeypatch, capsys, env):
    assert _run(monkeypatch, capsys, json.dumps({"prompt": "refactor this function"})) == ""


def test_generic_question_gets_no_nudge(monkeypatch, capsys, env):
    assert _run(monkeypatch, capsys, json.dumps({"prompt": "What version should I pin?"})) == ""


def test_host_question_names_the_host(monkeypatch, capsys, env):
    out = _run(monkeypatch, capsys, json.dumps({"prompt": "what is running on worker-1?"}))
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "worker 1" in ctx
    assert "brain_git" in ctx


def test_uncovered_docs_request_captures_gap_and_suggests(monkeypatch, capsys, env):
    out = _run(monkeypatch, capsys, json.dumps({"prompt": "docs for obscure-lib-xyz"}))
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "obscure lib xyz" in ctx
    assert "record_gap" in ctx
    records = list((env / "gaps" / "gaps").glob("*/record.json"))
    assert len(records) == 1
    data = json.loads(records[0].read_text())
    assert data["need_class"] == "missing_coverage"
    assert data["identity"] == "unknown:obscure lib xyz"


def test_replay_does_not_duplicate_capture(monkeypatch, capsys, env):
    for _ in range(2):
        _run(monkeypatch, capsys, json.dumps({"prompt": "docs for obscure-lib-xyz", "event_id": "repeat-delivery"}))
    records = list((env / "gaps" / "gaps").glob("*/record.json"))
    assert len(records) == 1
    data = json.loads(records[0].read_text())
    assert data["recurrence"] == 1  # identical event key: replay-safe


def test_suppression_visible_in_suggestion(monkeypatch, capsys, env):
    from doclayer.negative_cache import NegativeCache

    NegativeCache(env / "nc.json").record(
        "which models on model studio?",
        "alibaba-model-studio",
        sources=["brain_index"],
    )
    out = _run(monkeypatch, capsys, json.dumps({"prompt": "which models on model studio?"}))
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    # The hook has no freshly attested index generation. A stale cache hint
    # must not override newly committed coverage or require a network probe.
    assert "recent lookup did not answer this request" not in ctx


def test_slash_commands_skipped(monkeypatch, capsys, env):
    assert _run(monkeypatch, capsys, json.dumps({"prompt": "  /brain model studio"})) == ""


@pytest.mark.parametrize("stdin", ["not json", "{}", json.dumps({"prompt": 42}), json.dumps([1, 2])])
def test_bad_stdin_silent(monkeypatch, capsys, stdin, env):
    assert _run(monkeypatch, capsys, stdin) == ""


def test_capture_failure_silent(monkeypatch, capsys, env):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(routing, "load_catalog", boom)
    assert _run(monkeypatch, capsys, json.dumps({"prompt": "docs for a thing"})) == ""


def test_private_product_nudge_never_names_external_route(monkeypatch, capsys, env):
    out = _run(
        monkeypatch,
        capsys,
        json.dumps({"prompt": "Show model studio docs for /home/user/private-project configuration"}),
    )
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "brain_git" in ctx and "brain_index" in ctx
    assert "public_docs" not in ctx and "Context7" not in ctx
    assert not list((env / "gaps").rglob("record.json"))
