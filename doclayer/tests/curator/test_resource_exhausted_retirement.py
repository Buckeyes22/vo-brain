"""Actual owning retirement and scheduler recovery, never native inference."""

import asyncio
import json

import httpx
import pytest
from test_curator_mcp_server import mcp_env  # noqa: F401 -- fixture registration

from doclayer.curator.ops import OpsError, handle
from doclayer.curator.pipeline import Pipeline
from doclayer.dispatcher import GapDispatcher
from doclayer.gap_budget import today
from doclayer.gaps import GapLedger


class ActualOps:
    def __init__(self, cfg, store):
        self.cfg, self.store, self.calls = cfg, store, []
        self.lose = False

    def call(self, op, payload, **kwargs):
        self.calls.append((op, dict(payload)))
        reply = handle(op, payload, self.cfg, self.store)
        if self.lose and op == "retire_allowance":
            self.lose = False
            raise TimeoutError("lost retirement reply")
        return reply


def setup(tmp_path, cfg, store):
    ledger = GapLedger(tmp_path / "gaps")
    ledger.root.mkdir()
    (ledger.root / "curator-ready").touch()
    ops = ActualOps(cfg, store)
    d = GapDispatcher(ledger, transport=ops)
    gap, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="device",
        facets=["authentication"],
        representative_question="What does official Device documentation say about authentication?",
        source_candidates=[{"url": "https://example.org/device", "authority": "official"}],
    )
    ledger.transition(gap.gap_id, "ready_for_curation")
    outcome = d.dispatch(gap.gap_id)
    state = store.load(outcome.job_id)
    state.status = "retryable"
    state.stage = "draft"
    state.error = "limit reached: model_calls"
    state.usage.model_calls = 50
    store.save(state)
    return ledger, ops, d, gap.gap_id, state.job_id


def job(ops, job_id):
    return ops.call("status", {"job_id": job_id})["job"]


def request(d, observed):
    active = d.active()
    return dict(
        job_id=active["job_id"],
        attempt_id=active["attempt_id"],
        original_grant=active["calls"],
        expected_checkpoint=observed["resume_checkpoint"],
    )


def test_real_charged_pipeline_hard_cap_retires_allowance_not_work(tmp_path, cfg, store, monkeypatch):
    ledger, ops, d, gap_id, job_id = setup(tmp_path, cfg, store)
    state = store.load(job_id)
    state.status = "queued"
    state.error = None
    state.usage.model_calls = 0
    store.save(state)
    charged = []

    def http(req):
        charged.append(req)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "literal"}, "finish_reason": "stop"}]},
        )

    pipeline = Pipeline(cfg, store, inference_transport=httpx.MockTransport(http))

    async def stage(state):
        for _ in range(51):
            await pipeline._model(state).chat("Fixture", "Whole data")

    monkeypatch.setattr(pipeline, "_run_stage", stage)
    stopped = asyncio.run(pipeline.run(state))
    assert len(charged) == 50 and stopped.usage.model_calls == 50
    assert stopped.error == "limit reached: model_calls"
    before = store.state_path(job_id).read_bytes()
    reservation = ledger.load(gap_id).submission["reservation"]
    assert d.retire_exhausted(gap_id, job(ops, job_id)).action == "resource_exhausted"
    assert store.state_path(job_id).read_bytes() == before
    row = ledger.load(gap_id)
    assert row.status == "failed" and row.status != "resolved" and "resource_exhausted" in row.failure_reason
    assert row.submission["reservation"] == reservation and row.submission["state"] == "terminal"
    assert d.active() is None and d.day_ledger.load(today())["reserved_calls"] == 50
    assert d.dispatch(gap_id).action == "skipped" and len(store.list_ids()) == 1
    other, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="other",
        facets=["configuration"],
        representative_question="How does Other configure?",
        source_candidates=[{"url": "https://example.org/other", "authority": "official"}],
    )
    ledger.transition(other.gap_id, "ready_for_curation")
    next_job = d.dispatch(other.gap_id).job_id
    assert next_job is not None and next_job != job_id
    assert store.load(next_job).limits["max_model_calls"] == 50
    assert reservation["calls"] == 50
    assert ledger.load(gap_id).submission["reservation"] == reservation
    assert store.state_path(job_id).read_bytes() == before
    assert d.day_ledger.load(today())["reserved_calls"] == 100


def test_lost_reply_exact_replay_and_explicit_manual_readmission(tmp_path, cfg, store):
    ledger, ops, d, gap_id, job_id = setup(tmp_path, cfg, store)
    observed = job(ops, job_id)
    req = request(d, observed)
    before = store.state_path(job_id).read_bytes()
    ops.lose = True
    assert d.retire_exhausted(gap_id, observed).action == "uncertain"
    assert d.active()["job_id"] == job_id and store.state_path(job_id).read_bytes() == before
    receipt = (store.job_dir(job_id) / "allowance-retirement.json").read_bytes()
    with pytest.raises(OpsError, match="operator readmission"):
        handle("resume", {"job_id": job_id, "extend": {"max_model_calls": 50}}, cfg, store)
    with pytest.raises(OpsError, match="operator readmission"):
        handle(
            "resume",
            {"job_id": job_id, "expected_checkpoint": observed["resume_checkpoint"]},
            cfg,
            store,
        )
    assert d.retire_exhausted(gap_id, observed).action == "resource_exhausted"
    assert (store.job_dir(job_id) / "allowance-retirement.json").read_bytes() == receipt
    reply = handle(
        "resume",
        {
            "job_id": job_id,
            "operator_readmission": True,
            "extend": {"max_model_calls": 50},
        },
        cfg,
        store,
    )
    assert reply["status"] == "queued" and store.load(job_id).limits["max_model_calls"] == 100
    assert store.load(job_id).usage.model_calls == 50
    with pytest.raises(OpsError, match="retirement withheld"):
        handle("retire_allowance", req, cfg, store)


def test_crash_after_terminal_checkpoint_recovers_singleton_without_refund(tmp_path, cfg, store, monkeypatch):
    ledger, ops, d, gap_id, job_id = setup(tmp_path, cfg, store)
    from pathlib import Path

    unlink = Path.unlink

    def crash(path, *args, **kwargs):
        if path == d.slot_path(gap_id):
            raise RuntimeError("power loss before singleton removal")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", crash)
        with pytest.raises(RuntimeError, match="power loss"):
            d.retire_exhausted(gap_id, job(ops, job_id))
    assert ledger.load(gap_id).submission["state"] == "terminal" and d.slot_path(gap_id).exists()
    restarted = GapDispatcher(GapLedger(ledger.root), transport=ops)
    assert restarted.active() is None
    assert restarted.day_ledger.load(today())["reserved_calls"] == 50
    assert len([op for op, _ in ops.calls if op == "retire_allowance"]) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "cancel",
        "commit",
        "pushed",
        "pr",
        "merged",
        "publish",
        "manual",
        "cap300",
        "malformed",
        "foreign",
        "stale",
    ],
)
def test_owning_refusal_never_retires_unsafe_stop(tmp_path, cfg, store, mutation):
    ledger, ops, d, gap_id, job_id = setup(tmp_path, cfg, store)
    req = request(d, job(ops, job_id))
    s = store.load(job_id)
    if mutation == "cancel":
        s.cancel_requested = True
    elif mutation == "commit":
        s.publication.commit_sha = "a" * 40
    elif mutation == "pushed":
        s.publication.pushed = True
    elif mutation == "pr":
        s.publication.pr_number = 2
    elif mutation == "merged":
        s.publication.merged = True
    elif mutation == "publish":
        s.stage = "publish"
    elif mutation == "manual":
        s.submitted_by = "operator"
    elif mutation == "cap300":
        s.limits["max_model_calls"] = 300
        s.usage.model_calls = 300
    elif mutation == "malformed":
        req["original_grant"] = True
    elif mutation == "foreign":
        req["attempt_id"] = "foreign"
    elif mutation == "stale":
        s.intent += " changed"
    store.save(s)
    before = store.state_path(job_id).read_bytes()
    with pytest.raises(OpsError):
        handle("retire_allowance", req, cfg, store)
    assert store.state_path(job_id).read_bytes() == before
    assert not (store.job_dir(job_id) / "allowance-retirement.json").exists()
    assert d.active()["job_id"] == job_id


def test_claim_and_dayrollover_preserve_original_reservation(tmp_path, cfg, store, monkeypatch):
    ledger, ops, d, gap_id, job_id = setup(tmp_path, cfg, store)
    req = request(d, job(ops, job_id))
    claim = store.job_claim(job_id)
    assert claim.acquire(blocking=False)
    try:
        with pytest.raises(OpsError, match="claimed"):
            handle("retire_allowance", req, cfg, store)
    finally:
        claim.release()
    monkeypatch.setattr("doclayer.gap_budget.today", lambda: "2099-01-02")
    assert d.retire_exhausted(gap_id, job(ops, job_id)).action == "resource_exhausted"
    budget = d.day_ledger.load("2099-01-02")
    assert budget["reserved_calls"] == 50 and budget["entries"][0]["carryover"] is True


def test_actual_worker_retires_only_bound_cap_and_keeps_unknown_obligations(tmp_path, cfg, store):
    from doclayer.gap_worker import _verify_published

    ledger, ops, d, gap_id, job_id = setup(tmp_path, cfg, store)
    evidence = store.job_dir(job_id) / "evidence.jsonl"
    evidence.write_text('{"id":"required-unknown-original","support_status":"unknown"}\n')
    original = evidence.read_bytes()
    notes = []
    _verify_published(ledger, d, notes)
    row = ledger.load(gap_id)
    assert row.status == "failed" and "resource_exhausted" in row.failure_reason
    assert any("resource_exhausted" in note for note in notes)
    assert evidence.read_bytes() == original and store.load(job_id).status == "retryable"
    assert d.active() is None
    _verify_published(ledger, d, notes)
    assert len([op for op, _ in ops.calls if op == "retire_allowance"]) == 1


def test_operator_readmission_cannot_advance_between_guard_and_retirement(tmp_path, cfg, store, monkeypatch):
    import threading

    ledger, ops, d, gap_id, job_id = setup(tmp_path, cfg, store)
    req = request(d, job(ops, job_id))
    entered = threading.Event()
    release = threading.Event()
    results = []
    import doclayer.curator.ops as owning

    write = owning.atomic_write_json

    def blocked(path, data):
        if path.name == "allowance-retirement.json":
            entered.set()
            assert release.wait(3)
        return write(path, data)

    monkeypatch.setattr(owning, "atomic_write_json", blocked)

    def retire():
        results.append(handle("retire_allowance", req, cfg, store))

    thread = threading.Thread(target=retire)
    thread.start()
    assert entered.wait(3)
    try:
        with pytest.raises(OpsError, match="claimed"):
            handle(
                "resume",
                {"job_id": job_id, "extend": {"max_model_calls": 50}},
                cfg,
                store,
            )
    finally:
        release.set()
        thread.join(3)
    assert len(results) == 1 and results[0]["ok"] is True
    assert store.load(job_id).limits["max_model_calls"] == 50
    with pytest.raises(OpsError, match="operator readmission"):
        handle("resume", {"job_id": job_id, "extend": {"max_model_calls": 50}}, cfg, store)


def test_foreign_remote_receipt_does_not_release_slot(tmp_path, cfg, store):
    ledger, ops, d, gap_id, job_id = setup(tmp_path, cfg, store)
    actual = ops.call

    def foreign(op, payload, **kw):
        reply = actual(op, payload, **kw)
        if op == "retire_allowance":
            reply["receipt"]["attempt_id"] = "another-attempt"
        return reply

    ops.call = foreign
    assert d.retire_exhausted(gap_id, job(ops, job_id)).action == "uncertain"
    assert d.active()["job_id"] == job_id and ledger.load(gap_id).status == "curating"


def test_standard_cli_operator_readmission_reaches_actual_owner(tmp_path, cfg, store, monkeypatch):
    from doclayer.curator import cli

    ledger, ops, d, gap_id, job_id = setup(tmp_path, cfg, store)
    assert d.retire_exhausted(gap_id, job(ops, job_id)).action == "resource_exhausted"
    monkeypatch.setattr(cli, "load_config", lambda _: cfg)
    monkeypatch.setattr(cli, "LocalTransport", lambda _: ops)
    assert (
        cli.main(
            [
                "--local",
                "resume",
                "--job",
                job_id,
                "--operator-readmission",
                "--extend",
                "max_model_calls=50",
            ]
        )
        == 0
    )
    assert ops.calls[-1] == (
        "resume",
        {
            "job_id": job_id,
            "operator_readmission": True,
            "extend": {"max_model_calls": 50},
        },
    )
    assert store.load(job_id).status == "queued" and store.load(job_id).usage.model_calls == 50


@pytest.mark.asyncio
async def test_fresh_mcp_sdk_operator_readmission_actual_ops(mcp_env):  # noqa: F811 -- fixture registration
    import sys
    from pathlib import Path

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from doclayer.curator.config import load_config
    from doclayer.curator.jobstore import JobStore

    cfg = load_config(mcp_env["BRAIN_CURATOR_CONFIG"])
    store = JobStore(cfg.jobs_dir())
    submitted = handle(
        "submit",
        {
            "intent": "Device import",
            "urls": ["https://example.org/device"],
            "submitted_by": "doclayer-gap-dispatcher",
            "idempotency_key": "actual-mcp-retirement",
            "limits": {"max_model_calls": 50},
        },
        cfg,
        store,
    )
    state = store.load(submitted["job_id"])
    state.status = "retryable"
    state.stage = "draft"
    state.error = "limit reached: model_calls"
    state.usage.model_calls = 50
    store.save(state)
    observed = handle("status", {"job_id": state.job_id}, cfg, store)["job"]
    handle(
        "retire_allowance",
        dict(
            job_id=state.job_id,
            attempt_id=state.idempotency_key,
            original_grant=50,
            expected_checkpoint=observed["resume_checkpoint"],
        ),
        cfg,
        store,
    )
    env = dict(mcp_env)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "doclayer.curator.mcp_server"], env=env)
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            schema = next(t for t in tools.tools if t.name == "job_resume").inputSchema
            assert schema["properties"]["operator_readmission"]["default"] is False
            refused = await session.call_tool("job_resume", {"job_id": state.job_id})
            assert json.loads(refused.content[0].text)["ok"] is False
            result = await session.call_tool(
                "job_resume",
                {
                    "job_id": state.job_id,
                    "operator_readmission": True,
                    "extend": {"max_model_calls": 50},
                },
            )
            assert json.loads(result.content[0].text)["status"] == "queued"
    assert store.load(state.job_id).limits["max_model_calls"] == 100
    assert store.load(state.job_id).usage.model_calls == 50


def test_retired_need_remains_outstanding_in_summary(tmp_path, cfg, store):
    ledger, ops, d, gap_id, job_id = setup(tmp_path, cfg, store)
    assert d.retire_exhausted(gap_id, job(ops, job_id)).action == "resource_exhausted"
    summary = ledger.summary()
    assert summary["outstanding"] == 1 and summary["resource_exhausted"] == 1
    assert summary["by_status"] == {"failed": 1}
    assert summary["quarantined"] == 0
    from doclayer.gap_worker import digest

    assert "1 unresolved need(s) parked" in digest(summary)
    assert ledger.load(gap_id).resolution_verification == {}
    ledger.transition(gap_id, "suppressed")
    assert ledger.summary()["outstanding"] == 0
    assert ledger.summary()["resource_exhausted"] == 0


@pytest.mark.parametrize(
    "status,cancel",
    [
        ("cancelled", False),
        ("retryable", True),
        ("covered", False),
        ("published", False),
        ("failed", False),
    ],
)
def test_rejected_or_finished_readmission_never_writes_ack_or_extends(tmp_path, cfg, store, status, cancel):
    ledger, ops, d, gap_id, job_id = setup(tmp_path, cfg, store)
    assert d.retire_exhausted(gap_id, job(ops, job_id)).action == "resource_exhausted"
    receipt = (store.job_dir(job_id) / "allowance-retirement.json").read_bytes()
    state = store.load(job_id)
    state.status = status
    state.cancel_requested = cancel
    store.save(state)
    original = store.state_path(job_id).read_bytes()
    payload = {
        "job_id": job_id,
        "operator_readmission": True,
        "extend": {"max_model_calls": 50},
    }
    if cancel or status == "cancelled":
        with pytest.raises(OpsError, match="cancelled"):
            handle("resume", payload, cfg, store)
    else:
        reply = handle("resume", payload, cfg, store)
        assert reply["status"] == status and "no readmission" in reply["note"]
    assert store.state_path(job_id).read_bytes() == original
    assert (store.job_dir(job_id) / "allowance-retirement.json").read_bytes() == receipt
    assert not (store.job_dir(job_id) / "allowance-readmission.json").exists()
    assert store.load(job_id).limits["max_model_calls"] == 50
    assert store.load(job_id).usage.model_calls == 50


def test_ambiguous_private_need_does_not_dispatch_an_official_url(tmp_path, cfg, store):
    ledger = GapLedger(tmp_path / "gaps")
    ledger.root.mkdir()
    (ledger.root / "curator-ready").touch()
    ops = ActualOps(cfg, store)
    dispatcher = GapDispatcher(ledger, transport=ops)
    record, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="unidentified",
        facets=["authentication"],
        representative_question="Which of our local accounts authenticates this setup?",
        source_candidates=[{"url": "https://example.org/device", "authority": "official"}],
    )
    ledger.transition(record.gap_id, "ready_for_curation")
    outcome = dispatcher.dispatch(record.gap_id)
    assert outcome.job_id is None
    assert ledger.load(record.gap_id).status == "needs_local_evidence"
    assert not any(operation == "submit" for operation, _ in ops.calls)
    assert ledger.load(record.gap_id).job_links == []
