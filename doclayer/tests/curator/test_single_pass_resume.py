"""Single-pass resume and per-page call caps: a saved draft reply is never
re-asked, JSON re-asks count toward the page cap, job limits stop retryably."""

from __future__ import annotations

import asyncio
import json

import httpx
from conftest import HttpFixture, run_git
from test_pipeline import LONG_BODY, git_show
from test_single_pass_pipeline import (
    CLAIM_A,
    CLAIM_B,
    TARGET,
    URL,
    draft_reply,
    extract_reply,
    page_calls,
    push_canonical_edit,
    sp_job,
    tasks,
    two_claims,
)

from doclayer.curator import single_pass
from doclayer.curator.forgejo import ForgejoError
from doclayer.curator.ops import handle
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.publish import Publisher

OTHER = "wiki/other-page.md"
CLAIM_C = "Other content now covers routing tables for every region."
PAGES = {
    URL: (
        200,
        (
            "<html><head><title>Model X</title></head><body><article><h1>Model X</h1>"
            f"<p>{LONG_BODY}</p><p>{CLAIM_C}</p><p>{CLAIM_B}</p></article></body></html>"
        ),
    )
}


class FlakyPublisher(Publisher):
    """Fails the next `failures` publications the way a Forgejo outage does."""

    def __init__(self, *args, failures: int = 1, **kwargs):
        super().__init__(*args, **kwargs)
        self.failures = failures

    def publish_changes(self, *args, **kwargs):
        if self.failures:
            self.failures -= 1
            raise ForgejoError("forgejo POST pulls: HTTP 502 (flaky)")
        return super().publish_changes(*args, **kwargs)


def run(cfg, store, qwen, state, publisher):
    pipeline = Pipeline(
        cfg,
        store,
        fetch_transport=HttpFixture(dict(PAGES)).transport(),
        inference_transport=qwen.transport(),
        publisher=publisher,
    )
    return asyncio.run(pipeline.run(state))


def resume(cfg, store, state, **payload):
    store.save(state)
    assert handle("resume", {"job_id": state.job_id, **payload}, cfg, store)["status"] == "queued"
    return store.load(state.job_id)


def two_pages(qwen, store, checkout):
    """Claim A goes to the existing model page, claim C to the other page."""
    reply = extract_reply((CLAIM_A, "128,000-token maximum output"), (CLAIM_C, "routing tables for every region"))
    qwen.overrides["extract"] = [reply] * 6
    return sp_job(store, checkout, targets=(TARGET, OTHER))


def test_resume_reuses_saved_draft_reply(cfg, store, qwen, checkout, origin, forgejo):
    state = run(cfg, store, qwen, sp_job(store, checkout), FlakyPublisher(cfg, store, forgejo=forgejo))
    assert (state.status, state.stage) == ("retryable", "publish"), state.error
    assert tasks(qwen).count("sp_draft") == 1 and tasks(qwen).count("sp_ground") == 1
    # A crash after the reply was saved, before the page was marked drafted.
    (change,) = state.changes
    change.status, change.draft_body, state.stage = "pending", "", "draft"
    state = run(cfg, store, qwen, resume(cfg, store, state), Publisher(cfg, store, forgejo=forgejo))
    assert state.status == "published", state.summary or state.error
    assert tasks(qwen).count("sp_draft") == 1 and tasks(qwen).count("sp_ground") == 1
    assert page_calls(state) == {TARGET: 2}
    assert CLAIM_A in git_show(origin, f"main:{TARGET}")


def test_page_cap_counts_json_repair(cfg, store, qwen, checkout, origin, forgejo):
    """The existing page's draft needs a JSON re-ask (2 calls) and its grounding
    reply is unparseable (call 3), so the grounding re-ask meets the cap of 3."""
    cfg.single_pass.max_calls_per_page = 3  # the scenario is sized for this cap
    state = two_pages(qwen, store, checkout)
    qwen.overrides["sp_draft"] = ["this is not JSON", lambda user: draft_reply(user)]
    qwen.overrides["sp_ground"] = ["still not JSON"]
    state = run(cfg, store, qwen, state, Publisher(cfg, store, forgejo=forgejo))
    existing, other = state.changes
    assert existing.path == TARGET and existing.status == "failed"
    assert page_calls(state)[TARGET] == 3
    assert any(
        gap.startswith(f"{TARGET}: grounding chunk 1: unit dropped") and "page call cap" in gap
        for gap in state.coverage_gaps
    ), state.coverage_gaps
    assert other.status == "published" and state.status == "partial"
    assert CLAIM_C in git_show(origin, f"main:{OTHER}") and CLAIM_A not in git_show(origin, f"main:{TARGET}")
    assert all(calls <= cfg.single_pass.max_calls_per_page for calls in page_calls(state).values())


def test_budget_stop_is_retryable_and_resumes(cfg, store, qwen, checkout, origin, forgejo, monkeypatch):
    original = single_pass.STAGE_HANDLERS["place"]

    async def place_then_allow_one_call(p, state):
        result = await original(p, state)
        state.limits["max_model_calls"] = state.usage.model_calls + 1  # the first page's draft only
        return result

    monkeypatch.setitem(single_pass.STAGE_HANDLERS, "place", place_then_allow_one_call)
    state = run(cfg, store, qwen, two_pages(qwen, store, checkout), Publisher(cfg, store, forgejo=forgejo))
    assert (state.status, state.stage, state.error) == ("retryable", "draft", "limit reached: model_calls")
    assert [change.status for change in state.changes] == ["drafted", "pending"]
    assert page_calls(state) == {TARGET: 1, OTHER: 0}

    monkeypatch.setitem(single_pass.STAGE_HANDLERS, "place", original)
    state = run(
        cfg,
        store,
        qwen,
        resume(cfg, store, state, extend={"max_model_calls": 10}),
        Publisher(cfg, store, forgejo=forgejo),
    )
    assert state.status == "published", state.summary or state.error
    assert tasks(qwen).count("sp_draft") == 2
    assert page_calls(state) == {TARGET: 2, OTHER: 2}
    assert CLAIM_A in git_show(origin, f"main:{TARGET}") and CLAIM_C in git_show(origin, f"main:{OTHER}")


def test_binding_change_discards_saved_reply(cfg, store, qwen, checkout, origin, forgejo, tmp_path):
    cfg.single_pass.max_calls_per_page = 3  # the scenario is sized for this cap
    state = run(cfg, store, qwen, sp_job(store, checkout), FlakyPublisher(cfg, store, forgejo=forgejo))
    assert state.status == "retryable" and page_calls(state) == {TARGET: 2}
    old_binding = state.evidence_selection["single_pass"]["pages"][TARGET]["binding"]
    claims = list(state.changes[0].claim_ids)
    # The page is redrafted against a newer canonical base: its blob changed.
    push_canonical_edit(tmp_path, origin, TARGET, "Known safety notes.", "Known safety notes, revised.")
    run_git(checkout, "fetch", "-q", "origin")
    (change,) = state.changes
    change.status, change.base_commit, state.stage = "pending", run_git(checkout, "rev-parse", "origin/main"), "draft"
    state = run(cfg, store, qwen, resume(cfg, store, state), Publisher(cfg, store, forgejo=forgejo))
    row = state.evidence_selection["single_pass"]["pages"][TARGET]
    assert row["binding"] != old_binding and tasks(qwen).count("sp_draft") == 2
    # The new draft took the page's last call, so grounding had none left.
    assert row["calls"] == 3 and tasks(qwen).count("sp_ground") == 1
    assert change.path == state.changes[0].path and state.changes[0].status == "failed"
    assert any("exceed the page call cap" in gap for gap in state.coverage_gaps)

    # With the cap already spent, a changed binding makes no call at all.
    (change,) = state.changes
    change.status, change.claim_ids, state.stage = "pending", claims, "draft"
    row["binding"] = "stale"
    state = run(cfg, store, qwen, resume(cfg, store, state), Publisher(cfg, store, forgejo=forgejo))
    assert tasks(qwen).count("sp_draft") == 2 and page_calls(state) == {TARGET: 3}
    assert state.changes[0].status == "failed"
    assert any(
        gap.startswith(f"{TARGET}: single-pass draft: unit dropped") and "page call cap" in gap
        for gap in state.coverage_gaps
    )


def test_cancel_between_pages(cfg, store, qwen, checkout, origin, forgejo):
    state = two_pages(qwen, store, checkout)

    def reply_then_cancel(user):
        handle("cancel", {"job_id": state.job_id}, cfg, store)
        return draft_reply(user)

    qwen.overrides["sp_draft"] = [reply_then_cancel]
    state = run(cfg, store, qwen, state, Publisher(cfg, store, forgejo=forgejo))
    assert state.status == "cancelled"
    assert [change.status for change in state.changes] == ["drafted", "pending"]
    assert tasks(qwen).count("sp_draft") == 1 and not forgejo.prs
    assert page_calls(state) == {TARGET: 1}


def test_correction_recorded_after_validation_is_honoured_at_publish(cfg, store, qwen, checkout, origin, forgejo):
    """A publish outage, then an operator correction of
    claim B, then a resume. B never lands, and no model call is made."""
    two_claims(qwen)
    state = run(cfg, store, qwen, sp_job(store, checkout), FlakyPublisher(cfg, store, forgejo=forgejo))
    assert (state.status, state.stage) == ("retryable", "publish"), state.error
    assert state.changes[0].status == "validated" and CLAIM_B in state.changes[0].draft_body
    calls = len(qwen.calls)
    store.add_correction({"page": TARGET, "claim": CLAIM_B, "instruction": "the refusal rate is wrong; remove it"})
    state = run(cfg, store, qwen, resume(cfg, store, state), Publisher(cfg, store, forgejo=forgejo))
    assert state.status == "published", state.summary or state.error
    landed = git_show(origin, f"main:{TARGET}")
    assert CLAIM_A in landed and CLAIM_B not in landed
    assert len(qwen.calls) == calls
    assert any("operator correction" in gap and "after validation" in gap for gap in state.coverage_gaps)


def test_late_correction_of_the_only_contribution_skips_the_page(cfg, store, qwen, checkout, origin, forgejo):
    state = run(cfg, store, qwen, sp_job(store, checkout), FlakyPublisher(cfg, store, forgejo=forgejo))
    store.add_correction({"page": TARGET, "claim": CLAIM_A, "instruction": "wrong; remove it"})
    state = run(cfg, store, qwen, resume(cfg, store, state), Publisher(cfg, store, forgejo=forgejo))
    assert state.changes[0].status == "skipped" and len(forgejo.prs) == 0
    assert CLAIM_A not in git_show(origin, f"main:{TARGET}")


def test_inference_outage_refunds_page_calls_and_stops_retryable(cfg, store, qwen, checkout, origin, forgejo):
    cfg.inference.retries = 1  # two HTTP attempts, both unanswered
    down = {"sp_draft": True}
    inner = qwen.transport()

    def handler(request):
        if request.method == "GET":  # reachability probe: the server answers, only the POST drops
            return httpx.Response(404)
        if down["sp_draft"] and qwen._task_for(json.loads(request.content)["messages"][0]["content"]) == "sp_draft":
            raise httpx.ConnectError("connection refused")
        return inner.handle_request(request)

    pipeline = Pipeline(
        cfg,
        store,
        fetch_transport=HttpFixture(dict(PAGES)).transport(),
        inference_transport=httpx.MockTransport(handler),
        publisher=Publisher(cfg, store, forgejo=forgejo),
    )
    state = asyncio.run(pipeline.run(sp_job(store, checkout)))
    assert state.status == "retryable" and state.stage == "draft" and "inference" in state.error
    assert page_calls(state) == {TARGET: 0}
    assert state.changes[0].status == "pending"
    down["sp_draft"] = False
    state = run(cfg, store, qwen, resume(cfg, store, state), Publisher(cfg, store, forgejo=forgejo))
    assert state.status == "published", state.summary or state.error
    assert page_calls(state) == {TARGET: 2}


def test_outage_during_grounding_after_a_draft_stops_retryable_and_resume_publishes(
    cfg, store, qwen, checkout, origin, forgejo
):
    cfg.inference.retries = 2  # three attempts: with the draft's call they would exceed the page cap of 3
    down = {"sp_ground": True}
    inner = qwen.transport()

    def handler(request):
        task = qwen._task_for(json.loads(request.content)["messages"][0]["content"])
        if down.get(task):
            return httpx.Response(503, text="unavailable")
        return inner.handle_request(request)

    pipeline = Pipeline(
        cfg,
        store,
        fetch_transport=HttpFixture(dict(PAGES)).transport(),
        inference_transport=httpx.MockTransport(handler),
        publisher=Publisher(cfg, store, forgejo=forgejo),
    )
    state = asyncio.run(pipeline.run(sp_job(store, checkout)))
    assert state.status == "retryable" and state.stage == "validate", (state.status, state.error)
    assert page_calls(state) == {TARGET: 1} and state.changes[0].status == "drafted"
    down["sp_ground"] = False
    state = run(cfg, store, qwen, resume(cfg, store, state), Publisher(cfg, store, forgejo=forgejo))
    assert state.status == "published", state.summary or state.error
    assert page_calls(state) == {TARGET: 2}


def test_outage_during_placement_refunds_and_resumes(cfg, store, qwen, checkout, origin, forgejo):
    cfg.inference.retries = 1
    down = {"plan_pages": True}
    inner = qwen.transport()

    def handler(request):
        if request.method == "GET":  # reachability probe: the server answers, only the POST drops
            return httpx.Response(404)
        if down.get(qwen._task_for(json.loads(request.content)["messages"][0]["content"])):
            raise httpx.ConnectError("refused")
        return inner.handle_request(request)

    pipeline = Pipeline(
        cfg,
        store,
        fetch_transport=HttpFixture(dict(PAGES)).transport(),
        inference_transport=httpx.MockTransport(handler),
        publisher=Publisher(cfg, store, forgejo=forgejo),
    )
    state = asyncio.run(pipeline.run(sp_job(store, checkout, targets=())))
    assert state.status == "retryable" and state.stage == "place"
    assert state.evidence_selection["single_pass"]["place"]["calls"] == 0
    down["plan_pages"] = False
    state = run(cfg, store, qwen, resume(cfg, store, state), Publisher(cfg, store, forgejo=forgejo))
    assert state.status == "published", state.summary or state.error


def test_late_correction_differing_only_by_case_drops_just_that_contribution(
    cfg, store, qwen, checkout, origin, forgejo
):
    two_claims(qwen)
    state = run(cfg, store, qwen, sp_job(store, checkout), FlakyPublisher(cfg, store, forgejo=forgejo))
    store.add_correction({"page": TARGET, "claim": CLAIM_B.upper(), "instruction": "wrong; remove it"})
    state = run(cfg, store, qwen, resume(cfg, store, state), Publisher(cfg, store, forgejo=forgejo))
    assert state.status == "published", (state.changes[0].status, state.changes[0].error)
    landed = git_show(origin, f"main:{TARGET}")
    assert CLAIM_A in landed and CLAIM_B not in landed


def test_unset_forgejo_url_fails_the_job_without_retry(cfg, store, qwen, checkout, origin):
    """An empty publish.forgejo_url is a configuration error: the job ends `failed`, not `retryable`."""
    cfg.publish.forgejo_url = ""
    state = run(cfg, store, qwen, sp_job(store, checkout), Publisher(cfg, store))
    assert (state.status, state.stage) == ("failed", "publish"), state.error
    assert state.error == "publication: publish.forgejo_url is not configured"
