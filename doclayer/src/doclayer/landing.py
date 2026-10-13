"""Landing fetched doc copies as Forgejo pull requests.

After a refresh the fetcher commits the changed copies on a branch off
`origin/main`, pushes, and opens a PR. It merges the PR itself only when the
diff touches nothing but `sources/<product>/**` and that product's recipe
file AND every copy passed the junk checks — anything else stays open for
the maintainer. A refresh that changes nothing (only `last_attempt` churn) opens no
PR and the recipe's attempt timestamp is reset so it stays out of commits.

The Forgejo token lives in `~/.config/doclayer/forgejo-token` (chmod 600) on
the host that runs the fetcher. It is never printed or committed. With no
Forgejo URL configured (`DOC_LAYER_FORGEJO_URL` empty) landing reports
"publishing not configured" and touches neither git nor the network.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from doclayer.settings import settings


@dataclass
class LandingResult:
    """What the landing step did (or deliberately didn't)."""

    action: str  # no-changes | pr-created | pr-merged | pr-left-open | not-configured | error
    changed: list[str] = field(default_factory=list)
    pr_number: int | None = None
    pr_url: str | None = None
    reasons: list[str] = field(default_factory=list)


def forgejo_token() -> str | None:
    """The API token from the token file, or None."""
    try:
        return settings.forgejo_token_file.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def parse_remote(remote_url: str) -> tuple[str, str]:
    """(owner, repo) from a Forgejo remote URL (ssh or https)."""
    match = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?$", remote_url)
    if match is None:
        raise ValueError(f"cannot parse owner/repo from {remote_url!r}")
    return match.group(1), match.group(2)


NOT_CONFIGURED = "publishing not configured: DOC_LAYER_FORGEJO_URL is empty"


class ForgejoClient:
    """The few Forgejo API calls landing needs, token-authenticated."""

    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or settings.forgejo_url).rstrip("/")
        self.token = token if token is not None else forgejo_token()

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise RuntimeError(f"no Forgejo token at {settings.forgejo_token_file}")
        return {"Authorization": f"token {self.token}"}

    def _require_url(self) -> None:
        if not self.base_url:
            raise RuntimeError(NOT_CONFIGURED)

    def _call(self, method: str, path: str, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_url()
        response = httpx.request(
            method,
            f"{self.base_url}/api/v1{path}",
            json=json_body,
            headers=self._headers(),
            timeout=30.0,
        )
        if response.status_code >= 300:
            raise RuntimeError(f"forgejo {method} {path}: HTTP {response.status_code}: {response.text[:200]}")
        data: dict[str, Any] = response.json() if response.content else {}
        return data

    def create_pr(self, owner: str, repo: str, *, title: str, head: str, base: str, body: str) -> dict[str, Any]:
        return self._call(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            {"title": title, "head": head, "base": base, "body": body},
        )

    def approve(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        """Best-effort approval. Forgejo forbids approving your own PR (422) —
        the token's user is also the repo admin, so the merge below carries the
        PR with force_merge; the review is still attempted for the record."""
        try:
            return self._call(
                "POST",
                f"/repos/{owner}/{repo}/pulls/{number}/reviews",
                {"event": "APPROVED", "body": "sources-only refresh, junk checks passed"},
            )
        except RuntimeError as exc:
            if "approve your own pull" in str(exc):
                return {}
            raise

    def merge(self, owner: str, repo: str, number: int) -> None:
        """Merge as the org admin. Branch protection wants 1 approval and
        Forgejo forbids self-approval, so the admin bypass (force_merge) is
        the only way a fetcher-authored PR merges; the maintainer approved this flow."""
        self._call(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{number}/merge",
            {"Do": "merge", "force_merge": True},
        )


# --------------------------------------------------------------------------- #
# Git plumbing
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def _remote_url(repo: Path, remote: str = "origin") -> str:
    return _git(repo, "remote", "get-url", remote).strip()


# --------------------------------------------------------------------------- #
# Landing
# --------------------------------------------------------------------------- #


def changed_files(repo: Path, base: str = "origin/main") -> list[str]:
    """Files the working tree (staged or not) changed relative to base."""
    out = _git(repo, "status", "--porcelain")
    return sorted(line[3:].strip('"') for line in out.splitlines() if line[3:].strip())


def land_refresh(
    recipe_path: Path,
    report_rows: list[dict[str, str]],
    *,
    remote: str = "origin",
    base: str = "main",
    client: ForgejoClient | None = None,
) -> LandingResult:
    """Branch, commit, push, PR — and auto-merge a clean sources-only refresh.

    `report_rows` is the refresh's per-page report: a page whose `result` is
    not "ok" blocks auto-merge. The work branch is reset to `origin/<base>`
    first, so "changed" is always measured against what would be the PR base.
    With no injected client and no Forgejo URL configured, nothing runs and the
    result is "not-configured" (a setup state, not a retryable failure).
    """
    if client is None and not settings.forgejo_url:
        return LandingResult(action="not-configured", reasons=[NOT_CONFIGURED])
    repo = recipe_path.resolve().parents[2]
    product = recipe_path.stem if recipe_path.name != "recipe.yaml" else recipe_path.parent.name
    recipe_rel = recipe_path.relative_to(repo).as_posix()
    sources_rel = f"sources/{product}/"
    client = client or ForgejoClient()
    branch = "doclayer-fetch/{}-{}".format(product, datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))

    # Reset the work branch onto the PR base; the uncommitted refresh (new and
    # modified copies) carries over. Fails loudly if that would clobber.
    _git(repo, "fetch", remote, base)
    _git(repo, "checkout", "-B", branch, f"{remote}/{base}")

    changed = changed_files(repo)
    content_changes = [path for path in changed if path.startswith(sources_rel)]
    recipe_changed = recipe_rel in changed

    # `last_attempt` churn alone is not a change: reset the recipe and stop.
    # A recipe-only delta with no new copies and no error state is the same.
    if not content_changes and (not recipe_changed or "last_error: null" in recipe_path.read_text(encoding="utf-8")):
        _git(repo, "checkout", "--", recipe_rel)
        return LandingResult(action="no-changes", changed=[])

    _git(repo, "add", "--", sources_rel, recipe_rel)
    _git(repo, "commit", "-m", f"sources({product}): refresh {datetime.now(UTC).date()}")
    _git(repo, "push", "-u", remote, branch)

    owner, repo_name = parse_remote(_remote_url(repo, remote))
    committed = _git(repo, "diff", "--name-only", f"{remote}/{base}..HEAD").split()
    failed = [f"{row['slug']}: {row['result']}" for row in report_rows if row.get("result") != "ok"]
    # The PR itself only ever contains the staged paths, but a working tree
    # that also changed elsewhere means this refresh touched something outside
    # the recipe's scope — auto-merge would silently drop it.
    allowed = all(path.startswith(sources_rel) or path == recipe_rel for path in changed)
    reasons: list[str] = []
    if failed:
        reasons.append(f"junk/error pages in this refresh: {', '.join(failed)}")
    if not allowed:
        reasons.append(f"diff touches files outside {sources_rel} and {recipe_rel}")

    pr = client.create_pr(
        owner,
        repo_name,
        title=f"sources({product}): refresh {datetime.now(UTC).date()}",
        head=branch,
        base=base,
        body=(
            f"Automated refresh of `{sources_rel}` by doclayer-fetch.\n\n"
            f"Pages ok: {sum(1 for r in report_rows if r.get('result') == 'ok')}/"
            f"{len(report_rows)}." + (f"\n\n**Not auto-merged:** {'; '.join(reasons)}" if reasons else "")
        ),
    )
    number = int(pr["number"])
    url = pr.get("html_url")
    result = LandingResult(
        action="pr-created",
        changed=committed,
        pr_number=number,
        pr_url=url,
        reasons=reasons,
    )
    if reasons:
        result.action = "pr-left-open"  # the maintainer decides
    else:
        client.approve(owner, repo_name, number)
        client.merge(owner, repo_name, number)
        result.action = "pr-merged"
    return result
