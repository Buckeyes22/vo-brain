"""Model client: pinned local Qwen, budget enforcement, defensive parsing,
bounded repair, no silent fallback."""

from __future__ import annotations

import json

import httpx
import pytest

from doclayer.curator.config import CuratorConfig
from doclayer.curator.model import (
    BudgetExceeded,
    ModelClient,
    ModelError,
    OutputTruncated,
    extract_json,
)
from doclayer.curator.state import Usage


def qwen_transport(responses: list[httpx.Response]) -> httpx.MockTransport:
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if queue:
            return queue.pop(0)
        return httpx.Response(500, text="scripted exhaustion")

    return httpx.MockTransport(handler)


def ok(content: str, finish: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def make_client(cfg: CuratorConfig, transport, max_calls: int = 10) -> ModelClient:
    usage = Usage()
    return ModelClient(cfg, usage, limits={"max_model_calls": max_calls}, transport=transport)


@pytest.mark.asyncio
async def test_transport_retries_cannot_exceed_actual_request_budget(cfg):
    cfg.inference.retries = 3
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503)

    client = make_client(cfg, httpx.MockTransport(handler), max_calls=2)
    with pytest.raises(BudgetExceeded):
        await client.chat("system", "user")
    assert len(requests) == client.usage.model_calls == 2


@pytest.fixture()
def cfg() -> CuratorConfig:
    c = CuratorConfig()
    c.inference.url = "http://inference.test/v1"
    c.inference.model = "qwen-test"
    c.inference.retries = 1
    return c


@pytest.mark.asyncio
async def test_chat_records_usage_and_pins_model(cfg: CuratorConfig) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        seen["model"] = body["model"]
        assert request.url.path == "/v1/chat/completions"
        return ok("hello")

    usage = Usage()
    client = ModelClient(cfg, usage, transport=httpx.MockTransport(handler))
    reply = await client.chat("sys", "user", json_mode=False)
    assert reply.text == "hello"
    assert seen["model"] == "qwen-test"
    assert usage.model_calls == 1
    assert usage.model_input_tokens == 10 and usage.model_output_tokens == 5


@pytest.mark.asyncio
async def test_budget_stop_is_recorded_not_swallowed(cfg: CuratorConfig) -> None:
    client = make_client(cfg, qwen_transport([ok("a")]), max_calls=1)
    await client.chat("s", "u")
    with pytest.raises(BudgetExceeded):
        await client.chat("s", "u")


@pytest.mark.asyncio
async def test_transient_5xx_retried_then_succeeds(cfg: CuratorConfig) -> None:
    client = make_client(cfg, qwen_transport([httpx.Response(503, text="x"), ok("fine")]))
    reply = await client.chat("s", "u")
    assert reply.text == "fine"


@pytest.mark.asyncio
async def test_auth_failure_is_not_retried_as_transient(cfg: CuratorConfig) -> None:
    client = make_client(cfg, qwen_transport([httpx.Response(401, text="no")]))
    with pytest.raises(ModelError) as excinfo:
        await client.chat("s", "u")
    assert excinfo.value.transient is False


@pytest.mark.asyncio
async def test_truncation_raises_recoverable_condition(cfg: CuratorConfig) -> None:
    client = make_client(cfg, qwen_transport([ok('{"partial":', finish="length")]))
    with pytest.raises(OutputTruncated):
        await client.chat("s", "u")


@pytest.mark.asyncio
async def test_chat_json_repairs_a_malformed_reply_once(cfg: CuratorConfig) -> None:
    transport = qwen_transport([ok("oops not json at all"), ok('{"fixed": true}')])
    client = make_client(cfg, transport)
    data = await client.chat_json("s", "u")
    assert data == {"fixed": True}
    assert client.usage.model_calls == 2  # original + one bounded repair ask


@pytest.mark.asyncio
async def test_chat_json_fails_closed_after_repair(cfg: CuratorConfig) -> None:
    client = make_client(cfg, qwen_transport([ok("nope"), ok("still nope")]))
    with pytest.raises(ModelError):
        await client.chat_json("s", "u")


# -- extract_json defensiveness ---------------------------------------------- #


def test_extract_json_handles_fences_and_prose() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here it is: {"a": {"b": 2}} hope that helps') == {"a": {"b": 2}}
    assert extract_json('{"s": "brace in string }", "t": 1}') == {"s": "brace in string }", "t": 1}
    assert extract_json("") is None
    assert extract_json("no json here") is None
    assert extract_json("[1, 2, 3]") is None  # top-level array is not an object


@pytest.mark.parametrize(
    "text",
    [
        '[{"id":"one"},{"id":"two"}]',
        '```json\n[{"id":"one"},{"id":"two"}]\n```',
        'Results: [{"id":"one"},{"id":"two"}] done',
    ],
)
def test_root_array_never_silently_loses_siblings(text):
    assert extract_json(text) is None
    assert extract_json(text, root_array_key="verdicts") == {"verdicts": [{"id": "one"}, {"id": "two"}]}


def test_malformed_outer_object_never_exposes_valid_nested_object():
    assert extract_json('{broken: {"id":"nested"}}') is None


@pytest.mark.asyncio
async def test_generic_json_task_repairs_array_instead_of_using_first_object(cfg):
    client = make_client(cfg, qwen_transport([ok('[{"first":true},{"second":true}]'), ok('{"whole":true}')]))
    assert await client.chat_json("system", "user") == {"whole": True}
    assert client.usage.model_calls == 2


@pytest.mark.asyncio
async def test_verdict_parse_repair_preserves_material_and_combined_prompt_bound(cfg):
    seen = []

    def respond(request):
        messages = json.loads(request.content)["messages"]
        seen.append(messages)
        assert sum(len(message["content"]) for message in messages) <= 2000
        return ok("not JSON " * 1000) if len(seen) == 1 else ok("[]")

    client = make_client(cfg, httpx.MockTransport(respond))
    system = "Required contract. " * 30
    user = "Source evidence. " * 80
    assert await client.chat_verdicts(system, user, char_budget=2000) == {"verdicts": []}
    assert len(seen) == 2 and seen[1][1]["content"].startswith(user)


@pytest.mark.asyncio
async def test_json_repair_reports_actual_decode_error_and_retains_input(cfg):
    requests = []

    def handler(request):
        item = json.loads(request.content)
        requests.append(item["messages"])
        return ok('{"verdicts":[{"id":"a",}]}') if len(requests) == 1 else ok('{"verdicts":[]}')

    client = make_client(cfg, httpx.MockTransport(handler))
    result = await client.chat_verdicts("system", "SOURCE AND CLAIM", char_budget=1000)
    assert result == {"verdicts": []}
    feedback = requests[1][1]["content"]
    assert feedback.startswith("SOURCE AND CLAIM")
    assert "Expecting property name" in feedback or "Illegal trailing comma" in feedback
    assert "no balanced" not in feedback


@pytest.mark.asyncio
async def test_json_failure_diagnostics_are_precise_without_source_archive(cfg):
    client = make_client(cfg, qwen_transport([ok('{"verdicts":[{"id":"a",}]}')]))
    with pytest.raises(ModelError) as caught:
        await client.chat_json("system", "source", repair_attempts=0, root_array_key="verdicts")
    diagnostic = caught.value.diagnostic
    assert diagnostic["error_kind"] == "decode"
    assert diagnostic["position"] > 0 and diagnostic["line"] == 1
    assert "Expecting property name" in diagnostic["error"] or "Illegal trailing comma" in diagnostic["error"]
    assert "source" not in diagnostic and "response" not in diagnostic


@pytest.mark.asyncio
async def test_no_identical_reask_when_feedback_cannot_fit(cfg):
    client = make_client(cfg, qwen_transport([ok("{broken")]))
    with pytest.raises(ModelError) as caught:
        await client.chat_verdicts("system", "source", char_budget=12)
    assert client.usage.model_calls == 1
    assert caught.value.diagnostic["repair_unavailable"]


def test_multiple_roots_cannot_discard_later_siblings():
    assert extract_json('{"a":1} {"b":2}') is None
    assert extract_json('prefix [{"a":1}] [{"b":2}]', root_array_key="verdicts") is None


@pytest.mark.parametrize(
    "reply", ['null {"verdicts":[]}', '{"verdicts":[]} null', '{"a":1} then {"b":2}', 'true [{"id":"a"}]']
)
def test_other_root_cannot_hide_behind_prose_or_scalar(reply):
    assert extract_json(reply, root_array_key="verdicts") is None
