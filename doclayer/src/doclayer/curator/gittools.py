"""Git plumbing the curator needs: revisions, blobs, worktrees, commits.

Every git invocation uses an argument array (no shell interpolation).
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path


class GitError(RuntimeError):
    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr or message


def git(repo: Path, *args: str, timeout: int = 120, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])}: {proc.stderr.strip()[:300]}", proc.stderr)
    return proc.stdout


def git_ok(repo: Path, *args: str, timeout: int = 120) -> bool:
    try:
        git(repo, *args, timeout=timeout, check=True)
        return True
    except GitError:
        return False


def rev_parse(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", ref).strip()


_FETCH_LOCKS: dict[str, threading.Lock] = {}
_FETCH_LOCKS_GUARD = threading.Lock()


#: A ref another writer holds locked at this moment (git's wording differs by
#: version and ref backend). Only these fetch failures are retried.
_REF_LOCK_CONTENTION = re.compile(
    r"cannot lock ref|unable to update local ref|Unable to create '[^']*\.lock'"
    r"|\.lock'?: File exists|fetching ref \S+ failed",
    re.IGNORECASE,
)
#: Waits between fetch attempts that met a held ref lock (about 8 s in all).
FETCH_LOCK_RETRY_DELAYS_S: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0)


def fetch(repo: Path, remote: str = "origin", timeout: int = 300) -> None:
    # The worker runs several jobs in threads against one checkout; two
    # simultaneous `fetch --prune` can lose a ref-lock race, so fetches of the
    # same checkout take turns. Publication's push and worktree ref writes run
    # under the store's publish lock, which a place-stage fetch does not take
    # (publication itself fetches inside it); a fetch that meets their ref lock
    # waits briefly and retries instead of failing the job.
    key = str(Path(repo).expanduser().resolve())
    with _FETCH_LOCKS_GUARD:
        lock = _FETCH_LOCKS.setdefault(key, threading.Lock())
    with lock:
        for delay in (*FETCH_LOCK_RETRY_DELAYS_S, None):
            try:
                git(repo, "fetch", "--prune", remote, timeout=timeout)
                return
            except GitError as exc:
                if delay is None or not _REF_LOCK_CONTENTION.search(exc.stderr):
                    raise
            time.sleep(delay)


def remote_origin_url(repo: Path, remote: str = "origin") -> str:
    return git(repo, "remote", "get-url", remote).strip()


def blob_hash(repo: Path, rev_path: str) -> str:
    """Blob hash of path at rev ("rev:path"), or "" when absent."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", rev_path],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def read_blob(repo: Path, rev: str, path: str) -> str | None:
    """File content at rev, or None when the file does not exist there."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "show", f"{rev}:{path}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def tracked_paths(repo: Path, rev: str) -> list[str]:
    out = git(repo, "ls-tree", "-r", "--name-only", "-z", rev)
    return sorted(p for p in out.split("\0") if p)


def add_worktree(repo: Path, worktree_path: Path, ref: str, detach: bool = True) -> Path:
    """Create an isolated worktree. Caller removes it when done."""
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    args = ["worktree", "add"]
    if detach:
        args.append("--detach")
    args += [str(worktree_path), ref]
    git(repo, *args, timeout=300)
    return worktree_path


def remove_worktree(repo: Path, worktree_path: Path, force: bool = False) -> None:
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(worktree_path))
    git(repo, *args, timeout=120)


def commit_all(
    repo: Path,
    message: str,
    author_name: str,
    author_email: str,
    paths: list[str] | None = None,
) -> str:
    """Stage the explicit paths (or all modifications when None) and commit.

    The author/committer identity is the job-derived curator identity; the
    caller records the commit sha for the durable publication evidence.
    """
    if paths is not None:
        for path in paths:
            git(repo, "add", "--", path)
    else:
        git(repo, "add", "-A")
    env_name = ("-c", f"user.name={author_name}")
    env_email = ("-c", f"user.email={author_email}")
    git(repo, *env_name, *env_email, "commit", "-m", message)
    return rev_parse(repo, "HEAD")


def has_staged_changes(repo: Path) -> bool:
    out = git(repo, "status", "--porcelain")
    return bool(out.strip())


def is_ancestor(repo: Path, maybe_ancestor: str, descendant: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", maybe_ancestor, descendant],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc.returncode == 0
