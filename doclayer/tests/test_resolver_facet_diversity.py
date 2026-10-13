"""Multipage answers keep complementary facts and honest unclassified evidence."""

import pytest
from test_resolver_selection import Repo, page, sources  # noqa: F401

from doclayer import resolver


@pytest.mark.asyncio
@pytest.mark.usefixtures("sources")
async def test_multipage_mechanisms_cover_complementary_facets_before_neighbors(tmp_path):
    question = "How can repeated prompt prefixes and response scheduling affect Harbor API latency or throughput?"
    pages = [
        page(
            "wiki/websocket.md",
            "# Harbor API\nWebSocket response delivery reduces API latency for multi-agent workflows.\n",
            "Harbor API",
        ),
        page(
            "doclayer/scheduler.md",
            "# Scheduler\nResponse scheduling affects API workflow coordination.\n",
            "Scheduler",
        ),
        page(
            "wiki/performance.md",
            """# Harbor API
## Prompt Caching
Enterprise deployment only.
Cache reuse requires the entire rendered prefix to match; changes before the breakpoint prevent reuse. [source: https://harbor.test/cache]
## Processing
Priority processing delivers up to twice faster API responses for regular traffic only. [source: https://harbor.test/priority]
""",
            "Harbor API",
        ),
    ]
    answer = await resolver.resolve(question, repo=Repo(tmp_path, pages), live=False, capture=False)
    text = answer._prose()
    assert "entire rendered prefix to match" in text
    assert "twice faster API responses for regular traffic only" in text
    assert "Enterprise deployment only." in text
    assert "https://harbor.test/priority" in text
    assert len(answer.git_facts) <= 8


@pytest.mark.asyncio
@pytest.mark.usefixtures("sources")
async def test_unclassified_delivered_runtime_evidence_is_partial_not_absent(tmp_path):
    question = "Does continuing with previous_response_id preserve browser sessions, files and Python variables?"
    body = """# Device API
## Runtime State
API conversation and execution environments have separate state.
Continuing with previous_response_id does not restore browser sessions or runtime variables. [source: https://device.test/runtime]
"""
    answer = await resolver.resolve(
        question, repo=Repo(tmp_path, [page("wiki/runtime.md", body, "Device API")]), live=False, capture=False
    )
    assert "does not restore browser sessions" in answer._prose()
    assert "partial" in answer.dispositions
    assert "answered" not in answer.dispositions
    assert "not_found_in_checked_sources" not in answer.dispositions
    assert any("files" in unknown for unknown in answer.unknowns)
    assert resolver.answer_evidence(answer)
    assert all(not row["facets"] for row in resolver.answer_evidence(answer))


@pytest.mark.asyncio
@pytest.mark.usefixtures("sources")
async def test_unclassified_foreign_evidence_does_not_become_partial(tmp_path):
    answer = await resolver.resolve(
        "Does Nimbus preserve browser sessions on Harbor?",
        repo=Repo(
            tmp_path,
            [page("wiki/other.md", "# Nimbus\nNimbus preserves browser sessions.\n", "Nimbus", platform="OtherCloud")],
        ),
        live=False,
        capture=False,
    )
    assert not answer.git_facts
    assert not resolver.answer_evidence(answer)
    assert "partial" not in answer.dispositions
    assert "not_found_in_checked_sources" in answer.dispositions


def test_single_facet_selection_keeps_ordinary_relevance_and_cap():
    question = "What is the Nimbus port?"
    answer = resolver.Answer(question, None)
    for p in [
        page("wiki/nimbus.md", "# Nimbus\nNimbus port is 8080.\n", "Nimbus"),
        page("wiki/other.md", "# Other\nAn unrelated port is 9000.\n", "Other"),
    ]:
        resolver._append_page_facts(answer, p, question)
    result = resolver._rank_facts(answer.git_facts, question, 1)
    assert len(result) == 1 and "8080" in result[0]["value"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("sources")
async def test_heldout_parameters_remain_unknown_even_with_matching_facet(tmp_path):
    answer = await resolver.resolve(
        "Which AWS Bedrock Converse API parameters should I use for Nimbus?",
        repo=Repo(
            tmp_path,
            [
                page(
                    "wiki/api.md",
                    "# Nimbus\nParameters include max_output_tokens for the Responses API.\n",
                    "Nimbus",
                    platform="OpenAI",
                )
            ],
        ),
        live=False,
        capture=False,
    )
    assert answer.scope_constraints
    assert not answer.git_facts
    assert not resolver.answer_evidence(answer)
    assert "answered" not in answer.dispositions
    assert "partial" not in answer.dispositions


def test_unclassified_withheld_or_unattested_material_never_becomes_partial():
    question = "Does continuing preserve browser sessions and Python variables?"
    for context in ["", "x" * (resolver.EVIDENCE_CHARS + 1)]:
        answer = resolver.Answer(question, None)
        answer.git_facts = [
            {
                "fact": "Runtime",
                "value": "Browser sessions and Python variables persist.",
                "evidence_context": context,
                "source": "wiki/runtime.md@aaaaaaaa",
            }
        ]
        assert not resolver.answer_evidence(answer)
        dispositions = resolver._dispositions(answer, lag_flags=[], called={"brain_git": True})
        assert "partial" not in dispositions
        assert "answered" not in dispositions
    answer = resolver.Answer(question, None)
    answer.search_hits = [
        {"path": "wiki/runtime.md", "commit": "a" * 40, "content": "Browser sessions and Python variables persist."}
    ]
    assert not resolver.answer_evidence(answer)
