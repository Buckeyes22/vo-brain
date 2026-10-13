"""Article review retains distinct literal occurrences and governing ancestors."""

import copy
import json

import pytest

from doclayer.curator import curate
from doclayer.curator.editing import page_units
from doclayer.curator.state import Evidence, JobState, PageChange


class ContextModel:
    def __init__(self):
        self.frames = []

    async def chat_json(self, system, user):
        assert user.startswith("ARTICLE EDITORIAL REVIEW\n")
        assert len(system) + len(user) <= 6000
        frame = json.loads(user.split("\n", 1)[1])
        self.frames.append(frame)
        return {"complete": True, "failures": [], "conflicts": []}


def source(text):
    return Evidence(
        "policy",
        "Actual policy",
        "#Policy",
        "quote",
        text,
        quote=text,
        support_status="supported",
    )


@pytest.mark.asyncio
async def test_identical_leaf_regions_keep_distinct_ancestor_context_and_restart(store):
    prefix = "### Gateway\nGateway records traces. "
    leaf = prefix + "x" * (1000 - len(prefix) - 1) + "\n"
    body = (
        "# Policy\n## Laboratory only\nLab-only qualifier.\n"
        + leaf
        + "## Production only\nProduction qualifier.\n"
        + leaf
    )
    change = PageChange("g", "wiki/gateway.md", "update", draft_body=body)
    records = [
        source("Laboratory only: Gateway records traces under the lab policy."),
        source("Production only: Gateway records traces under the production policy."),
    ]
    state = store.create(JobState("", "", "Gateway", changes=[change]))[0]
    model = ContextModel()
    result = await curate.check_draft(
        model,
        "Gateway",
        change,
        records,
        [],
        char_budget=6000,
        progress=lambda _: store.save(state),
    )
    assert not result["failures"]
    # Identical leaves have distinct parent occurrence identities; the complete
    # original page and exact offset map reach the reviewer, without claim maps.
    shown = {section["id"]: section for request in model.frames for section in request["sections"]}
    leaves = [section for section in shown.values() if section["label"].endswith("Gateway")]
    assert len(leaves) == 2 and leaves[0]["id"] != leaves[1]["id"]
    # Wire coordinates index each complete shown task body; durable coordinates
    # index the complete canonical body and preserve occurrence identity.
    for request in model.frames:
        for section in request["sections"]:
            if section["label"].endswith("Gateway"):
                assert request["body"][section["start"] : section["end"]] == leaf
    saved_leaves = [
        section
        for section in change.check_progress["article_review"]["sections"]
        if section["label"].endswith("Gateway")
    ]
    assert len(saved_leaves) == 2
    assert all(body[section["start"] : section["end"]] == leaf for section in saved_leaves)
    assert "Laboratory only / Gateway" in leaves[0]["label"]
    assert "Production only / Gateway" in leaves[1]["label"]
    for section, qualifier in zip(leaves, ("Lab-only qualifier.", "Production qualifier.")):
        request = next(request for request in model.frames if section["id"] in request["target_section_ids"])
        assert leaf in request["body"]
        context = request["body"] + "".join(row["text"] for row in request["governing_context"])
        assert qualifier in context
    assert set(shown) == {unit.id for unit in page_units(body)}
    restored = store.load(state.job_id).changes[0]
    restarted = ContextModel()
    await curate.check_draft(restarted, "Gateway", restored, records, [], char_budget=6000)
    assert restarted.frames == []
    assert "claims" not in restored.check_progress
    changed = copy.deepcopy(restored)
    changed.draft_body = body.replace("Lab-only qualifier.", "Field-only qualifier.")
    fresh = ContextModel()
    await curate.check_draft(fresh, "Gateway", changed, records, [], char_budget=6000)
    assert fresh.frames and any("Field-only qualifier." in row["body"] for row in fresh.frames)
    assert changed.check_progress["article_review"]["binding"] != restored.check_progress["article_review"]["binding"]


@pytest.mark.asyncio
async def test_identical_leaf_occurrences_in_same_named_scope_are_not_deduplicated():
    prefix = "### Gateway\nGateway records traces. "
    leaf = prefix + "x" * (1000 - len(prefix) - 1) + "\n"
    body = "# Policy\n" + leaf + leaf
    change = PageChange("g", "wiki/gateway.md", "update", draft_body=body)
    model = ContextModel()
    await curate.check_draft(
        model,
        "Gateway",
        change,
        [source("Gateway records traces under this policy.")],
        [],
        char_budget=6000,
    )
    frame = model.frames[0]
    assert frame["body"] == body
    leaves = [section for section in frame["sections"] if section["label"].endswith("Gateway")]
    assert len(leaves) == 2 and leaves[0]["id"] != leaves[1]["id"]
    assert len({section["id"] for section in leaves} & set(frame["target_section_ids"])) == 2
    assert body[leaves[0]["start"] : leaves[0]["end"]] == body[leaves[1]["start"] : leaves[1]["end"]] == leaf
    assert len(page_units(body)) == 3
