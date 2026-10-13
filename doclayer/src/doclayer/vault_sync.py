"""Publisher-independent, conservative fast-forward of explicitly registered consumers.

Never discovers development checkouts, stashes edits, commits logs, or resets history.
Git's own index lock serializes the actual update with non-cooperating Git callers;
our common-directory lock serializes periodic/manual invocations across worktrees.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


class SyncError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=120, env=env)
    except subprocess.TimeoutExpired:
        raise SyncError(f"git {args[0]} timed out") from None
    if proc.returncode:
        # Remote URLs and credential helpers can echo credentials: never persist stderr.
        raise SyncError(f"git {args[0]} failed (exit {proc.returncode}); inspect privately")
    return proc.stdout.decode("utf-8", errors="surrogateescape").rstrip("\n")


def _normalized_remote(value: str) -> str:
    return value.rstrip("/").removesuffix(".git")


def _network_remote(value: str) -> tuple[str, str, str, int] | None:
    # Git also supports scp-style SSH remotes. Local/file paths are compared
    # exactly, never promoted to a network identity by a URL rewrite.
    if "://" not in value:
        match = re.fullmatch(r"(?:[^/@:]+@)?([^/:]+):(.+)", value)
        if not match:
            return None
        value = f"ssh://{match[1]}/{match[2]}"
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("https", "ssh") or not parsed.hostname or parsed.query or parsed.fragment:
            return None
        path = _normalized_remote(parsed.path).lstrip("/")
        if not path or any(part in (".", "..") for part in path.split("/")):
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 22)
        return parsed.scheme, parsed.hostname.lower(), path, port
    except ValueError:
        return None


def validate_origin(repo: Path, expected_remote: str) -> None:
    """Require the registered declaration and a same-repository effective route.

    Respect ordinary Git insteadOf authentication routes without trusting an
    arbitrary rewritten server. Cross HTTPS/SSH routes may use different ports;
    within one protocol the port remains part of the server identity.
    """
    configured = git(repo, "config", "--get-all", "remote.origin.url").splitlines()
    if len(configured) != 1 or _normalized_remote(configured[0]) != _normalized_remote(expected_remote):
        raise SyncError("origin differs from registered canonical remote")
    effective = git(repo, "remote", "get-url", "origin")
    if _normalized_remote(effective) == _normalized_remote(expected_remote):
        return
    declared_identity = _network_remote(expected_remote)
    effective_identity = _network_remote(effective)
    if declared_identity is None or effective_identity is None:
        raise SyncError("effective origin rewrite differs from registered canonical remote")
    protocol, host, path, port = declared_identity
    route_protocol, route_host, route_path, route_port = effective_identity
    if host != route_host or path != route_path or (protocol == route_protocol and port != route_port):
        raise SyncError("effective origin rewrite differs from registered canonical remote")


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(data, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def dirty_paths(repo: Path) -> list[str]:
    modified = git(repo, "diff", "--name-only", "-z").split("\0")
    modified += git(repo, "diff", "--cached", "--name-only", "-z", "HEAD").split("\0")
    extra = git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    return sorted(set(filter(None, modified + extra)))


def fingerprint(path: Path) -> dict:
    if not path.exists() and not path.is_symlink():
        return {"type": "absent"}
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return {"type": "symlink", "target": os.readlink(path), "mode": info.st_mode}
    if not stat.S_ISREG(info.st_mode):
        raise SyncError("non-file local change requires operator handling")
    return {"type": "file", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mode": info.st_mode}


def overlaps(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def sync_consumer(repo: Path, state_path: Path, *, expected_remote: str, branch: str = "main") -> dict:
    result = {
        "repo": str(repo),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "error",
        "local_commit": None,
        "canonical_commit": None,
        "lag_commits": None,
        "error": None,
    }
    try:
        old_state = json.loads(state_path.read_text()) if state_path.exists() else {}
    except (OSError, ValueError):
        old_state = {}  # An interrupted/obsolete status file must not disable reconciliation.
    result["last_success_at"] = old_state.get("last_success_at")
    try:
        repo = repo.resolve(strict=True)
        common = Path(git(repo, "rev-parse", "--git-common-dir"))
        if not common.is_absolute():
            common = repo / common
        with (common / "brain-vault-sync.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SyncError("another vault reconciliation holds the lock") from None
            result["local_commit"] = git(repo, "rev-parse", "HEAD")
            if git(repo, "symbolic-ref", "--short", "HEAD") != branch:
                raise SyncError(f"consumer must remain on {branch}; no checkout performed")
            validate_origin(repo, expected_remote)
            gitdir = Path(git(repo, "rev-parse", "--absolute-git-dir"))
            if any(
                (gitdir / name).exists()
                for name in (
                    "MERGE_HEAD",
                    "CHERRY_PICK_HEAD",
                    "REVERT_HEAD",
                    "rebase-merge",
                    "rebase-apply",
                    "index.lock",
                )
            ):
                raise SyncError("Git operation in progress; retry after it finishes")
            git(repo, "fetch", "--no-tags", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
            target = git(repo, "rev-parse", f"origin/{branch}")
            result["canonical_commit"] = target
            result["lag_commits"] = int(git(repo, "rev-list", "--count", f"HEAD..{target}"))
            try:
                git(repo, "merge-base", "--is-ancestor", "HEAD", target)
            except SyncError:
                raise SyncError("local history diverges or is ahead; reconcile manually without reset") from None
            if result["local_commit"] != target:
                dirty = dirty_paths(repo)
                changed = list(filter(None, git(repo, "diff", "--name-only", "-z", "HEAD", target).split("\0")))
                conflicts = [path for path in dirty if any(overlaps(path, other) for other in changed)]
                if conflicts:
                    result["conflicting_paths"] = conflicts
                    raise SyncError("local changes overlap canonical update; resolve these paths manually")
                before = {path: fingerprint(repo / path) for path in dirty}
                index_before = {path: git(repo, "ls-files", "--stage", "-z", "--", path) for path in dirty}
                backup = state_path.parent / "backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
                backup.mkdir(parents=True, mode=0o700)
                # Save original index/staged state, HEAD and exact modified/untracked bytes.
                shutil.copy2(gitdir / "index", backup / "index")
                os.chmod(backup / "index", 0o600)
                for path, meta in before.items():
                    if meta["type"] != "absent":
                        dest = backup / "files" / path
                        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        shutil.copy2(repo / path, dest, follow_symlinks=False)
                        if meta["type"] == "file":
                            os.chmod(dest, 0o600)
                            if fingerprint(dest)["sha256"] != meta["sha256"]:
                                raise SyncError("local file changed during backup; retry without modifying the vault")
                        elif os.readlink(dest) != meta["target"]:
                            raise SyncError("local symlink changed during backup; retry without modifying the vault")
                # Keep pre-update history reachable even after future Git garbage collection.
                backup_ref = "refs/brain-vault-sync/backups/" + backup.name
                git(repo, "update-ref", backup_ref, result["local_commit"])
                atomic_json(
                    backup / "manifest.json",
                    {
                        "head": result["local_commit"],
                        "backup_ref": backup_ref,
                        "files": before,
                        "index_entries": index_before,
                    },
                )
                if any(fingerprint(repo / path) != meta for path, meta in before.items()):
                    raise SyncError("local edits changed while backing up; retry without modifying the vault")
                result["backup_path"] = str(backup)
                git(repo, "merge", "--ff-only", "--no-overwrite-ignore", target)
                result["local_commit"] = git(repo, "rev-parse", "HEAD")
                if result["local_commit"] != target:
                    raise SyncError("consumer revision changed concurrently; retry after inspecting state")
                if any(fingerprint(repo / path) != meta for path, meta in before.items()):
                    raise SyncError("local edits changed during update; preserved backup requires inspection")
                if any(
                    git(repo, "ls-files", "--stage", "-z", "--", path) != entry for path, entry in index_before.items()
                ):
                    raise SyncError("staged edits changed during update; preserved index backup requires inspection")
                result["preserved_paths"] = dirty
            result["lag_commits"] = 0
            result["status"] = "current"
            result["last_success_at"] = result["checked_at"]
    except (SyncError, OSError, ValueError) as exc:
        result["error"] = str(exc)
    try:
        atomic_json(state_path, result)
    except OSError:
        result["status"] = "error"
        result["error"] = "cannot persist consumer status; inspect state directory permissions/storage"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    results = []
    for consumer in config["consumers"]:
        state = Path(consumer["state"])
        if args.status:
            results.append(
                json.loads(state.read_text()) if state.exists() else {"status": "not_run", "repo": consumer["repo"]}
            )
        else:
            results.append(
                sync_consumer(
                    Path(consumer["repo"]),
                    state,
                    expected_remote=consumer["remote"],
                    branch=consumer.get("branch", "main"),
                )
            )
    print(json.dumps({"consumers": results}, indent=2))
    raise SystemExit(0 if all(item["status"] == "current" for item in results) else 1)


if __name__ == "__main__":
    main()
