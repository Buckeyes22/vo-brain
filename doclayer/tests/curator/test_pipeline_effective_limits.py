"""Recovery observes the same caps that the pipeline actually enforces."""

import pytest

from doclayer.curator.ops import _status_view
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import JobState


@pytest.mark.asyncio
async def test_partial_dispatch_limits_preserve_calls_and_enforce_active_cap(cfg, store):
    cfg.limits.max_active_seconds = 12
    state, _ = store.create(
        JobState(job_id="", idempotency_key="", intent="bounded dispatch", limits={"max_model_calls": 50})
    )
    state.usage.model_calls = 7
    state.usage.active_seconds = 12
    store.save(state)

    await Pipeline(cfg, store).run(state)

    persisted = store.load(state.job_id)
    view = _status_view(persisted, store)
    assert view["status"] == "retryable"
    assert view["error"] == "limit reached: active_time"
    assert view["limits"]["max_model_calls"] == 50
    assert view["limits"]["max_active_seconds"] == 12
    assert view["usage"]["model_calls"] == 7
    assert view["usage"]["active_seconds"] >= 12

    # Later configuration changes cannot grant a stopped job more time.
    cfg.limits.max_active_seconds = 120
    persisted.status = "queued"
    store.save(persisted)
    await Pipeline(cfg, store).run(persisted)
    assert store.load(state.job_id).limits["max_active_seconds"] == 12
    assert store.load(state.job_id).error == "limit reached: active_time"


@pytest.mark.asyncio
async def test_finished_job_is_not_modified_to_fill_missing_caps(cfg, store):
    state, _ = store.create(
        JobState(job_id="", idempotency_key="", intent="finished import", limits={"max_model_calls": 50})
    )
    state.status = "published"
    store.save(state)
    before = state.to_dict()
    await Pipeline(cfg, store).run(state)
    assert state.to_dict() == before
    assert store.load(state.job_id).to_dict() == before
