#!/usr/bin/env python3
"""Install the explicit doclayer reference consumer without touching developer trees."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from doclayer.vault_sync import SyncError, validate_origin

REMOTE = "https://git.example.org/example/brain"
EXECUTABLE = Path.home() / "git/brain/doclayer/.venv/bin/brain-vault-sync"


def run(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        # Git may echo credentials in a remote URL: never relay subprocess output.
        raise RuntimeError(f"{args[0]} failed (exit {result.returncode}); inspect the local service/tool privately")
    return result.stdout.strip()


def atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(
    home: Path, artifacts: Path, *, remote: str = REMOTE, executable: Path = EXECUTABLE, activate: bool = True
) -> Path:
    home = home.resolve()
    repo = home / ".local/share/doclayer/brain-reference"
    pointer = home / ".config/doclayer/brain-repo"
    registry = home / ".config/brain-vault-sync/consumers.json"
    state = home / ".local/state/brain-vault-sync"
    unit_dir = home / ".config/systemd/user"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError(f"installed sync executable missing: {executable}")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "workstation-install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = json.loads(registry.read_text()) if registry.exists() else {"consumers": []}
        if not isinstance(config, dict) or not isinstance(config.get("consumers"), list):
            raise ValueError(f"invalid consumer registry: {registry}")
        for entry in config["consumers"]:
            if not isinstance(entry, dict):
                raise ValueError(f"invalid consumer registration: {registry}")
            if entry.get("repo") == str(repo) and (entry.get("remote") != remote or entry.get("branch") != "main"):
                raise ValueError("existing reference registration has a different owner/remote/branch")
        if repo.exists() or repo.is_symlink():
            if repo.is_symlink() or not repo.is_dir() or not (repo / ".git").is_dir():
                raise ValueError(f"refusing unrelated existing reference path: {repo}")
            try:
                validate_origin(repo, remote)
            except SyncError as exc:
                raise ValueError("existing reference has an unrelated origin or effective rewrite") from exc
            if run("git", "-C", str(repo), "branch", "--show-current") != "main":
                raise ValueError("existing reference is not on main")
        else:
            repo.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = Path(tempfile.mkdtemp(prefix="brain-reference-", dir=repo.parent))
            try:
                run("git", "clone", "--branch", "main", "--single-branch", remote, str(temporary))
                temporary.rename(repo)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        validate_origin(repo, remote)
        # Preserve config, unit metadata and private job status before any replacement.
        backup = state / "deployment-backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup.mkdir(parents=True, mode=0o700)
        for name, path in [
            ("brain-repo", pointer),
            ("consumers.json", registry),
            ("brain-vault-sync.service", unit_dir / "brain-vault-sync.service"),
            ("brain-vault-sync.timer", unit_dir / "brain-vault-sync.timer"),
        ]:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError(f"refusing non-file installation target: {path}")
            if path.exists():
                shutil.copy2(path, backup / name)
        if activate:
            for action in ("is-enabled", "is-active"):
                previous = subprocess.run(
                    ["systemctl", "--user", action, "brain-vault-sync.timer"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                atomic(backup / f"previous-{action}.txt", previous.stdout.strip() + "\n")
        for path in state.glob("*.json"):
            shutil.copy2(path, backup / ("state-" + path.name))
        consumer = {
            "repo": str(repo),
            "remote": remote,
            "branch": "main",
            "state": str(state / "doclayer-reference.json"),
        }
        if not any(item.get("repo") == str(repo) for item in config["consumers"]):
            config["consumers"].append(consumer)
        atomic(registry, json.dumps(config, indent=2) + "\n")
        service = (artifacts / "brain-vault-sync.service").read_text()
        service = service.replace("%h/git/brain-curator/doclayer/.venv/bin/brain-vault-sync", str(executable))
        service = service.replace("%h/.config/brain-vault-sync/consumers.json", str(registry))
        # The workstation's established Git/SSH route is verified to fail in
        # systemd's private /tmp namespace. Keep its existing authentication
        # environment accessible; retain the other unit hardening unchanged.
        service = service.replace("PrivateTmp=true", "PrivateTmp=false")
        atomic(unit_dir / "brain-vault-sync.service", service)
        atomic(unit_dir / "brain-vault-sync.timer", (artifacts / "brain-vault-sync.timer").read_text())
        # Prove initial normal-auth reconciliation before activating the read pointer.
        initial = backup / "initial-reference.json"
        atomic(initial, json.dumps({"consumers": [consumer]}))
        run(str(executable), "--config", str(initial))
        atomic(pointer, str(repo) + "\n")
        if activate:
            run("systemctl", "--user", "daemon-reload")
            run("systemctl", "--user", "enable", "--now", "brain-vault-sync.timer")
        return backup


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    backup = install(Path.home(), Path(__file__).resolve().parent)
    print(f"Canonical doclayer reference installed; deployment backup: {backup}")


if __name__ == "__main__":
    main()
