import pytest

from doclayer.gap_ops import handle
from doclayer.gaps import GapLedger


def gap(ledger, question, subject="dispatch-cli"):
    row, _ = ledger.submit_event(need_class="missing_coverage", subject=subject, representative_question=question)
    return row.gap_id


def test_supersede_marks_duplicate_and_links_primary(tmp_path):
    ledger = GapLedger(tmp_path)
    primary = gap(ledger, "How is dispatch-cli CLI installed on the workstation?")
    duplicate = gap(ledger, "How is dispatch-cli installed and authenticated here?")
    out = handle("supersede", {"gap_id": duplicate, "by": primary, "reason": "same question"}, ledger)
    record = ledger.load(duplicate)
    assert out["ok"] is True
    assert record.status == "superseded" and record.superseded_by == primary
    assert "same question" in ledger.events_path(duplicate).read_text()
    assert ledger.load(primary).status != "superseded"


def test_supersede_rejects_self_missing_and_active(tmp_path):
    ledger = GapLedger(tmp_path)
    a = gap(ledger, "Question A about dispatch-cli?")
    with pytest.raises(ValueError, match="itself"):
        handle("supersede", {"gap_id": a, "by": a}, ledger)
    missing = "gap-" + "0" * 20
    with pytest.raises(ValueError, match="unknown gap"):
        handle("supersede", {"gap_id": a, "by": missing}, ledger)
    with pytest.raises(ValueError, match="unknown gap"):
        handle("supersede", {"gap_id": missing, "by": a}, ledger)
    b = gap(ledger, "Question B about dispatch-cli?")
    ledger.transition(b, "queued")
    with pytest.raises(ValueError, match="active gap"):
        handle("supersede", {"gap_id": b, "by": a}, ledger)
    assert ledger.load(a).status != "superseded"
    assert ledger.load(b).status == "queued"


def test_cli_verify_existing_uses_existing_mode(tmp_path, monkeypatch):
    from doclayer import gaps_cli, verification

    ledger = GapLedger(tmp_path)
    gid = gap(ledger, "Question C about dispatch-cli?")
    seen = {}

    class Result:
        outcome = "resolved"

    def fake_existing(gap_id, ledger):
        seen["existing"] = gap_id
        r = Result()
        r.__dict__["outcome"] = "resolved"
        return r

    monkeypatch.setattr(verification, "verify_existing_gap", fake_existing)
    monkeypatch.setattr(gaps_cli.settings, "gap_owner", "local", raising=False)
    assert gaps_cli.main(["--state-dir", str(tmp_path), "verify", gid, "--existing"]) == 0
    assert seen == {"existing": gid}


def test_supersede_rejects_cycles_and_superseded_primary(tmp_path):
    ledger = GapLedger(tmp_path)
    a = gap(ledger, "Question A about dispatch-cli?")
    b = gap(ledger, "Question B about dispatch-cli?")
    c = gap(ledger, "Question C about dispatch-cli?")
    handle("supersede", {"gap_id": a, "by": b}, ledger)
    before = ledger.load(b).to_dict()
    with pytest.raises(ValueError, match="primary is itself superseded"):
        handle("supersede", {"gap_id": b, "by": a}, ledger)
    assert ledger.load(b).to_dict() == before
    with pytest.raises(ValueError, match="primary is itself superseded"):
        handle("supersede", {"gap_id": c, "by": a}, ledger)
    assert ledger.load(c).status != "superseded"


def test_cli_supersede_remote_forwards_by(tmp_path, monkeypatch):
    from doclayer import gap_client, gaps_cli

    calls = []

    def fake_operation(self, operation, payload):
        calls.append((operation, payload))
        return {"ok": True}

    monkeypatch.setattr(gap_client.GapClient, "operation", fake_operation)
    monkeypatch.setattr(gaps_cli.settings, "gap_owner", "ssh", raising=False)
    assert gaps_cli.main(["supersede", "gap-" + "1" * 20, "--by", "gap-" + "2" * 20]) == 0
    assert len(calls) == 1
    operation, payload = calls[0]
    assert operation == "supersede"
    assert payload["by"] == "gap-" + "2" * 20
    assert payload["gap_id"] == "gap-" + "1" * 20


@pytest.mark.parametrize(
    "status", ["superseded", "suppressed", "resolved", "researching", "queued", "curating", "awaiting_index"]
)
def test_cli_verify_existing_refuses_non_verifiable_status(tmp_path, monkeypatch, capsys, status):
    from doclayer import gaps_cli, verification

    ledger = GapLedger(tmp_path)
    primary = gap(ledger, "Primary question about dispatch-cli?")
    gid = gap(ledger, "Duplicate question about dispatch-cli?")
    if status == "superseded":
        handle("supersede", {"gap_id": gid, "by": primary}, ledger)
    elif status == "suppressed":
        ledger.transition(gid, "suppressed")
    else:
        ledger.transition(gid, status)
    if status == "awaiting_index":
        # Only an awaiting_index record owned by a curator job is refused.
        ledger.update(gid, lambda r: r.job_links.append({"job_id": "job-abcdef12"}))
    before = ledger.load(gid).to_dict()
    called = []
    monkeypatch.setattr(verification, "verify_existing_gap", lambda *a, **k: called.append(a))
    monkeypatch.setattr(gaps_cli.settings, "gap_owner", "local", raising=False)
    rc = gaps_cli.main(["--state-dir", str(tmp_path), "verify", gid, "--existing"])
    assert rc != 0
    assert called == []
    assert ledger.load(gid).to_dict() == before
    err = capsys.readouterr().err
    assert status in err
    if status == "superseded":
        assert primary in err


def test_supersede_rechecks_primary_inside_target_lock(tmp_path, monkeypatch):
    ledger = GapLedger(tmp_path)
    primary = gap(ledger, "Primary question about dispatch-cli?")
    other = gap(ledger, "Other question about dispatch-cli?")
    target = gap(ledger, "Target question about dispatch-cli?")
    real_load = ledger.load
    loads = {"primary": 0}

    def flipping_load(gap_id):
        record = real_load(gap_id)
        if gap_id == primary:
            loads["primary"] += 1
            if loads["primary"] == 1:
                # The first (pre-lock) check sees a live primary; a concurrent
                # supersede then retires it before our transition.
                ledger.transition(primary, "superseded", superseded_by=other)
        return record

    monkeypatch.setattr(ledger, "load", flipping_load)
    before = real_load(target).to_dict()
    with pytest.raises(ValueError, match="primary is itself superseded"):
        handle("supersede", {"gap_id": target, "by": primary}, ledger)
    assert real_load(target).to_dict() == before
    assert real_load(target).status != "superseded"


def test_concurrent_mutual_supersede_has_exactly_one_winner(tmp_path, monkeypatch):
    import threading

    ledger = GapLedger(tmp_path)
    a = gap(ledger, "Question A about dispatch-cli?")
    b = gap(ledger, "Question B about dispatch-cli?")
    real_transition = ledger.transition
    # Hold each caller just before its transition so both have already passed
    # every check. Without cross-gap locking both then transition (a cycle).
    # With it, the second caller is still blocked on the first caller's locks,
    # so the barrier times out and the first caller proceeds alone.
    barrier = threading.Barrier(2, timeout=0.5)

    def held_transition(gap_id, status, **kw):
        if status == "superseded":
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return real_transition(gap_id, status, **kw)

    monkeypatch.setattr(ledger, "transition", held_transition)
    results = {}

    def run(target, primary):
        try:
            handle("supersede", {"gap_id": target, "by": primary}, ledger)
            results[target] = "ok"
        except ValueError as exc:
            results[target] = str(exc)

    threads = [
        threading.Thread(target=run, args=(a, b)),
        threading.Thread(target=run, args=(b, a)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads)
    winners = [g for g, r in results.items() if r == "ok"]
    losers = [g for g, r in results.items() if r != "ok"]
    assert len(winners) == 1 and len(losers) == 1
    assert "primary is itself superseded" in results[losers[0]]
    assert ledger.load(winners[0]).status == "superseded"
    assert ledger.load(losers[0]).status != "superseded"
