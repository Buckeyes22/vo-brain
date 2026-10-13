"""CLI operations tests: status/list/inspect/retry/suppress/
pause/resume/digest operate on a state root and never touch real user state."""

from __future__ import annotations

import json

import pytest

from doclayer.gaps import GapLedger
from doclayer.gaps_cli import main


@pytest.fixture
def state(tmp_path):
    ledger = GapLedger(tmp_path / "gaps")
    record, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject=None,
        representative_question="docs for widget-thing",
        identity_override="unknown:widget thing",
    )
    return tmp_path, ledger, record.gap_id


def _main(args, tmp_path):
    return main(["--state-dir", str(tmp_path / "gaps"), *args])


def test_status_reports_summary(state, capsys):
    tmp_path, ledger, gap_id = state
    assert _main(["status"], tmp_path) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["total"] == 1
    assert data["outstanding"] == 1
    assert "curator_ready" in data and "dispatch_paused" in data


def test_list_and_inspect(state, capsys):
    tmp_path, ledger, gap_id = state
    assert _main(["list"], tmp_path) == 0
    assert gap_id in capsys.readouterr().out
    assert _main(["inspect", gap_id], tmp_path) == 0
    out = capsys.readouterr().out
    assert gap_id in out and "event trail" in out


def test_retry_clears_backoff(state):
    tmp_path, ledger, gap_id = state
    ledger.transition(
        gap_id,
        "retry_wait",
        reason="backoff",
        backoff_until="2030-01-01T00:00:00+00:00",
    )
    assert _main(["retry", gap_id], tmp_path) == 0
    record = ledger.load(gap_id)
    assert record.status == "candidate"
    assert record.backoff_until is None
    assert record.attempts == 0


def test_suppress_is_not_deletion(state):
    tmp_path, ledger, gap_id = state
    assert _main(["suppress", gap_id, "--reason", "noisy need"], tmp_path) == 0
    record = ledger.load(gap_id)
    assert record.status == "suppressed"
    assert record.suppressed_reason == "noisy need"
    # evidence and record are retained
    assert ledger.record_path(gap_id).exists()


def test_pause_and_resume(state):
    tmp_path, ledger, gap_id = state
    assert _main(["dispatch-pause"], tmp_path) == 0
    assert (tmp_path / "gaps" / "dispatch-paused").exists()
    assert _main(["dispatch-resume"], tmp_path) == 0
    assert not (tmp_path / "gaps" / "dispatch-paused").exists()


def test_digest_runs_one_cycle(state, capsys):
    tmp_path, ledger, gap_id = state
    assert _main(["digest"], tmp_path) == 0
    out = capsys.readouterr().out
    assert "knowledge gaps" in out
    assert "dry-run" in out  # readiness not proven: visible in the digest
