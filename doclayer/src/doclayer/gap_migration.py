"""Offline owner retirement/export. Never starts workers or merges owner ledgers.

The caller must reconnect obsolete clients first. A disk install cannot update
already-imported Python code, whose automatic capture may suppress write errors.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tarfile
from contextlib import ExitStack
from pathlib import Path

from doclayer.curator.jobstore import atomic_write_json
from doclayer.gaps import GapError, GapLedger


def inventory(root: Path) -> dict:
    """All owner bytes and permissions, excluding the separate writable outbox."""
    rows = {}
    if root.is_symlink():
        raise GapError("owner root must not be a symbolic link")
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts[0] == "outbox":
            continue
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise GapError("unsupported owner entry: " + str(rel))
        row = {"mode": stat.S_IMODE(path.stat().st_mode), "directory": path.is_dir()}
        if path.is_file():
            row["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            row["bytes"] = path.stat().st_size
        rows[str(rel)] = row
    return {
        "root": str(root),
        "root_mode": stat.S_IMODE(root.stat().st_mode),
        "entries": rows,
    }


def retire_and_export(
    root: Path,
    journal_dir: Path,
    *,
    owner: str,
    legacy_clients_reconnected: bool = False,
) -> dict:
    """Freeze old owning namespace and export every record/event/budget unchanged.

    Journal stages are durable. An interrupted freeze stays fail-closed and must
    be recovered using original modes in the journal, never by resetting budgets.
    Outbox data is deliberately neither frozen nor exported to the remote owner.
    """
    if root.is_symlink():
        raise GapError("owner root must not be a symbolic link")
    root = root.resolve()
    if legacy_clients_reconnected is not True:
        raise GapError("obsolete clients must reconnect before lossless owner migration")
    if not owner or not root.is_dir():
        raise GapError("existing owner root and nonempty destination required")
    if journal_dir.exists() or journal_dir.resolve().is_relative_to(root):
        raise GapError("journal must be new and outside the owning root")
    ledger = GapLedger(root)
    ledger.assert_owner_writable()
    with ExitStack() as stack:
        # Current supported writers serialize on these locks. Filesystem retirement
        # additionally blocks obsolete writers that do not know the new marker.
        for name in [
            "worker-cycle",
            "dispatcher",
            "spool",
            "migration",
            *ledger.list_ids(),
        ]:
            stack.enter_context(ledger.lock(name))
        budget_lock = root / "budgets" / "budgets.lock"
        if budget_lock.exists():
            from doclayer.curator.jobstore import FileLock

            stack.enter_context(FileLock(budget_lock))
        (root / "outbox").mkdir(mode=0o700, exist_ok=True)
        before = inventory(root)
        journal_dir.mkdir(mode=0o700, parents=True)
        journal = {
            "version": 1,
            "owner": owner,
            "phase": "prepared",
            "original": before,
            "legacy_clients_reconnected": True,
        }
        atomic_write_json(journal_dir / "journal.json", journal)
        atomic_write_json(
            root / "owner-migrated.json",
            {"version": 1, "owner": owner, "journal": str(journal_dir)},
        )
        return _finish_export(root, journal_dir, journal)


def _finish_export(root: Path, journal_dir: Path, journal: dict) -> dict:
    before = journal["original"]
    # Parent first: no new records or temporary atomic-write files can appear.
    os.chmod(root, before["root_mode"] & ~0o222)
    for rel, row in sorted(before["entries"].items(), key=lambda x: x[0].count("/")):
        os.chmod(root / rel, row["mode"] & ~0o222)
    os.chmod(root / "owner-migrated.json", 0o444)
    after = inventory(root)
    for rel, row in before["entries"].items():
        if "sha256" in row and after["entries"].get(rel, {}).get("sha256") != row["sha256"]:
            raise GapError("owner changed during retirement; journal retained; do not activate destination")
    extras = set(after["entries"]) - set(before["entries"]) - {"owner-migrated.json"}
    if extras:
        raise GapError("new owner entries during retirement; journal retained; do not activate destination")
    archive = journal_dir / "owner-state.tar"
    with tarfile.open(archive, "w") as tar:
        for rel in before["entries"]:

            def original_mode(info, rel=rel):
                info.mode = before["entries"][rel]["mode"]
                return info

            tar.add(root / rel, arcname=rel, recursive=False, filter=original_mode)
    os.chmod(archive, 0o600)
    with archive.open("rb") as stream:
        os.fsync(stream.fileno())
    journal.update(
        phase="retired_exported",
        archive=str(archive),
        archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
    )
    atomic_write_json(journal_dir / "journal.json", journal)
    return journal


def _read_retirement(journal_dir: Path) -> tuple[Path, dict]:
    journal = json.loads((journal_dir / "journal.json").read_text())
    if journal.get("version") != 1 or journal.get("phase") not in {
        "prepared",
        "retired_exported",
    }:
        raise GapError("journal is not a recoverable retirement")
    root = Path(journal["original"]["root"])
    if root.is_symlink():
        raise GapError("retired owner root became a symbolic link")
    marker = json.loads((root / "owner-migrated.json").read_text())
    if marker != {"version": 1, "owner": journal["owner"], "journal": str(journal_dir)}:
        raise GapError("moved-owner marker does not match journal")
    actual = inventory(root)["entries"]
    expected = journal["original"]["entries"]
    if set(actual) != set(expected) | {"owner-migrated.json"}:
        raise GapError("retired owner inventory changed; manual recovery required")
    for rel, row in expected.items():
        if "sha256" in row and actual[rel].get("sha256") != row["sha256"]:
            raise GapError("retired owner bytes changed; manual recovery required")
    return root, journal


def resume_retirement(journal_dir: Path) -> dict:
    """Finish an interrupted export from its original immutable snapshot intent."""
    from doclayer.curator.jobstore import FileLock

    with FileLock(journal_dir / "recovery.lock"):
        root, journal = _read_retirement(journal_dir)
        return _finish_export(root, journal_dir, journal)


def rollback_retirement(journal_dir: Path, *, destination_absent: bool = False) -> dict:
    """Restore original modes only before any destination owner has been activated.

    Caller must prove destination absence, keep research timers stopped, and never
    restore this old namespace over progressed remote state. Outbox stays intact.
    """
    from doclayer.curator.jobstore import FileLock

    if destination_absent is not True:
        raise GapError("rollback requires proven absent destination; never overwrite owner progress")
    with FileLock(journal_dir / "recovery.lock"):
        root, journal = _read_retirement(journal_dir)
        os.chmod(root, journal["original"]["root_mode"])
        for rel, row in sorted(journal["original"]["entries"].items(), key=lambda x: x[0].count("/")):
            os.chmod(root / rel, row["mode"])
        # New writer guard remains until every original permission is restored.
        (root / "owner-migrated.json").unlink()
        journal["phase"] = "rolled_back_before_destination"
        atomic_write_json(journal_dir / "journal.json", journal)
        return journal


def install_export(archive: Path, journal_path: Path, target: Path) -> dict:
    """Install into an absent owner; verify every byte before atomic activation.

    Run only on the owning host before its worker or RPC has been installed.
    Existing owner progress is never overwritten or combined with an old export.
    """
    import shutil
    import tempfile

    journal = json.loads(journal_path.read_text())
    if journal.get("phase") != "retired_exported":
        raise GapError("export retirement is not complete")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != journal.get("archive_sha256"):
        raise GapError("archive hash mismatch")
    if target.exists() or target.is_symlink():
        raise GapError("destination owner already exists; never overwrite its progress")
    entries = journal["original"]["entries"]
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".gap-owner-import-", dir=target.parent))
    try:
        with tarfile.open(archive) as tar:
            members = tar.getmembers()
            if len(members) != len(entries) or {m.name for m in members} != set(entries):
                raise GapError("archive member inventory mismatch")
            for member in members:
                rel = Path(member.name)
                if rel.is_absolute() or ".." in rel.parts or not (member.isfile() or member.isdir()):
                    raise GapError("unsafe archive member")
            for member in sorted(members, key=lambda m: (m.name.count("/"), m.name)):
                dest = temporary / member.name
                if member.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(member) as src, dest.open("xb") as dst:
                        shutil.copyfileobj(src, dst)
                        dst.flush()
                        os.fsync(dst.fileno())
                os.chmod(dest, entries[member.name]["mode"])
        os.chmod(temporary, journal["original"]["root_mode"])
        actual = inventory(temporary)
        if actual["entries"] != entries:
            raise GapError("import byte/mode inventory mismatch")
        # A cooperating new owner uses this parent lock; production sequencing
        # must also keep RPC/worker absent until this import completes.
        from doclayer.curator.jobstore import FileLock

        with FileLock(target.parent / ".gap-owner-import.lock"):
            if target.exists() or target.is_symlink():
                raise GapError("destination appeared during import")
            os.rename(temporary, target)
            directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return {
            "phase": "installed",
            "target": str(target),
            "archive_sha256": journal["archive_sha256"],
            "entries": len(entries),
        }
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--journal", type=Path)
    parser.add_argument(
        "--owner",
        help="new owning ledger, as HOST:PATH (for example worker1:~/.local/state/doclayer/knowledge-gaps);"
        " required with --apply unless --install-archive is given",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--install-archive", type=Path)
    parser.add_argument("--legacy-clients-reconnected", action="store_true")
    args = parser.parse_args()
    if args.apply:
        if args.journal is None:
            parser.error("--apply requires --journal")
        if args.install_archive:
            print(
                json.dumps(
                    install_export(args.install_archive, args.journal, args.root),
                    sort_keys=True,
                )
            )
            return
        if not args.owner:
            parser.error("--owner is required with --apply")
        result = retire_and_export(
            args.root,
            args.journal,
            owner=args.owner,
            legacy_clients_reconnected=args.legacy_clients_reconnected,
        )
    else:
        result = inventory(args.root)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
