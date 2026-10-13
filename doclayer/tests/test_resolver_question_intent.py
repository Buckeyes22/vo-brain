"""Ordinary quantity and choice questions retain applicable complete evidence."""

import pytest
from test_resolver_selection import Repo, page, sources  # noqa: F401

from doclayer import resolver
from doclayer.applicability import requested_scopes


@pytest.mark.parametrize("unit", ["one reply", "a single response", "each request", "one call", "one turn"])
def test_request_quantities_are_not_usage_platforms(unit):
    assert (
        requested_scopes(
            f"How much conversation history can Nimbus accept, and how much can it generate in {unit}?",
            "Nimbus",
        )
        == []
    )


def test_request_quantity_does_not_remove_real_hosting_constraint():
    assert requested_scopes("How much can Nimbus generate in one reply on Harbor?", "Nimbus") == [
        {"dimension": "platform", "value": "Harbor"}
    ]
    assert requested_scopes("What parameters apply in OneCloud?", "Nimbus") == [
        {"dimension": "platform", "value": "OneCloud"}
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("sources")
async def test_capacity_question_delivers_both_quantities_without_invented_platform(
    tmp_path,
):
    question = "How much conversation history can GPT-6 Astra accept, and how much can it generate in one reply?"
    body = "# GPT-6 Astra\n## Specifications\n| Feature | Value |\n|---|---|\n| Context Window | 1,050,000 tokens |\n| Max Output | 128,000 tokens |\n"
    answer = await resolver.resolve(
        question,
        repo=Repo(tmp_path, [page("wiki/model.md", body, "GPT-6 Astra")]),
        live=False,
        capture=False,
    )
    assert not answer.scope_constraints
    assert "1,050,000 tokens" in answer._prose() and "128,000 tokens" in answer._prose()
    assert {"context", "output"} <= {facet for row in resolver.answer_evidence(answer) for facet in row["facets"]}


@pytest.mark.asyncio
@pytest.mark.usefixtures("sources")
@pytest.mark.parametrize("names", [("Astra", "Sol"), ("Nimbus", "Cirrus")])
async def test_choice_facts_beat_incidental_task_mentions_with_complete_context(tmp_path, names):
    first, second = names
    question = f"How should I choose between {first} and the other models for a task?"
    choice = page(
        "wiki/choices.md",
        f"# Model choices\nProvider recommendations for text workloads only.\n## Selection\n{first} is the default recommendation if cost and latency are not concerns.\n{second} is recommended to balance intelligence and cost.\n",
        "Model Choices",
    )
    incidental = page(
        "wiki/tools.md",
        f"# API Tools\n{first} tools execute tasks in browser runtimes; the model can choose when pending task results are needed.\nTool sessions preserve task state.\n",
        "API Tools",
    )
    answer = await resolver.resolve(question, repo=Repo(tmp_path, [incidental, choice]), live=False, capture=False)
    assert answer.git_facts and all(f["source"].startswith("wiki/choices.md@") for f in answer.git_facts)
    rendered = answer._prose()
    assert "balance intelligence and cost" in rendered
    assert "Provider recommendations for text workloads only." in rendered
    assert "browser runtimes" not in rendered
    assert all("selection" in row["facets"] for row in resolver.answer_evidence(answer))
    assert len(answer.git_facts) <= 8


@pytest.mark.asyncio
@pytest.mark.usefixtures("sources")
async def test_choice_evidence_still_requires_requested_hosting_scope(tmp_path):
    question = "How should I choose between Nimbus and other models on Harbor?"
    wrong = page(
        "wiki/other.md",
        "# Models\nNimbus is recommended for all tasks.\n",
        "Models",
        platform="OtherCloud",
    )
    right = page(
        "wiki/harbor.md",
        "# Models\nEnterprise workloads only.\nNimbus is recommended for complex tasks on Harbor.\n",
        "Models",
        platform="Harbor",
    )
    answer = await resolver.resolve(question, repo=Repo(tmp_path, [wrong, right]), live=False, capture=False)
    assert answer.scope_constraints == [{"dimension": "platform", "value": "Harbor"}]
    assert answer.git_facts and all(f["source"].startswith("wiki/harbor.md@") for f in answer.git_facts)
    assert "Enterprise workloads only." in answer._prose()


def test_live_model_identity_query_is_not_a_selection_request():
    assert "selection" not in resolver._question_aspects("Which model is running on Harbor?")
    assert not resolver._matches_requested_aspects(
        "Nimbus tools perform tasks.",
        "How should I choose between Nimbus and other models?",
    )


@pytest.mark.parametrize("phrase", ["in one reply on harbor", "in each response via harbor"])
@pytest.mark.asyncio
@pytest.mark.usefixtures("sources")
async def test_quantity_keeps_lowercase_hosting_scope_and_rejects_foreign_evidence(tmp_path, phrase):
    question = f"How much can Nimbus generate {phrase}?"
    assert requested_scopes(question, "Nimbus") == [{"dimension": "platform", "value": "harbor"}]
    wrong = page(
        "wiki/other.md",
        "# Nimbus\nMax Output: 900 tokens.\n",
        "Nimbus",
        platform="OtherCloud",
    )
    right = page(
        "wiki/harbor.md",
        "# Nimbus\nMax Output: 100 tokens.\n",
        "Nimbus",
        platform="harbor",
    )
    answer = await resolver.resolve(question, repo=Repo(tmp_path, [wrong, right]), live=False, capture=False)
    assert answer.scope_constraints == [{"dimension": "platform", "value": "harbor"}]
    assert answer.git_facts and all(f["source"].startswith("wiki/harbor.md@") for f in answer.git_facts)
    assert "900 tokens" not in answer._prose()
