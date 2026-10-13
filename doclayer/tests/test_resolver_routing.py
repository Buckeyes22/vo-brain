"""Resolver selective-execution tests (source-call counters).

Source-call counters demonstrate zero Context7 calls for local-only cases and
zero brain retrieval for clearly uncovered public subjects. Dispositions,
negative-cache suppression and gap capture are exercised here.
"""

from __future__ import annotations

import json

import pytest

from doclayer import resolver
from doclayer.coverage import Catalog, CatalogPage, SubjectEntry
from doclayer.negative_cache import NegativeCache
from doclayer.settings import settings


class Repo:
    def __init__(self, root, pages):
        self.root = root
        self.pages = {p.path: p for p in pages}

    def markdown_paths(self):
        return list(self.pages)

    def read_page(self, path):
        return self.pages[path]

    def source_paths(self, subject):
        return []

    def has_path(self, path):
        return False

    def head_commit(self):
        return "a" * 40


def page(path, body, title=None, **meta):
    return resolver.BrainPage(path, {"title": title or path, "updated": "2026-10-02", **meta}, body, "a" * 40)


@pytest.fixture
def counters(monkeypatch):
    calls = {"brain_index": 0, "public_docs": 0, "live_checks": 0}

    async def query(question, top_k=8):
        calls["brain_index"] += 1
        return [], None

    async def docs(question, subject=None):
        calls["public_docs"] += 1
        return {"error": "offline"}

    async def probes(names=None):
        calls["live_checks"] += 1
        return []

    monkeypatch.setattr(resolver, "query_vo_rag", query)
    monkeypatch.setattr(resolver, "query_context7", docs)
    monkeypatch.setattr(resolver, "run_checks", probes)
    monkeypatch.setattr(resolver, "_safe_index_lag", lambda repo: [])
    monkeypatch.setattr(resolver, "stale_observation_flags", lambda repo: [])
    monkeypatch.setattr(resolver, "declared_vs_running_flags", lambda repo, live: [])
    return calls


@pytest.fixture
def catalog(monkeypatch):
    cat = Catalog(status="ok", revision="a" * 40, policy_version=1)
    for entry in (
        SubjectEntry(slug="worker1", kind="host", title="worker1", page_status="policy"),
        SubjectEntry(
            slug="alibaba-model-studio",
            kind="product",
            title="Alibaba Model Studio",
            aliases=["model studio"],
            pages=[CatalogPage(path="wiki/alibaba-model-studio.md", concept_id=None)],
        ),
    ):
        cat.subjects[entry.slug] = entry
    monkeypatch.setattr(resolver, "_route_inputs", lambda root: (cat, []))
    return cat


@pytest.mark.asyncio
async def test_local_only_question_makes_zero_context7_calls(tmp_path, counters, catalog):
    answer = await resolver.resolve("What is running on worker1?", repo=Repo(tmp_path, []), live=False)
    assert counters["public_docs"] == 0
    assert counters["brain_index"] == 1
    assert answer.source_calls.get("brain_index") == 1
    assert "public_docs" in answer.skipped_sources or "public_docs" not in answer.source_calls
    assert answer.skipped_sources.get("public_docs", "").startswith(("local", "running-state"))
    assert "not_found_in_checked_sources" in answer.dispositions


@pytest.mark.asyncio
async def test_clearly_uncovered_public_subject_skips_brain_retrieval(tmp_path, counters, catalog, monkeypatch):
    def forbidden(*args):
        raise AssertionError("clearly uncovered public need must not fan out to local/index state")

    repo = Repo(tmp_path, [])
    monkeypatch.setattr(repo, "read_page", forbidden)
    monkeypatch.setattr(repo, "markdown_paths", forbidden)
    monkeypatch.setattr(repo, "head_commit", forbidden)
    monkeypatch.setattr(resolver, "brain_sync_state", forbidden)
    monkeypatch.setattr(resolver, "_safe_index_lag", forbidden)
    answer = await resolver.resolve("docs for obscure-library-xyz", repo=repo, live=False, capture=False)
    assert counters["brain_index"] == 0
    assert counters["public_docs"] == 1
    assert answer.skipped_sources.get("brain_index", "").startswith("clearly uncovered")
    assert answer.intent == "explicit_brain"


@pytest.mark.asyncio
async def test_unknown_subject_still_gets_bounded_brain_search(tmp_path, counters, catalog):
    answer = await resolver.resolve("What is the frobnicator bandwidth?", repo=Repo(tmp_path, []), live=False)
    assert counters["brain_index"] == 1
    assert "not_found_in_checked_sources" in answer.dispositions


@pytest.mark.asyncio
async def test_private_deployment_question_uses_local_sources(tmp_path, counters, catalog):
    await resolver.resolve(
        "How do I use model studio with our deployment?",
        repo=Repo(
            tmp_path,
            [
                page(
                    "wiki/alibaba-model-studio.md",
                    "## Facts\n| Fact | Declared | Observed |\n|---|---|---|\n| Base URL | `https://x` | — |\n",
                    "Model Studio",
                    product="alibaba-model-studio",
                )
            ],
        ),
        live=False,
    )
    assert counters["public_docs"] == 0
    assert counters["brain_index"] == 1


@pytest.mark.asyncio
async def test_answered_question_reports_disposition(tmp_path, counters, catalog):
    answer = await resolver.resolve(
        "what models are on model studio?",
        repo=Repo(
            tmp_path,
            [
                page(
                    "wiki/alibaba-model-studio.md",
                    "## Facts\n| Fact | Declared | Observed |\n|---|---|---|\n| Models | `qwen3.8-max` | — |\n",
                    "Model Studio",
                    product="alibaba-model-studio",
                )
            ],
        ),
        live=False,
    )
    assert answer.git_facts
    assert "answered" in answer.dispositions
    assert "brain_git" in answer.checked_sources


@pytest.mark.asyncio
async def test_source_unavailable_disposition(tmp_path, counters, catalog):
    answer = await resolver.resolve("docs for frobnicator", repo=Repo(tmp_path, []), product="frobnicator", live=False)
    assert counters["public_docs"] == 1  # docs-need question, unknown subject
    assert "source_unavailable" in answer.dispositions


@pytest.mark.asyncio
async def test_negative_cache_suppresses_and_force_retries(tmp_path, counters, catalog, monkeypatch):
    monkeypatch.setattr(settings, "negative_cache_path", tmp_path / "nc.json")
    question = "docs for frobnicator"
    await resolver.resolve(question, repo=Repo(tmp_path, []), product="frobnicator", live=False)
    assert counters["brain_index"] == 1 and counters["public_docs"] == 1
    # both sources observed a miss: recorded
    cache = NegativeCache(tmp_path / "nc.json")
    assert len(list(_entries(cache))) == 1

    second = await resolver.resolve(question, repo=Repo(tmp_path, []), product="frobnicator", live=False)
    assert counters["brain_index"] == 1 and counters["public_docs"] == 1  # suppressed
    assert "recent lookup did not answer this request" in " ".join(second.flags)
    assert second.skipped_sources
    assert "recent lookup did not answer this request" in second.markdown()

    await resolver.resolve(question, repo=Repo(tmp_path, []), product="frobnicator", live=False, force=True)
    assert counters["brain_index"] == 2 and counters["public_docs"] == 2


def _entries(cache):
    return cache._load().get("entries", {}).items()


@pytest.mark.asyncio
async def test_outages_are_never_cached(tmp_path, counters, catalog, monkeypatch):
    monkeypatch.setattr(settings, "negative_cache_path", tmp_path / "nc.json")
    cache = NegativeCache(tmp_path / "nc.json")

    async def failing(question, top_k=8):
        return [], "ConnectionError: down"

    monkeypatch.setattr(resolver, "query_vo_rag", failing)
    await resolver.resolve("What is the frobnicator bandwidth?", repo=Repo(tmp_path, []), live=False)
    assert not list(_entries(cache))


@pytest.mark.asyncio
async def test_scope_mismatch_is_not_a_cacheable_miss(tmp_path, counters, catalog, monkeypatch):
    monkeypatch.setattr(settings, "negative_cache_path", tmp_path / "nc.json")
    cache = NegativeCache(tmp_path / "nc.json")

    async def wrong_scope(question, top_k=8):
        return [
            {
                "path": "wiki/x.md",
                "heading": "h",
                "commit": "abc",
                "score": "1",
                "content": "Request parameters include a_field.",
                "_scope": resolver.source_scope({"platform": "Wrong"}, title="x"),
            }
        ], None

    monkeypatch.setattr(resolver, "query_vo_rag", wrong_scope)
    await resolver.resolve(
        "What Harbor request parameters invoke Model A?",
        repo=Repo(tmp_path, []),
        live=False,
    )
    assert not list(_entries(cache))  # ambiguous applicability is not a miss


@pytest.mark.asyncio
async def test_unsuccessful_outcome_captures_gap(tmp_path, counters, catalog, monkeypatch):
    monkeypatch.setattr(settings, "gap_state_dir", tmp_path / "gaps")
    monkeypatch.setattr(settings, "gap_capture_enabled", True)
    await resolver.resolve("docs for obscure-library-xyz", repo=Repo(tmp_path, []), live=False)
    records = list((tmp_path / "gaps" / "gaps").glob("*/record.json"))
    assert len(records) == 1
    data = json.loads(records[0].read_text())
    assert data["need_class"] == "missing_coverage"
    assert data["dispositions"]


@pytest.mark.asyncio
async def test_capture_disabled_for_verification_requests(tmp_path, counters, catalog, monkeypatch):
    monkeypatch.setattr(settings, "gap_state_dir", tmp_path / "gaps")
    monkeypatch.setattr(settings, "gap_capture_enabled", True)
    await resolver.resolve(
        "docs for obscure-library-xyz",
        repo=Repo(tmp_path, []),
        live=False,
        capture=False,
    )
    assert not list((tmp_path / "gaps" / "gaps").glob("*/record.json"))


@pytest.mark.asyncio
async def test_capture_never_breaks_answering(tmp_path, counters, catalog, monkeypatch):
    monkeypatch.setattr(settings, "gap_state_dir", tmp_path / "unwritable-file")
    (tmp_path / "unwritable-file").write_text("not a dir")
    monkeypatch.setattr(settings, "gap_capture_enabled", True)
    answer = await resolver.resolve("What is the frobnicator bandwidth?", repo=Repo(tmp_path, []), live=False)
    assert "not_found_in_checked_sources" in answer.dispositions


@pytest.mark.asyncio
async def test_withheld_evidence_is_its_own_disposition(tmp_path, counters, catalog, monkeypatch):
    async def withheld(question, top_k=8):
        return [
            {
                "path": "wiki/big.md",
                "heading": "h",
                "commit": "abc",
                "score": "1",
                "content": "",
                "_context_error": "indexed governing context exceeds delivery limit",
            }
        ], None

    monkeypatch.setattr(resolver, "query_vo_rag", withheld)
    answer = await resolver.resolve("What is the frobnicator bandwidth?", repo=Repo(tmp_path, []), live=False)
    assert any("withheld" in u for u in answer.unknowns)
    assert "evidence_withheld" in answer.dispositions
    assert "infrastructure" not in answer.dispositions  # dispositions ≠ need classes


@pytest.mark.asyncio
async def test_index_lag_is_its_own_disposition(tmp_path, counters, catalog, monkeypatch):
    monkeypatch.setattr(
        resolver,
        "_safe_index_lag",
        lambda repo: ["index behind Git: brain_sync indexed aaa, Git HEAD is bbb"],
    )
    answer = await resolver.resolve("What is the frobnicator bandwidth?", repo=Repo(tmp_path, []), live=False)
    assert "index_lag" in answer.dispositions


@pytest.mark.asyncio
async def test_captured_generation_attestation_comes_from_actual_resolver_generation(
    tmp_path, counters, catalog, monkeypatch
):
    from doclayer import gap_client

    captured = []

    class Receiver:
        def submit_event(self, **event):
            captured.append(event)

    monkeypatch.setattr(gap_client, "GapClient", Receiver)
    monkeypatch.setattr(settings, "gap_capture_enabled", True)
    for _ in range(2):
        await resolver.resolve(
            "docs for obscure-library-xyz",
            repo=Repo(tmp_path, []),
            live=False,
            force=True,
            capture=True,
        )
    assert len(captured) == 2 and captured[0]["event_key"] != captured[1]["event_key"]
    assert captured[0]["generation_attested"] is True
    assert captured[0]["coverage_revision"] == catalog.revision
    assert captured[0]["evidence_generation"] == captured[1]["evidence_generation"]
    assert len(captured[0]["evidence_generation"]) == 64
    assert "answered" not in captured[0]["dispositions"]


@pytest.mark.asyncio
async def test_early_project_question_points_at_its_folder_and_records_no_gap(tmp_path, counters, catalog, monkeypatch):
    monkeypatch.setattr(settings, "gap_state_dir", tmp_path / "gaps")
    monkeypatch.setattr(settings, "gap_capture_enabled", True)
    catalog.early_projects["example-connector"] = {
        "aliases": ["example connector"],
        "path": "/home/user/Downloads/pip/example-connector",
    }
    answer = await resolver.resolve(
        "Where is the Example Connector design and acceptance evidence?",
        repo=Repo(tmp_path, []),
        live=False,
    )
    assert any(
        "early-stage project" in flag and "/home/user/Downloads/pip/example-connector" in flag for flag in answer.flags
    )
    assert not list((tmp_path / "gaps" / "gaps").glob("*/record.json"))
