"""Worker-cycle and verification tests: candidate → discovery → ready →
dispatch (dry-run) → publication → index acknowledgement → resolved, plus the
retrieval-defect and outage paths."""

from __future__ import annotations

import pytest

# Verification contract regressions use actual ops/Git/model transport fixtures;
# the previous FakeCuratorTransport top-level shape could not occur in production.
from test_verification_contract import client, positive, published  # noqa: F401

from doclayer import gap_worker, verification
from doclayer.coverage import Catalog
from doclayer.curator.transport import Transport
from doclayer.dispatcher import GapDispatcher
from doclayer.gap_budget import ResourceBudget
from doclayer.gaps import GapLedger
from doclayer.settings import settings


class FakeCuratorTransport(Transport):
    """Curator job that publishes immediately with one landed page."""

    def __init__(self, changes):
        self.changes = changes
        self.calls = []

    def call(self, op, payload, **kwargs):
        self.calls.append({"op": op, "payload": payload})
        if op == "submit":
            return {"ok": True, "job_id": "job-fake0001", "created": True}
        if op == "status":
            return {
                "ok": True,
                "status": "published",
                "publication": {"merged": True, "merge_commit": "c" * 40},
            }
        if op == "result":
            return {
                "ok": True,
                "result": {
                    "changes": self.changes,
                    "publication": {"merged": True, "merge_commit": "c" * 40},
                },
            }
        raise AssertionError(op)


@pytest.fixture
def env(tmp_path, monkeypatch):
    ledger = GapLedger(tmp_path / "gaps")
    (tmp_path / "gaps").mkdir(parents=True, exist_ok=True)
    (tmp_path / "gaps" / "curator-ready").write_text("ready\n")
    catalog = Catalog(status="ok", revision="a" * 40, policy_version=1)

    class FakeBrainRepo:
        root = tmp_path

        def markdown_paths(self):
            return []

        def read_page(self, path):
            raise AssertionError("no pages")

        def source_paths(self, subject):
            return []

        def has_path(self, path):
            return False

        def head_commit(self):
            return "a" * 40

    monkeypatch.setattr("doclayer.gap_worker.load_catalog", lambda *a, **k: catalog)
    monkeypatch.setattr("doclayer.gap_worker.ssh_host_aliases", lambda *a, **k: [])
    monkeypatch.setattr(settings, "gap_state_dir", tmp_path / "gaps")
    monkeypatch.setattr(settings, "negative_cache_path", tmp_path / "nc.json")
    real_verify_existing = verification.verify_existing_gap

    def verify_existing(gap_id, owner, **kwargs):
        # The pre-park check proves nothing here unless a test patches it.
        if kwargs.get("resolve_only"):
            return verification.VerificationResult(gap_id, "awaiting_index")
        return real_verify_existing(gap_id, owner, **kwargs)

    monkeypatch.setattr(verification, "verify_existing_gap", verify_existing)
    return tmp_path, ledger, catalog


def test_outage_and_retrieval_defects_follow_their_own_paths(env):
    tmp_path, ledger, catalog = env
    # an outage gap is captured and never dispatched for import
    record, _ = ledger.submit_event(
        need_class="infrastructure", subject="vo-rag", representative_question="is vo-rag up?"
    )
    # a retrieval defect is recorded with reproducible examples, no import
    defect, _ = ledger.submit_event(
        need_class="retrieval_defect", subject="prod", representative_question="where is it?"
    )
    ledger.transition(defect.gap_id, "retrieval_defect", reason="committed evidence exists")
    gap_worker.run_cycle(ledger)
    assert ledger.load(record.gap_id).status == "candidate"
    assert ledger.load(defect.gap_id).status == "retrieval_defect"


def test_end_to_end_dry_run_until_ready_then_submission(env):
    tmp_path, ledger, catalog = env
    record, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject=None,
        representative_question="docs for widget-thing",
        identity_override="unknown:widget thing",
    )
    # discovery is bounded and offline here: no network -> unresolved -> wait
    gap_worker.run_cycle(ledger)
    reloaded = ledger.load(record.gap_id)
    assert reloaded.status in ("researching", "retry_wait")

    # with seeds attached, the gap becomes dispatchable
    reloaded.source_candidates = [
        {"url": "https://docs.example/widget-thing", "origin": "attached", "authority": "official"}
    ]
    ledger.save(reloaded)
    ledger.transition(reloaded.gap_id, "ready_for_curation", reason="seeds attached")
    transport = FakeCuratorTransport(changes=[{"path": "wiki/widget-thing.md", "action": "add", "status": "published"}])

    outcome = GapDispatcher(ledger, transport=transport).dispatch(record.gap_id)
    assert outcome.action == "submitted"
    assert ledger.load(record.gap_id).status == "curating"


def test_dispatch_without_readiness_stays_dry_run(env):
    tmp_path, ledger, catalog = env
    (tmp_path / "gaps" / "curator-ready").unlink()
    record, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject=None,
        representative_question="docs for widget-thing",
        identity_override="unknown:widget thing",
    )
    record.source_candidates = [{"url": "https://docs.example/widget-thing", "authority": "official"}]
    ledger.save(record)
    ledger.transition(record.gap_id, "ready_for_curation", reason="seeds")
    transport = FakeCuratorTransport(changes=[])

    outcome = GapDispatcher(ledger, transport=transport).dispatch(record.gap_id)
    assert outcome.action == "dry-run"
    assert transport.calls == []  # nothing submitted to the curator


def test_verification_resolved_path(published, tmp_path):
    root, path, commit, ledger, record, reply, index = published
    model, usage = client(tmp_path, positive(path))
    result = verification.verify_gap(
        record.gap_id, ledger, job_result=reply, model_client=model, budget=ResourceBudget(ledger.root)
    )
    assert result.outcome == "resolved" and ledger.load(record.gap_id).status == "resolved"


def test_verification_retrieval_defect_path(published, tmp_path, monkeypatch):
    root, path, commit, ledger, record, reply, index = published
    # Canonical source independently answers; actual delivered cited paths do
    # not include the publication. Discovery membership cannot close it.
    monkeypatch.setattr(verification, "_delivered_pages", lambda answer, pages: [])
    model, usage = client(tmp_path, positive(path))
    result = verification.verify_gap(
        record.gap_id, ledger, job_result=reply, model_client=model, budget=ResourceBudget(ledger.root)
    )
    assert result.outcome == "retrieval_defect" and ledger.load(record.gap_id).status == "retrieval_defect"


def test_verification_requires_index_acknowledgement(published, tmp_path):
    root, path, commit, ledger, record, reply, index = published
    index["files"][path] = "0" * 64
    model, usage = client(tmp_path, positive(path))
    result = verification.verify_gap(
        record.gap_id, ledger, job_result=reply, model_client=model, budget=ResourceBudget(ledger.root)
    )
    assert result.outcome == "awaiting_index" and not result.index_acknowledged and usage.model_calls == 0


def test_verification_failed_unrelated_import_stays_unresolved(published, tmp_path):
    root, path, commit, ledger, record, reply, index = published
    model, usage = client(
        tmp_path,
        dict(
            answers=False,
            facets=[dict(facet="rate", answered=False, scope_preserved=True, evidence=[])],
            reason="Plausible documentation does not establish requested rate",
        ),
    )
    result = verification.verify_gap(
        record.gap_id, ledger, job_result=reply, model_client=model, budget=ResourceBudget(ledger.root)
    )
    assert result.outcome == "failed" and ledger.load(record.gap_id).status == "failed"
    assert ledger.load(record.gap_id).job_links == record.job_links
