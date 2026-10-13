"""Forgejo API calls for curator publication.

Distinct from the source-fetcher's landing helper on purpose: no branch
resets, no self-approval attempts, no sources-only scope assumptions. This
client only does what unattended publication needs:

- create a PR (for history),
- find existing PRs for a head branch (recovery: never open a duplicate),
- read a PR's merged state and merge commit,
- merge with Forgejo's admin ``force_merge`` override when
  ``publish.force_merge`` is enabled (Forgejo forbids self-approval),
- check whether a branch exists remotely.

Every uncertain network failure raises; the publication code recovers by
re-querying this client rather than retrying blind writes.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import quote

import httpx

from doclayer.curator.config import CuratorConfig
from doclayer.landing import parse_remote

_TIMEOUT_S = 30.0


class ForgejoError(RuntimeError):
    pass


class PublishNotConfigured(ValueError):
    """`publish.forgejo_url` is empty: a configuration error, never retried."""


class CuratorForgejo:
    """Token-authenticated Forgejo API for one owner/repo."""

    def __init__(
        self,
        owner: str,
        repo: str,
        *,
        base_url: str | None = None,
        token: str | None = None,
        force_merge: bool = False,
        base_branch: str = "main",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.owner = owner
        self.repo = repo
        if not base_url:
            raise PublishNotConfigured("publish.forgejo_url is not configured")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.force_merge = force_merge
        self.base_branch = base_branch
        self._transport = transport

    @classmethod
    def from_config(cls, cfg: CuratorConfig, remote_url: str, token: str | None = None) -> CuratorForgejo:
        owner, repo = parse_remote(remote_url)
        if token is None:
            token = forgejo_token_from(cfg)
        return cls(
            owner,
            repo,
            base_url=cfg.publish.forgejo_url,
            token=token,
            force_merge=cfg.publish.force_merge,
            base_branch=cfg.publish.base_branch,
        )

    # -- plumbing ---------------------------------------------------------- #

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise ForgejoError("no Forgejo token available; publication cannot proceed unattended")
        return {"Authorization": f"token {self.token}"}

    def _call(self, method: str, path: str, json_body: dict[str, Any] | None = None) -> Any:
        try:
            with httpx.Client(transport=self._transport, timeout=_TIMEOUT_S) as client:
                response = client.request(
                    method,
                    f"{self.base_url}/api/v1{path}",
                    json=json_body,
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise ForgejoError(f"forgejo {method} {path}: {type(exc).__name__}") from exc
        if response.status_code >= 300:
            raise ForgejoError(f"forgejo {method} {path}: HTTP {response.status_code}: {response.text[:200]}")
        try:
            return response.json() if response.content else {}
        except ValueError as exc:
            raise ForgejoError("Forgejo returned malformed JSON") from exc

    # -- operations ---------------------------------------------------------- #

    def branch_exists(self, branch: str) -> bool:
        try:
            self._call("GET", f"/repos/{self.owner}/{self.repo}/branches/{branch}")
            return True
        except ForgejoError as exc:
            if "404" in str(exc):
                return False
            raise

    def create_pr(self, *, title: str, head: str, base: str, body: str) -> dict[str, Any]:
        return self._call(
            "POST",
            f"/repos/{self.owner}/{self.repo}/pulls",
            {"title": title, "head": head, "base": base, "body": body},
        )

    def get_pr(self, number: int) -> dict[str, Any]:
        return self._call("GET", f"/repos/{self.owner}/{self.repo}/pulls/{number}")

    def branch_head(self, branch: str) -> str | None:
        """Return actual remote commit identity, or a confirmed missing branch."""
        try:
            data = self._call("GET", f"/repos/{self.owner}/{self.repo}/branches/{quote(branch, safe='')}")
        except ForgejoError as exc:
            if ": HTTP 404:" in str(exc):
                return None
            raise
        sha = data.get("commit", {}).get("id") if isinstance(data, dict) else None
        if not isinstance(sha, str) or not sha:
            raise ForgejoError("branch lacks a readable commit identity")
        return sha

    def find_prs_for_head(self, head: str) -> list[dict[str, Any]]:
        """All PRs (any state), including older uncertain-create receipts."""
        found: dict[int, dict[str, Any]] = {}
        seen: set[int] = set()
        page = 1
        while True:
            data = self._call(
                "GET",
                f"/repos/{self.owner}/{self.repo}/pulls?state=all&sort=recentupdated&type=pulls&limit=20&page={page}",
            )
            if not isinstance(data, list):
                raise ForgejoError("malformed PR discovery page")
            ids: set[int] = set()
            for pr in data:
                number = pr.get("number") if isinstance(pr, dict) else None
                if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
                    raise ForgejoError("malformed PR discovery identity")
                ids.add(number)
                if pr.get("head", {}).get("ref") == head:
                    found[number] = pr
            if data and ids <= seen:
                raise ForgejoError("PR discovery pagination did not advance")
            seen.update(ids)
            if len(data) < 20:
                return list(found.values())
            page += 1

    def _required_checks(self) -> tuple[bool, tuple[str, ...]]:
        """Use the server's effective rule, rather than guessing glob precedence."""
        branch = self._call(
            "GET",
            f"/repos/{self.owner}/{self.repo}/branches/{quote(self.base_branch, safe='')}",
        )
        if not isinstance(branch, dict) or not isinstance(branch.get("protected"), bool):
            raise ForgejoError("cannot establish effective branch protection")
        enabled = branch.get("enable_status_check")
        if not isinstance(enabled, bool):
            raise ForgejoError("cannot establish required status-check policy")
        contexts = branch.get("status_check_contexts") or []
        if not isinstance(contexts, list) or any(not isinstance(c, str) or not c for c in contexts):
            raise ForgejoError("malformed required status-check contexts")
        if enabled and not contexts:
            raise ForgejoError("status checks enabled without readable required contexts")
        return enabled, tuple(contexts) if enabled else ()

    def _check_statuses(self, sha: str, required: tuple[str, ...]) -> None:
        latest: dict[str, dict[str, Any]] = {}
        page = 1
        seen_ids: set[int] = set()
        while True:
            data = self._call(
                "GET",
                f"/repos/{self.owner}/{self.repo}/commits/{sha}/status?page={page}&limit=50",
            )
            if not isinstance(data, dict) or not isinstance(data.get("statuses"), list):
                raise ForgejoError("cannot establish commit status evidence for validated head")
            rows = data["statuses"]
            # Forgejo returns an empty SHA on the terminal empty page. It is
            # pagination exhaustion, not evidence for another commit. Only a
            # later empty page after verified head-bound rows can use it.
            empty_terminal = page > 1 and bool(seen_ids) and not rows and data.get("sha") == ""
            if empty_terminal and "total_count" in data:
                count = data["total_count"]
                if type(count) is not int or count < 0 or count > len(seen_ids):
                    raise ForgejoError("cannot establish terminal commit status pagination")
            if data.get("sha") != sha and not empty_terminal:
                raise ForgejoError("cannot establish commit status evidence for validated head")
            if not rows:
                break
            new_ids = set()
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("context"), str) or not row["context"]:
                    raise ForgejoError("malformed commit status context")
                sid = row.get("id")
                if not isinstance(sid, int) or isinstance(sid, bool):
                    raise ForgejoError("malformed commit status identity")
                new_ids.add(sid)
                old = latest.get(row["context"])
                if old is None or sid > old["id"]:
                    latest[row["context"]] = row
            if new_ids <= seen_ids:
                raise ForgejoError("commit status pagination did not advance")
            seen_ids.update(new_ids)
            page += 1
        for pattern in required:
            matching = [row for context, row in latest.items() if fnmatchcase(context, pattern)]
            if not matching or any(row.get("status") != "success" for row in matching):
                raise ForgejoError(f"required CI check {pattern!r} is missing or not successful")

    def merge_pr(self, number: int, *, expected_head: str | None = None) -> dict[str, Any]:
        pr = self.get_pr(number)
        if self.pr_merged(pr):
            return pr
        sha = (pr.get("head") or {}).get("sha")
        if not isinstance(sha, str) or not sha or (expected_head and sha != expected_head):
            raise ForgejoError("PR head changed or lacks a validated commit identity")
        if (pr.get("base") or {}).get("ref") != self.base_branch:
            raise ForgejoError("PR targets an unexpected base branch")
        policy = self._required_checks()
        if policy[0]:
            self._check_statuses(sha, policy[1])
        # Recheck policy and PR immediately before the guarded server-side write.
        if self._required_checks() != policy:
            raise ForgejoError("required CI policy changed during validation")
        current = self.get_pr(number)
        if self.pr_merged(current):
            return current
        if (current.get("head") or {}).get("sha") != sha or (current.get("base") or {}).get("ref") != self.base_branch:
            raise ForgejoError("PR head or base changed during CI verification")
        body: dict[str, Any] = {"Do": "merge", "head_commit_id": sha}
        if self.force_merge:
            body["force_merge"] = True
        self._call("POST", f"/repos/{self.owner}/{self.repo}/pulls/{number}/merge", body)
        return self.get_pr(number)

    # -- recovery helpers ------------------------------------------------ #

    @staticmethod
    def pr_merged(pr: dict[str, Any]) -> bool:
        return bool(pr.get("merged")) or pr.get("state") == "closed" and bool(pr.get("merge_commit_sha"))

    @staticmethod
    def merge_commit(pr: dict[str, Any]) -> str | None:
        return pr.get("merge_commit_sha") or None


def forgejo_token_from(cfg: CuratorConfig) -> str | None:
    """The configured token file's contents, or None. Never logged."""
    from pathlib import Path

    try:
        return Path(cfg.publish.forgejo_token_file).expanduser().read_text(encoding="utf-8").strip() or None
    except OSError:
        return None
