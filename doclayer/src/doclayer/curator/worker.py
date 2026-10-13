"""brain-curator-worker: the long-lived job processor.

Reads the filesystem queue in a loop, claims jobs (the global worker lock plus
per-job claim files) and runs up to ``limits.parallel_jobs`` of them at once,
one thread (and event loop) per job; publication stays one at a time behind the
store's publish lock. Each job runs the pipeline with stage-level persistence,
and the worker recovers cleanly after a restart:

- queued jobs are claimed in submission order;
- a job stuck in processing/validating/publishing from a dead process is
  re-claimed and resumed at its recorded stage (publication recovery is
  idempotent — an uncertain push/PR/merge is re-queried, not repeated);
- cancel requests are honored at stage boundaries;
- finished/cancelled job artifacts and the source cache expire per the
  configured retention policy; corrections and dedup indexes survive.

SIGTERM/SIGINT stop claiming new jobs and wait for running jobs to finish,
then exit 0. systemd allows TimeoutStopSec=120 for that and then SIGKILLs the
worker, so a long job is cut off at its last persisted checkpoint; it stays
processing/validating/publishing on disk, and recover_orphans resumes it at
that stage when the unit starts again. No job is lost because its submitting
client hung up.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

from doclayer.curator.config import CuratorConfig, load_config, require_count
from doclayer.curator.jobstore import JobStore
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import JobState

_STOP = False


def _request_stop(signum: int, _frame: object) -> None:
    global _STOP
    _STOP = True


def cleanup_cache(
    cfg: CuratorConfig,
    store: JobStore,
    now: float | None = None,
    running: frozenset[str] = frozenset(),
) -> None:
    """Expire per-job source caches (retention measured from the job's latest
    activity) and enforce the global cache cap, evicting oldest inactive
    entries first. Durable evidence and job state live outside the cache and
    are never touched here. Jobs in ``running`` (owned by a worker thread right
    now) are never removed, whatever their on-disk state."""
    import shutil

    cache_root = cfg.cache_dir()
    if not cache_root.is_dir():
        return
    now = time.time() if now is None else now
    retention_s = cfg.cache.retention_days * 86400

    def inactive(job_id: str) -> bool:
        """Queued/preparing work owns its cache even before a worker claim."""
        if job_id in running:
            return False
        from doclayer.curator.jobstore import FileLock

        if store.exists(job_id):
            state = store.load(job_id)
            if state.in_flight or state.status == "preparing":
                return False
        lock = FileLock(store.job_dir(job_id) / ".claim")
        if lock.acquire(blocking=False):
            lock.release()
            return True
        return False

    jobs: dict[str, float] = {}
    for job_cache in cache_root.iterdir():
        if not job_cache.is_dir():
            continue
        jobs[job_cache.name] = job_cache.stat().st_mtime
        # retention: 7 days after the job's latest processing activity
        latest = _latest_activity(store, job_cache.name, default=job_cache.stat().st_mtime)
        if now - latest > retention_s and inactive(job_cache.name):
            shutil.rmtree(job_cache, ignore_errors=True)
            jobs.pop(job_cache.name, None)
    # global cap: evict oldest inactive (finished or unclaimed) first
    total = sum(_dir_size(cache_root / name) for name in jobs)
    if total <= cfg.cache.max_bytes:
        return

    for job_id in sorted(jobs, key=lambda j: jobs[j]):
        if total <= cfg.cache.max_bytes:
            break
        if not inactive(job_id):
            continue
        size = _dir_size(cache_root / job_id)
        shutil.rmtree(cache_root / job_id, ignore_errors=True)
        total -= size


def _latest_activity(store: JobStore, job_id: str, default: float) -> float:
    try:
        path = store.state_path(job_id)
        return max(path.stat().st_mtime, default)
    except OSError:
        return default


def _dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


def cleanup_finished_jobs(cfg: CuratorConfig, store: JobStore) -> list[str]:
    removed = store.cleanup_finished_jobs(cfg.retention.finished_job_days)
    # corrections survive cleanup by design (they live outside job dirs)
    return removed


def recover_orphans(store: JobStore) -> list[JobState]:
    """Jobs whose worker died mid-flight: re-claimable, resumed at their
    recorded stage by the normal loop."""
    orphans = []
    for state in store.load_all():
        if state.status not in ("processing", "validating", "publishing"):
            continue
        claim = store.job_claim(state.job_id)
        if claim.acquire(blocking=False):
            claim.release()
            orphans.append(state)
    return orphans


def process_job(cfg: CuratorConfig, store: JobStore, state: JobState, pipeline_factory=None) -> JobState:
    claim = store.job_claim(state.job_id)
    if not claim.acquire(blocking=False):
        return state
    try:
        pipeline = pipeline_factory(cfg, store) if pipeline_factory else Pipeline(cfg, store)

        async def _run() -> JobState:
            return await pipeline.run(state)

        return asyncio.run(_run())
    finally:
        claim.release()


def _record_crash(store: JobStore, job_id: str, exc: BaseException) -> None:
    """Leave a crashed job `retryable` (as the pipeline's own handlers do for
    operational failures) so job_status, resume and the gap dispatcher see an
    ordinary stopped job instead of one that is still "processing". A job whose
    cancellation was requested ends `cancelled`: it is never resumed."""
    with store.control_lock(job_id):
        state = store.load(job_id)
        if state.finished:
            return  # cancelled/finished before the crash: leave it alone
        state.status = "cancelled" if state.cancel_requested else "retryable"
        state.error = f"worker crash: {exc!r}"[:500]
        store.save(state)


def _report(store: JobStore, job_id: str, future: Future) -> None:
    """Print a finished job's outcome; a crashed job is logged and marked
    retryable, never fatal to the worker."""
    try:
        done = future.result()
    except BaseException as exc:  # one job's crash must not stop the worker
        print(f"job {job_id} failed: {exc!r}", file=sys.stderr, flush=True)
        traceback.print_exception(exc, file=sys.stderr)
        sys.stderr.flush()
        try:
            _record_crash(store, job_id, exc)
        except Exception as save_exc:
            print(
                f"job {job_id}: could not record the crash as retryable: {save_exc!r}",
                file=sys.stderr,
                flush=True,
            )
        return
    print(f"job {done.job_id}: {done.outcome_line()}", flush=True)


def run_worker(
    cfg: CuratorConfig,
    *,
    once: bool = False,
    poll_interval_s: float = 10.0,
    pipeline_factory=None,
) -> int:
    parallel = require_count(cfg.limits.parallel_jobs, "limits.parallel_jobs")
    if not cfg.publish.forgejo_url:
        # Every job ends by publishing; fail fast instead of after all model work.
        print("publish.forgejo_url is not configured; set it in the curator config", file=sys.stderr, flush=True)
        return 2
    store = JobStore(cfg.jobs_dir())
    store.jobs_dir.mkdir(parents=True, exist_ok=True)
    worker_lock = store.worker_lock()
    if not worker_lock.acquire(blocking=False):
        print("another worker holds the queue lock; exiting", flush=True)
        return 1
    executor = ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="curator-job")
    running: dict[Future, str] = {}

    def reap() -> None:
        for future in [f for f in running if f.done()]:
            _report(store, running.pop(future), future)

    def wait_for_completion(timeout: float | None) -> None:
        if running:
            wait(running, timeout=timeout, return_when=FIRST_COMPLETED)
        reap()

    def submit(state: JobState) -> None:
        future = executor.submit(process_job, cfg, store, state, pipeline_factory)
        running[future] = state.job_id

    try:
        signal.signal(signal.SIGTERM, _request_stop)
        signal.signal(signal.SIGINT, _request_stop)
        for orphan in recover_orphans(store):
            while len(running) >= parallel and not _STOP:
                wait_for_completion(None)
            if _STOP:
                break
            print(f"recovering interrupted job {orphan.job_id} at stage {orphan.stage}", flush=True)
            submit(orphan)
        while not _STOP:
            reap()
            if len(running) >= parallel:
                wait_for_completion(poll_interval_s)
                continue
            cleanup_finished_jobs(cfg, store)
            cleanup_cache(cfg, store, running=frozenset(running.values()))
            state = store.claim_next_queued()
            if state is None:
                if once:
                    break
                if running:
                    wait_for_completion(poll_interval_s)
                else:
                    time.sleep(poll_interval_s)
                continue
            print(f"processing job {state.job_id} (intent: {state.intent[:60]})", flush=True)
            submit(state)
            if once:
                break
    finally:
        # Stop or finish: running jobs complete before the worker lock is released.
        executor.shutdown(wait=True)
        reap()
        worker_lock.release()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brain-curator-worker")
    parser.add_argument("--config", default=None, help="config file path")
    parser.add_argument("--once", action="store_true", help="process at most one job, then exit")
    parser.add_argument("--poll-interval", type=float, default=10.0)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    with contextlib.suppress(KeyboardInterrupt):
        return run_worker(cfg, once=args.once, poll_interval_s=args.poll_interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
