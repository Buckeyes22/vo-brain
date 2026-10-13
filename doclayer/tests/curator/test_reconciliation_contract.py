"""Strict reconciliation repair and contract-bound partial checkpoints."""

import copy
import json
import re

import pytest

from doclayer import frontmatter
from doclayer.curator import reconciliation
from doclayer.curator.model import ModelError
from doclayer.curator.state import PageChange


class Script:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    async def chat_bounded_json(self, system, user, *, char_budget):
        assert len(system) + len(user) <= char_budget
        self.calls.append((system, user))
        reply = next(self.replies)
        return reply(user) if callable(reply) else copy.deepcopy(reply)


def edit(user, append="Supported contribution."):
    return {
        "section_id": re.search(r"Section ID: (\S+)", user)[1],
        "edits": [],
        "append": append,
        "handled_claim_ids": [],
        "claim_dispositions": [],
    }


@pytest.mark.asyncio
async def test_invalid_cached_merge_slot_reasks_without_losing_other_progress():
    current = "# Device\nOperator qualification is retained.\n"
    proposed = "Additional supported contribution.\n"
    change = PageChange("group", "wiki/device.md", "update")
    saved = {"independent-accepted-slot": "preserved bytes"}
    first = Script([lambda u: edit(u, "Supported detail. " * 165)])
    await reconciliation._merge_material(
        first, "topic", change, current, proposed, [], 32768, "binding", saved, None, "overlap", protected=current
    )
    old = dict(saved)
    assert len(json.dumps(json.loads(next(v for k, v in saved.items() if k.startswith("overlap"))))) > 2250
    repaired = Script([edit])
    checkpoints = []
    result = await reconciliation._merge_material(
        repaired,
        "topic",
        change,
        current,
        proposed,
        [],
        4500,
        "binding",
        saved,
        lambda p: checkpoints.append(copy.deepcopy(p)),
        "overlap",
        protected=current,
    )
    assert len(repaired.calls) == 1
    assert "Previous edit failed" in repaired.calls[0][1]
    assert "2250" in repaired.calls[0][1]
    assert saved["independent-accepted-slot"] == old["independent-accepted-slot"]
    assert current in result
    assert any(len(p["results"]) == 1 for p in checkpoints)


@pytest.mark.asyncio
async def test_context_output_contract_invalidates_collision_progress():
    body = "# Device\nOperator qualification retained.\n"
    change = PageChange(
        "group", "wiki/device.md", "create", title="Device", draft_body="# Device\nSupported contribution.\n"
    )
    raw = frontmatter.build({"title": "Device", "concept_id": "c-operator"}, body)

    class Client:
        def __init__(self):
            self.calls = []

        async def chat_bounded_json(self, system, user, *, char_budget):
            self.calls.append((system, user))
            assert len(system) + len(user) <= char_budget
            if 'Return {"resolution"' in user:
                return {"resolution": "same-subject", "explanation": "same qualified subject"}
            return edit(user)

    client = Client()
    first = await reconciliation.reconcile_collision(client, "topic", change, raw, char_budget=12000)
    change.reconcile_progress = first["reconcile_progress"]
    count = len(client.calls)
    second = await reconciliation.reconcile_collision(client, "topic", change, raw, char_budget=4500)
    assert first["reconcile_progress"]["binding"] != second["reconcile_progress"]["binding"]
    assert len(client.calls) > count
    assert body in second["body"]
    assert second["frontmatter"]["concept_id"] == "c-operator"


@pytest.mark.asyncio
async def test_subject_decision_repairs_schema_and_size_on_same_complete_views():
    change = PageChange("group", "wiki/device.md", "create", title="Device", draft_body="# Device\nEnterprise only.\n")
    raw = frontmatter.build({"title": "Device", "platform": "different"}, "# Device\nConsumer only.\n")
    client = Script(
        [
            {"resolution": "merge", "explanation": 4},
            {"resolution": "distinct-subject", "explanation": "x" * 2100},
            {"resolution": "distinct-subject", "explanation": "different platforms"},
        ]
    )
    result = await reconciliation.reconcile_collision(client, "topic", change, raw, char_budget=7000)
    assert result["resolution"] == "distinct-subject"
    assert result["body"] == change.draft_body
    assert len(client.calls) == 3
    original = client.calls[0][1]
    assert original in client.calls[1][1] and original in client.calls[2][1]
    assert "resolution:" in client.calls[1][1] and "explanation:" in client.calls[1][1]
    assert "1750" in client.calls[2][1]


@pytest.mark.asyncio
async def test_invalid_subject_responses_remain_retryable_without_completed_verdict():
    change = PageChange("group", "wiki/device.md", "create", title="Device", draft_body="# Device\nEnterprise only.\n")
    raw = frontmatter.build({"title": "Device"}, "# Device\nConsumer only.\n")
    client = Script([{}, {}, {}])
    saved = []
    with pytest.raises(ModelError) as error:
        await reconciliation.reconcile_collision(client, "topic", change, raw, char_budget=7000, progress=saved.append)
    assert error.value.transient
    assert error.value.diagnostic["attempts"] == 3
    assert not saved


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [["same-subject"], {"resolution": "same-subject"}])
async def test_subject_resolution_containers_are_repaired_instead_of_crashing(invalid):
    change = PageChange("group", "wiki/device.md", "create", title="Device", draft_body="# Device\nEnterprise only.\n")
    raw = frontmatter.build({"title": "Device"}, "# Device\nConsumer only.\n")
    client = Script(
        [
            {"resolution": invalid, "explanation": "candidate"},
            {"resolution": "distinct-subject", "explanation": "different qualified subjects"},
        ]
    )
    result = await reconciliation.reconcile_collision(client, "topic", change, raw, char_budget=7000)
    assert result["resolution"] == "distinct-subject"
    assert len(client.calls) == 2
    assert "resolution:" in client.calls[1][1]
    assert result["body"] == change.draft_body


@pytest.mark.asyncio
async def test_reconciliation_uses_source_free_failed_edit_field_feedback():
    current = "# Device\nOnly enterprise deployments in region one.\n"
    change = PageChange("group", "wiki/device.md", "update")

    def invalid(user):
        result = edit(user, "")
        result["edits"] = [{"old": "private absent anchor", "new": "private replacement"}]
        return result

    client = Script([invalid, edit])
    result = await reconciliation._merge_material(
        client,
        "topic",
        change,
        current,
        "Supported contribution.\n",
        [],
        7000,
        "binding",
        {},
        None,
        "overlap",
        protected=current,
    )
    assert len(client.calls) == 2
    assert "edits[0].old" in client.calls[1][1]
    assert "private absent anchor" not in client.calls[1][1]
    assert "private replacement" not in client.calls[1][1]
    assert current in result
