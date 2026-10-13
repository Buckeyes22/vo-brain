"""Native intake preserves strict extraction/literal guards under bounded thinking."""

import copy
import json

import httpx
import pytest

from doclayer.curator import curate, source_constraints
from doclayer.curator.model import ModelClient, ModelError
from doclayer.curator.source_admission import _ReservedClient
from doclayer.curator.state import Usage


def response(value):
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value)}, "finish_reason": "stop"}]})


def check_policy(payload, output_target):
    assert payload["max_tokens"] == output_target
    assert payload["thinking_token_budget"] == min(2048, output_target // 2)
    assert payload["chat_template_kwargs"] == {"enable_thinking": True}
    assert payload["response_format"] == {"type": "json_object"}
    assert sum(len(m["content"]) for m in payload["messages"]) <= 6000


@pytest.mark.asyncio
@pytest.mark.parametrize("output_target", [512, 4096])
@pytest.mark.parametrize("corrected", [False, True])
async def test_native_extraction_rejects_undeclared_applicability_without_coercion(cfg, output_target, corrected):
    cfg.inference.enable_thinking = False
    cfg.inference.output_token_target = output_target
    usage = Usage()
    seen = []
    good = {"claims": [{"claim": "The documented deployment applies to App1.", "applicability": {"scope": "App1"}}]}

    def handler(request):
        payload = json.loads(request.content)
        check_policy(payload, output_target)
        seen.append(payload)
        value = copy.deepcopy(good)
        if len(seen) == 1 or not corrected:
            value["claims"][0]["applicability"] = {"host": "App1"}
        if len(seen) > 1:
            assert "applicability has an undeclared field" in payload["messages"][0]["content"]
        return response(value)

    client = _ReservedClient(
        ModelClient(cfg, usage, limits={"max_model_calls": 10}, transport=httpx.MockTransport(handler)), reserve=0
    )
    if corrected:
        assert (
            await curate._extract_json(client, "Extract exact documented facts.", "Complete source text.", 6000) == good
        )
    else:
        with pytest.raises(ModelError, match="invalid whole source-claim"):
            await curate._extract_json(client, "Extract exact documented facts.", "Complete source text.", 6000)
    assert usage.model_calls == len(seen) == (2 if corrected else 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("output_target", [512, 4096])
@pytest.mark.parametrize("invalid", ["wrong-wire", "nonliteral"])
@pytest.mark.parametrize("corrected", [False, True])
async def test_native_quote_repair_keeps_literal_and_id_edits_contract(cfg, output_target, invalid, corrected):
    cfg.inference.enable_thinking = False
    cfg.inference.output_token_target = output_target
    usage = Usage()
    body = "Only laboratory installations. Access requires approval."
    original = {
        "id": "scope",
        "supported": False,
        "consistent": True,
        "reason": "Scope only.",
        "qualifiers": ["Only laboratory installations.", "invented label"],
        "support_quotes": [],
    }
    targets = source_constraints._quote_targets([original], "scope", ["qualifiers[1] is not a literal source span"])
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        check_policy(payload, output_target)
        seen.append(payload)
        assert body in payload["messages"][1]["content"]
        good = {
            "verdicts": [
                {
                    "id": "scope",
                    "edits": [{"field": "qualifiers", "index": 1, "replacement": "Access requires approval."}],
                }
            ]
        }
        if len(seen) == 1 or not corrected:
            if invalid == "wrong-wire":
                return response({"verdicts": [original]})
            good["verdicts"][0]["edits"][0]["replacement"] = "Invented condition."
        return response(good)

    client = _ReservedClient(
        ModelClient(cfg, usage, limits={"max_model_calls": 10}, transport=httpx.MockTransport(handler)), reserve=0
    )
    call = source_constraints._repair_quote_fields(
        client,
        original,
        targets,
        {"ancestor_context": body},
        [("Manual", body)],
        body,
        body,
        [],
        [],
        char_budget=6000,
        attempts=2,
        metadata={},
    )
    if corrected:
        value = (await call)[0]
        assert value == {**original, "qualifiers": ["Only laboratory installations.", "Access requires approval."]}
    else:
        with pytest.raises(ModelError, match="invalid source quotation repair"):
            await call
    assert original["qualifiers"] == ["Only laboratory installations.", "invented label"]
    assert usage.model_calls == len(seen) == 2


@pytest.mark.asyncio
async def test_profile_does_not_bypass_source_reserve_or_exhausted_usage(cfg):
    from doclayer.curator.source_admission import SourceAdmissionDeferred, source_budget
    from doclayer.curator.state import BudgetExceeded

    cfg.inference.enable_thinking = False
    calls = []

    def handler(request):
        calls.append(request)
        return response({"claims": []})

    for spent, error in [(8, SourceAdmissionDeferred), (10, BudgetExceeded)]:
        usage = Usage(model_calls=spent)
        native = ModelClient(cfg, usage, limits={"max_model_calls": 10}, transport=httpx.MockTransport(handler))
        with source_budget(native, reserve=2) as client:
            with pytest.raises(error):
                await curate._extract_json(client, "Extract.", "Complete source.", 6000)
        assert usage.model_calls == spent
    assert calls == []
