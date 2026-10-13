"""A page's tags and aliases are scope context for host matching."""

from doclayer.applicability import matches_scope, source_scope

UBUNTU = [{"dimension": "platform", "value": "App1"}]


def test_tag_ubuntu_server_matches_app1_scope():
    meta = {"tags": ["nimbus-metar-taf", "app-1", "redis"]}
    scope = source_scope(
        meta,
        title="Nimbus METAR/TAF weather analytics app - deployment, release, dbt, TAF/Redis, gate cleanup",
        heading="Redis publication",
        statement="maxmemory is 2gb",
    )
    assert matches_scope(scope, UBUNTU)


def test_same_page_without_the_tag_does_not_match():
    scope = source_scope(
        {"tags": ["nimbus-metar-taf", "redis"]}, title="Nimbus app", heading="Redis", statement="maxmemory is 2gb"
    )
    assert not matches_scope(scope, UBUNTU)


def test_alias_matches_scope():
    scope = source_scope({"aliases": ["App 1", "App1 box"]}, title="Host inventory", statement="row")
    assert matches_scope(scope, UBUNTU)


def test_scalar_tag_and_alias_values_are_accepted():
    assert matches_scope(source_scope({"tags": "app-1"}, title="t"), UBUNTU)
    assert matches_scope(source_scope({"aliases": "App1"}, title="t"), UBUNTU)


def test_tags_are_context_only_never_explicit_scope():
    import json

    data = json.loads(source_scope({"tags": ["app-1"]}, title="t"))
    assert data["explicit"] == {}
    assert "app-1" in data["context"]


def test_explicit_declared_platform_still_overrides_tags():
    scope = source_scope({"platform": "OneCloud", "tags": ["app-1"]}, title="t")
    assert not matches_scope(scope, UBUNTU)


def test_path_stem_is_scope_context():
    scope = source_scope({}, title="App DB access", path="wiki/app-db-app-1-access.md", statement="read-only role")
    assert matches_scope(scope, UBUNTU)
    assert not matches_scope(source_scope({}, title="App DB access", path="wiki/app-db-access.md"), UBUNTU)


def test_product_named_in_its_own_title_heading_is_not_explicit_platform(identities):
    identities(hosts=["App1"], subjects=["vo-rag"])
    import json

    scope = source_scope(
        {"tags": ["app-1"]},
        title="vo-rag Production Deployment Topology",
        heading="vo-rag Production Deployment Topology / Storage",
        statement="cache override",
    )
    assert json.loads(scope)["explicit"] == {}
    assert matches_scope(scope, UBUNTU)
