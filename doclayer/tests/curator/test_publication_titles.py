"""At publication the page's titles are checked against
the page actually being published, and after reconciliation the plan title is
taken from the fresh page. A same-subject collision whose plan title differs,
and an operator retitle landed between drafting and publication, both publish.
These run the real Publisher and the pipeline's real revalidate; only the
model-backed merge and the model reviews are replaced.
"""

import pytest
from conftest import run_git

from doclayer import frontmatter as fm
from doclayer.curator import curate, gittools, reconciliation
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.publish import Publisher
from doclayer.curator.state import JobState, PageChange

PAGE = "wiki/existing-model.md"


def git_show(repo, spec):
    return run_git(repo, "show", spec)


def page(raw):
    """(frontmatter with plain-string values as a draft carries them, body)."""
    meta, body = fm.parse(raw)
    return {key: value.isoformat() if hasattr(value, "isoformat") else value for key, value in meta.items()}, body


def no_model(monkeypatch):
    async def verify(*args):
        pass

    async def clean(*args, **kwargs):
        return {"failures": [], "conflicts": []}

    monkeypatch.setattr(Pipeline, "_verify_selected", verify)
    monkeypatch.setattr(Pipeline, "_evidence", lambda *args: [])
    monkeypatch.setattr(Pipeline, "_preserve_existing", clean)
    monkeypatch.setattr(curate, "check_draft", clean)


def operator_commit(origin, checkout, text):
    seed = origin.parent / "seed-brain"
    (seed / PAGE).write_text(text)
    run_git(seed, "add", "-A")
    run_git(seed, "commit", "-qm", "operator edit after drafting")
    run_git(seed, "push", "-q", str(origin), "main")
    run_git(checkout, "fetch", "origin")


def publish_job(store, checkout, change):
    state = store.create(
        JobState(
            job_id="",
            idempotency_key="titles",
            intent="publish titles",
            base_commit=run_git(checkout, "rev-parse", "HEAD"),
        )
    )[0]
    change.base_commit = change.base_commit or state.base_commit
    state.stage, state.status = "publish", "publishing"
    state.changes = [change]
    store.save(state)
    return state


@pytest.mark.asyncio
async def test_a_same_subject_collision_publishes_under_the_existing_page_title(
    cfg, store, checkout, origin, forgejo, monkeypatch
):
    no_model(monkeypatch)
    fresh_meta, fresh_body = page(git_show(origin, f"main:{PAGE}"))
    change = PageChange(
        "g1",
        PAGE,
        "create",
        title="Existing model guide",
        status="validated",
        draft_body="# Existing model guide\n\nA new fact. [source: x]\n",
        draft_meta={**fresh_meta, "concept_id": "c-9999aaaa", "title": "Existing model guide"},
    )
    state = publish_job(store, checkout, change)

    async def merge(client, topic, change, existing_page_text, corrections=None, **kwargs):
        return {
            "resolution": "same-subject",
            "frontmatter": dict(fresh_meta),
            "body": fresh_body + "\n## New facts\n\nA new fact. [source: x]\n",
        }

    monkeypatch.setattr(reconciliation, "reconcile_collision", merge)
    await Pipeline(cfg, store, publisher=Publisher(cfg, store, forgejo))._stage_publish(state)
    change = state.changes[0]
    assert change.status == "published", (change.status, change.error)
    assert change.action == "update" and change.title == fresh_meta["title"] == "Existing model"
    landed = git_show(origin, f"main:{PAGE}")
    assert "# Existing model\n" in landed and "A new fact." in landed


@pytest.mark.asyncio
async def test_an_operator_retitle_between_drafting_and_publication_is_respected(
    cfg, store, checkout, origin, forgejo, monkeypatch
):
    no_model(monkeypatch)
    base_meta, base_body = page(git_show(origin, f"main:{PAGE}"))
    head = run_git(checkout, "rev-parse", "HEAD")
    change = PageChange(
        "g1",
        PAGE,
        "update",
        title="Existing model",
        status="validated",
        base_commit=head,
        base_blob=gittools.blob_hash(checkout, f"{head}:{PAGE}") or "",
        draft_meta=dict(base_meta),
        draft_body=base_body + "\n## New facts\n\nA new fact. [source: x]\n",
    )
    state = publish_job(store, checkout, change)
    retitled_meta = {**base_meta, "title": "Existing model (operator title)"}
    retitled_body = base_body.replace("# Existing model\n", "# Existing model (operator title)\n")
    operator_commit(origin, checkout, fm.build(retitled_meta, retitled_body))

    async def merge(client, topic, change, fresh_page, corrections, **kwargs):
        return {
            "frontmatter": dict(retitled_meta),
            "body": retitled_body + "\n## New facts\n\nA new fact. [source: x]\n",
        }

    monkeypatch.setattr(reconciliation, "reconcile_stale", merge)
    await Pipeline(cfg, store, publisher=Publisher(cfg, store, forgejo))._stage_publish(state)
    change = state.changes[0]
    assert change.status == "published", (change.status, change.error)
    assert change.title == "Existing model (operator title)"
    landed_meta, landed_body = fm.parse(git_show(origin, f"main:{PAGE}"))
    assert landed_meta["title"] == "Existing model (operator title)"
    assert landed_body.startswith("# Existing model (operator title)\n") and "A new fact." in landed_body
