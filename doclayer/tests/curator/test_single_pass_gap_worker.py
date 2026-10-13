"""A single-pass job whose only page was superseded by an operator edit ends
`skipped`, so the job is partial or covered and the gap worker verifies the
gap against the canonical page instead of failing it."""

from __future__ import annotations

from types import SimpleNamespace

from test_pipeline import run_pipeline
from test_single_pass_pipeline import (
    PAGES,
    TARGET,
    frame_of,
    push_canonical_edit,
    sp_job,
)

from doclayer import gap_worker
from doclayer.curator.ops import handle
from doclayer.dispatcher import GapDispatcher
from doclayer.gaps import GapLedger


def test_gap_worker_verifies_a_superseded_single_pass_job(
    cfg, store, qwen, checkout, origin, forgejo, tmp_path, monkeypatch
):
    def ground_after_operator_edit(user):
        push_canonical_edit(
            tmp_path,
            origin,
            TARGET,
            "# Existing model\n\n",
            "# Existing model\n\nOperator introduction written after drafting.\n\n",
        )
        return {
            "verdicts": [
                {"id": row["id"], "supported": True, "reason": "ok"} for row in frame_of(user)["contributions"]
            ]
        }

    qwen.overrides["sp_ground"] = [ground_after_operator_edit]
    state = run_pipeline(cfg, store, qwen, PAGES, sp_job(store, checkout), forgejo)
    assert state.changes[0].status == "skipped"
    reply = handle("status", {"job_id": state.job_id}, cfg, store)
    assert reply["job"]["status"] in ("partial", "covered")

    ledger = GapLedger(tmp_path / "gaps")
    row, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="test-model",
        representative_question="What is the test model's maximum output?",
    )

    def submitted(record):
        record.job_links = [{"job_id": state.job_id, "attempt": 1}]
        record.attempts = 1
        record.status = "curating"

    ledger.update(row.gap_id, submitted)

    class StatusOnly:
        def call(self, operation, payload, **kwargs):
            assert operation == "status" and payload == {"job_id": state.job_id}
            return reply

    verified = []

    def verify(gap_id, owner):
        verified.append(gap_id)
        return SimpleNamespace(outcome="resolved", notes=[])

    monkeypatch.setattr(gap_worker.verification, "verify_gap", verify)
    notes = []
    gap_worker._verify_published(ledger, GapDispatcher(ledger, transport=StatusOnly()), notes)
    assert verified == [row.gap_id]
    assert ledger.load(row.gap_id).status != "failed"
    assert notes == [f"verification attempted for {row.gap_id}"]
