"""A gap job that has started publication is never retired or replaced.

Once it has committed (commit, push, PR or merge), the same job is resumed on a
growing backoff (5 min doubling to 6 h, no count cap) while it keeps its
dispatcher slot, visibly: the gap's failure_reason and the digest name it.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_outage_resume import observe, setup_gap

from doclayer.curator.ops import OpsError, handle
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import PageChange
from doclayer.dispatcher import GapDispatcher
from doclayer.gap_worker import _verify_published, digest
from doclayer.gaps import GapLedger

ERROR = "publication: Forgejo unavailable: HTTP 503"


def started(store, job_id, point, *, error=ERROR, **usage):
    state = store.load(job_id)
    state.status, state.stage, state.error = "retryable", "publish", error
    pub = state.publication
    pub.branch = "curator/fixture"
    pub.commit_sha = "c" * 40
    pub.committed_paths = ["wiki/fixture.md"]
    if point in ("push", "pr", "merge"):
        pub.pushed = True
    if point in ("pr", "merge"):
        pub.pr_number = 7
    for key, value in usage.items():
        setattr(state.usage, key, value)
    store.save(state)
    return state


def make_due(ledger, gap_id):
    ledger.update(
        gap_id,
        lambda r: r.submission["publication_resume"].update(
            next_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        ),
    )


def wait_s(ledger, gap_id):
    row = ledger.load(gap_id).submission["publication_resume"]
    return (datetime.fromisoformat(row["next_at"]) - datetime.now(UTC)).total_seconds()


def stop_again(store, job_id, error=ERROR):
    state = store.load(job_id)
    state.status, state.error = "retryable", error
    state.usage.active_seconds += 1  # a new stop generation
    store.save(state)


@pytest.mark.parametrize("point", ["commit", "push", "pr", "merge"])
def test_publication_started_job_is_resumed_on_growing_backoff_never_retired(tmp_path, cfg, store, point):
    ledger, ops, d, gap_id, job_id = setup_gap(tmp_path, cfg, store)
    started(store, job_id, point)
    waits = []
    for _ in range(6):
        notes = []
        _verify_published(ledger, d, notes)
        assert "publication pending" in notes[-1] and "slot retained" in notes[-1]
        waits.append(wait_s(ledger, gap_id))
        make_due(ledger, gap_id)
        _verify_published(ledger, d, notes)
        assert store.load(job_id).status == "queued"  # the same job, resumed
        stop_again(store, job_id)
        d = GapDispatcher(GapLedger(ledger.root), transport=ops)
    # 5 min doubling (no cap reached in six tries), never a count limit
    assert [round(w / 60) for w in waits] == [5, 10, 20, 40, 80, 160]
    assert len([op for op, _ in ops.calls if op == "resume"]) == 6
    assert not any(op == "retire_allowance" for op, _ in ops.calls)
    assert not (store.job_dir(job_id) / "allowance-retirement.json").exists()
    assert len(store.list_ids()) == 1 and d.active()["job_id"] == job_id
    assert ledger.load(gap_id).status == "curating"


def test_backoff_is_capped_at_six_hours(tmp_path, cfg, store):
    ledger, ops, d, gap_id, job_id = setup_gap(tmp_path, cfg, store)
    started(store, job_id, "pr")
    ledger.update(
        gap_id,
        lambda r: r.submission.update(publication_resume={"attempts": 12, "checkpoint": ""}),
    )
    _verify_published(ledger, d, [])
    assert 6 * 3600 - 5 < wait_s(ledger, gap_id) <= 6 * 3600


def test_slot_held_visibly_with_reason_and_digest(tmp_path, cfg, store):
    ledger, ops, d, gap_id, job_id = setup_gap(tmp_path, cfg, store)
    started(store, job_id, "push")
    _verify_published(ledger, d, [])
    row = ledger.load(gap_id)
    assert d.active()["job_id"] == job_id
    assert row.failure_reason.startswith(f"publication pending: {ERROR}")
    assert "next try" in row.failure_reason
    summary = ledger.summary()
    assert summary["publication_pending"] == 1
    assert "1 gap job(s) hold their slot while publication retries" in digest(summary)


@pytest.mark.parametrize(
    "error,usage",
    [
        ("limit reached: active_time", {"active_seconds": 900}),
        ("limit reached: model_calls", {"model_calls": 50}),
        ("worker crash: RuntimeError('boom')", {}),
        ("inference: model did not return parseable JSON", {}),
    ],
)
def test_no_stop_of_a_publication_started_job_retires_it(tmp_path, cfg, store, error, usage):
    ledger, ops, d, gap_id, job_id = setup_gap(tmp_path, cfg, store)
    started(store, job_id, "commit", error=error, **usage)
    _verify_published(ledger, d, [])
    make_due(ledger, gap_id)
    _verify_published(ledger, d, [])
    assert store.load(job_id).status == "queued"
    assert not any(op == "retire_allowance" for op, _ in ops.calls)
    assert d.active()["job_id"] == job_id
    if error == "limit reached: active_time":
        # the resume grants bounded active time so the publication can finish
        assert store.load(job_id).limits["max_active_seconds"] > 900


def test_curator_refuses_to_retire_a_publication_started_job(tmp_path, cfg, store):
    ledger, ops, d, gap_id, job_id = setup_gap(tmp_path, cfg, store)
    started(store, job_id, "commit", error="limit reached: active_time", active_seconds=900)
    active = d.active()
    with pytest.raises(OpsError, match="publication"):
        handle(
            "retire_allowance",
            dict(
                job_id=job_id,
                attempt_id=active["attempt_id"],
                original_grant=active["calls"],
                expected_checkpoint=observe(ops, job_id)["resume_checkpoint"],
                stop_category="stopped_unresolved",
            ),
            cfg,
            store,
        )


def test_the_same_job_is_always_resumed_never_replaced(tmp_path, cfg, store):
    """Invariant: no new attempt for a gap whose job started publication."""
    ledger, ops, d, gap_id, job_id = setup_gap(tmp_path, cfg, store)
    started(store, job_id, "merge")
    for _ in range(8):
        _verify_published(ledger, d, [])
        make_due(ledger, gap_id)
        _verify_published(ledger, d, [])
        stop_again(store, job_id)
        outcome = d.dispatch(gap_id)
        assert outcome.action == "skipped" and outcome.job_id == job_id
        assert ledger.load(gap_id).status == "curating"
    assert len(store.list_ids()) == 1
    assert not any(op == "submit" for op, _ in ops.calls[1:])


def test_unrecorded_merge_is_detected_on_resume_and_finishes_published(tmp_path, cfg, store, monkeypatch):
    from doclayer import verification

    ledger, ops, d, gap_id, job_id = setup_gap(tmp_path, cfg, store)
    state = started(store, job_id, "merge", active_seconds=900, error="limit reached: active_time")
    state.changes = [
        PageChange("c-aaaa0001", "wiki/fixture.md", "create", draft_body="# Fixture\n", status="validated")
    ]
    store.save(state)

    class LandedRemote:
        """The PR merged, but the job never recorded it (lost receipt)."""

        def recover_existing_merge(self, job, changes, persist):
            job.publication.merged = True
            job.publication.merge_commit = "m" * 40
            for change in changes:
                change.status = "published"
            persist()
            return SimpleNamespace(merged=True)

    _verify_published(ledger, d, [])
    make_due(ledger, gap_id)
    _verify_published(ledger, d, [])
    assert store.load(job_id).status == "queued"
    result = asyncio.run(Pipeline(cfg, store, publisher=LandedRemote()).run(store.load(job_id)))
    assert result.status == "published" and result.publication.merged
    monkeypatch.setattr(verification, "verify_gap", lambda *a, **k: SimpleNamespace(outcome="verifying", notes=[]))
    _verify_published(ledger, d, [])
    assert d.active() is None and ledger.load(gap_id).status != "retry_wait"
    assert len(store.list_ids()) == 1
    assert not any(op in ("retire_allowance", "submit") for op, _ in ops.calls[1:])
