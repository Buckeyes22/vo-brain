"""Suppressed gaps stay suppressed under verify; idle verifying gaps can be suppressed."""

import pytest
from test_verification_contract import published  # noqa: F401

from doclayer import gaps_cli, verification
from doclayer.gap_ops import handle
from doclayer.gaps import GapLedger


def _gap(ledger, question="How is dispatch-cli installed?"):
    row, _ = ledger.submit_event(
        need_class="missing_coverage", subject="dispatch-cli", representative_question=question
    )
    return row.gap_id


# -- verify --existing never lifts suppression ----------------------------------


def test_cli_verify_existing_refuses_suppressed_gap(tmp_path, monkeypatch, capsys):
    ledger = GapLedger(tmp_path)
    gid = _gap(ledger)
    handle("suppress", {"gap_id": gid, "reason": "operator call"}, ledger)
    before = ledger.load(gid).to_dict()
    called = []
    monkeypatch.setattr(verification, "verify_existing_gap", lambda *a, **k: called.append(a))
    assert gaps_cli.main(["--state-dir", str(tmp_path), "verify", gid, "--existing"]) == 2
    err = capsys.readouterr().err
    assert "status is suppressed" in err and "the record was not changed" in err
    assert called == []
    assert ledger.load(gid).to_dict() == before


@pytest.mark.parametrize("closed", ["suppressed", "superseded"])
def test_verify_existing_gap_does_not_reopen_suppressed_or_superseded_gap(tmp_path, closed):
    ledger = GapLedger(tmp_path)
    gid = _gap(ledger)
    if closed == "superseded":
        other = _gap(ledger, "How is dispatch-cli installed and authenticated?")
        handle("supersede", {"gap_id": gid, "by": other}, ledger)
    else:
        ledger.transition(gid, closed, reason="operator call")
    before = ledger.load(gid).to_dict()
    result = verification.verify_existing_gap(gid, ledger)
    assert result.outcome == closed
    after = ledger.load(gid).to_dict()
    assert after == before and after["status"] == closed
    assert after["suppressed_reason"] == before["suppressed_reason"]


def test_cli_verify_existing_still_runs_for_verifying_gap(tmp_path, monkeypatch):
    ledger = GapLedger(tmp_path)
    gid = _gap(ledger)
    ledger.transition(gid, "verifying")
    seen = []

    def fake(gap_id, led):
        seen.append(gap_id)
        return type("R", (), {"outcome": "resolved"})()

    monkeypatch.setattr(verification, "verify_existing_gap", fake)
    assert gaps_cli.main(["--state-dir", str(tmp_path), "verify", gid, "--existing"]) == 0
    assert seen == [gid]


# -- suppress on a verifying gap ------------------------------------------------


def test_suppress_idle_verifying_gap_without_job_or_lease(tmp_path):
    ledger = GapLedger(tmp_path)
    gid = _gap(ledger)
    ledger.transition(gid, "verifying")
    assert ledger.load(gid).lease is None and not ledger.load(gid).job_links
    out = handle("suppress", {"gap_id": gid, "reason": "no longer wanted"}, ledger)
    record = ledger.load(gid)
    assert out["ok"] is True
    assert record.status == "suppressed" and record.suppressed_reason == "no longer wanted"


def test_suppress_verifying_gap_with_terminal_job(tmp_path):
    ledger = GapLedger(tmp_path)
    gid = _gap(ledger)
    ledger.update(
        gid,
        lambda r: (
            r.job_links.append({"job_id": "job-1"}),
            r.submission.update(state="terminal", job_id="job-1", remote_status="published"),
        ),
    )
    ledger.transition(gid, "verifying")
    handle("suppress", {"gap_id": gid}, ledger)
    assert ledger.load(gid).status == "suppressed"


def test_suppress_refuses_verifying_gap_holding_a_lease(tmp_path):
    ledger = GapLedger(tmp_path)
    gid = _gap(ledger)
    ledger.transition(gid, "verifying")
    assert ledger.claim(gid, "worker-a")
    with pytest.raises(ValueError, match="active gap"):
        handle("suppress", {"gap_id": gid}, ledger)
    assert ledger.load(gid).status == "verifying"


@pytest.mark.parametrize("state", [None, "staged", "accepted", "uncertain"])
def test_suppress_refuses_verifying_gap_with_in_flight_job(tmp_path, state):
    ledger = GapLedger(tmp_path)
    gid = _gap(ledger)
    ledger.update(
        gid,
        lambda r: (
            r.job_links.append({"job_id": "job-1"}),
            r.submission.update(state=state, job_id="job-1") if state else None,
        ),
    )
    ledger.transition(gid, "verifying")
    with pytest.raises(ValueError, match="active gap"):
        handle("suppress", {"gap_id": gid}, ledger)
    assert ledger.load(gid).status == "verifying"


def test_suppress_refuses_verifying_gap_with_staged_submission_and_no_job_link(tmp_path):
    ledger = GapLedger(tmp_path)
    gid = _gap(ledger)
    ledger.update(gid, lambda r: r.submission.update(state="staged"))
    ledger.transition(gid, "verifying")
    assert not ledger.load(gid).job_links
    with pytest.raises(ValueError, match="active gap"):
        handle("suppress", {"gap_id": gid}, ledger)
    assert ledger.load(gid).status == "verifying"


@pytest.mark.parametrize("status", ["queued", "curating", "awaiting_index"])
def test_suppress_still_refuses_other_active_states(tmp_path, status):
    ledger = GapLedger(tmp_path)
    gid = _gap(ledger)
    ledger.transition(gid, status)
    with pytest.raises(ValueError, match="active gap"):
        handle("suppress", {"gap_id": gid}, ledger)
    assert ledger.load(gid).status == status


def test_retry_still_refuses_idle_verifying_gap(tmp_path):
    ledger = GapLedger(tmp_path)
    gid = _gap(ledger)
    ledger.transition(gid, "verifying")
    with pytest.raises(ValueError, match="active gap"):
        handle("retry", {"gap_id": gid}, ledger)
    assert ledger.load(gid).status == "verifying"


def test_suppressed_gap_leaves_suppression_only_through_retry(tmp_path):
    ledger = GapLedger(tmp_path)
    gid = _gap(ledger)
    handle("suppress", {"gap_id": gid, "reason": "x"}, ledger)
    assert verification.verify_existing_gap(gid, ledger).outcome == "suppressed"
    handle("retry", {"gap_id": gid}, ledger)
    assert ledger.load(gid).status == "candidate"


# -- suppress racing the worker and a running verification -------------------------


def _terminal_verifying_gap(ledger):
    gid = _gap(ledger)
    ledger.update(
        gid,
        lambda r: (
            r.job_links.append({"job_id": "job-30f6d5680a", "attempt": 1}),
            r.submission.update(state="terminal", job_id="job-30f6d5680a", remote_status="published"),
        ),
    )
    ledger.transition(gid, "verifying")
    return gid


@pytest.mark.parametrize("status", ["published", "failed"])
def test_suppress_during_worker_status_poll_is_not_overwritten(tmp_path, monkeypatch, status):
    from doclayer import gap_worker
    from doclayer.dispatcher import GapDispatcher

    ledger = GapLedger(tmp_path)
    gid = _terminal_verifying_gap(ledger)
    verified = []

    class SuppressDuringPoll:
        def call(self, operation, payload, **kwargs):
            # The operator's suppress lands between the worker's listing and its write.
            handle("suppress", {"gap_id": gid, "reason": "operator"}, ledger)
            return {"ok": True, "job": {"job_id": "job-30f6d5680a", "status": status}}

    monkeypatch.setattr(gap_worker.verification, "verify_gap", lambda g, led, **k: verified.append(g))
    notes = []
    dispatcher = GapDispatcher(ledger, transport=SuppressDuringPoll())
    gap_worker._verify_published(ledger, dispatcher, notes)
    record = ledger.load(gid)
    assert record.status == "suppressed" and record.suppressed_reason == "operator"
    assert verified == []


def test_suppress_during_in_flight_verification_is_not_overwritten(published, tmp_path):
    import httpx
    from test_verification_contract import positive

    from doclayer.curator.config import CuratorConfig
    from doclayer.curator.model import ModelClient
    from doclayer.curator.state import Usage
    from doclayer.gap_budget import ResourceBudget

    root, path, commit, ledger, record, reply, index = published
    ledger.update(
        record.gap_id,
        lambda r: r.submission.update(state="terminal", job_id=r.job_links[-1]["job_id"]),
    )
    ledger.transition(record.gap_id, "verifying")
    cfg = CuratorConfig()
    cfg.credentials_file = str(tmp_path / "no-credentials")
    cfg.inference.retries = 0

    def respond(request):
        # Suppress while the verifier is waiting on the model.
        handle("suppress", {"gap_id": record.gap_id, "reason": "operator"}, ledger)
        import json

        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(positive(path))}, "finish_reason": "stop"}]},
        )

    model = ModelClient(cfg, Usage(), transport=httpx.MockTransport(respond))
    result = verification.verify_gap(
        record.gap_id,
        ledger,
        job_result=reply,
        model_client=model,
        budget=ResourceBudget(ledger.root),
    )
    saved = ledger.load(record.gap_id)
    assert result.outcome == "suppressed"
    assert saved.status == "suppressed" and saved.suppressed_reason == "operator"
    assert not saved.resolution_verification.get("canonical_checkpoint")


def test_manual_retry_resets_the_proof_wait_backoff(tmp_path):
    ledger = GapLedger(tmp_path)
    gap_id = _gap(ledger)
    ledger.transition(gap_id, "retry_wait", backoff_until="2099-01-01T00:00:00+00:00", proof_wait_attempts=6)
    record = handle("retry", {"gap_id": gap_id}, ledger)["record"]
    assert record["proof_wait_attempts"] == 0 and record["backoff_until"] is None
