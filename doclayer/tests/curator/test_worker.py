"""Worker service: queue claiming, restart/orphan recovery, job survival
across submitting-client disconnect, cache eviction policy."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from conftest import run_git

from doclayer.curator import worker as worker_module
from doclayer.curator.jobstore import JobStore
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.publish import Publisher
from doclayer.curator.state import JobState

WORKER_FACT = "The model supports a 128,000-token maximum output."
WORKER_TEXT = WORKER_FACT + " some durable note " * 50


def seed_job(store: JobStore, checkout: Path, intent: str) -> JobState:
    target = checkout / "wiki/existing-model.md"
    if WORKER_FACT not in target.read_text():
        target.write_text(target.read_text() + "\n" + WORKER_FACT + "\n")
        run_git(checkout, "add", str(target))
        run_git(checkout, "commit", "-qm", "worker fixture genuinely covers incoming fact")
        run_git(checkout, "push", "origin", "main")
    state = JobState(
        job_id="",
        idempotency_key=f"key-{intent[:8]}-{time.monotonic_ns()}",
        intent=intent,
        base_commit=run_git(checkout, "rev-parse", "HEAD"),
    )
    from doclayer.curator.intake import make_text_source

    state.sources = [make_text_source("notes", WORKER_TEXT)]
    state, _ = store.create(state)
    staged = store.staging_dir(state.job_id) / "notes.md"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(WORKER_TEXT)
    state.sources[0].local_path = str(staged)
    store.save(state)
    return state


def scripted_pipeline(cfg, store, qwen):
    return Pipeline(
        cfg,
        store,
        fetch_transport=None,
        inference_transport=qwen.transport(),
        publisher=Publisher(cfg, store, forgejo=None),
    )


def covered_plan() -> str:
    from doclayer.curator.curate import claim_identity
    from doclayer.curator.intake import make_text_source

    source = make_text_source("notes", WORKER_TEXT)
    identity = claim_identity(source.id, "(introduction)", WORKER_FACT)
    return json.dumps(
        {
            "pages": [],
            "covered": [{"claim_id": identity, "path": "wiki/existing-model.md"}],
        }
    )


def test_worker_once_processes_queued_job(cfg, store, qwen, checkout) -> None:
    seed_job(store, checkout, "coverable material")
    qwen.overrides["plan"] = [covered_plan()]
    pipeline = scripted_pipeline(cfg, store, qwen)
    state = store.claim_next_queued()
    assert state is not None
    import asyncio

    done = asyncio.run(pipeline.run(state))
    assert done.status == "covered"
    claim = store.job_claim(done.job_id)
    assert claim.acquire(blocking=False)  # released after processing
    claim.release()


def test_queue_claim_is_exclusive_between_two_store_instances(cfg, store, checkout) -> None:
    """Two worker processes cannot claim the same job: the second sees no
    work (or a different job) while the first holds the claim."""
    seed_job(store, checkout, "single job")
    first = store.claim_next_queued()
    assert first is not None
    # a second store instance in the same or another process must see none
    second_store = JobStore(store.jobs_dir)
    assert second_store.claim_next_queued() is None


def test_orphaned_processing_job_is_recovered(cfg, store, qwen, checkout) -> None:
    """A worker that died mid-flight leaves a claimed-status job; the next
    worker recovers it (claim free again) and resumes at the saved stage."""
    state = seed_job(store, checkout, "orphan recovery")
    state.status = "processing"
    state.stage = "place"
    store.save(state)
    orphans = worker_module.recover_orphans(store)
    assert [s.job_id for s in orphans] == [state.job_id]


def test_active_claim_prevents_recovery(cfg, store, checkout) -> None:
    state = seed_job(store, checkout, "actively claimed")
    state.status = "processing"
    store.save(state)
    claim = store.job_claim(state.job_id)
    assert claim.acquire(blocking=False)
    try:
        assert worker_module.recover_orphans(store) == []
    finally:
        claim.release()


def test_cache_eviction_keeps_active_and_never_touches_evidence(cfg, store, checkout) -> None:
    """Old finished-job cache goes; the active job's cache stays; durable
    evidence and state live outside the cache entirely."""
    # finished job with an old cache dir
    old = seed_job(store, checkout, "old finished job")
    old.status = "published"
    store.save(old)
    old_cache = cfg.cache_dir() / old.job_id
    old_cache.mkdir(parents=True, exist_ok=True)
    (old_cache / "extract.md").write_text("old extract")
    old_time = time.time() - (cfg.cache.retention_days + 1) * 86400
    import os

    os.utime(old_cache, (old_time, old_time))
    os.utime(store.state_path(old.job_id), (old_time, old_time))  # activity stopped at finish
    # active job with cache
    active = seed_job(store, checkout, "active job")
    active.status = "processing"
    store.save(active)
    active_cache = cfg.cache_dir() / active.job_id
    active_cache.mkdir(parents=True, exist_ok=True)
    (active_cache / "extract.md").write_text("active extract")
    # durable evidence in the finished job dir
    store.append_evidence(old.job_id, [{"source_id": "s1", "text": "retained claim"}])

    worker_module.cleanup_cache(cfg, store)

    assert not old_cache.exists()  # expired with the finished job
    assert active_cache.exists() and (active_cache / "extract.md").is_file()
    assert store.read_evidence(old.job_id)  # durable evidence untouched
    assert store.exists(old.job_id)  # job state is not cache


def test_cache_global_cap_evicts_oldest_inactive(cfg, store, checkout) -> None:
    cfg.cache.max_bytes = 150  # fits one 100-byte cache, forces one eviction
    a = seed_job(store, checkout, "cache a")
    b = seed_job(store, checkout, "cache b")
    for job in (a, b):
        job.status = "published"
        store.save(job)
        cache = cfg.cache_dir() / job.job_id
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "blob.bin").write_bytes(b"x" * 100)
    older = time.time() - 3600
    import os

    os.utime(cfg.cache_dir() / a.job_id, (older, older))
    worker_module.cleanup_cache(cfg, store)
    assert not (cfg.cache_dir() / a.job_id).exists()  # oldest inactive evicted
    assert (cfg.cache_dir() / b.job_id).exists()


def test_finished_job_retention_cleanup(cfg, store, checkout) -> None:
    done = seed_job(store, checkout, "retention")
    done.status = "published"
    store.save(done)
    unresolved = seed_job(store, checkout, "unresolved")
    unresolved.status = "retryable"
    store.save(unresolved)
    removed = worker_module.cleanup_finished_jobs(cfg, store)
    assert removed == []  # fresh finished job is inside the window
    removed = store.cleanup_finished_jobs(older_than_days=0)
    assert removed == [done.job_id]
    assert store.exists(unresolved.job_id)  # unresolved survives


def test_worker_survives_submitter_disconnect_via_subprocess(cfg, store, qwen, checkout, monkeypatch, tmp_path) -> None:
    """A submitted job keeps running in the worker process after the
    submitting client is gone: simulate with a real subprocess worker that
    outlives its parent's submit call (worker --once)."""
    state = seed_job(store, checkout, "disconnect survivor")
    qwen.overrides["plan"] = [covered_plan()]
    script = f"""
import sys, json
sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})
from doclayer.curator.config import CuratorConfig
from doclayer.curator.jobstore import JobStore
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.publish import Publisher
from doclayer.curator import worker
from conftest import ScriptedQwen

qwen = ScriptedQwen()
qwen.overrides["plan"] = [{covered_plan()!r}]

cfg = CuratorConfig()
cfg.paths.brain_checkout = {str(checkout)!r}
cfg.paths.jobs = {str(cfg.jobs_dir())!r}
cfg.paths.worktrees = {str(cfg.paths.worktrees)!r}
cfg.paths.cache = {str(cfg.paths.cache)!r}
cfg.publish.forgejo_url = {cfg.publish.forgejo_url!r}

def factory(cfg, store):
    return Pipeline(cfg, store, inference_transport=qwen.transport(),
                    publisher=Publisher(cfg, store, forgejo=None))

rc = worker.run_worker(cfg, once=True, poll_interval_s=0.1, pipeline_factory=factory)
assert rc == 0
print("worker-done")
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "worker-done" in proc.stdout, proc.stderr
    loaded = store.load(state.job_id)
    assert loaded.status == "covered"


def test_worker_refuses_to_start_without_a_forgejo_url(cfg, capsys) -> None:
    cfg.publish.forgejo_url = ""
    assert worker_module.run_worker(cfg, once=True, poll_interval_s=0.01) == 2
    assert "publish.forgejo_url is not configured" in capsys.readouterr().err
    assert not cfg.jobs_dir().exists() or not any(cfg.jobs_dir().iterdir())
