"""Gaps stuck in the verifier's own states are re-verified.

`verify --existing` may run on `verifying`, and the worker routes jobless
verifying / retrieval_defect / needs_local_evidence gaps through
`verify_existing_gap` on their backoff.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_gap_worker import env  # noqa: F401
from test_verification_contract import published  # noqa: F401

from doclayer import gap_worker, gaps_cli, verification
from doclayer.gaps import GapLedger

STUCK = ("verifying", "retrieval_defect", "needs_local_evidence")


def make(ledger, question, status, *, subject="dispatch-cli", need_class="missing_coverage", **updates):
    row, _ = ledger.submit_event(need_class=need_class, subject=subject, representative_question=question)
    if status == "needs_local_evidence":
        # Re-verification follows an evidence attempt; a gap with none gathers
        # evidence first (test_gap_scheduling_fairness covers that path).
        updates.setdefault("private_attempts", 1)
    ledger.transition(row.gap_id, status, reason="fixture", **updates)
    return row.gap_id


class FrozenClock(datetime):
    """The worker's clock, frozen: time passes only when a test moves it.

    Backoff waits are measured against the same instant the worker set them
    at, so a slow or loaded run cannot shift them."""

    current = datetime.now(UTC).replace(microsecond=0)

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz is not None else cls.current.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(FrozenClock, "current", datetime.now(UTC).replace(microsecond=0))
    monkeypatch.setattr(gap_worker, "datetime", FrozenClock)
    return FrozenClock


def now():
    return gap_worker.datetime.now(UTC)


def future():
    return (now() + timedelta(hours=1)).isoformat(timespec="seconds")


def past():
    return (now() - timedelta(hours=1)).isoformat(timespec="seconds")


# -- CLI ------------------------------------------------------------------- #


def run_cli(tmp_path, monkeypatch, gid):
    called = []

    def fake(gap_id, ledger):
        called.append(gap_id)
        return SimpleNamespace(outcome="failed", __dict__={"outcome": "failed"})

    monkeypatch.setattr(verification, "verify_existing_gap", fake)
    monkeypatch.setattr(gaps_cli.settings, "gap_owner", "local", raising=False)
    rc = gaps_cli.main(["--state-dir", str(tmp_path), "verify", gid, "--existing"])
    return rc, called


def test_cli_verify_existing_runs_on_verifying(tmp_path, monkeypatch):
    ledger = GapLedger(tmp_path)
    gid = make(ledger, "Question about dispatch-cli?", "verifying")
    rc, called = run_cli(tmp_path, monkeypatch, gid)
    assert called == [gid]


def test_cli_verify_existing_awaiting_index_without_curator_submission_runs(tmp_path, monkeypatch):
    ledger = GapLedger(tmp_path)
    gid = make(ledger, "Question about dispatch-cli?", "awaiting_index")
    rc, called = run_cli(tmp_path, monkeypatch, gid)
    assert called == [gid]


@pytest.mark.parametrize(
    "update",
    [
        lambda r: r.job_links.append({"job_id": "job-abcdef12"}),
        lambda r: r.submission.update(state="accepted"),
    ],
)
def test_cli_verify_existing_awaiting_index_with_active_submission_is_refused(tmp_path, monkeypatch, capsys, update):
    ledger = GapLedger(tmp_path)
    gid = make(ledger, "Question about dispatch-cli?", "awaiting_index")
    ledger.update(gid, update)
    before = ledger.load(gid).to_dict()
    rc, called = run_cli(tmp_path, monkeypatch, gid)
    assert rc == 2 and called == []
    assert ledger.load(gid).to_dict() == before
    assert "awaiting_index" in capsys.readouterr().err


# -- worker ---------------------------------------------------------------- #


def stub_verify(monkeypatch, outcome="resolved", notes=()):
    called = []

    def verify(gap_id, owner):
        called.append(gap_id)
        if outcome == "resolved":
            owner.transition(gap_id, "resolved")
        return SimpleNamespace(outcome=outcome, notes=list(notes))

    monkeypatch.setattr(gap_worker.verification, "verify_existing_gap", verify)
    return called


@pytest.mark.parametrize("status", STUCK)
def test_jobless_stuck_gap_is_reverified_when_backoff_has_passed(env, monkeypatch, status):
    _, ledger, _ = env
    gid = make(ledger, "Is dispatch-cli installed?", status, backoff_until=past())
    called = stub_verify(monkeypatch)
    gap_worker.run_cycle(ledger)
    assert called == [gid]
    assert ledger.load(gid).status == "resolved"


@pytest.mark.parametrize("status", STUCK)
def test_jobless_stuck_gap_waits_for_its_backoff(env, monkeypatch, status):
    _, ledger, _ = env
    gid = make(ledger, "Is dispatch-cli installed?", status, backoff_until=future())
    called = stub_verify(monkeypatch)
    gap_worker.run_cycle(ledger)
    assert called == []
    assert ledger.load(gid).status == status


def test_private_needs_local_evidence_gap_is_reverified_once_a_page_exists(env, monkeypatch):
    _, ledger, _ = env
    gid = make(
        ledger,
        "Where is the private dispatch-cli config on ws1?",
        "needs_local_evidence",
        backoff_until=past(),
        need_class="infrastructure",
    )
    called = stub_verify(monkeypatch)
    gap_worker.run_cycle(ledger)
    assert called == [gid]


def test_gap_with_a_curator_job_is_not_reverified_here(env, monkeypatch):
    _, ledger, _ = env
    gid = make(ledger, "Is dispatch-cli installed?", "verifying", backoff_until=past())
    ledger.update(gid, lambda r: r.job_links.append({"job_id": "job-abcdef12"}))
    called = stub_verify(monkeypatch)
    gap_worker.run_cycle(ledger)
    assert called == []


def test_retrieval_defect_is_verified_once_per_cycle(env, monkeypatch):
    _, ledger, _ = env
    gid = make(
        ledger,
        "Where is it?",
        "retrieval_defect",
        need_class="retrieval_defect",
        subject="prod",
        backoff_until=past(),
    )
    called = stub_verify(monkeypatch, outcome="retrieval_defect", notes=["still defect"])
    gap_worker.run_cycle(ledger)
    assert called == [gid]
    row = ledger.load(gid)
    assert row.status == "retrieval_defect" and row.backoff_until > now().isoformat()


@pytest.mark.parametrize(
    ("status", "seconds"),
    [("needs_local_evidence", 86400), ("verifying", 3600), ("retrieval_defect", 3600)],
)
def test_no_candidate_outcome_backs_off_and_does_not_block_the_next_gap(env, monkeypatch, status, seconds):
    _, ledger, _ = env
    first = make(ledger, "First dispatch-cli question?", status, backoff_until=past())
    second = make(ledger, "Second dispatch-cli question?", status, backoff_until=past())
    called = stub_verify(
        monkeypatch, outcome="awaiting_index", notes=["covered receipt has no actual canonical answer candidate"]
    )
    gap_worker.run_cycle(ledger)
    assert sorted(called) == sorted([first, second])
    for gid in (first, second):
        row = ledger.load(gid)
        assert row.status == status
        wait = (datetime.fromisoformat(row.backoff_until) - now()).total_seconds()
        assert seconds - 120 < wait <= seconds


def test_model_backed_outcome_limits_the_cycle_to_one_verification(env, monkeypatch):
    _, ledger, _ = env
    make(ledger, "First dispatch-cli question?", "verifying", backoff_until=past())
    make(ledger, "Second dispatch-cli question?", "verifying", backoff_until=past())
    called = stub_verify(monkeypatch)
    gap_worker.run_cycle(ledger)
    assert len(called) == 1


def test_pending_verdict_retries_after_five_minutes(env, monkeypatch):
    _, ledger, _ = env
    gid = make(ledger, "Is dispatch-cli installed?", "verifying", backoff_until=past())
    stub_verify(monkeypatch, outcome="verifying", notes=["semantic verification unavailable"])
    gap_worker.run_cycle(ledger)
    row = ledger.load(gid)
    wait = (datetime.fromisoformat(row.backoff_until) - now()).total_seconds()
    assert row.status == "verifying" and 180 < wait <= 300


def test_failed_outcome_returns_the_gap_to_research(env, monkeypatch):
    _, ledger, _ = env
    gid = make(ledger, "Is dispatch-cli installed?", "verifying", backoff_until=past())
    stub_verify(monkeypatch, outcome="failed", notes=["does not answer"])
    gap_worker.run_cycle(ledger)
    assert ledger.load(gid).status != "verifying"


def test_verifier_exception_is_isolated_and_backs_off(env, monkeypatch):
    _, ledger, _ = env
    gid = make(ledger, "Is dispatch-cli installed?", "verifying", backoff_until=past())

    def boom(gap_id, owner):
        raise RuntimeError("inference down")

    monkeypatch.setattr(gap_worker.verification, "verify_existing_gap", boom)
    result = gap_worker.run_cycle(ledger)
    row = ledger.load(gid)
    assert row.status == "verifying" and row.backoff_until > now().isoformat()
    assert any(gid in note for note in result["notes"])


# -- starvation, backoff growth, single judgement per cycle ------------------ #


def wait_s(ledger, gid):
    row = ledger.load(gid)
    return (datetime.fromisoformat(row.backoff_until) - now()).total_seconds()


def pass_time(ledger):
    """Pretend every pending backoff has elapsed."""
    for row in ledger.list_records():
        if row.backoff_until and row.backoff_until > now().isoformat():
            ledger.update(
                row.gap_id,
                lambda r: setattr(
                    r,
                    "backoff_until",
                    (now() - timedelta(seconds=1)).isoformat(timespec="seconds"),
                ),
            )


def test_a_gap_that_stays_verifying_does_not_starve_the_others(env, monkeypatch):
    _, ledger, _ = env
    # One timestamp for all three: past() has one-second precision, so separate
    # calls can straddle a second and order the gaps by time instead of id.
    when = past()
    ids = [make(ledger, f"Question {n} about dispatch-cli?", "verifying", backoff_until=when) for n in range(3)]
    stuck = min(ids)
    called = []

    def verify(gap_id, owner):
        called.append(gap_id)
        if gap_id == stuck:
            return SimpleNamespace(outcome="verifying", notes=["semantic verification unavailable"])
        owner.transition(gap_id, "resolved")
        return SimpleNamespace(outcome="resolved", notes=[])

    monkeypatch.setattr(gap_worker.verification, "verify_existing_gap", verify)
    waits = []
    for _ in range(4):
        gap_worker.run_cycle(ledger)
        if called and called[-1] == stuck:
            waits.append(wait_s(ledger, stuck))
        pass_time(ledger)
    assert set(called) == set(ids), called
    assert called[0] == stuck  # all were eligible; ties go in gap-id order
    assert called.count(stuck) >= 2
    assert 240 < waits[0] <= 300
    assert 540 < waits[1] <= 600 and waits[1] > waits[0]  # doubled


def test_backoff_doubles_from_five_minutes_and_caps_at_six_hours(env, monkeypatch):
    _, ledger, _ = env
    gid = make(ledger, "Is dispatch-cli installed?", "verifying", backoff_until=past())
    stub_verify(monkeypatch, outcome="verifying", notes=["pending"])
    seen = []
    for _ in range(9):
        gap_worker.run_cycle(ledger)
        seen.append(round(wait_s(ledger, gid) / 60))
        pass_time(ledger)
    assert seen[:4] == [5, 10, 20, 40]
    assert max(seen) == 360 and seen[-1] == 360
    assert ledger.load(gid).resolution_verification["reverify_attempts"] == 9


def test_oldest_backoff_goes_first_not_gap_id_order(env, monkeypatch):
    _, ledger, _ = env
    a = make(ledger, "A question about dispatch-cli?", "verifying")
    b = make(ledger, "B question about dispatch-cli?", "verifying")
    older, newer = sorted([a, b])[1], sorted([a, b])[0]  # older has the larger gap id
    ledger.update(
        older, lambda r: setattr(r, "backoff_until", (now() - timedelta(hours=3)).isoformat(timespec="seconds"))
    )
    ledger.update(
        newer, lambda r: setattr(r, "backoff_until", (now() - timedelta(minutes=1)).isoformat(timespec="seconds"))
    )
    called = stub_verify(monkeypatch, outcome="verifying", notes=["pending"])
    gap_worker.run_cycle(ledger)
    assert called == [older]


@pytest.mark.parametrize("outcome", ["failed", "partial", "retrieval_defect", "verifying"])
def test_a_cycle_judges_each_gap_at_most_once_and_keeps_its_backoff(env, monkeypatch, outcome):
    _, ledger, _ = env
    # need_class retrieval_defect makes _research_one re-verify covered gaps
    gid = make(ledger, "Where is it?", "verifying", need_class="retrieval_defect", subject="prod", backoff_until=past())
    called = stub_verify(monkeypatch, outcome=outcome, notes=["not answered"])
    gap_worker.run_cycle(ledger)
    assert called == [gid]
    row = ledger.load(gid)
    assert row.backoff_until and wait_s(ledger, gid) > 240


# -- need binding ignores catalog-derived scope computation ------------------- #


def test_catalog_change_that_leaves_the_gap_alone_keeps_the_binding(published, tmp_path, monkeypatch):
    from test_verification_contract import client, positive

    from doclayer import applicability
    from doclayer.gap_budget import ResourceBudget

    root, path, commit, ledger, record, reply, index = published
    record.job_links = []
    ledger.save(record)
    model1, usage1 = client(tmp_path, positive(path))
    first = verification.verify_existing_gap(
        record.gap_id, ledger, model_client=model1, budget=ResourceBudget(ledger.root)
    )
    assert first.outcome == "resolved" and usage1.model_calls == 2
    binding1 = ledger.load(record.gap_id).resolution_verification["canonical_checkpoint"]["binding"]

    # A new catalog revision / known-identity set changes the computed scopes,
    # but not the gap's own question, facets or applicability.
    real = applicability.effective_question_scopes
    monkeypatch.setattr(
        applicability,
        "effective_question_scopes",
        lambda question, scopes: [*real(question, scopes), {"dimension": "region", "value": "eu"}],
    )
    model2, usage2 = client(tmp_path, positive(path))
    second = verification.verify_existing_gap(
        record.gap_id, ledger, model_client=model2, budget=ResourceBudget(ledger.root)
    )
    assert second.outcome == "resolved"
    binding2 = ledger.load(record.gap_id).resolution_verification["canonical_checkpoint"]["binding"]
    assert binding2 == binding1
    assert usage2.model_calls == 1  # canonical verdict reused; only delivery judged


def test_changing_the_gaps_own_applicability_changes_the_binding(published, tmp_path):
    from test_verification_contract import client, positive

    from doclayer.gap_budget import ResourceBudget

    root, path, commit, ledger, record, reply, index = published
    record.job_links = []
    ledger.save(record)
    model1, _ = client(tmp_path, positive(path))
    verification.verify_existing_gap(record.gap_id, ledger, model_client=model1, budget=ResourceBudget(ledger.root))
    binding1 = ledger.load(record.gap_id).resolution_verification["canonical_checkpoint"]["binding"]
    changed = ledger.load(record.gap_id)
    changed.applicability = [{"dimension": "edition", "value": "Enterprise"}]
    ledger.save(changed)
    model2, usage2 = client(tmp_path, positive(path))
    verification.verify_existing_gap(record.gap_id, ledger, model_client=model2, budget=ResourceBudget(ledger.root))
    binding2 = ledger.load(record.gap_id).resolution_verification["canonical_checkpoint"]["binding"]
    assert binding2 != binding1 and usage2.model_calls == 2


# -- one shared backoff for every re-verification path ------------------------ #


def test_curator_job_gap_stuck_in_verifying_is_judged_once_then_backs_off(env, monkeypatch):
    from doclayer.dispatcher import GapDispatcher

    _, ledger, _ = env
    job = {"job_id": "job-30f6d5680a", "status": "published", "usage": {"model_calls": 1}}
    gid = make(ledger, "Is dispatch-cli installed?", "verifying")
    ledger.update(gid, lambda r: r.job_links.append({"job_id": job["job_id"], "attempt": 1}))
    calls = []

    class StatusOnly:
        def call(self, operation, payload, **kwargs):
            assert operation == "status"
            return {"ok": True, "job": dict(job)}

    def verify(gap_id, owner):
        calls.append(gap_id)
        owner.transition(gap_id, "verifying", reason="pending")
        return SimpleNamespace(outcome="verifying", notes=["pending"])

    monkeypatch.setattr(gap_worker.verification, "verify_gap", verify)
    dispatcher = GapDispatcher(ledger, transport=StatusOnly())
    waits = []
    for _ in range(4):
        gap_worker._verify_published(ledger, dispatcher, [])
        gap_worker._verify_published(ledger, dispatcher, [])  # same instant: must wait
        waits.append(round(wait_s(ledger, gid) / 60))
        pass_time(ledger)
    assert calls == [gid] * 4  # exactly one judgement per elapsed backoff
    assert waits == [5, 10, 20, 40]
    assert ledger.load(gid).resolution_verification["reverify_attempts"] == 4


def test_grouped_need_stuck_in_verifying_backs_off_with_growth(env, monkeypatch):
    from test_gap_backlog_worker_progress import pair

    _, ledger, _ = env
    parent, child = pair(ledger)
    calls = []

    def verify(gap_id, owner):
        calls.append(gap_id)
        owner.transition(gap_id, "verifying", reason="pending")
        return SimpleNamespace(outcome="verifying", notes=["pending"])

    monkeypatch.setattr(gap_worker.verification, "verify_existing_gap", verify)
    waits = []
    for _ in range(4):
        gap_worker.run_cycle(ledger)
        gap_worker.run_cycle(ledger)  # immediately again: backoff holds
        waits.append(round(wait_s(ledger, child.gap_id) / 60))
        pass_time(ledger)
    assert calls == [child.gap_id] * 4
    assert waits == [5, 10, 20, 40]


def test_a_rejected_verdict_is_retried_on_backoff_and_can_resolve(env, monkeypatch):
    # The model's verdict is nondeterministic: a rejected reply leaves the gap
    # `verifying` with a 5 minute backoff, and the same frame resolves on the retry.
    _, ledger, _ = env
    gid = make(ledger, "Is dispatch-cli installed?", "verifying", backoff_until=past())
    replies = iter(["verifying", "resolved"])

    def verify(gap_id, owner):
        outcome = next(replies)
        if outcome == "resolved":
            owner.transition(gap_id, "resolved")
        return SimpleNamespace(outcome=outcome, notes=["semantic verification unavailable, malformed"])

    monkeypatch.setattr(gap_worker.verification, "verify_existing_gap", verify)
    gap_worker.run_cycle(ledger)
    row = ledger.load(gid)
    assert row.status == "verifying" and row.resolution_verification["reverify_attempts"] == 1
    assert row.backoff_until > now().isoformat()
    gap_worker.run_cycle(ledger)
    assert ledger.load(gid).status == "verifying"  # still inside its backoff: no second model call
    ledger.update(gid, lambda r: setattr(r, "backoff_until", past()))
    gap_worker.run_cycle(ledger)
    assert ledger.load(gid).status == "resolved"
