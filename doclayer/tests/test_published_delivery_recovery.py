"""Published delivery defects retain the owning publication verification path."""

from types import SimpleNamespace

import pytest
from test_gap_worker import FakeCuratorTransport
from test_gap_worker import env as env

from doclayer import gap_worker
from doclayer.dispatcher import GapDispatcher


@pytest.mark.parametrize("outcome", ["resolved", "retrieval_defect", "verifying"])
def test_parked_publication_rechecks_delivery_without_research_or_new_attempt(env, monkeypatch, outcome):
    _, ledger, _ = env
    row, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="unidentified",
        representative_question="How does Gluetun use a custom OpenVPN profile?",
    )
    jobs = [
        {"job_id": "job-old1", "attempt": 1},
        {"job_id": "job-old2", "attempt": 2},
        {"job_id": "job-fake0001", "attempt": 3},
    ]

    def parked(r):
        r.status = "retry_wait"
        r.attempts = 3
        r.job_links = jobs
        r.submission = {
            "state": "terminal",
            "job_id": "job-fake0001",
            "remote_status": "published",
            "reservation": {"calls": 50},
            "payload": {"immutable": True},
        }
        r.resolution_verification = {
            "outcome": "retrieval_defect",
            "publication_commit": "c" * 40,
        }
        r.backoff_until = "2099-01-01T00:00:00+00:00"

    ledger.update(row.gap_id, parked)
    before = ledger.load(row.gap_id)

    class PublishedTransport(FakeCuratorTransport):
        def call(self, op, payload, **kwargs):
            reply = super().call(op, payload, **kwargs)
            return {
                "ok": True,
                "job": {
                    "job_id": "job-fake0001",
                    "status": "published",
                    "publication": reply["publication"],
                },
            }

    transport = PublishedTransport([])
    monkeypatch.setattr(
        gap_worker,
        "GapDispatcher",
        lambda owner, **kw: GapDispatcher(owner, transport=transport, **kw),
    )
    checks = []

    def verify(gap_id, owner):
        checks.append((gap_id, owner.load(gap_id).job_links[-1]["job_id"]))
        owner.transition(gap_id, outcome)
        return SimpleNamespace(outcome=outcome, notes=["fresh full-bound publication check"])

    monkeypatch.setattr(gap_worker.verification, "verify_gap", verify)

    def forbidden(*args, **kwargs):
        raise AssertionError("published delivery defect must not research or verify a different candidate")

    monkeypatch.setattr(gap_worker, "discover", forbidden)
    monkeypatch.setattr(gap_worker.verification, "verify_existing_gap", forbidden)
    gap_worker.run_cycle(ledger)
    after = ledger.load(row.gap_id)
    assert checks == [(row.gap_id, "job-fake0001")]
    assert after.status == outcome
    assert after.attempts == 3 and after.job_links == jobs
    assert after.submission == before.submission
    assert all(call["op"] != "submit" for call in transport.calls)
    if outcome == "retrieval_defect":
        gap_worker.run_cycle(ledger)
        assert len(checks) == 1  # Durable delivery backoff; no duplicate judgment.
