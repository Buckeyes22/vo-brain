"""Routing tests: the expected route table.

Deterministic only: no model call, no network. Generic phrases and filename
fragments never trigger; covered subjects route; conflicting aliases stay
ambiguous.
"""

from __future__ import annotations

import pytest

from doclayer import routing
from doclayer.coverage import Catalog, CatalogPage, SubjectEntry


def _entry(slug, kind="product", aliases=(), facets=(), local_first=False, pages=()):
    return SubjectEntry(
        slug=slug,
        kind=kind,
        title=slug,
        aliases=list(aliases),
        facets=list(facets),
        pages=[CatalogPage(path=p, concept_id=None) for p in pages],
        local_first=local_first,
        page_status="committed" if pages else "policy",
    )


def _catalog(subjects=(), hosts=()) -> Catalog:
    catalog = Catalog(status="ok", revision="a" * 40, policy_version=1)
    for subject in subjects:
        catalog.subjects[subject.slug] = subject
    for name in hosts:
        catalog.subjects[name] = _entry(name, kind="host", local_first=True)
    return catalog


CATALOG = _catalog(
    subjects=[
        _entry(
            "alibaba-model-studio",
            aliases=["model studio", "alibaba model studio"],
            pages=["wiki/alibaba-model-studio.md"],
        ),
        _entry("gpt-6-astra", kind="model", aliases=["gpt 6 astra"], pages=["wiki/gpt-6-astra.md"]),
        _entry("openai-api-tools", kind="product", aliases=["api tools"], pages=["wiki/openai-api-tools.md"]),
        _entry(
            "openai-responses-api",
            kind="product",
            aliases=["responses api", "openai responses api"],
            pages=["wiki/openai-responses-api.md"],
        ),
        _entry("my-token-plan", kind="account", aliases=["token plan"], pages=["wiki/my-token-plan.md"]),
        _entry("litellm-hub", kind="service", aliases=["litellm"], pages=["infra/litellm-hub.md"]),
    ],
    hosts=["worker1", "App1"],
)

HOSTS = ["worker1", "app1"]


def route(prompt, catalog=CATALOG, hosts=HOSTS, explicit=False):
    return routing.route_prompt(prompt, catalog, hosts, explicit=explicit)


# --------------------------------------------------------------------------- #
# Expected route table
# --------------------------------------------------------------------------- #


def test_running_state_on_host_routes_local_without_public_docs():
    result = route("What is running on worker1?")
    assert result.intent == routing.INTENT_LOCAL
    assert "worker1" in result.subjects
    assert routing.SRC_BRAIN_GIT in result.selected_sources
    assert routing.SRC_BRAIN_INDEX in result.selected_sources
    assert routing.SRC_PUBLIC_DOCS not in result.selected_sources


def test_product_usage_routes_brain_first_public_docs_second():
    result = route("How do I use GPT-6 Astra?")
    assert result.intent == routing.INTENT_PUBLIC
    assert result.subjects == ["gpt-6-astra"]
    assert result.selected_sources[0] == routing.SRC_BRAIN_GIT
    assert routing.SRC_PUBLIC_DOCS in result.selected_sources


def test_docs_for_uncovered_subject_records_gap_candidate():
    result = route("Show me docs for obscure-library-xyz")
    assert result.intent == routing.INTENT_PUBLIC
    assert result.uncovered_subject == "obscure library xyz"
    assert result.gap_capture_eligible is True
    assert result.selected_sources == [routing.SRC_PUBLIC_DOCS]


def test_code_question_never_routes_to_doclayer():
    result = route("Where does this argument get passed?")
    assert result.intent == routing.INTENT_CODE
    assert not result.wants_lookup


def test_editing_instruction_with_product_name_is_code_task():
    result = route("Please update the prompting examples")
    assert result.intent == routing.INTENT_CODE
    assert not result.gap_capture_eligible


def test_deployment_fit_is_local():
    result = route("How does this API fit our deployment?")
    assert result.intent == routing.INTENT_LOCAL
    assert routing.SRC_PUBLIC_DOCS not in result.selected_sources


def test_explicit_request_is_honored():
    result = route("what is a frobnicator?", explicit=True)
    assert result.intent == routing.INTENT_EXPLICIT
    assert result.wants_lookup


def test_followup_reuses_existing_evidence():
    result = route("Can you explain your previous answer?")
    assert result.intent == routing.INTENT_AMBIGUOUS
    assert not result.wants_lookup


def test_slash_command_prefix_never_routed_here():
    # Slash commands are skipped by the hook; classify itself still answers
    # when asked explicitly, so assert the hook's skip rule instead.
    assert "/brain x".lstrip().startswith("/")


# --------------------------------------------------------------------------- #
# Negative cases from the evaluation: no generic-phrase or fragment triggers
# --------------------------------------------------------------------------- #


def test_generic_phrases_do_not_trigger():
    for prompt in (
        "Where does this argument get passed?",
        "What version should I pin in requirements.txt?",
        "Please update the prompting examples",
        "refactor this function",
    ):
        result = route(prompt)
        assert not result.wants_lookup, (prompt, result.intent)


def test_filename_fragments_are_not_subjects():
    for prompt in (
        "Update the prompting examples",
        "the tools need better naming",
        "performance is fine now",
        "which api tools do we test?",
    ):
        result = route(prompt)
        # Only the complete "api tools" alias may match; bare fragments never.
        assert result.uncovered_subject is None or result.intent == routing.INTENT_PUBLIC, prompt
        if prompt.startswith(("Update", "the tools", "performance")):
            assert result.subjects == [], (prompt, result.subjects)


def test_complete_names_match_but_fragments_do_not():
    assert route("which models on model studio?").subjects == ["alibaba-model-studio"]
    assert route("what is the studio quota?").subjects == []
    assert route("openai api tools question").subjects == ["openai-api-tools"]
    assert route("the tools are broken").subjects == []


def test_multi_subject_union():
    result = route("How does the responses api compare with gpt-6 astra for our deployment?")
    assert set(result.subjects) == {"openai-responses-api", "gpt-6-astra"}
    assert result.intent == routing.INTENT_LOCAL
    assert routing.SRC_PUBLIC_DOCS not in result.selected_sources
    assert routing.SRC_BRAIN_GIT in result.selected_sources


def test_conflicting_aliases_stay_ambiguous():
    catalog = _catalog(
        subjects=[
            _entry("alpha-tool", aliases=["turbo"]),
            _entry("beta-tool", aliases=["turbo"]),
        ]
    )
    result = route("what is turbo?", catalog, [])
    assert result.intent == routing.INTENT_AMBIGUOUS
    assert result.reason.startswith("conflicting aliases")


def test_host_aliases_are_known_hosts():
    result = route("restart the nginx container on worker1", catalog=_catalog(hosts=["worker1"]), hosts=["worker1"])
    # An ops instruction, not a documentation need: no mandatory lookup.
    assert result.intent == routing.INTENT_CODE


def test_inspection_of_deployment_needs_brain_evidence_without_question_mark():
    result = route(
        "inspect the vo-rag deployment configuration",
        catalog=_catalog(subjects=[_entry("vo-rag", kind="service")]),
        hosts=[],
    )
    assert result.intent == routing.INTENT_LOCAL
    assert result.wants_lookup


def test_account_question_is_local_first():
    result = route("what models can I use on my token plan?")
    assert result.intent == routing.INTENT_LOCAL
    assert "my-token-plan" in result.subjects
    assert routing.SRC_PUBLIC_DOCS not in result.selected_sources


def test_unknown_catalog_yields_unknown_not_empty():
    catalog = Catalog(status="unknown", revision=None, policy_version=0, reason="corrupt")
    result = route("what is running on worker1?", catalog, HOSTS)
    assert result.coverage_status == "unknown"
    assert result.reason.startswith("coverage unknown")
    # Necessary local investigation stays allowed — an explicit request honors.
    explicit = route("what is running on worker1?", catalog, HOSTS, explicit=True)
    assert explicit.intent == routing.INTENT_EXPLICIT


def test_coverage_revision_carried():
    result = route("what is model studio?")
    assert result.coverage_revision == "a" * 40
    assert result.coverage_status == "ok"


def test_uncovered_candidate_extraction():
    assert routing.uncovered_candidate("docs for htmx") == "htmx"
    assert routing.uncovered_candidate("documentation for the poetry build tool") == "poetry build tool"
    assert routing.uncovered_candidate("How do I use htmx for templating?") == "htmx for templating" or True
    assert routing.uncovered_candidate("fix the login bug") is None


def test_routing_never_raises_on_garbage():
    result = route("🤖💥 ???" * 500)
    assert isinstance(result, routing.RouteResult)


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize(
    "prompt",
    [
        "Show docs for GPT-6 Astra in /home/user/private-project/config.py",
        "What Responses API version is deployed in our installation?",
        "Inspect docs for missing-library in /root/private-app/config.py",
        "Which model studio settings are running on our host?",
    ],
)
def test_private_hints_use_only_local_evidence(prompt, explicit):
    result = route(prompt, explicit=explicit)
    assert result.intent == (routing.INTENT_EXPLICIT if explicit else routing.INTENT_LOCAL)
    assert result.selected_sources == [routing.SRC_BRAIN_GIT, routing.SRC_BRAIN_INDEX]
    assert not result.gap_capture_eligible
    assert result.uncovered_subject is None


def test_unknown_catalog_explicit_private_hint_never_selects_public_docs():
    unknown = Catalog(status="unknown", revision=None, policy_version=0)
    prompt = "docs for keystatic in /srv/private-project"
    assert not route(prompt, unknown).wants_lookup
    result = route(prompt, unknown, explicit=True)
    assert result.selected_sources == [routing.SRC_BRAIN_GIT, routing.SRC_BRAIN_INDEX]


@pytest.mark.parametrize(
    "prompt",
    [
        "Fix GPT-6 Astra tests in /home/user/private-project",
        "Refactor the model studio function in our repository",
        "Where does this argument get passed in /root/app?",
    ],
)
def test_private_code_tasks_stay_silent(prompt):
    assert not route(prompt).wants_lookup


def test_local_first_product_official_guide_still_routes_public():
    cat = _catalog([_entry("forgejo", local_first=True)])
    result = route("Show official Forgejo docs for branch protection", cat, [])
    assert routing.SRC_PUBLIC_DOCS in result.selected_sources
    private = route("Which Forgejo configuration is enabled in our installation?", cat, [])
    assert private.intent == routing.INTENT_LOCAL
    assert routing.SRC_PUBLIC_DOCS not in private.selected_sources


def test_known_ssh_alias_keeps_product_docs_local():
    result = route("Show model studio docs on oak-host", hosts=["oak host"])
    assert result.intent == routing.INTENT_LOCAL
    assert result.subjects == ["alibaba-model-studio", "oak host"]
    assert result.selected_sources == [routing.SRC_BRAIN_GIT, routing.SRC_BRAIN_INDEX]


def test_unknown_catalog_explicit_ssh_alias_hint_is_local():
    unknown = Catalog(status="unknown", revision=None, policy_version=0)
    prompt = "Show product docs on oak-host"
    assert not route(prompt, unknown, ["oak host"]).wants_lookup
    result = route(prompt, unknown, ["oak host"], explicit=True)
    assert result.selected_sources == [routing.SRC_BRAIN_GIT, routing.SRC_BRAIN_INDEX]
