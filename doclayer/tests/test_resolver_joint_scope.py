"""Joint selected scopes retain qualifications without borrowing sibling support."""

import hashlib
import json

import httpx
import pytest
from test_resolver_selection import Repo, sources  # noqa: F401

from doclayer import resolver
from doclayer.applicability import matches_scope, requested_scopes, source_scope

_REAL_QUERY = resolver.query_vo_rag


@pytest.mark.parametrize(
    "heading,question,dimension,values",
    [
        (
            "Can both Personal Edition and Team Edition coexist?",
            "Can Personal Edition and Team Edition be purchased together?",
            "edition",
            ["Personal", "Team"],
        ),
        (
            "Compatibility for version v2 and version v3",
            "What compatibility applies to version v2 and version v3?",
            "version",
            ["v2", "v3"],
        ),
    ],
)
def test_joint_constraints_from_one_heading(heading, question, dimension, values):
    scope = source_scope({dimension: ["legacy", *values]}, heading=heading)
    assert json.loads(scope)["explicit"][dimension] == values
    assert matches_scope(scope, requested_scopes(question))
    assert not matches_scope(scope, [{"dimension": dimension, "value": "unrelated"}])


@pytest.mark.parametrize(
    "heading,dimension,accepted,rejected",
    [
        ("Enterprise Edition / Community Edition / Facts", "edition", "Community", "Enterprise"),
        ("version v2 / version v3 / Facts", "version", "v3", "v2"),
        ("Personal Edition / Both Team Edition and Business Edition", "edition", "Team", "Personal"),
    ],
)
def test_nearest_heading_overrides_parent_and_metadata(heading, dimension, accepted, rejected):
    scope = source_scope({dimension: [accepted, rejected, "Other"]}, heading=heading)
    assert matches_scope(scope, [{"dimension": dimension, "value": accepted}])
    assert not matches_scope(scope, [{"dimension": dimension, "value": rejected}])
    assert not matches_scope(scope, [{"dimension": dimension, "value": "Other"}])


def test_siblings_cannot_supply_joint_scope():
    requested = requested_scopes("Can Personal Edition and Team Edition coexist?")
    for heading in ["Personal Edition", "Team Edition"]:
        assert not matches_scope(source_scope({"edition": ["Personal", "Team"]}, heading=heading), requested)


@pytest.mark.asyncio
async def test_actual_source_copy_shape_joint_answer_and_whole_provenance(tmp_path, sources, monkeypatch):
    # The failed live case had both scopes in ONE selected FAQ heading.
    root = "# Token Plan overview\n\nToken Plan offers Personal Edition and Team Edition.\n\nToken Plan is currently available only in the **Singapore** region.\n"
    local = "## FAQ\n\n### Can I purchase both Personal Edition and Team Edition?\n\nYes. The same Alibaba Cloud account can hold both Personal Edition and Team Edition subscriptions simultaneously. They are billed independently.\n"
    body = root + "\n" + local
    revision = "a" * 40
    path = "sources/alibaba-model-studio/token-plan-overview.md"
    url = "https://www.alibabacloud.com/help/en/model-studio/token-plan-overview"
    payload = dict(
        path=path,
        source_id=path,
        commit=revision,
        kind="source",
        source_origin="external",
        source_lineage=["url:" + url],
        url=url,
        fetched_at="2026-10-02T04:09:49Z",
        title="Token Plan overview",
        heading="FAQ / Can I purchase both Personal Edition and Team Edition?",
        content=body,
        source_section_text=local,
        governing_context_version=1,
        governing_context_complete=True,
        governing_context=[
            dict(
                text=root,
                source_id=path,
                collection="vo_rag_brain_v1",
                point_id="faq",
                source_revision=revision,
                section_occurrence=1,
                heading="",
                content_hash=hashlib.sha256(root.encode()).hexdigest(),
            )
        ],
    )
    original = httpx.AsyncClient
    seen = []

    def query(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"results": [dict(id="faq", collection="vo_rag_brain_v1", score=0.9, payload=payload)]}
        )

    transport = httpx.MockTransport(query)
    monkeypatch.setattr(resolver.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    monkeypatch.setattr(resolver, "query_vo_rag", _REAL_QUERY)
    answer = await resolver.resolve(
        "Can Alibaba Model Studio Token Plan Personal Edition and Team Edition be purchased together?",
        product="alibaba-model-studio",
        live=False,
        repo=Repo(tmp_path, []),
    )
    text = answer.markdown()
    assert seen[0]["collections"] == ["vo_rag_brain_v1"]
    assert "subscriptions simultaneously" in text and "billed independently" in text
    assert "**Singapore**" in text and body == answer.search_hits[0]["content"]
    assert url in text and "2026-10-02T04:09:49Z" in text and revision in text
    assert "source reference; source statements require applicability" in text
    assert "requested applicability is unknown" not in text and "Not answerable" not in text
    assert "automatic coverage unresolved" in text and "may miss paraphrases" in text
    assert "no source covers" not in text and "checked, not found" not in text


def test_lexical_miss_is_not_source_absence():
    answer = resolver.Answer(
        "Can the editions be purchased together?",
        None,
        git_facts=[
            dict(
                fact="Edition subscriptions",
                value="They coexist and are billed independently.",
                source="wiki/plan.md@abc",
            )
        ],
    )
    answer.unknowns = resolver.unknown_flags(answer, [])
    text = answer.markdown()
    assert "They coexist" in text and "together" in text
    assert "not proof of source absence" in text and "may miss paraphrases" in text
    assert "no checked source states anything" not in text and "checked, not found" not in text
