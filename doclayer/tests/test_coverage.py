"""Coverage catalog tests: committed bytes only, cache keyed by
HEAD + policy, corrupt catalogs degrade to coverage_unknown, SSH aliases are
cached separately with Include support."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from doclayer import coverage
from doclayer.coverage import (
    build_catalog,
    load_catalog,
    normalize,
    ssh_host_aliases,
)

POLICY = """\
version: 3
subjects:
  my-prod:
    kind: product
    aliases: ["prod alias"]
    facets: [api]
    public_docs:
      base_url: "https://docs.example/prod"
  infra-thing:
    kind: service
    aliases: ["the-thing"]
hosts:
  extra-host: {}
exclusions: ["welcome"]
local_first: ["infra-thing"]
sources: {}
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _brain(tmp_path: Path, commit: bool = True) -> Path:
    brain = tmp_path / "brain"
    (brain / "doclayer").mkdir(parents=True)
    (brain / "doclayer" / "routing-policy.yaml").write_text(POLICY)
    (brain / "wiki").mkdir()
    (brain / "wiki" / "my-prod.md").write_text(
        "---\nprivacy: internal\nproduct: my-prod\ntitle: My Prod\nconcept_id: c-12345678\n---\n\n# My Prod\n"
    )
    (brain / "wiki" / "welcome.md").write_text("---\ntitle: welcome\n---\n# x\n")
    (brain / "infra").mkdir()
    (brain / "infra" / "infra-thing.md").write_text("---\ntitle: infra-thing\n---\n# Infra thing\n")
    # nested canonical page
    (brain / "wiki" / "nested").mkdir()
    (brain / "wiki" / "nested" / "sub-page.md").write_text("---\nsubject_type: product\ntitle: Sub Page\n---\n# Sub\n")
    (brain / "sources" / "my-prod").mkdir(parents=True)
    (brain / "sources" / "my-prod" / "copy.md").write_text("---\nurl: x\n---\ncopy\n")
    (brain / "sources" / "empty-prod").mkdir(parents=True)  # an empty folder is not coverage
    if commit:
        _git(brain, "init", "-q")
        _git(brain, "add", "-A")
        _git(brain, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "a")
    return brain


def test_catalog_from_committed_pages(tmp_path):
    brain = _brain(tmp_path)
    catalog = build_catalog(brain)
    assert catalog.status == "ok"
    assert catalog.policy_version == 3
    subject = catalog.subjects["my-prod"]
    assert subject.kind == "product"
    assert [p.path for p in subject.pages] == ["wiki/my-prod.md"]
    assert subject.pages[0].concept_id == "c-12345678"
    assert subject.aliases == ["prod alias"]
    assert subject.facets == ["api"]
    assert subject.public_docs == {"base_url": "https://docs.example/prod"}
    assert subject.source_copies == ["sources/my-prod/copy.md"]
    # nested canonical pages are recognized
    assert "sub-page" in catalog.subjects
    # legacy pages bind through the policy's complete-name subjects
    infra = catalog.subjects["infra-thing"]
    assert [p.path for p in infra.pages] == ["infra/infra-thing.md"]
    assert infra.page_status == "committed"
    assert infra.local_first is True
    # exclusions and empty source folders never appear
    assert "welcome" not in catalog.subjects
    assert "empty-prod" not in catalog.subjects
    assert catalog.subjects["extra-host"].kind == "host"


def test_policy_subject_without_page_is_labeled_not_fabricated(tmp_path):
    brain = _brain(tmp_path)
    (brain / "doclayer" / "routing-policy.yaml").write_text(
        POLICY.replace(
            "hosts:",
            "  ghost-thing:\n    kind: service\nhosts:",
        )
    )
    catalog = build_catalog(brain)
    ghost = catalog.subjects["ghost-thing"]
    assert ghost.page_status == "policy"
    assert ghost.pages == []


def test_dirty_working_content_never_enters_the_catalog(tmp_path):
    brain = _brain(tmp_path)
    # Uncommitted change: a new page and an edited alias — HEAD still governs.
    (brain / "wiki" / "uncommitted.md").write_text("---\nsubject_type: product\ntitle: Uncommitted\n---\n")
    (brain / "wiki" / "my-prod.md").write_text("---\nproduct: my-prod\ntitle: DIRTY\naliases: [dirty-alias]\n---\n")
    catalog = build_catalog(brain)
    assert "uncommitted" not in catalog.subjects
    assert catalog.subjects["my-prod"].title == "My Prod"
    assert "dirty-alias" not in catalog.subjects["my-prod"].aliases


def test_cache_keyed_by_head(tmp_path):
    brain = _brain(tmp_path)
    cache = tmp_path / "cache"
    first = load_catalog(brain, cache)
    assert len(list(cache.glob("catalog-*.json"))) >= 2  # keyed + latest snapshot
    cached = load_catalog(brain, cache)
    assert cached.status == "ok" and cached.revision == first.revision
    (brain / "infra" / "second-thing.md").write_text("---\ntitle: second thing\n---\n")
    _git(brain, "add", "-A")
    _git(brain, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b")
    second = load_catalog(brain, cache)
    assert second.revision != first.revision


def test_corrupt_cache_rebuilds_and_unbuildable_is_unknown(tmp_path):
    brain = _brain(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    head = coverage.head_commit(brain)
    key, _latest = coverage._cache_key(brain, head, None)
    (cache / key).write_text("{not json")
    loaded = load_catalog(brain, cache)
    assert loaded.status == "ok"  # rebuilt from committed bytes

    broken = tmp_path / "nobrain"
    broken.mkdir()
    assert load_catalog(broken, cache).status == "unknown"


def test_unbuildable_catalog_keeps_explicitly_stale_snapshot(tmp_path):
    brain = _brain(tmp_path)
    cache = tmp_path / "cache"
    good = load_catalog(brain, cache)
    # New HEAD whose build fails: the explicitly stale prior snapshot wins.
    (brain / "doclayer" / "routing-policy.yaml").write_text(POLICY + "\n# bump\n")
    _git(brain, "add", "-A")
    _git(brain, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b")
    new_head = coverage.head_commit(brain)
    _key, latest = coverage._cache_key(brain, new_head, None)
    cache.joinpath(latest).write_text(json.dumps(good.to_dict()))
    # make the rebuild fail: HEAD now points at a commit with no policy file
    _git(brain, "rm", "-q", "doclayer/routing-policy.yaml")
    _git(brain, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c")
    stale = load_catalog(brain, cache)
    assert stale.status == "stale"
    assert stale.subjects


def test_catalog_schema_version_guard(tmp_path):
    brain = _brain(tmp_path)
    cache = tmp_path / "cache"
    good = load_catalog(brain, cache)
    data = good.to_dict()
    data["schema_version"] = 999
    key, _ = coverage._cache_key(brain, good.revision, None)
    (cache / key).write_text(json.dumps(data))
    reloaded = load_catalog(brain, cache)
    assert reloaded.status == "ok"  # schema mismatch forces a rebuild


def test_ssh_host_aliases_with_include(tmp_path):
    included = tmp_path / "config.d"
    included.mkdir()
    (included / "fleet.conf").write_text("Host worker-1 gpu2\n  HostName x\n")
    main = tmp_path / "config"
    main.write_text(
        f"Host App1\n  HostName 10.0.0.2\nInclude {included}/*.conf\nHost 192.0.2.1 *.wild prod.example.com !negated\n"
    )
    aliases = ssh_host_aliases(main)
    assert aliases == ["app1", "worker 1", "gpu2"]


def test_ssh_host_aliases_cached_separately(tmp_path):
    main = tmp_path / "config"
    main.write_text("Host a-host\n")
    cache = tmp_path / "cache"
    assert ssh_host_aliases(main, cache) == ["a host"]
    assert len(list(cache.glob("ssh-hosts-*.json"))) == 1
    assert ssh_host_aliases(main, cache) == ["a host"]
    assert len(list(cache.glob("ssh-hosts-*.json"))) == 1  # same stamp: reused
    main.write_text("Host a-host\nHost b-host\n")
    assert ssh_host_aliases(main, cache) == ["a host", "b host"]
    assert len(list(cache.glob("ssh-hosts-*.json"))) == 1  # old stamp replaced


def test_normalize():
    assert normalize("Worker-1_fleet") == "worker 1 fleet"


def test_policy_early_projects_load_match_and_survive_the_cache(tmp_path):
    brain = _brain(tmp_path)
    (brain / "doclayer" / "routing-policy.yaml").write_text(
        POLICY
        + "early_projects:\n"
        + "  example-connector:\n"
        + '    aliases: ["example connector", "example-to-data"]\n'
        + "    path: /home/user/Downloads/pip/example-connector\n"
    )
    catalog = build_catalog(brain)
    assert coverage.Catalog.from_dict(catalog.to_dict()).early_projects == catalog.early_projects
    for question in (
        "Where is the Example Connector design?",
        "what's the example-connector status",
        "How does the Example-to-data integration work?",
        "Read /home/user/Downloads/pip/example-connector/README.md",
    ):
        assert coverage.early_project(catalog, question)[0] == "example-connector"
    for question in ("What is example?", "Where is the connector config on worker-1?"):
        assert coverage.early_project(catalog, question) is None
    assert "example-connector" not in catalog.subjects
