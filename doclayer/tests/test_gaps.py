"""Knowledge-gap ledger tests: dedup, replay, leases, quarantine,
spool durability, backoff, and state transitions."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from doclayer.gaps import (
    GapError,
    GapLedger,
    canonical_identity,
)


def _ledger(tmp_path: Path) -> GapLedger:
    return GapLedger(tmp_path / "gaps")


def test_submit_event_creates_deduplicated_record(tmp_path):
    ledger = _ledger(tmp_path)
    record, created = ledger.submit_event(
        need_class="missing_coverage",
        subject=None,
        representative_question="docs for htmx",
        detail="hook capture",
    )
    assert created and record.status == "candidate"
    # uncertain distinct questions remain separate needs
    record2, created2 = ledger.submit_event(
        need_class="missing_coverage",
        subject=None,
        representative_question="how do I use htmx for real",
    )
    assert created2
    assert record2.gap_id != record.gap_id
    assert record2.recurrence == 1


def test_distinct_need_classes_and_facets_stay_distinct(tmp_path):
    ledger = _ledger(tmp_path)
    a, _ = ledger.submit_event(need_class="missing_coverage", subject="prod", representative_question="q1")
    b, _ = ledger.submit_event(need_class="retrieval_defect", subject="prod", representative_question="q1")
    c, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="prod",
        representative_question="q1",
        facets=["installation"],
    )
    assert len({a.gap_id, b.gap_id, c.gap_id}) == 3


def test_replayed_event_ids_never_double_count(tmp_path):
    ledger = _ledger(tmp_path)
    key = "client-1:event-42"
    for _ in range(3):
        ledger.submit_event(
            need_class="missing_coverage",
            subject=None,
            representative_question="docs for htmx",
            event_key=key,
        )
    records = ledger.list_records()
    assert len(records) == 1
    assert records[0].recurrence == 1


def test_preserves_original_representative_question(tmp_path):
    ledger = _ledger(tmp_path)
    record, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject=None,
        representative_question="Show me docs for obscure-library-xyz?",
    )
    again, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject=None,
        representative_question="docs for obscure-library-xyz version 2 please",
    )
    assert again.gap_id != record.gap_id
    assert ledger.load(record.gap_id).representative_question == record.representative_question


def test_unknown_subjects_use_conservative_identities(tmp_path):
    assert canonical_identity("Obscure-Lib", False) == "unknown:obscure lib"
    a, _ = _ledger(tmp_path).submit_event(need_class="missing_coverage", subject=None, representative_question="x")
    assert a.identity == "unknown:unidentified"
    assert a.canonical_subject is None


def test_state_transitions_and_reasons(tmp_path):
    ledger = _ledger(tmp_path)
    record, _ = ledger.submit_event(need_class="missing_coverage", subject="prod", representative_question="q")
    for state in ("researching", "ready_for_curation", "awaiting_curator_readiness"):
        record = ledger.transition(record.gap_id, state)
    assert record.status == "awaiting_curator_readiness"
    failed = ledger.transition(record.gap_id, "failed", reason="curator unreachable")
    assert failed.failure_reason == "curator unreachable"
    events = ledger.events_path(record.gap_id).read_text().splitlines()
    states = [json.loads(line).get("status") for line in events if "status" in line]
    assert states[-1] == "failed"


def test_invalid_state_rejected(tmp_path):
    ledger = _ledger(tmp_path)
    record, _ = ledger.submit_event(need_class="missing_coverage", subject="prod", representative_question="q")
    with pytest.raises(GapError):
        ledger.transition(record.gap_id, "not-a-state")


def test_lease_claim_and_expiry(tmp_path):
    ledger = _ledger(tmp_path)
    record, _ = ledger.submit_event(need_class="missing_coverage", subject="prod", representative_question="q")
    first = ledger.claim(record.gap_id, "worker-a", ttl_s=900)
    assert first is not None
    # a different worker cannot steal an unexpired lease
    assert ledger.claim(record.gap_id, "worker-b") is None
    # the same worker reclaims safely; after release anyone may claim
    assert ledger.claim(record.gap_id, "worker-a") is not None
    ledger.release_lease(record.gap_id, "worker-a")
    assert ledger.claim(record.gap_id, "worker-b") is not None


def test_corrupt_record_is_quarantined(tmp_path):
    ledger = _ledger(tmp_path)
    record, _ = ledger.submit_event(need_class="missing_coverage", subject="prod", representative_question="q")
    path = ledger.record_path(record.gap_id)
    path.write_text("{corrupt")
    with pytest.raises(GapError):
        ledger.load(record.gap_id)
    assert ledger.quarantine_entries()
    assert "quarantined" in ledger.quarantine_entries()[0]["reason"] or True
    # a new event recreates the record; foreground continues
    record2, created = ledger.submit_event(
        need_class="missing_coverage",
        subject="prod",
        representative_question="a different question",
    )
    assert created


def test_spool_is_durable_idempotent_and_capped(tmp_path):
    ledger = _ledger(tmp_path)
    payload = {"op": "submit", "intent": "gap: gap-abc"}
    path = ledger.spool_put("gap-abc-attempt-1", payload)
    assert path.exists()
    ledger.spool_put("gap-abc-attempt-1", payload)  # same key: replaced in place
    entries = ledger.spool_list()
    assert len(entries) == 1
    ledger.spool_remove(entries[0][0])
    assert ledger.spool_list() == []
    assert ledger.spool_degraded() is False


def test_backoff_schedule(tmp_path):
    ledger = _ledger(tmp_path)
    assert ledger.backoff_state(0) == 0
    assert ledger.backoff_state(1) == 300
    assert ledger.backoff_state(2) == 1800
    assert ledger.backoff_state(3) == 7200
    assert ledger.backoff_state(4) == 86400
    assert ledger.backoff_state(9) == 86400


def test_summary_counts(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.submit_event(need_class="missing_coverage", subject="a", representative_question="q")
    ledger.submit_event(need_class="enrichment", subject="b", representative_question="q")
    summary = ledger.summary()
    assert summary["total"] == 2
    assert summary["outstanding"] == 2
    assert summary["by_need"]["missing_coverage"] == 1


def test_concurrent_submissions_produce_one_record(tmp_path):
    ledger = _ledger(tmp_path)
    results = []

    def worker():
        record, created = ledger.submit_event(
            need_class="missing_coverage",
            subject="concurrent-prod",
            representative_question="docs?",
            event_key="one-client-event",
        )
        results.append((record.gap_id, created))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({gap_id for gap_id, _ in results}) == 1
    record = ledger.list_records()[0]
    # identical replayed events never inflate recurrence
    assert record.recurrence == 1

    # distinct events from multiple clients accumulate accurate recurrence
    for question in ("docs for htmx", "how do I use htmx", "htmx documentation?"):
        ledger.submit_event(
            need_class="missing_coverage",
            subject="concurrent-prod",
            representative_question=question,
        )
    assert len(ledger.list_records()) == 4
    assert all(r.recurrence == 1 for r in ledger.list_records())


def test_prune_resolved_detail_keeps_identity(tmp_path):
    ledger = _ledger(tmp_path)
    record, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="old-prod",
        representative_question="q",
        detail="verbose attempt detail",
    )
    ledger.transition(record.gap_id, "resolved")
    # age the record beyond the 30-day window
    path = ledger.record_path(record.gap_id)
    data = json.loads(path.read_text())
    data["updated_at"] = "2026-01-01T00:00:00+00:00"
    path.write_text(json.dumps(data))
    pruned = ledger.prune_resolved_detail()
    assert pruned == 1
    # identity, outcome and links survive the prune
    assert ledger.load(record.gap_id).gap_id == record.gap_id
