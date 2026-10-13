"""Native curator reservations cannot consume both downstream verdict calls."""

import httpx
import pytest

from doclayer.curator.config import CuratorConfig
from doclayer.curator.model import ModelClient
from doclayer.curator.state import Usage
from doclayer.gap_budget import BudgetUnavailable, ResourceBudget, today


@pytest.mark.asyncio
async def test_prior_attempt_kept_and_two_downstream_http_calls_fit(tmp_path, monkeypatch):
    budget = ResourceBudget(tmp_path, max_jobs=3, max_calls_day=100, max_calls_job=50)
    prior = budget.reserve_attempt("prior", "gap-prior", 50)
    admitted = budget.reserve_attempt("next", "gap-next", 50, headroom=2)
    assert prior["calls"] == 50 and admitted["calls"] == 48
    assert budget.reserve_attempt("prior", "gap-prior", 50, headroom=2) == prior
    cfg = CuratorConfig()
    cfg.inference.url = "http://inference.test/v1"
    cfg.inference.api_key_env = "HEADROOM_TEST_KEY"
    monkeypatch.setenv("HEADROOM_TEST_KEY", "fixture-only-key")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"supported":true}'}, "finish_reason": "stop"}]}
        )

    client = ModelClient(
        cfg,
        Usage(),
        transport=httpx.MockTransport(respond),
        before_call=lambda: budget.consume_model(kind="verification", attempt_id="gap-next"),
    )
    for kind in ("canonical evidence", "delivered evidence"):
        assert await client.chat_json("Check source support", kind) == {"supported": True}
    with pytest.raises(BudgetUnavailable, match="daily inference"):
        await client.chat_json("Check source support", "third attempt")
    assert len(requests) == 2
    saved = budget.load(today())
    assert saved["reserved_calls"] == 98 and saved["model_calls"] == 2
    assert saved["entries"][0]["reserved_calls"] == 50 and not saved["entries"][0]["released"]
    assert budget.reserve_attempt("another", "gap-another", 50, headroom=2) is None


def test_only_verification_room_left_does_not_admit_a_job(tmp_path):
    budget = ResourceBudget(tmp_path, max_jobs=3, max_calls_day=52, max_calls_job=50)
    assert budget.reserve_attempt("prior", "gap-prior", 50)["calls"] == 50
    assert budget.reserve_attempt("next", "gap-next", 50, headroom=2) is None
    assert budget.load(today())["reserved_calls"] == 50


def test_invalid_headroom_cannot_expand_allowance(tmp_path):
    budget = ResourceBudget(tmp_path)
    for bad in (-1, True, 1.5):
        with pytest.raises(ValueError, match="nonnegative integer"):
            budget.reserve_attempt("next", "gap-next", 50, headroom=bad)
