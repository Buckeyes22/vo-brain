"""A private partial goes to needs_local_evidence; an idle verifying gap can be superseded."""

import json

import pytest
from test_verification_contract import client, published  # noqa: F401

from doclayer import verification
from doclayer.gap_budget import ResourceBudget
from doclayer.gap_ops import handle
from doclayer.gaps import GapLedger
from doclayer.local_material import DIRECT_AUTHORING_REASON


def partial_reply(path):
    """Two facets: the first is answered with a literal quote, the second is not."""
    return dict(
        answers=False,
        reason="partial",
        facets=[
            dict(
                facet="rate",
                answered=True,
                scope_preserved=True,
                evidence=[dict(path=path, quote="Nimbus request rate limit is 40 RPM.")],
            ),
            dict(facet="burst", answered=False, scope_preserved=True, evidence=[]),
        ],
    )


def run(published, tmp_path, *, private):
    root, path, commit, ledger, record, reply, index = published

    def setup(r):
        r.facets = ["rate", "burst"]
        if private:
            r.need_class = "needs_local_evidence"

    ledger.update(record.gap_id, setup)
    model, usage = client(tmp_path, partial_reply(path))
    result = verification.verify_gap(
        record.gap_id, ledger, job_result=reply, model_client=model, budget=ResourceBudget(ledger.root)
    )
    return result, ledger, record.gap_id


def transitions(ledger, gap_id):
    lines = ledger.events_path(gap_id).read_text().splitlines()
    return [e for e in map(json.loads, lines) if e.get("kind") == "transition"]


def test_a_public_partial_goes_back_to_curation(published, tmp_path):
    result, ledger, gap_id = run(published, tmp_path, private=False)
    assert result.outcome == "partial"
    assert ledger.load(gap_id).status == "ready_for_curation"
    assert transitions(ledger, gap_id)[-1]["status"] == "ready_for_curation"


def test_a_private_partial_goes_to_needs_local_evidence_with_the_direct_authoring_reason(published, tmp_path):
    result, ledger, gap_id = run(published, tmp_path, private=True)
    assert result.outcome == "partial"
    assert ledger.load(gap_id).status == "needs_local_evidence"
    seen = transitions(ledger, gap_id)
    assert seen[-1]["status"] == "needs_local_evidence" and seen[-1]["reason"] == DIRECT_AUTHORING_REASON[:500]
    assert all(e["status"] != "ready_for_curation" for e in seen)


# -- supersede uses the same idle-verifying guard as suppress ----------------------------------- #


def _gap(ledger, question):
    row, _ = ledger.submit_event(
        need_class="missing_coverage", subject="dispatch-cli", representative_question=question
    )
    return row.gap_id


def test_supersede_an_idle_verifying_gap_without_lease_or_job(tmp_path):
    ledger = GapLedger(tmp_path)
    primary = _gap(ledger, "How is dispatch-cli installed?")
    gid = _gap(ledger, "How do I install dispatch-cli?")
    ledger.transition(gid, "verifying")
    assert ledger.load(gid).lease is None and not ledger.load(gid).job_links
    out = handle("supersede", {"gap_id": gid, "by": primary}, ledger)
    record = ledger.load(gid)
    assert out["ok"] is True and record.status == "superseded" and record.superseded_by == primary


def test_supersede_a_verifying_gap_with_a_terminal_job(tmp_path):
    ledger = GapLedger(tmp_path)
    primary = _gap(ledger, "How is dispatch-cli installed?")
    gid = _gap(ledger, "How do I install dispatch-cli?")
    ledger.update(
        gid,
        lambda r: (
            r.job_links.append({"job_id": "job-1"}),
            r.submission.update(state="terminal", job_id="job-1", remote_status="published"),
        ),
    )
    ledger.transition(gid, "verifying")
    handle("supersede", {"gap_id": gid, "by": primary}, ledger)
    assert ledger.load(gid).status == "superseded"


def test_supersede_refuses_a_verifying_gap_holding_a_lease(tmp_path):
    ledger = GapLedger(tmp_path)
    primary = _gap(ledger, "How is dispatch-cli installed?")
    gid = _gap(ledger, "How do I install dispatch-cli?")
    ledger.transition(gid, "verifying")
    assert ledger.claim(gid, "worker-a")
    with pytest.raises(ValueError, match="active gap"):
        handle("supersede", {"gap_id": gid, "by": primary}, ledger)
    assert ledger.load(gid).status == "verifying"


@pytest.mark.parametrize("state", [None, "staged", "accepted", "uncertain"])
def test_supersede_refuses_a_verifying_gap_with_an_in_flight_job(tmp_path, state):
    ledger = GapLedger(tmp_path)
    primary = _gap(ledger, "How is dispatch-cli installed?")
    gid = _gap(ledger, "How do I install dispatch-cli?")
    ledger.update(
        gid,
        lambda r: (
            r.job_links.append({"job_id": "job-1"}),
            r.submission.update(state=state, job_id="job-1") if state else None,
        ),
    )
    ledger.transition(gid, "verifying")
    with pytest.raises(ValueError, match="active gap"):
        handle("supersede", {"gap_id": gid, "by": primary}, ledger)
    assert ledger.load(gid).status == "verifying"


@pytest.mark.parametrize("status", ["queued", "curating", "awaiting_index"])
def test_supersede_still_refuses_other_active_states(tmp_path, status):
    ledger = GapLedger(tmp_path)
    primary = _gap(ledger, "How is dispatch-cli installed?")
    gid = _gap(ledger, "How do I install dispatch-cli?")
    ledger.transition(gid, status)
    with pytest.raises(ValueError, match="active gap"):
        handle("supersede", {"gap_id": gid, "by": primary}, ledger)
    assert ledger.load(gid).status == status


@pytest.mark.parametrize("operation", ["suppress", "supersede"])
def test_suppress_and_supersede_both_act_on_an_idle_verifying_gap(tmp_path, operation):
    ledger = GapLedger(tmp_path)
    primary = _gap(ledger, "How is dispatch-cli installed?")
    gid = _gap(ledger, "How do I install dispatch-cli?")
    ledger.transition(gid, "verifying")
    payload = {"gap_id": gid, "by": primary} if operation == "supersede" else {"gap_id": gid, "reason": "x"}
    assert handle(operation, payload, ledger)["ok"] is True
    assert ledger.load(gid).status == ("superseded" if operation == "supersede" else "suppressed")


@pytest.mark.parametrize("operation", ["suppress", "supersede"])
def test_suppress_and_supersede_both_refuse_a_leased_verifying_gap(tmp_path, operation):
    ledger = GapLedger(tmp_path)
    primary = _gap(ledger, "How is dispatch-cli installed?")
    gid = _gap(ledger, "How do I install dispatch-cli?")
    ledger.transition(gid, "verifying")
    assert ledger.claim(gid, "worker-a")
    payload = {"gap_id": gid, "by": primary} if operation == "supersede" else {"gap_id": gid, "reason": "x"}
    with pytest.raises(ValueError, match="active gap"):
        handle(operation, payload, ledger)
    assert ledger.load(gid).status == "verifying"


# -- the privacy judgement: regex path, catalog path, and a catalog failure ----------------------- #


def test_a_gap_private_by_its_question_regex_goes_to_needs_local_evidence(published, tmp_path):
    root, path, commit, ledger, record, reply, index = published
    ledger.update(
        record.gap_id,
        lambda r: (
            setattr(r, "facets", ["rate", "burst"]),
            setattr(
                r, "representative_question", "What is the Nimbus rate limit configured in /etc/nimbus/nimbus.conf?"
            ),
        ),
    )
    model, _ = client(tmp_path, partial_reply(path))
    result = verification.verify_gap(
        record.gap_id, ledger, job_result=reply, model_client=model, budget=ResourceBudget(ledger.root)
    )
    assert result.outcome == "partial"
    assert ledger.load(record.gap_id).status == "needs_local_evidence"
    assert ledger.load(record.gap_id).need_class != "needs_local_evidence"


def test_a_gap_private_only_by_its_catalog_entry_goes_to_needs_local_evidence(published, tmp_path, monkeypatch):
    from doclayer import coverage
    from doclayer.coverage import Catalog, SubjectEntry

    root, path, commit, ledger, record, reply, index = published
    ledger.update(record.gap_id, lambda r: setattr(r, "facets", ["rate", "burst"]))
    catalog = Catalog(
        status="ok",
        revision="x",
        policy_version=1,
        subjects={"nimbus": SubjectEntry(slug="nimbus", kind="host", title="Nimbus")},
    )
    loads = []
    monkeypatch.setattr(coverage, "load_catalog", lambda *a, **k: loads.append(a) or catalog)
    model, _ = client(tmp_path, partial_reply(path))
    result = verification.verify_gap(
        record.gap_id, ledger, job_result=reply, model_client=model, budget=ResourceBudget(ledger.root)
    )
    assert result.outcome == "partial" and len(loads) == 1  # the catalog is read once per verify call
    assert ledger.load(record.gap_id).status == "needs_local_evidence"


def test_a_catalog_failure_leaves_the_gap_pending_not_in_curation(published, tmp_path, monkeypatch):
    from doclayer import coverage

    root, path, commit, ledger, record, reply, index = published
    ledger.update(record.gap_id, lambda r: setattr(r, "facets", ["rate", "burst"]))

    def broken(*args, **kwargs):
        raise RuntimeError("catalog cache unreadable")

    monkeypatch.setattr(coverage, "load_catalog", broken)
    model, _ = client(tmp_path, partial_reply(path))
    result = verification.verify_gap(
        record.gap_id, ledger, job_result=reply, model_client=model, budget=ResourceBudget(ledger.root)
    )
    assert result.outcome == "verifying" and any("catalog cache unreadable" in n for n in result.notes)
    assert ledger.load(record.gap_id).status != "ready_for_curation"


def test_the_catalog_is_not_read_for_outcomes_other_than_partial(published, tmp_path, monkeypatch):
    from doclayer import coverage

    root, path, commit, ledger, record, reply, index = published
    loads = []
    monkeypatch.setattr(coverage, "load_catalog", lambda *a, **k: loads.append(a) or None)
    from test_verification_contract import positive

    model, _ = client(tmp_path, positive(path))
    result = verification.verify_gap(
        record.gap_id, ledger, job_result=reply, model_client=model, budget=ResourceBudget(ledger.root)
    )
    assert result.outcome == "resolved" and loads == []


# -- the worker keeps a private partial in needs_local_evidence ---------------------------------- #


def test_requeue_uncovered_keeps_a_private_partial_and_requeues_the_rest(tmp_path):
    from doclayer import gap_worker

    ledger = GapLedger(tmp_path)
    private = _gap(ledger, "Where is the dispatch-cli config?")
    ledger.transition(private, "needs_local_evidence")
    gap_worker._requeue_uncovered(ledger, private, "partial", "research")
    assert ledger.load(private).status == "needs_local_evidence"
    # a failed outcome, or a public partial (not parked), still goes back to research
    gap_worker._requeue_uncovered(ledger, private, "failed", "research")
    assert ledger.load(private).status == "candidate"
    public = _gap(ledger, "How do I install dispatch-cli?")
    ledger.transition(public, "ready_for_curation")
    gap_worker._requeue_uncovered(ledger, public, "partial", "research")
    assert ledger.load(public).status == "candidate"


def test_the_jobless_reverify_sweep_does_not_overwrite_a_private_partial(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from doclayer import gap_worker

    ledger = GapLedger(tmp_path)
    gid = _gap(ledger, "Where is the dispatch-cli config?")
    ledger.transition(gid, "verifying")

    def fake(gap_id, led, **kwargs):
        led.transition(gap_id, "needs_local_evidence", reason=DIRECT_AUTHORING_REASON)
        return SimpleNamespace(outcome="partial", notes=[])

    monkeypatch.setattr(verification, "verify_existing_gap", fake)
    gap_worker._reverify_jobless(ledger, [])
    assert ledger.load(gid).status == "needs_local_evidence"
