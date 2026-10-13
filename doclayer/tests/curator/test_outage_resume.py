"""Actual ops/CAS and charged pipeline stops; no remote or native inference."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from doclayer.curator.ops import OpsError, handle
from doclayer.curator.pipeline import Pipeline
from doclayer.dispatcher import GapDispatcher
from doclayer.gaps import GapLedger


class OpsTransport:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.calls = []
        self.lose = False

    def call(self, op, payload, **kw):
        self.calls.append((op, dict(payload)))
        reply = handle(op, payload, self.cfg, self.store)
        if self.lose and op == "resume":
            self.lose = False
            raise TimeoutError("lost acknowledged queue reply")
        return reply


def setup_gap(tmp_path, cfg, store):
    ledger = GapLedger(tmp_path / "gaps")
    ledger.root.mkdir()
    (ledger.root / "curator-ready").touch()
    record, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="widget",
        facets=["authentication"],
        representative_question="What does official Widget documentation say about authentication?",
        source_candidates=[{"url": "https://example.org/widget", "authority": "official"}],
    )
    ledger.transition(record.gap_id, "ready_for_curation")
    ops = OpsTransport(cfg, store)
    dispatcher = GapDispatcher(ledger, transport=ops)
    outcome = dispatcher.dispatch(record.gap_id)
    state = store.load(outcome.job_id)
    state.limits.update(max_active_seconds=900)
    state.status = "retryable"
    state.error = "inference: inference HTTP 503 (transient)"
    state.usage.model_calls = 2
    store.save(state)
    return ledger, ops, dispatcher, record.gap_id, state.job_id


def observe(ops, job_id):
    return ops.call("status", {"job_id": job_id})["job"]


def make_due(ledger, gap_id):
    ledger.update(
        gap_id,
        lambda r: r.submission["outage_resume"].update(next_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat()),
    )


def test_lost_resume_reply_queries_same_actual_job_no_new_allowance(tmp_path, cfg, store):
    ledger, ops, dispatcher, gap_id, job_id = setup_gap(tmp_path, cfg, store)
    before = ledger.load(gap_id).submission["reservation"]
    job = observe(ops, job_id)
    assert dispatcher.resume_outage(gap_id, job).action == "waiting"
    make_due(ledger, gap_id)
    ops.lose = True
    assert dispatcher.resume_outage(gap_id, job).action == "uncertain"
    current = observe(ops, job_id)
    assert current["status"] == "queued" and current["usage"]["model_calls"] == 2
    assert dispatcher.resume_outage(gap_id, current).action == "parked"
    assert len([op for op, _ in ops.calls if op == "resume"]) == 1
    assert ledger.load(gap_id).submission["reservation"] == before
    assert len(store.list_ids()) == 1 and dispatcher.active()["job_id"] == job_id
    assert ledger.load(gap_id).status == "curating"  # unanswered remains open
    assert all("extend" not in p for op, p in ops.calls if op == "resume")


@pytest.mark.parametrize(
    "error",
    [
        "limit reached: max_model_calls",
        "inference: model did not return parseable JSON",
        "inference: invalid source-support verification response",
        "inference: invalid draft enumeration fidelity response",
        "inference: inference rejected credentials: HTTP 401",
        "model output truncated repeatedly; split the batch or raise the serving output limit",
    ],
)
def test_semantic_parser_auth_and_budget_never_auto_resume(tmp_path, cfg, store, error):
    ledger, ops, d, gap_id, job_id = setup_gap(tmp_path, cfg, store)
    state = store.load(job_id)
    state.error = error
    store.save(state)
    # Never resumed; the allowance is retired so the slot is released.
    assert d.resume_outage(gap_id, observe(ops, job_id)).action == "stopped_unresolved"
    assert not any(op == "resume" for op, _ in ops.calls)
    assert d.active() is None and ledger.load(gap_id).status == "retry_wait"


@pytest.mark.parametrize("field,value", [("model_calls", 50), ("active_seconds", 900)])
def test_outage_at_existing_cap_is_retired(tmp_path, cfg, store, field, value):
    _, ops, d, gap_id, job_id = setup_gap(tmp_path, cfg, store)
    state = store.load(job_id)
    setattr(state.usage, field, value)
    store.save(state)
    assert d.resume_outage(gap_id, observe(ops, job_id)).action == "stopped_unresolved"
    assert not any(op == "resume" for op, _ in ops.calls)
    assert d.active() is None


def test_conditional_checkpoint_and_claim_races_do_not_overwrite(tmp_path, cfg, store):
    _, ops, _, _, job_id = setup_gap(tmp_path, cfg, store)
    token = observe(ops, job_id)["resume_checkpoint"]
    state = store.load(job_id)
    state.usage.model_calls += 1
    store.save(state)
    with pytest.raises(OpsError, match="checkpoint changed"):
        ops.call("resume", {"job_id": job_id, "expected_checkpoint": token})
    assert store.load(job_id).usage.model_calls == 3
    claim = store.job_claim(job_id)
    assert claim.acquire(blocking=False)
    try:
        with pytest.raises(OpsError, match="claimed"):
            ops.call("resume", {"job_id": job_id})
    finally:
        claim.release()
    state = store.load(job_id)
    state.status = "processing"
    store.save(state)
    with pytest.raises(OpsError, match="already active"):
        ops.call("resume", {"job_id": job_id})
    assert store.load(job_id).status == "processing"
    state.status = "retryable"
    store.save(state)
    assert ops.call("resume", {"job_id": job_id, "extend": {"max_model_calls": 5}})["ok"]
    assert store.load(job_id).limits["max_model_calls"] == 55
    assert store.load(job_id).usage.model_calls == 3


def test_actual_charged_pipeline_outage_then_resume_keeps_counter(tmp_path, cfg, store, monkeypatch):
    ledger, ops, d, gap_id, job_id = setup_gap(tmp_path, cfg, store)
    cfg.inference.retries = 0
    statuses = [503, 200]

    def http(request):
        status = statuses.pop(0)
        return httpx.Response(
            status,
            json={"choices": [{"message": {"content": "literal fixture"}, "finish_reason": "stop"}]},
        )

    pipeline = Pipeline(cfg, store, inference_transport=httpx.MockTransport(http))

    async def stage(state):
        await pipeline._model(state).chat("Fixture", "Full bounded fixture data")
        state.status = "covered"
        return None

    monkeypatch.setattr(pipeline, "_run_stage", stage)
    state = store.load(job_id)
    state.status = "queued"
    store.save(state)
    stopped = asyncio.run(pipeline.run(state))
    assert stopped.status == "retryable" and stopped.error == "inference: inference HTTP 503 (transient)"
    assert stopped.usage.model_calls == 3
    job = observe(ops, job_id)
    assert d.resume_outage(gap_id, job).action == "waiting"
    make_due(ledger, gap_id)
    assert d.resume_outage(gap_id, job).action == "resumed"
    result = asyncio.run(pipeline.run(store.load(job_id)))
    assert result.status == "covered" and result.usage.model_calls == 4
    # Curator completion itself is not semantic answer closure.
    d.observe(gap_id, observe(ops, job_id))
    assert d.active() is None and ledger.load(gap_id).status != "resolved"


def test_three_resume_cap_survives_new_error_generations_and_restart(tmp_path, cfg, store):
    ledger, ops, d, gap_id, job_id = setup_gap(tmp_path, cfg, store)
    for attempt in range(3):
        job = observe(ops, job_id)
        assert d.resume_outage(gap_id, job).action == "waiting"
        make_due(ledger, gap_id)
        assert d.resume_outage(gap_id, job).action == "resumed"
        state = store.load(job_id)
        state.status = "retryable"
        state.error = "inference: inference unreachable: ConnectError"
        state.usage.model_calls += 1
        store.save(state)
        d = GapDispatcher(GapLedger(ledger.root), transport=ops)
    job = observe(ops, job_id)
    # A stop after the third resume retires the allowance and frees the slot.
    assert d.resume_outage(gap_id, job).action == "stopped_unresolved"
    assert len([op for op, _ in ops.calls if op == "resume"]) == 3
    assert d.active() is None and ledger.load(gap_id).status == "retry_wait"
    assert store.load(job_id).status == "retryable"  # the job keeps its work


def test_daily_limit_missing_caps_and_paused_owner_withhold(tmp_path, cfg, store):
    ledger, ops, d, gap_id, job_id = setup_gap(tmp_path, cfg, store)
    job = observe(ops, job_id)
    missing = json.loads(json.dumps(job))
    missing["limits"].pop("max_active_seconds")
    assert d.resume_outage(gap_id, missing).action == "parked"
    d.set_paused(True)
    assert d.resume_outage(gap_id, job).action == "parked"
    d.set_paused(False)
    assert d.resume_outage(gap_id, job).action == "waiting"
    make_due(ledger, gap_id)
    # A preexisting hold cannot authorize work after a tightened daily cap.
    d.day_ledger.max_calls_day = 49
    assert d.resume_outage(gap_id, job).action == "parked"
    assert not any(op == "resume" for op, _ in ops.calls)


def test_actual_worker_observation_resumes_only_outage_and_keeps_unanswered(tmp_path, cfg, store):
    from doclayer.gap_worker import _verify_published

    ledger, ops, d, gap_id, job_id = setup_gap(tmp_path, cfg, store)
    notes = []
    _verify_published(ledger, d, notes)
    assert "waiting" in notes[-1]
    make_due(ledger, gap_id)
    _verify_published(ledger, d, notes)
    assert "resumed" in notes[-1] and store.load(job_id).status == "queued"
    assert ledger.load(gap_id).status != "resolved" and d.active()


def test_full_state_checkpoint_rejects_changed_source_even_same_usage_timestamp(tmp_path, cfg, store):
    from doclayer.curator.ops import resume_checkpoint

    _, ops, _, _, job_id = setup_gap(tmp_path, cfg, store)
    state = store.load(job_id)
    token = resume_checkpoint(state)
    old_updated = state.updated_at
    state.sources[0].content_hash = "f" * 64
    state.updated_at = old_updated
    # Hash contract includes full source/evidence material, not just counters.
    assert resume_checkpoint(state) != token
    store.save(state)
    before = store.load(job_id).to_dict()
    with pytest.raises(OpsError, match="checkpoint changed"):
        ops.call("resume", {"job_id": job_id, "expected_checkpoint": token})
    assert store.load(job_id).to_dict() == before


def test_preparing_continuation_cancel_survives_final_queue(cfg, store, monkeypatch):
    from test_continuation_frontier import parent_job

    from doclayer.curator import curate
    from doclayer.curator.ops import _op_continue

    parent, cache = parent_job(cfg, store)
    original = store.state_path(parent.job_id).read_bytes()
    original_map = curate.map_source_structure
    cancelled = []

    def cancel_during_prepare(source, text, **kw):
        child = next(s for s in store.load_all() if s.job_id != parent.job_id)
        assert child.status == "preparing"
        with pytest.raises(OpsError, match="already active"):
            handle("resume", {"job_id": child.job_id}, cfg, store)
        handle("cancel", {"job_id": child.job_id}, cfg, store)
        cancelled.append(child.job_id)
        return original_map(source, text, **kw)

    monkeypatch.setattr(curate, "map_source_structure", cancel_during_prepare)
    result = _op_continue({"job_id": parent.job_id}, cfg, store)
    child = store.load(result["job_id"])
    assert cancelled and child.cancel_requested and child.status == "cancelled"
    assert store.state_path(parent.job_id).read_bytes() == original
    assert cache.is_file()


def test_staging_path_checkpoint_serializes_operator_cancel(tmp_path, cfg, store, monkeypatch):
    import threading

    from doclayer.curator.ops import _move_staging

    _, ops, _, _, job_id = setup_gap(tmp_path, cfg, store)
    staging = tmp_path / "incoming"
    staging.mkdir()
    staged_file = staging / "input.md"
    staged_file.write_text("complete preserved bytes")
    state = store.load(job_id)
    state.status = "queued"
    state.sources[0].local_path = str(staged_file)
    store.save(state)
    original_load = store.load
    loaded, release = threading.Event(), threading.Event()
    errors = []

    def paused_load(identifier):
        value = original_load(identifier)
        if threading.current_thread().name == "staging-owner":
            loaded.set()
            assert release.wait(5)
        return value

    monkeypatch.setattr(store, "load", paused_load)

    def move():
        try:
            _move_staging(store, "key", job_id, staging)
        except BaseException as exc:
            errors.append(exc)

    mover = threading.Thread(target=move, name="staging-owner")
    cancel = threading.Thread(target=lambda: ops.call("cancel", {"job_id": job_id}))
    mover.start()
    try:
        assert loaded.wait(5)
        probe = store.control_lock(job_id)
        acquired = probe.acquire(blocking=False)
        if acquired:
            probe.release()
        assert not acquired  # pre-fix path save exposed the stale-state window
        cancel.start()
    finally:
        release.set()
        mover.join(5)
        if cancel.ident is not None:
            cancel.join(5)
    assert not errors and not mover.is_alive() and not cancel.is_alive()
    result = original_load(job_id)
    assert result.cancel_requested and result.status == "cancelled"
    assert result.sources[0].local_path == str(store.staging_dir(job_id) / "input.md")
    assert (store.staging_dir(job_id) / "input.md").read_text() == "complete preserved bytes"
