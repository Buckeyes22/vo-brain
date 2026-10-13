"""Ordinary performance questions deliver qualified mechanisms, not just intros."""

import pytest
from test_resolver_selection import Repo, page, sources  # noqa: F401

from doclayer import resolver


@pytest.mark.asyncio
@pytest.mark.usefixtures("sources")
async def test_performance_question_returns_distinct_mechanisms_with_scope(tmp_path):
    question = "How can repeated prompt prefixes and response scheduling affect Harbor API latency or throughput?"
    body = """# Harbor API
This page describes API latency, throughput and repeated prompt prefixes.
## Prompt Caching
Enterprise deployments only.
Cache reuse requires the entire rendered prefix to match; changed content before the breakpoint prevents reuse. [source: https://harbor.test/cache]
Cached tokens do not count against the input-token throughput quota. [source: https://harbor.test/cache]
## Processing
Priority processing delivers up to twice faster responses only for steady production traffic. [source: https://harbor.test/priority]
Configure a service tier per request; the default processes requests at standard speed. [source: https://harbor.test/priority]
"""
    answer = await resolver.resolve(
        question, repo=Repo(tmp_path, [page("wiki/harbor.md", body, "Harbor API")]), live=False, capture=False
    )
    rendered = answer._prose()
    assert "entire rendered prefix to match" in rendered
    assert "steady production traffic" in rendered
    assert "Enterprise deployments only." in rendered
    assert "https://harbor.test/cache" in rendered
    assert "https://harbor.test/priority" in rendered
    assert len(answer.git_facts) <= 8
    assert {"caching", "scheduling"} <= {facet for row in resolver.answer_evidence(answer) for facet in row["facets"]}


@pytest.mark.asyncio
@pytest.mark.usefixtures("sources")
async def test_performance_mechanisms_do_not_admit_foreign_platform(tmp_path):
    question = "How can caching and priority processing affect Nimbus latency on harbor?"
    wrong = page(
        "wiki/other.md",
        "# Nimbus\nCache reuse improves latency. Priority processing is faster.\n",
        "Nimbus",
        platform="OtherCloud",
    )
    right = page(
        "wiki/harbor.md",
        "# Nimbus\n## Caching\nEnterprise only.\nCache reuse requires an identical prefix.\n## Processing\nPriority processing is faster for steady traffic only.\n",
        "Nimbus",
        platform="harbor",
    )
    answer = await resolver.resolve(question, repo=Repo(tmp_path, [wrong, right]), live=False, capture=False)
    assert answer.git_facts
    assert all(row["source"].startswith("wiki/harbor.md@") for row in answer.git_facts)
    assert "Enterprise only." in answer._prose()
    assert "steady traffic only" in answer._prose()


def test_topic_labels_and_citation_urls_do_not_establish_mechanisms():
    question = "How can caching and scheduling improve latency or throughput?"
    assert not resolver._matching_aspects(
        "Documentation [source: https://example.test/caching/priority/latency]", question
    )
    assert not resolver._matching_aspects("An unrelated page describes quota and addresses.", question)


def test_mechanism_wording_is_not_a_closed_verb_list():
    question = "How can caching affect latency?"
    assert resolver._matching_aspects(
        "Cache hits bypass prefill computation, lowering first-token latency.", question
    ) == {"caching", "latency"}
    assert not resolver._matching_aspects("This page describes caching by topic and latency.", question)
