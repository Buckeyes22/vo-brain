"""Scoped decoder constraints preserve semantic validation and charged limits."""

import json

import httpx
import pytest

from doclayer.curator.config import CuratorConfig
from doclayer.curator.model import ModelClient, ModelError, OutputTruncated
from doclayer.curator.state import BudgetExceeded, Usage


def client_with(replies, *, retries=0, max_calls=50):
    cfg = CuratorConfig()
    cfg.inference.url = "http://pinned-local.test/v1"
    cfg.inference.retries = retries
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        status, content, finish = replies.pop(0)
        return httpx.Response(
            status,
            json={
                "choices": [{"message": {"content": content}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            },
        )

    return ModelClient(
        cfg, Usage(), limits={"max_model_calls": max_calls}, transport=httpx.MockTransport(handler)
    ), seen


@pytest.mark.asyncio
async def test_scoped_decoder_survives_parse_repair_and_preserves_source_and_caps():
    client, seen = client_with([(200, "invalid", "stop"), (200, '{"complete":false}', "stop")])
    result = await client.chat_bounded_json(
        "system",
        "whole unchanged source",
        char_budget=4500,
        json_object=True,
        enable_thinking=True,
        thinking_token_budget=1024,
    )
    assert result == {"complete": False}
    assert client.usage.model_calls == 2
    assert client.usage.model_output_tokens == 40
    for payload in seen:
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["thinking_token_budget"] == 1024
        assert payload["max_tokens"] == client.cfg.inference.effective_output_target
        assert payload["chat_template_kwargs"]["enable_thinking"] is True
        assert "whole unchanged source" in payload["messages"][1]["content"]


@pytest.mark.asyncio
async def test_server_rejection_has_no_unconstrained_or_provider_fallback():
    client, seen = client_with([(400, "unsupported format", "stop")])
    with pytest.raises(ModelError, match="HTTP 400") as error:
        await client.chat_bounded_json("system", "source", char_budget=4500, json_object=True)
    assert error.value.transient is False
    assert len(seen) == client.usage.model_calls == 1
    assert seen[0]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_valid_looking_truncated_json_is_still_rejected():
    client, seen = client_with([(200, '{"complete":true}', "length")])
    with pytest.raises(OutputTruncated):
        await client.chat_bounded_json("system", "source", char_budget=4500, json_object=True)
    assert len(seen) == client.usage.model_calls == 1


@pytest.mark.asyncio
async def test_unscoped_whole_array_contract_is_unchanged():
    client, seen = client_with([(200, '[{"id":"one","valid":false}]', "stop")])
    assert await client.chat_verdicts("system", "source") == {"verdicts": [{"id": "one", "valid": False}]}
    assert "response_format" not in seen[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"json_object": True, "json_mode": False},
        {"json_object": "true"},
    ],
)
async def test_invalid_decoder_policy_does_not_charge(arguments):
    client, seen = client_with([])
    with pytest.raises(ModelError, match="JSON task"):
        await client.chat("system", "source", **arguments)
    assert not seen and client.usage.model_calls == 0


@pytest.mark.asyncio
async def test_transport_retry_cannot_exceed_original_allowance(monkeypatch):
    client, seen = client_with([(503, "outage", "stop")], retries=1, max_calls=1)

    async def no_wait(_seconds):
        pass

    monkeypatch.setattr("asyncio.sleep", no_wait)
    with pytest.raises(BudgetExceeded):
        await client.chat_bounded_json("system", "source", char_budget=4500, json_object=True)
    assert len(seen) == client.usage.model_calls == client.max_calls == 1
    assert seen[0]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_thinking_budget_uses_configured_request_key():
    client, seen = client_with([(200, '{"ok": true}', "stop")])
    client.cfg.inference.thinking_budget_field = "thinking_budget"
    await client.chat("system", "user", json_object=True, enable_thinking=True, thinking_token_budget=512)
    assert seen[0]["thinking_budget"] == 512 and "thinking_token_budget" not in seen[0]


def test_thinking_budget_defaults_preserve_local_behavior(tmp_path):
    from doclayer.curator.config import load_config

    cfg = load_config(tmp_path / "absent.yaml")
    assert cfg.inference.thinking_token_budget == 2048
    path = tmp_path / "config.yaml"
    path.write_text(
        "inference:\n  thinking_token_budget: 16384\n  serving_max_output_tokens: 131072\n  output_token_target: 32768\n"
    )
    cfg = load_config(path)
    assert min(cfg.inference.thinking_token_budget, cfg.inference.effective_output_target // 2) == 16384
