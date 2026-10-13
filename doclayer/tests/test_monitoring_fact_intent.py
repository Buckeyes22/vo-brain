"""Ordinary multi-component operational questions retain actual private facts."""

import pytest
from test_resolver_selection import page

from doclayer import resolver
from doclayer.applicability import requested_scopes

SHORT = "Current app-coach deployment and Prometheus/Alertmanager runtime versions and config paths on App1 for B15 correction"
LONG = "What are the current app-coach deployment and Prometheus/Alertmanager runtime versions and configuration paths on App1 for replay of B15 corrective monitoring and secret isolation?"


@pytest.mark.parametrize("question", [SHORT, LONG])
def test_actual_multi_component_deployment_question_has_host_not_subject_or_phase_scope(question):
    assert requested_scopes(question) == [{"dimension": "platform", "value": "App1"}]
    assert resolver._question_aspects(question) >= {"version", "path"}


@pytest.mark.parametrize("preposition", ["on", "via"])
def test_explicit_lowercase_host_is_retained_despite_named_work_phase(preposition):
    q = f"Current Beacon/Siren runtime versions {preposition} onecloud for R12 correction"
    assert requested_scopes(q) == [{"dimension": "platform", "value": "onecloud"}]
    assert requested_scopes("Harbor API request parameters for Nimbus")


def collect(p, question, subject=None):
    answer = resolver.Answer(question, subject)
    resolver._append_page_facts(answer, p, question)
    return answer


def test_host_subject_hint_does_not_discard_explicitly_requested_component_versions():
    p = page(
        "wiki/services.md",
        "## Beacon\nRuntime version: 2.48.0.\n## Siren\nRuntime version: 0.27.0.\n",
        "Monitoring on OneCloud",
    )
    q = "What are the Beacon/Siren runtime versions on OneCloud?"
    answer = collect(p, q, "OneCloud")
    assert any("2.48.0" in f["value"] for f in answer.git_facts)
    assert any("0.27.0" in f["value"] for f in answer.git_facts)
    assert not collect(p, q, "OtherHost").git_facts


def test_named_neighbor_version_cannot_borrow_requested_owner_heading():
    p = page("wiki/services.md", "## Beacon\nNeighbor runtime version: 9.9.9.\n", "Monitoring on OneCloud")
    assert not collect(p, "What is the Beacon runtime version on OneCloud?", "OneCloud").git_facts


def test_incidental_neighbor_not_in_version_target_phrase_does_not_override_subject():
    p = page("wiki/services.md", "## Neighbor\nRuntime version: 9.9.9.\n", "Nimbus on OneCloud")
    assert not collect(p, "What Nimbus version integrates with Neighbor on OneCloud?", "Nimbus").git_facts


def test_paths_and_container_names_select_actual_values_not_topic_introduction():
    q = "Where are the live Beacon and Siren compose directories and container names on OneCloud?"
    p = page(
        "wiki/services.md",
        "This page describes compose directories and container names for monitoring.\n## Beacon\nCompose directory: `/opt/stacks/beacon`.\nContainer name: `beacon-metrics`.\n## Siren\nCompose directory: `/opt/stacks/siren`.\nContainer name: `siren-alerts`.\n",
        "Monitoring on OneCloud",
    )
    answer = collect(p, q)
    assert resolver._question_aspects(q) == {"path", "container"}
    assert len(answer.git_facts) == 4
    assert all("/opt/stacks/" in f["value"] or "name:" in f["value"] for f in answer.git_facts)
    assert not collect(page(p.path, p.body, p.title, platform="OtherHost"), q).git_facts


def test_topic_version_intro_and_citation_path_are_not_actual_values():
    q = "What are current Beacon runtime versions and configuration paths on OneCloud?"
    p = page(
        "wiki/services.md",
        "## Beacon\nThis page records current runtime versions and configuration paths [source: docs/configuration.yml@abcdef123].\n",
        "Monitoring on OneCloud",
    )
    assert not collect(p, q).git_facts


def test_three_component_versions_and_directories_survive_same_page_cutoff_with_qualified_context():
    q = "Current Worker deployment and Beacon/Siren runtime versions and config paths on OneCloud for R12 correction"
    body = (
        "All values are a dated read-only observation; presence does not establish health.\n"
        "## Worker\nImage tag: `worker:repair-deadbeef7`.\nCompose directory: `/opt/stacks/worker`.\n"
        "## Beacon\nRuntime version: 2.48.0.\nCompose directory: `/opt/stacks/beacon`.\n"
        "## Siren\nRuntime version: 0.27.0.\nCompose directory: `/opt/stacks/siren`.\n"
    )
    answer = collect(page("wiki/monitoring.md", body, "Monitoring on OneCloud"), q, "OneCloud")
    selected = resolver._rank_facts(answer.git_facts, q, cap=8)
    assert len(selected) <= 8
    for literal in [
        "repair-deadbeef7",
        "2.48.0",
        "0.27.0",
        "/opt/stacks/worker",
        "/opt/stacks/beacon",
        "/opt/stacks/siren",
    ]:
        assert any(literal in f["value"] for f in selected), literal
    assert all("presence does not establish health" in f["evidence_context"] for f in selected)
    assert not collect(page("wiki/monitoring.md", body, "Monitoring on OtherHost"), q, "OneCloud").git_facts


def test_no_read_only_command_is_invented_from_directory_or_observation_fact():
    q = "Where are the Beacon/Siren compose directories and container names on OneCloud, and which read-only commands check them?"
    body = (
        "## Beacon\nCompose directory: `/opt/stacks/beacon`.\nContainer name: `beacon-metrics`.\n"
        "## Siren\nCompose directory: `/opt/stacks/siren`.\nContainer name: `siren-alerts`.\n"
    )
    answer = collect(page("wiki/monitoring.md", body, "Monitoring on OneCloud"), q)
    selected = resolver._rank_facts(answer.git_facts, q, cap=8)
    for literal in ["beacon-metrics", "siren-alerts", "/opt/stacks/beacon", "/opt/stacks/siren"]:
        assert any(literal in f["value"] for f in selected)
    assert all("docker inspect" not in f["value"] and "curl" not in f["value"] for f in selected)


def test_mutating_script_is_not_a_compose_directory_or_read_only_check():
    q = "Which Beacon compose directories and supported read-only checks exist on OneCloud?"
    p = page(
        "wiki/monitoring.md",
        "## Beacon\nMerge script: `/opt/stacks/beacon/rewrite.sh` replaces alert rules.\nCompose directory: `/opt/stacks/beacon`.\nRead-only check: `docker inspect beacon-metrics`.\n",
        "Monitoring on OneCloud",
    )
    facts = collect(p, q).git_facts
    assert any("/opt/stacks/beacon`" in f["value"] for f in facts)
    assert any("docker inspect beacon-metrics" in f["value"] for f in facts)
    assert all("rewrite.sh" not in f["value"] for f in facts)


def test_leading_named_deployment_host_remains_a_real_constraint():
    q = "OneCloud deployment of Model A"
    assert requested_scopes(q, "Model A") == [{"dimension": "platform", "value": "OneCloud"}]
    p = page(
        "wiki/model.md",
        "Model A deployment endpoint is http://otherhost:8001.\n",
        "Model A deployment",
        platform="OtherHost",
    )
    assert not collect(p, q, "Model A").git_facts


def test_compound_host_subject_hint_matches_own_title_without_borrowing_sibling_body():
    q = "Where are the Beacon compose directories on OneCloud?"
    p = page("wiki/beacon.md", "## Beacon\nCompose directory: `/opt/stacks/beacon`.\n", "Beacon One Cloud Access")
    assert any("/opt/stacks/beacon" in f["value"] for f in collect(p, q, "OneCloud").git_facts)
    wrong = page(
        "wiki/beacon.md",
        "## Beacon\nCompose directory: `/opt/stacks/beacon`.\n## Neighbor\nOneCloud supports an unrelated feature.\n",
        "Beacon Other Host Access",
    )
    assert not collect(wrong, q, "OneCloud").git_facts


@pytest.mark.parametrize(
    "command",
    [
        "curl -X POST /api/reset",
        "curl -XDELETE /api/item",
        "curl --data=x /api/reset",
        "curl --request=POST /api/reset",
        "docker ps; touch /tmp/changed",
        "systemctl status api && systemctl restart api",
    ],
)
def test_mutating_or_chained_command_cannot_establish_read_only_check(command):
    assert "command" not in resolver._matching_aspects(
        f"Read-only check: `{command}`.", "Which read-only checks exist for Nimbus?"
    )


@pytest.mark.parametrize(
    "command",
    [
        "curl http://192.0.2.1/health",
        "curl -X GET http://192.0.2.1/health",
        "docker inspect beacon",
        "systemctl show api",
    ],
)
def test_literal_read_only_command_with_url_retains_command_facet(command):
    assert "command" in resolver._matching_aspects(
        f"Read-only check: `{command}`.", "Which read-only checks exist for Nimbus?"
    )


def test_versions_of_components_has_same_owner_and_host_guards():
    question = "What are the versions of Beacon and Siren on OneCloud?"
    assert resolver._requested_fact_owners(question) == {"beacon", "siren"}
    p = page(
        "wiki/services.md",
        "## Beacon\nRuntime version: 2.48.0.\n## Siren\nRuntime version: 0.27.0.\n## Neighbor\nRuntime version: 9.9.9.\n",
        "Monitoring on OneCloud",
    )
    facts = collect(p, question, "OneCloud").git_facts
    assert any("2.48.0" in f["value"] for f in facts)
    assert any("0.27.0" in f["value"] for f in facts)
    assert all("9.9.9" not in f["value"] for f in facts)
    assert not collect(page(p.path, p.body, p.title, platform="OtherHost"), question, "OneCloud").git_facts
