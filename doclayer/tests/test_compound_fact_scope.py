"""Exact local fact identity and compound renderings are applicability evidence."""

import pytest
from test_resolver_selection import page

from doclayer import resolver
from doclayer.applicability import matches_scope, requested_scopes, source_scope


@pytest.mark.parametrize(
    "wanted, rendered",
    [("OneCloud", "One Cloud"), ("onecloud", "One Cloud"), ("onecloud", "one cloud"), ("One Cloud", "OneCloud")],
)
def test_contiguous_identifier_renderings_match_without_host_specific_aliases(wanted, rendered):
    assert matches_scope(
        source_scope({}, title=f"StreamDB {rendered} Access"), [{"dimension": "platform", "value": wanted}]
    )


@pytest.mark.parametrize("title", ["One unrelated Cloud", "One / Cloud", "One; Cloud", "Cloud One"])
def test_compound_identity_does_not_join_unrelated_or_reversed_title_tokens(title):
    assert not matches_scope(source_scope({}, title=title), [{"dimension": "platform", "value": "OneCloud"}])


def test_identity_rendering_does_not_join_distinct_declared_scopes_or_weaken_versions_editions():
    assert not matches_scope(
        source_scope({"platform": ["One", "Cloud"]}), [{"dimension": "platform", "value": "OneCloud"}]
    )
    assert not matches_scope(source_scope({"version": "v1.2"}, title="v12"), [{"dimension": "version", "value": "v12"}])
    assert not matches_scope(
        source_scope({"edition": "Community"}, title="Enterprise"), [{"dimension": "edition", "value": "Enterprise"}]
    )
    assert not matches_scope(
        source_scope({"provider": "Primary", "service": "Harbor Hosted"}, statement="Primary native API"),
        [{"dimension": "platform", "value": "Primary"}],
    )


def test_exact_fact_statement_can_supply_application_identity_but_explicit_scope_wins():
    question = "Which database is used on app-intel?"
    wanted = requested_scopes(question, "OneCloud")
    assert wanted == [{"dimension": "platform", "value": "app-intel"}]
    scope = source_scope({}, title="App DB One Cloud Access", statement="app-intel uses PostgreSQL on OneCloud.")
    assert matches_scope(scope, wanted)
    assert not matches_scope(
        source_scope(
            {"platform": "OtherService"},
            title="App DB One Cloud Access",
            statement="app-intel uses PostgreSQL on OneCloud.",
        ),
        wanted,
    )


def collect(pages, question, subject):
    answer = resolver.Answer(question, subject)
    for p in pages:
        resolver._append_page_facts(answer, p, question)
    return answer


def test_actual_local_host_and_application_scope_are_kept_with_complete_source():
    body = (
        "## Database\napp-intel uses PostgreSQL hosted on OneCloud, reachable only through the approved SSH tunnel.\n"
    )
    p = page("wiki/app-db-access.md", body, "App DB One Cloud Access")
    answer = collect([p], "What database does app-intel use on OneCloud?", None)
    assert any(
        "PostgreSQL" in fact["value"] and "only through the approved SSH tunnel" in fact["value"]
        for fact in answer.git_facts
    )
    assert all("only through the approved SSH tunnel" in fact["evidence_context"] for fact in answer.git_facts)
    host_answer = collect([p], "What database is used on app-intel?", "OneCloud")
    assert any("PostgreSQL" in fact["value"] for fact in host_answer.git_facts)


def test_neighbor_application_scope_does_not_promote_other_fact_on_same_page():
    p = page(
        "wiki/services.md",
        "## Applications\nOtherService uses MariaDB on OneCloud.\napp-intel supports a 100-token output capacity.\n",
        "One Cloud Services",
    )
    answer = collect([p], "What database is used on app-intel?", "OneCloud")
    assert not any("MariaDB" in fact["value"] for fact in answer.git_facts)


@pytest.mark.parametrize(
    "meta",
    [
        {"platform": "OtherHost"},
        {"provider": "OneCloud", "service": "Harbor Hosted"},
        {"version": "v1"},
        {"edition": "Community"},
    ],
)
def test_own_statement_cannot_override_explicit_platform_version_or_edition(meta):
    p = page(
        "wiki/model.md",
        "## Invocation\nModel A request parameters include wrong_field on OneCloud for version v2 in Enterprise edition.\n",
        "Model A",
        **meta,
    )
    question = "What OneCloud request parameters invoke Model A?"
    if "version" in meta:
        question += " Use version v2."
    if "edition" in meta:
        question += " Use Enterprise edition."
    answer = collect([p], question, "Model A")
    assert not answer.git_facts
