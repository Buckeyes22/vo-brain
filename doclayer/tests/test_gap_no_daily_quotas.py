"""Default owner admits bounded jobs without aggregate daily quotas."""

import copy

import httpx
import pytest
from test_gap_contract_recovery import ready, setup

from doclayer.curator.model import ModelClient
from doclayer.curator.state import Usage
from doclayer.gap_budget import BudgetUnavailable, ResourceBudget, today
from doclayer.gap_ops import handle
from doclayer.settings import Settings


def ignore_retired_environment(monkeypatch, calls):
    monkeypatch.setenv("DOC_LAYER_GAP_MODEL_CALLS_DAY", str(calls))
    monkeypatch.setenv("DOC_LAYER_GAP_JOBS_PER_DAY", "3")
    settings = Settings()
    assert settings.gap_model_calls_day is None
    assert settings.gap_jobs_per_day is None
    # Exercise the actual owning constructor against these configured settings.
    monkeypatch.setattr("doclayer.settings.settings", settings)


def configure_http(cfg, monkeypatch, requests):
    cfg.inference.url = "http://inference.test/v1"
    cfg.inference.api_key_env = "NO_DAILY_QUOTA_FIXTURE_KEY"
    monkeypatch.setenv("NO_DAILY_QUOTA_FIXTURE_KEY", "fixture-only-key")

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": '{"supported":true}'},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    return httpx.MockTransport(respond)


@pytest.mark.asyncio
@pytest.mark.parametrize("retired_daily_limit", [100, 101])
async def test_default_four_real_jobs_and_140_actual_http_attempts_keep_per_job_limits(
    tmp_path, monkeypatch, retired_daily_limit
):
    ignore_retired_environment(monkeypatch, retired_daily_limit)
    ledger, ops, dispatcher = setup(tmp_path)
    assert dispatcher.day_ledger.max_calls_day is None
    assert dispatcher.day_ledger.max_jobs is None
    records = [ready(ledger, f"widget-{i}") for i in range(4)]
    requests = []
    transport = configure_http(ops.cfg, monkeypatch, requests)
    for index, record in enumerate(records):
        result = dispatcher.dispatch(record.gap_id)
        assert result.action == "submitted"
        state = ops.store.load(result.job_id)
        assert state.limits["max_model_calls"] == 50
        if index + 1 < len(records):
            assert dispatcher.dispatch(records[index + 1].gap_id).action == "skipped"
            assert len(ops.store.list_ids()) == index + 1  # singleton unchanged
        client = ModelClient(
            ops.cfg,
            state.usage,
            limits=state.limits,
            transport=transport,
            before_call=lambda: dispatcher.day_ledger.consume_model(kind="verification", attempt_id=state.job_id),
        )
        for _ in range(35):
            assert await client.chat_json("Check fixture evidence", "Complete fixture data") == {"supported": True}
        assert state.usage.model_calls == 35
        state.status = "cancelled"
        ops.store.save(state)
        dispatcher.dispatch(record.gap_id)  # Actual owning status releases slot.
        assert dispatcher.active() is None
    saved = dispatcher.day_ledger.load(today())
    assert len(requests) == saved["model_calls"] == 140
    assert saved["jobs"] == len(ops.store.list_ids()) == 4
    assert saved["reserved_calls"] == 200
    assert all(entry["reserved_calls"] == 50 and not entry["released"] for entry in saved["entries"])
    summary = handle("summary", {}, ledger)["summary"]
    assert summary["daily_quotas_enabled"] is False
    assert summary["daily_usage"]["model_calls"] == 140


@pytest.mark.asyncio
async def test_default_old_50_and_48_grants_and_101_charges_do_not_block_verification_or_new_job(tmp_path, monkeypatch):
    ignore_retired_environment(monkeypatch, 100)
    ledger, ops, dispatcher = setup(tmp_path)
    legacy = ResourceBudget(ledger.root, max_jobs=3, max_calls_day=100, max_calls_job=50)
    prior = legacy.reserve_attempt("old-first", "gap-first", 50)
    reduced = legacy.reserve_attempt("old-second", "gap-second", 50, headroom=2)
    assert prior["calls"] == 50 and reduced["calls"] == 48
    historical_states = {}
    for key, cap, used in (("old-first", 50, 28), ("old-second", 48, 26)):
        reply = ops.call(
            "submit",
            {
                "intent": "Historical fixture source import",
                "urls": ["https://example.org/legacy"],
                "limits": {"max_model_calls": cap},
                "idempotency_key": key,
            },
        )
        state = ops.store.load(reply["job_id"])
        state.status = "failed"
        state.usage.model_calls = used
        ops.store.save(state)
        historical_states[state.job_id] = ops.store.state_path(state.job_id).read_bytes()
    before_entries = copy.deepcopy(legacy.load(today())["entries"])
    current = ResourceBudget(ledger.root)
    for _ in range(101):
        current.consume_model(kind="discovery")
    usage = Usage()
    requests = []
    transport = configure_http(ops.cfg, monkeypatch, requests)
    client = ModelClient(
        ops.cfg,
        usage,
        transport=transport,
        before_call=lambda: current.consume_model(kind="verification", attempt_id="published-gap"),
    )
    assert await client.chat_json("Check delivered evidence", "Full fixture source") == {"supported": True}
    new = dispatcher.dispatch(ready(ledger).gap_id)
    assert new.action == "submitted"
    assert ops.store.load(new.job_id).limits["max_model_calls"] == 50
    saved = current.load(today())
    assert saved["model_calls"] == 102 and len(requests) == usage.model_calls == 1
    assert saved["reserved_calls"] == 148 and saved["jobs"] == 3
    assert saved["entries"][:2] == before_entries
    assert current.reserve_attempt("old-first", "gap-first", 50) == prior
    assert current.reserve_attempt("old-second", "gap-second", 50) == reduced
    assert len(ops.store.list_ids()) == 3
    assert all(ops.store.state_path(job_id).read_bytes() == original for job_id, original in historical_states.items())


@pytest.mark.asyncio
async def test_default_aggregate_unlimited_does_not_increase_single_attempt_50_call_bound(tmp_path, monkeypatch):
    ledger, ops, _ = setup(tmp_path)
    budget = ResourceBudget(ledger.root)
    requests = []
    transport = configure_http(ops.cfg, monkeypatch, requests)
    usage = Usage()
    client = ModelClient(
        ops.cfg,
        usage,
        limits={"max_model_calls": 50},
        transport=transport,
        before_call=lambda: budget.consume_model(kind="verification", attempt_id="one-job"),
    )
    for _ in range(50):
        await client.chat_json("Check fixture source", "Full fixture")
    with pytest.raises(BudgetUnavailable, match="attempt inference"):
        await client.chat_json("Check fixture source", "No extra grant")
    assert len(requests) == usage.model_calls == 50
    assert budget.load(today())["model_calls"] == 50


def test_explicit_legacy_jobs_quota_remains_optional_api_compatibility(tmp_path):
    budget = ResourceBudget(tmp_path, max_jobs=3, max_calls_day=1000)
    for i in range(3):
        assert budget.reserve_attempt(f"a{i}", f"gap{i}", 50)["calls"] == 50
    assert budget.reserve_attempt("fourth", "gap4", 50) is None
    assert budget.load(today())["jobs"] == 3
