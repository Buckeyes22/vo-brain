"""An unreachable inference endpoint is not evidence that documentation does not
exist: discovery says so, and the gap worker waits briefly without penalising
the gap. A server that answers (a verdict, an HTTP error) keeps today's path."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from test_discovery_limits import GAP, catalog
from test_gap_worker import env as env  # noqa: F401

from doclayer import discovery, gap_worker
from doclayer.curator import config
from doclayer.discovery import DiscoveryResult

LINKS = '<a href="/one">one</a><a href="/two">two</a><a href="/three">three</a>'


def run_discovery(monkeypatch, tmp_path, judge, probe=200):
    """Discover over a root page with three unjudgeable links; ``judge(n)`` is the
    model endpoint's reply (or exception) for the nth judgment request and
    ``probe`` what ``GET /v1/models`` does (a status, or an exception)."""
    cfg = SimpleNamespace(
        limits=SimpleNamespace(prompt_char_budget=60000),
        inference=SimpleNamespace(
            url="http://native.test/v1", model="local-qwen", timeout_s=60, effective_output_target=4096
        ),
        inference_api_key=lambda: "fixture-key",
        stages={},
    )
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    page_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=LINKS if request.url.path == "/" else "unrelated material")
        )
    )
    real_client = httpx.Client
    calls = []
    probes = []

    def model(request):
        if request.method == "GET":
            assert request.url.path == "/v1/models"
            probes.append(request)
            if isinstance(probe, Exception):
                raise probe
            return httpx.Response(probe, text="x")
        calls.append(json.loads(request.content))
        step = judge(len(calls))
        if isinstance(step, Exception):
            raise step
        if isinstance(step, int):
            return httpx.Response(step, text="boom")
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"relevant": step})}}]}
        )

    monkeypatch.setattr(discovery.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(model), **kw))
    result = discovery.discover(GAP, catalog(), client=page_client, state_dir=tmp_path)
    result.probes = probes
    return result, calls


def test_all_judgments_failing_at_the_connection_level_is_an_outage(monkeypatch, tmp_path):
    result, calls = run_discovery(
        monkeypatch, tmp_path, lambda n: httpx.ConnectError("refused"), probe=httpx.ConnectError("refused")
    )
    assert result.status == "unresolved" and not result.seeds
    assert len(calls) >= 2
    assert result.outage_failures == len(calls) and result.answered_judgments == 0
    assert result.outage is True


def test_a_not_relevant_verdict_among_failures_is_a_real_outcome(monkeypatch, tmp_path):
    result, _ = run_discovery(
        monkeypatch,
        tmp_path,
        lambda n: False if n == 1 else httpx.ConnectError("refused"),
        probe=httpx.ConnectError("refused"),
    )
    assert result.status == "unresolved"
    assert result.outage_failures >= 1 and result.answered_judgments == 1
    assert result.outage is False


def test_http_503_is_a_server_answering_not_an_outage(monkeypatch, tmp_path):
    result, calls = run_discovery(monkeypatch, tmp_path, lambda n: 503)
    assert result.status == "unresolved" and len(calls) >= 2
    assert result.outage_failures == 0 and result.answered_judgments == len(calls)
    assert result.outage is False


def test_timeouts_and_dropped_connections_count_as_outage_when_the_probe_fails(monkeypatch, tmp_path):
    steps = [httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("dropped"), httpx.ConnectTimeout("t")]
    result, _ = run_discovery(monkeypatch, tmp_path, lambda n: steps[(n - 1) % 3], probe=httpx.ConnectError("refused"))
    assert result.outage is True and len(result.probes) == result.outage_failures


@pytest.mark.parametrize("probe", [200, 401, 404])
def test_a_timeout_from_an_endpoint_that_still_answers_is_not_an_outage(monkeypatch, tmp_path, probe):
    result, calls = run_discovery(monkeypatch, tmp_path, lambda n: httpx.ReadTimeout("slow"), probe=probe)
    assert result.status == "unresolved" and len(calls) >= 2
    assert result.outage_failures == 0 and result.answered_judgments == len(calls)
    assert result.outage is False


def test_outage_flag_never_applies_to_a_result_with_seeds():
    assert DiscoveryResult("g", "discovered", seeds=[object()], outage_failures=2).outage is False
    assert DiscoveryResult("g", "unresolved").outage is False


# -- gap worker ------------------------------------------------------------------ #


def _public_gap(ledger, attempts=2):
    """A public need already two research attempts deep, so the 1 h / 6 h ladder
    is distinguishable from the short outage wait."""
    row, _ = ledger.submit_event(
        need_class="missing_coverage", subject="widget", representative_question="docs for widget: authentication"
    )
    ledger.update(row.gap_id, lambda r: setattr(r, "research_attempts", attempts))
    return row.gap_id


def _reason(ledger, gap_id) -> str:
    """The reason on the gap's latest transition event."""
    events = [json.loads(line) for line in ledger.events_path(gap_id).read_text().splitlines()]
    return [e for e in events if e.get("kind") == "transition"][-1]["reason"]


def _wait_s(row) -> float:
    return (datetime.fromisoformat(row.backoff_until) - datetime.now(UTC)).total_seconds()


def test_outage_unresolved_waits_five_minutes_and_costs_no_attempt(env, monkeypatch):
    _, ledger, _ = env
    gap_id = _public_gap(ledger)
    monkeypatch.setattr(
        gap_worker,
        "discover",
        lambda record, catalog, **kw: DiscoveryResult(
            record["gap_id"], "unresolved", outage_failures=3, notes=["candidate relevance unresolved"]
        ),
    )
    gap_worker.run_cycle(ledger, budget_s=600)
    row = ledger.load(gap_id)
    assert row.status == "retry_wait"
    assert row.research_attempts == 2
    assert _reason(ledger, gap_id) == "discovery paused: inference endpoint unreachable"
    assert 240 < _wait_s(row) <= 300


def test_repeated_outages_never_climb_the_backoff_ladder(env, monkeypatch):
    _, ledger, _ = env
    gap_id = _public_gap(ledger)
    monkeypatch.setattr(
        gap_worker,
        "discover",
        lambda record, catalog, **kw: DiscoveryResult(record["gap_id"], "unresolved", outage_failures=1),
    )
    for _ in range(3):
        ledger.update(gap_id, lambda r: setattr(r, "backoff_until", "2000-01-01T00:00:00+00:00"))
        gap_worker.run_cycle(ledger, budget_s=600)
    row = ledger.load(gap_id)
    assert row.research_attempts == 2 and 240 < _wait_s(row) <= 300


@pytest.mark.parametrize(
    "result_kwargs",
    [
        dict(outage_failures=2, answered_judgments=1),  # mixed: a real "not relevant" verdict
        dict(answered_judgments=3),  # HTTP 503s and verdicts only
        dict(),  # nothing judged at all
    ],
)
def test_non_outage_unresolved_keeps_todays_penalty(env, monkeypatch, result_kwargs):
    _, ledger, _ = env
    gap_id = _public_gap(ledger)
    monkeypatch.setattr(
        gap_worker,
        "discover",
        lambda record, catalog, **kw: DiscoveryResult(
            record["gap_id"], "unresolved", notes=["candidate relevance unresolved"], **result_kwargs
        ),
    )
    gap_worker.run_cycle(ledger, budget_s=600)
    row = ledger.load(gap_id)
    assert row.status == "retry_wait" and row.research_attempts == 3
    assert _reason(ledger, gap_id) == "candidate relevance unresolved"
    assert abs(_wait_s(row) - ledger.backoff_state(3)) < 60
    assert _wait_s(row) > 600
