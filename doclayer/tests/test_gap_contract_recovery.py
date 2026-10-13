"""Lost replies/restarts use real owning ops, not invented response mocks."""

import json
import threading

import pytest

from doclayer.curator.config import CuratorConfig, PathsConfig
from doclayer.curator.jobstore import JobStore
from doclayer.curator.ops import handle
from doclayer.curator_reply import parse_job_reply
from doclayer.dispatcher import GapDispatcher
from doclayer.gap_budget import BudgetUnavailable, ResourceBudget
from doclayer.gaps import GapError, GapLedger


class RealOps:
    def __init__(self, path):
        self.cfg = CuratorConfig(paths=PathsConfig(jobs=str(path / "jobs"), cache=str(path / "cache")))
        self.store = JobStore(path / "jobs")
        self.calls = []
        self.lose = False

    def call(self, op, payload, **kwargs):
        self.calls.append((op, json.loads(json.dumps(payload))))
        reply = handle(op, payload, self.cfg, self.store)
        if self.lose and op == "submit":
            self.lose = False
            raise TimeoutError("lost AFTER success")
        return reply


def ready(ledger, name="widget"):
    r, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject=name,
        representative_question=f"docs for {name}: authentication",
        facets=["authentication"],
        source_candidates=[{"url": f"https://example.org/{name}", "authority": "official"}],
    )
    return ledger.transition(r.gap_id, "ready_for_curation")


def setup(path):
    ledger = GapLedger(path / "gaps")
    ledger.root.mkdir()
    (ledger.root / "curator-ready").touch()
    ops = RealOps(path)
    return ledger, ops, GapDispatcher(ledger, transport=ops)


def test_lost_success_restart_same_payload_key_reservation_one_real_job(tmp_path):
    ledger, ops, dispatcher = setup(tmp_path)
    r = ready(ledger)
    ops.lose = True
    assert dispatcher.dispatch(r.gap_id).action == "skipped"
    old = ledger.load(r.gap_id).submission
    assert old["state"] == "uncertain" and len(ops.store.list_ids()) == 1
    assert dispatcher.dispatch(ready(ledger, "other").gap_id).action == "skipped"
    out = GapDispatcher(GapLedger(ledger.root), transport=ops).dispatch(r.gap_id)
    assert out.action == "submitted" and len(ops.store.list_ids()) == 1
    assert [p for op, p in ops.calls if op == "submit"] == [
        old["payload"],
        old["payload"],
    ]
    new = ledger.load(r.gap_id)
    assert new.attempts == 1 and len(new.job_links) == 1 and new.submission["reservation"] == old["reservation"]
    assert ResourceBudget(ledger.root).load(old["reservation"]["day"])["jobs"] == 1


def test_real_status_result_and_singleton_terminal_release(tmp_path):
    ledger, ops, d = setup(tmp_path)
    one, two = ready(ledger), ready(ledger, "second")
    out = d.dispatch(one.gap_id)
    assert d.dispatch(two.gap_id).action == "skipped"
    for status in ("retryable", "partial", "covered", "cancelled"):
        state = ops.store.load(out.job_id)
        state.status = status
        ops.store.save(state)
        reply = ops.call("result", {"job_id": out.job_id})
        assert parse_job_reply(reply)["status"] == status and "digest" in reply
    state.status = "retryable"
    ops.store.save(state)
    d.dispatch(one.gap_id)
    assert d.active() and d.dispatch(two.gap_id).action == "skipped"
    state.status = "cancelled"
    ops.store.save(state)
    d.dispatch(one.gap_id)
    assert d.active() is None and d.dispatch(two.gap_id).action == "submitted"


@pytest.mark.parametrize(
    "reply",
    [
        {"ok": False, "job_id": "job-x", "created": True},
        {"ok": True, "created": True},
        {"ok": True, "job_id": "None", "created": True},
        {"ok": True, "job_id": "job-x", "created": "true"},
    ],
)
def test_invalid_receipt_keeps_uncertain_slot(tmp_path, reply):
    ledger, ops, d = setup(tmp_path)

    class Invalid:
        def call(self, *a, **k):
            return reply

    d.transport = Invalid()
    r = ready(ledger)
    assert d.dispatch(r.gap_id).action == "skipped"
    assert ledger.load(r.gap_id).submission["state"] == "uncertain" and d.active()["gap_id"] == r.gap_id


def test_parked_dryrun_drained_on_activation(tmp_path):
    ledger, ops, d = setup(tmp_path)
    (ledger.root / "curator-ready").unlink()
    r = ready(ledger)
    assert d.dispatch(r.gap_id).action == "dry-run"
    payload = ledger.load(r.gap_id).submission["payload"]
    assert ledger.spool_list()
    (ledger.root / "curator-ready").touch()
    assert GapDispatcher(ledger, transport=ops).dispatch(r.gap_id).action == "submitted"
    assert [p for op, p in ops.calls if op == "submit"] == [payload] and not ledger.spool_list()


def test_concurrent_dispatchers_one_job(tmp_path):
    ledger, ops, d = setup(tmp_path)
    ids = [ready(ledger, f"widget-{n}").gap_id for n in range(4)]
    results = []

    def dispatch(g):
        results.append(GapDispatcher(GapLedger(ledger.root), transport=ops).dispatch(g))

    threads = [threading.Thread(target=dispatch, args=(g,)) for g in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r.action == "submitted" for r in results) == 1 and len(ops.store.list_ids()) == 1


def test_stale_worker_cannot_erase_capture_merge(tmp_path):
    ledger = GapLedger(tmp_path)
    args = dict(
        need_class="enrichment",
        subject="known",
        catalog_subject=True,
        representative_question="quota?",
        facets=["quota"],
    )
    r, _ = ledger.submit_event(**args, event_key="first")
    assert r.canonical_subject == "known"
    ledger.submit_event(
        **args,
        event_key="second",
        source_candidates=[{"url": "https://example.org/new"}],
        checked_sources=[{"source": "canonical"}],
        dispositions=["partial"],
    )
    with pytest.raises(GapError, match="concurrently"):
        ledger.save(r)
    current = ledger.load(r.gap_id)
    assert current.recurrence == 2 and current.source_candidates and current.dispositions == ["partial"]


def test_applicability_question_client_identity_and_replay(tmp_path):
    ledger = GapLedger(tmp_path)

    def event(q, edition, key=None):
        return ledger.submit_event(
            need_class="missing_coverage",
            subject="model",
            catalog_subject=True,
            representative_question=q,
            applicability=[{"edition": edition}],
            event_key=key,
        )[0]

    a, b, c = (
        event("quota?", "Personal"),
        event("quota?", "Team"),
        event("authenticate?", "Personal"),
    )
    assert len({a.gap_id, b.gap_id, c.gap_id}) == 3
    assert event("quota?", "Personal", "client-42").recurrence == 2
    assert event("quota?", "Personal", "client-42").recurrence == 2
    assert event("quota?", "Personal").recurrence == 3


def test_replay_survives_pruned_event_trail(tmp_path):
    ledger = GapLedger(tmp_path)
    args = dict(
        need_class="missing_coverage",
        subject="x",
        representative_question="q",
        event_key="once",
    )
    r, _ = ledger.submit_event(**args)
    for _ in range(510):
        ledger.transition(r.gap_id, "candidate")
    assert len(ledger.events_path(r.gap_id).read_text().splitlines()) <= 500
    assert ledger.submit_event(**args)[0].recurrence == 1


def test_spool_refuses_capacity_preserves_existing(tmp_path, monkeypatch):
    import doclayer.gaps as module

    monkeypatch.setattr(module, "SPOOL_CAP_BYTES", 100)
    ledger = GapLedger(tmp_path)
    ledger.spool_put("a", {"event": "first"})
    with pytest.raises(GapError, match="capacity"):
        ledger.spool_put("b", {"event": "x" * 100})
    assert ledger.spool_list() == [("a.json", {"event": "first"})]


def test_corrupt_budget_and_reduced_grant_truthful(tmp_path):
    ledger, ops, d = setup(tmp_path)
    d = GapDispatcher(ledger, transport=ops, max_calls_day=100, jobs_per_day=3)
    budget = ResourceBudget(ledger.root, max_calls_day=100, max_jobs=3)
    from doclayer.gap_budget import today

    path = budget.dir / f"{today()}.json"
    path.write_text("{corrupt")
    assert d.dispatch(ready(ledger).gap_id).action == "skipped" and not ops.calls
    with pytest.raises(BudgetUnavailable):
        budget.consume_model(kind="discovery")
    path.unlink()
    for _ in range(90):
        budget.consume_model(kind="verification")
    out = d.dispatch(ready(ledger).gap_id)
    assert out.action == "submitted" and ops.store.load(out.job_id).limits["max_model_calls"] == 10


def test_midnight_active_hold_and_failed_attempts_count(tmp_path, monkeypatch):
    import doclayer.gap_budget as module

    ledger, ops, d = setup(tmp_path)
    monkeypatch.setattr(module, "today", lambda: "2026-10-03")
    assert d.dispatch(ready(ledger).gap_id).action == "submitted"
    monkeypatch.setattr(module, "today", lambda: "2026-10-04")
    budget = ResourceBudget(ledger.root, max_calls_day=100, max_jobs=3)
    for _ in range(50):
        budget.consume_model(kind="discovery")
    with pytest.raises(BudgetUnavailable):
        budget.consume_model(kind="discovery")
    data = budget.load("2026-10-04")
    assert data["reserved_calls"] == 50 and data["model_calls"] == 50


def worker_env(monkeypatch, ledger, ops):
    from doclayer import gap_worker
    from doclayer.coverage import Catalog

    monkeypatch.setattr(
        gap_worker,
        "load_catalog",
        lambda *a, **k: Catalog(status="ok", revision="a" * 40, policy_version=1),
    )
    monkeypatch.setattr(gap_worker, "ssh_host_aliases", lambda *a, **k: [])
    monkeypatch.setattr(
        gap_worker,
        "GapDispatcher",
        lambda led, **k: GapDispatcher(led, transport=ops, **k),
    )
    return gap_worker


def test_actual_worker_two_cycles_single_remote_job_and_readiness_activation(tmp_path, monkeypatch):
    ledger, ops, d = setup(tmp_path)
    worker = worker_env(monkeypatch, ledger, ops)
    (ledger.root / "curator-ready").unlink()
    ready(ledger)
    ready(ledger, "second")
    worker.run_cycle(ledger)
    assert len(ledger.spool_list()) == 1 and not ops.store.list_ids()
    (ledger.root / "curator-ready").touch()
    worker.run_cycle(ledger)
    worker.run_cycle(ledger)
    assert len(ops.store.list_ids()) == 1
    assert sum(r.status == "curating" for r in ledger.list_records()) == 1
    assert not ledger.spool_list()


def test_crash_after_reserved_record_reconstructs_singleton(tmp_path):
    ledger, ops, d = setup(tmp_path)
    one, two = ready(ledger), ready(ledger, "other")
    ops.lose = True
    d.dispatch(one.gap_id)
    d.slot_path(one.gap_id).unlink()  # simulated crash window; record remains authoritative
    restarted = GapDispatcher(GapLedger(ledger.root), transport=ops)
    assert restarted.dispatch(two.gap_id).action == "skipped"
    assert restarted.dispatch(one.gap_id).action == "submitted"
    assert len(ops.store.list_ids()) == 1


def test_expired_research_recovers_and_backoff_attempts_advance(tmp_path, monkeypatch):
    from doclayer.discovery import DiscoveryResult

    ledger, ops, d = setup(tmp_path)
    worker = worker_env(monkeypatch, ledger, ops)
    r = ready(ledger)
    ledger.transition(
        r.gap_id,
        "researching",
        lease={"worker": "dead", "expires_at": "2000-01-01T00:00:00Z"},
    )

    def unresolved(record, catalog, **kwargs):
        assert kwargs["state_dir"] == ledger.root
        return DiscoveryResult(record["gap_id"], "unresolved", notes=["no authoritative source"])

    monkeypatch.setattr(worker, "discover", unresolved)
    worker.run_cycle(ledger)
    current = ledger.load(r.gap_id)
    assert current.status == "retry_wait" and current.research_attempts == 1 and current.lease is None
    ledger.transition(r.gap_id, "retry_wait", backoff_until="2000-01-01T00:00:00Z")
    worker.run_cycle(ledger)
    current = ledger.load(r.gap_id)
    assert current.research_attempts == 2 and current.backoff_until and not ops.calls


def test_crash_event_trail_does_not_lose_capture_count(tmp_path, monkeypatch):
    ledger = GapLedger(tmp_path)
    args = dict(need_class="missing_coverage", subject="x", representative_question="q")
    first, _ = ledger.submit_event(**args, event_key="one")
    original = ledger.append_event

    def crash(*a, **k):
        raise RuntimeError("event trail failed after authoritative save")

    monkeypatch.setattr(ledger, "append_event", crash)
    with pytest.raises(RuntimeError):
        ledger.submit_event(**args, event_key="two", source_candidates=[{"url": "https://example.org"}])
    monkeypatch.setattr(ledger, "append_event", original)
    repeated, _ = ledger.submit_event(**args, event_key="two")
    assert repeated.recurrence == 2 and repeated.source_candidates


def test_daily_parked_staged_attempt_resumes_next_day_without_research(tmp_path, monkeypatch):
    import doclayer.gap_budget as module

    ledger, ops, d = setup(tmp_path)
    d = GapDispatcher(ledger, transport=ops, max_calls_day=100, jobs_per_day=3)
    worker = worker_env(monkeypatch, ledger, ops)
    monkeypatch.setattr(
        worker,
        "GapDispatcher",
        lambda led, **k: GapDispatcher(led, transport=ops, **dict(k, max_calls_day=100, jobs_per_day=3)),
    )
    monkeypatch.setattr(module, "today", lambda: "2026-10-03")
    allowance = ResourceBudget(ledger.root, max_calls_day=100, max_jobs=3)
    for _ in range(100):
        allowance.consume_model(kind="verification")
    r = ready(ledger)
    assert d.dispatch(r.gap_id).action == "skipped"
    saved = ledger.load(r.gap_id).submission
    assert saved["state"] == "staged" and not ops.store.list_ids()
    monkeypatch.setattr(module, "today", lambda: "2026-10-04")
    ledger.transition(r.gap_id, "retry_wait", backoff_until="2000-01-01T00:00:00Z")

    def no_research(*a, **k):
        raise AssertionError("staged dispatch must not be rediscovered")

    monkeypatch.setattr(worker, "discover", no_research)
    worker.run_cycle(ledger)
    assert len(ops.store.list_ids()) == 1
    current = ledger.load(r.gap_id)
    assert current.submission["attempt_id"] == saved["attempt_id"] and current.attempts == 1


def test_same_event_key_cannot_create_different_question_or_erase_original(tmp_path):
    ledger = GapLedger(tmp_path)
    args = dict(
        need_class="missing_coverage",
        subject="model",
        representative_question="quota?",
        event_key="client-delivery",
    )
    first, _ = ledger.submit_event(**args)
    with pytest.raises(GapError, match="different payload"):
        ledger.submit_event(**dict(args, representative_question="authentication?"))
    assert ledger.list_ids() == [first.gap_id]
    assert ledger.load(first.gap_id).recurrence == 1


def test_legacy_exact_need_migration_preserves_identity_jobs_and_unknown_proofs(
    tmp_path,
):
    import hashlib

    from doclayer.gaps import GapRecord

    ledger = GapLedger(tmp_path)
    legacy_id = "gap-" + hashlib.sha256(b"unknown:known model\x00missing_coverage\x00quota").hexdigest()[:20]
    old = GapRecord(
        gap_id=legacy_id,
        identity="unknown:known model",
        canonical_subject=None,
        need_class="missing_coverage",
        status="awaiting_index",
        representative_question="What quota?",
        facets=["quota"],
        applicability=[{"edition": "Team"}],
        job_links=[{"job_id": "job-12345678"}],
        resolution_verification={"unknown": "retained"},
        recurrence=5,
    )
    ledger.save(old)
    args = dict(
        need_class="missing_coverage",
        subject="known-model",
        catalog_subject=True,
        representative_question="What quota?",
        facets=["quota"],
        applicability=[{"edition": "Team"}],
    )
    new, created = ledger.submit_event(**args, event_key="new-client")
    assert not created and new.gap_id == legacy_id and new.canonical_subject == "known-model"
    assert (
        new.job_links == old.job_links
        and new.resolution_verification == old.resolution_verification
        and new.recurrence == 6
    )
    different, _ = ledger.submit_event(**dict(args, applicability=[{"edition": "Personal"}]))
    other, _ = ledger.submit_event(**dict(args, representative_question="How authenticate?"))
    assert len({new.gap_id, different.gap_id, other.gap_id}) == 3


def test_foreign_active_status_cannot_mutate_recovered_receipt(tmp_path):
    ledger, ops, d = setup(tmp_path)
    r = ready(ledger)
    out = d.dispatch(r.gap_id)
    before = ledger.load(r.gap_id).to_dict()
    real = ops.call("status", {"job_id": out.job_id})
    real["job"]["job_id"] = "job-foreign0"
    real["job"]["status"] = "published"

    class Foreign:
        def call(self, *a, **k):
            return real

    d.transport = Foreign()
    assert d.dispatch(r.gap_id).action == "skipped"
    assert ledger.load(r.gap_id).to_dict() == before
    assert d.active()["job_id"] == out.job_id


def test_midnight_terminal_observation_accounts_native_day_before_releasing_slot(tmp_path, monkeypatch):
    import doclayer.gap_budget as module

    ledger, ops, d = setup(tmp_path)
    monkeypatch.setattr(module, "today", lambda: "2026-10-03")
    r = ready(ledger)
    out = d.dispatch(r.gap_id)
    monkeypatch.setattr(module, "today", lambda: "2026-10-04")
    state = ops.store.load(out.job_id)
    state.status = "cancelled"
    ops.store.save(state)
    d.dispatch(r.gap_id)
    assert d.active() is None
    budget = ResourceBudget(ledger.root, max_calls_day=100, max_jobs=3)
    assert budget.load("2026-10-04")["reserved_calls"] == 50
    for _ in range(50):
        budget.consume_model(kind="verification")
    with pytest.raises(BudgetUnavailable):
        budget.consume_model(kind="verification")


def test_dispatch_admits_official_seeds_only_not_other_retained_candidates(tmp_path):
    ledger, ops, d = setup(tmp_path)
    r = ready(ledger)
    ledger.update(
        r.gap_id,
        lambda row: row.source_candidates.append(
            {"url": "https://unverified.example/claim", "authority": "unverified"}
        ),
    )
    assert d.dispatch(r.gap_id).action == "submitted"
    payload = next(p for op, p in ops.calls if op == "submit")
    assert payload["urls"] == ["https://example.org/widget"]
    other = ready(ledger, "unverified")
    ledger.update(
        other.gap_id,
        lambda row: setattr(row, "source_candidates", [{"url": "https://unverified.example/claim"}]),
    )
    # No verified seeds cannot reserve a new job even without another active one.
    first = ledger.load(r.gap_id).job_links[-1]["job_id"]
    state = ops.store.load(first)
    state.status = "cancelled"
    ops.store.save(state)
    d.dispatch(r.gap_id)
    assert d.dispatch(other.gap_id).action == "skipped"
    assert not ledger.load(other.gap_id).submission


def test_identical_terminal_inputs_park_new_source_or_explicit_retry_advances_once(
    tmp_path,
):
    ledger, ops, d = setup(tmp_path)
    r = ready(ledger)
    out = d.dispatch(r.gap_id)
    state = ops.store.load(out.job_id)
    state.status = "partial"
    ops.store.save(state)
    d.dispatch(r.gap_id)
    ledger.transition(r.gap_id, "ready_for_curation")
    assert d.dispatch(r.gap_id).action == "skipped"
    assert ledger.load(r.gap_id).status == "retry_wait" and len(ops.store.list_ids()) == 1

    def refreshed(row):
        row.source_candidates[0]["source_hash"] = "a" * 64
        row.status = "ready_for_curation"

    ledger.update(r.gap_id, refreshed)
    advanced = d.dispatch(r.gap_id)
    assert advanced.action == "submitted" and len(ops.store.list_ids()) == 2
    current = ledger.load(r.gap_id)
    assert current.attempts == 2 and len(current.submission_history) == 1
    assert current.submission_history[0]["input_sources"][0]["source_hash"] == ""
    state = ops.store.load(advanced.job_id)
    state.status = "cancelled"
    ops.store.save(state)
    d.dispatch(r.gap_id)

    def explicit(row):
        row.submission.update(manual_retry=True)
        row.status = "ready_for_curation"

    ledger.update(r.gap_id, explicit)
    # New explicit logical attempt is staged once; observing the active job
    # must not regenerate its ID/reservation.
    d.dispatch(r.gap_id)
    staged = ledger.load(r.gap_id).submission
    assert staged["attempt"] == 3 and "manual_retry" not in staged
    d.dispatch(r.gap_id)
    assert ledger.load(r.gap_id).submission["attempt_id"] == staged["attempt_id"]


def test_worker_refresh_replaces_source_hash_without_duplicate_current_candidates(tmp_path, monkeypatch):
    from doclayer.discovery import DiscoveryResult, Seed

    ledger, ops, d = setup(tmp_path)
    worker = worker_env(monkeypatch, ledger, ops)
    r = ready(ledger)
    ledger.transition(r.gap_id, "candidate")
    seed = Seed(
        "https://example.org/widget",
        "catalog-root",
        "deterministic",
        source_hash="b" * 64,
    )
    monkeypatch.setattr(
        worker,
        "discover",
        lambda *a, **k: DiscoveryResult(r.gap_id, "discovered", seeds=[seed]),
    )
    worker.run_cycle(ledger)
    current = ledger.load(r.gap_id)
    assert [s for s in current.source_candidates if s["url"] == seed.url] == [seed.to_dict()]
    assert current.submission["input_sources"] == [{"url": seed.url, "source_hash": "b" * 64}]


def test_resolved_gap_reopens_only_fresh_attested_changed_unresolved_generation(
    tmp_path,
):
    ledger = GapLedger(tmp_path)
    base = dict(
        need_class="missing_coverage",
        subject="widget",
        representative_question="What is Widget retention?",
        facets=["retention"],
        catalog_subject=True,
    )
    record, _ = ledger.submit_event(
        **base,
        event_key="initial",
        evidence_generation="a" * 64,
        coverage_revision="old",
        generation_attested=True,
        dispositions=["partial"],
    )

    def resolved(row):
        row.status = "resolved"
        row.resolution_verification = {
            "outcome": "resolved",
            "canonical_revision": "old",
            "quote": "Enterprise retention is seven days.",
        }
        row.index_revision = "index-old"
        row.publication_revision = "publication-old"
        row.job_links = [{"job_id": "job-original1"}]
        row.submission = {"state": "terminal", "input_signature": "old"}
        row.backoff_until = "2099-01-01T00:00:00+00:00"

    ledger.update(record.gap_id, resolved)
    # Exact replay of the initial unresolved event cannot revoke a later proof.
    replay, _ = ledger.submit_event(
        **base,
        event_key="initial",
        evidence_generation="a" * 64,
        coverage_revision="old",
        generation_attested=True,
        dispositions=["partial"],
    )
    assert replay.status == "resolved"
    for event, generation, attested, dispositions in [
        ("same", "a" * 64, True, ["partial"]),
        ("unattested", "b" * 64, False, ["partial"]),
        ("malformed", "not-a-generation", True, ["partial"]),
        ("answered", "b" * 64, True, ["answered", "partial"]),
    ]:
        current, _ = ledger.submit_event(
            **base,
            event_key=event,
            evidence_generation=generation,
            coverage_revision="current",
            generation_attested=attested,
            dispositions=dispositions,
        )
        assert current.status == "resolved" and not current.resolution_history
    # A newer complete need observation changes generation without losing old proof.
    reopened, _ = ledger.submit_event(
        **base,
        event_key="fresh",
        evidence_generation="c" * 64,
        coverage_revision="new",
        generation_attested=True,
        dispositions=["not_found_in_checked_sources"],
    )
    assert reopened.status == "candidate" and reopened.backoff_until is None
    assert reopened.resolution_verification["outcome"] == "resolved"
    assert reopened.job_links == [{"job_id": "job-original1"}]
    assert reopened.submission == {"state": "terminal", "input_signature": "old"}
    assert reopened.resolution_history[0]["resolution_verification"]["quote"] == "Enterprise retention is seven days."
    assert reopened.resolution_history[0]["evidence_generation"] == "b" * 64
    ledger.update(record.gap_id, resolved)
    replay, _ = ledger.submit_event(
        **base,
        event_key="fresh",
        evidence_generation="c" * 64,
        coverage_revision="new",
        generation_attested=True,
        dispositions=["not_found_in_checked_sources"],
    )
    assert replay.status == "resolved" and len(replay.resolution_history) == 1


def test_missing_prior_attested_generation_does_not_invent_change(tmp_path):
    ledger = GapLedger(tmp_path)
    record, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="widget",
        representative_question="What is retention?",
    )
    ledger.transition(record.gap_id, "resolved")
    row, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="widget",
        representative_question="What is retention?",
        evidence_generation="b" * 64,
        coverage_revision="new",
        generation_attested=True,
        dispositions=["partial"],
    )
    assert row.status == "resolved" and not row.resolution_history


def test_worker_backlog_researches_all_then_preserves_active_job_budget(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from doclayer import gap_worker
    from doclayer.discovery import DiscoveryResult, Seed

    ledger, ops, dispatcher = setup(tmp_path)
    for name in ("one", "two", "three"):
        ledger.submit_event(
            need_class="missing_coverage",
            subject=name,
            representative_question=f"docs for {name}: authentication",
        )
    discovered = []

    def discover(record, catalog, **kwargs):
        discovered.append(record["gap_id"])
        ResourceBudget(ledger.root).consume_model(kind="discovery")
        return DiscoveryResult(
            record["gap_id"],
            "found",
            seeds=[
                Seed(
                    "https://example.org/docs",
                    "catalog-root",
                    "complete source",
                    source_hash="a" * 64,
                )
            ],
        )

    monkeypatch.setattr(gap_worker, "GapDispatcher", lambda _, **kw: dispatcher)
    monkeypatch.setattr(gap_worker, "discover", discover)
    monkeypatch.setattr(
        gap_worker,
        "load_catalog",
        lambda *a, **kw: SimpleNamespace(subjects={}, policy_version=1),
    )
    monkeypatch.setattr(gap_worker, "ssh_host_aliases", lambda *a, **kw: [])
    gap_worker.run_cycle(ledger)
    # One cycle researches the whole backlog; still only one job is submitted.
    assert len(discovered) == 3 and len(ops.store.list_ids()) == 1
    assert len([r for r in ledger.list_records() if r.status == "ready_for_curation"]) == 2
    gap_worker.run_cycle(ledger)
    # The active job keeps its slot: no new research, no second submission.
    assert len(discovered) == 3 and len(ops.store.list_ids()) == 1
    assert (
        ResourceBudget(ledger.root).load(__import__("doclayer.gap_budget", fromlist=["today"]).today())["model_calls"]
        == 3
    )


def test_worker_dispatches_parked_ready_before_researching_backlog(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from doclayer import gap_worker

    ledger, ops, dispatcher = setup(tmp_path)
    waiting = ready(ledger, "ready-first")
    ledger.submit_event(
        need_class="missing_coverage",
        subject="new",
        representative_question="How authenticate new?",
    )
    monkeypatch.setattr(gap_worker, "GapDispatcher", lambda _, **kw: dispatcher)
    monkeypatch.setattr(
        gap_worker,
        "discover",
        lambda *a, **kw: pytest.fail("existing ready submission must precede new research"),
    )
    monkeypatch.setattr(
        gap_worker,
        "load_catalog",
        lambda *a, **kw: SimpleNamespace(subjects={}, policy_version=1),
    )
    monkeypatch.setattr(gap_worker, "ssh_host_aliases", lambda *a, **kw: [])
    gap_worker.run_cycle(ledger)
    assert len(ops.store.list_ids()) == 1 and ledger.load(waiting.gap_id).status == "curating"


def test_real_ops_reopened_canonical_generation_reimports_same_upstream_once(tmp_path):
    ledger, ops, dispatcher = setup(tmp_path)
    record = ready(ledger)

    def source_hash(row):
        row.source_candidates[0]["source_hash"] = "c" * 64

    ledger.update(record.gap_id, source_hash)
    event = dict(
        need_class="missing_coverage",
        subject="widget",
        representative_question="docs for widget: authentication",
        facets=["authentication"],
        generation_attested=True,
        dispositions=["partial"],
    )
    ledger.submit_event(
        **event,
        event_key="canonical-initial",
        evidence_generation="a" * 64,
        coverage_revision="canonical-old",
    )
    first = dispatcher.dispatch(record.gap_id)
    ops.call("cancel", {"job_id": first.job_id})
    dispatcher.observe(record.gap_id, parse_job_reply(ops.call("status", {"job_id": first.job_id})))

    def resolved(row):
        row.status = "resolved"
        row.resolution_verification = {
            "outcome": "resolved",
            "canonical_revision": "canonical-old",
        }

    ledger.update(record.gap_id, resolved)
    reopened, _ = ledger.submit_event(
        **event,
        event_key="canonical-fresh",
        evidence_generation="b" * 64,
        coverage_revision="canonical-new",
    )
    assert reopened.status == "candidate" and len(reopened.resolution_history) == 1
    assert reopened.source_candidates[0]["source_hash"] == "c" * 64
    ledger.transition(record.gap_id, "ready_for_curation")
    second = dispatcher.dispatch(record.gap_id)
    assert second.action == "submitted" and second.job_id != first.job_id
    current = ledger.load(record.gap_id)
    assert current.attempts == 2 and len(ops.store.list_ids()) == 2
    assert current.submission_history[0]["input_sources"] == current.submission["input_sources"]
    assert current.submission_history[0]["input_signature"] != current.submission["input_signature"]
    assert dispatcher.dispatch(record.gap_id).action == "skipped"
    ops.call("cancel", {"job_id": second.job_id})
    dispatcher.observe(record.gap_id, parse_job_reply(ops.call("status", {"job_id": second.job_id})))
    ledger.transition(record.gap_id, "ready_for_curation")
    assert dispatcher.dispatch(record.gap_id).action == "skipped"
    assert len(ops.store.list_ids()) == 2
