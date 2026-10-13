"""A verification outcome never overwrites a gap closed meanwhile."""

import json

import httpx
import pytest
from test_verification_contract import client, git, positive, published  # noqa: F401

from doclayer import gap_ops, gap_worker, verification
from doclayer.curator.model import ModelClient
from doclayer.gap_budget import ResourceBudget


def superseding_client(tmp_path, ledger, gap_id, primary_id, reply):
    """A model client that supersedes the gap while the model call is in flight."""
    base, usage = client(tmp_path, reply)

    def response(request):
        gap_ops.handle("supersede", {"gap_id": gap_id, "by": primary_id}, ledger)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}]}
        )

    return ModelClient(base.cfg, usage, transport=httpx.MockTransport(response))


def prepare(published):
    root, path, commit, ledger, record, reply, index = published
    primary, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="nimbus",
        catalog_subject=True,
        representative_question="What is the Nimbus rate limit per minute?",
        facets=["rate"],
    )
    ledger.update(record.gap_id, lambda r: setattr(r, "job_links", []))
    ledger.transition(record.gap_id, "needs_local_evidence", reason="fixture")
    return path, ledger, record, primary


@pytest.mark.parametrize("answers", [True, False])
def test_superseding_during_the_model_call_survives_verify_existing(published, tmp_path, answers):
    path, ledger, record, primary = prepare(published)
    reply = (
        positive(path)
        if answers
        else dict(
            answers=False, facets=[dict(facet="rate", answered=False, scope_preserved=True, evidence=[])], reason="no"
        )
    )
    model = superseding_client(tmp_path, ledger, record.gap_id, primary.gap_id, reply)
    result = verification.verify_existing_gap(
        record.gap_id, ledger, model_client=model, budget=ResourceBudget(ledger.root)
    )
    saved = ledger.load(record.gap_id)
    assert saved.status == "superseded" and saved.superseded_by == primary.gap_id
    assert result.outcome == "superseded"
    assert any("superseded during verification" in note for note in result.notes)


def test_worker_defer_and_transitions_skip_a_gap_closed_after_verification(published, tmp_path, monkeypatch):
    path, ledger, record, primary = prepare(published)
    ledger.update(record.gap_id, lambda r: (setattr(r, "backoff_until", None), setattr(r, "private_attempts", 1)))

    def verify_then_supersede(gap_id, led, **kwargs):
        gap_ops.handle("supersede", {"gap_id": gap_id, "by": primary.gap_id}, led)
        from types import SimpleNamespace

        return SimpleNamespace(outcome="failed", notes=["x"])

    monkeypatch.setattr(verification, "verify_existing_gap", verify_then_supersede)
    gap_worker._reverify_jobless(ledger, [])
    saved = ledger.load(record.gap_id)
    assert saved.status == "superseded" and saved.superseded_by == primary.gap_id
    assert saved.backoff_until is None or "reverify_attempts" not in saved.resolution_verification


def test_research_of_a_gap_superseded_during_its_verification_does_not_continue(published, tmp_path, monkeypatch):
    path, ledger, record, primary = prepare(published)
    reply = positive(path)
    model = superseding_client(tmp_path, ledger, record.gap_id, primary.gap_id, reply)
    real = verification.verify_existing_gap
    monkeypatch.setattr(
        verification,
        "verify_existing_gap",
        lambda gap_id, led, **kw: real(gap_id, led, model_client=model, budget=ResourceBudget(led.root)),
    )
    ledger.update(record.gap_id, lambda r: setattr(r, "need_class", "retrieval_defect"))
    ledger.transition(record.gap_id, "candidate", reason="fixture")
    notes = []
    gap_worker._research_one(ledger, ledger.load(record.gap_id), type("C", (), {"subjects": {}})(), {}, notes)
    saved = ledger.load(record.gap_id)
    assert saved.status == "superseded" and saved.superseded_by == primary.gap_id
