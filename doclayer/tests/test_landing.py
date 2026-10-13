"""Tests for landing fetched copies as PRs (real git repo, fake Forgejo API)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from doclayer.landing import ForgejoClient, land_refresh, parse_remote


def _run(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return proc.stdout


@pytest.fixture()
def clone(tmp_path: Path) -> Path:
    """origin repo with a recipe + one copy, and a clone of it."""
    origin = tmp_path / "origin"
    (origin / "doclayer" / "recipes").mkdir(parents=True)
    (origin / "sources" / "prod").mkdir(parents=True)
    (origin / "doclayer" / "recipes" / "prod.yaml").write_text("product: prod\nlast_attempt: null\nlast_error: null\n")
    (origin / "sources" / "prod" / "old.md").write_text("old\n")
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["add", "-A"],
        ["commit", "-qm", "init"],
    ):
        _run(origin, *cmd)
    clone = tmp_path / "clone"
    _run(tmp_path, "clone", "-q", str(origin), str(clone))
    _run(clone, "config", "user.email", "t@t")
    _run(clone, "config", "user.name", "t")
    return clone


class FakeForgejo:
    """Records API calls; canned PR payload."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def create_pr(self, owner, repo, *, title, head, base, body):
        self.calls.append(("create", 0))
        self.title, self.head, self.base = title, head, base
        return {"number": 7, "html_url": f"https://forgejo.test/{owner}/{repo}/pulls/7"}

    def approve(self, owner, repo, number):
        self.calls.append(("approve", number))

    def merge(self, owner, repo, number):
        self.calls.append(("merge", number))


ROWS_OK = [{"slug": "new", "result": "ok"}]


def _write_new_copy(clone: Path) -> None:
    (clone / "sources" / "prod" / "new.md").write_text("---\nproduct: prod\n---\n\n# new\n\ncontent\n")


def test_sources_only_clean_refresh_is_auto_merged(clone):
    _write_new_copy(clone)
    fake = FakeForgejo()
    result = land_refresh(clone / "doclayer" / "recipes" / "prod.yaml", ROWS_OK, client=fake)
    assert result.action == "pr-merged"
    assert result.pr_number == 7
    assert [c[0] for c in fake.calls] == ["create", "approve", "merge"]
    assert result.changed == ["sources/prod/new.md"]
    # The branch reached origin and holds exactly the new copy.
    branch = _run(clone, "rev-parse", "--abbrev-ref", "HEAD").strip()
    assert branch.startswith("doclayer-fetch/prod-")
    _run(clone, "fetch", "origin")
    committed = _run(clone, "diff", "--name-only", f"origin/main..origin/{branch}").split()
    assert committed == ["sources/prod/new.md"]


def test_no_changes_means_no_pr(clone):
    result = land_refresh(clone / "doclayer" / "recipes" / "prod.yaml", ROWS_OK, client=FakeForgejo())
    assert result.action == "no-changes"
    assert result.pr_number is None


def test_junk_page_blocks_auto_merge(clone):
    _write_new_copy(clone)
    fake = FakeForgejo()
    rows = [{"slug": "new", "result": "ok"}, {"slug": "bad", "result": "junk:soft_404"}]
    result = land_refresh(clone / "doclayer" / "recipes" / "prod.yaml", rows, client=fake)
    assert result.action == "pr-left-open"
    assert [c[0] for c in fake.calls] == ["create"]
    assert any("soft_404" in reason for reason in result.reasons)


def test_out_of_scope_file_blocks_auto_merge(clone):
    _write_new_copy(clone)
    (clone / "wiki").mkdir()
    (clone / "wiki" / "other.md").write_text("touched\n")
    fake = FakeForgejo()
    result = land_refresh(clone / "doclayer" / "recipes" / "prod.yaml", ROWS_OK, client=fake)
    assert result.action == "pr-left-open"
    assert [c[0] for c in fake.calls] == ["create"]
    assert any("outside" in reason for reason in result.reasons)


def test_recipe_only_attempt_churn_is_no_change(clone):
    recipe = clone / "doclayer" / "recipes" / "prod.yaml"
    recipe.write_text("product: prod\nlast_attempt: 2026-10-01T00:00:00Z\nlast_error: null\n")
    fake = FakeForgejo()
    result = land_refresh(recipe, ROWS_OK, client=fake)
    assert result.action == "no-changes"
    assert fake.calls == []
    assert "last_attempt: null" in recipe.read_text()  # churn reset, not committed


def test_parse_remote_handles_ssh_and_https():
    assert parse_remote("ssh://git@git.example.org:2224/Example-org/brain") == (
        "Example-org",
        "brain",
    )
    assert parse_remote("https://forgejo.test/Example-org/vo-rag.git") == (
        "Example-org",
        "vo-rag",
    )


def test_client_sends_token_header():
    client = ForgejoClient(base_url="https://forgejo.test", token="sekrit-token")
    assert client._headers() == {"Authorization": "token sekrit-token"}
    anonymous = ForgejoClient(base_url="https://forgejo.test", token="")
    with pytest.raises(RuntimeError, match="no Forgejo token"):
        anonymous._headers()
