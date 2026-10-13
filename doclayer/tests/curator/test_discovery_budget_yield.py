"""Real charged discovery HTTP preserves a durable frontier and later-stage budget."""

import ast
import json

import httpx
import pytest

from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import JobState, SourceRecord


class LinkWire:
    def __init__(self, *, relevant=True):
        self.batches = []
        self.relevant = relevant

    def handler(self, request):
        frame = json.loads(request.content)
        user = frame["messages"][1]["content"]
        assert user.startswith("Batch topic:")
        listing = ast.literal_eval(user.split("Candidate links (JSON): ")[1].split("\n\n")[0])
        assert len(listing) <= 12
        self.batches.append([row["url"] for row in listing])
        reply = {
            "assessments": [
                {"url": row["url"], "relevant": self.relevant, "priority": "normal", "why": "Primary documentation."}
                for row in listing
            ]
        }
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}]}
        )


def setup_frontier(store, pipeline, calls):
    source = SourceRecord(
        "seed",
        "url",
        "Widget primary reference",
        url="https://docs.example.org/widget",
        status="fetched",
        submitted=True,
    )
    state, _ = store.create(
        JobState(
            "",
            f"peer-discovery-{calls}",
            "Widget",
            sources=[source],
            stage="discover",
            status="processing",
            limits={"max_model_calls": calls, "max_sources": 2},
        )
    )
    body = "# Widget\nComplete primary Widget information.\n" + "\n".join(
        f"[Reference {i}](https://docs.example.org/widget/ref-{i})" for i in range(600)
    )
    pipeline._store_extract(state, source, body)
    state.usage.sources_fetched = 1  # primary intake was already acknowledged
    store.save(state)
    return state


@pytest.mark.asyncio
async def test_six_hundred_link_frontier_yields_with_downstream_allowance_and_restart(cfg, store):
    wire = LinkWire()
    fetches = []

    def fetch(request):
        fetches.append(str(request.url))
        return httpx.Response(
            200,
            text="# Widget detail\nComplete scoped documentation facts. " * 20,
            headers={"content-type": "text/markdown"},
        )

    pipeline = Pipeline(
        cfg, store, inference_transport=httpx.MockTransport(wire.handler), fetch_transport=httpx.MockTransport(fetch)
    )
    state = setup_frontier(store, pipeline, 50)
    assert await pipeline._stage_discover(state) == "extract"
    assert state.usage.model_calls == len(wire.batches) <= 5
    assert len(state.sources) == 601
    pending = {s.url for s in state.sources if s.status == "pending"}
    assert pending and len(pending) + sum(s.status != "pending" for s in state.sources[1:]) == 600
    assert any(s.status == "fetched" and s.id != "seed" for s in state.sources)
    assert fetches and len(fetches) == 1
    before_batches = list(wire.batches)
    saved = store.load(state.job_id)
    assert {s.url for s in saved.sources if s.status == "pending"} == pending
    restarted = Pipeline(
        cfg, store, inference_transport=httpx.MockTransport(wire.handler), fetch_transport=httpx.MockTransport(fetch)
    )
    # The two-source cap remains durable. A restart neither rescans existing
    # assessments nor pretends hundreds of unresolved links were reviewed.
    assert await restarted._stage_discover(saved) == "extract"
    assert wire.batches == before_batches
    assert {s.url for s in saved.sources if s.status == "pending"} == pending
    assert saved.usage.model_calls == state.usage.model_calls
    assert saved.effective_coverage == "incomplete"


@pytest.mark.asyncio
@pytest.mark.parametrize("calls, expected", [(50, 5), (200, 20)])
async def test_discovery_allowance_scales_with_actual_job_budget(cfg, store, calls, expected):
    wire = LinkWire(relevant=False)
    pipeline = Pipeline(
        cfg,
        store,
        inference_transport=httpx.MockTransport(wire.handler),
        fetch_transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                text="# Widget detail\nScoped documentation facts. " * 20,
                headers={"content-type": "text/markdown"},
            )
        ),
    )
    state = setup_frontier(store, pipeline, calls)
    assert await pipeline._stage_discover(state) == "extract"
    # The200-call policy permits20 scoring calls versus5 for the50-call
    # job, preserving the rest for source review/publication.
    assert state.usage.model_calls == len(wire.batches) == expected
    assert sum(s.status == "rejected" for s in state.sources) == expected * 12
    assert sum(s.status == "pending" for s in state.sources) == 600 - expected * 12


@pytest.mark.asyncio
async def test_scouting_yield_still_fetches_already_scored_relevant_links(cfg, store):
    wire = LinkWire()
    pipeline = Pipeline(
        cfg,
        store,
        inference_transport=httpx.MockTransport(wire.handler),
        fetch_transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text=f"# Widget detail\nScoped fact number {request.url.path.rsplit('-', 1)[-1]}. " * 20,
                headers={"content-type": "text/markdown"},
            )
        ),
    )
    state = setup_frontier(store, pipeline, 50)
    state.limits["max_sources"] = 10
    state.usage.model_calls = 4  # one scouting batch remains before soft yield
    assert await pipeline._stage_discover(state) == "extract"
    assert len(wire.batches) == 1 and state.usage.model_calls == 5
    # Scored relevant links need no new model decisions. They must not be
    # trapped behind the remaining588 unscored links when scouting yields.
    assert state.usage.sources_fetched == 10
    assert sum(s.status == "fetched" for s in state.sources) == 10
    assert sum(s.status == "pending" for s in state.sources) == 588
    assert sum(s.status == "scored" for s in state.sources) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("first_reply", ["malformed_json", "transient_http"])
async def test_discovery_reserve_guards_each_actual_retry_and_repair_attempt(cfg, store, first_reply):
    requests = []

    def handler(request):
        requests.append(request)
        if first_reply == "transient_http":
            return httpx.Response(503)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "not valid JSON"}, "finish_reason": "stop"}]}
        )

    pipeline = Pipeline(
        cfg,
        store,
        inference_transport=httpx.MockTransport(handler),
        fetch_transport=httpx.MockTransport(lambda _: pytest.fail("no scored link to fetch")),
    )
    state = setup_frontier(store, pipeline, 50)
    state.usage.model_calls = 4
    assert await pipeline._stage_discover(state) == "extract"
    assert len(requests) == 1 and state.usage.model_calls == 5
    assert sum(s.status == "pending" for s in state.sources) == 600
    assert not any(s.status == "rejected" for s in state.sources)
    assert state.effective_coverage == "incomplete"
    saved = store.load(state.job_id)
    restarted = Pipeline(cfg, store, inference_transport=httpx.MockTransport(handler))
    assert await restarted._stage_discover(saved) == "extract"
    assert len(requests) == 1 and saved.usage.model_calls == 5
    assert sum(s.status == "pending" for s in saved.sources) == 600
