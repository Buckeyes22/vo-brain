"""A question naming a host only gets candidates that mention that host."""

from types import SimpleNamespace

from test_verification_contract import git, published  # noqa: F401

from doclayer import resolver, verification
from doclayer.brainrepo import BrainRepo

QUESTION = (
    "What documented root-filesystem cleanup or data relocation conventions apply to "
    "App1, especially /home/user/example-app-* and /var/lib/apport?"
)


def commit_pages(root):
    pages = {
        "wiki/app-1-disk.md": "---\ntitle: Disk layout\n---\n# Disk\nApp1 root filesystem is cleaned weekly.\n",
        "wiki/tagged-host.md": "---\ntitle: Tagged\ntags:\n  - app-1\n---\n# T\nCleanup notes.\n",
        "wiki/openai-api-performance-modes.md": "---\ntitle: Perf\n---\n# Perf\nWebSockets are recommended, especially for agents.\n",
        "wiki/ws1-orca-accessibility-state.md": "---\ntitle: Orca\n---\n# Orca\nOrca is installed on ws1, especially for apport.\n",
    }
    for path, text in pages.items():
        (root / path).write_text(text)
        git(root, "add", path)
    git(root, "commit", "-m", "pages")
    return list(pages)


def test_unrelated_ranked_pages_are_not_candidates_for_a_host_question(published, identities):
    root = published[0]
    identities(hosts=["App1", "ws1"])
    paths = commit_pages(root)
    kept = verification._host_relevant(set(paths), QUESTION, BrainRepo(root, snapshot=True))
    assert kept == {"wiki/app-1-disk.md", "wiki/tagged-host.md"}


def test_question_without_a_known_host_keeps_every_ranked_candidate(published, identities):
    root = published[0]
    identities(hosts=["ws1"])
    paths = commit_pages(root)
    question = "What cleanup conventions apply to the root filesystem?"
    assert verification._host_relevant(set(paths), question, BrainRepo(root, snapshot=True)) == set(paths)


def test_verify_existing_gap_frames_only_host_pages(published, identities, tmp_path, monkeypatch):
    root, path, commit, ledger, record, reply, index = published
    identities(hosts=["App1"])
    paths = commit_pages(root)
    head = git(root, "rev-parse", "HEAD")
    import hashlib

    index.update(
        last_commit=head,
        files={p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in [path, *paths]},
        file_writer_versions={p: "brain-v4-governing-context" for p in [path, *paths]},
    )
    ledger.update(
        record.gap_id,
        lambda r: (
            setattr(r, "job_links", []),
            setattr(r, "representative_question", QUESTION),
            setattr(r, "facets", []),
        ),
    )
    facts = [dict(source=f"{p}@{head[:8]}", value="x", evidence_context="x") for p in paths]

    async def fake_resolve(*args, **kwargs):
        return SimpleNamespace(
            git_facts=facts, doc_facts=[], search_hits=[], scope_constraints=[], question=QUESTION, _prose=lambda: ""
        )

    monkeypatch.setattr(resolver, "resolve", fake_resolve)
    framed = []

    async def fake_semantic(question, facets, scopes, pages, **kwargs):
        framed.append(sorted(p["path"] for p in pages))
        return None

    monkeypatch.setattr(verification, "_semantic_evidence", fake_semantic)
    verification.verify_existing_gap(
        record.gap_id,
        ledger,
        model_client=SimpleNamespace(
            cfg=SimpleNamespace(
                inference=SimpleNamespace(
                    url="u", model="m", enable_thinking=False, effective_output_target=1, effective_prompt_target=1
                ),
                limits=SimpleNamespace(prompt_char_budget=1),
            ),
            usage=None,
        ),
        budget=object(),
    )
    assert framed == [["wiki/app-1-disk.md", "wiki/tagged-host.md"]]


def commit_equivalent_pages(root):
    pages = {
        "wiki/my-token-plan.md": "---\ntitle: My token plan\n---\n# Token plan\nRoute row: LiteLLM 192.0.2.228:4000 serves gateway/S.\n",
        "wiki/fw-forgejo.md": "---\ntitle: Forgejo\n---\n# Forgejo\nForgejo runs on fw.\n",
        "wiki/unrelated.md": "---\ntitle: Unrelated\n---\n# U\nBitwarden clients on ws1 use 192.0.2.187.\n",
    }
    for path, text in pages.items():
        (root / path).write_text(text)
        git(root, "add", path)
    git(root, "commit", "-m", "equivalents")
    return pages


def test_page_naming_the_host_only_by_ip_is_kept(published, identities):
    root = published[0]
    identities(hosts=["App1", "ws1"], groups=[["App1", "192.0.2.228"], ["ws1", "192.0.2.187"]])
    paths = list(commit_equivalent_pages(root))
    q = "Which Token Plan deployments back gateway/S on the LiteLLM gateway that App1 runs?"
    kept = verification._host_relevant(set(paths), q, BrainRepo(root, snapshot=True))
    assert kept == {"wiki/my-token-plan.md"}


def test_page_naming_an_alias_that_shares_the_hosts_address_is_kept(published, identities):
    root = published[0]
    identities(hosts=["SVC1", "fw"], groups=[["SVC1", "192.0.2.157"], ["fw", "192.0.2.157"]])
    paths = list(commit_equivalent_pages(root))
    kept = verification._host_relevant(set(paths), "What does SVC1 serve?", BrainRepo(root, snapshot=True))
    assert kept == {"wiki/fw-forgejo.md"}


def test_unrelated_page_without_an_equivalent_identity_is_dropped(published, identities):
    root = published[0]
    identities(hosts=["App1", "ws1"], groups=[["App1", "192.0.2.228"]])
    paths = list(commit_equivalent_pages(root))
    kept = verification._host_relevant(set(paths), "What does App1 run?", BrainRepo(root, snapshot=True))
    assert "wiki/unrelated.md" not in kept and "wiki/fw-forgejo.md" not in kept


def test_candidates_stay_the_two_host_pages_with_equivalents_loaded(published, identities):
    root = published[0]
    identities(hosts=["App1", "ws1"], groups=[["App1", "192.0.2.228"], ["ws1", "192.0.2.187"]])
    paths = commit_pages(root)
    kept = verification._host_relevant(set(paths), QUESTION, BrainRepo(root, snapshot=True))
    assert kept == {"wiki/app-1-disk.md", "wiki/tagged-host.md"}


def test_ssh_config_aliases_sharing_a_hostname_and_fleet_page_addresses_become_one_group(tmp_path, monkeypatch):
    from doclayer import applicability, coverage

    config = tmp_path / "ssh_config"
    config.write_text(
        "Host SVC1\n  HostName 10.0.0.7\nHost fw\n  HostName 10.0.0.7\nHost *\n  User x\n"
        "Host 10.0.0.7 10.0.0.9\n  User user\n"
    )
    assert sorted(map(sorted, coverage.ssh_host_groups(config))) == [["10.0.0.7", "SVC1"], ["10.0.0.7", "fw"]]
    assert coverage.fleet_host_addresses("- **App1** — `192.0.2.228`. GPU.") == [("App1", "192.0.2.228")]
    merged = applicability._merge_groups([{"svc1", "10007"}, {"fw", "10007"}, {"app1", "19202228"}])  # noqa: SLF001
    assert frozenset({"svc1", "fw", "10007"}) in merged and len(merged) == 2


# -- host pages and recorded services also say "this host" ------------------------------- #

import pytest  # noqa: E402

from doclayer import applicability, coverage  # noqa: E402

FLEET = """# Fleet hosts

## Hosts and roles

- **App1** — `192.0.2.228`. GPU inference. The LiteLLM hub traffic terminates here.
- **SVC1** — services host; co-located with Forgejo. Monitoring exporters run here.
- **HV1** — heavy compute host.

## Notes

Nothing about services here.
"""


def entry(slug, kind, title=None, aliases=(), pages=()):
    return coverage.SubjectEntry(
        slug=slug,
        kind=kind,
        title=title or slug,
        aliases=list(aliases),
        pages=[coverage.CatalogPage(p, None) for p in pages],
    )


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "infra").mkdir()
    (tmp_path / "infra/fleet-hosts.md").write_text(FLEET)
    (tmp_path / "infra/forgejo.md").write_text("---\ntitle: forgejo\ntags:\n  - infra\n  - git\n---\n# Forgejo\n")
    (tmp_path / "infra/svc1-services.md").write_text(
        "---\ntitle: svc1-services\ntags:\n  - svc1\n---\n# SVC1 services\n"
    )
    catalog = SimpleNamespace(
        subjects={
            s.slug: s
            for s in [
                entry("forgejo", "service", "Forgejo"),
                entry("litellm-hub", "service", "LiteLLM hub", aliases=["litellm"]),
                entry("SVC1", "host"),
                entry("App1", "host"),
                entry("fleet-hosts", "host-inventory"),
            ]
        }
    )
    return tmp_path, catalog


def test_profile_builder_reads_services_from_the_fleet_page_and_pages_from_infra(vault):
    root, catalog = vault
    groups = applicability._merge_groups([{"svc1", "1920215"}])  # noqa: SLF001
    profiles = {p.keys: p for p in applicability._host_profiles(root, catalog, groups)}  # noqa: SLF001
    svc1 = next(p for k, p in profiles.items() if "svc1" in k)
    assert svc1.services == {"forgejo"} and "infra/svc1-services" in svc1.pages
    ubuntu = next(p for k, p in profiles.items() if "app1" in k)
    assert "litellmhub" in ubuntu.services and "forgejo" not in ubuntu.services


def profiles_for(vault):
    root, catalog = vault
    return applicability._host_profiles(root, catalog, applicability._merge_groups([]))  # noqa: SLF001


def commit_relation_pages(root):
    pages = {
        "wiki/git-host.md": "---\ntitle: Git host\n---\n# Git\nForgejo serves the repositories over ssh.\n",
        "wiki/linker.md": "---\ntitle: Linker\n---\n# L\nCleanup runs nightly. See [[infra/svc1-services]] for the machine.\n",
        "wiki/stem-linker.md": "---\ntitle: Stem linker\n---\n# L\nSee [[svc1-services|the host]].\n",
        "wiki/unrelated.md": "---\ntitle: Unrelated\n---\n# U\nBitwarden clients on ws1; see [[infra/fleet-hosts]] and [[wiki/other]].\n",
    }
    for path, text in pages.items():
        (root / path).write_text(text)
        git(root, "add", path)
    git(root, "commit", "-m", "relations")
    return list(pages)


def test_forgejo_only_page_is_kept_for_an_svc1_question(published, vault, identities):
    root = published[0]
    identities(hosts=["SVC1", "App1"], profiles=profiles_for(vault))
    paths = commit_relation_pages(root)
    kept = verification._host_relevant(set(paths), "What does SVC1 serve?", BrainRepo(root, snapshot=True))
    assert "wiki/git-host.md" in kept and "wiki/unrelated.md" not in kept
    # The same page is not about App1: Forgejo is not recorded there.
    other = verification._host_relevant(set(paths), "What does App1 serve?", BrainRepo(root, snapshot=True))
    assert "wiki/git-host.md" not in other


def test_page_that_only_wikilinks_to_the_host_page_is_kept(published, vault, identities):
    root = published[0]
    identities(hosts=["SVC1"], profiles=profiles_for(vault))
    paths = commit_relation_pages(root)
    kept = verification._host_relevant(set(paths), "Which cleanup runs on SVC1?", BrainRepo(root, snapshot=True))
    assert {"wiki/linker.md", "wiki/stem-linker.md"} <= kept and "wiki/unrelated.md" not in kept


def test_page_with_no_host_service_or_link_is_still_dropped(published, vault, identities):
    root = published[0]
    identities(hosts=["SVC1"], profiles=profiles_for(vault))
    paths = commit_relation_pages(root)
    kept = verification._host_relevant(set(paths), "What does SVC1 do?", BrainRepo(root, snapshot=True))
    assert "wiki/unrelated.md" not in kept


def test_non_host_headings_and_the_inventory_page_make_no_profile(vault):
    root, catalog = vault
    (root / "infra/fleet-hosts.md").write_text(
        FLEET + "\n## Services notes\n\nForgejo and LiteLLM are mentioned in passing.\n"
    )
    keys = {k for p in profiles_for(vault) for k in p.keys}
    assert "notes" not in keys and "servicesnotes" not in keys and "fleethosts" not in keys
