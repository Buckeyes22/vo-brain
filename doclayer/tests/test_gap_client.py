import json
from unittest.mock import Mock

import pytest

from doclayer.gap_client import GapClient
from doclayer.settings import Settings


def client(tmp_path):
    cfg = Settings()
    cfg.gap_owner = "ssh"
    cfg.gap_state_dir = tmp_path
    return GapClient(cfg)


def event():
    return dict(
        need_class="missing_coverage",
        subject=None,
        representative_question="docs for widgets",
        event_key="evt-one",
    )


def test_hook_capture_does_not_contact_owner(tmp_path, monkeypatch):
    c = client(tmp_path)
    remote = Mock(side_effect=AssertionError("hook network"))
    monkeypatch.setattr(c, "operation", remote)
    receipt, _ = c.submit_event(defer=True, **event())
    assert receipt.status == "pending_delivery"
    remote.assert_not_called()
    assert len(list(c.outbox.glob("*.json"))) == 1


def test_offline_then_lost_reply_preserves_event_identity(tmp_path, monkeypatch):
    c = client(tmp_path)
    seen = []

    def remote(op, payload):
        seen.append(payload["event_key"])
        if len(seen) < 3:
            raise RuntimeError("lost response")
        return {
            "ok": True,
            "created": False,
            "record": {
                "gap_id": "gap-one",
                "status": "candidate",
                "recurrence": 1,
                "need_class": "missing_coverage",
            },
        }

    monkeypatch.setattr(c, "operation", remote)
    c.submit_event(**event())
    assert c.flush()["remaining"] == 1
    assert c.flush()["remaining"] == 0
    assert seen == ["evt-one"] * 3


def test_local_event_and_remote_event_use_real_same_contract(tmp_path):
    from doclayer.gap_ops import handle
    from doclayer.gaps import GapLedger

    c = client(tmp_path / "client")
    ledger = GapLedger(tmp_path / "owner")
    c.operation = lambda op, payload: handle(op, payload, ledger)
    a, _ = c.submit_event(**event())
    b, created = c.submit_event(**event())
    assert a.gap_id == b.gap_id
    assert not created and b.recurrence == 1


def test_full_outbox_is_visible_and_bounded(tmp_path, monkeypatch):
    c = client(tmp_path)
    monkeypatch.setattr("doclayer.gap_client.OUTBOX_LIMIT", 1)
    with pytest.raises(RuntimeError, match="outbox is full"):
        c.submit_event(defer=True, **event())
    assert (c.outbox / "capture-degraded").exists()
    assert not list(c.outbox.glob("*.json"))


def test_attach_to_same_gap_updates_actual_record(tmp_path):
    from doclayer.gap_ops import handle
    from doclayer.gaps import GapLedger

    ledger = GapLedger(tmp_path)
    record, _ = ledger.submit_event(**event())
    reply = handle(
        "attach_source",
        {
            "gap_id": record.gap_id,
            "source": {
                "url": "https://docs.example.com/widgets",
                "authority": "official",
            },
        },
        ledger,
    )
    assert reply["record"]["source_candidates"][0]["authority"] == "official"


def test_unicode_request_cap_includes_ascii_wire_envelope(tmp_path):
    c = client(tmp_path)
    with pytest.raises(ValueError, match="bounded technical"):
        c.submit_event(defer=True, **(event() | {"detail": "界" * 3000}))
    assert not list(c.outbox.glob("*.json"))


def test_outbox_cap_measures_exact_persisted_unicode(tmp_path, monkeypatch):
    c = client(tmp_path)
    request = event() | {"detail": "界" * 100}
    exact = len((json.dumps(request, indent=2) + "\n").encode())
    monkeypatch.setattr("doclayer.gap_client.OUTBOX_LIMIT", exact - 1)
    with pytest.raises(RuntimeError, match="outbox is full"):
        c.submit_event(defer=True, **request)
    assert not list(c.outbox.glob("*.json"))


def test_none_event_id_is_materialized_once_before_delivery(tmp_path, monkeypatch):
    c = client(tmp_path)
    seen = []

    def offline(op, payload):
        seen.append(payload["event_key"])
        raise RuntimeError("offline")

    monkeypatch.setattr(c, "operation", offline)
    c.submit_event(**(event() | {"event_key": None}))
    c.flush()
    assert seen[0] and seen[0] == seen[1]
    assert len(list(c.outbox.glob("*.json"))) == 1


@pytest.mark.parametrize("operation", ["retry", "suppress"])
def test_ops_cannot_strand_uncertain_remote_attempt(tmp_path, operation):
    from doclayer.gap_ops import handle
    from doclayer.gaps import GapLedger

    ledger = GapLedger(tmp_path)
    row, _ = ledger.submit_event(**event())
    ledger.update(
        row.gap_id,
        lambda r: setattr(
            r,
            "submission",
            {"state": "uncertain", "attempt_id": "gap-attempt-original"},
        ),
    )
    with pytest.raises(ValueError, match="existing attempt"):
        handle(operation, {"gap_id": row.gap_id}, ledger)
    assert ledger.load(row.gap_id).submission["state"] == "uncertain"


def test_manual_retry_preserves_attempt_and_requeues_staged_payload(tmp_path):
    from doclayer.gap_ops import handle
    from doclayer.gaps import GapLedger

    ledger = GapLedger(tmp_path)
    row, _ = ledger.submit_event(**event())
    staged = {"state": "staged", "attempt_id": "original", "payload": {"urls": ["https://docs.example/"]}}
    ledger.update(row.gap_id, lambda r: setattr(r, "submission", staged))
    handle("retry", {"gap_id": row.gap_id}, ledger)
    current = ledger.load(row.gap_id)
    assert current.status == "queued" and current.submission == staged
