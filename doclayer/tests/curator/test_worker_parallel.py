"""Worker pool: up to limits.parallel_jobs jobs at once, publishing serialized.

Every test uses fake pipelines through ``pipeline_factory``; nothing here talks
to a model. Threads synchronise on barriers and events, not on sleeps.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path

import pytest

from doclayer.curator import gittools
from doclayer.curator import worker as worker_module
from doclayer.curator.jobstore import JobStore
from doclayer.curator.state import JobState

TIMEOUT = 20.0


@pytest.fixture(autouse=True)
def _isolate_stop_and_signals(monkeypatch):
    """run_worker installs SIGTERM/SIGINT handlers and the stop flag is module
    global; restore both so no test leaks into another."""
    monkeypatch.setattr(worker_module, "_STOP", False)
    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    yield
    for sig, handler in old.items():
        signal.signal(sig, handler)


def queue_jobs(store: JobStore, count: int) -> list[str]:
    ids = []
    for i in range(count):
        state, _ = store.create(
            JobState(
                job_id="",
                idempotency_key=f"parallel-{i}",
                intent=f"parallel job {i}",
                base_commit="0" * 40,
                created_at=f"2026-10-10T00:00:{i:02d}Z",
            )
        )
        ids.append(state.job_id)
    return ids


class FakePipeline:
    """Runs one job: calls ``body(state)`` then marks the job covered."""

    def __init__(self, store: JobStore, body) -> None:
        self.store = store
        self.body = body

    async def run(self, state: JobState) -> JobState:
        self.body(state)
        state.status = "covered"
        self.store.save(state)
        return state


def factory_for(body):
    def factory(cfg, store):
        return FakePipeline(store, body)

    return factory


class Tracker:
    """Thread-safe record of starts, finishes and peak concurrency."""

    def __init__(self, stop_after: int, monkeypatch) -> None:
        self.lock = threading.Lock()
        self.started: list[str] = []
        self.finished: list[str] = []
        self.running = 0
        self.peak = 0
        self.stop_after = stop_after
        self._monkeypatch = monkeypatch

    def enter(self, job_id: str) -> None:
        with self.lock:
            self.started.append(job_id)
            self.running += 1
            self.peak = max(self.peak, self.running)

    def leave(self, job_id: str) -> None:
        with self.lock:
            self.running -= 1
            self.finished.append(job_id)
            if len(self.finished) >= self.stop_after:
                # what the signal handler does: stop claiming, let the pool drain
                worker_module._STOP = True


def test_three_queued_jobs_run_concurrently_with_parallel_jobs_three(cfg, store, monkeypatch) -> None:
    cfg.limits.parallel_jobs = 3
    ids = queue_jobs(store, 3)
    tracker = Tracker(stop_after=3, monkeypatch=monkeypatch)
    barrier = threading.Barrier(3, timeout=TIMEOUT)
    threads: set[int] = set()

    def body(state: JobState) -> None:
        tracker.enter(state.job_id)
        threads.add(threading.get_ident())
        barrier.wait()  # only passes when all three jobs are running at once
        tracker.leave(state.job_id)

    rc = worker_module.run_worker(cfg, poll_interval_s=0.05, pipeline_factory=factory_for(body))
    assert rc == 0
    assert tracker.peak == 3
    assert sorted(tracker.finished) == sorted(ids)
    assert len(threads) == 3  # one worker thread (and event loop) per job
    assert threading.get_ident() not in threads
    assert all(store.load(job_id).status == "covered" for job_id in ids)


def test_parallel_jobs_one_keeps_sequential_order(cfg, store, monkeypatch) -> None:
    assert cfg.limits.parallel_jobs == 1  # the default is today's behaviour
    ids = queue_jobs(store, 3)
    tracker = Tracker(stop_after=3, monkeypatch=monkeypatch)

    def body(state: JobState) -> None:
        tracker.enter(state.job_id)
        tracker.leave(state.job_id)

    rc = worker_module.run_worker(cfg, poll_interval_s=0.05, pipeline_factory=factory_for(body))
    assert rc == 0
    assert tracker.started == ids  # oldest submission first
    assert tracker.finished == ids
    assert tracker.peak == 1


def test_once_processes_at_most_one_job_even_with_a_wide_pool(cfg, store) -> None:
    cfg.limits.parallel_jobs = 3
    ids = queue_jobs(store, 3)
    seen: list[str] = []

    def body(state: JobState) -> None:
        seen.append(state.job_id)

    rc = worker_module.run_worker(cfg, once=True, poll_interval_s=0.05, pipeline_factory=factory_for(body))
    assert rc == 0
    assert seen == ids[:1]
    assert [store.load(i).status for i in ids] == ["covered", "queued", "queued"]


def test_interrupted_jobs_resume_through_the_same_pool(cfg, store, monkeypatch) -> None:
    cfg.limits.parallel_jobs = 2
    ids = queue_jobs(store, 2)
    for job_id in ids:
        state = store.load(job_id)
        state.status = "processing"
        state.stage = "place"
        store.save(state)
    tracker = Tracker(stop_after=2, monkeypatch=monkeypatch)
    barrier = threading.Barrier(2, timeout=TIMEOUT)

    def body(state: JobState) -> None:
        tracker.enter(state.job_id)
        barrier.wait()
        tracker.leave(state.job_id)

    rc = worker_module.run_worker(cfg, poll_interval_s=0.05, pipeline_factory=factory_for(body))
    assert rc == 0
    assert tracker.peak == 2
    assert sorted(tracker.finished) == sorted(ids)


def test_publication_intervals_never_overlap(cfg, store, monkeypatch) -> None:
    cfg.limits.parallel_jobs = 3
    queue_jobs(store, 3)
    tracker = Tracker(stop_after=3, monkeypatch=monkeypatch)
    all_ready = threading.Barrier(3, timeout=TIMEOUT)
    intervals: list[tuple[float, float]] = []

    def body(state: JobState) -> None:
        tracker.enter(state.job_id)
        all_ready.wait()  # all three reach publication at the same moment
        with store.publish_lock():
            began = time.monotonic()
            time.sleep(0.05)  # the publish step works while holding the lock
            intervals.append((began, time.monotonic()))
        tracker.leave(state.job_id)

    rc = worker_module.run_worker(cfg, poll_interval_s=0.05, pipeline_factory=factory_for(body))
    assert rc == 0
    assert tracker.peak == 3
    assert len(intervals) == 3
    intervals.sort()
    for (_, earlier_end), (later_start, _) in zip(intervals, intervals[1:]):
        assert earlier_end <= later_start


def test_one_failing_job_does_not_stop_the_others(cfg, store, monkeypatch, capsys) -> None:
    cfg.limits.parallel_jobs = 3
    ids = queue_jobs(store, 3)
    tracker = Tracker(stop_after=3, monkeypatch=monkeypatch)
    others_running = threading.Barrier(3, timeout=TIMEOUT)

    def body(state: JobState) -> None:
        tracker.enter(state.job_id)
        others_running.wait()
        if state.job_id == ids[0]:
            tracker.leave(state.job_id)
            raise RuntimeError("scripted pipeline crash")
        tracker.leave(state.job_id)

    rc = worker_module.run_worker(cfg, poll_interval_s=0.05, pipeline_factory=factory_for(body))
    assert rc == 0
    assert sorted(tracker.finished) == sorted(ids)
    assert store.load(ids[1]).status == "covered"
    assert store.load(ids[2]).status == "covered"
    captured = capsys.readouterr()
    report = captured.out + captured.err
    assert f"job {ids[0]} failed" in report
    assert "scripted pipeline crash" in report
    crashed = store.load(ids[0])
    assert crashed.status == "retryable"  # not left "processing": job_status and the dispatcher can see it
    assert "worker crash" in crashed.error
    assert "scripted pipeline crash" in crashed.error
    # the crashed job's claim was released so a later worker can recover it
    claim = store.job_claim(ids[0])
    assert claim.acquire(blocking=False)
    claim.release()


def test_failure_recording_a_crash_is_logged_and_the_worker_continues(cfg, store, monkeypatch, capsys) -> None:
    ids = queue_jobs(store, 2)
    tracker = Tracker(stop_after=2, monkeypatch=monkeypatch)

    def body(state: JobState) -> None:
        tracker.enter(state.job_id)
        tracker.leave(state.job_id)
        if state.job_id == ids[0]:
            raise RuntimeError("scripted pipeline crash")

    def broken_save(self, state):
        if state.status == "retryable":
            raise OSError("disk full")
        return real_save(self, state)

    real_save = JobStore.save
    monkeypatch.setattr(JobStore, "save", broken_save)
    rc = worker_module.run_worker(cfg, poll_interval_s=0.05, pipeline_factory=factory_for(body))
    assert rc == 0
    assert tracker.finished == ids  # the next job still ran
    captured = capsys.readouterr()
    assert f"job {ids[0]}: could not record the crash as retryable" in captured.err
    assert "disk full" in captured.err


def test_stop_waits_for_running_jobs(cfg, store) -> None:
    cfg.limits.parallel_jobs = 2
    ids = queue_jobs(store, 3)
    signalled = threading.Event()
    both_running = threading.Barrier(2, timeout=TIMEOUT)
    finished: list[str] = []

    def body(state: JobState) -> None:
        both_running.wait()
        if state.job_id == ids[0]:
            os.kill(os.getpid(), signal.SIGTERM)  # the real signal path
            signalled.set()
        else:
            assert signalled.wait(TIMEOUT)
        # keep running until the main thread has handled the signal
        deadline = time.monotonic() + TIMEOUT
        while not worker_module._STOP and time.monotonic() < deadline:
            time.sleep(0.005)
        # still running after the stop request: run_worker must wait for us
        time.sleep(0.2)
        finished.append(state.job_id)

    rc = worker_module.run_worker(cfg, poll_interval_s=0.05, pipeline_factory=factory_for(body))
    assert rc == 0
    assert sorted(finished) == sorted(ids[:2])  # both finished before run_worker returned
    assert store.load(ids[0]).status == "covered"
    assert store.load(ids[1]).status == "covered"
    assert store.load(ids[2]).status == "queued"  # nothing new was claimed after the stop


def test_cache_cleanup_never_removes_a_running_jobs_cache(cfg, store) -> None:
    (job_id,) = queue_jobs(store, 1)
    state = store.load(job_id)
    state.status = "covered"  # looks inactive on disk, but a thread still owns it
    store.save(state)
    cache = cfg.cache_dir() / job_id
    cache.mkdir(parents=True)
    (cache / "source.bin").write_bytes(b"x" * 10)
    ancient = time.time() - 365 * 86400
    os.utime(cache, (ancient, ancient))
    os.utime(store.state_path(job_id), (ancient, ancient))

    worker_module.cleanup_cache(cfg, store, running=frozenset({job_id}))
    assert cache.is_dir()
    worker_module.cleanup_cache(cfg, store)
    assert not cache.exists()


def test_concurrent_fetches_of_one_checkout_are_serialized(tmp_path, monkeypatch) -> None:
    """Parallel jobs fetch the shared checkout in the place stage; two
    simultaneous `git fetch --prune` can lose a ref-lock race."""
    inside = 0
    peak = 0
    guard = threading.Lock()

    def fake_git(repo, *args, **kwargs):
        nonlocal inside, peak
        with guard:
            inside += 1
            peak = max(peak, inside)
        time.sleep(0.02)
        with guard:
            inside -= 1
        return ""

    monkeypatch.setattr(gittools, "git", fake_git)
    repo = Path(tmp_path)
    threads = [threading.Thread(target=gittools.fetch, args=(repo, "origin")) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(TIMEOUT)
    assert peak == 1


def test_crash_of_a_cancel_requested_job_ends_cancelled(cfg, store) -> None:
    (job_id,) = queue_jobs(store, 1)
    state = store.load(job_id)
    state.status = "processing"
    state.cancel_requested = True
    store.save(state)
    worker_module._record_crash(store, job_id, RuntimeError("scripted pipeline crash"))
    crashed = store.load(job_id)
    assert crashed.status == "cancelled"
    assert "scripted pipeline crash" in crashed.error


def _git(*args, cwd=None):
    import subprocess

    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_fetch_waits_out_a_ref_lock_held_by_publication(tmp_path) -> None:
    """Publication pushes and worktree ref writes run under the publish lock,
    which a place-stage fetch does not take: a fetch that meets their ref lock
    retries instead of failing the job with a spurious `publication:` error."""
    remote, clone, work = tmp_path / "remote.git", tmp_path / "clone", tmp_path / "work"
    _git("init", "--bare", "-b", "main", str(remote))
    _git("clone", str(remote), str(work))
    for name in ("one", "two"):
        (work / name).write_text(name)
        _git("add", name, cwd=work)
        _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", name, cwd=work)
        _git("push", "origin", "HEAD:main", cwd=work)
        if name == "one":
            _git("clone", str(remote), str(clone))
    lock = clone / ".git" / "refs" / "remotes" / "origin" / "main.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("held by a concurrent ref write\n")
    releaser = threading.Timer(0.4, lock.unlink)
    releaser.start()
    try:
        gittools.fetch(clone, "origin")
    finally:
        releaser.join(TIMEOUT)
    assert gittools.rev_parse(clone, "origin/main") == gittools.rev_parse(work, "HEAD")


def test_fetch_does_not_retry_other_failures(tmp_path, monkeypatch) -> None:
    calls = []

    def failing(repo, *args, **kwargs):
        calls.append(args)
        raise gittools.GitError("git fetch --prune origin: fatal: repository not found")

    monkeypatch.setattr(gittools, "git", failing)
    with pytest.raises(gittools.GitError, match="not found"):
        gittools.fetch(tmp_path, "origin")
    assert len(calls) == 1


@pytest.mark.parametrize("value", [0, -1, "5", True, 2.5])
def test_worker_refuses_an_invalid_parallel_jobs(cfg, value) -> None:
    cfg.limits.parallel_jobs = value
    with pytest.raises(ValueError, match="limits.parallel_jobs"):
        worker_module.run_worker(cfg, once=True, poll_interval_s=0.01)
