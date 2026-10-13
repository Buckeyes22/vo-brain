"""Independent actual owning-ops interleaving: cancellation wins over auto-resume."""

import threading

from doclayer.curator.ops import OpsError, handle
from doclayer.curator.state import JobState


def test_conditional_resume_cannot_overwrite_operator_cancel_after_admission(cfg, store, monkeypatch):
    state, _ = store.create(
        JobState(
            job_id="",
            idempotency_key="peer-cancel-resume",
            intent="Keep complete Widget authentication facts",
            status="retryable",
            error="inference: inference HTTP 503 (transient)",
        )
    )
    reply = handle("status", {"job_id": state.job_id}, cfg, store)
    checkpoint = reply["job"]["resume_checkpoint"]
    original_save = store.save
    fired = False
    cancelled = threading.Event()
    errors = []
    cancel_thread = None

    def cancel():
        try:
            handle("cancel", {"job_id": state.job_id}, cfg, store)
        except Exception as exc:  # noqa: BLE001 — propagate thread failure to test
            errors.append(exc)
        finally:
            cancelled.set()

    def interleaved_save(candidate):
        nonlocal fired, cancel_thread
        if candidate.status == "queued" and not fired:
            fired = True
            # Actual cancel operation persists its request after resume inspected
            # the old generation, immediately before resume would write queued.
            cancel_thread = threading.Thread(target=cancel)
            cancel_thread.start()
            # Without state-mutation serialization, cancel acknowledges first and
            # resume overwrites it. A correct lock may defer cancel until resume
            # commits; that is safe if the cancellation survives afterwards.
            cancelled.wait(timeout=1)
        return original_save(candidate)

    monkeypatch.setattr(store, "save", interleaved_save)
    try:
        handle(
            "resume",
            {"job_id": state.job_id, "expected_checkpoint": checkpoint},
            cfg,
            store,
        )
    except OpsError:
        pass  # A changed-generation/operation-conflict refusal is safe.
    assert fired
    cancel_thread.join(timeout=5)
    assert not cancel_thread.is_alive()
    assert not errors
    assert store.load(state.job_id).cancel_requested is True
