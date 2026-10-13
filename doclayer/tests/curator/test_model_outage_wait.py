"""A connection-level failure on a chat request waits for the endpoint to return
(no timeout, not active time, cancel honoured) instead of consuming retries;
HTTP errors and a reachable-but-failing endpoint keep the bounded behaviour."""

import httpx
import pytest

from doclayer.curator import model as model_module
from doclayer.curator.config import CuratorConfig
from doclayer.curator.model import ModelClient, ModelError
from doclayer.curator.state import Usage

OK_BODY = {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}], "usage": {}}
GATE_URL = "http://gpu.test:9292/upstream/m/metrics"
IDLE_METRICS = (
    'vllm:num_requests_running{engine="0",model_name="m"} 0.0\n'
    'vllm:num_requests_waiting{engine="0",model_name="m"} 0.0\n'
)


BUSY_METRICS = (
    'vllm:num_requests_running{engine="0",model_name="m"} 3.0\n'
    'vllm:num_requests_waiting{engine="0",model_name="m"} 2.0\n'
)


class Cancelled(Exception):
    pass


class Endpoint:
    """Scripted endpoint. ``posts``/``probes``/``running``/``metrics`` are scripts
    (last step repeats): an Exception to raise, an int status, or "ok"."""

    def __init__(self, posts, probes=("ok",), running=None, metrics=("idle",), busy_polls_after_post=0):
        self.post_script = list(posts)
        self.probe_script = list(probes)
        self.running_script = list(running) if running else ["ready"]
        self.metrics_script = list(metrics)
        self.post_calls = self.probe_calls = self.running_calls = self.metrics_calls = 0
        self.probe_auth = []
        #: after every POST the gate reads "busy" this many times (other users, never down)
        self.busy_polls_after_post = busy_polls_after_post
        self.busy_left = 0

    @staticmethod
    def _step(script):
        return script.pop(0) if len(script) > 1 else script[0]

    def transport(self):
        def handler(request):
            path = request.url.path
            if request.method == "POST":
                self.post_calls += 1
                self.busy_left = self.busy_polls_after_post
                step = self._step(self.post_script)
                if isinstance(step, Exception):
                    raise step
                if isinstance(step, int):
                    return httpx.Response(step, text="boom")
                return httpx.Response(200, json=OK_BODY)
            if path == "/running":
                self.running_calls += 1
                step = self._step(self.running_script)
                if isinstance(step, Exception):
                    raise step
                return httpx.Response(200, json={"running": [{"model": "m", "state": step}]})
            if path.endswith("/metrics"):
                self.metrics_calls += 1
                step = self._step(self.metrics_script)
                if isinstance(step, Exception):
                    raise step
                if self.busy_left > 0:
                    self.busy_left -= 1
                    return httpx.Response(200, text=BUSY_METRICS)
                return httpx.Response(200, text=IDLE_METRICS)
            assert path == "/v1/models", path
            self.probe_calls += 1
            self.probe_auth.append(request.headers.get("authorization"))
            step = self._step(self.probe_script)
            if isinstance(step, Exception):
                raise step
            return httpx.Response(step if isinstance(step, int) else 200, text="x")

        return httpx.MockTransport(handler)


def make_client(endpoint, *, retries=0, gated=False, wait=True, **hooks):
    cfg = CuratorConfig()
    cfg.inference.url = "http://gpu.test:9292/v1"
    cfg.inference.retries = retries
    cfg.inference.idle_poll_s = 0.01
    if gated:
        cfg.inference.idle_metrics_url = GATE_URL
    return ModelClient(cfg, Usage(), transport=endpoint.transport(), wait_out_outages=wait, **hooks)


def conn():
    return httpx.ConnectError("refused")


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """Keep the bounded-retry backoff from slowing the suite."""
    real_sleep = model_module.asyncio.sleep

    async def fast(delay, *a, **k):
        await real_sleep(min(delay, 0.01))

    monkeypatch.setattr(model_module.asyncio, "sleep", fast)


@pytest.fixture(autouse=True)
def _clean_inflight():
    yield
    assert model_module._own_inflight(GATE_URL) == 0


# -- ungated -------------------------------------------------------------------- #


async def test_ungated_outage_waits_then_retries_without_consuming_retries(monkeypatch):
    monkeypatch.setenv("DOCLAYER_INFERENCE_API_KEY", "k")
    ep = Endpoint(posts=[conn(), "ok"], probes=[conn(), conn(), 404])
    client = make_client(ep, retries=0)
    reply = await client.chat("s", "u")
    assert reply.text == "{}"
    assert ep.post_calls == 2
    assert ep.probe_calls == 3
    assert client.usage.idle_wait_seconds > 0
    assert client.usage.model_calls == 2  # one charged POST per attempt; the wait itself is free


async def test_ungated_probe_sends_the_bearer_key(monkeypatch):
    cfg_key = "secret-key"
    ep = Endpoint(posts=[conn(), "ok"], probes=[conn(), 200])
    client = make_client(ep, retries=0)
    monkeypatch.setattr(client.cfg, "inference_api_key", lambda: cfg_key)
    await client.chat("s", "u")
    assert set(ep.probe_auth) == {f"Bearer {cfg_key}"}


async def test_ungated_any_http_answer_counts_as_reachable():
    # 401/503 from /models still prove the server answers: bounded attempt, no wait
    ep = Endpoint(posts=[conn()], probes=[401])
    client = make_client(ep, retries=0)
    with pytest.raises(ModelError, match="unreachable: ConnectError"):
        await client.chat("s", "u")
    assert ep.post_calls == 1 and client.usage.idle_wait_seconds == 0


async def test_ungated_blip_counts_as_a_normal_attempt():
    ep = Endpoint(posts=[conn(), "ok"], probes=[200])
    client = make_client(ep, retries=1)
    reply = await client.chat("s", "u")
    assert reply.text == "{}" and reply.calls_made == 2
    assert ep.post_calls == 2 and client.usage.idle_wait_seconds == 0


async def test_ungated_flapping_endpoint_is_bounded_when_probe_is_reachable():
    ep = Endpoint(posts=[conn()], probes=[200])
    client = make_client(ep, retries=0)
    with pytest.raises(ModelError, match="unreachable: ConnectError") as err:
        await client.chat("s", "u")
    assert err.value.transient is True
    assert ep.post_calls == 1 and client.usage.idle_wait_seconds == 0

    ep = Endpoint(posts=[conn()], probes=[200])
    client = make_client(ep, retries=2)
    with pytest.raises(ModelError, match="unreachable"):
        await client.chat("s", "u")
    assert ep.post_calls == 3


async def test_outage_can_repeat_and_each_wait_is_free_of_retries():
    ep = Endpoint(posts=[conn(), httpx.ReadTimeout("slow"), "ok"], probes=[conn(), 200, conn(), 200])
    client = make_client(ep, retries=0)
    reply = await client.chat("s", "u")
    assert reply.text == "{}" and ep.post_calls == 3


# -- gated ---------------------------------------------------------------------- #


async def test_gated_mid_response_drop_waits_at_the_gate_and_retries():
    ep = Endpoint(posts=[httpx.ReadError("dropped"), "ok"], running=["ready", conn(), conn(), "ready"])
    client = make_client(ep, retries=0, gated=True)
    reply = await client.chat("s", "u")
    assert reply.text == "{}"
    assert ep.post_calls == 2
    assert ep.probe_calls == 0  # the gate is the probe; /models is never used
    assert client.usage.idle_wait_seconds > 0


async def test_gated_reachable_and_idle_after_drop_is_a_normal_attempt():
    ep = Endpoint(posts=[httpx.ReadError("dropped")])
    client = make_client(ep, retries=0, gated=True)
    with pytest.raises(ModelError, match="unreachable: ReadError"):
        await client.chat("s", "u")
    assert ep.post_calls == 1 and client.usage.idle_wait_seconds == 0


# -- cancel and active time ----------------------------------------------------- #


async def test_cancel_during_the_down_wait_stops_without_another_post():
    state = {"cancel": False}

    def check_cancel():
        if state["cancel"]:
            raise Cancelled()

    ep = Endpoint(posts=[conn(), "ok"], probes=[conn()])
    client = make_client(ep, retries=0, check_cancel=check_cancel)
    real_get = ep.transport

    # flip the cancel flag once the first probe has seen the endpoint down
    def flip_transport():
        inner = real_get()

        def handler(request):
            try:
                return inner.handle_request(request)
            finally:
                if request.method == "GET":
                    state["cancel"] = True

        return httpx.MockTransport(handler)

    client._transport = flip_transport()
    with pytest.raises(Cancelled):
        await client.chat("s", "u")
    assert ep.post_calls == 1


@pytest.mark.parametrize("gated", [False, True])
async def test_down_wait_is_wrapped_in_the_idle_hooks_exactly_once(gated):
    events = []
    if gated:
        ep = Endpoint(posts=[httpx.ReadError("dropped"), "ok"], running=["ready", conn(), conn(), conn(), "ready"])
    else:
        ep = Endpoint(posts=[conn(), "ok"], probes=[conn(), conn(), conn(), 200])
    client = make_client(
        ep,
        retries=0,
        gated=gated,
        before_idle_wait=lambda: events.append("before"),
        after_idle_wait=lambda: events.append("after"),
    )
    await client.chat("s", "u")
    assert events == ["before", "after"]  # one wait, many polls, one pair of hooks
    assert client.usage.idle_wait_seconds > 0


async def test_hooks_are_not_called_without_an_outage():
    events = []
    ep = Endpoint(posts=["ok"])
    client = make_client(ep, before_idle_wait=lambda: events.append("b"), after_idle_wait=lambda: events.append("a"))
    await client.chat("s", "u")
    assert events == []


async def test_each_outage_logs_one_warning_and_one_return(caplog):
    caplog.set_level("INFO", logger=model_module.log.name)
    # two separate outages, each with three failed probes: still one WARNING apiece
    ep = Endpoint(posts=[conn(), conn(), "ok"], probes=[conn(), conn(), conn(), 200, conn(), conn(), 200])
    client = make_client(ep, retries=0)
    await client.chat("s", "u")
    warnings = [r for r in caplog.records if "inference endpoint unreachable (ConnectError)" in r.getMessage()]
    returns = [r for r in caplog.records if r.levelname == "INFO" and "returned" in r.getMessage()]
    assert len(warnings) == 2 and all(r.levelname == "WARNING" for r in warnings)
    assert len(returns) == 2


async def test_polling_one_outage_does_not_repeat_the_warning(caplog):
    caplog.set_level("INFO", logger=model_module.log.name)
    ep = Endpoint(posts=[conn(), "ok"], probes=[conn(), conn(), conn(), conn(), 200])
    await make_client(ep, retries=0).chat("s", "u")
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


# -- the wait is opt-in, and a busy endpoint is not a down one --------------------- #


async def test_default_client_keeps_the_bounded_behaviour_on_a_down_endpoint():
    ep = Endpoint(posts=[conn()], probes=[conn()])
    client = make_client(ep, retries=1, wait=False)
    with pytest.raises(ModelError, match="unreachable: ConnectError"):
        await client.chat("s", "u")
    assert ep.post_calls == 2 and ep.probe_calls == 0 and client.usage.idle_wait_seconds == 0


async def test_default_client_gated_is_bounded_too():
    ep = Endpoint(posts=[httpx.ReadError("dropped")], running=["ready", conn(), "ready"])
    client = make_client(ep, retries=1, gated=True, wait=False)
    with pytest.raises(ModelError):
        await client.chat("s", "u")
    assert ep.post_calls == 2


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("slow"), httpx.ReadError("dropped")])
async def test_gated_endpoint_that_is_only_busy_is_a_normal_attempt(exc):
    ep = Endpoint(posts=[exc], busy_polls_after_post=2)
    client = make_client(ep, retries=2, gated=True)
    with pytest.raises(ModelError, match="unreachable"):
        await client.chat("s", "u")
    assert ep.post_calls == 3  # retries + 1, never an unbounded loop
    assert client.usage.model_calls == 3
    assert client.usage.idle_wait_seconds > 0  # it did wait for the busy gate


async def test_gated_busy_then_unreachable_in_one_wait_counts_as_down():
    # busy on the first poll, down on a later one: the endpoint did go away
    ep = Endpoint(
        posts=[httpx.ReadTimeout("slow"), "ok"], busy_polls_after_post=1, running=["ready", "ready", conn(), "ready"]
    )
    client = make_client(ep, retries=0, gated=True)
    reply = await client.chat("s", "u")
    assert reply.text == "{}" and ep.post_calls == 2


# -- HTTP errors keep today's behaviour ----------------------------------------- #


@pytest.mark.parametrize("status,transient", [(503, True), (429, True), (400, False), (401, False)])
async def test_http_status_errors_never_enter_the_outage_wait(status, transient):
    events = []
    ep = Endpoint(posts=[status], probes=[conn()])
    client = make_client(ep, retries=1, before_idle_wait=lambda: events.append("b"))
    with pytest.raises(ModelError) as err:
        await client.chat("s", "u")
    assert err.value.transient is transient
    assert ep.probe_calls == 0 and events == [] and client.usage.idle_wait_seconds == 0
    assert ep.post_calls == (2 if transient else 1)


async def test_charge_call_runs_once_per_post_attempt():
    ep = Endpoint(posts=[conn(), conn(), "ok"], probes=[conn(), 200, conn(), 200])
    client = make_client(ep, retries=0)
    await client.chat("s", "u")
    assert client.usage.model_calls == ep.post_calls == 3


# -- the pipeline waits out an outage instead of going retryable -------------------- #


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_pipeline_job_stays_processing_through_an_outage_and_finishes(cfg, store, qwen, checkout):
    import asyncio

    from conftest import HttpFixture
    from test_pipeline import submit_payload_job

    from doclayer.curator.pipeline import Pipeline

    cfg.inference.idle_poll_s = 0.01
    clock = Clock()
    inner = qwen.transport()
    seen = {"probes": 0, "posts_while_down": 0, "status_while_down": set(), "state": None}
    down_probes = 4  # the endpoint answers again on the fifth probe

    def handler(request):
        if request.method == "GET":
            seen["probes"] += 1
            if seen["probes"] <= down_probes:
                if seen["probes"] > 1:  # the first probe is active time; the polls are the wait
                    clock.now += 100.0
                seen["status_while_down"].add(store.load(seen["state"].job_id).status)
                raise httpx.ConnectError("no route")
            return httpx.Response(404)
        if not seen.get("tripped"):
            seen["tripped"] = True  # the very first request is already in flight when the host dies
            raise httpx.ConnectError("connection reset")
        if 0 < seen["probes"] <= down_probes:
            seen["posts_while_down"] += 1
        return inner.handle_request(request)

    state = submit_payload_job(store, checkout, texts=[("n", "note " * 60)], intent="outage test")
    seen["state"] = state
    pipeline = Pipeline(
        cfg,
        store,
        fetch_transport=HttpFixture({}).transport(),
        inference_transport=httpx.MockTransport(handler),
        clock=clock,
    )
    result = asyncio.run(pipeline.run(state))
    assert seen["posts_while_down"] == 0  # nothing was sent while it was down
    assert seen["probes"] == down_probes + 1
    assert seen["status_while_down"] == {"processing"}
    # every model stage ran to the end; only publication (no Forgejo in this fixture) stops it
    assert result.stage == "publish" and (result.error or "").startswith("publication:"), (result.stage, result.error)
    assert result.usage.idle_wait_seconds > 0
    assert clock.now == 100.0 * (down_probes - 1)
    assert result.usage.active_seconds == 0  # the wait is not charged as active time
