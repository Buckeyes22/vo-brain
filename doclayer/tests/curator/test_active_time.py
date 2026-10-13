"""Active-time limits account failures and checkpoints without charging offline time."""

import httpx
import pytest

from doclayer.curator import intake, ops
from doclayer.curator.model import ModelError, OutputTruncated
from doclayer.curator.pipeline import CancelledByRequest, Pipeline
from doclayer.curator.state import JobState


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def queued(store, limit=100):
    state = store.create(JobState(job_id="", idempotency_key="", intent="device procedure"))[0]
    state.stage = "extract"
    state.limits = {"max_active_seconds": limit, "max_model_calls": 20}
    store.save(state)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ModelError("outage"), OutputTruncated("partial"), CancelledByRequest()])
async def test_failed_cancelled_and_truncated_stages_charge_elapsed_checkpoint_time(cfg, store, error):
    clock = Clock()

    class Broken(Pipeline):
        async def _run_stage(self, state):
            clock.advance(7)
            self._persist(state)
            assert store.load(state.job_id).usage.active_seconds == 7
            clock.advance(2)
            raise error

    state = await Broken(cfg, store, clock=clock).run(queued(store))
    assert state.usage.active_seconds == store.load(state.job_id).usage.active_seconds == 9
    assert "active processing: 9s" in state.summary
    assert state.status == ("cancelled" if isinstance(error, CancelledByRequest) else "retryable")


@pytest.mark.asyncio
async def test_resume_excludes_offline_time_and_never_double_charges_checkpoints(cfg, store):
    clock = Clock()

    class Resumable(Pipeline):
        attempts = 0

        async def _run_stage(self, state):
            self.attempts += 1
            clock.advance(3)
            self._persist(state)
            self._persist(state)
            self._check_time(state)
            if self.attempts == 1:
                clock.advance(2)
                raise ModelError("outage")
            state.status = "covered"
            return None

    runner = Resumable(cfg, store, clock=clock)
    first = await runner.run(queued(store))
    assert first.usage.active_seconds == 5
    clock.advance(1000)
    ops.handle("resume", {"job_id": first.job_id}, cfg, store)
    second = await runner.run(store.load(first.job_id))
    assert second.status == "covered" and second.usage.active_seconds == 8


@pytest.mark.asyncio
async def test_long_stage_stops_at_durable_checkpoint_before_later_work(cfg, store):
    clock = Clock()
    continued = []

    class Long(Pipeline):
        async def _run_stage(self, state):
            clock.advance(4)
            self.store.append_evidence(state.job_id, [{"text": "first fact"}])
            self._persist(state)
            clock.advance(3)
            self.store.append_evidence(state.job_id, [{"text": "second fact"}])
            self._persist(state)
            continued.append("publication")
            return "publish"

    state = await Long(cfg, store, clock=clock).run(queued(store, limit=6))
    assert state.status == "retryable" and state.error == "limit reached: active_time"
    assert state.usage.active_seconds == 7 and not continued
    assert len(store.read_evidence(state.job_id)) == 2 and not state.publication.merged


@pytest.mark.asyncio
@pytest.mark.parametrize("call_limit,expected_calls,error", [(20, 2, "active_time"), (1, 1, "model_calls")])
async def test_each_actual_model_attempt_checks_active_and_call_caps(cfg, store, call_limit, expected_calls, error):
    clock = Clock()
    requests = []

    def respond(request):
        requests.append(request)
        clock.advance(3)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})

    class Calls(Pipeline):
        async def _run_stage(self, state):
            client = self._model(state)
            for i in range(5):
                await client.chat("contract", "source")
            state.status = "covered"
            return None

    state = queued(store, limit=5)
    state.limits["max_model_calls"] = call_limit
    result = await Calls(cfg, store, clock=clock, inference_transport=httpx.MockTransport(respond)).run(state)
    assert result.status == "retryable" and result.error == "limit reached: " + error
    assert len(requests) == result.usage.model_calls == expected_calls
    assert result.usage.active_seconds == 3 * expected_calls


@pytest.mark.asyncio
async def test_intake_checkpoint_stops_before_fetching_the_next_source(cfg, store):
    clock = Clock()
    requests = []

    def respond(request):
        requests.append(request)
        clock.advance(4)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<h1>Device</h1><p>" + ("Documented device procedure and configuration. " * 10) + "</p>",
        )

    state = queued(store, limit=3)
    state.stage = "intake"
    state.limits["fetch_concurrency"] = 1
    state.sources = [
        intake.make_url_source("https://device.example/one"),
        intake.make_url_source("https://device.example/two"),
    ]
    store.save(state)
    result = await Pipeline(cfg, store, clock=clock, fetch_transport=httpx.MockTransport(respond)).run(state)
    assert result.status == "retryable" and result.error == "limit reached: active_time"
    assert len(requests) == 1 and result.sources[0].status == "fetched" and result.sources[1].status == "pending"
    assert result.usage.active_seconds == 4


@pytest.mark.asyncio
async def test_final_checkpoint_preserves_concurrent_cancellation_and_custom_persist(cfg, store):
    clock = Clock()
    calls = []

    class Done(Pipeline):
        async def _run_stage(self, state):
            clock.advance(2)
            state.status = "covered"
            return None

    state = queued(store)
    runner = Done(cfg, store, clock=clock)

    def checkpoint():
        runner._persist(state)
        calls.append(state.status)
        if len(calls) == 2:
            concurrent = store.load(state.job_id)
            concurrent.cancel_requested = True
            store.save(concurrent)

    result = await runner.run(state, persist=checkpoint)
    assert len(calls) == 3
    assert result.cancel_requested and store.load(state.job_id).cancel_requested
    assert result.usage.active_seconds == 2


@pytest.mark.asyncio
async def test_admitted_attempt_is_durable_before_http_and_denied_attempts_are_not(cfg, store):
    observed = []

    def respond(request):
        observed.append(store.load(state.job_id).usage.model_calls)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})

    state = queued(store)
    state.limits["max_model_calls"] = 1

    class Calls(Pipeline):
        async def _run_stage(self, current):
            client = self._model(current)
            await client.chat("contract", "source")
            await client.chat("contract", "source")

    result = await Calls(cfg, store, inference_transport=httpx.MockTransport(respond)).run(state)
    assert observed == [1] and result.usage.model_calls == 1 and result.error == "limit reached: model_calls"


def test_worker_killed_during_admitted_http_cannot_reuse_attempt_budget(cfg, store):
    import asyncio
    import multiprocessing
    import os

    from doclayer.curator.worker import recover_orphans

    state = queued(store)
    state.limits["max_model_calls"] = 1
    store.save(state)

    def killed_worker():
        def in_flight(request):
            # No Python finally or normal error handling runs after this point.
            os._exit(73 if store.load(state.job_id).usage.model_calls == 1 else 74)

        class Interrupted(Pipeline):
            async def _run_stage(self, current):
                await self._model(current).chat("contract", "source")

        asyncio.run(
            Interrupted(cfg, store, inference_transport=httpx.MockTransport(in_flight)).run(store.load(state.job_id))
        )

    process = multiprocessing.get_context("fork").Process(target=killed_worker)
    process.start()
    process.join(timeout=10)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("interrupted worker did not exit")
    assert process.exitcode == 73
    recovered = recover_orphans(store)
    assert len(recovered) == 1 and recovered[0].usage.model_calls == 1
    requests = []

    def forbid(request):
        requests.append(request)
        pytest.fail("exhausted attempt dispatched again")

    class Restarted(Pipeline):
        async def _run_stage(self, current):
            await self._model(current).chat("contract", "source")

    resumed = asyncio.run(Restarted(cfg, store, inference_transport=httpx.MockTransport(forbid)).run(recovered[0]))
    assert resumed.status == "retryable" and resumed.error == "limit reached: model_calls"
    assert resumed.usage.model_calls == 1 and not requests
