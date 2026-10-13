"""Planner classifications, rather than generated aliases, own new-page metadata."""

import copy
import json

import httpx
import pytest
from conftest import valid_meta
from test_pipeline import git_show, run_pipeline, submit_payload_job

from doclayer import frontmatter
from doclayer.curator import placement
from doclayer.curator.model import ModelClient, ModelError
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import Evidence, PageChange, Usage


def response(value):
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(value)}, "finish_reason": "stop"}]},
    )


@pytest.mark.asyncio
async def test_invalid_planner_type_repaired_and_valid_saved_plan_replays_without_http(
    cfg,
):
    record = Evidence(
        "manual",
        "Manual",
        "Overview",
        "quote",
        "Library reads PDF text.",
        quote="Library reads PDF text.",
        support_status="supported",
    )
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        system, user = [x["content"] for x in payload["messages"]]
        assert '"product"' in system and '"general"' in system
        ids = [x["id"] for x in json.loads(user.split("Claims JSON:\n")[1])]
        seen.append(user)
        return response(
            {
                "covered": [],
                "pages": [
                    {
                        "action": "create",
                        "path": "wiki/library.md",
                        "title": "Library",
                        "subject_type": "software-library" if len(seen) == 1 else "product",
                        "rationale": "library documentation",
                        "source_ids": ["manual"],
                        "claim_ids": ids,
                        "related": [],
                    }
                ],
            }
        )

    usage = Usage()
    client = ModelClient(cfg, usage, transport=httpx.MockTransport(handler))
    plan = await placement.plan_batch(client, "Library", [], [record], [], [], char_budget=12000)
    assert usage.model_calls == 2 and plan["pages"][0]["subject_type"] == "product"
    old = copy.deepcopy(plan["ledger"])
    replay = await placement.plan_batch(client, "Library", [], [record], [], [], char_budget=12000, ledger=old)
    assert replay["pages"] == plan["pages"] and usage.model_calls == 2
    # An old admitted invalid plan and its invalid saved reply cannot bypass the enum.
    stale = copy.deepcopy(old)
    for row in stale["plans"].values():
        row["pages"][0]["subject_type"] = "software-library"
    stale["batch"]["reply"]["pages"][0]["subject_type"] = "software-library"
    corrected = await placement.plan_batch(client, "Library", [], [record], [], [], char_budget=12000, ledger=stale)
    assert corrected["pages"][0]["subject_type"] == "product" and usage.model_calls == 3
    assert old == plan["ledger"]


def test_only_validated_new_page_type_is_retained():
    new = PageChange("g", "wiki/library.md", "create", subject_type="product")
    meta = {"subject_type": "software", "title": "Library"}
    Pipeline._retain_planned_type(new, meta)
    assert meta == {"subject_type": "product", "title": "Library"}
    new.subject_type = "software-library"
    with pytest.raises(ModelError, match="valid planned"):
        Pipeline._retain_planned_type(new, meta)
    update = PageChange("g", "wiki/existing.md", "update", subject_type="product")
    canonical = {"subject_type": "procedure", "platform": "operator-owned"}
    Pipeline._retain_planned_type(update, canonical)
    assert canonical == {"subject_type": "procedure", "platform": "operator-owned"}


def test_real_library_creation_and_repair_cannot_override_planner_type(cfg, store, qwen, checkout, origin, forgejo):
    fact = "Library reads PDF text."
    qwen.overrides["extract"] = [
        json.dumps(
            {
                "doc_kind": "other",
                "claims": [
                    {
                        "claim": fact,
                        "locator": "(introduction)",
                        "modality": "quote",
                        "quote": fact,
                        "applicability": {},
                        "important": True,
                        "excluded_topic": False,
                    }
                ],
            }
        )
    ]

    def plan(user):
        selected = json.loads(user.split("Claims JSON:\n")[1])
        return {
            "covered": [],
            "pages": [
                {
                    "action": "create",
                    "path": "wiki/library.md",
                    "title": "Library",
                    "subject_type": "product",
                    "rationale": "library reference",
                    "source_ids": [x["source_id"] for x in selected],
                    "claim_ids": [x["id"] for x in selected],
                    "related": [],
                }
            ],
        }

    qwen.overrides["plan"] = [plan]

    def draft(user):
        assert "Pipeline-owned new-page subject_type: product" in user
        return {
            "frontmatter": valid_meta(subject_type="software", title="Library"),
            "body": "# Library\n\n" + fact,
            "notes": [],
        }

    qwen.overrides["draft"] = [draft]

    # Missing source citation causes a real deterministic content repair.
    def check(user):
        frame = json.loads(user.split("\n", 1)[1])
        assert frame["body"]
        return {"complete": True, "failures": [], "conflicts": []}

    qwen.overrides["check"] = [check] * 20

    def repair(user):
        assert "Pipeline-owned new-page subject_type: product" in user
        return {
            "frontmatter": valid_meta(subject_type="software", title="Library"),
            "body": "# Library\n\n" + fact + " [source: manual]",
            "notes": [],
        }

    qwen.overrides["repair"] = [repair]
    state = submit_payload_job(store, checkout, texts=[("manual", fact)], intent="Library documentation")
    import asyncio

    from doclayer.curator.publish import Publisher

    class Interrupted(Pipeline):
        async def _stage_validate(self, state):
            raise ModelError("validation outage fixture", transient=True)

    halted = asyncio.run(
        Interrupted(
            cfg,
            store,
            inference_transport=qwen.transport(),
            publisher=Publisher(cfg, store, forgejo=forgejo),
        ).run(state)
    )
    assert halted.status == "retryable" and halted.stage == "validate"
    saved = store.load(state.job_id)
    assert saved.changes[0].draft_meta["subject_type"] == "product"
    charged = saved.usage.model_calls
    done = run_pipeline(cfg, store, qwen, {}, saved, forgejo)
    assert done.usage.model_calls >= charged
    assert done.status == "published", done.error or done.summary
    landed = git_show(origin, "main:wiki/library.md")
    assert frontmatter.parse(landed)[0]["subject_type"] == "product"
    assert done.changes[0].repair_passes == 1 and fact in landed


def test_operator_update_metadata_is_not_reclassified(cfg, store, qwen, checkout, origin, forgejo):
    import asyncio

    from test_corrections import make_pipeline, retain_operator_backing

    from doclayer.curator.ops import handle

    reply = handle(
        "correct",
        {
            "page": "wiki/existing-model.md",
            "claim": "Known facts.",
            "instruction": "Keep the facts; classify this operator page as procedure.",
        },
        cfg,
        store,
    )
    state = store.load(reply["job_id"])
    retain_operator_backing(cfg, store, state)
    original = """# Existing model

## Capabilities

Known facts.

## Safety

Known safety notes.
"""
    qwen.overrides["draft"] = [
        json.dumps(
            {
                "frontmatter": valid_meta(cid="c-1111aaaa", subject_type="procedure", title="Existing model"),
                "body": original,
                "notes": [],
            }
        )
    ]
    qwen.overrides["check"] = [lambda user: {"complete": True, "failures": [], "conflicts": []}] * 20
    done = asyncio.run(make_pipeline(cfg, store, qwen, forgejo).run(state))
    assert done.status == "published", done.error or done.summary
    meta, body = frontmatter.parse(git_show(origin, "main:wiki/existing-model.md"))
    assert meta["subject_type"] == "procedure" and body.strip() == original.strip()
    assert done.changes[0].action == "update" and done.changes[0].subject_type == "general"


@pytest.mark.asyncio
async def test_saved_secondary_coverage_fallback_recertifies_invalid_type_only(cfg):
    record = Evidence(
        "manual",
        "Manual",
        "Overview",
        "quote",
        "Library reads PDF text.",
        quote="Library reads PDF text.",
        support_status="supported",
    )
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        system, user = [x["content"] for x in payload["messages"]]
        selected = json.loads(user.split("Claims JSON:\n")[1])
        seen.append(user)
        if len(seen) == 1:
            return response(
                {
                    "pages": [],
                    "covered": [{"claim_id": selected[0]["id"], "path": "wiki/absent.md"}],
                }
            )
        assert "FAILED independent canonical coverage" in system
        return response(
            {
                "covered": [],
                "pages": [
                    {
                        "action": "create",
                        "path": "wiki/library.md",
                        "title": "Library",
                        "subject_type": "product",
                        "rationale": "library facts",
                        "source_ids": ["manual"],
                        "claim_ids": [selected[0]["id"]],
                        "related": [],
                    }
                ],
            }
        )

    usage = Usage()
    client = ModelClient(cfg, usage, transport=httpx.MockTransport(handler))
    saved = []

    class PowerLoss(Exception):
        pass

    def retain(ledger):
        if ledger.get("batch", {}).get("retry"):
            saved.append(copy.deepcopy(ledger))
            raise PowerLoss

    with pytest.raises(PowerLoss):
        await placement.plan_batch(client, "Library", [], [record], [], [], char_budget=12000, progress=retain)
    assert usage.model_calls == 2 and saved
    valid_saved = copy.deepcopy(saved[-1])
    replay = await placement.plan_batch(client, "Library", [], [record], [], [], char_budget=12000, ledger=valid_saved)
    assert replay["pages"][0]["subject_type"] == "product" and usage.model_calls == 2
    invalid_saved = copy.deepcopy(saved[-1])
    invalid_saved["batch"]["retry"]["pages"][0]["subject_type"] = "software-library"
    repaired = await placement.plan_batch(
        client, "Library", [], [record], [], [], char_budget=12000, ledger=invalid_saved
    )
    assert repaired["pages"][0]["subject_type"] == "product" and usage.model_calls == 3
    assert valid_saved == saved[-1]  # No rewriting the prior persisted candidate.
