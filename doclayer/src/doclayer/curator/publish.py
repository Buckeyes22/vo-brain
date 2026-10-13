"""Automatic publication of validated curator changes.

Sequence per publication attempt:

1. Take the global publication lock (one landing at a time).
2. Fetch ``origin`` and cut an isolated worktree at fresh ``origin/main`` —
   never the operator's or another tool's live checkout.
3. Re-read every destination page from that fresh state. A stale base
   (intervening operator edit) or a new-path collision goes through the
   reconciler and its checks again; a change that cannot reconcile stays
   behind while unaffected groups continue.
4. Write only validated destinations, stage the explicit allowed paths, and
   commit with the job-derived identity.
5. Push the branch, create the PR (history), merge through the documented
   admin bypass — recovering from every uncertain response by re-querying
   the remote instead of retrying blind writes. A merge is only recorded
   when the PR reports merged with a merge commit.

The durable publication identity (branch, push, commit, PR, merge) is
persisted to the job state after every step via `persist()`, so a restart or
disconnect resumes the existing attempt instead of duplicating it.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from doclayer import frontmatter as fm
from doclayer.curator import gittools
from doclayer.curator.config import CuratorConfig, stage_config
from doclayer.curator.forgejo import CuratorForgejo, ForgejoError
from doclayer.curator.jobstore import JobStore
from doclayer.curator.state import JobState, PageChange
from doclayer.curator.validate import Failure, blockers, check_path_allowed

Reconciler = Callable[[PageChange, str, bool], dict[str, Any]]
"""(change, fresh_page_text, collision) -> {"meta":…, "body":…, "notes":[…]}

collision=True means the create-path exists canonically now. The reconciler
returns resolution="distinct-subject" (plus meta/body) when the existing page
is a different subject and the draft must take a new path. Raises
ReconcileFailed when the change cannot be reconciled."""


class ReconcileFailed(RuntimeError):
    pass


ValidateFn = Callable[[PageChange, dict[str, Any] | None, str | None], list[Failure]]
"""(change, fresh_meta, fresh_body) -> failures; blockers hold the change."""


@dataclass
class PublishOutcome:
    merged: bool = False
    commit_sha: str | None = None
    merge_commit: str | None = None
    branch: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    published_changes: list[str] = field(default_factory=list)
    conflicted_changes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].strip("-") or "import"


def branch_for(job_id: str, intent: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"curator/{job_id}-{slugify(intent)}-{stamp}"


def commit_message(state: JobState, model: str) -> str:
    return "\n".join(
        [
            f"curator({state.job_id}): {state.intent[:80]}",
            "",
            f"Curated import by brain-curator (model {model}); automated validation passed.",
            f"Job: {state.job_id}",
        ]
    )


def apply_superseded(change: PageChange, rows: list[dict[str, Any]]) -> None:
    """Record draft sections reconciliation kept exactly as fresh, in the same
    in-memory update (and so the same state write) as the reconciled body that
    dropped them. Their anchored claims leave the page's obligations. A
    re-reconciliation of the same section never records it twice."""
    seen = {row.get("digest") for row in change.superseded_by_fresh}
    for row in rows:
        if row.get("digest") not in seen:
            seen.add(row.get("digest"))
            change.superseded_by_fresh.append(copy.deepcopy(row))
    dropped = {identity for row in change.superseded_by_fresh for identity in row.get("claim_ids", [])}
    change.claim_ids = [identity for identity in change.claim_ids if identity not in dropped]


def clear_superseded(change: PageChange) -> None:
    """An unpublished reconciliation supersedes nothing: restore its claims."""
    restored = [identity for row in change.superseded_by_fresh for identity in row.get("claim_ids", [])]
    change.claim_ids = list(dict.fromkeys(change.claim_ids + restored))
    change.superseded_by_fresh = []


def alternate_path(taken: set[str], path: str) -> str:
    """A free sibling path for a distinct-subject collision (-2, -3, …)."""
    stem, ext = path.removesuffix(".md"), ".md"
    for index in range(2, 100):
        candidate = f"{stem}-{index}{ext}"
        if candidate not in taken:
            return candidate
    raise ReconcileFailed(f"no free path for collision at {path}")


class Publisher:
    def __init__(
        self,
        cfg: CuratorConfig,
        store: JobStore,
        forgejo: CuratorForgejo | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.checkout = Path(cfg.paths.brain_checkout).expanduser()
        self._forgejo = forgejo

    def forgejo(self) -> CuratorForgejo:
        if self._forgejo is None:
            remote_url = gittools.remote_origin_url(self.checkout, self.cfg.publish.remote)
            self._forgejo = CuratorForgejo.from_config(self.cfg, remote_url)
        return self._forgejo

    # -- main entry ---------------------------------------------------------- #

    def recover_existing_merge(
        self,
        state: JobState,
        changes: list[PageChange],
        persist: Callable[[], None],
    ) -> PublishOutcome | None:
        """Recover a real merged receipt without running new editorial work.

        Read-only remote operations precede any new publication checks: an
        already landed commit must remain observable during model outages or
        after its model budget has been exhausted. Open PRs are never merged
        here and remain subject to the caller's current validation contract.
        """
        pub = state.publication
        if pub.pr_number is None and not (pub.branch and pub.commit_sha):
            return None
        if not pub.commit_sha:
            raise ForgejoError("existing publication lacks a recorded commit identity")
        with self.store.publish_lock():
            gittools.fetch(self.checkout, self.cfg.publish.remote)
            forgejo = self.forgejo()
            if pub.pr_number is not None:
                snapshot = forgejo.get_pr(pub.pr_number)
            else:
                matches = [
                    pr
                    for pr in forgejo.find_prs_for_head(pub.branch)
                    if pr.get("head", {}).get("sha") == pub.commit_sha
                    and pr.get("base", {}).get("ref") == self.cfg.publish.base_branch
                ]
                if not matches:
                    return None
                if len(matches) != 1:
                    raise ForgejoError("existing publication has ambiguous PR identity")
                snapshot = forgejo.get_pr(int(matches[0]["number"]))
            head = snapshot.get("head", {})
            if (
                head.get("sha") != pub.commit_sha
                or (pub.branch and head.get("ref") != pub.branch)
                or snapshot.get("base", {}).get("ref") != self.cfg.publish.base_branch
            ):
                raise ForgejoError("existing PR does not match recorded publication head/base")
            if pub.pr_number is None:
                pub.pr_number = int(snapshot["number"])
                pub.pr_url = snapshot.get("html_url")
                persist()
            if not CuratorForgejo.pr_merged(snapshot):
                return None
            outcome = PublishOutcome()
            self._record_merge(state, changes, snapshot, outcome)
            self._cleanup_worktree(state)
            persist()
            return outcome

    def publish_changes(
        self,
        state: JobState,
        changes: list[PageChange],
        reconciler: Reconciler,
        revalidate: ValidateFn,
        persist: Callable[[], None],
    ) -> PublishOutcome:
        """Land the validated changes and return the outcome. Marks published
        changes `published` and unlandable ones `conflict` (with reasons)."""
        outcome = PublishOutcome()
        pub = state.publication
        cfg = self.cfg
        base_ref = f"{cfg.publish.remote}/{cfg.publish.base_branch}"
        # Changes that already failed validation stay behind; the digest
        # reports them even though publication never touches them.
        outcome.conflicted_changes = [c.path for c in changes if c.status in ("failed", "conflict") and c.path]

        with self.store.publish_lock():
            gittools.fetch(self.checkout, cfg.publish.remote)
            try:
                if pub.merged and pub.merge_commit:
                    if pub.pr_number is None:
                        raise ForgejoError("merged publication lacks a PR identity")
                    snapshot = self.forgejo().get_pr(pub.pr_number)
                    self._record_merge(state, changes, snapshot, outcome)
                    self._cleanup_worktree(state)
                    persist()
                    return outcome
                if not (pub.branch and pub.commit_sha):
                    # Fresh application: reconcile against current main, commit.
                    branch = pub.branch or branch_for(state.job_id, state.intent)
                    pub.branch = branch
                    persist()
                    worktree = self._make_worktree(state, base_ref)
                    persist()
                    try:
                        staged = self._apply_changes(
                            state, changes, worktree, base_ref, reconciler, revalidate, outcome
                        )
                        if staged:
                            pub.commit_sha = gittools.commit_all(
                                worktree,
                                commit_message(state, stage_config(cfg, "curation").inference.model),
                                cfg.publish.author_name,
                                cfg.publish.author_email,
                                paths=[c.path for c in changes if c.status == "validated"],
                            )
                            pub.committed_paths = []
                            pub.committed_paths = self._committed_paths(state)
                            pub.pushed = False
                            persist()
                        else:
                            outcome.notes.append("no applicable changes survived reconciliation")
                            self._cleanup_worktree(state)
                            persist()
                            return outcome
                    finally:
                        pass  # worktree kept until merge verification cleans it up
                else:
                    outcome.published_changes = [c.path for c in changes if c.status in ("validated", "published")]

                branch = pub.branch or ""
                self._committed_paths(state)
                persist()
                self._ensure_pushed(state, branch, persist)
                pr = self._ensure_pr(state, branch, changes, persist)
                merged_pr = self._ensure_merged(state, pr, persist)

                # Verify the PR and exact committed subset against canonical Git.
                gittools.fetch(self.checkout, cfg.publish.remote)
                self._record_merge(state, changes, merged_pr, outcome)
                self._cleanup_worktree(state)
                persist()
                return outcome
            except (gittools.GitError, ForgejoError, ReconcileFailed):
                # Durable state already persisted per step; the worktree stays
                # for a resumed attempt unless nothing was committed yet.
                persist()
                raise

    def _committed_paths(self, state: JobState) -> list[str]:
        """Recover legacy manifests from the recorded commit, never page statuses."""
        pub = state.publication
        if not pub.commit_sha or not re.fullmatch(r"[0-9a-f]{40,64}", pub.commit_sha):
            raise ReconcileFailed("publication manifest lacks a valid commit identity")
        paths = sorted(
            p
            for p in gittools.git(
                self.checkout,
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-only",
                "-r",
                "-z",
                pub.commit_sha,
            ).split("\0")
            if p
        )
        if not paths or any(check_path_allowed(p, self.cfg) for p in paths):
            raise ReconcileFailed("publication manifest contains no changes or disallowed paths")
        if pub.committed_paths and sorted(pub.committed_paths) != paths:
            raise ReconcileFailed("publication manifest does not match recorded commit")
        pub.committed_paths = paths
        return paths

    def _record_merge(
        self,
        state: JobState,
        changes: list[PageChange],
        pr: dict[str, Any],
        outcome: PublishOutcome,
    ) -> None:
        pub = state.publication
        head, base = pr.get("head"), pr.get("base")
        if (
            not isinstance(head, dict)
            or not isinstance(base, dict)
            or head.get("sha") != pub.commit_sha
            or (pub.branch and head.get("ref") != pub.branch)
            or base.get("ref") != self.cfg.publish.base_branch
            or (pub.pr_number is not None and pr.get("number") != pub.pr_number)
        ):
            raise ForgejoError("merged PR does not match recorded publication head/base")
        merge_sha = CuratorForgejo.merge_commit(pr)
        if not CuratorForgejo.pr_merged(pr) or not merge_sha:
            raise ForgejoError("PR is not verified merged with a merge commit")
        if pub.merge_commit and pub.merge_commit != merge_sha:
            raise ForgejoError("merged PR does not match recorded merge identity")
        paths = self._committed_paths(state)
        base_ref = f"{self.cfg.publish.remote}/{self.cfg.publish.base_branch}"
        if not gittools.is_ancestor(self.checkout, merge_sha, base_ref) or not gittools.is_ancestor(
            self.checkout,
            pub.commit_sha,
            merge_sha,
        ):
            raise ForgejoError("merged publication commit not verified in canonical Git")
        pub.merged = True
        pub.merge_commit = merge_sha
        for change in changes + state.changes:
            if change.path in paths:
                change.status = "published"
        outcome.merged = True
        outcome.merge_commit = merge_sha
        outcome.commit_sha = pub.commit_sha
        outcome.branch = pub.branch
        outcome.pr_number = pub.pr_number
        outcome.pr_url = pub.pr_url
        outcome.published_changes = paths

    # -- steps ------------------------------------------------------------ #

    def _make_worktree(self, state: JobState, base_ref: str) -> Path:
        stale = state.publication.worktree
        if stale and Path(stale).exists():
            self._cleanup_worktree(state)  # a previous attempt's worktree
        path = Path(self.cfg.paths.worktrees).expanduser() / f"{state.job_id}-{datetime.now(UTC).strftime('%H%M%S%f')}"
        gittools.add_worktree(self.checkout, path, base_ref, detach=True)
        state.publication.worktree = str(path)
        return path

    def _cleanup_worktree(self, state: JobState) -> None:
        path = state.publication.worktree
        if path and Path(path).exists():
            try:
                gittools.remove_worktree(self.checkout, Path(path), force=True)
            except gittools.GitError:
                pass  # leftover worktrees are handled by deploy maintenance
        state.publication.worktree = None

    def _apply_changes(
        self,
        state: JobState,
        changes: list[PageChange],
        worktree: Path,
        base_ref: str,
        reconciler: Reconciler,
        revalidate: ValidateFn,
        outcome: PublishOutcome,
    ) -> bool:
        """Reconcile + write validated changes into the worktree. Two passes:
        reconcile every change first (collisions may rename paths and require
        link rewrites in sibling changes), then write the files. Returns True
        when at least one change is staged for commit."""
        renames: dict[str, str] = {}
        for change in list(changes):
            if change.status != "validated":
                continue
            fresh = gittools.read_blob(worktree, "HEAD", change.path)
            fresh_hash = gittools.blob_hash(worktree, f"HEAD:{change.path}")
            collision = change.action == "create" and fresh is not None
            if change.action == "update" and fresh is None:
                change.status = "conflict"
                change.error = f"{change.path} does not exist on {base_ref}; manual reconciliation needed"
                outcome.conflicted_changes.append(change.path)
                continue
            if collision or (fresh is not None and fresh_hash and fresh_hash != change.base_blob):
                reason = (
                    "new-path collision with existing canonical page"
                    if collision
                    else "canonical page changed since drafting"
                )
                try:
                    merged = reconciler(change, fresh or "", collision)
                except ReconcileFailed as exc:
                    clear_superseded(change)
                    change.status = "conflict"
                    change.error = f"{reason}: {exc}"
                    outcome.conflicted_changes.append(change.path)
                    continue
                if collision and merged.get("resolution") == "distinct-subject":
                    taken = {c.path for c in changes} | set(gittools.tracked_paths(worktree, "HEAD"))
                    new_path = alternate_path(taken, change.path)
                    renames[change.path] = new_path
                    change.notes.append(
                        f"collision at {change.path}: distinct subject, took {new_path} "
                        f"({merged.get('explanation', '')[:150]})"
                    )
                    change.path = new_path
                    change.action = "create"
                else:
                    change.draft_meta = merged.get("meta") or change.draft_meta
                    change.draft_body = merged.get("body") or change.draft_body
                    # The owning reconciler returns proof bound to its assembled
                    # body; old editorial task/check completion must not survive.
                    if "draft_progress" in merged:
                        change.draft_progress = merged["draft_progress"]
                        change.check_progress = merged.get("check_progress") or {}
                    if "reconcile_progress" in merged:
                        change.reconcile_progress = merged["reconcile_progress"]
                    if collision:
                        change.action = "update"
                    apply_superseded(change, merged.get("superseded") or [])
                    change.notes.append(f"reconciled at publish: {reason}")
                change.notes.extend(merged.get("notes", []))
                existing_meta: dict[str, Any] | None = None
                existing_body: str | None = None
                if fresh:
                    existing_meta, existing_body = fm.parse(fresh)
                failures = blockers(revalidate(change, existing_meta, existing_body))
                if failures:
                    clear_superseded(change)
                    change.status = "conflict"
                    change.error = f"{reason}: reconciled draft failed revalidation: " + "; ".join(
                        f"{f.check}: {f.detail[:120]}" for f in failures[:3]
                    )
                    outcome.conflicted_changes.append(change.path)
                    continue
        if renames:
            self._rewrite_links_for_renames(changes, renames)
        staged_any = False
        for change in changes:
            if change.status != "validated":
                continue
            # Stage the exact allowed path (defense in depth: re-checked here).
            path_failure = check_path_allowed(change.path, self.cfg)
            if path_failure:
                clear_superseded(change)
                change.status = "conflict"
                change.error = f"destination rejected at staging: {path_failure.detail}"
                outcome.conflicted_changes.append(change.path)
                continue
            target = worktree / change.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(fm.build(change.draft_meta, change.draft_body), encoding="utf-8")
            staged_any = True
        return staged_any

    def _rewrite_links_for_renames(self, changes: list[PageChange], renames: dict[str, str]) -> None:
        for old, new in renames.items():
            stems = {f"[[{old}]]": f"[[{new}]]"}
            if old.endswith(".md"):
                stems[f"[[{old[:-3]}]]"] = f"[[{new[:-3]}]]"
            for change in changes:
                for old_link, new_link in stems.items():
                    change.draft_body = change.draft_body.replace(old_link, new_link)
                change.related = [new if r == old else r for r in change.related]

    # -- remote steps, each recovering from uncertain outcomes ------------- #

    def _ensure_pushed(self, state: JobState, branch: str, persist: Callable[[], None]) -> None:
        pub = state.publication
        if pub.pushed:
            return
        worktree = pub.worktree
        try:
            if worktree and Path(worktree).exists():
                gittools.git(
                    Path(worktree),
                    "push",
                    "-u",
                    self.cfg.publish.remote,
                    f"HEAD:refs/heads/{branch}",
                    timeout=300,
                )
            else:
                # Recovered session: push the recorded commit from the checkout.
                gittools.git(
                    self.checkout,
                    "push",
                    self.cfg.publish.remote,
                    f"{pub.commit_sha}:refs/heads/{branch}",
                    timeout=300,
                )
        except gittools.GitError as exc:
            # Uncertain: the push may have landed anyway. Query the remote.
            if self.forgejo().branch_head(branch) == pub.commit_sha:
                pub.pushed = True
                persist()
                pub.attempts.append({"step": "push", "result": "landed-unknown", "error": str(exc)[:200]})
                return
            pub.attempts.append({"step": "push", "result": "failed", "error": str(exc)[:200]})
            raise
        pub.pushed = True
        persist()

    def _ensure_pr(
        self,
        state: JobState,
        branch: str,
        changes: list[PageChange],
        persist: Callable[[], None],
    ) -> dict[str, Any]:
        pub = state.publication
        forgejo = self.forgejo()
        if pub.pr_number:
            try:
                return forgejo.get_pr(pub.pr_number)
            except ForgejoError:
                pass  # fall through to discovery below
        existing = forgejo.find_prs_for_head(branch)
        if existing:
            pr = existing[0]
            pub.pr_number = int(pr["number"])
            pub.pr_url = pr.get("html_url")
            persist()
            return pr
        page_list = ", ".join(f"`{c.path}`" for c in changes if c.status in ("validated", "published"))
        pr = forgejo.create_pr(
            title=f"curator({state.job_id}): {state.intent[:70]}",
            head=branch,
            base=self.cfg.publish.base_branch,
            body=(
                f"Automated curated import by brain-curator (model `{stage_config(self.cfg, 'curation').inference.model}`).\n\n"
                f"Job: `{state.job_id}` · intent: {state.intent}\n"
                f"Pages: {page_list or '(see commit)'}\n"
                f"Coverage: {state.coverage}"
                + (f"; gaps: {'; '.join(state.coverage_gaps[:5])}" if state.coverage_gaps else "")
            ),
        )
        pub.pr_number = int(pr["number"])
        pub.pr_url = pr.get("html_url")
        persist()
        return pr

    def _ensure_merged(
        self,
        state: JobState,
        pr: dict[str, Any],
        persist: Callable[[], None],
    ) -> dict[str, Any]:
        pub = state.publication
        forgejo = self.forgejo()

        def current() -> dict[str, Any]:
            number = int(pr["number"])
            pub.pr_number = number
            return forgejo.get_pr(number)

        try:
            snapshot = current()
            if CuratorForgejo.pr_merged(snapshot):
                persist()
                return snapshot
            merged = forgejo.merge_pr(int(pr["number"]), expected_head=pub.commit_sha)
            pub.attempts.append({"step": "merge", "result": "merged"})
            persist()
            return merged
        except ForgejoError as exc:
            # Uncertain: inspect the PR's actual state before any retry.
            try:
                snapshot = current()
            except ForgejoError:
                pub.attempts.append({"step": "merge", "result": "uncertain", "error": str(exc)[:200]})
                raise
            if CuratorForgejo.pr_merged(snapshot):
                pub.attempts.append({"step": "merge", "result": "landed-unknown"})
                persist()
                return snapshot
            pub.attempts.append({"step": "merge", "result": "failed", "error": str(exc)[:200]})
            raise
