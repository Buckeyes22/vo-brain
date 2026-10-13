from __future__ import annotations

import hashlib
import json
import os
import sys

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from doclayer import resolver
from doclayer.brainrepo import BrainPage
from doclayer.resolver_context import EVIDENCE_CHARS, indexed_evidence, line_contexts


def answer(body, question="What is Nimbus retention?"):
    result = resolver.Answer(question, "Nimbus")
    resolver._append_page_facts(result, BrainPage("wiki/nimbus.md", {"title": "Nimbus"}, body, "a" * 40), question)
    result.git_facts = resolver._rank_facts(result.git_facts, question, 8)
    return result


def test_selected_fact_keeps_late_governing_condition_and_complete_local_unit():
    body = (
        "# Nimbus\n"
        + "Neutral maintenance detail.\n" * 25
        + "Enterprise firmware 3 only.\n## Retention\nRetention is 7 days.\n"
    )
    result = answer(body)
    assert result.git_facts
    rendered = result.markdown()
    assert "Enterprise firmware 3 only." in rendered
    assert "Retention is 7 days." in rendered
    assert "wiki/nimbus.md@aaaaaaaa" in rendered
    assert "…" not in rendered
    assert "source extract with full context" in rendered


def test_sibling_scope_does_not_govern_and_duplicate_heading_uses_occurrence():
    body = "# Nimbus\nGeneral operation.\n## First\nEnterprise only.\n### Limits\nStorage is 20 GB.\n## Second\nCommunity only.\n### Limits\nRetention is 7 days.\n"
    rendered = answer(body).markdown()
    assert "Community only." in rendered
    assert "Enterprise only." not in rendered
    assert "Storage is 20 GB." not in rendered


def test_neutral_heading_and_fences_remain_exact_evidence_not_scope_invention():
    body = "# Nimbus\nRoutine maintenance.\n## Facts\n~~~text\n# Enterprise only\n~~~\nRetention is 7 days.\n"
    contexts = line_contexts(body)
    assert contexts[-1] == body
    rendered = answer(body).markdown()
    assert "Routine maintenance." in rendered
    assert "> ~~~text" in rendered
    assert "Retention is 7 days." in rendered


def test_fact_table_retains_condition_and_observed_declaration():
    body = "# Nimbus\nEnterprise only.\n## Facts\n| Fact | Declared | Observed |\n|---|---|---|\n| Retention | 7 days | 2026-10-02: 8 days |\n"
    result = answer(body, "What is Nimbus retention currently?")
    assert result.git_facts[0]["value"].startswith("2026-10-02: 8 days")
    assert "declared: 7 days" in result.git_facts[0]["value"]
    assert "Enterprise only." in result.markdown()


def test_standard_fact_extract_keeps_whole_table_and_late_condition():
    body = "# Nimbus\nEnterprise only.\n## Facts\n| Fact | Declared | Observed |\n|---|---|---|\n| Retention | 7 days | — |\n| API address | 10.0.0.2 | — |\n| Models | unrelated-model | — |\n\nThese limits require firmware 3.\n"
    result = answer(body)
    rendered = result.markdown()
    assert "Enterprise only." in rendered and "These limits require firmware 3." in rendered
    assert "| Fact | Declared | Observed |" in rendered and "| Retention | 7 days | — |" in rendered
    assert "10.0.0.2" in rendered and "unrelated-model" in rendered
    # Syntax cannot prove sibling rows lack governing significance.
    assert "source extract with full context" in rendered


def test_oversized_context_withholds_whole_fact_without_breaking_other_source():
    result = answer("# Nimbus\n" + "x" * EVIDENCE_CHARS + "\n## Retention\nRetention is 7 days.\n")
    assert not result.git_facts
    assert "withheld" in result.markdown()
    assert "Retention is 7 days." not in result.markdown()
    result.live_results = []
    resolver._append_page_facts(
        result,
        BrainPage("wiki/nimbus-small.md", {"title": "Nimbus"}, "# Nimbus\nRetention is 4 days.\n", "b" * 40),
        result.question,
    )
    assert "Retention is 4 days." in result.markdown()


def payload(**changes):
    value = dict(
        path="wiki/nimbus.md",
        heading="Retention",
        commit="a" * 40,
        source_id="nimbus",
        content="Retention is 7 days.",
        source_section_text="Retention is 7 days.",
        governing_context_version=1,
        governing_context_complete=True,
        governing_context=[
            dict(
                text="Enterprise only.",
                source_id="nimbus",
                collection=resolver.settings.brain_collection,
                point_id="parent",
                section_occurrence=0,
                source_revision="a" * 40,
                content_hash=hashlib.sha256(b"Enterprise only.").hexdigest(),
                heading="Nimbus",
            )
        ],
    )
    value.update(changes)
    return value


def test_hydrated_context_keeps_all_segments_citations_and_bounds():
    content, segments, error = indexed_evidence(payload())
    assert content == "Enterprise only.\nRetention is 7 days."
    assert segments[0]["point_id"] == "parent" and not error
    for changes in (
        {"governing_context_complete": False},
        {"governing_context": [{"point_id": "missing"}]},
        {"content": "x" * (EVIDENCE_CHARS + 1)},
        {"governing_context_version": 2},
    ):
        content, _, error = indexed_evidence(payload(**changes))
        assert not content and error


def test_repeated_fact_rows_keep_their_own_occurrence_scope():
    body = "# Nimbus\n## Enterprise edition\nEnterprise only.\n### Facts\n| Fact | Declared | Observed |\n|---|---|---|\n| Retention | 7 days | — |\n## Community edition\nCommunity only.\n### Facts\n| Fact | Declared | Observed |\n|---|---|---|\n| Retention | 30 days | — |\n"
    first = answer(body, "What is Nimbus retention for Enterprise edition?")
    second = answer(body, "What is Nimbus retention for Community edition?")
    assert [f["value"] for f in first.git_facts if f["fact"] == "Retention"] == ["7 days"]
    assert [f["value"] for f in second.git_facts if f["fact"] == "Retention"] == ["30 days"]
    assert "Community only." not in first.markdown()
    assert "Enterprise only." not in second.markdown()


def test_fenced_facts_tables_are_not_authoritative_rows():
    from doclayer.brainrepo import fact_rows

    body = "# Nimbus\n````markdown\n## Facts\n| Fact | Declared | Observed |\n|---|---|---|\n| Retention | 100 days | — |\n```\n````\n## Actual\nRetention is 7 days.\n"
    page = BrainPage("wiki/nimbus.md", {"title": "Nimbus"}, body, "a" * 40)
    assert fact_rows(page) == []
    selected = answer(body)
    assert all(f["value"] != "100 days" for f in selected.git_facts)
    assert "Retention is 7 days." in selected.markdown()


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_id", "other"),
        ("source_revision", "b" * 40),
        ("content_hash", "0" * 64),
        ("collection", "other"),
        ("section_occurrence", None),
    ],
)
def test_hydrated_context_rejects_foreign_or_unbound_provenance(field, value):
    material = payload()
    material["governing_context"][0][field] = value
    content, _, error = indexed_evidence(material, collection=resolver.settings.brain_collection)
    assert not content and error


def test_doc_copy_delivery_preserves_whole_received_source_context():
    body = "# Nimbus\n" + "Neutral detail.\n" * 40 + "Enterprise only.\n## Limits\n- **Retention quota**: 7 days.\n"
    page = BrainPage(
        "sources/nimbus/limits.md",
        {"title": "Nimbus", "url": "https://example.invalid/limits", "fetched_at": "2026-10-02"},
        body,
        "a" * 40,
    )
    facts = resolver._doc_copy_facts(page)
    result = resolver.Answer("Nimbus retention?", None, doc_facts=facts)
    text = result.markdown()
    assert "Enterprise only." in text and "7 days." in text
    assert "https://example.invalid/limits@2026-10-02" in text
    assert "…" not in text


@pytest.mark.asyncio
async def test_real_query_contract_keeps_condition_beyond_old_400_limit(monkeypatch):
    original = httpx.AsyncClient
    body = "Neutral detail. " * 40 + "Enterprise only.\nRetention is 7 days."
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "results": [
                    {"collection": resolver.settings.brain_collection, "score": 0.9, "payload": payload(content=body)}
                ]
            },
        )
    )
    monkeypatch.setattr(resolver.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    hits, error = await resolver.query_vo_rag("Nimbus retention?")
    assert error is None and body in hits[0]["content"]
    result = resolver.Answer("Nimbus retention?", None, search_hits=hits)
    rendered = result.markdown()
    assert "Enterprise only." in rendered and "point parent" in rendered
    assert "wiki/nimbus.md@aaaaaaaa" in rendered


@pytest.mark.asyncio
async def test_indexed_wire_and_aggregate_bounds_are_explicit(monkeypatch):
    original = httpx.AsyncClient
    oversized = httpx.MockTransport(lambda request: httpx.Response(200, content=b" " * 1_048_577))
    monkeypatch.setattr(resolver.httpx, "AsyncClient", lambda **kwargs: original(transport=oversized, **kwargs))
    hits, error = await resolver.query_vo_rag("Nimbus retention?")
    assert not hits and "1 MiB" in error
    many = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "results": [
                    {
                        "collection": resolver.settings.brain_collection,
                        "score": 0.9,
                        "payload": payload(content="Enterprise only.\n" + "x" * (resolver.EVIDENCE_CHARS - 484)),
                    }
                    for _ in range(8)
                ]
            },
        )
    )
    monkeypatch.setattr(resolver.httpx, "AsyncClient", lambda **kwargs: original(transport=many, **kwargs))
    hits, error = await resolver.query_vo_rag("Nimbus retention?")
    assert 1 <= len(hits) <= 8
    assert error is None or "authority metadata exceeds bounded delivery limit" in error
    assert sum(bool(hit["content"]) for hit in hits) == 4
    assert all("total delivery limit" in hit["_context_error"] for hit in hits[4:])


@pytest.mark.asyncio
async def test_brain_query_cannot_accept_cross_corpus_or_unidentified_results(monkeypatch):
    original = httpx.AsyncClient

    def handler(request):
        assert json.loads(request.content)["collections"] == [resolver.settings.brain_collection]
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "collection": "books_raw",
                        "score": 0.99,
                        "payload": payload(content="Book-only retention claim."),
                    },
                    {"score": 0.98, "payload": payload(content="Unidentified corpus claim.")},
                    {
                        "collection": resolver.settings.brain_collection,
                        "score": 0.97,
                        "payload": payload(collection="books_sterile"),
                    },
                    {"collection": resolver.settings.brain_collection, "score": 0.9, "payload": payload()},
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(resolver.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    hits, error = await resolver.query_vo_rag("Nimbus retention?")
    assert len(hits) == 1 and "Retention is 7 days." in hits[0]["content"]
    assert "configured brain collection" in error
    rendered = resolver.Answer("Nimbus retention?", None, search_hits=hits, search_error=error).markdown()
    assert "Book-only" not in rendered and "Unidentified corpus" not in rendered
    assert "Enterprise only." in rendered and "configured brain collection" in rendered


@pytest.mark.asyncio
async def test_context7_received_fragment_keeps_late_condition_or_reports_bound(monkeypatch):
    fragment = "Neutral detail. " * 150 + "Enterprise only.\nRetention is 7 days."

    async def call(client, tool, args):
        if tool == "resolve-library-id":
            return {"content": [{"text": "Library ID: /nimbus/docs\nTitle: Nimbus\n"}]}
        return {"content": [{"text": fragment}]}

    monkeypatch.setattr(resolver, "_context7_call", call)
    monkeypatch.setattr(resolver, "_context7_libraries", lambda response: [{"id": "/nimbus/docs", "title": "Nimbus"}])
    result = await resolver.query_context7("Nimbus retention?", "Nimbus")
    assert result["summary"] == fragment
    rendered = resolver.Answer("Nimbus retention?", None, context7=result).markdown()
    assert "Enterprise only." in rendered and "Retention is 7 days." in rendered
    fragment = "Neutral detail. " * EVIDENCE_CHARS
    result = await resolver.query_context7("Nimbus retention?", "Nimbus")
    assert not result["summary"] and "withheld" in result["context_error"]


@pytest.mark.asyncio
async def test_fresh_sdk_mcp_context_delivery_and_unknown(tmp_path):
    # A fresh real SDK stdio session imports the owning implementation and
    # performs resolve; only network/live routes are deterministic fixtures.
    runner = tmp_path / "fixture_server.py"
    runner.write_text("""
from doclayer import resolver
from doclayer.brainrepo import BrainPage
from doclayer.mcp_server import build_server
class Repo:
    root = __import__('pathlib').Path('/unused')
    def markdown_paths(self): return ['wiki/nimbus.md']
    def read_page(self, path):
        return BrainPage(path, {'title': 'Nimbus'}, '# Nimbus\\nEnterprise firmware 3 only.\\n## Retention\\nRetention is 7 days.\\n', 'a'*40)
    def source_paths(self, subject): return []
    def has_path(self, path): return False
async def search(*args):
    return [dict(path='wiki/unavailable.md', heading='Limits', commit='bbbbbbbb', score='0.900', content='', _context_error='indexed governing context is unresolved or incomplete')], None
async def docs(*args): return {'error': 'fixture unavailable'}
original = resolver.resolve
async def resolve(question, product=None, live=None, **kwargs):
    return await original(question, product=product, live=False, repo=Repo())
resolver.resolve = resolve
resolver.query_vo_rag = search
resolver.query_context7 = docs
resolver._safe_index_lag = lambda repo: []
resolver.stale_observation_flags = lambda repo: []
build_server().run(transport='stdio')
""")
    env = dict(
        os.environ,
        PYTHONPATH=str(__import__("pathlib").Path(resolver.__file__).parents[1]),
        PYTHONDONTWRITEBYTECODE="1",
    )
    params = StdioServerParameters(command=sys.executable, args=[str(runner)], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            response = await session.call_tool("resolve", {"question": "What is Nimbus retention?", "live": False})
    text = "\n".join(item.text for item in response.content if hasattr(item, "text"))
    assert "Enterprise firmware 3 only." in text
    assert "Retention is 7 days." in text
    assert "wiki/nimbus.md@aaaaaaaa" in text
    assert "unresolved or incomplete" in text and "passage withheld" in text


@pytest.mark.parametrize(
    "field,value",
    [("source_id", None), ("source_id", ""), ("source_id", " "), ("commit", None), ("commit", ""), ("commit", " ")],
)
def test_versioned_empty_context_still_requires_local_identity_and_revision(field, value):
    source = payload(governing_context=[])
    source[field] = value
    content, segments, error = indexed_evidence(source, collection=resolver.settings.brain_collection)
    assert not content and not segments
    assert "local source" in error and "missing" in error


@pytest.mark.parametrize("empty_segments", [True, False])
def test_revision_alias_cannot_override_contradictory_captured_commit(empty_segments):
    source = payload(source_revision="b" * 40)
    source["governing_context"][0]["source_revision"] = "b" * 40
    if empty_segments:
        source["governing_context"] = []
    content, segments, error = indexed_evidence(source, collection=resolver.settings.brain_collection)
    assert not content and not segments and "contradict" in error


def test_explicit_consistent_snapshot_without_ancestors_and_concept_identity_is_valid():
    source = payload(source_id="concept:nimbus", source_revision="a" * 40, governing_context=[])
    content, segments, error = indexed_evidence(source, collection=resolver.settings.brain_collection)
    assert content == source["content"] and not segments and error is None
    del source["commit"]
    assert indexed_evidence(source)[2] is None  # source_revision alone is explicit identity


def test_unversioned_legacy_is_not_given_guessed_provenance():
    source = {"content": "Nimbus retention is 7 days."}
    assert indexed_evidence(source) == (source["content"], [], None)
    assert "source_id" not in source and "commit" not in source


@pytest.mark.parametrize("field", ["source_id", "commit"])
def test_versioned_empty_context_missing_local_field_is_withheld(field):
    source = payload(governing_context=[])
    del source[field]
    content, segments, error = indexed_evidence(source)
    assert not content and not segments and "missing" in error


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_context_version_requires_integer_contract(version):
    content, segments, error = indexed_evidence(payload(governing_context_version=version))
    assert not content and not segments and "unsupported" in error


def test_versioned_missing_manifest_is_not_guessed_as_empty():
    source = payload()
    del source["governing_context"]
    content, segments, error = indexed_evidence(source)
    assert not content and not segments and "manifest is missing" in error


def test_whitespace_context_citation_is_withheld():
    source = payload()
    source["governing_context"][0]["point_id"] = "   "
    content, segments, error = indexed_evidence(source)
    assert not content and not segments and "provenance is incomplete" in error
