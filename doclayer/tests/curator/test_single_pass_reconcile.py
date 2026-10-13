"""Single-pass stale reconciliation: deterministic three-way merge, no model."""

import pytest

from doclayer import frontmatter
from doclayer.curator import reconciliation
from doclayer.curator.state import PageChange

BASE = "# P\n\nA.\n\nB.\n"


def change(body):
    return PageChange("group", "wiki/p.md", "update", title="P", draft_body=body)


def page(body):
    return frontmatter.build({"title": "P", "concept_id": "c-operator", "privacy": "internal"}, body)


class NoModel:
    def __init__(self):
        self.calls = 0

    async def chat_bounded_json(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("single-pass reconciliation must not invoke a model")


@pytest.mark.asyncio
async def test_reconcile_without_model_keeps_fresh_overlap():
    fresh = BASE.replace("A.", "A2.")
    draft = "# P\n\nA.\nA addition.\n\nB.\nB addition.\n"
    client = NoModel()
    result = await reconciliation.reconcile_stale(
        client,
        "topic",
        change(draft),
        page(fresh),
        [],
        base_page=page(BASE),
        char_budget=7000,
        model_merge=False,
    )
    assert "A2." in result["body"]
    assert "B addition." in result["body"]
    assert "A addition." not in result["body"]
    assert len(result["dropped"]) == 1
    assert "A addition." in result["dropped"][0]["text"]
    assert client.calls == 0


@pytest.mark.asyncio
async def test_reconcile_non_overlap_merges_both_sides():
    fresh = BASE.replace("A.", "A2.")
    draft = BASE.replace("B.", "B.\nB addition.")
    client = NoModel()
    result = await reconciliation.reconcile_stale(
        client,
        "topic",
        change(draft),
        page(fresh),
        [],
        base_page=page(BASE),
        char_budget=7000,
        model_merge=False,
    )
    assert "A2." in result["body"] and "B addition." in result["body"]
    assert result["dropped"] == []
    assert client.calls == 0


@pytest.mark.asyncio
async def test_reconcile_without_model_resume_records_one_drop():
    fresh = BASE.replace("A.", "A2.")
    draft = "# P\n\nA.\nA addition.\n\nB.\nB addition.\n"
    saved = []
    first = change(draft)
    kwargs = dict(base_page=page(BASE), char_budget=7000, model_merge=False)
    client = NoModel()
    result = await reconciliation.reconcile_stale(
        client, "topic", first, page(fresh), [], progress=saved.append, **kwargs
    )
    assert saved
    resumed = change(draft)
    resumed.reconcile_progress = saved[-1]
    again = await reconciliation.reconcile_stale(client, "topic", resumed, page(fresh), [], **kwargs)
    assert len(again["dropped"]) == 1 and len(result["dropped"]) == 1
    assert again["body"] == result["body"]
    assert client.calls == 0


def test_binding_includes_model_merge_and_keeps_model_merge_hash():
    import hashlib
    import json

    c = change("# P\n\nA.\n")
    args = ("topic", c, page(BASE), page(BASE), [], 7000)
    legacy = [
        reconciliation.VERSION,
        {
            "context_chars": 7000,
            "view_chars": max(256, min(2000, 7000 // 6)),
            "edit_output_chars": min(6000, max(256, 7000 // 2)),
            "subject_output_chars": max(256, 7000 // 4),
        },
        "topic",
        c.path,
        c.title,
        c.subject_type,
        c.rationale,
        page(BASE),
        page(BASE),
        c.draft_body,
        c.draft_meta,
        {},
        [],
    ]
    expected = hashlib.sha256(json.dumps(legacy, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    assert reconciliation._binding(*args) == expected
    assert reconciliation._binding(*args, model_merge=True) == expected
    assert reconciliation._binding(*args, model_merge=False) != expected
