"""Every way a gap job can stop recovers on its own or releases its slot.

A stopped gap job that is neither a model-call cap nor a typed task stop gets
at most three conditional resumes of the same job (crash, publication failure
and inference outage only); then, or at once for every other stop, its
allowance is retired: the slot is released, the gap returns to a visible
backoff, and the job's work stays on the curator untouched.
"""

import pytest
from test_outage_resume import make_due, setup_gap

from doclayer.dispatcher import GapDispatcher
from doclayer.gap_worker import _verify_published
from doclayer.gaps import GapLedger


def stop(store, job_id, *, error, stage=None, **usage):
    state = store.load(job_id)
    state.status = "retryable"
    state.error = error
    if stage is not None:
        state.stage = stage
    for key, value in usage.items():
        setattr(state.usage, key, value)
    store.save(state)
    return state


def released_to_backoff(ledger, d, store, gap_id, job_id, before):
    """The slot is gone, the gap waits on a backoff, the job is untouched."""
    row = ledger.load(gap_id)
    assert d.active() is None
    assert row.status == "retry_wait" and row.backoff_until
    assert row.failure_reason.startswith("stopped_unresolved")
    assert row.submission["state"] == "terminal"
    assert row.submission["retirement_receipt"]["disposition"] == "stopped_unresolved"
    assert store.state_path(job_id).read_bytes() == before
    assert (store.job_dir(job_id) / "allowance-retirement.json").exists()


def resume_until_cap(ledger, ops, d, store, gap_id, job_id, error, **stage):
    """Three bounded resumes of the same job, each followed by the same stop."""
    for attempt in range(3):
        notes = []
        _verify_published(ledger, d, notes)
        assert "waiting" in notes[-1], notes
        make_due(ledger, gap_id)
        _verify_published(ledger, d, notes)
        assert "resumed" in notes[-1], notes
        assert store.load(job_id).status == "queued"
        state = store.load(job_id)
        stop(store, job_id, error=error, model_calls=state.usage.model_calls + 1, **stage)
        d = GapDispatcher(GapLedger(ledger.root), transport=ops)
    return d


@pytest.mark.parametrize(
    "error,stage",
    [
        ("worker crash: RuntimeError('boom')", None),
        # Publication failed before anything was committed; a job that has
        # committed is never retired (test_publication_pending.py).
        ("publication: git fetch --prune origin: cannot lock ref 'refs/remotes/origin/main'", "publish"),
        ("inference: inference HTTP 503 (transient)", None),
    ],
)
def test_resumable_stop_resumes_three_times_then_releases_slot(tmp_path, cfg, store, error, stage):
    ledger, ops, d, gap_id, job_id = setup_gap(tmp_path, cfg, store)
    staged = {"stage": stage} if stage else {}
    stop(store, job_id, error=error, **staged)
    d = resume_until_cap(ledger, ops, d, store, gap_id, job_id, error, **staged)
    before = store.state_path(job_id).read_bytes()
    notes = []
    _verify_published(ledger, d, notes)
    assert "stopped_unresolved" in notes[-1] and "slot retained" not in notes[-1]
    assert len([op for op, _ in ops.calls if op == "resume"]) == 3
    released_to_backoff(ledger, d, store, gap_id, job_id, before)
    # The next cycle neither resumes nor retires the same job again.
    _verify_published(ledger, d, notes)
    assert len([op for op, _ in ops.calls if op == "retire_allowance"]) == 1


@pytest.mark.parametrize(
    "error,stage,usage",
    [
        ("limit reached: active_time", "draft", {"active_seconds": 900}),
        (
            "model output truncated repeatedly; split the batch or raise the serving output limit",
            "report",
            {},
        ),
        ("inference: model did not return parseable JSON", "publish", {}),
        ("inference: inference HTTP 503 (transient)", "draft", {"model_calls": 50}),
    ],
)
def test_unresumable_stop_releases_slot_without_resume(tmp_path, cfg, store, error, stage, usage):
    ledger, ops, d, gap_id, job_id = setup_gap(tmp_path, cfg, store)
    stop(store, job_id, error=error, stage=stage, **usage)
    before = store.state_path(job_id).read_bytes()
    notes = []
    _verify_published(ledger, d, notes)
    assert not any(op == "resume" for op, _ in ops.calls)
    assert "stopped_unresolved" in notes[-1]
    released_to_backoff(ledger, d, store, gap_id, job_id, before)


def test_released_gap_gets_a_new_attempt_with_identical_material(tmp_path, cfg, store):
    ledger, ops, d, gap_id, job_id = setup_gap(tmp_path, cfg, store)
    stop(store, job_id, error="limit reached: active_time", active_seconds=900)
    _verify_published(ledger, d, [])
    assert d.active() is None
    # Research makes it ready again with the same sources: a stopped job is
    # retried, not parked as an identical-input reimport.
    ledger.transition(gap_id, "ready_for_curation", backoff_until=None)
    outcome = d.dispatch(gap_id)
    assert outcome.action == "submitted" and outcome.job_id != job_id
    assert store.load(job_id).status == "retryable"  # old work kept for readmission


def test_private_stopped_job_returns_to_evidence_backoff(tmp_path, cfg, store):
    ledger, ops, d, gap_id, job_id = setup_gap(tmp_path, cfg, store)
    ledger.update(gap_id, lambda r: r.submission["payload"].update(private=True))
    stop(store, job_id, error="limit reached: active_time", active_seconds=900)
    _verify_published(ledger, d, [])
    row = ledger.load(gap_id)
    assert d.active() is None
    assert row.status == "needs_local_evidence" and row.backoff_until


def test_cancel_requested_stop_is_cancelled_and_releases_slot(tmp_path, cfg, store):
    ledger, ops, d, gap_id, job_id = setup_gap(tmp_path, cfg, store)
    state = stop(store, job_id, error="worker crash: RuntimeError('boom')")
    state.cancel_requested = True
    store.save(state)
    notes = []
    _verify_published(ledger, d, notes)
    assert store.load(job_id).status == "cancelled"
    _verify_published(ledger, d, notes)
    assert d.active() is None
    assert ledger.load(gap_id).status == "failed"
    assert not any(op == "resume" for op, _ in ops.calls)


def test_retired_stopped_need_is_counted_in_digest(tmp_path, cfg, store):
    from doclayer.gap_worker import digest

    ledger, ops, d, gap_id, job_id = setup_gap(tmp_path, cfg, store)
    stop(store, job_id, error="limit reached: active_time", active_seconds=900)
    _verify_published(ledger, d, [])
    summary = ledger.summary()
    assert summary["stopped_unresolved"] == 1 and summary["outstanding"] == 1
    assert "1 need(s) retrying after a stopped curator job" in digest(summary)
