"""Read-only recovery records actual remote merges before any new model work."""

import pytest
from test_publish import make_job, publish_with_branch_tracking, write_change

from doclayer.curator.forgejo import ForgejoError
from doclayer.curator.model import ModelClient
from doclayer.curator.publish import Publisher


def land(cfg, store, forgejo, checkout):
    publisher = Publisher(cfg, store, forgejo=forgejo)
    job = make_job(store, checkout)
    change = write_change(
        "wiki/recovered.md",
        "c-aaaa0031",
        "# Recovered\nAn actual source-backed fact. [source: x]\n",
    )
    job.changes = [change]
    outcome = publish_with_branch_tracking(publisher, job, [change], forgejo, persist=lambda: store.save(job))
    assert outcome.merged
    job.publication.merged = False
    job.publication.merge_commit = None
    change.status = "validated"
    job.usage.model_calls = 130
    job.limits["max_model_calls"] = 130
    store.save(job)
    return publisher, store.load(job.job_id)


@pytest.mark.parametrize("discover", [False, True])
@pytest.mark.parametrize("local_merged", [False, True])
def test_lost_merge_receipt_recovers_with_exhausted_budget_and_no_model(
    cfg, store, forgejo, checkout, monkeypatch, discover, local_merged
):
    publisher, job = land(cfg, store, forgejo, checkout)
    original_pr = job.publication.pr_number
    if local_merged:
        job.publication.merged = True
        job.publication.merge_commit = forgejo.prs[original_pr]["merge_commit_sha"]
    if discover:
        job.publication.pr_number = None
        job.publication.pr_url = None
    calls_before = len(forgejo.merge_calls)
    prs_before = len(forgejo.prs)

    def forbidden(*args, **kwargs):
        pytest.fail("Receipt recovery cannot create/merge or invoke a model")

    monkeypatch.setattr(forgejo, "merge_pr", forbidden)
    monkeypatch.setattr(forgejo, "create_pr", forbidden)
    monkeypatch.setattr(ModelClient, "_charge_call", forbidden)
    result = publisher.recover_existing_merge(job, job.changes, lambda: store.save(job))
    assert result and result.merged and result.pr_number == original_pr
    assert job.usage.model_calls == 130 and job.changes[0].status == "published"
    assert store.load(job.job_id).publication.merged
    assert len(forgejo.merge_calls) == calls_before and len(forgejo.prs) == prs_before


@pytest.mark.parametrize("field", ["head", "base", "ancestry"])
def test_mismatched_remote_receipt_never_records_publication(cfg, store, forgejo, checkout, field):
    publisher, job = land(cfg, store, forgejo, checkout)
    pr = forgejo.prs[job.publication.pr_number]
    if field == "head":
        pr["head"]["sha"] = "0" * 40
    elif field == "base":
        pr["base"]["ref"] = "different-base"
    else:
        pr["merge_commit_sha"] = "0" * 40
    with pytest.raises(
        ForgejoError,
        match="canonical Git" if field == "ancestry" else "publication head/base",
    ):
        publisher.recover_existing_merge(job, job.changes, lambda: store.save(job))
    assert not job.publication.merged and job.changes[0].status == "validated"
    assert not store.load(job.job_id).publication.merged


def test_unmerged_pr_is_read_only_and_remains_unpublished(cfg, store, forgejo, checkout, monkeypatch):
    publisher = Publisher(cfg, store, forgejo=forgejo)
    job = make_job(store, checkout)
    change = write_change("wiki/not-merged.md", "c-aaaa0032", "# Unmerged [source: x]\n")
    job.changes = [change]
    forgejo.fail_merge = 1
    with pytest.raises(ForgejoError):
        publish_with_branch_tracking(publisher, job, [change], forgejo, persist=lambda: store.save(job))
    assert job.publication.pr_number and not job.publication.merged

    def forbidden(*args, **kwargs):
        pytest.fail("Recovery must not publish an open PR")

    monkeypatch.setattr(forgejo, "merge_pr", forbidden)
    monkeypatch.setattr(forgejo, "create_pr", forbidden)
    assert publisher.recover_existing_merge(job, [change], lambda: store.save(job)) is None
    assert not job.publication.merged and change.status == "validated"


def test_read_outage_does_not_invent_receipt_or_change_budget(cfg, store, forgejo, checkout, monkeypatch):
    publisher, job = land(cfg, store, forgejo, checkout)

    def outage(*args):
        raise ForgejoError("Forgejo unavailable")

    monkeypatch.setattr(forgejo, "get_pr", outage)
    with pytest.raises(ForgejoError, match="unavailable"):
        publisher.recover_existing_merge(job, job.changes, lambda: store.save(job))
    assert not job.publication.merged and job.usage.model_calls == 130
    assert job.changes[0].status == "validated"


def test_uncertain_push_flag_does_not_hide_actual_merge(cfg, store, forgejo, checkout):
    publisher, job = land(cfg, store, forgejo, checkout)
    job.publication.pushed = False
    job.publication.pr_number = None
    assert publisher.recover_existing_merge(job, job.changes, lambda: store.save(job)).merged


def test_uncertain_failed_push_existing_wrong_branch_cannot_publish(cfg, store, forgejo, checkout, monkeypatch):
    from doclayer.curator import gittools

    publisher = Publisher(cfg, store, forgejo=forgejo)
    job = make_job(store, checkout)
    job.publication.branch = "curator/uncertain"
    job.publication.commit_sha = "a" * 40
    job.publication.worktree = None
    forgejo.branches.add(job.publication.branch)
    monkeypatch.setattr(forgejo, "branch_head", lambda branch: "b" * 40)
    original = gittools.git

    def failed_push(path, *args, **kwargs):
        if args and args[0] == "push":
            raise gittools.GitError("non-fast-forward or uncertain push")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(gittools, "git", failed_push)
    with pytest.raises(gittools.GitError):
        publisher._ensure_pushed(job, job.publication.branch, lambda: store.save(job))
    assert not job.publication.pushed and not forgejo.prs and not forgejo.merge_calls


@pytest.mark.parametrize("field", ["head", "base", "branch", "number"])
def test_all_merge_receipt_paths_require_exact_remote_identity(cfg, store, forgejo, checkout, field):
    from doclayer.curator.publish import PublishOutcome

    publisher, job = land(cfg, store, forgejo, checkout)
    pr = forgejo.prs[job.publication.pr_number]
    if field == "head":
        pr["head"]["sha"] = "0" * 40
    elif field == "branch":
        pr["head"]["ref"] = "other-job"
    elif field == "base":
        pr["base"]["ref"] = "other-base"
    else:
        pr["number"] += 1
    with pytest.raises(ForgejoError, match="publication head/base"):
        publisher._record_merge(job, job.changes, pr, PublishOutcome())
    assert not job.publication.merged and job.changes[0].status == "validated"
