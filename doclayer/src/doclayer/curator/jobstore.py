"""Durable filesystem job store for the curator.

Layout (all under the configured jobs dir, default
``~/.local/share/brain-curator/jobs``)::

    <job_id>/state.json        atomically replaced, fsynced
    <job_id>/summary.md        the completion digest
    <job_id>/drafts/…          draft pages (never canonical)
    <job_id>/evidence.jsonl    retained evidence records (durable, not cache)
    <job_id>/staging/…         submitted local files transferred by the CLI
    <job_id>/.claim            flock: one processor at a time per job
    index/idem-<key>.json      submission idempotency: key -> job_id
    index/input-<digest>.json  published-input index for repeat dedup
    index/locks/.worker.lock   global worker claim
    index/locks/.publish.lock  publication serialization
    corrections.jsonl          operator corrections; survives job cleanup

Writes are atomic (temp file + os.replace + fsync). Job IDs are short,
unique, and independent of topic.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from doclayer.curator.state import JobState

_JOB_ID_RE = re.compile(r"^job-[0-9a-z]{8,24}$")


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_text(path: Path, text: str) -> None:
    """Replace path's contents atomically and durably."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{secrets.token_hex(4)}")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=False) + "\n")


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass  # best effort on filesystems that do not fsync directories


def content_digest(*parts: str) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part.encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()


class FileLock:
    """An advisory flock held on a lock file for the life of the object."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self, blocking: bool = True) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


class JobStore:
    def __init__(self, jobs_dir: Path | str) -> None:
        self.jobs_dir = Path(jobs_dir).expanduser()
        self.index_dir = self.jobs_dir / "index"
        self.locks_dir = self.index_dir / "locks"

    # -- paths ----------------------------------------------------------- #

    def job_dir(self, job_id: str) -> Path:
        if not _JOB_ID_RE.match(job_id):
            raise ValueError(f"malformed job id: {job_id!r}")
        return self.jobs_dir / job_id

    def state_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "state.json"

    def _new_job_id(self) -> str:
        return f"job-{secrets.token_hex(5)}"  # 10 hex chars

    # -- create / load ---------------------------------------------------- #

    def create(self, state: JobState) -> tuple[JobState, bool]:
        """Durably create a job. Returns (state, created). If the
        idempotency key already maps to a job, returns that job unchanged."""
        state.created_at = state.created_at or now_iso()
        state.updated_at = now_iso()
        if state.idempotency_key:
            key_path = self.index_dir / f"idem-{state.idempotency_key}.json"
            key_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                existing = json.loads(key_path.read_text(encoding="utf-8"))
                prior = self.load(existing["job_id"])
                return prior, False
            except (OSError, KeyError, ValueError):
                pass
        state.job_id = state.job_id or self._new_job_id()
        job_dir = self.job_dir(state.job_id)
        job_dir.mkdir(parents=True, exist_ok=False)
        (job_dir / "drafts").mkdir()
        (job_dir / "staging").mkdir()
        atomic_write_json(self.state_path(state.job_id), state.to_dict())
        if state.idempotency_key:
            atomic_write_json(
                self.index_dir / f"idem-{state.idempotency_key}.json",
                {"job_id": state.job_id},
            )
        return state, True

    def load(self, job_id: str) -> JobState:
        data = json.loads(self.state_path(job_id).read_text(encoding="utf-8"))
        return JobState.from_dict(data)

    def save(self, state: JobState) -> None:
        """Persist the state atomically (the only sanctioned write path)."""
        state.updated_at = now_iso()
        atomic_write_json(self.state_path(state.job_id), state.to_dict())

    def exists(self, job_id: str) -> bool:
        return self.state_path(job_id).is_file()

    def list_ids(self) -> list[str]:
        if not self.jobs_dir.is_dir():
            return []
        return sorted(
            p.name
            for p in self.jobs_dir.iterdir()
            if p.is_dir() and _JOB_ID_RE.match(p.name) and (p / "state.json").is_file()
        )

    def load_all(self) -> list[JobState]:
        states = []
        for job_id in self.list_ids():
            try:
                states.append(self.load(job_id))
            except (OSError, ValueError):
                continue  # a torn write from a crash is skipped, not fatal
        return states

    # -- claims ----------------------------------------------------------- #

    def worker_lock(self) -> FileLock:
        return FileLock(self.locks_dir / ".worker.lock")

    def publish_lock(self) -> FileLock:
        return FileLock(self.locks_dir / ".publish.lock")

    def job_claim(self, job_id: str) -> FileLock:
        return FileLock(self.job_dir(job_id) / ".claim")

    def control_lock(self, job_id: str) -> FileLock:
        """Serialize cancellation with checkpoint/control writes.

        Workers acquire their job claim first; cancellation never waits for
        a job claim while holding this lock.
        """
        return FileLock(self.job_dir(job_id) / ".control")

    def claim_next_queued(self) -> JobState | None:
        """Claim the oldest queued job for this worker process, or None.

        The claim lock makes the queue-check-and-flip atomic; it is released
        before returning — the processing worker re-acquires it for the job's
        whole run (process_job), and other workers skip the job because its
        status is no longer queued.
        """
        queued = []
        for state in self.load_all():
            if state.status != "queued" or state.cancel_requested:
                continue
            try:
                created = datetime.fromisoformat(state.created_at.replace("Z", "+00:00"))
                stamp = created.replace(tzinfo=UTC).timestamp() if created.tzinfo is None else created.timestamp()
            except (AttributeError, TypeError, ValueError, OverflowError):
                # Malformed legacy dates never put new work ahead of known older
                # work; they remain eligible after dated submissions.
                stamp = float("inf")
            queued.append((stamp, state.job_id))
        for _, job_id in sorted(queued):
            claim = self.job_claim(job_id)
            if not claim.acquire(blocking=False):
                continue
            try:
                with self.control_lock(job_id):
                    state = self.load(job_id)
                    if state.status != "queued" or state.cancel_requested:
                        continue
                    state.status = "processing"
                    self.save(state)
            finally:
                claim.release()
            return state
        return None

    def hold_claim(self, job_id: str) -> FileLock | None:
        """Re-acquire a job's claim (resume path). None if someone else holds it."""
        claim = self.job_claim(job_id)
        if claim.acquire(blocking=False):
            return claim
        return None

    # -- drafts / staging / evidence -------------------------------------- #

    def write_draft(self, job_id: str, rel_path: str, text: str) -> Path:
        safe = _safe_rel(rel_path)
        path = self.job_dir(job_id) / "drafts" / safe
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def read_draft(self, job_id: str, rel_path: str) -> str | None:
        path = self.job_dir(job_id) / "drafts" / _safe_rel(rel_path)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def write_summary(self, job_id: str, text: str) -> Path:
        path = self.job_dir(job_id) / "summary.md"
        atomic_write_text(path, text)
        return path

    def read_summary(self, job_id: str) -> str | None:
        try:
            return (self.job_dir(job_id) / "summary.md").read_text(encoding="utf-8")
        except OSError:
            return None

    def append_evidence(self, job_id: str, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        path = self.job_dir(job_id) / "evidence.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def read_evidence(self, job_id: str) -> list[dict[str, Any]]:
        path = self.job_dir(job_id) / "evidence.jsonl"
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def staging_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "staging"

    # -- dedup index ------------------------------------------------------- #

    def record_published_input(self, digest: str, job_id: str, pages: list[str]) -> None:
        atomic_write_json(
            self.index_dir / f"input-{digest}.json",
            {"job_id": job_id, "pages": pages, "recorded_at": now_iso()},
        )

    def find_published_input(self, digest: str) -> dict[str, Any] | None:
        path = self.index_dir / f"input-{digest}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # -- corrections (durable beyond job cleanup) -------------------------- #

    @property
    def corrections_path(self) -> Path:
        return self.jobs_dir / "corrections.jsonl"

    def add_correction(self, record: dict[str, Any]) -> dict[str, Any]:
        record = {
            "recorded_at": now_iso(),
            "correction_id": f"corr-{secrets.token_hex(4)}",
            **record,
        }
        self.corrections_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.corrections_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return record

    def corrections(self, page: str | None = None) -> list[dict[str, Any]]:
        if not self.corrections_path.is_file():
            return []
        out = []
        for line in self.corrections_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if page is None or record.get("page") == page:
                out.append(record)
        return out

    # -- cleanup ----------------------------------------------------------- #

    def cleanup_finished_jobs(self, older_than_days: float, now_s: float | None = None) -> list[str]:
        """Delete finished jobs older than the retention window. Corrections
        and the dedup index are NOT job artifacts and survive this."""
        now_s = time.time() if now_s is None else now_s
        removed = []
        cutoff = older_than_days * 86400
        for job_id in self.list_ids():
            try:
                state = self.load(job_id)
            except (OSError, ValueError):
                continue
            if not state.finished:
                continue
            try:
                mtime = self.state_path(job_id).stat().st_mtime
            except OSError:
                continue
            if now_s - mtime > cutoff:
                shutil.rmtree(self.job_dir(job_id), ignore_errors=True)
                removed.append(job_id)
        return removed


def _safe_rel(rel_path: str) -> Path:
    """Reject path escapes for draft paths (model-proposed paths are data)."""
    candidate = Path(rel_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"unsafe draft path: {rel_path!r}")
    return candidate
