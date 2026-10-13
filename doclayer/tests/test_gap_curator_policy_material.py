"""Incomplete published private answers return to collection; failed proofs stay outstanding."""

from types import SimpleNamespace

import pytest
from local_sources import local_source


@pytest.fixture(autouse=True)
def _fictional_profiles(local_profiles):
    local_profiles()


def need():
    return SimpleNamespace(
        canonical_subject="brain",
        identity="brain",
        representative_question="Which model handles wiki curation and what happens if it is unavailable?",
        source_candidates=[],
    )


@pytest.mark.parametrize("outcome", ["partial", "failed"])
def test_incomplete_published_private_answer_requires_fresh_collection(tmp_path, monkeypatch, outcome):
    from copy import deepcopy

    from doclayer import gap_worker
    from doclayer.dispatcher import GapDispatcher
    from doclayer.gaps import GapLedger

    ledger = GapLedger(tmp_path / "gaps")
    row, _ = ledger.submit_event(
        need_class="needs_local_evidence",
        subject="brain",
        representative_question=need().representative_question,
        source_candidates=[local_source("Old runbook", "Backfill retains old attempts.")],
    )
    job = {
        "job_id": "job-30f6d5680a",
        "status": "published",
        "usage": {"model_calls": 28},
    }
    receipt = {
        "state": "terminal",
        "job_id": job["job_id"],
        "reservation": {"calls": 50},
        "usage": job["usage"],
    }

    def published(r):
        r.job_links = [{"job_id": job["job_id"], "attempt": 1}]
        r.attempts = 1
        r.submission = deepcopy(receipt)
        r.submission_history = [{"old_attempt": "immutable"}]
        r.status = "awaiting_index"

    ledger.update(row.gap_id, published)
    before = ledger.load(row.gap_id)
    calls = []

    class StatusOnly:
        def call(self, operation, payload, **kwargs):
            calls.append((operation, payload))
            assert operation == "status" and payload == {"job_id": job["job_id"]}
            return {"ok": True, "job": deepcopy(job)}

    proof = {"outcome": outcome, "missing": ["configured model", "outage behavior"]}

    def verify(gap_id, owner):
        def retain(r):
            r.resolution_verification = deepcopy(proof)
            r.status = "ready_for_curation"

        owner.update(gap_id, retain)
        return SimpleNamespace(outcome=outcome)

    monkeypatch.setattr(gap_worker.verification, "verify_gap", verify)
    notes = []
    gap_worker._verify_published(ledger, GapDispatcher(ledger, transport=StatusOnly()), notes)
    after = ledger.load(row.gap_id)
    assert after.status == "candidate"
    assert after.resolution_verification == proof
    assert after.source_candidates == before.source_candidates
    assert after.submission == before.submission == receipt
    assert after.job_links == before.job_links and after.attempts == 1
    assert after.submission_history == before.submission_history
    assert calls == [("status", {"job_id": job["job_id"]})]
    assert notes == [f"verification attempted for {row.gap_id}"]


def test_failed_answer_proof_remains_an_outstanding_need(tmp_path):
    from doclayer.gaps import GapLedger

    ledger = GapLedger(tmp_path / "gaps")
    row, _ = ledger.submit_event(
        need_class="needs_local_evidence",
        subject="brain",
        representative_question=need().representative_question,
    )
    ledger.transition(row.gap_id, "failed", reason="Published page does not answer the question")
    assert ledger.summary()["outstanding"] == 1
    assert ledger.summary()["by_status"]["failed"] == 1
