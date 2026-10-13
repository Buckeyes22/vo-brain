"""Assumption audit, Lane 2: the gap worker no longer withholds knowledge.

- A private need gathers evidence before the same pages are judged again.
- One cycle handles every eligible need, least-recently-claimed first, bounded
  by wall-clock time; a proof-pending wait grows instead of hot-looping.
- A private need is never failed only for having tried; retries back off and
  the planner sees its earlier plans.
- Private jobs may update up to `single_pass.max_pages` pages; no 50-call literal.
- An operator chat remark is suppressed as "not a documentation need".
"""

from __future__ import annotations

import hashlib
import inspect
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_gap_worker import FakeCuratorTransport  # noqa: F401
from test_gap_worker import env as env

from doclayer import dispatcher as dispatcher_module
from doclayer import gap_worker, private_writer, verification
from doclayer.coverage import CatalogPage, SubjectEntry
from doclayer.curator.config import CuratorConfig
from doclayer.discovery import DiscoveryResult
from doclayer.dispatcher import GapDispatcher
from doclayer.gaps import NOT_A_DOCUMENTATION_NEED, GapLedger, is_chat_remark
from doclayer.private_writer import PRIVATE_ORIGIN, Gathered

NOW = datetime(2026, 10, 10, 18, 0, tzinfo=UTC)


def _past() -> str:
    return (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")


def _wait_s(row) -> float:
    return (datetime.fromisoformat(row.backoff_until) - datetime.now(UTC)).total_seconds()


def _private_gap(ledger, question="What is the installed acli tool on SVC1 and its authentication method?"):
    row, _ = ledger.submit_event(
        need_class="needs_local_evidence", subject="acli", representative_question=question, facets=["installation"]
    )
    return row


def _unresolved_discovery(seen):
    def discover(record, catalog, **kwargs):
        seen.append(record["gap_id"])
        return DiscoveryResult(record["gap_id"], "unresolved", notes=["offline test"])

    return discover


# -- Task 1: evidence before re-judging ------------------------------------ #


def test_parked_private_need_gathers_evidence_before_any_jobless_reverify(env, monkeypatch):
    _, ledger, _ = env
    row = _private_gap(ledger)
    ledger.transition(row.gap_id, "needs_local_evidence", reason="partial: direct authoring", backoff_until=_past())
    order = []

    def verify(gap_id, owner, **kwargs):
        order.append(("verify", gap_id))
        return SimpleNamespace(outcome="partial", notes=["does not answer"])

    def prepare(ledger_, record, catalog, notes, **kwargs):
        order.append(("evidence", record.gap_id))
        ledger_.transition(
            record.gap_id,
            "ready_for_curation",
            reason="private evidence gathered",
            private_attempts=record.private_attempts + 1,
        )
        return True

    monkeypatch.setattr(verification, "verify_existing_gap", verify)
    monkeypatch.setattr(private_writer, "prepare_private_gap", prepare)
    gap_worker.run_cycle(ledger, budget_s=600)
    assert order == [("evidence", row.gap_id)]
    assert ledger.load(row.gap_id).private_attempts == 1


def test_private_need_with_an_evidence_attempt_is_still_reverified(env, monkeypatch):
    _, ledger, _ = env
    row = _private_gap(ledger)
    ledger.transition(row.gap_id, "needs_local_evidence", reason="partial", backoff_until=_past(), private_attempts=1)
    called = []
    monkeypatch.setattr(
        verification,
        "verify_existing_gap",
        lambda gap_id, owner, **kw: called.append(gap_id) or SimpleNamespace(outcome="partial", notes=[]),
    )
    gap_worker._reverify_jobless(ledger, [])
    assert called == [row.gap_id]


# -- Task 2: fair scheduling, several needs per cycle ---------------------- #


def _hot_proof_pending_gap(ledger, catalog):
    catalog.subjects["widget"] = SubjectEntry(
        slug="widget", kind="product", title="Widget", pages=[CatalogPage(path="wiki/widget.md", concept_id=None)]
    )
    hot, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="widget",
        catalog_subject=True,
        representative_question="What is the Widget API rate limit?",
    )
    ledger.transition(
        hot.gap_id,
        "retry_wait",
        reason="existing canonical answer proof pending; no duplicate import",
        backoff_until=_past(),
        last_claimed_at=_past(),
    )
    return hot


def test_one_hot_retry_gap_no_longer_starves_never_claimed_candidates(env, monkeypatch):
    _, ledger, catalog = env
    hot = _hot_proof_pending_gap(ledger, catalog)
    candidates = [
        ledger.submit_event(
            need_class="missing_coverage", subject=name, representative_question=f"docs for {name}: authentication"
        )[0].gap_id
        for name in ("alpha", "bravo", "charlie")
    ]
    seen = []
    monkeypatch.setattr(gap_worker, "discover", _unresolved_discovery(seen))
    monkeypatch.setattr(
        verification,
        "verify_existing_gap",
        lambda gap_id, owner, **kw: SimpleNamespace(outcome="verifying", notes=["pending"]),
    )
    order = []
    real = gap_worker._research_one
    monkeypatch.setattr(
        gap_worker,
        "_research_one",
        lambda led, record, *args: (order.append(record.gap_id), real(led, record, *args))[1],
    )
    gap_worker.run_cycle(ledger, budget_s=600)
    assert sorted(seen) == sorted(candidates)
    for gap_id in candidates:
        row = ledger.load(gap_id)
        assert row.last_claimed_at and row.research_attempts == 1
    # Never-claimed needs went first; the hot gap was served after them.
    assert set(order[:3]) == set(candidates) and order[3:] == [hot.gap_id]


def test_proof_pending_wait_grows_and_is_not_bypassed(env, monkeypatch):
    _, ledger, catalog = env
    hot = _hot_proof_pending_gap(ledger, catalog)
    calls = []
    monkeypatch.setattr(
        verification,
        "verify_existing_gap",
        lambda gap_id, owner, **kw: calls.append(gap_id) or SimpleNamespace(outcome="verifying", notes=[]),
    )
    gap_worker.run_cycle(ledger, budget_s=600)
    first = ledger.load(hot.gap_id)
    assert first.status == "retry_wait" and first.proof_wait_attempts == 1
    assert 240 < _wait_s(first) <= 300
    gap_worker.run_cycle(ledger, budget_s=600)  # the wait is honoured: no hot loop
    assert calls == [hot.gap_id]
    ledger.update(hot.gap_id, lambda r: setattr(r, "backoff_until", _past()))
    gap_worker.run_cycle(ledger, budget_s=600)
    second = ledger.load(hot.gap_id)
    assert second.proof_wait_attempts == 2 and 540 < _wait_s(second) <= 600


def test_proof_pending_backoff_doubles_to_a_six_hour_cap():
    waits = [gap_worker._proof_pending_backoff_s(n) for n in range(1, 12)]
    assert waits[:4] == [300, 600, 1200, 2400]
    assert max(waits) == 6 * 3600 and waits[-1] == 6 * 3600


def test_research_stops_when_the_cycle_budget_is_spent(env, monkeypatch):
    _, ledger, _ = env
    for name in ("alpha", "bravo", "charlie"):
        ledger.submit_event(
            need_class="missing_coverage", subject=name, representative_question=f"docs for {name}: authentication"
        )
    seen = []
    monkeypatch.setattr(gap_worker, "discover", _unresolved_discovery(seen))
    summary = gap_worker.run_cycle(ledger, budget_s=0)
    assert seen == []
    assert any("research budget" in note for note in summary["notes"])


def test_cycle_budget_reads_config_with_a_default():
    import math

    assert gap_worker.cycle_budget_s(CuratorConfig()) == math.inf
    cfg = CuratorConfig()
    cfg.gap_worker = SimpleNamespace(cycle_budget_s=900)
    assert gap_worker.cycle_budget_s(cfg) == 900.0


def test_infrastructure_question_saying_documented_is_researched(env, monkeypatch):
    _, ledger, _ = env
    row, _ = ledger.submit_event(
        need_class="infrastructure",
        subject="example-tui",
        representative_question="What documented capabilities does the local example-tui platform expose through its HTTP API and PWA?",
    )
    outage, _ = ledger.submit_event(
        need_class="infrastructure", subject="vo-rag", representative_question="is vo-rag up?"
    )
    claimed = []
    monkeypatch.setattr(gap_worker, "_research_one", lambda led, record, *args: claimed.append(record.gap_id))
    gap_worker.run_cycle(ledger, budget_s=600)
    assert claimed == [row.gap_id]


def test_infrastructure_rule_prefers_question_scope(monkeypatch):
    from doclayer import question_scope

    monkeypatch.setattr(question_scope, "infrastructure_documentation_need", lambda q: q == "yes", raising=False)
    assert gap_worker._infrastructure_documentation_need("yes")
    assert not gap_worker._infrastructure_documentation_need("installed documentation")


# -- Task 3: no permanent failure for having tried ------------------------- #


@pytest.fixture
def cfg(monkeypatch):
    cfg = CuratorConfig()
    cfg.private_writer.hosts = ["ws1"]
    monkeypatch.setattr(private_writer, "load_config", lambda: cfg)
    monkeypatch.setattr(private_writer, "_model_client", lambda cfg: object())
    return cfg


class PlanClient:
    """A planner that records every prompt and returns the same plan."""

    def __init__(self, plan):
        self.plan, self.prompts = plan, []

    async def chat_json(self, system, user, **kwargs):
        self.prompts.append((system, user))
        return {"commands": self.plan}


def test_empty_evidence_twice_backs_off_and_the_planner_sees_the_previous_plan(env, real_private_writer, cfg):
    _, ledger, catalog = env
    row = _private_gap(ledger)
    # `cat` stays outside the read-only allowlist, so this plan yields no evidence.
    plan = [{"host": "SVC1", "argv": ["cat", "/opt/acli/README.md"], "why": "read acli notes"}]
    client = PlanClient(plan)
    private_writer.prepare_private_gap(
        ledger,
        ledger.load(row.gap_id),
        catalog,
        [],
        model_client=client,
        runner=lambda *a, **k: pytest.fail("refused command ran"),
        now=NOW,
    )
    first = ledger.load(row.gap_id)
    assert first.status == "needs_local_evidence" and first.private_attempts == 1
    assert datetime.fromisoformat(first.backoff_until) == NOW + timedelta(hours=1)
    assert first.private_plans[-1]["plan"][0]["argv"][0] == "cat" and first.private_plans[-1]["refused"]

    private_writer.prepare_private_gap(
        ledger,
        first,
        catalog,
        [],
        model_client=client,
        runner=lambda *a, **k: pytest.fail("identical plan ran"),
        now=NOW,
    )
    second = ledger.load(row.gap_id)
    assert second.status == "needs_local_evidence" and second.private_attempts == 2
    assert datetime.fromisoformat(second.backoff_until) == NOW + timedelta(hours=6)
    assert "identical to an earlier attempt" in second.private_plans[-1]["reason"]
    system, prompt = client.prompts[-1]
    assert '"previous_plans"' in prompt and '"cat"' in prompt and "refused" in prompt
    assert "never return a plan identical" in system


def test_private_backoff_is_one_six_twentyfour_hours_then_daily():
    waits = [private_writer.private_retry_backoff_s(n) for n in range(1, 8)]
    assert waits == [3600, 6 * 3600, 24 * 3600, 24 * 3600, 24 * 3600, 24 * 3600, 24 * 3600]


def test_many_private_attempts_never_fail_the_gap(env, real_private_writer, cfg, monkeypatch):
    _, ledger, catalog = env
    row = _private_gap(ledger)
    ledger.update(row.gap_id, lambda r: setattr(r, "private_attempts", 7))
    monkeypatch.setattr(
        private_writer,
        "gather_evidence",
        lambda *a, **k: Gathered("empty", [], {"plan": [], "refused": [], "withheld": []}, "nothing"),
    )
    assert private_writer.prepare_private_gap(ledger, ledger.load(row.gap_id), catalog, [], now=NOW)
    current = ledger.load(row.gap_id)
    assert current.status == "needs_local_evidence" and current.private_attempts == 8
    assert datetime.fromisoformat(current.backoff_until) == NOW + timedelta(hours=24)


def test_plan_size_follows_configured_max_commands(cfg):
    cfg.private_writer.max_commands = 30
    system = private_writer.plan_system(cfg.private_writer)
    assert "at most 30 entries" in system and "{max_commands}" not in system


# -- Task 4: target pages and call ceilings -------------------------------- #


def _ready_private_gap(ledger, catalog, pages):
    catalog.subjects["acli"] = SubjectEntry(
        slug="acli",
        kind="product",
        title="acli",
        pages=[CatalogPage(path=f"infra/acli-{n}.md", concept_id=None) for n in range(pages)],
    )
    row, _ = ledger.submit_event(
        need_class="needs_local_evidence",
        subject="acli",
        catalog_subject=True,
        representative_question="What is the installed acli tool on SVC1?",
    )
    text = "host: SVC1\ncommand: command -v acli\nexit: 0\nstdout:\n/usr/local/bin/acli\nstderr:\n(no output)\n"
    source = {
        "kind": "text",
        "label": "live check SVC1 command #1",
        "text": text,
        "authority": "local-observation",
        "origin": PRIVATE_ORIGIN,
        "source_hash": hashlib.sha256(text.encode()).hexdigest(),
    }
    ledger.update(row.gap_id, lambda r: r.source_candidates.append(source))
    return ledger.load(row.gap_id)


def test_private_payload_targets_up_to_max_pages(env, monkeypatch):
    _, ledger, catalog = env
    cfg = CuratorConfig()
    cfg.single_pass.max_pages = 5
    monkeypatch.setattr(dispatcher_module, "load_config", lambda: cfg)
    record = _ready_private_gap(ledger, catalog, pages=7)
    payload = GapDispatcher(ledger, transport=FakeCuratorTransport([]), catalog=catalog).build_payload(record, [], 1)
    assert payload["targets"] == [f"infra/acli-{n}.md" for n in range(5)]


def test_no_literal_fifty_call_ceiling_remains_in_the_dispatcher():
    source = inspect.getsource(dispatcher_module)
    assert not re.search(r"(?<![\w.])50(?![\w.])", source)
    assert "[:2]" not in source


def test_model_call_grant_follows_configuration(tmp_path):
    ledger = GapLedger(tmp_path / "gaps")
    dispatcher = GapDispatcher(ledger, transport=FakeCuratorTransport([]), max_calls_job=200)
    assert dispatcher.day_ledger.max_calls_job == 200
    active = {"calls": 200, "job_id": "job-a", "attempt_id": "att-a"}
    intent = {"job_id": "job-a", "attempt_id": "att-a", "original_grant": 200, "expected_checkpoint": "c" * 64}
    receipt = {
        "disposition": "resource_exhausted",
        "job_id": "job-a",
        "attempt_id": "att-a",
        "original_grant": 200,
        "checkpoint": "c" * 64,
        "model_calls": 200,
    }
    GapDispatcher._check_retirement_receipt(active, intent, receipt)  # a 200-call grant is valid


# -- Task 5: chat remarks --------------------------------------------------- #


REMARKS = [
    "i need you to have another sonnet subagent identify all of the models that we have documentation on",
    "god forbid you just know how to use your own harness...",
    "make sure the plan is grounded in the codebase and the official documentation for all areas",
    "please apply/implement any and all improvements that need to be made to either or both skills",
    "ok so how can we use the reports, data, and videos to help with this project",
]
QUESTIONS = [
    "What is the installed acli tool on SVC1, its authentication method?",
    "Langfuse ClickHouse storage and seven-day log purge on App1",
    "How do I submit a native brain curator import manifest?",
    "Is Caddy installed on prod?",
    "For Nimbus on App1, what is the existing authenticated NWS+ acquisition route?",
    "Which services run on SVC1?",
]


@pytest.mark.parametrize("text", REMARKS)
def test_chat_remarks_are_recognised(text):
    assert is_chat_remark(text)


@pytest.mark.parametrize("text", QUESTIONS)
def test_documentation_questions_are_not_chat_remarks(text):
    assert not is_chat_remark(text)


def test_chat_remark_need_ends_suppressed_not_retried(env, monkeypatch):
    _, ledger, _ = env
    retrying, _ = ledger.submit_event(need_class="missing_coverage", subject=None, representative_question=REMARKS[0])
    ledger.transition(
        retrying.gap_id, "retry_wait", reason="no official source", backoff_until=_past(), research_attempts=6
    )
    failed, _ = ledger.submit_event(need_class="missing_coverage", subject=None, representative_question=REMARKS[2])
    ledger.transition(failed.gap_id, "failed", reason="partial job, nothing published")
    seen = []
    monkeypatch.setattr(gap_worker, "discover", _unresolved_discovery(seen))
    gap_worker.run_cycle(ledger, budget_s=600)
    assert seen == []
    for gap_id in (retrying.gap_id, failed.gap_id):
        row = ledger.load(gap_id)
        assert row.status == "suppressed" and row.suppressed_reason == NOT_A_DOCUMENTATION_NEED


def test_research_backoff_never_exceeds_daily(tmp_path):
    ledger = GapLedger(tmp_path / "gaps")
    waits = [ledger.backoff_state(n) for n in range(0, 50)]
    assert max(waits) == 86400 and waits[-1] == 86400


def test_cycle_budget_is_a_real_config_field(tmp_path):
    from doclayer.curator.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text("gap_worker:\n  cycle_budget_s: 600\n")
    assert gap_worker.cycle_budget_s(load_config(path)) == 600.0


def test_cycle_budget_is_unlimited_by_default():
    import math

    from doclayer.curator.config import CuratorConfig

    assert gap_worker.cycle_budget_s(CuratorConfig()) == math.inf
    assert gap_worker.DEFAULT_CYCLE_BUDGET_S == math.inf


def test_unit_hang_guard_is_eight_hours():
    from pathlib import Path

    deploy = Path(gap_worker.__file__).resolve().parents[2] / "deploy"
    worker_dir = next(d for d in sorted(deploy.iterdir()) if (d / "deploy.sh").is_file())
    unit = worker_dir / "doclayer-gap-worker.service"
    assert "TimeoutStartSec=8h" in unit.read_text()
