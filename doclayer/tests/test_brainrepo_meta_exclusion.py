"""Repository meta (README, CLAUDE.md, LICENSE, docs/, doclayer/, .github/) is never a brain page."""

import subprocess

import pytest
from test_resolver_selection import sources  # noqa: F401

from doclayer import resolver
from doclayer.brainrepo import BrainRepo, is_brain_page

FACTS = "## Facts\n\n| Fact | Declared | Observed |\n|---|---|---|\n| Zephyrport listen port | `{port}` | — |\n"

META = {
    "README.md": FACTS.format(port=1111),
    "CLAUDE.md": FACTS.format(port=2222),
    "LICENSE": "MIT License zephyrport\n",
    "docs/architecture.md": FACTS.format(port=3333),
    "doclayer/STATUS.md": FACTS.format(port=4444),
    "doclayer/examples/notes.md": FACTS.format(port=5555),
    ".github/pull_request_template.md": FACTS.format(port=6666),
}
VAULT = {
    "wiki/a.md": "---\ntitle: A\nprivacy: internal\n---\n# A\n\nUnrelated page.\n",
    "infra/fleet-hosts.md": "---\ntitle: Fleet hosts\nprivacy: internal\n---\n# Fleet hosts\n\nNo services listed here.\n",
}


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "brain"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    for rel, text in {**META, **VAULT}.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-m", "fixture")
    return root


@pytest.mark.parametrize("snapshot", [False, True])
def test_only_vault_pages_are_candidates(vault, snapshot):
    repo = BrainRepo(vault, snapshot=snapshot)
    assert repo.markdown_paths() == ["infra/fleet-hosts.md", "wiki/a.md"]
    assert repo.find_page("README") is None
    assert repo.find_page("STATUS") is None
    assert resolver.git_page_scan(repo, "What is the zephyrport listen port?") == []


@pytest.mark.asyncio
async def test_resolver_never_cites_repository_meta(vault, sources):  # noqa: F811
    answer = await resolver.resolve(
        "What is the zephyrport listen port?", repo=BrainRepo(vault), live=False, capture=False
    )
    cited = repr(answer.git_facts) + answer._prose()
    for path in META:
        assert path not in cited
    for port in ("1111", "2222", "3333", "4444", "5555", "6666"):
        assert port not in cited


@pytest.mark.parametrize(
    "path,expected",
    [
        ("wiki/a.md", True),
        ("infra/fleet-hosts.md", True),
        ("sources/vendor/page.md", True),
        ("inbox/proposal.md", True),
        ("index.md", True),
        ("README.md", False),
        ("CLAUDE.md", False),
        ("LICENSE", False),
        ("docs/curator.md", False),
        ("doclayer/STATUS.md", False),
        (".github/workflows/ci.md", False),
        ("wiki/docs/page.md", True),
        ("wiki/README.md", True),
    ],
)
def test_is_brain_page(path, expected):
    assert is_brain_page(path) is expected


@pytest.mark.asyncio
async def test_brain_index_hits_on_repository_meta_are_dropped(monkeypatch):
    import httpx

    def result(path):
        payload = dict(path=path, heading="Facts", commit="a" * 40, kind="page", content=f"zephyrport in {path}")
        return dict(collection=resolver.settings.brain_collection, payload=payload, score=0.9)

    results = [result("docs/x.md"), result("wiki/a.md"), result("README.md"), result("doclayer/STATUS.md")]
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"results": results}))
    monkeypatch.setattr(resolver.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    hits, error = await resolver.query_vo_rag("What is the zephyrport listen port?")
    assert error is None
    assert [hit["path"] for hit in hits] == ["wiki/a.md"]
