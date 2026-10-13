"""Proven response stops free the singleton without pretending the need was answered."""

import asyncio

import httpx
import pytest
from test_resource_exhausted_retirement import job, request, setup

from doclayer.curator.model import ModelError
from doclayer.curator.ops import OpsError, handle
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.stopped_task import record_task_stop, task_stop
from doclayer.gap_budget import today
from doclayer.gap_worker import digest


def stopped(tmp_path, cfg, store, monkeypatch, *, finish="stop", source_schema=False):
    ledger, ops, dispatcher, gap_id, job_id = setup(tmp_path, cfg, store)
    state = store.load(job_id)
    state.status, state.stage, state.error = "queued", "draft", None
    state.usage.model_calls = 0
    store.save(state)
    attempts = []

    def respond(req):
        attempts.append(req)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "{}" if source_schema else "not JSON"},
                        "finish_reason": finish,
                    }
                ]
            },
        )

    pipeline = Pipeline(cfg, store, inference_transport=httpx.MockTransport(respond))

    async def stage(state):
        if source_schema:
            from doclayer.curator import degrade, editorial
            from doclayer.curator.state import PageChange

            # Inside a job a rejected review chunk is dropped as a current gap
            # (degrade.py); with no gap list to record on it still stops typed.
            with degrade.recording(None):
                await editorial.review(
                    pipeline._model(state),
                    "Device",
                    PageChange("g", "wiki/device.md", "create", draft_body="# Device\nAn unsupported statement.\n"),
                    [],
                    [],
                    char_budget=12000,
                )
        else:
            await pipeline._model(state).chat_json("Typed schema", "Whole original source")

    monkeypatch.setattr(pipeline, "_run_stage", stage)
    result = asyncio.run(pipeline.run(state))
    assert result.status == "retryable" and result.usage.model_calls == len(attempts) < 50
    assert task_stop(result)["category"] == "model_response_unresolved"
    return ledger, ops, dispatcher, gap_id, job_id, attempts


@pytest.mark.parametrize("finish", ["stop", "length"])
def test_actual_charged_schema_or_output_stop_releases_unrelated_work(
    tmp_path,
    cfg,
    store,
    monkeypatch,
    finish,
):
    ledger, ops, d, gap_id, job_id, attempts = stopped(tmp_path, cfg, store, monkeypatch, finish=finish)
    before = store.state_path(job_id).read_bytes()
    reservation = ledger.load(gap_id).submission["reservation"]
    assert d.retire_exhausted(gap_id, job(ops, job_id)).action == "model_response_unresolved"
    assert store.state_path(job_id).read_bytes() == before
    record = ledger.load(gap_id)
    assert record.status == "failed" and record.submission["reservation"] == reservation
    assert record.submission["retirement_receipt"]["model_calls"] == len(attempts)
    assert ledger.summary()["outstanding"] == ledger.summary()["model_response_unresolved"] == 1
    assert "unresolved" in digest(ledger.summary())
    assert d.active() is None and d.dispatch(gap_id).action == "skipped"
    other, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="other",
        representative_question="How does Other work?",
        facets=["operation"],
        source_candidates=[{"url": "https://example.org/other", "authority": "official"}],
    )
    ledger.transition(other.gap_id, "ready_for_curation")
    next_job = d.dispatch(other.gap_id).job_id
    assert next_job is not None and next_job != job_id
    assert store.load(next_job).limits["max_model_calls"] == 50
    assert reservation["calls"] == 50
    assert ledger.load(gap_id).submission["reservation"] == reservation
    assert store.state_path(job_id).read_bytes() == before
    assert d.day_ledger.load(today())["reserved_calls"] == 100


def test_response_stop_lost_reply_replays_receipt_without_extending_remaining_capacity(
    tmp_path,
    cfg,
    store,
    monkeypatch,
):
    ledger, ops, d, gap_id, job_id, _ = stopped(tmp_path, cfg, store, monkeypatch)
    before = store.state_path(job_id).read_bytes()
    ops.lose = True
    assert d.retire_exhausted(gap_id, job(ops, job_id)).action == "uncertain"
    receipt = (store.job_dir(job_id) / "allowance-retirement.json").read_bytes()
    with pytest.raises(OpsError, match="operator readmission"):
        handle("resume", {"job_id": job_id}, cfg, store)
    assert d.retire_exhausted(gap_id, job(ops, job_id)).action == "model_response_unresolved"
    assert store.state_path(job_id).read_bytes() == before
    assert (store.job_dir(job_id) / "allowance-retirement.json").read_bytes() == receipt
    assert len(store.list_ids()) == 1 and d.active() is None


def test_actual_bounded_source_schema_stop_is_unknown_not_negative(
    tmp_path,
    cfg,
    store,
    monkeypatch,
):
    ledger, ops, d, gap_id, job_id, attempts = stopped(tmp_path, cfg, store, monkeypatch, source_schema=True)
    assert len(attempts) == 2
    state = store.load(job_id)
    assert task_stop(state)["task_operation"] == "article-review"
    assert state.inference_diagnostics[-1]["operation"] == "article-review"
    assert not state.publication.commit_sha and state.usage.model_calls == 2
    before = store.state_path(job_id).read_bytes()
    assert d.retire_exhausted(gap_id, job(ops, job_id)).action == "model_response_unresolved"
    assert store.state_path(job_id).read_bytes() == before
    assert ledger.load(gap_id).status != "resolved" and d.active() is None


@pytest.mark.parametrize(
    "change",
    ["claimed", "cancel", "commit", "push", "pr", "merge", "publish-stage", "stale"],
)
def test_response_stop_owner_refuses_races_and_publication_uncertainty(
    tmp_path,
    cfg,
    store,
    monkeypatch,
    change,
):
    _, ops, d, _, job_id, _ = stopped(tmp_path, cfg, store, monkeypatch)
    observed = job(ops, job_id)
    payload = {**request(d, observed), "stop_category": "model_response_unresolved"}
    state = store.load(job_id)
    claim = None
    if change == "claimed":
        claim = store.job_claim(job_id)
        assert claim.acquire(blocking=False)
    elif change == "cancel":
        state.cancel_requested = True
    elif change == "commit":
        state.publication.commit_sha = "a" * 40
    elif change == "push":
        state.publication.pushed = True
    elif change == "pr":
        state.publication.pr_number = 7
    elif change == "merge":
        state.publication.merged = True
    elif change == "publish-stage":
        state.stage = "publish"
    elif change == "stale":
        state.usage.model_calls += 1
    if change != "claimed":
        store.save(state)
    before = store.state_path(job_id).read_bytes()
    try:
        with pytest.raises(OpsError):
            handle("retire_allowance", payload, cfg, store)
    finally:
        if claim:
            claim.release()
    assert store.state_path(job_id).read_bytes() == before
    assert not (store.job_dir(job_id) / "allowance-retirement.json").exists()
    assert d.active()["job_id"] == job_id


@pytest.mark.parametrize("kind", ["legacy", "changed-error", "outage", "http", "published-stage"])
def test_no_legacy_inference_or_outage_marker(tmp_path, cfg, store, kind):
    _, ops, d, _, job_id = setup(tmp_path, cfg, store)
    state = store.load(job_id)
    state.stage, state.error, state.usage.model_calls = (
        "draft",
        "inference: invalid schema",
        2,
    )
    if kind == "changed-error":
        record_task_stop(state, kind="model_task", operation="json-parse")
        state.error = "inference: changed stop"
    elif kind in ("outage", "http"):
        state.error = "inference: inference HTTP 503 (transient)"
        record_task_stop(state, kind="model_task", operation="chat")
    elif kind == "published-stage":
        state.stage = "publish"
        record_task_stop(state, kind="model_task", operation="json-parse")
    store.save(state)
    assert job(ops, job_id)["automatic_stop"] is None
    assert d.active()["job_id"] == job_id


def test_new_execution_cannot_reuse_prior_stop_with_identical_error(
    tmp_path,
    cfg,
    store,
    monkeypatch,
):
    _, _, _, _, job_id = setup(tmp_path, cfg, store)
    state = store.load(job_id)
    state.stage, state.error = "draft", "inference: invalid schema"
    record_task_stop(state, kind="model_task", operation="json-parse")
    state.inference_diagnostics.insert(0, {"operation": "preserved-evidence"})
    state.status = "queued"
    pipeline = Pipeline(cfg, store)

    async def stage(state):
        raise ModelError("invalid schema", transient=True)

    monkeypatch.setattr(pipeline, "_run_stage", stage)
    result = asyncio.run(pipeline.run(state))
    assert result.status == "retryable" and result.error == "inference: invalid schema"
    assert task_stop(result) is None
    assert {"operation": "preserved-evidence"} in result.inference_diagnostics


@pytest.mark.parametrize(
    "operation",
    [
        "placement-json",
        "bounded-edit",
        "bounded-section-routing",
        "source-claim-extraction",
        "source-quotation-repair",
        "source-semantic-recheck",
        "source-ancestor-constraints",
    ],
)
def test_known_bounded_prepublication_validators_preserve_diagnostics_and_retire(
    tmp_path,
    cfg,
    store,
    monkeypatch,
    operation,
):
    ledger, ops, dispatcher, gap_id, job_id = setup(tmp_path, cfg, store)
    state = store.load(job_id)
    state.status, state.stage, state.error = "queued", "draft", None
    diagnostic = {
        "operation": operation,
        "attempts": 3,
        "issues": ["missing required field"],
    }
    pipeline = Pipeline(cfg, store)

    async def stage(state):
        raise ModelError("bounded task schema unresolved", transient=True, diagnostic=diagnostic)

    monkeypatch.setattr(pipeline, "_run_stage", stage)
    result = asyncio.run(pipeline.run(state))
    assert result.inference_diagnostics[-1] == diagnostic
    assert task_stop(result)["task_operation"] == operation
    before = store.state_path(job_id).read_bytes()
    assert dispatcher.retire_exhausted(gap_id, job(ops, job_id)).action == "model_response_unresolved"
    assert store.state_path(job_id).read_bytes() == before
    assert ledger.load(gap_id).status != "resolved"
