"""Accepted canonical verdicts survive delivery failures without recharging."""

import json

import httpx
import pytest
from test_verification_contract import client, git, positive
from test_verification_contract import published as published

from doclayer import verification
from doclayer.curator.model import ModelClient
from doclayer.gap_budget import ResourceBudget, today
from doclayer.gap_ops import handle


def native(tmp_path, path, replies):
    model, usage = client(tmp_path, positive(path))
    seen = []

    def transport(request):
        seen.append(json.loads(request.content))
        value = replies.pop(0)
        if value is None:
            return httpx.Response(503, text="fixture outage")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(value)}, "finish_reason": "stop"}]},
        )

    return (
        ModelClient(model.cfg, usage, transport=httpx.MockTransport(transport)),
        usage,
        seen,
    )


def test_delivery_outage_retains_canonical_and_restart_only_charges_delivery(published, tmp_path):
    root, path, commit, ledger, record, reply, index = published
    model, usage, seen = native(tmp_path, path, [positive(path), None, positive(path)])
    budget = ResourceBudget(ledger.root)
    first = verification.verify_gap(record.gap_id, ledger, job_result=reply, model_client=model, budget=budget)
    saved = ledger.load(record.gap_id)
    assert first.outcome == "verifying" and saved.status == "verifying"
    checkpoint = saved.resolution_verification["canonical_checkpoint"]
    assert checkpoint["verdict"] == positive(path) and usage.model_calls == 2
    resumed, resumed_usage, resumed_seen = native(tmp_path, path, [positive(path)])
    resumed.cfg = model.cfg
    completed = verification.verify_gap(record.gap_id, ledger, job_result=reply, model_client=resumed, budget=budget)
    assert completed.outcome == "resolved"
    assert usage.model_calls == 2 and resumed_usage.model_calls == 1
    assert len(seen) == 2 and len(resumed_seen) == 1
    assert ledger.load(record.gap_id).resolution_verification["canonical_checkpoint"] == checkpoint
    assert budget.load(today())["model_calls"] == 3


@pytest.mark.parametrize("change", ["need", "source", "policy", "tampered", "tampered_resealed", "legacy"])
def test_changed_binding_or_malformed_saved_verdict_requires_fresh_judgement(published, tmp_path, change):
    root, path, commit, ledger, record, reply, index = published
    model, usage, seen = native(tmp_path, path, [positive(path), None, positive(path), positive(path)])
    budget = ResourceBudget(ledger.root)
    verification.verify_gap(record.gap_id, ledger, job_result=reply, model_client=model, budget=budget)
    saved = ledger.load(record.gap_id)
    if change == "need":
        saved.representative_question = "What is Nimbus request rate limit in RPM?"
        ledger.save(saved)
    elif change == "source":
        (root / path).write_text((root / path).read_text() + "\nAn additional scoped note.\n")
        git(root, "add", path)
        git(root, "commit", "-m", "Source change")
        import hashlib

        index["last_commit"] = git(root, "rev-parse", "HEAD")
        index["files"][path] = hashlib.sha256((root / path).read_bytes()).hexdigest()
    elif change == "policy":
        model.cfg.inference.enable_thinking = True
    elif change == "legacy":
        saved.resolution_verification.pop("canonical_checkpoint")
        saved.resolution_verification.update(outcome="retrieval_defect", page_hashes=index["files"])
        ledger.save(saved)
    else:
        saved.resolution_verification["canonical_checkpoint"]["verdict"]["facets"][0]["evidence"][0]["quote"] = (
            "Fabricated quote."
        )
        if change == "tampered_resealed":
            import hashlib

            checkpoint = saved.resolution_verification["canonical_checkpoint"]
            checkpoint["verdict_hash"] = hashlib.sha256(
                json.dumps(checkpoint["verdict"], sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
        ledger.save(saved)
    completed = verification.verify_gap(record.gap_id, ledger, job_result=reply, model_client=model, budget=budget)
    assert completed.outcome == "resolved" and usage.model_calls == 4 and len(seen) == 4
    assert budget.load(today())["model_calls"] == 4


def test_late_budget_deferral_stays_verifying_and_reuses_proof_next_day(published, tmp_path, monkeypatch):
    from doclayer import gap_budget

    root, path, commit, ledger, record, reply, index = published
    model, usage, seen = native(tmp_path, path, [positive(path), positive(path)])
    budget = ResourceBudget(ledger.root, max_calls_day=1)
    first = verification.verify_gap(record.gap_id, ledger, job_result=reply, model_client=model, budget=budget)
    assert first.outcome == "verifying" and ledger.load(record.gap_id).status == "verifying"
    old_day = today()
    assert budget.load(old_day)["model_calls"] == 1 and usage.model_calls == 1
    verification.verify_gap(record.gap_id, ledger, job_result=reply, model_client=model, budget=budget)
    assert len(seen) == 1 and usage.model_calls == 1  # No recharge on the exhausted day.
    monkeypatch.setattr(gap_budget, "today", lambda: "2099-01-01")
    done = verification.verify_gap(record.gap_id, ledger, job_result=reply, model_client=model, budget=budget)
    assert done.outcome == "resolved" and len(seen) == 2 and usage.model_calls == 2
    assert budget.load(old_day)["model_calls"] == 1 and budget.load("2099-01-01")["model_calls"] == 1


@pytest.mark.parametrize("remote", ["published", "partial", "covered", "failed"])
def test_retry_keeps_verified_publication_job_but_failed_import_retries_normally(published, remote):
    root, path, commit, ledger, record, reply, index = published
    record.status = "retrieval_defect"
    record.attempts = 1
    record.submission = {
        "state": "terminal",
        "job_id": record.job_links[-1]["job_id"],
        "remote_status": remote,
        "attempt": 1,
        "reservation": {"calls": 50},
    }
    record.publication_revision = commit if remote in ("published", "partial") else None
    record.resolution_verification = {
        "index_acknowledged": True,
        "verified_paths": [path],
        "publication_commit": record.publication_revision,
        "basis": "existing canonical evidence (covered no-op)" if remote == "covered" else "merged publication",
    }
    ledger.save(record)
    old = record.submission.copy()
    result = handle("retry", {"gap_id": record.gap_id}, ledger)["record"]
    if remote == "failed":
        assert result["status"] == "candidate" and result["submission"]["manual_retry"] is True
    else:
        assert result["status"] == "awaiting_index" and result["submission"] == old
    assert result["attempts"] == 1 and result["job_links"] == record.job_links


def test_a_changed_landed_page_set_invalidates_the_stored_canonical_binding(published, tmp_path, monkeypatch):
    """The binding hashes the whole page set, so a candidate page that joins it forces a fresh judgement."""
    import hashlib
    from types import SimpleNamespace

    from doclayer import resolver

    root, path, commit, ledger, record, reply, index = published
    extra = "wiki/nimbus-limits.md"
    (root / extra).write_text("---\ntitle: Nimbus limits\n---\n# Limits\nNimbus request rate limit is 40 RPM.\n")
    git(root, "add", extra)
    git(root, "commit", "-m", "second page")
    head = git(root, "rev-parse", "HEAD")
    index.update(
        last_commit=head,
        files={p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in (path, extra)},
        file_writer_versions={p: "brain-v4-governing-context" for p in (path, extra)},
    )
    ledger.update(record.gap_id, lambda r: setattr(r, "job_links", []))
    candidates = [path]

    async def fake_resolve(*args, **kwargs):
        context = "Nimbus request rate limit is 40 RPM."
        facts = [dict(source=f"{p}@{head[:8]}", value="x", evidence_context=context) for p in candidates]
        return SimpleNamespace(
            git_facts=facts,
            doc_facts=[],
            search_hits=[],
            scope_constraints=[],
            question=record.representative_question,
            _prose=lambda: context,
        )

    monkeypatch.setattr(resolver, "resolve", fake_resolve)
    model, usage, seen = native(tmp_path, path, [positive(path)] * 6)
    budget = ResourceBudget(ledger.root)

    def run():
        return verification.verify_existing_gap(record.gap_id, ledger, model_client=model, budget=budget)

    first = run()
    calls_after_first = usage.model_calls
    assert first.outcome == "resolved" and calls_after_first == 2
    # same page set: the stored canonical verdict is reused (only the delivered proof is charged)
    run()
    assert usage.model_calls == calls_after_first + 1
    # a second page joins the landed set: the binding no longer matches, so both proofs are judged again
    candidates.append(extra)
    before = usage.model_calls
    run()
    assert usage.model_calls == before + 2
