"""Job store durability: idempotent create, atomic state, claims, cleanup,
and the corrections ledger that outlives jobs."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from doclayer.curator.jobstore import JobStore, atomic_write_text
from doclayer.curator.state import JobState


def _state(job_id: str = "", key: str = "k1", intent: str = "test intent") -> JobState:
    return JobState(job_id=job_id, idempotency_key=key, intent=intent)


def test_create_is_durable_and_idempotent(store: JobStore) -> None:
    state, created = store.create(_state(key="same-key"))
    assert created
    assert state.job_id.startswith("job-")
    assert (store.state_path(state.job_id)).is_file()

    again, created_again = store.create(_state(key="same-key", intent="different text"))
    assert not created_again
    assert again.job_id == state.job_id


def test_create_without_key_makes_unique_jobs(store: JobStore) -> None:
    a, _ = store.create(_state(key=""))
    b, _ = store.create(_state(key=""))
    assert a.job_id != b.job_id


def test_save_roundtrip_preserves_schema(store: JobStore) -> None:
    state, _ = store.create(_state())
    state.exclusions = ["bedrock usage"]
    state.coverage = "incomplete"
    store.save(state)
    loaded = store.load(state.job_id)
    assert loaded.exclusions == ["bedrock usage"]
    assert loaded.coverage == "incomplete"
    data = json.loads(store.state_path(state.job_id).read_text())
    assert data["schema_version"] == 1


def test_interrupted_atomic_write_leaves_previous_state(store: JobStore) -> None:
    state, _ = store.create(_state())
    good = store.state_path(state.job_id).read_text()
    # simulate a crash mid-write: a leftover temp file must not be mistaken
    # for state, and the real state file must be untouched
    tmp = store.state_path(state.job_id).with_name(".state.json.tmp-deadbeef")
    tmp.write_text("{ this is torn")
    loaded = store.load(state.job_id)
    assert loaded.job_id == state.job_id
    assert store.state_path(state.job_id).read_text() == good
    tmp.unlink()


def test_claim_next_queued_is_exclusive(store: JobStore) -> None:
    a, _ = store.create(_state(key=""))
    b, _ = store.create(_state(key=""))
    first = store.claim_next_queued()
    assert first is not None and first.status == "processing"
    second = store.claim_next_queued()
    assert second is not None and second.job_id != first.job_id
    # re-processing from a second worker context must not double-claim
    third = store.claim_next_queued()
    assert third is None


def test_cancelled_job_is_never_claimed(store: JobStore) -> None:
    state, _ = store.create(_state(key=""))
    state.cancel_requested = True
    store.save(state)
    assert store.claim_next_queued() is None


def test_correction_ledger_survives_job_cleanup(store: JobStore) -> None:
    state, _ = store.create(_state(key=""))
    state.status = "published"
    store.save(state)
    store.add_correction({"page": "wiki/x.md", "claim": "the wrong claim text", "instruction": "say the right thing"})
    removed = store.cleanup_finished_jobs(older_than_days=0)
    assert removed == [state.job_id]
    assert not store.job_dir(state.job_id).exists()
    corrections = store.corrections(page="wiki/x.md")
    assert len(corrections) == 1
    assert corrections[0]["instruction"] == "say the right thing"


def test_unresolved_jobs_survive_cleanup(store: JobStore) -> None:
    state, _ = store.create(_state(key=""))
    state.status = "retryable"
    store.save(state)
    time.sleep(0.01)
    assert store.cleanup_finished_jobs(older_than_days=0) == []
    assert store.job_dir(state.job_id).exists()


def test_draft_paths_reject_escape(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs")
    state, _ = store.create(_state(key=""))
    store.write_draft(state.job_id, "wiki/ok.md", "hello")
    assert store.read_draft(state.job_id, "wiki/ok.md") == "hello"
    with pytest.raises(ValueError):
        store.write_draft(state.job_id, "../escape.md", "no")
    with pytest.raises(ValueError):
        store.write_draft(state.job_id, "/etc/passwd", "no")


def test_atomic_write_replaces_atomically(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    atomic_write_text(target, "v1")
    atomic_write_text(target, "v2")
    assert target.read_text() == "v2"
    assert not list(tmp_path.glob(".*tmp*"))


def test_queue_uses_submission_age_not_random_job_id(store):
    older = _state(job_id="job-ffffffff00", key="older")
    older.created_at = "2026-10-04T10:00:00Z"
    newer = _state(job_id="job-0000000000", key="newer")
    newer.created_at = "2026-10-04T10:00:01Z"
    store.create(newer)
    store.create(older)
    assert store.claim_next_queued().job_id == older.job_id
    assert store.claim_next_queued().job_id == newer.job_id


def test_queue_normalizes_dates_and_skips_locked_or_cancelled_old_work(store):
    oldest = _state(job_id="job-aaaaaaaa00", key="oldest")
    oldest.created_at = "2026-10-04T08:00:00-04:00"
    oldest, _ = store.create(oldest)
    next_job = _state(job_id="job-bbbbbbbb00", key="next")
    next_job.created_at = "2026-10-04T12:00:01Z"
    next_job, _ = store.create(next_job)
    later = _state(job_id="job-0000000000", key="later")
    later.created_at = "2026-10-04T12:00:02Z"
    store.create(later)
    lock = store.job_claim(oldest.job_id)
    assert lock.acquire(blocking=False)
    try:
        assert store.claim_next_queued().job_id == next_job.job_id
    finally:
        lock.release()
    oldest.cancel_requested = True
    store.save(oldest)
    assert store.claim_next_queued().job_id == later.job_id
    assert store.load(oldest.job_id).status == "queued"
    assert store.claim_next_queued() is None


def test_legacy_bad_date_does_not_starve_dated_queued_work(store):
    old = _state(job_id="job-0000000000", key="invalid")
    old.created_at = "legacy invalid date"
    store.create(old)
    valid = _state(job_id="job-ffffffff00", key="valid")
    valid.created_at = "2026-10-04T10:00:00Z"
    store.create(valid)
    assert store.claim_next_queued().job_id == valid.job_id
    assert store.claim_next_queued().job_id == old.job_id
