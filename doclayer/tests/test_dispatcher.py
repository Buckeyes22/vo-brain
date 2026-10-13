"""Dispatcher tests: durable budgets, idempotency, pause,
dry-run until readiness, transport-timeout recovery."""

from __future__ import annotations

from pathlib import Path

from doclayer.curator.transport import Transport, TransportError
from doclayer.dispatcher import (
    MAX_MODEL_CALLS_PER_JOB,
    DayLedger,
    GapDispatcher,
    attempt_key,
)
from doclayer.gap_budget import ResourceBudget
from doclayer.gaps import GapLedger


class FakeTransport(Transport):
    def __init__(self, *, fail: bool = False, duplicate: bool = False):
        self.calls: list[dict] = []
        self.fail = fail
        self.duplicate = duplicate

    def call(self, op, payload, **kwargs):
        if self.fail:
            raise TransportError("remote ops call timed out (the job stays durable)")
        self.calls.append({"op": op, "payload": payload})
        job_id = f"job-{attempt_key(payload['idempotency_key'], 0, [])[:8]}"
        return {"ok": True, "job_id": job_id, "created": not self.duplicate}


def _ready_gap(ledger: GapLedger, gap_id_suffix: str = "a") -> str:
    record, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject=None,
        representative_question=f"docs for thing-{gap_id_suffix}",
        identity_override=f"unknown:thing {gap_id_suffix}",
        source_candidates=[{"url": "https://docs.example/thing", "authority": "official"}],
    )
    ledger.transition(record.gap_id, "ready_for_curation", reason="seeds gathered")
    return record.gap_id


def _dispatcher(tmp_path: Path, transport) -> GapDispatcher:
    ledger = GapLedger(tmp_path / "gaps")
    return GapDispatcher(ledger, transport=transport)


def test_dry_run_until_curator_ready(tmp_path):
    ledger = GapLedger(tmp_path / "gaps")
    dispatcher = GapDispatcher(ledger, transport=FakeTransport())
    gap_id = _ready_gap(ledger)
    outcome = dispatcher.dispatch(gap_id)
    assert outcome.action == "dry-run"
    assert ledger.load(gap_id).status == "awaiting_curator_readiness"
    assert ledger.spool_list()  # payload staged durably
    assert not ledger.load(gap_id).job_links


def test_readiness_flag_permits_real_submission(tmp_path):
    ledger = GapLedger(tmp_path / "gaps")
    transport = FakeTransport()
    dispatcher = GapDispatcher(ledger, transport=transport)
    (tmp_path / "gaps").mkdir(parents=True, exist_ok=True)
    (tmp_path / "gaps" / "curator-ready").write_text("ready\n")
    gap_id = _ready_gap(ledger)
    outcome = dispatcher.dispatch(gap_id)
    assert outcome.action == "submitted"
    assert outcome.job_id
    assert ledger.load(gap_id).status == "curating"
    payload = transport.calls[0]["payload"]
    assert payload["limits"]["max_model_calls"] == MAX_MODEL_CALLS_PER_JOB
    assert payload["submitted_by"] == "doclayer-gap-dispatcher"


def test_idempotency_key_stable_across_retries(tmp_path):
    key = attempt_key("gap-x", 1, ["https://a", "https://b"])
    assert key == attempt_key("gap-x", 1, ["https://b", "https://a"])
    assert key != attempt_key("gap-x", 2, ["https://a", "https://b"])


def test_daily_budget_three_jobs_and_exhaustion_preserves_work(tmp_path):
    ledger = GapLedger(tmp_path / "gaps")
    dispatcher = GapDispatcher(ledger, transport=FakeTransport(), max_calls_day=100, jobs_per_day=3)
    (tmp_path / "gaps").mkdir(parents=True, exist_ok=True)
    (tmp_path / "gaps" / "curator-ready").write_text("ready\n")
    dispatched = []
    for index in range(100 // MAX_MODEL_CALLS_PER_JOB + 1):
        gap_id = _ready_gap(ledger, f"thing{index}")
        outcome = dispatcher.dispatch(gap_id)
        dispatched.append(outcome.action)
        if outcome.job_id:
            dispatcher.observe(gap_id, {"job_id": outcome.job_id, "status": "cancelled"})
    # 100 calls/day at 50 per job: two submitted, further needs deferred
    submitted = dispatched.count("submitted")
    assert submitted == 100 // MAX_MODEL_CALLS_PER_JOB
    assert "skipped" in dispatched
    deferred = [r for r in ledger.list_records() if r.status == "retry_wait"]
    assert deferred and all(r.backoff_until for r in deferred)


def test_transport_timeout_keeps_reservation_and_retry_is_idempotent(tmp_path):
    ledger = GapLedger(tmp_path / "gaps")
    (tmp_path / "gaps").mkdir(parents=True, exist_ok=True)
    (tmp_path / "gaps" / "curator-ready").write_text("ready\n")
    failing = FakeTransport(fail=True)
    dispatcher = GapDispatcher(ledger, transport=failing)
    gap_id = _ready_gap(ledger)
    outcome = dispatcher.dispatch(gap_id)
    assert outcome.action == "skipped" and "transport error" in outcome.detail
    assert ledger.load(gap_id).status == "retry_wait"
    # the attempt may have succeeded despite the timeout: reservation kept
    assert DayLedger(tmp_path / "gaps").reserved_calls() == MAX_MODEL_CALLS_PER_JOB

    # retry with the same key returns the same job; a duplicate releases
    succeeding = FakeTransport(duplicate=True)
    dispatcher2 = GapDispatcher(ledger, transport=succeeding)
    ledger.transition(gap_id, "ready_for_curation", reason="retry now")
    outcome = dispatcher2.dispatch(gap_id)
    assert outcome.action == "submitted" and outcome.detail == "idempotent duplicate"
    # A duplicate confirms the uncertain original job. It cannot prove any
    # allowance unused: one immutable reservation remains, not a new one.
    assert DayLedger(tmp_path / "gaps").reserved_calls() == MAX_MODEL_CALLS_PER_JOB


def test_manual_pause_disables_submissions_not_capture(tmp_path):
    ledger = GapLedger(tmp_path / "gaps")
    dispatcher = GapDispatcher(ledger, transport=FakeTransport())
    gap_id = _ready_gap(ledger)
    dispatcher.set_paused(True)
    assert dispatcher.dispatch(gap_id).action == "paused"
    # capture still works
    ledger.submit_event(
        need_class="missing_coverage",
        subject="other",
        representative_question="more docs?",
    )
    assert len(ledger.list_records()) == 2
    dispatcher.set_paused(False)
    assert dispatcher.paused() is False


def test_day_ledger_release_only_proven_unused(tmp_path):
    day = DayLedger(tmp_path / "gaps")
    assert day.reserve("gap-a", MAX_MODEL_CALLS_PER_JOB)
    assert day.reserved_calls() == MAX_MODEL_CALLS_PER_JOB
    assert day.release("gap-b", proven_unused=True) == 0  # nothing reserved for gap-b
    assert day.release("gap-a", proven_unused=True) == MAX_MODEL_CALLS_PER_JOB
    assert day.reserved_calls() == 0


def test_daily_call_ceiling_enforced(tmp_path):
    day = ResourceBudget(tmp_path / "gaps", max_calls_day=100, max_jobs=3)
    reserved = []
    for index in range(100 // MAX_MODEL_CALLS_PER_JOB + 2):
        reserved.append(day.reserve_attempt(f"attempt-{index}", f"gap-{index}", MAX_MODEL_CALLS_PER_JOB) is not None)
    assert sum(reserved) <= 100
    assert not any(reserved[100 // MAX_MODEL_CALLS_PER_JOB + 1 :])


def test_new_payload_leads_with_the_actual_question(tmp_path):
    ledger = GapLedger(tmp_path / "gaps")
    gap_id = _ready_gap(ledger)
    record = ledger.load(gap_id)
    payload = GapDispatcher(ledger, transport=FakeTransport()).build_payload(record, ["https://docs.example/thing"], 1)
    assert payload["intent"].startswith(f"Documentation question: {record.representative_question}\n")
    assert "leave unsupported parts unresolved" in payload["intent"]
    assert payload["intent"].index("Documentation question:") < payload["intent"].index("Tracking:")
