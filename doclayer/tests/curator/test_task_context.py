"""Import reminders cannot pass publication as a warning about subject facts."""

import ast
import json
import re

import httpx
import pytest

from doclayer import frontmatter
from doclayer.curator import curate, editing
from doclayer.curator.config import CuratorConfig
from doclayer.curator.model import ModelClient
from doclayer.curator.state import Evidence, PageChange, Usage


@pytest.mark.asyncio
async def test_located_task_reminder_warning_blocks_and_survives_resume(tmp_path):
    cfg = CuratorConfig()
    cfg.credentials_file = str(tmp_path / "no-credentials")
    cfg.inference.retries = 0
    usage = Usage()
    anchor = "Database component variables must not be inferred from environment URLs."
    body = "# Event CMS\n\nCMS writes to the dev branch.\n\n## Notes\n\n" + anchor + "\n"
    frames = []

    def http(request):
        frame = json.loads(json.loads(request.content)["messages"][-1]["content"].split("\n", 1)[1])
        frames.append(frame)
        offset = frame["body"].index(anchor)
        target = next(section for section in frame["sections"] if section["start"] <= offset < section["end"])
        reply = {
            "complete": True,
            "failures": [
                {
                    "severity": "warning",
                    "check": "task-scaffolding",
                    "detail": "An import reminder about another subject is not CMS knowledge.",
                    "section_id": target["id"],
                    "anchor": anchor,
                }
            ],
            "conflicts": [],
        }
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}]}
        )

    model = ModelClient(cfg, usage, transport=httpx.MockTransport(http))
    change = PageChange(
        "g", "wiki/event-cms.md", "create", title="Event CMS", draft_body=body, draft_meta={"title": "Event CMS"}
    )
    result = await curate.check_draft(
        model, "Document event CMS; use disposable database fixtures for imports.", change, [], [], char_budget=12000
    )
    assert usage.model_calls == 1 and result["complete"]
    assert result["failures"][0]["severity"] == "blocker"
    assert result["failures"][0]["anchor"] == anchor
    fresh = ModelClient(cfg, usage, transport=httpx.MockTransport(http))
    replay = await curate.check_draft(
        fresh, "Document event CMS; use disposable database fixtures for imports.", change, [], [], char_budget=12000
    )
    assert usage.model_calls == 1
    assert replay["failures"][0]["severity"] == "blocker"
    assert len(frames) == 1


@pytest.mark.asyncio
async def test_changed_writer_policy_rechecks_completed_section_without_duplication(cfg, monkeypatch):
    usage = Usage()
    fact = "CMS writes directly to the dev branch."
    record = Evidence(
        "s",
        "CMS config",
        "## Publishing",
        "quote",
        fact,
        quote=fact,
        support_status="supported",
        url="https://cms.example/config",
    )

    def http(request):
        payload = json.loads(request.content)
        user = payload["messages"][-1]["content"]
        if user.startswith("BOUNDED EDIT CLAIM ROUTING"):
            frame = json.loads(user.split("\n", 1)[1].split("\nEvidence:\n", 1)[0])
            identity = re.search(r"^- \[(claim-[^ /]+)", user, re.M)[1]
            reply = {
                "routes": [
                    {
                        "id": identity,
                        "section_id": frame["sections"][0]["section_id"],
                        "new_heading": None,
                        "rationale": "CMS publication configuration.",
                    }
                ]
            }
        else:
            assert editing.SUBJECT_CONTENT_POLICY in payload["messages"][0]["content"]
            identities = ast.literal_eval(re.search(r"Claim IDs to integrate or prove already present: (.+)", user)[1])
            view = user.split("Current section view:\n", 1)[1].split("\nClaim IDs", 1)[0]
            present = fact in view
            reply = {
                "section_id": re.search(r"Section ID: (.+)", user)[1],
                "edits": [],
                "append": "" if present else fact + " [Source](https://cms.example/config)",
                "handled_claim_ids": identities,
                "claim_dispositions": [
                    {"id": identities[0], "status": "already_present" if present else "integrated", "anchor": fact}
                ],
                "notes": [],
            }
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}]}
        )

    client = ModelClient(cfg, usage, transport=httpx.MockTransport(http))
    change = PageChange("g", "wiki/event-cms.md", "update", title="CMS")
    first = await editing.edit_page(
        client, "CMS", change, [record], "# CMS\nOperator fact.\n", [], char_budget=12000, today="2026-10-04"
    )
    assert usage.model_calls == 2
    change.draft_progress = first["draft_progress"]
    raw = frontmatter.build(first["frontmatter"], first["body"])
    replay = await editing.edit_page(client, "CMS", change, [record], raw, [], char_budget=12000, today="2026-10-04")
    assert usage.model_calls == 2 and replay["body"] == first["body"]
    monkeypatch.setattr(editing, "SUBJECT_CONTENT_POLICY", editing.SUBJECT_CONTENT_POLICY + " Keep source scope. ")
    updated = await editing.edit_page(client, "CMS", change, [record], raw, [], char_budget=12000, today="2026-10-04")
    assert usage.model_calls == 4
    assert updated["draft_progress"]["selection_hash"] != first["draft_progress"]["selection_hash"]
    assert updated["body"] == first["body"] and updated["body"].count(fact) == 1
