"""Knowledge-limiting caps removed (assumption audit, 2026-10-10): every
supported claim is placed, a bad disposition is repaired instead of dropping
its section, and the concurrency/retry knobs take effect."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from test_pipeline import LONG_BODY, git_show, run_pipeline
from test_single_pass_pipeline import TARGET, URL, extract_reply, sp_job, tasks

from doclayer.curator import intake
from doclayer.curator import pipeline as pipeline_module
from doclayer.curator.model import ModelClient
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import JobState, Usage

FACTS = [f"Feature {n} of the model accepts up to {1000 + n} requests per minute." for n in range(40)]
PAGE40 = {
    URL: (
        200,
        (
            "<html><head><title>Model X</title></head><body><article><h1>Model X</h1>"
            f"<p>{LONG_BODY}</p>" + "".join(f"<p>{fact}</p>" for fact in FACTS) + "</article></body></html>"
        ),
    )
}


def claims(qwen, facts=FACTS) -> None:
    reply = extract_reply(*[(fact, fact) for fact in facts])
    qwen.overrides["extract"] = [reply] * 6


def test_page_with_forty_supported_claims_places_all_forty(cfg, store, qwen, checkout, origin, forgejo):
    """No per-page claim cap: all 40 are placed. Their support excerpts do not
    fit one draft request, so the rest are drafted in a continuation request and
    each request's material is grounded as its own contribution."""
    claims(qwen)
    state = run_pipeline(cfg, store, qwen, PAGE40, sp_job(store, checkout), forgejo)
    assert state.status == "published", state.summary or state.error
    (change,) = state.changes
    assert len(change.claim_ids) == 40
    assert not [
        gap for gap in state.coverage_gaps if "claim cap" in gap or "unplaced" in gap or "not integrated" in gap
    ]
    assert tasks(qwen).count("sp_draft") >= 2
    landed = git_show(origin, f"main:{TARGET}")
    assert all(landed.count(fact) == 1 for fact in FACTS)


def test_one_invalid_disposition_is_repaired_and_every_claim_lands(cfg, store, qwen, checkout, origin, forgejo):
    facts = FACTS[:10]  # all fit the first request, so the second call is only the repair
    claims(qwen, facts)
    bad = {}

    def first_reply(user):
        # The default reply, with one claim's disposition given an invalid status.
        reply = json.loads(qwen.default_reply("sp_draft", user))
        entry = next(entry for entry in reply["sections"] if len(entry["claim_dispositions"]) > 1)
        entry["claim_dispositions"][0]["status"] = "added"
        bad["id"] = entry["claim_dispositions"][0]["id"]
        return reply

    def second_reply(user):
        bad["repair"] = json.JSONDecoder().raw_decode(user.split("\n", 1)[1])[0]
        return json.loads(qwen.default_reply("sp_draft", user))

    qwen.overrides["sp_draft"] = [first_reply, second_reply]
    state = run_pipeline(cfg, store, qwen, PAGE40, sp_job(store, checkout), forgejo)
    assert state.status == "published", state.summary or state.error
    # One draft call, then one repair call that shows the exact error and asks only for the failed claim.
    assert tasks(qwen).count("sp_draft") == 2
    repair = bad["repair"]
    assert [row["id"] for row in repair["evidence"]] == [bad["id"]]
    (section,) = repair["repair"]["sections"]
    assert section["claim_ids"] == [bad["id"]] and "accepted_entry" in section
    assert any("integrated" in error and "already_present" in error for error in section["errors"])
    # The section's valid claims were never dropped, and the repaired claim landed too.
    assert not [gap for gap in state.coverage_gaps if "not integrated" in gap or "unit dropped" in gap]
    (change,) = state.changes
    assert len(change.claim_ids) == 10
    landed = git_show(origin, f"main:{TARGET}")
    assert all(landed.count(fact) == 1 for fact in facts)


def test_disposition_faults_downgrade_one_claim_not_the_section():
    """editing: a missing, duplicated or invalid-status disposition makes only
    that claim unhandled under allow_partial; the other claims are applied."""
    from doclayer.curator import editing

    unit = editing.page_units("# Page\n\nIntro.\n")[0]
    sentences = {f"c{n}": f"Fact {n} holds for build {n}." for n in range(3)}
    base = {
        "section_id": unit.id,
        "edits": [],
        "append": "\n\n".join(sentences.values()),
        "handled_claim_ids": list(sentences),
    }
    good = [{"id": identity, "status": "integrated", "anchor": text} for identity, text in sentences.items()]
    for dispositions, bad in (
        (good[1:], "c0"),
        (good + good[:1], "c0"),
        ([{**good[0], "status": "added"}, *good[1:]], "c0"),
    ):
        result = {**base, "claim_dispositions": [dict(item) for item in dispositions]}
        text = editing.apply_edits(unit, result, set(sentences), max_output_chars=100000, allow_partial=True)
        assert result["handled_claim_ids"] == ["c1", "c2"]
        assert sentences["c1"] in text and sentences["c2"] in text and sentences[bad] not in text
        assert [row["id"] for row in result["_downgraded"]] == [bad]
    # Without allow_partial the contract stays strict.
    with pytest.raises(Exception, match="invalid bounded-edit claim dispositions"):
        editing.apply_edits(unit, {**base, "claim_dispositions": good[1:]}, set(sentences), max_output_chars=100000)


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 2, 3])
async def test_inference_concurrency_bounds_requests_in_flight(cfg, concurrency):
    cfg.inference.concurrency = concurrency
    cfg.inference.retries = 0
    inflight = {"now": 0, "max": 0}

    async def handler(request):
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        await asyncio.sleep(0.01)
        inflight["now"] -= 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})

    client = ModelClient(cfg, Usage(), limits={"max_model_calls": 10}, transport=httpx.MockTransport(handler))
    await asyncio.gather(*(client.chat("system", "user") for _ in range(6)))
    assert inflight["max"] == concurrency and client.usage.model_calls == 6


def intake_state(store, urls, **limits) -> JobState:
    state = store.create(JobState(job_id="", idempotency_key="", intent="device procedure"))[0]
    state.stage = "intake"
    state.limits = {"max_active_seconds": 3600, "max_model_calls": 20, **limits}
    state.sources = [intake.make_url_source(url) for url in urls]
    store.save(state)
    return state


PAGE_HTML = "<h1>Device {n}</h1><p>" + "Documented device procedure and configuration. " * 10 + "</p>"


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [1, 2, 4])
async def test_fetch_concurrency_bounds_source_reads_in_flight(cfg, store, width):
    inflight = {"now": 0, "max": 0}

    async def handler(request):
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        await asyncio.sleep(0.01)
        inflight["now"] -= 1
        return httpx.Response(200, headers={"content-type": "text/html"}, text=PAGE_HTML.format(n=request.url.path))

    urls = [f"https://device.example/{n}" for n in range(6)]
    state = intake_state(store, urls, fetch_concurrency=width)
    await Pipeline(cfg, store, fetch_transport=httpx.MockTransport(handler))._stage_intake(state)
    assert inflight["max"] == width
    assert [source.status for source in state.sources] == ["fetched"] * 6  # committed in submission order
    assert state.usage.sources_fetched == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("retries,status", [(0, "failed"), (1, "failed"), (2, "fetched"), (3, "fetched")])
async def test_transient_retries_control_fetch_retries(cfg, store, monkeypatch, retries, status):
    monkeypatch.setattr(pipeline_module, "FETCH_RETRY_BACKOFF_S", 0.0)
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) <= 2:  # two transient failures, then the page
            return httpx.Response(503)
        return httpx.Response(200, headers={"content-type": "text/html"}, text=PAGE_HTML.format(n=1))

    state = intake_state(store, ["https://device.example/flaky"], transient_retries=retries)
    await Pipeline(cfg, store, fetch_transport=httpx.MockTransport(handler))._stage_intake(state)
    assert state.sources[0].status == status
    assert len(requests) == min(retries + 1, 3)


@pytest.mark.asyncio
async def test_permanent_fetch_failure_is_not_retried(cfg, store, monkeypatch):
    monkeypatch.setattr(pipeline_module, "FETCH_RETRY_BACKOFF_S", 0.0)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(404)

    state = intake_state(store, ["https://device.example/missing"], transient_retries=3)
    await Pipeline(cfg, store, fetch_transport=httpx.MockTransport(handler))._stage_intake(state)
    assert state.sources[0].status == "failed" and len(requests) == 1
