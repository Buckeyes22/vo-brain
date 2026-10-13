"""Grouped needs verify their own questions and retain a working research owner."""

from types import SimpleNamespace

from test_gap_worker import env as env

from doclayer import gap_worker
from doclayer.coverage import SubjectEntry
from doclayer.discovery import DiscoveryResult
from doclayer.gaps import GapLedger


def pair(ledger):
    args = dict(
        subject="widget",
        representative_question="Does Widget retain text?",
        facets=["retains text"],
    )
    a, _ = ledger.submit_event(need_class="missing_coverage", **args)
    b, _ = ledger.submit_event(need_class="enrichment", **args)
    ledger.reconcile_duplicates()
    ledger.transition(a.gap_id, "resolved")
    return a, b


def test_grouped_child_is_independently_verified_after_parent_resolved(env, monkeypatch):
    _, ledger, _ = env
    parent, child = pair(ledger)
    called = []

    def verify(gap_id, owner):
        called.append(gap_id)
        owner.transition(gap_id, "resolved")
        return SimpleNamespace(outcome="resolved", notes=[])

    monkeypatch.setattr(gap_worker.verification, "verify_existing_gap", verify)
    result = gap_worker.run_cycle(ledger)
    assert called == [child.gap_id]
    assert ledger.load(parent.gap_id).status == ledger.load(child.gap_id).status == "resolved"
    assert not ledger.load(child.gap_id).job_links
    assert any("grouped need verification" in note for note in result["notes"])


def test_grouped_partial_without_seeds_reaches_own_source_discovery(env, monkeypatch):
    _, ledger, catalog = env
    catalog.subjects["widget"] = SubjectEntry(
        slug="widget",
        kind="product",
        title="Widget",
        public_docs={"base_url": "https://docs.widget.example/"},
    )
    _, child = pair(ledger)

    def verify(gap_id, owner):
        owner.transition(gap_id, "ready_for_curation")
        return SimpleNamespace(outcome="partial", notes=["Only part of original question answered"])

    researched = []

    def discover(record, *args, **kwargs):
        researched.append(record["gap_id"])
        return DiscoveryResult(record["gap_id"], "unresolved", notes=["source unavailable"])

    monkeypatch.setattr(gap_worker.verification, "verify_existing_gap", verify)
    monkeypatch.setattr(gap_worker, "discover", discover)
    gap_worker.run_cycle(ledger)
    assert researched == [child.gap_id]
    current = ledger.load(child.gap_id)
    assert current.status == "retry_wait" and not current.submission and not current.job_links
    assert not current.supersession.get("verification_required")


def test_ambiguous_record_cannot_strand_an_actionable_same_facet_owner(tmp_path):
    ledger = GapLedger(tmp_path)
    args = dict(
        subject="widget",
        representative_question="Does Widget retain text?",
        facets=["retains text"],
    )
    uncertain, _ = ledger.submit_event(need_class="ambiguous", **args)
    actionable, _ = ledger.submit_event(need_class="missing_coverage", **args)
    ledger.reconcile_duplicates()
    assert ledger.load(actionable.gap_id).status == "candidate"
    assert ledger.load(uncertain.gap_id).status == "candidate"


def test_private_local_need_with_attributed_text_is_not_submitted_to_curator(tmp_path):
    from local_sources import local_source
    from test_gap_contract_recovery import RealOps

    from doclayer.dispatcher import GapDispatcher
    from doclayer.local_material import DIRECT_AUTHORING_REASON

    ledger = GapLedger(tmp_path / "gaps")
    ledger.root.mkdir()
    (ledger.root / "curator-ready").touch()
    ops = RealOps(tmp_path)
    source = local_source(
        "Committed Widget setup",
        "Widget's configured edition is Community; runtime edition remains unknown.",
    )
    row, _ = ledger.submit_event(
        need_class="needs_local_evidence",
        subject="widget",
        representative_question="Which Widget edition is configured?",
        source_candidates=[source],
    )
    ledger.transition(row.gap_id, "ready_for_curation")
    outcome = GapDispatcher(ledger, transport=ops).dispatch(row.gap_id)
    assert outcome.action == "skipped" and outcome.detail == DIRECT_AUTHORING_REASON
    assert not [op for op, _ in ops.calls if op == "submit"]
    current = ledger.load(row.gap_id)
    assert current.status == "needs_local_evidence"
    assert not current.submission and not current.job_links and current.attempts == 0


def test_private_brain_need_is_parked_for_direct_authoring_not_curated(env, monkeypatch):
    from doclayer.local_material import DIRECT_AUTHORING_REASON, is_local_need

    _, ledger, _ = env
    row, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="brain",
        representative_question="Which model handles wiki curation?",
    )
    ledger.transition(row.gap_id, "retry_wait", backoff_until="2999-01-01T00:00:00+00:00")
    assert is_local_need(ledger.load(row.gap_id))
    monkeypatch.setattr(
        gap_worker,
        "discover",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("public discovery")),
    )
    gap_worker.run_cycle(ledger)
    current = ledger.load(row.gap_id)
    assert current.status == "needs_local_evidence" and current.backoff_until
    assert DIRECT_AUTHORING_REASON in ledger.events_path(row.gap_id).read_text()
    assert not current.source_candidates and not current.job_links
    assert current.attempts == 0 and not current.submission
    before = current.research_attempts
    gap_worker.run_cycle(ledger)
    assert ledger.load(row.gap_id).research_attempts == before


def test_unsupported_local_runtime_need_keeps_new_backoff_without_rework(env, monkeypatch, identities):
    from doclayer.local_material import DIRECT_AUTHORING_REASON, is_local_need

    identities(hosts=["svc1"])
    _, ledger, _ = env
    row, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="nimbus",
        representative_question="Which runtime hosts Nimbus on svc1?",
    )
    ledger.transition(row.gap_id, "retry_wait", backoff_until="2999-01-01T00:00:00+00:00")
    assert is_local_need(ledger.load(row.gap_id))
    monkeypatch.setattr(
        gap_worker,
        "discover",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("public discovery")),
    )
    gap_worker.run_cycle(ledger)
    current = ledger.load(row.gap_id)
    assert current.status == "needs_local_evidence" and current.backoff_until
    assert DIRECT_AUTHORING_REASON in ledger.events_path(row.gap_id).read_text()
    assert not current.source_candidates and not current.job_links
    parked = ledger.record_path(row.gap_id).read_bytes()
    gap_worker.run_cycle(ledger)
    assert ledger.record_path(row.gap_id).read_bytes() == parked


def test_prior_native_attempt_cannot_bypass_local_collection_backoff(env, monkeypatch):
    _, ledger, _ = env
    row, _ = ledger.submit_event(
        need_class="needs_local_evidence",
        subject="brain",
        representative_question="Which model handles wiki curation?",
    )
    ledger.update(row.gap_id, lambda record: setattr(record, "attempts", 1))
    ledger.transition(row.gap_id, "retry_wait", backoff_until="2999-01-01T00:00:00+00:00")
    before = ledger.record_path(row.gap_id).read_bytes()
    gap_worker.run_cycle(ledger)
    assert ledger.record_path(row.gap_id).read_bytes() == before


def _uncovered_private_need(ledger):
    row, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="unidentified",
        representative_question="What is the supported SSH access route to svc1?",
    )
    return row


def test_private_need_answered_by_an_existing_page_resolves_instead_of_parking(env, monkeypatch, identities):
    identities(hosts=["svc1"])
    _, ledger, _ = env
    row = _uncovered_private_need(ledger)
    calls = []

    def verify(gap_id, owner, **kwargs):
        calls.append(kwargs)
        owner.transition(gap_id, "resolved")
        return SimpleNamespace(outcome="resolved", notes=[])

    monkeypatch.setattr(gap_worker.verification, "verify_existing_gap", verify)
    gap_worker.run_cycle(ledger)
    assert calls == [{"resolve_only": True}]
    assert ledger.load(row.gap_id).status == "resolved"


def test_private_need_without_an_existing_answer_is_still_parked(env, monkeypatch, identities):
    from doclayer.local_material import DIRECT_AUTHORING_REASON

    identities(hosts=["svc1"])
    _, ledger, _ = env
    row = _uncovered_private_need(ledger)
    monkeypatch.setattr(
        gap_worker.verification,
        "verify_existing_gap",
        lambda gap_id, owner, **kwargs: SimpleNamespace(outcome="failed", notes=[]),
    )
    gap_worker.run_cycle(ledger)
    current = ledger.load(row.gap_id)
    assert current.status == "needs_local_evidence" and current.backoff_until
    assert DIRECT_AUTHORING_REASON in ledger.events_path(row.gap_id).read_text()
