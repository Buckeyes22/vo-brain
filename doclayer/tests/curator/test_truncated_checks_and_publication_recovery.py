"""Regression coverage for truncated checks and publication recovery."""

import asyncio

import pytest
from conftest import run_git
from test_publish import make_job, publish_with_branch_tracking, write_change

from doclayer.curator import curate
from doclayer.curator.model import OutputTruncated
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.publish import Publisher, ReconcileFailed
from doclayer.curator.state import JobState, Publication


@pytest.fixture
def publisher(cfg, store, forgejo):
    return Publisher(cfg, store, forgejo)


@pytest.mark.parametrize("truncated_checks", [1, 100])
def test_truncated_check_never_publishes_without_completed_check(
    cfg,
    store,
    checkout,
    forgejo,
    monkeypatch,
    truncated_checks,
):
    job = make_job(store, checkout)
    job.stage = "validate"
    job.changes = [
        write_change("wiki/unchecked.md", "c-aaaa0009", "# Unchecked [source: x]"),
        write_change("wiki/checked.md", "c-bbbb0009", "# Checked [source: x]"),
    ]
    for ch in job.changes:
        ch.status = "drafted"
    attempts = 0

    async def check(_client, _topic, change, *_args, **_kwargs):
        nonlocal attempts
        if change.path == "wiki/unchecked.md":
            attempts += 1
            if attempts <= truncated_checks:
                raise OutputTruncated("incomplete evidence check")
        return {"failures": [], "conflicts": []}

    async def repair(_client, _topic, change, *_args, **_kwargs):
        return {"frontmatter": change.draft_meta, "body": change.draft_body}

    monkeypatch.setattr(curate, "check_draft", check)
    monkeypatch.setattr(curate, "repair_draft", repair)
    pipe = Pipeline(cfg, store, publisher=Publisher(cfg, store, forgejo))
    store.save(job)
    asyncio.run(pipe.run(job))
    unchecked, checked = job.changes
    assert checked.status == "published"
    if truncated_checks == 1:
        assert unchecked.status == "published"
        assert attempts == 3  # Validation repair plus mandatory current publication recheck.
        assert job.status == "published"
    else:
        assert unchecked.status == "failed"
        assert "truncated" in unchecked.error
        assert attempts == cfg.limits.max_repair_passes + 1
        assert job.status == "partial"
        assert unchecked.path not in job.publication.committed_paths
    assert checked.path in job.publication.committed_paths


@pytest.mark.parametrize("legacy", [False, True])
def test_partial_merge_recovery_reports_only_committed_pages(
    store,
    checkout,
    origin,
    forgejo,
    publisher,
    legacy,
):
    job = make_job(store, checkout)
    good = write_change("wiki/landed.md", "c-aaaa0031", "# Landed [source: x]")
    conflict = write_change("wiki/conflict.md", "c-aaaa0032", "# Conflict [source: x]")
    conflict.status = "conflict"
    failed = write_change("wiki/failed.md", "c-aaaa0033", "# Failed [source: x]")
    failed.status = "failed"
    job.changes = [good, conflict, failed]
    outcome = publish_with_branch_tracking(publisher, job, job.changes, forgejo, lambda: store.save(job))
    assert outcome.published_changes == [good.path]
    replay = JobState.from_dict(store.load(job.job_id).to_dict())
    replay.changes[0].status = "validated"  # interrupted reporting
    if legacy:
        serialized = replay.publication.to_dict()
        serialized.pop("committed_paths", None)
        replay.publication = Publication.from_dict(serialized)
    calls_before = list(forgejo.merge_calls)
    recovered = publisher.publish_changes(
        replay, replay.changes, lambda *_: {}, lambda *_: [], lambda: store.save(replay)
    )
    assert recovered.published_changes == [good.path]
    assert recovered.conflicted_changes == [conflict.path, failed.path]
    assert [c.status for c in replay.changes] == ["published", "conflict", "failed"]
    assert forgejo.merge_calls == calls_before
    assert len(forgejo.prs) == 1
    assert run_git(origin, "ls-tree", "--name-only", "-r", "main").find(conflict.path) == -1


def test_recovery_rejects_manifest_not_matching_recorded_commit(store, checkout, forgejo, publisher):
    job = make_job(store, checkout)
    good = write_change("wiki/landed.md", "c-aaaa0034", "# Landed [source: x]")
    job.changes = [good]
    publish_with_branch_tracking(publisher, job, job.changes, forgejo, lambda: store.save(job))
    job.publication.committed_paths = ["wiki/imaginary.md"]
    with pytest.raises(ReconcileFailed, match="manifest"):
        publisher.publish_changes(job, job.changes, lambda *_: {}, lambda *_: [], lambda: None)


def test_unmerged_api_reply_is_not_publication(store, checkout, forgejo, publisher, monkeypatch):
    job = make_job(store, checkout)
    good = write_change("wiki/pending.md", "c-aaaa0035", "# Pending [source: x]")
    monkeypatch.setattr(forgejo, "merge_pr", lambda number, **kwargs: forgejo.get_pr(number))
    with pytest.raises(Exception, match="merged"):
        publish_with_branch_tracking(publisher, job, [good], forgejo, lambda: store.save(job))
    assert not job.publication.merged
    assert good.status == "validated"
