"""Publication: real git worktrees against an isolated bare origin, with a
fake Forgejo whose merges really advance main — recovery, stale edits,
collisions, partial publication, scope discipline."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import FakeForgejo, run_git, valid_meta

from doclayer import frontmatter as fm
from doclayer.curator.config import CuratorConfig
from doclayer.curator.jobstore import JobStore
from doclayer.curator.publish import (
    Publisher,
    alternate_path,
)
from doclayer.curator.state import JobState, PageChange
from doclayer.curator.validate import check_update_scope


def write_change(
    path: str,
    cid: str,
    body: str,
    action: str = "create",
    base_blob: str = "",
    base_commit: str = "",
) -> PageChange:
    return PageChange(
        group_id="g1",
        path=path,
        action=action,
        title="T",
        draft_meta=valid_meta(cid=cid),
        draft_body=body,
        status="validated",
        base_blob=base_blob,
        base_commit=base_commit,
        source_ids=["src-1"],
    )


@pytest.fixture()
def publisher(cfg: CuratorConfig, store: JobStore, forgejo: FakeForgejo) -> Publisher:
    return Publisher(cfg, store, forgejo=forgejo)


def make_job(store: JobStore, checkout: Path, job_id: str = "job-testpub01") -> JobState:
    state = JobState(job_id=job_id, idempotency_key="pub-key", intent="publish test")
    state.base_commit = run_git(checkout, "rev-parse", "HEAD")
    state, _ = store.create(state)
    return state


def no_revalidate(change: PageChange, fresh_meta, fresh_body) -> list:
    return []


def identity_reconciler(change: PageChange, fresh_page: str, collision: bool) -> dict:
    return {"meta": change.draft_meta, "body": change.draft_body, "notes": []}


def publish_with_branch_tracking(
    publisher,
    job,
    changes,
    forgejo,
    persist=lambda: None,
    reconciler=None,
    revalidate=None,
):
    """Mirror real pushes into the fake Forgejo's branch set around the real
    git push the publisher performs (class-level patch, always restored)."""
    original = Publisher._ensure_pushed

    def tracked(self, state, branch, persist_fn):
        original(self, state, branch, persist_fn)
        forgejo.branches.add(branch)

    Publisher._ensure_pushed = tracked  # type: ignore[method-assign]
    try:
        return publisher.publish_changes(
            job,
            changes,
            reconciler or identity_reconciler,
            revalidate or no_revalidate,
            persist,
        )
    finally:
        Publisher._ensure_pushed = original  # type: ignore[method-assign]


def git_show(repo: Path, ref_path: str) -> str | None:
    proc = subprocess.run(["git", "-C", str(repo), "show", ref_path], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def test_publish_creates_page_pr_and_merges(cfg, store, forgejo, checkout, origin, publisher) -> None:
    job = make_job(store, checkout)
    change = write_change("wiki/new-page.md", "c-aaaa0001", "# New\n\nContent. [source: x]")

    outcome = publish_with_branch_tracking(publisher, job, [change], forgejo, persist=lambda: store.save(job))
    assert outcome.merged
    assert outcome.pr_number is not None
    assert forgejo.prs[outcome.pr_number]["merged"]
    assert change.status == "published"
    # canonical changed on origin/main (the fake merge really landed it)
    landed = git_show(origin, f"main:{change.path}")
    assert landed is not None and "New" in landed
    # publication identity recorded durably on the job
    assert job.publication.merge_commit
    assert job.publication.merged
    # worktree cleaned up after verified merge
    assert job.publication.worktree is None
    assert not list(Path(cfg.paths.worktrees).glob("job-*"))
    # merge commit is reachable from origin/main
    assert outcome.merge_commit == job.publication.merge_commit


def test_uncertain_push_recovered_without_duplicate_publication(
    cfg, store, forgejo, checkout, origin, publisher
) -> None:
    """A push that dies mid-flight must not corrupt state; the retry resumes
    the same job/branch and lands exactly one PR."""
    job = make_job(store, checkout)
    change = write_change("wiki/recover.md", "c-aaaa0002", "# R [source: x]")

    original_push = Publisher._ensure_pushed

    def flaky_push(self, state, branch, persist):
        # the push landed remotely but the client saw a failure
        forgejo.branches.add(branch)
        from doclayer.curator import gittools

        raise gittools.GitError("push: connection died mid-flight")

    Publisher._ensure_pushed = flaky_push  # type: ignore[method-assign]
    try:
        with pytest.raises(Exception):
            publish_with_branch_tracking(publisher, job, [change], forgejo, persist=lambda: store.save(job))
    finally:
        Publisher._ensure_pushed = original_push  # type: ignore[method-assign]
    assert job.publication.branch is not None
    assert forgejo.prs == {}  # nothing published yet, no premature PR

    outcome = publish_with_branch_tracking(publisher, job, [change], forgejo, persist=lambda: store.save(job))
    assert outcome.merged
    assert len(forgejo.prs) == 1
    assert git_show(origin, f"main:{change.path}")


def test_merge_reply_lost_is_recovered_by_query(cfg, store, forgejo, checkout, origin, publisher) -> None:
    """The merge lands but the HTTP response is lost: the publisher must
    observe merged state via the recovery query and report merged — once."""
    job = make_job(store, checkout)
    change = write_change("wiki/lost-reply.md", "c-aaaa0003", "# L [source: x]")
    forgejo.fail_after_merge = 1
    outcome = publish_with_branch_tracking(publisher, job, [change], forgejo, persist=lambda: store.save(job))
    assert outcome.merged
    assert len(forgejo.merge_calls) == 1  # no duplicate merge attempt
    assert forgejo.prs[outcome.pr_number]["merged"]


def test_resume_after_merge_reuses_pr_and_never_duplicates(cfg, store, forgejo, checkout, origin, publisher) -> None:
    job = make_job(store, checkout)
    change = write_change("wiki/done.md", "c-aaaa0004", "# D [source: x]")
    outcome = publish_with_branch_tracking(publisher, job, [change], forgejo, persist=lambda: store.save(job))
    assert outcome.merged
    pr_number = outcome.pr_number
    merge_sha = forgejo.prs[pr_number]["merge_commit_sha"]

    # a job record that lost everything after the merge (restart mid-report)
    replay = JobState(job_id=job.job_id, idempotency_key="pub-key", intent="publish test")
    replay.publication.branch = job.publication.branch
    replay.publication.pushed = True
    replay.publication.commit_sha = job.publication.commit_sha
    replay.publication.pr_number = pr_number
    replay.publication.merged = True
    replay.publication.merge_commit = merge_sha
    change2 = write_change("wiki/done.md", "c-aaaa0004", "# D [source: x]")
    outcome2 = publisher.publish_changes(
        replay, [change2], identity_reconciler, no_revalidate, lambda: store.save(replay)
    )
    assert outcome2.merged
    assert outcome2.pr_number == pr_number
    assert len(forgejo.prs) == 1  # no duplicate PR
    assert change2.status == "published"


def test_stale_base_reconciles_and_preserves_operator_edit(cfg, store, forgejo, checkout, origin, publisher) -> None:
    job = make_job(store, checkout)
    existing = write_change(
        "wiki/existing-model.md",
        "c-1111aaaa",
        "# placeholder",
        action="update",
        base_blob=run_git(checkout, "rev-parse", f"{job.base_commit}:wiki/existing-model.md").strip(),
        base_commit=job.base_commit,
    )
    existing.draft_meta = valid_meta(cid="c-1111aaaa")
    # operator edit lands on origin after drafting
    new_page = fm.build(
        valid_meta(cid="c-1111aaaa"),
        "# Existing model\n\n## Capabilities\n\nKnown facts.\n\n## Safety\n\nKnown safety notes.\n\n## Operator note\n\nThe operator's edit after drafting.\n",
    )
    seed = origin.parent / "seed-brain"
    (seed / "wiki" / "existing-model.md").write_text(new_page)
    run_git(seed, "add", "-A")
    run_git(seed, "commit", "-qm", "operator edit lands after draft")
    run_git(seed, "push", "-q", str(origin), "main")
    run_git(checkout, "fetch", "origin")

    seen = {}
    existing.check_progress = {"completed": "obsolete draft check"}

    def reconciler(change, fresh_page, collision):
        seen["fresh"] = fresh_page
        return {
            "meta": valid_meta(cid="c-1111aaaa"),
            "body": "# Existing model\n\n## Capabilities\n\nKnown facts.\n\n## Safety\n\nKnown safety notes.\n\n## Operator note\n\nThe operator's edit after drafting.\n\n## New facts\n\nDraft addition. [source: x]\n",
            "notes": ["merged onto fresh page"],
            "draft_progress": {"reconciled": "bound by owning reconciler"},
        }

    def revalidate(change, fresh_meta, fresh_body):
        assert fresh_meta == fm.parse(new_page)[0]
        assert fresh_body == fm.parse(new_page)[1]
        assert change.draft_progress == {"reconciled": "bound by owning reconciler"}
        assert change.check_progress == {}
        return []

    publisher.publish_changes(job, [existing], reconciler, revalidate, lambda: store.save(job))
    assert "The operator's edit" in seen["fresh"]
    assert existing.status == "published"
    landed = git_show(origin, "main:wiki/existing-model.md")
    assert "The operator's edit" in landed  # intervening edit survived
    assert "Draft addition" in landed  # draft knowledge landed too


def test_collision_same_subject_becomes_update(cfg, store, forgejo, checkout, origin, publisher) -> None:
    job = make_job(store, checkout)
    change = write_change("wiki/existing-model.md", "c-aaaa0005", "# draft for existing subject [source: x]")
    calls = []

    def reconciler(change, fresh_page, collision):
        calls.append(collision)
        assert collision
        return {
            "resolution": "same-subject",
            "meta": valid_meta(cid="c-1111aaaa"),
            "body": "# merged page [source: x]",
        }

    def revalidate(change, fresh_meta, fresh_body):
        return []

    outcome = publish_with_branch_tracking(
        publisher,
        job,
        [change],
        forgejo,
        persist=lambda: store.save(job),
        reconciler=reconciler,
    )
    assert calls == [True]
    assert outcome.merged
    assert change.action == "update"
    landed = git_show(origin, "main:wiki/existing-model.md")
    assert "merged page" in landed


def test_collision_distinct_subject_renames_and_rewrites_links(
    cfg, store, forgejo, checkout, origin, publisher
) -> None:
    job = make_job(store, checkout)
    colliding = write_change("wiki/existing-model.md", "c-aaaa0006", "# distinct subject draft [source: x]")
    sibling = write_change("wiki/overview.md", "c-aaaa0007", "# overview linking [[wiki/existing-model]] [source: x]")

    def reconciler(change, fresh_page, collision):
        assert collision
        return {
            "resolution": "distinct-subject",
            "explanation": "different subject entirely",
            "meta": change.draft_meta,
            "body": change.draft_body,
        }

    outcome = publish_with_branch_tracking(
        publisher,
        job,
        [colliding, sibling],
        forgejo,
        persist=lambda: store.save(job),
        reconciler=reconciler,
    )
    assert outcome.merged
    assert colliding.path != "wiki/existing-model.md"
    assert colliding.path.startswith("wiki/existing-model-")
    new_stem = colliding.path[:-3]  # wikilinks use the stem form
    assert f"[[{new_stem}]]" in sibling.draft_body  # links followed the rename
    assert "[[wiki/existing-model]]" not in sibling.draft_body
    assert git_show(origin, f"main:{colliding.path}") is not None


def test_partial_publication_lands_only_coherent_groups(cfg, store, forgejo, checkout, origin, publisher) -> None:
    """A group that failed validation stays behind; the validated group
    lands with its links intact."""
    job = make_job(store, checkout)
    good = write_change("wiki/lands.md", "c-aaaa0008", "# lands [source: x]")
    bad = write_change("wiki/conflicts.md", "c-aaaa0009", "# conflicts [source: x]")
    bad.status = "conflict"  # failed validation earlier in the pipeline
    bad.error = "evidence check: unsupported claim"

    outcome = publish_with_branch_tracking(publisher, job, [good, bad], forgejo, persist=lambda: store.save(job))
    assert outcome.merged
    assert outcome.published_changes == ["wiki/lands.md"]
    assert outcome.conflicted_changes == ["wiki/conflicts.md"]
    assert good.status == "published"
    assert bad.status == "conflict" and bad.error
    assert git_show(origin, "main:wiki/conflicts.md") is None  # never touched canonical
    assert git_show(origin, "main:wiki/lands.md") is not None


def test_reconciliation_that_fails_revalidation_conflicts_the_change(
    cfg, store, forgejo, checkout, origin, publisher
) -> None:
    job = make_job(store, checkout)
    change = write_change(
        "wiki/existing-model.md",
        "c-1111aaaa",
        "# x",
        action="update",
        base_blob=run_git(checkout, "rev-parse", f"{job.base_commit}:wiki/existing-model.md").strip(),
    )
    seed = origin.parent / "seed-brain"
    (seed / "wiki" / "existing-model.md").write_text(
        fm.build(valid_meta(cid="c-1111aaaa"), "# Changed upstream\n\n## Operator note\n\nedited\n")
    )
    run_git(seed, "add", "-A")
    run_git(seed, "commit", "-qm", "upstream change")
    run_git(seed, "push", "-q", str(origin), "main")
    run_git(checkout, "fetch", "origin")

    def reconciler(change, fresh_page, collision):
        # a "merge" that would drop the operator's new section
        return {"meta": valid_meta(cid="c-1111aaaa"), "body": "# merged without operator note [source: x]"}

    def revalidate(change, fresh_meta, fresh_body):
        return check_update_scope(fresh_meta or {}, fresh_body or "", change.draft_meta, change.draft_body, change.path)

    outcome = publisher.publish_changes(job, [change], reconciler, revalidate, lambda: store.save(job))
    assert change.status == "conflict"
    assert "revalidation" in (change.error or "")
    assert outcome.published_changes == []
    assert forgejo.prs == {}  # nothing to publish: no PR, no push


def test_publication_leaves_operator_checkout_dirty_state_alone(
    cfg, store, forgejo, checkout, origin, publisher
) -> None:
    job = make_job(store, checkout)
    (checkout / "unrelated-draft.md").write_text("operator's local scratch\n")
    change = write_change("wiki/clean.md", "c-aaaa000a", "# clean [source: x]")
    outcome = publish_with_branch_tracking(publisher, job, [change], forgejo, persist=lambda: store.save(job))
    assert outcome.merged
    status = run_git(checkout, "status", "--porcelain")
    assert "unrelated-draft.md" in status
    assert "wiki/clean.md" not in status
    # the checkout's HEAD stayed on its branch; publication happened in a worktree
    assert run_git(checkout, "rev-parse", "HEAD") == job.base_commit


def test_alternate_path_skips_taken_names() -> None:
    taken = {"wiki/topic.md", "wiki/topic-2.md", "wiki/topic-3.md"}
    assert alternate_path(taken, "wiki/topic.md") == "wiki/topic-4.md"
