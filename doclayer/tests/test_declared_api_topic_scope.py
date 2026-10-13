"""Committed model/API topic declarations never waive real hosting constraints."""

import subprocess

import pytest

from doclayer import resolver
from doclayer.applicability import matches_scope, requested_scopes, source_scope
from doclayer.brainrepo import BrainRepo
from doclayer.coverage import Catalog, CatalogPage, SubjectEntry


@pytest.fixture
def declarations(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for path, kind, tags in [
        ("models.md", "model-family", "[vendor, nimbus-2, api]"),
        ("responses.md", "product", "[vendor, nimbus-2, responses-api, api]"),
    ]:
        (tmp_path / path).write_text(f"---\nsubject_type: {kind}\ntags: {tags}\n---\n# Declared topic\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "Declared metadata",
        ],
        check=True,
    )
    catalog = Catalog(
        "ok",
        "declared",
        1,
        subjects={
            "models": SubjectEntry("models", "model-family", "Model Lineup", pages=[CatalogPage("models.md", None)]),
            "responses": SubjectEntry(
                "responses",
                "product",
                "Vendor Responses API",
                aliases=["responses api"],
                pages=[CatalogPage("responses.md", None)],
            ),
        },
    )
    return BrainRepo(tmp_path), catalog


def effective(declarations, question, subject="vendor", hosts=()):
    repo, catalog = declarations
    raw = requested_scopes(question, subject)
    return resolver._declared_api_topic_scopes(repo, catalog, hosts, question, subject, raw)


@pytest.mark.parametrize("prefix", ["Nimbus-2", "Nimbus-2 Responses"])
def test_exact_declared_model_api_topics_do_not_become_host_constraints(declarations, prefix):
    q = f"For {prefix} API, what is the output capacity?"
    assert requested_scopes(q, "vendor")
    assert effective(declarations, q) == []
    answer = resolver.Answer(q, "vendor")
    answer.scope_constraints = effective(declarations, q)
    page = resolver.BrainPage(
        "responses.md",
        {"title": "Vendor Responses API"},
        "Max output: 100 tokens, only in supported configurations.\n",
        "commit",
    )
    resolver._append_page_facts(answer, page, q)
    assert any("100 tokens" in fact["value"] and "only in supported" in fact["value"] for fact in answer.git_facts)


@pytest.mark.parametrize(
    "question",
    [
        "For OneCloud API, what is the output capacity?",
        "For Unknown-2 API, what is the output capacity?",
        "For Nimbus-2 Unlisted API, what is the output capacity?",
        "For Nimbus-2 API, what is the output capacity on OneCloud?",
        "For Nimbus-2 API, what is the output capacity via OneCloud?",
        "For Nimbus-2 API for provider OneCloud, what is the output capacity?",
    ],
)
def test_unknown_and_explicit_usage_constraints_still_block_other_hosts(declarations, question):
    scopes = effective(declarations, question)
    assert scopes
    assert not matches_scope(source_scope({"platform": "OtherHost"}, title="Vendor Responses API"), scopes)


@pytest.mark.parametrize("kind", ["host", "service", "account", "product"])
def test_ambiguous_known_provider_or_host_vetoes_topic_interpretation(declarations, kind):
    repo, catalog = declarations
    catalog.subjects["conflict"] = SubjectEntry("conflict", kind, "Nimbus-2")
    q = "For Nimbus-2 API, what is the output capacity?"
    assert effective((repo, catalog), q) == requested_scopes(q, "vendor")


def test_host_inventory_unknown_catalog_wrong_subject_and_uncommitted_tags_do_not_waive_scope(declarations):
    repo, catalog = declarations
    q = "For Nimbus-2 API, what is the output capacity?"
    assert effective(declarations, q, hosts=["nimbus 2"])
    assert effective(declarations, q, subject="OtherVendor")
    (repo.root / "models.md").write_text("---\nsubject_type: model-family\ntags: [vendor, invented-3, api]\n---\n")
    (repo.root / "responses.md").write_text("---\nsubject_type: product\ntags: [vendor, invented-3, api]\n---\n")
    assert effective(declarations, "For Invented-3 API, what is the output capacity?")
    catalog.status = "unknown"
    assert effective(declarations, q)


@pytest.mark.parametrize("clause", ["on Nimbus-2", "via Nimbus-2", "for provider Nimbus-2"])
def test_explicit_same_named_constraint_is_not_lost_to_deduplication(declarations, clause):
    assert effective(declarations, f"For Nimbus-2 API {clause}, what is the output capacity?") == [
        {"dimension": "platform", "value": "Nimbus-2"}
    ]


@pytest.mark.asyncio
async def test_normal_resolver_applies_declared_topic_before_source_selection(declarations, monkeypatch):
    from test_resolver_selection import Repo, page

    repo, catalog = declarations
    p = page(
        "wiki/vendor.md",
        "## Output capacity\nMax output: 100 tokens, only in supported configurations.\n",
        "Vendor Responses API",
    )
    monkeypatch.setattr(resolver, "_route_inputs", lambda root: (catalog, []))
    monkeypatch.setattr(resolver, "_safe_index_lag", lambda repo: [])
    monkeypatch.setattr(resolver, "stale_observation_flags", lambda repo: [])

    async def query(*args):
        return [], "offline"

    async def docs(*args):
        return {"error": "offline"}

    monkeypatch.setattr(resolver, "query_vo_rag", query)
    monkeypatch.setattr(resolver, "query_context7", docs)
    answer = await resolver.resolve(
        "For Nimbus-2 Responses API, what is the output capacity?",
        product="vendor",
        repo=Repo(repo.root, [p]),
        live=False,
        force=True,
        capture=False,
    )
    assert answer.scope_constraints == []
    assert "100 tokens" in answer._prose()
    assert "only in supported configurations" in answer._prose()


@pytest.mark.parametrize("name", ["Nimbus-2", "Nimbus2"])
def test_known_host_or_provider_rendering_cannot_be_waived_as_model_topic(declarations, name):
    repo, catalog = declarations
    question = "For Nimbus-2 Responses API, what is the output capacity?"
    raw = requested_scopes(question, "vendor")
    assert effective(declarations, question, hosts=[name]) == raw
    catalog.subjects["provider"] = SubjectEntry("provider", "product", name)
    assert effective((repo, catalog), question) == raw
