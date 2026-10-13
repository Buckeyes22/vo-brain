"""Platform, edition and version are applicability constraints, not rank boosts."""

import pytest
from test_resolver_selection import Repo, page, sources  # noqa: F401

from doclayer import resolver
from doclayer.applicability import matches_scope, requested_scopes, source_scope


def test_scope_extraction_uses_generic_question_grammar():
    assert requested_scopes("What Bedrock request parameters should I use?") == [
        {"dimension": "platform", "value": "Bedrock"}
    ]
    assert requested_scopes("what azure openai API parameters should I use?") == [
        {"dimension": "platform", "value": "azure openai"}
    ]
    assert requested_scopes("What Community edition parameters are supported?") == [
        {"dimension": "edition", "value": "Community"}
    ]
    assert requested_scopes("What parameters apply to version v2?") == [{"dimension": "version", "value": "v2"}]
    assert not requested_scopes("Which model handles curation?")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question,meta,heading",
    [
        ("What Harbor request parameters invoke Model A?", {"platform": "Harbor"}, "Invocation"),
        ("What Community edition parameters invoke Model A?", {"edition": "Community"}, "Invocation"),
        ("What parameters invoke Model A on version v2?", {"version": "v2"}, "Invocation"),
    ],
)
async def test_requested_scope_requires_matching_metadata(tmp_path, sources, question, meta, heading):
    wrong = {key: "different" for key in meta}
    pages = [
        page("wiki/wrong.md", "## Invocation\nModel A request parameters include wrong_field.", "Model A", **wrong),
        page("wiki/right.md", f"## {heading}\nModel A request parameters include right_field.", "Model A", **meta),
    ]
    answer = await resolver.resolve(question, repo=Repo(tmp_path, pages), live=False)
    assert any("right_field" in fact["value"] for fact in answer.git_facts)
    assert not any("wrong_field" in fact["value"] for fact in answer.git_facts)


@pytest.mark.asyncio
async def test_mixed_source_lineage_does_not_make_every_statement_apply_to_every_platform(tmp_path, sources):
    p = page(
        "wiki/model-a.md",
        "## API Configuration\nModel A request parameters include primary_field. [source: https://docs.primary.test/api/parameters]\nModel A is a capable model. [source: https://docs.secondary.test/harbor/model-card]",
        "Model A",
        source_lineage=["source:https://docs.secondary.test/harbor/model-card"],
    )
    answer = await resolver.resolve(
        "What Harbor request parameters invoke Model A?", repo=Repo(tmp_path, [p]), live=False
    )
    assert not answer.git_facts
    assert any("platform=Harbor" in value for value in answer.unknowns)
    assert "primary_field" not in answer._prose()


@pytest.mark.asyncio
async def test_provider_matched_per_statement_provenance_still_answers(tmp_path, sources):
    p = page(
        "wiki/model-a.md",
        "## API Configuration\nModel A request parameters include correct_field. [source: https://docs.hosted.test/harbor/api/parameters]",
        "Model A",
    )
    answer = await resolver.resolve(
        "What Harbor request parameters invoke Model A?", repo=Repo(tmp_path, [p]), live=False
    )
    assert "correct_field" in answer._prose()
    assert not any("applicability is unknown" in value for value in answer.unknowns)


@pytest.mark.asyncio
async def test_wrong_index_and_context7_cannot_become_answer_fallback(tmp_path, sources, monkeypatch):
    async def query(*args):
        return [
            {
                "path": "wiki/model-a.md",
                "heading": "API Configuration",
                "commit": "abc",
                "score": "4",
                "content": "Request parameters include wrong_field.",
                "_scope": source_scope({"platform": "Primary"}, title="Model A"),
            }
        ], None

    async def docs(*args):
        return {"library": "/primary/sdk", "summary": "Request parameters include wrong_field."}

    monkeypatch.setattr(resolver, "query_vo_rag", query)
    monkeypatch.setattr(resolver, "query_context7", docs)
    answer = await resolver.resolve(
        "What Harbor request parameters invoke Model A?", repo=Repo(tmp_path, []), live=False
    )
    assert not answer._prose() and "wrong_field" not in answer.markdown()
    assert answer.search_hits  # retained as discovery, never asserted applicable
    assert any("platform=Harbor" in value for value in answer.unknowns)


def test_specific_subsection_overrides_broad_page_edition_and_version():
    scope = source_scope(
        {"edition": ["Community", "Enterprise"], "version": ["v1", "v2"]},
        title="Model A",
        heading="Enterprise edition / version v1",
    )
    assert not matches_scope(scope, requested_scopes("What Community edition parameters apply to version v2?"))
    assert matches_scope(scope, requested_scopes("What Enterprise edition parameters apply to version v1?"))


@pytest.mark.parametrize("field", ["service", "offering"])
def test_explicit_hosting_scope_cannot_be_bypassed_by_model_provider(field):
    scope = source_scope(
        {"provider": "Primary", field: "Harbor Hosted"}, title="Model A", urls=["https://primary.test/api"]
    )
    assert not matches_scope(scope, [{"dimension": "platform", "value": "Primary"}])
    assert matches_scope(scope, [{"dimension": "platform", "value": "Harbor"}])
    assert matches_scope(source_scope({"provider": "Primary"}), [{"dimension": "platform", "value": "Primary"}])
    assert not matches_scope(
        source_scope({"provider": "Primary", "service": "Harbor Hosted", "offering": "Primary Model A"}),
        [{"dimension": "platform", "value": "Primary"}],
    )


@pytest.mark.asyncio
async def test_hosted_service_provider_alias_does_not_answer_native_invocation(tmp_path, sources):
    hosted = page(
        "wiki/model-a.md",
        "## Invocation\nModel A request parameters include hosted_field.",
        "Model A",
        provider="Primary",
        service="Harbor Hosted",
    )
    wrong = await resolver.resolve(
        "What Primary request parameters invoke Model A?", repo=Repo(tmp_path, [hosted]), live=False
    )
    right = await resolver.resolve(
        "What Harbor request parameters invoke Model A?", repo=Repo(tmp_path, [hosted]), live=False
    )
    assert not wrong._prose()
    assert any("platform=Primary" in item for item in wrong.unknowns)
    assert "hosted_field" in right._prose()


@pytest.mark.asyncio
async def test_correct_platform_profile_is_not_request_parameter_fallback(tmp_path, sources, monkeypatch):
    async def query(*args):
        return [
            {
                "path": "wiki/model-a.md",
                "heading": "Model profile",
                "commit": "abc",
                "content": "Model A has a million-token context window.",
                "_scope": source_scope({}, urls=["https://docs.test/harbor/model-card"]),
            }
        ], None

    monkeypatch.setattr(resolver, "query_vo_rag", query)
    answer = await resolver.resolve(
        "What Harbor request parameters invoke Model A?", repo=Repo(tmp_path, []), live=False
    )
    assert not answer._prose()
    assert any("platform=Harbor" in value for value in answer.unknowns)


@pytest.mark.asyncio
async def test_applicable_indexed_parameters_remain_available_without_git(tmp_path, sources, monkeypatch):
    async def query(*args):
        return [
            {
                "path": "wiki/model-a.md",
                "heading": "Invocation",
                "commit": "abc",
                "content": "Model A request parameters include correct_field.",
                "_scope": source_scope({"platform": "Harbor"}),
            }
        ], None

    monkeypatch.setattr(resolver, "query_vo_rag", query)
    answer = await resolver.resolve(
        "What Harbor request parameters invoke Model A?", repo=Repo(tmp_path, []), live=False
    )
    assert "correct_field" in answer._prose()
    assert not any("applicability is unknown" in value for value in answer.unknowns)


def test_lowercase_multiword_hosting_names_and_version_are_separate():
    assert requested_scopes("Which parameters work on azure openai for Model A?") == [
        {"dimension": "platform", "value": "azure openai"}
    ]
    assert requested_scopes("Which parameters work via harbor?") == [{"dimension": "platform", "value": "harbor"}]
    assert requested_scopes("Which parameters work on version v2?") == [{"dimension": "version", "value": "v2"}]


def test_scope_filter_precedes_fallback_cap():
    answer = resolver.Answer(question="What Harbor request parameters invoke Model A?", subject="Model A")
    answer.search_hits = [
        {
            "content": "Request parameters include wrong_field.",
            "heading": "Invocation",
            "_scope": source_scope({"platform": "Wrong"}),
        }
        for _ in range(3)
    ]
    answer.search_hits.append(
        {
            "content": "Request parameters include right_field.",
            "heading": "Invocation",
            "_scope": source_scope({"platform": "Harbor"}),
        }
    )
    assert "right_field" in answer._prose()
    assert "wrong_field" not in answer._prose()


def test_versioned_parameter_question_still_requires_parameters_not_just_version():
    assert not resolver._matches_requested_aspects(
        "Version v2 supports a large context window.", "What request parameters apply to version v2?"
    )


@pytest.mark.parametrize(
    "question",
    [
        "What request parameters invoke Model A in Harbor?",
        "What request parameters invoke Model A in harbor?",
        "What request parameters invoke Model A in Harbor environments?",
    ],
)
@pytest.mark.asyncio
async def test_in_usage_platform_cannot_leak_other_provider_parameters(tmp_path, sources, question):
    p = page(
        "wiki/model-a.md",
        "Model A request parameters include wrong_field. [source: https://docs.primary.test/api/parameters]",
        "Model A",
    )
    answer = await resolver.resolve(question, repo=Repo(tmp_path, [p]), live=False)
    assert not answer._prose()
    assert any("platform=" in value for value in answer.unknowns)


def test_usage_edition_and_version_are_not_duplicated_as_platform_constraints():
    assert requested_scopes("Which parameters work in Enterprise edition?") == [
        {"dimension": "edition", "value": "Enterprise"}
    ]
    assert requested_scopes("Which parameters work in v2?") == [{"dimension": "version", "value": "v2"}]


@pytest.mark.parametrize(
    "question",
    [
        "What request parameters invoke Model A for Harbor?",
        "What request parameters invoke Model A with Harbor?",
        "What request parameters invoke Model A for service harbor?",
        "What request parameters invoke Model A with provider harbor?",
    ],
)
@pytest.mark.asyncio
async def test_explicit_for_with_service_usage_preserves_provider_boundaries(tmp_path, sources, question):
    pages = [
        page("wiki/wrong.md", "Model A request parameters include wrong_field.", "Model A", platform="Primary"),
        page("wiki/right.md", "Model A request parameters include right_field.", "Model A", platform="Harbor"),
    ]
    answer = await resolver.resolve(question, repo=Repo(tmp_path, pages), live=False)
    assert "right_field" in answer._prose() and "wrong_field" not in answer._prose()


def test_regions_and_incidental_purpose_clauses_are_not_platforms():
    assert requested_scopes("Which parameters work in us-east-1 region?") == [
        {"dimension": "region", "value": "us-east-1"}
    ]
    assert requested_scopes("Which parameters work in region us-east-1?") == [
        {"dimension": "region", "value": "us-east-1"}
    ]
    assert not requested_scopes("Which model should I use for curation?")
    assert not requested_scopes("What parameters should I use for reliability with streaming?")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question,dimension,requested",
    [
        ("What is Nimbus Community edition storage capacity and maximum connections?", "edition", "Community"),
        ("What is Nimbus storage capacity and maximum connections in Harbor?", "platform", "Harbor"),
    ],
)
async def test_numeric_multifacet_does_not_cross_requested_scope(tmp_path, sources, question, dimension, requested):
    body = "## Facts\n| Fact | Declared | Observed |\n|---|---|---|\n| Storage capacity | 8 TB | — |\n| Maximum connections | 256 | — |"
    wrong = page("wiki/nimbus-wrong.md", body, "Nimbus", **{dimension: "Other"})
    right = page("wiki/nimbus-right.md", body, "Nimbus", **{dimension: requested})
    missing = await resolver.resolve(question, repo=Repo(tmp_path, [wrong]), live=False)
    assert not missing.git_facts
    answered = await resolver.resolve(question, repo=Repo(tmp_path, [wrong, right]), live=False)
    assert {f["fact"] for f in answered.git_facts} == {"Storage capacity", "Maximum connections"}
    assert all("nimbus-right" in f["source"] for f in answered.git_facts)


@pytest.mark.parametrize("unit", ["GB", "GiB", "tokens", "seconds", "ms", "8 GB"])
def test_requested_measurement_units_are_not_hosting_platforms(unit):
    assert not requested_scopes("What is Nimbus capacity in " + unit + "?", "Nimbus")
