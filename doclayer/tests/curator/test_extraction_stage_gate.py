"""Extraction stage on its own endpoint: stage routing and inheritance, the
vLLM request priority, and the idle gate that defers to other users."""

import asyncio
import json
import time

import httpx
import pytest
from test_pipeline import LONG_BODY, submit_payload_job

from doclayer import discovery
from doclayer.curator import model as model_module
from doclayer.curator.config import CuratorConfig, load_config, stage_config
from doclayer.curator.model import ModelClient
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import Usage

METRICS_URL = "http://gpu.test:9292/upstream/m/metrics"


def metrics_text(running, waiting):
    return (
        "# HELP vllm:num_requests_running Number of requests in model execution batches.\n"
        f'vllm:num_requests_running{{engine="0",model_name="m"}} {float(running)}\n'
        f'vllm:num_requests_waiting{{engine="0",model_name="m"}} {float(waiting)}\n'
        'vllm:request_success_total{engine="0"} 5.0\n'
    )


OK_BODY = {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}], "usage": {}}


class Endpoint:
    """A metrics + chat endpoint: metrics replies come from a script (the last
    one repeats); chat requests are recorded."""

    def __init__(self, scripted, running=None, on_metrics=None):
        self.scripted = list(scripted)
        #: llama-swap /running replies, last repeating: a list of {"model","state"} rows,
        #: an int status, or an Exception. Default: the gated model is ready.
        self.running_script = list(running) if running else [[{"model": "m", "state": "ready"}]]
        self.running_calls = 0
        self.upstream_calls = 0
        self.on_metrics = on_metrics
        self.metrics_calls = 0
        self.posts = []
        self.metrics_auth = []

    def transport(self):
        def handler(request):
            if request.url.path == "/running":
                self.running_calls += 1
                step = self.running_script.pop(0) if len(self.running_script) > 1 else self.running_script[0]
                if isinstance(step, Exception):
                    raise step
                if isinstance(step, int):
                    return httpx.Response(step, text="boom")
                return httpx.Response(200, json={"running": step})
            if request.url.path.startswith("/upstream/"):
                self.upstream_calls += 1
            if request.url.path.endswith("/metrics"):
                self.metrics_calls += 1
                if self.on_metrics:
                    self.on_metrics()
                self.metrics_auth.append(request.headers.get("authorization"))
                step = self.scripted.pop(0) if len(self.scripted) > 1 else self.scripted[0]
                if isinstance(step, Exception):
                    raise step
                if isinstance(step, int):
                    return httpx.Response(step, text="boom")
                return httpx.Response(200, text=step)
            self.posts.append(json.loads(request.content))
            return httpx.Response(200, json=OK_BODY)

        return httpx.MockTransport(handler)


def gated_client(cfg, endpoint, *, poll=0.01):
    cfg.inference.url = "http://gpu.test:9292/v1"
    cfg.inference.idle_metrics_url = METRICS_URL
    cfg.inference.idle_poll_s = poll
    return ModelClient(cfg, Usage(), transport=endpoint.transport())


# -- extraction stage ---------------------------------------------------------- #


def test_extraction_is_a_known_stage_and_falls_back_to_curation():
    cfg = CuratorConfig()
    cfg.inference.model = "global"
    cfg.stages = {"curation": {"model": "curate-model"}}
    assert stage_config(cfg, "extraction").inference.model == "curate-model"
    assert stage_config(CuratorConfig(), "extraction").inference.model == CuratorConfig().inference.model


def test_absent_extraction_stage_inherits_curation(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "inference:\n  model: global\n  thinking_budget_field: thinking_budget\n"
        "stages:\n  curation:\n    model: curate-model\n    timeout_s: 77\n    prompt_token_target: 65536\n"
    )
    cfg = load_config(path)
    extraction, curation = stage_config(cfg, "extraction"), stage_config(cfg, "curation")
    assert extraction.inference == curation.inference
    assert extraction.limits.prompt_char_budget == curation.limits.prompt_char_budget
    # with overrides of its own, extraction layers them on top of curation's
    cfg.stages["extraction"] = {"model": "local-model", "request_priority": 100}
    layered = stage_config(cfg, "extraction")
    assert layered.inference.model == "local-model" and layered.inference.request_priority == 100
    assert layered.inference.timeout_s == 77 and layered.inference.thinking_budget_field == "thinking_budget"
    assert stage_config(cfg, "curation").inference.request_priority is None  # curation untouched
    assert stage_config(cfg, "discovery").inference.model == "global"


def test_gate_keys_are_validated_at_load(tmp_path):
    def load(text):
        path = tmp_path / "config.yaml"
        path.write_text(text)
        return load_config(path)

    with pytest.raises(ValueError, match="request_priority must be an integer"):
        load("stages:\n  extraction:\n    request_priority: high\n")
    with pytest.raises(ValueError, match="idle_metrics_url must be an http"):
        load("stages:\n  extraction:\n    idle_metrics_url: ftp://x/metrics\n")
    with pytest.raises(ValueError, match="idle_poll_s must be a positive number"):
        load("stages:\n  extraction:\n    idle_poll_s: 0\n")
    with pytest.raises(ValueError, match="request_priority must be an integer"):
        load("inference:\n  request_priority: 1.5\n")
    ok = load(
        "stages:\n  extraction:\n    request_priority: 100\n    idle_metrics_url: http://h:1/m\n    idle_poll_s: 15\n"
    )
    assert stage_config(ok, "extraction").inference.idle_poll_s == 15


def test_extraction_stage_routes_only_extract_calls(cfg, store, qwen, checkout):
    cfg.inference.model = "hosted-model"
    cfg.stages = {"extraction": {"model": "local-model", "request_priority": 100}}
    inner = qwen.transport()
    seen = []

    def handler(request):
        body = json.loads(request.content)
        reply = inner.handle_request(request)
        seen.append((qwen.calls[-1]["task"], body["model"], body.get("priority")))
        return reply

    urls = ["https://docs.test/model-a"]
    pages = {
        urls[0]: (
            200,
            f"<html><head><title>A</title></head><body><article><h1>A</h1><p>{LONG_BODY}</p></article></body></html>",
        )
    }
    from conftest import HttpFixture

    state = submit_payload_job(store, checkout, urls=urls, intent="build test-model knowledge")
    state.mode = "single-pass"
    store.save(state)
    pipeline = Pipeline(
        cfg, store, fetch_transport=HttpFixture(pages).transport(), inference_transport=httpx.MockTransport(handler)
    )
    assert pipeline._model(state, stage="extraction").cfg.inference.model == "local-model"
    assert pipeline._model(state).cfg.inference.model == "hosted-model"
    asyncio.run(pipeline.run(state))
    extract = [row for row in seen if row[0] == "extract"]
    other = [row for row in seen if row[0] != "extract"]
    assert extract and other
    assert {row[1] for row in extract} == {"local-model"} and {row[2] for row in extract} == {100}
    assert {row[1] for row in other} == {"hosted-model"} and {row[2] for row in other} == {None}


def test_extract_budget_follows_the_extraction_stage(cfg, store, checkout, monkeypatch):
    from doclayer.curator import curate

    cfg.inference.serving_context_tokens = 262144
    cfg.limits.prompt_char_budget = cfg.inference.prompt_char_budget
    cfg.stages = {"extraction": {"prompt_token_target": 4096}}
    expected = stage_config(cfg, "extraction").limits.prompt_char_budget
    assert expected != cfg.limits.prompt_char_budget
    budgets = []

    async def fake_extract(client, source, text, intent, exclusions, *, char_budget, **kw):
        budgets.append(char_budget)
        return []

    monkeypatch.setattr(curate, "extract_claims", fake_extract)
    state = submit_payload_job(store, checkout, texts=[("note", LONG_BODY)], intent="x")
    state.mode = "single-pass"
    pipeline = Pipeline(cfg, store)

    state.sources[0].status = "fetched"
    (pipeline._cache_file(state, state.sources[0])).write_text(LONG_BODY)
    asyncio.run(pipeline._stage_extract(state))
    assert budgets == [expected]


# -- request priority ------------------------------------------------------------ #


async def test_request_priority_sent_only_when_set_on_the_model_client(cfg):
    endpoint = Endpoint([metrics_text(0, 0)])
    client = ModelClient(cfg, Usage(), transport=endpoint.transport())
    await client.chat("s", "u")
    cfg.inference.request_priority = 100
    await client.chat("s", "u")
    cfg.inference.request_priority = 0
    await client.chat("s", "u")
    assert ["priority" in body for body in endpoint.posts] == [False, True, True]
    assert [body.get("priority") for body in endpoint.posts] == [None, 100, 0]


def test_request_priority_sent_only_when_set_in_discovery(tmp_path, monkeypatch):
    real_client = httpx.Client

    def run(priority_line):
        config = tmp_path / "config.yaml"
        config.write_text(f"stages:\n  discovery:\n    model: judge\n{priority_line}")
        monkeypatch.setenv("BRAIN_CURATOR_CONFIG", str(config))
        monkeypatch.setenv("CURATOR_INFERENCE_API_KEY", "k")
        seen = []

        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "choices": [{"finish_reason": "stop", "message": {"content": '{"relevant": true}'}}],
                    "usage": {},
                },
            )

        monkeypatch.setattr(
            discovery.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
        )
        discovery._qwen_judgment("https://x.test/doc", "Doc", "need", "body", state_dir=tmp_path)
        return seen[0]

    assert "priority" not in run("")
    assert run("    request_priority: 100\n")["priority"] == 100


# -- idle gate --------------------------------------------------------------------- #


def test_parse_vllm_load_sums_label_sets_and_rejects_missing_gauges():
    text = (
        'vllm:num_requests_running{engine="0"} 1.0\nvllm:num_requests_running{engine="1"} 2.0\n'
        'vllm:num_requests_waiting{engine="0"} 0.0\n'
    )
    assert model_module.parse_vllm_load(text) == (3.0, 0.0)
    assert model_module.parse_vllm_load("vllm:num_requests_running 1.0\n") is None
    assert model_module.parse_vllm_load("<html>not metrics</html>") is None
    assert model_module.parse_vllm_load("vllm:num_requests_running NaN\nvllm:num_requests_waiting 0\n") is None


async def test_idle_gate_waits_while_others_run_then_proceeds(cfg):
    endpoint = Endpoint([metrics_text(2, 0), metrics_text(0, 0)])
    client = gated_client(cfg, endpoint)
    reply = await client.chat("s", "u")
    assert reply.text == "{}"
    assert endpoint.metrics_calls == 2  # one busy check, one idle check: one wait cycle
    assert len(endpoint.posts) == 1
    assert client.idle_wait_seconds >= 0.01 and client.usage.idle_wait_seconds == client.idle_wait_seconds


async def test_idle_gate_waits_while_requests_are_queued(cfg):
    endpoint = Endpoint([metrics_text(0, 3), metrics_text(0, 0)])
    client = gated_client(cfg, endpoint)
    await client.chat("s", "u")
    assert endpoint.metrics_calls == 2 and len(endpoint.posts) == 1


async def test_idle_gate_sends_the_inference_bearer_key_to_metrics(cfg, monkeypatch):
    monkeypatch.setenv("CURATOR_INFERENCE_API_KEY", "secret-key")
    endpoint = Endpoint([metrics_text(0, 0)])
    await gated_client(cfg, endpoint).chat("s", "u")
    assert endpoint.metrics_auth == ["Bearer secret-key"]


async def test_idle_gate_ignores_own_inflight(cfg):
    endpoint = Endpoint([metrics_text(1, 0)])
    client = gated_client(cfg, endpoint)
    model_module._own_adjust(METRICS_URL, 1)  # another thread of this process is mid-request
    try:
        await asyncio.wait_for(client.chat("s", "u"), timeout=5)
    finally:
        model_module._own_adjust(METRICS_URL, -1)
    assert endpoint.metrics_calls == 1 and len(endpoint.posts) == 1
    assert client.idle_wait_seconds == 0
    assert model_module._own_inflight(METRICS_URL) == 0


async def test_idle_gate_own_count_is_released_after_each_request(cfg):
    endpoint = Endpoint([metrics_text(0, 0)])
    client = gated_client(cfg, endpoint)
    seen = []
    inner = endpoint.transport()

    def handler(request):
        if not request.url.path.endswith("/metrics") and request.url.path != "/running":
            seen.append(model_module._own_inflight(METRICS_URL))
        return inner.handle_request(request)

    client._transport = httpx.MockTransport(handler)
    await client.chat("s", "u")
    assert seen == [1] and model_module._own_inflight(METRICS_URL) == 0


@pytest.mark.parametrize(
    "failure",
    [500, metrics_text(0, 0).replace("vllm:num_requests_waiting", "x"), "<html>nope</html>"],
    ids=["http-500", "missing-gauge", "not-metrics"],
)
async def test_idle_gate_fails_open_on_metrics_error(cfg, failure, caplog):
    endpoint = Endpoint([failure])
    client = gated_client(cfg, endpoint)
    with caplog.at_level("WARNING", logger=model_module.__name__):
        await client.chat("s", "u")
        await client.chat("s", "u")
    assert len(endpoint.posts) == 2 and client.idle_wait_seconds == 0
    assert len([r for r in caplog.records if "idle gate" in r.getMessage()]) == 1  # one line per client


async def test_idle_gate_waiting_not_counted_in_request_timeout(cfg):
    # three busy checks (>= 0.3 s of waiting) against a 0.15 s request timeout
    endpoint = Endpoint([metrics_text(1, 0), metrics_text(1, 0), metrics_text(1, 0), metrics_text(0, 0)])
    cfg.inference.timeout_s = 0.15
    client = gated_client(cfg, endpoint, poll=0.1)
    started = time.monotonic()
    reply = await client.chat("s", "u")
    assert reply.text == "{}" and time.monotonic() - started >= 0.3
    assert len(endpoint.posts) == 1 and client.idle_wait_seconds >= 0.3


async def test_idle_gate_keeps_waiting_with_no_timeout_and_honours_cancel(cfg):
    from doclayer.curator.pipeline import CancelledByRequest

    endpoint = Endpoint([metrics_text(2, 0)])  # never idle
    ticks = []

    def check_cancel():
        ticks.append(1)
        if len(ticks) >= 3:
            raise CancelledByRequest("job")

    cfg.inference.idle_metrics_url = METRICS_URL
    cfg.inference.idle_poll_s = 0.01
    cfg.inference.timeout_s = 0.01
    client = ModelClient(cfg, Usage(), transport=endpoint.transport(), check_cancel=check_cancel)
    with pytest.raises(CancelledByRequest):
        await client.chat("s", "u")
    assert endpoint.posts == []


async def test_no_gate_means_no_metrics_request(cfg):
    endpoint = Endpoint([metrics_text(9, 9)])
    await ModelClient(cfg, Usage(), transport=endpoint.transport()).chat("s", "u")
    assert endpoint.metrics_calls == 0 and len(endpoint.posts) == 1


# -- resident-model check (llama-swap /running) -------------------------------------- #


async def test_gate_waits_without_touching_upstream_while_another_model_is_loaded(cfg):
    # /running shows someone else's model, then ours ready, then idle metrics
    other = [{"model": "someone-elses-70b", "state": "ready"}]
    ours = [{"model": "m", "state": "ready"}]
    endpoint = Endpoint([metrics_text(0, 0)], running=[other, other, ours])
    client = gated_client(cfg, endpoint)
    await client.chat("s", "u")
    assert endpoint.running_calls == 3
    assert endpoint.metrics_calls == 1  # only polled once our model was ready
    assert len(endpoint.posts) == 1
    assert client.idle_wait_seconds >= 0.02


async def test_gate_treats_another_or_a_starting_model_as_busy_and_never_polls_upstream(cfg):
    for rows in (
        [{"model": "someone-elses-70b", "state": "ready"}],
        [{"model": "m", "state": "starting"}],
        [{"model": "m", "state": "stopping"}, {"model": "other", "state": "ready"}],
    ):
        endpoint = Endpoint([metrics_text(0, 0)], running=[rows])
        cfg.inference.url = "http://gpu.test:9292/v1"
        cfg.inference.idle_metrics_url = METRICS_URL
        cfg.inference.idle_poll_s = 0.01
        client = ModelClient(cfg, Usage(), transport=endpoint.transport())
        task = asyncio.ensure_future(client.chat("s", "u"))
        await asyncio.sleep(0.1)
        assert not task.done() and endpoint.upstream_calls == 0 and endpoint.posts == []
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_gate_proceeds_when_nothing_is_loaded_so_the_post_cold_loads_the_model(cfg):
    endpoint = Endpoint([metrics_text(9, 9)], running=[[]])
    client = gated_client(cfg, endpoint)
    await asyncio.wait_for(client.chat("s", "u"), timeout=5)
    assert endpoint.running_calls == 1
    assert endpoint.upstream_calls == 0  # no metrics poll that could load anything
    assert len(endpoint.posts) == 1 and client.idle_wait_seconds == 0


async def test_gate_polls_metrics_when_our_model_is_ready(cfg):
    endpoint = Endpoint([metrics_text(0, 0)])
    await gated_client(cfg, endpoint).chat("s", "u")
    assert endpoint.running_calls == 1 and endpoint.upstream_calls == 1 and len(endpoint.posts) == 1


@pytest.mark.parametrize("failure", [500, "not json"], ids=["http-500", "not-json"])
async def test_gate_fails_open_when_running_endpoint_errors(cfg, failure, caplog):
    endpoint = Endpoint([metrics_text(0, 0)], running=[failure])
    if failure == "not json":
        endpoint.running_script = [None]  # {"running": null} -> unusable
    client = gated_client(cfg, endpoint)
    with caplog.at_level("WARNING", logger=model_module.__name__):
        await client.chat("s", "u")
    assert len(endpoint.posts) == 1 and client.idle_wait_seconds == 0


def test_llama_swap_target_strips_the_upstream_path():
    assert model_module.llama_swap_target("http://h:9292/upstream/a%20b/metrics") == ("http://h:9292/running", "a b")
    assert model_module.llama_swap_target("http://h:9292/pre/upstream/m/metrics") == ("http://h:9292/pre/running", "m")
    assert model_module.llama_swap_target("http://h:9292/metrics") is None


# -- idle waiting is not active time ---------------------------------------------------- #


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_idle_waiting_does_not_consume_the_active_time_budget(cfg, store, qwen, checkout):
    from conftest import HttpFixture

    clock = FakeClock()
    cfg.inference.url = "http://inference.test/v1"
    cfg.inference.model = "qwen-test"
    cfg.stages = {
        "extraction": {"idle_metrics_url": "http://inference.test/upstream/qwen-test/metrics", "idle_poll_s": 0.01}
    }
    cap = cfg.limits.max_active_seconds
    busy_polls = []
    inner = qwen.transport()

    def handler(request):
        if request.url.path == "/running":
            return httpx.Response(200, json={"running": [{"model": "qwen-test", "state": "ready"}]})
        if request.url.path.endswith("/metrics"):
            busy_polls.append(1)
            if len(busy_polls) > 1:  # the first poll is the gate's own entry check
                clock.now += cap  # each wait cycle outlasts the whole active-time budget
            return httpx.Response(200, text=metrics_text(3 if len(busy_polls) <= 3 else 0, 0))
        return inner.handle_request(request)

    urls = ["https://docs.test/model-a"]
    pages = {
        urls[0]: (
            200,
            f"<html><head><title>A</title></head><body><article><h1>A</h1><p>{LONG_BODY}</p></article></body></html>",
        )
    }
    state = submit_payload_job(store, checkout, urls=urls, intent="build test-model knowledge")
    state.mode = "single-pass"
    store.save(state)
    pipeline = Pipeline(
        cfg,
        store,
        fetch_transport=HttpFixture(pages).transport(),
        inference_transport=httpx.MockTransport(handler),
        clock=clock,
    )
    state = asyncio.run(pipeline.run(state))
    assert len(busy_polls) >= 4  # waited through three full budgets of clock time
    assert state.error != "limit reached: active_time", state.error
    assert "extract" in state.stages_done
    assert state.usage.idle_wait_seconds >= 0.03
    assert state.usage.active_seconds < cap  # the waits are not charged


# -- idle_metrics_url / model consistency ---------------------------------------------- #


def test_idle_metrics_url_model_must_match_the_blocks_model(tmp_path):
    def load(text):
        path = tmp_path / "config.yaml"
        path.write_text(text)
        return load_config(path)

    url = "http://h:9292/upstream/qwen%20x/metrics"
    with pytest.raises(
        ValueError,
        match=r"stages.extraction.idle_metrics_url names model 'qwen x' but stages.extraction uses model 'other'",
    ):
        load(f"stages:\n  extraction:\n    model: other\n    idle_metrics_url: {url}\n")
    with pytest.raises(
        ValueError, match=r"inference.idle_metrics_url names model 'qwen x' but inference uses model 'qwen-y'"
    ):
        load(f"inference:\n  model: qwen-y\n  idle_metrics_url: {url}\n")
    # layering: curation's model applies to extraction unless extraction names its own
    with pytest.raises(
        ValueError,
        match="stages.extraction.idle_metrics_url names model 'qwen x' but stages.extraction uses model 'curate'",
    ):
        load(f"stages:\n  curation:\n    model: curate\n  extraction:\n    idle_metrics_url: {url}\n")
    ok = load(
        f"stages:\n  curation:\n    model: other\n  extraction:\n    model: qwen x\n    idle_metrics_url: {url}\n"
    )
    assert stage_config(ok, "extraction").inference.idle_metrics_url == url
    plain = load(
        "stages:\n  extraction:\n    model: any\n    idle_metrics_url: http://h:1/metrics\n"
    )  # no /upstream/: nothing to compare
    assert stage_config(plain, "extraction").inference.model == "any"


# -- an unreachable gated endpoint is waited out, not failed into ---------------- #


@pytest.mark.parametrize(
    "down",
    [httpx.ConnectError("no route"), httpx.ConnectTimeout("t"), httpx.ReadTimeout("t")],
    ids=["connect-error", "connect-timeout", "read-timeout"],
)
async def test_gate_waits_while_the_running_endpoint_is_unreachable(cfg, down, caplog):
    endpoint = Endpoint([metrics_text(0, 0)], running=[down, down, [{"model": "m", "state": "ready"}]])
    client = gated_client(cfg, endpoint)
    with caplog.at_level("WARNING", logger=model_module.__name__):
        await client.chat("s", "u")
    assert len(endpoint.posts) == 1 and endpoint.running_calls >= 3
    assert client.idle_wait_seconds > 0
    assert any("unreachable" in r.getMessage() for r in caplog.records)


async def test_gate_waits_while_metrics_are_unreachable(cfg):
    endpoint = Endpoint([httpx.ConnectError("no route"), metrics_text(0, 0)])
    client = gated_client(cfg, endpoint)
    await client.chat("s", "u")
    assert len(endpoint.posts) == 1 and endpoint.metrics_calls >= 2 and client.idle_wait_seconds > 0


async def test_unreachable_gate_wait_honours_cancel(cfg):
    endpoint = Endpoint([metrics_text(0, 0)], running=[httpx.ConnectError("no route")])
    client = gated_client(cfg, endpoint)
    calls = []

    def cancel():
        calls.append(1)
        if len(calls) >= 3:
            raise RuntimeError("cancelled")

    client._check_cancel = cancel
    with pytest.raises(RuntimeError, match="cancelled"):
        await client.chat("s", "u")
    assert endpoint.posts == []
