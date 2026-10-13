"""Direct-authoring routing and profile separation."""

from types import SimpleNamespace

import pytest

from doclayer import gap_worker
from doclayer.curator.transport import Transport
from doclayer.dispatcher import GapDispatcher
from doclayer.gaps import GapLedger


@pytest.fixture(autouse=True)
def _fictional_profiles(local_profiles):
    local_profiles()


class RecordingTransport(Transport):
    def __init__(self):
        self.calls = []

    def call(self, op, payload, **kwargs):
        self.calls.append((op, payload))
        return {"ok": True, "job_id": "job-1234567890", "created": True}


def record(ledger, question="Where is the worker1 brain curator installed?"):
    row, _ = ledger.submit_event(
        need_class="needs_local_evidence",
        subject="brain",
        representative_question=question,
    )
    return row


def test_local_need_routes_to_direct_authoring_without_collection_or_curator(tmp_path, monkeypatch):
    from doclayer.local_material import DIRECT_AUTHORING_REASON

    ledger = GapLedger(tmp_path)
    row = record(ledger)
    monkeypatch.setattr(
        gap_worker,
        "discover",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("public discovery")),
    )
    notes = []
    gap_worker._research_one(ledger, row, SimpleNamespace(subjects={}), {}, notes)
    current = ledger.load(row.gap_id)
    assert current.status == "needs_local_evidence"
    assert current.backoff_until
    assert DIRECT_AUTHORING_REASON in ledger.events_path(row.gap_id).read_text()
    assert not current.job_links and not current.source_candidates
    (tmp_path / "curator-ready").write_text("ready\n")
    transport = RecordingTransport()
    ledger.transition(row.gap_id, "ready_for_curation")
    outcome = GapDispatcher(ledger, transport=transport).dispatch(row.gap_id)
    assert outcome.action == "skipped" and outcome.detail == DIRECT_AUTHORING_REASON
    assert transport.calls == []


def test_desktop_and_metrics_are_separate_profiles(tmp_path):
    from doclayer.local_material import is_local_need, local_profile

    ledger = GapLedger(tmp_path)
    desktop = record(
        ledger,
        "Which on-screen keyboard applications and AT-SPI test routes are installed on the workstation?",
    )
    desktop.identity = "unknown:desktop"
    assert local_profile(desktop) == "desktop-osk"
    assert is_local_need(desktop)
    other = record(ledger, "What is the installed metrics deployment on svc1?")
    other.identity = "unknown:metrics"
    assert is_local_need(other)
    assert local_profile(other) == "metrics"
