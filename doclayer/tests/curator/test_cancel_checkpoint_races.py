"""Actual worker/control writes serialize with acknowledged operator cancellation."""

import threading

import pytest

from doclayer.curator.ops import handle
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import JobState


@pytest.mark.parametrize("operation", ["checkpoint", "queue_claim"])
def test_worker_write_cannot_overwrite_cancel(cfg, store, monkeypatch, operation):
    state, _ = store.create(
        JobState(
            job_id="",
            idempotency_key=f"peer-worker-cancel-{operation}",
            intent="Preserve scoped Widget facts",
            status="queued" if operation == "queue_claim" else "processing",
        )
    )
    original_save = store.save
    cancel_started = threading.Event()
    cancel_finished = threading.Event()
    errors = []
    threads = []
    fired = False

    def cancel():
        cancel_started.set()
        try:
            handle("cancel", {"job_id": state.job_id}, cfg, store)
        except Exception as exc:  # noqa: BLE001 — surface thread failures
            errors.append(exc)
        finally:
            cancel_finished.set()

    def interleaved_save(candidate):
        nonlocal fired
        if candidate.status == "processing" and not fired:
            fired = True
            thread = threading.Thread(target=cancel)
            threads.append(thread)
            thread.start()
            assert cancel_started.wait(2)
            # Old unlocked writes allowed cancel to acknowledge here and then
            # overwrote it. Shared control serialization safely delays cancel.
            cancel_finished.wait(0.2)
        return original_save(candidate)

    monkeypatch.setattr(store, "save", interleaved_save)
    if operation == "checkpoint":
        Pipeline(cfg, store)._persist(state)
    else:
        claimed = store.claim_next_queued()
        assert claimed is not None and claimed.job_id == state.job_id
    assert fired
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert not errors
    assert cancel_finished.is_set()
    assert store.load(state.job_id).cancel_requested is True
    assert store.claim_next_queued() is None
    claim = store.job_claim(state.job_id)
    assert claim.acquire(blocking=False)
    claim.release()


def test_worker_checkpoint_preserves_previously_acknowledged_cancel(cfg, store):
    state, _ = store.create(
        JobState(
            job_id="",
            idempotency_key="peer-stale-worker-cancel",
            intent="Preserve facts",
            status="processing",
        )
    )
    handle("cancel", {"job_id": state.job_id}, cfg, store)
    assert state.cancel_requested is False
    Pipeline(cfg, store)._persist(state)
    assert state.cancel_requested is True
    assert store.load(state.job_id).cancel_requested is True
