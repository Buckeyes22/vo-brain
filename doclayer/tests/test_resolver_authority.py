"""Authority is explicit provenance, never inferred from relevance or headings."""

from __future__ import annotations

import httpx
import pytest

from doclayer import resolver
from doclayer.brainrepo import BrainPage
from doclayer.resolver_context import EVIDENCE_CHARS, authority_record


@pytest.mark.parametrize(
    "path,label",
    [
        ("inbox/nimbus.md", "acceptance not established"),
        ("sources/nimbus.md", "source reference"),
        ("wiki/nimbus.md", "curated wiki record"),
        ("infra/nimbus.md", "running state requires observation"),
    ],
)
@pytest.mark.asyncio
async def test_indexed_fallback_preserves_full_identity_explicit_lifecycle_and_area(monkeypatch, path, label):
    revision = "a" * 40
    metadata = dict(
        path=path,
        heading="Approved current facts",
        commit=revision,
        kind="design",
        source_origin="primary-documentation",
        source_lineage=[{"url": "https://example.invalid/v3", "claim_ids": ["one", "two"]}],
        status="proposed",
        updated="2026-10-02",
        content="Nimbus retention is 7 days for Enterprise only.",
    )
    original = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"results": [dict(collection=resolver.settings.brain_collection, payload=metadata, score=0.9)]}
        )
    )
    monkeypatch.setattr(resolver.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    hits, error = await resolver.query_vo_rag("What is Nimbus retention?")
    assert error is None
    hit = hits[0]
    assert hit["collection"] == resolver.settings.brain_collection
    assert hit["source_revision"] == revision
    assert hit["source_lineage"] == metadata["source_lineage"]
    assert hit["source_origin"] == metadata["source_origin"]
    answer = resolver.Answer("What is Nimbus retention?", "Nimbus", search_hits=hits, git_source_available=False)
    rendered = answer.markdown()
    assert f"{path}@{revision}" in rendered
    for value in (
        label,
        "proposed",
        "design",
        "2026-10-02",
        "primary-documentation",
        "https://example.invalid/v3",
        resolver.settings.brain_collection,
    ):
        assert value in rendered
    assert "Enterprise only." in rendered
    assert "current Git source unavailable" in rendered
    # A suggestive heading does not certify an inbox proposal as accepted.
    if path.startswith("inbox/"):
        assert hit["authority"].startswith("inbox proposal/work item")


def test_git_lifecycle_and_origin_survive_ranking_without_promoting_proposal():
    answer = resolver.Answer("What is Nimbus retention currently?", "Nimbus")
    proposal = BrainPage(
        "inbox/nimbus.md",
        dict(
            title="Nimbus",
            status="proposed",
            updated="2026-09-01",
            source_origin="operator proposal",
            source_lineage=["legacy-note"],
        ),
        "# Nimbus\nRetention is 99 days.\n",
        "b" * 40,
    )
    current = BrainPage(
        "infra/nimbus.md", dict(title="Nimbus", updated="2026-10-02"), "# Nimbus\nRetention is 7 days.\n", "c" * 40
    )
    for page in (proposal, current):
        resolver._append_page_facts(answer, page, answer.question)
    answer.git_facts = resolver._rank_facts(answer.git_facts, answer.question, 8)
    assert answer.git_facts[0]["value"] == "Retention is 7 days."
    assert len(answer.git_facts) == 1  # current question keeps the existing historical filter
    historical = resolver.Answer("What was the historical Nimbus retention proposal?", "Nimbus")
    resolver._append_page_facts(historical, proposal, historical.question)
    historical.git_facts = resolver._rank_facts(historical.git_facts, historical.question, 8)
    assert historical.git_facts[0]["source_lineage"] == ["legacy-note"]
    assert historical.git_facts[0]["source_revision"] == "b" * 40
    text = answer.markdown() + historical.markdown()
    assert "Retention is 99 days." not in answer.markdown()
    assert "Retention is 7 days." in answer.markdown()
    assert "historical/proposed" in text and "acceptance not established" in text
    assert "operator proposal" in text and "legacy-note" in text
    assert "running state requires observation" in text


def test_authority_is_path_based_and_explicit_metadata_not_heading_claims():
    metadata = dict(title="Approved curated current production facts", status="draft")
    record = authority_record("inbox/design.md", metadata)
    assert record["status"] == "draft"
    assert "inbox proposal" in record["authority"]
    assert "Approved" not in record["authority"]
    assert "source_origin" not in record


@pytest.mark.asyncio
async def test_oversized_authority_metadata_is_withheld_not_silently_clipped(monkeypatch):
    original = httpx.AsyncClient
    result = dict(
        collection=resolver.settings.brain_collection,
        score=0.9,
        payload=dict(
            path="inbox/nimbus.md",
            commit="a" * 40,
            content="Nimbus retention is 99 days.",
            source_lineage=["x" * EVIDENCE_CHARS],
        ),
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"results": [result]}))
    monkeypatch.setattr(resolver.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    hits, error = await resolver.query_vo_rag("What is Nimbus retention?")
    assert not hits and "withheld" in error


@pytest.mark.parametrize(
    "qualifier",
    ["| Edition | Enterprise firmware 3 only | — |", "| Entitlement | Authorized laboratory trials only | — |"],
)
@pytest.mark.parametrize("before", [True, False])
def test_typed_table_sibling_qualification_cannot_disappear(qualifier, before):
    rows = [qualifier, "| Retention | 7 days | — |"] if before else ["| Retention | 7 days | — |", qualifier]
    body = "# Nimbus\n## Facts\n| Fact | Declared | Observed |\n|---|---|---|\n" + "\n".join(rows) + "\n"
    page = BrainPage("wiki/nimbus.md", {"title": "Nimbus"}, body, "a" * 40)
    answer = resolver.Answer("What is Nimbus retention?", "Nimbus")
    resolver._append_page_facts(answer, page, answer.question)
    answer.git_facts = resolver._rank_facts(answer.git_facts, answer.question, 8)
    assert [fact["fact"] for fact in answer.git_facts] == ["Retention"]
    assert answer.git_facts[0]["evidence_context"] == body
    rendered = answer.markdown()
    assert qualifier in rendered and "| Retention | 7 days | — |" in rendered
    assert "- **Retention**: 7 days" not in rendered
