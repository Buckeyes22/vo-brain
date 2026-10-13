"""Corrections: operator corrections persist beyond jobs, correction jobs
publish, and replaying unchanged source evidence cannot restore a corrected
claim."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from conftest import valid_meta

from doclayer.curator.degrade import GAP_MARK
from doclayer.curator.ops import handle
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.publish import Publisher
from doclayer.curator.state import JobState


def git_show(repo: Path, ref_path: str) -> str | None:
    proc = subprocess.run(["git", "-C", str(repo), "show", ref_path], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def head_of(checkout: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def make_pipeline(cfg, store, qwen, forgejo=None):
    return Pipeline(
        cfg,
        store,
        inference_transport=qwen.transport(),
        publisher=Publisher(cfg, store, forgejo=forgejo) if forgejo else None,
    )


def retain_operator_backing(cfg, store, state):
    """The full old-page audit uses explicit retained original facts, not Git prose."""
    from doclayer.curator.state import Evidence, SourceRecord

    source = SourceRecord("operator-manual", "text", "operator manual", status="fetched")
    facts = [
        "Known facts.",
        "Known safety notes.",
        "The documented context window is 128k tokens, not unlimited.",
    ]
    state.sources.append(source)
    Pipeline(cfg, store)._store_extract(state, source, "# Manual\n" + "\n".join(facts))
    store.append_evidence(
        state.job_id,
        [Evidence(source.id, source.label, "#Manual", "quote", fact, quote=fact).to_dict() for fact in facts],
    )
    store.save(state)


def plan_for(path: str):
    from test_pipeline import placement_reply

    return placement_reply(
        {
            "already_covered": False,
            "explanation": "updating",
            "pages": [
                {
                    "action": "update",
                    "path": path,
                    "title": "Existing model",
                    "subject_type": "model",
                    "rationale": "revision",
                    "related": [],
                    "source_ids": [],
                }
            ],
            "consolidation_opportunities": [],
        }
    )


def test_correction_reads_fresh_canonical_page_not_deployment_head(cfg, store, checkout, origin, tmp_path):
    from conftest import run_git

    writer = tmp_path / "canonical-writer"
    run_git(tmp_path, "clone", "-q", str(origin), str(writer))
    run_git(writer, "config", "user.email", "fixture@example.invalid")
    run_git(writer, "config", "user.name", "fixture")
    page = "wiki/new-since-deployment.md"
    (writer / page).write_text((writer / "wiki/other-page.md").read_text())
    run_git(writer, "add", page)
    run_git(writer, "commit", "-qm", "new canonical page")
    fresh = run_git(writer, "rev-parse", "HEAD")
    run_git(writer, "push", "-q", "origin", "main")
    assert head_of(checkout) != fresh
    reply = handle(
        "correct",
        {"page": page, "claim": "old", "instruction": "correct this fact"},
        cfg=cfg,
        store=store,
    )
    job = store.load(reply["job_id"])
    assert job.base_commit == fresh
    assert job.changes[0].action == "update"
    assert head_of(checkout) != fresh


def test_correction_job_publishes_instruction(cfg, store, qwen, checkout, origin, forgejo) -> None:
    reply = handle(
        "correct",
        {
            "page": "wiki/existing-model.md",
            "claim": "the context window is unlimited",
            "instruction": "the documented context window is 128k tokens, not unlimited",
        },
        cfg=cfg,
        store=store,
    )
    assert reply["ok"]
    job = store.load(reply["job_id"])
    retain_operator_backing(cfg, store, job)
    # the correction instruction is the draft intent; corrections are binding
    corrected_draft = {
        "frontmatter": dict(valid_meta(cid="c-1111aaaa"), title="Existing model"),
        "body": (
            "# Existing model\n\n## Capabilities\n\nKnown facts.\n\n## Safety\n\n"
            "Known safety notes.\n\n## Corrections\n\nThe documented context window is "
            "128k tokens, not unlimited. [source: operator correction]\n"
        ),
    }
    qwen.overrides["draft"] = [json.dumps(corrected_draft)]
    import asyncio

    done = asyncio.run(make_pipeline(cfg, store, qwen, forgejo).run(job))
    assert done.status == "published", done.summary or done.error
    landed = git_show(origin, "main:wiki/existing-model.md")
    assert "128k tokens" in landed
    # correction survives the job's completion (durable, keyed to the page)
    assert store.corrections(page="wiki/existing-model.md")


def test_replaying_corrected_claim_is_blocked_then_repaired(cfg, store, qwen, checkout, origin, forgejo) -> None:
    handle(
        "correct",
        {
            "page": "wiki/existing-model.md",
            "claim": "The model supports unlimited context on all tiers",
            "instruction": "the documented context window is 128k tokens",
        },
        cfg=cfg,
        store=store,
    )
    # a later import drafts the corrected claim verbatim (replay of old source)
    reinstating = {
        "frontmatter": dict(valid_meta(cid="c-1111aaaa"), title="Existing model"),
        "body": (
            "# Existing model\n\n## Capabilities\n\nThe model supports unlimited "
            "context on all tiers per the source. [source: stale-docs]\n\n## Safety\n\n"
            "Known safety notes.\n"
        ),
    }
    fixed = {
        "frontmatter": dict(valid_meta(cid="c-1111aaaa"), title="Existing model"),
        "body": (
            "# Existing model\n\n## Capabilities\n\nThe documented context window is "
            "128k tokens, not unlimited. [source: operator correction]\n\nKnown facts. [source: operator manual]\n\nThe model supports a 128,000-token maximum output. [source: stale-docs]\n\n## Safety\n\nKnown safety notes.\n"
        ),
    }
    qwen.overrides["plan"] = [plan_for("wiki/existing-model.md")]
    qwen.overrides["draft"] = [json.dumps(reinstating)]
    # The actual deterministic correction guard rejects the replayed draft.
    # Do not script an extra false rejection of the already repaired body.
    qwen.overrides["repair"] = [
        json.dumps(fixed),
        json.dumps(fixed),
    ]  # bounded repair: up to 2 passes

    state = JobState(
        job_id="",
        idempotency_key="replay-test",
        intent="re-import old material about existing-model",
        base_commit=head_of(checkout),
    )
    submitted = handle(
        "submit",
        {
            "mode": "multi-stage",  # scripts the multi-stage plan/draft/repair tasks
            "intent": state.intent,
            "texts": [
                {
                    "label": "stale-docs",
                    "text": "The model supports a 128,000-token maximum output. "
                    + "The model supports unlimited context on all tiers. " * 30,
                }
            ],
        },
        cfg=cfg,
        store=store,
    )
    state = store.load(submitted["job_id"])
    retain_operator_backing(cfg, store, state)

    import asyncio

    done = asyncio.run(make_pipeline(cfg, store, qwen, forgejo).run(state))
    assert done.status == "published", done.summary or done.error
    landed = git_show(origin, "main:wiki/existing-model.md")
    assert "unlimited context" not in landed
    assert "128k tokens" in landed
    # the repair pass actually ran
    assert done.changes[0].repair_passes >= 1


def test_no_automatic_delete_or_rename_from_imports(cfg, store, qwen, checkout, origin, forgejo) -> None:
    """First-release imports create/revise; they must never delete or rename
    canonical pages — the plan stage is the only source of destinations and
    the destination scope check rejects anything else."""
    qwen.overrides["plan"] = [
        json.dumps(
            {
                "already_covered": False,
                "explanation": "attempting weird structural ops",
                "pages": [
                    {
                        "action": "delete",
                        "path": "wiki/other-page.md",
                        "title": "x",
                        "subject_type": "general",
                        "rationale": "cleanup",
                        "related": [],
                        "source_ids": [],
                    },
                    {
                        "action": "create",
                        "path": "sources/imported/thing.md",
                        "title": "outside scope",
                        "subject_type": "general",
                        "rationale": "r",
                        "related": [],
                        "source_ids": [],
                    },
                ],
                "consolidation_opportunities": [],
            }
        )
    ] * 3
    state = JobState(
        job_id="",
        idempotency_key="no-delete",
        intent="material",
        base_commit=head_of(checkout),
    )
    result = handle(
        "submit",
        {
            "mode": "multi-stage",  # scripts the multi-stage plan/draft/repair tasks
            "intent": state.intent,
            "texts": [
                {
                    "label": "manual",
                    "text": "The model supports a 128,000-token maximum output. " + "manual detail " * 60,
                }
            ],
        },
        cfg=cfg,
        store=store,
    )
    state = store.load(result["job_id"])
    import asyncio

    done = asyncio.run(make_pipeline(cfg, store, qwen, forgejo).run(state))
    # Invalid structural actions fail the exact bounded placement schema;
    # nothing is published and canonical content stays untouched. The
    # rejected placement drops just that claim as a current gap (one policy
    # for rejected replies, see degrade.py); the job reports instead of stopping.
    assert git_show(origin, "main:wiki/other-page.md") is not None
    assert all(c.status != "published" for c in done.changes)
    assert done.status == "partial" and done.error is None
    assert any(GAP_MARK in gap and "invalid bounded placement" in gap for gap in done.coverage_gaps)


def test_valid_placement_cannot_publish_outside_curator_destinations(cfg, store, qwen, checkout, origin, forgejo):
    from test_pipeline import placement_reply

    qwen.overrides["plan"] = [
        placement_reply(
            {
                "pages": [
                    {
                        "action": "create",
                        "path": "sources/imported/thing.md",
                        "title": "Outside scope",
                        "subject_type": "general",
                        "rationale": "source facts",
                        "source_ids": [],
                        "related": [],
                    }
                ]
            }
        )
    ]
    submitted = handle(
        "submit",
        {
            "mode": "multi-stage",  # scripts the multi-stage plan/draft/repair tasks
            "intent": "model material",
            "texts": [
                {
                    "label": "manual",
                    "text": "The model supports a 128,000-token maximum output. " + "manual detail " * 60,
                }
            ],
        },
        cfg=cfg,
        store=store,
    )
    state = store.load(submitted["job_id"])
    import asyncio

    done = asyncio.run(make_pipeline(cfg, store, qwen, forgejo).run(state))
    assert done.changes and all(change.status != "published" for change in done.changes)
    assert git_show(origin, "main:sources/imported/thing.md") is None
    assert git_show(origin, "main:wiki/other-page.md") is not None
    assert any("path" in str(failure).casefold() for change in done.changes for failure in change.validation)


def test_multifacet_operator_correction_requires_missing_addition_before_git_publication(
    cfg, store, qwen, checkout, origin, forgejo
):
    import asyncio
    import re

    instruction = "Correct the scope introduction AND add runtime files retained for 20 minutes by CI."
    reply = handle(
        "correct",
        {"page": "wiki/existing-model.md", "instruction": instruction},
        cfg=cfg,
        store=store,
    )
    job = store.load(reply["job_id"])
    retain_operator_backing(cfg, store, job)
    partial = "# Existing model\n\n## Capabilities\n\nKnown facts.\n\nScope introduction corrected.\n\n## Safety\n\nKnown safety notes.\n"
    qwen.overrides["draft"] = [
        json.dumps(
            {
                "frontmatter": dict(valid_meta(cid="c-1111aaaa"), title="Existing model"),
                "body": partial,
            }
        )
    ]
    missing = "Runtime files are retained for 20 minutes by CI. [source: operator correction]"
    reviewed = []

    def check(user):
        frame = json.loads(user.split("\n", 1)[1])
        assert frame["corrections"][0]["instruction"] == instruction
        if frame["mode"] == "correction":
            reviewed.append(missing in frame["body"])
            if missing not in frame["body"]:
                section = next(s for s in frame["sections"] if s["label"].endswith("Capabilities"))
                return {
                    "complete": True,
                    "failures": [
                        {
                            "severity": "warning",
                            "check": "correction-completion",
                            "detail": "Add the requested runtime files retained for 20 minutes by CI.",
                            "section_id": section["id"],
                            "anchor": None,
                        }
                    ],
                    "conflicts": [],
                }
        return {"complete": True, "failures": [], "conflicts": []}

    def edit(user):
        assert "correction-completion" in user and instruction in user
        assert missing not in (git_show(origin, "main:wiki/existing-model.md") or "")
        return {
            "section_id": re.search(r"Section ID: (.+)", user)[1],
            "edits": [],
            "append": missing,
            "handled_claim_ids": [],
            "claim_dispositions": [],
            "notes": [],
        }

    qwen.overrides["check"] = [check] * 20
    qwen.overrides["unknown"] = [edit]
    done = asyncio.run(make_pipeline(cfg, store, qwen, forgejo).run(job))
    assert done.status == "published", done.error or done.summary
    assert done.changes[0].repair_passes == 1
    assert reviewed[0] is False and all(reviewed[1:])
    landed = git_show(origin, "main:wiki/existing-model.md")
    assert "Scope introduction corrected." in landed and missing in landed
    calls = len(qwen.calls)
    completed = asyncio.run(make_pipeline(cfg, store, qwen, forgejo).run(store.load(done.job_id)))
    assert completed.status == "published" and len(qwen.calls) == calls
