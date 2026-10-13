"""Bounded model-selected movement cannot change canonical units or invent facts."""

import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from doclayer import frontmatter
from doclayer.curator import editing
from doclayer.curator.model import ModelClient, ModelError
from doclayer.curator.state import JobState, PageChange

ORIGINAL = "# Device\n## Skills\nCanonical skills.\n## Architecture\n### State\nCanonical state.\n## Tools\n### Shell\nCanonical shell.\n## Notes\nRepeated original one.\n## Notes\nRepeated original two.\n"
INTRO = "### Runtime\nAn exact qualified runtime fact. [Source](https://example.test/runtime)\n"
DUPLICATE = "#### Runtime\nAn exact qualified runtime fact. [Source](https://example.test/runtime)\n"
BODY = ORIGINAL.replace("## Architecture", INTRO + "## Architecture").replace(
    "## Notes\nRepeated original one.", DUPLICATE + "## Notes\nRepeated original one."
)


def prepared(body=BODY, original=ORIGINAL):
    units = editing.page_units(body)
    section = next(u for u in units if u.label.endswith(" / State"))
    rows = []
    offset = 0
    for unit in units:
        rows.append({"id": unit.id, "label": unit.label, "start": offset, "end": offset + len(unit.text)})
        offset += len(unit.text)
    change = PageChange(
        "g",
        "wiki/device.md",
        "update",
        draft_meta={"title": "Device"},
        draft_body=body,
        base_commit="a" * 40,
        base_blob="b" * 40,
    )
    change.check_progress = {
        "article_review": {
            "editorial_version": 1,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "sections": rows,
            "failures": [
                {
                    "severity": "blocker",
                    "check": "correction-completion",
                    "detail": "Move the single Runtime subsection immediately after State; remove the introduced duplicate.",
                    "section_id": section.id,
                    "anchor": None,
                }
            ],
        },
        "existing_preservation": {
            "editorial_version": 1,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "sections": rows,
            "failures": [],
            "original_body": original,
            "original_meta": {"title": "Device"},
            "canonical_binding": {"commit": change.base_commit, "blob": change.base_blob, "path": change.path},
        },
    }
    change.draft_progress = {"completed_task_ids": ["old-accepted-task"]}
    return change


def operations(frame):
    added = frame["introduced_units"]
    target = frame["destinations"][0]["id"]
    return {
        "operations": [
            {"action": "move", "section_id": added[0]["id"], "after_section_id": target, "level": 3},
            {"action": "remove", "section_id": added[1]["id"], "after_section_id": None, "level": None},
        ],
        "notes": ["Relocated exact job-added text; canonical content retained."],
    }


async def run(client, change, progress=None, budget=12000):
    return await editing.edit_page(
        client,
        "Place Runtime immediately after State",
        change,
        [],
        frontmatter.build(change.draft_meta, change.draft_body),
        [{"instruction": "Place Runtime immediately after State; one copy."}],
        char_budget=budget,
        today="2026-10-04",
        repair=True,
        progress=progress,
    )


@pytest.mark.asyncio
async def test_model_relocation_removes_only_introduced_duplicate_and_preserves_repeated_originals(cfg, store):
    cfg.limits.max_model_calls = 1
    requests = []

    def http(request):
        payload = json.loads(request.content)
        frame = json.loads(payload["messages"][1]["content"].split("\n", 1)[1])
        requests.append(frame)
        assert set(u["text"] for u in frame["introduced_units"]) == {INTRO, DUPLICATE}
        assert frame["destinations"][0]["text"] == "### State\nCanonical state.\n"
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(operations(frame))}, "finish_reason": "stop"}]}
        )

    state = store.create(JobState("", "structural-restart", "Repair placement"))[0]
    state.changes = [prepared()]

    def persist(result):
        c = state.changes[0]
        c.draft_body = result["body"]
        c.draft_progress = result["draft_progress"]
        store.save(state)
        if c.draft_progress.get("structural_repair", {}).get("status") == "accepted":
            raise RuntimeError("Power loss after durable structural result")

    model = ModelClient(cfg, usage=state.usage, transport=httpx.MockTransport(http))
    with pytest.raises(RuntimeError, match="Power loss"):
        await run(model, state.changes[0], persist)
    expected = ORIGINAL.replace("## Tools", INTRO + "## Tools")
    assert state.changes[0].draft_body == expected and state.usage.model_calls == 1
    assert state.changes[0].draft_progress["completed_task_ids"] == ["old-accepted-task"]
    restored = store.load(state.job_id)
    replay = await run(ModelClient(cfg, usage=restored.usage, transport=httpx.MockTransport(http)), restored.changes[0])
    assert replay["body"] == expected and len(requests) == 1 and restored.usage.model_calls == 1
    assert [u.text for u in editing.page_units(expected) if u.id in {v.id for v in editing.page_units(ORIGINAL)}] == [
        u.text for u in editing.page_units(ORIGINAL)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", ["unknown", "canonical_remove", "canonical_move", "wrong_destination", "wrong_level", "source_text"]
)
async def test_structural_reply_cannot_select_canonical_or_unknown_units_or_write_facts(bad):
    change = prepared()
    original_body = change.draft_body

    class Client:
        async def chat_json(self, system, user):
            frame = json.loads(user.split("\n", 1)[1])
            reply = operations(frame)
            if bad == "unknown":
                reply["operations"][0]["section_id"] = "unknown"
            elif bad.startswith("canonical_"):
                reply["operations"][0] = {
                    "action": bad.split("_")[1],
                    "section_id": frame["destinations"][0]["id"],
                    "after_section_id": None,
                    "level": None,
                }
            elif bad == "wrong_destination":
                reply["operations"][0]["after_section_id"] = "unknown"
            elif bad == "wrong_level":
                reply["operations"][0]["level"] = True
            else:
                reply["operations"][0]["replacement"] = "Invented fact."
            return reply

    with pytest.raises(ModelError, match="structural"):
        await run(Client(), change)
    assert change.draft_body == original_body


@pytest.mark.asyncio
async def test_missing_original_mapping_and_complete_unit_overflow_refuse_before_dispatch():
    class Client:
        async def chat_json(self, *args):
            raise AssertionError("No request permitted")

    changed = prepared(BODY.replace("## Skills", "## Renamed Skills"))
    with pytest.raises(ModelError, match="original heading mapping"):
        await run(Client(), changed)
    huge = prepared(BODY.replace("An exact qualified runtime fact.", "Complete unchanged added content. " * 400))
    with pytest.raises(ModelError, match="complete structural repair units"):
        await run(Client(), huge, budget=6000)


def test_structural_repair_finishes_multifacet_correction_through_real_git(cfg, store, qwen, checkout, origin, forgejo):
    import asyncio

    from conftest import valid_meta
    from test_corrections import git_show, make_pipeline, retain_operator_backing

    from doclayer.curator.ops import handle

    fact = "Runtime files expire after 20 minutes of inactivity. [source: operator correction]"
    instruction = "Add exactly one Runtime subsection immediately after Capabilities, keeping the supplied 20-minute inactivity qualification."
    reply = handle("correct", {"page": "wiki/existing-model.md", "instruction": instruction}, cfg=cfg, store=store)
    state = store.load(reply["job_id"])
    retain_operator_backing(cfg, store, state)
    original = "# Existing model\n\n## Capabilities\n\nKnown facts.\n\n## Safety\n\nKnown safety notes.\n"
    partial = (
        original.replace("## Capabilities", "### Runtime\n\n" + fact + "\n\n## Capabilities")
        + "\n### Runtime\n\n"
        + fact
        + "\n"
    )
    qwen.overrides["draft"] = [
        json.dumps({"frontmatter": dict(valid_meta(cid="c-1111aaaa"), title="Existing model"), "body": partial})
    ]
    checked = []

    def review(user):
        frame = json.loads(user.split("\n", 1)[1])
        if frame["mode"] == "correction":
            placed = (
                frame["body"].count("Runtime files expire") == 1
                and "Known facts.\n\n## Runtime\n\n" + fact in frame["body"]
            )
            checked.append(placed)
            if not placed:
                target = next(u for u in frame["sections"] if u["label"].endswith("Capabilities"))
                return {
                    "complete": True,
                    "failures": [
                        {
                            "severity": "blocker",
                            "check": "correction-completion",
                            "detail": "Relocate the introduced Runtime section after Capabilities and remove its introduced duplicate.",
                            "section_id": target["id"],
                            "anchor": None,
                        }
                    ],
                    "conflicts": [],
                }
        return {"complete": True, "failures": [], "conflicts": []}

    def structural(user):
        assert user.startswith("BOUNDED INTRODUCED SECTION REPAIR\n")
        frame = json.loads(user.split("\n", 1)[1])
        assert all(fact in u["text"] for u in frame["introduced_units"])
        assert fact not in git_show(origin, "main:wiki/existing-model.md")
        selected = operations(frame)
        selected["operations"][0]["level"] = 2
        return selected

    qwen.overrides["check"] = [review] * 20
    qwen.overrides["unknown"] = [structural]
    done = asyncio.run(make_pipeline(cfg, store, qwen, forgejo).run(state))
    assert done.status == "published", done.error or done.summary
    assert done.changes[0].repair_passes == 1 and checked[0] is False and all(checked[1:])
    landed = git_show(origin, "main:wiki/existing-model.md")
    assert landed.count("Runtime files expire") == 1 and "Known facts.\n\n## Runtime\n\n" + fact in landed
    assert "Known safety notes." in landed and "## Capabilities" in landed and "## Safety" in landed


@pytest.mark.asyncio
async def test_accepted_structure_is_not_reused_for_changed_correction_binding():
    change = prepared()

    class Client:
        calls = 0

        async def chat_json(self, system, user):
            self.calls += 1
            return operations(json.loads(user.split("\n", 1)[1]))

    client = Client()
    result = await run(client, change)
    change.draft_body = result["body"]
    change.draft_progress = result["draft_progress"]
    with pytest.raises(ModelError, match="body binding"):
        await editing.edit_page(
            client,
            "A different operator intent",
            change,
            [],
            frontmatter.build(change.draft_meta, change.draft_body),
            [{"instruction": "Move elsewhere."}],
            char_budget=12000,
            today="2026-10-04",
            repair=True,
        )
    assert client.calls == 1  # Fresh semantic review required, no stale operation authority.


@pytest.mark.asyncio
async def test_structural_noop_falls_through_and_all_completed_tasks_resume_without_dispatch():
    import re

    change = prepared()

    class Client:
        calls = []

        async def chat_json(self, system, user):
            self.calls.append(user)
            if user.startswith("BOUNDED INTRODUCED SECTION REPAIR\n"):
                return {"operations": [], "notes": ["Retain structure; ordinary section repair still required."]}
            assert "Article review repairs" in user
            return {
                "section_id": re.search(r"Section ID: (.+)", user)[1],
                "edits": [],
                "append": "",
                "handled_claim_ids": [],
                "claim_dispositions": [],
                "notes": [],
            }

    client = Client()
    result = await run(client, change)
    assert result["body"] == BODY and len(client.calls) > 1
    assert result["draft_progress"]["structural_repair"]["status"] == "no_operations"
    assert change.check_progress["article_review"]["failures"][0]["severity"] == "blocker"
    change.draft_body = result["body"]
    change.draft_progress = result["draft_progress"]
    calls = len(client.calls)
    replay = await run(client, change)
    assert replay["body"] == BODY and len(client.calls) == calls


@pytest.mark.asyncio
async def test_structural_repair_uses_seeded_baseline_when_preservation_did_not_run():
    change = prepared()
    preserved = change.check_progress.pop("existing_preservation")
    change.check_progress["structural_baseline"] = {
        k: preserved[k] for k in ("original_body", "original_meta", "body_sha256", "canonical_binding")
    }

    class Client:
        calls = 0

        async def chat_json(self, system, user):
            Client.calls += 1
            return operations(json.loads(user.split("\n", 1)[1]))

    result = await run(Client(), change)
    assert Client.calls == 1
    assert result["body"] == ORIGINAL.replace("## Tools", INTRO + "## Tools")


def test_seed_structural_baseline_reads_bound_canonical_page():
    from doclayer.curator.pipeline import Pipeline

    change = PageChange("g", "wiki/device.md", "update", base_commit="a" * 40, base_blob="b" * 40)
    change.check_progress = {"article_review": {"body_sha256": "f" * 64}}
    stub = SimpleNamespace(_reconciliation_base=lambda state, c: frontmatter.build({"title": "Device"}, ORIGINAL))
    Pipeline._seed_structural_baseline(stub, SimpleNamespace(base_commit="a" * 40), change)
    seeded = change.check_progress["structural_baseline"]
    assert seeded["original_body"] == frontmatter.parse(frontmatter.build({"title": "Device"}, ORIGINAL))[1]
    assert seeded["body_sha256"] == "f" * 64
    assert seeded["canonical_binding"] == {"commit": "a" * 40, "blob": "b" * 40, "path": "wiki/device.md"}


def test_seed_skips_when_base_unavailable():
    from doclayer.curator.pipeline import Pipeline

    change = PageChange("g", "wiki/device.md", "update", base_commit="a" * 40, base_blob="b" * 40)

    def missing(state, c):
        raise ModelError("stale reconciliation base does not match the recorded page blob", transient=False)

    Pipeline._seed_structural_baseline(
        SimpleNamespace(_reconciliation_base=missing), SimpleNamespace(base_commit="a" * 40), change
    )
    assert "structural_baseline" not in change.check_progress


def test_seed_structural_baseline_is_json_serializable_with_yaml_dates():
    import datetime

    from doclayer.curator.pipeline import Pipeline

    change = _seedable_change()
    stub = SimpleNamespace(_reconciliation_base=lambda state, c: "---\nupdated: 2026-10-01\n---\n" + ORIGINAL)
    Pipeline._seed_structural_baseline(stub, SimpleNamespace(base_commit="a" * 40), change)
    json.dumps(change.check_progress["structural_baseline"])
    assert not isinstance(change.check_progress["structural_baseline"]["original_meta"].get("updated"), datetime.date)


def _seedable_change():
    change = PageChange("g", "wiki/device.md", "update", base_commit="a" * 40, base_blob="b" * 40)
    change.check_progress = {"article_review": {"body_sha256": "f" * 64}}
    return change


def _stub():
    return SimpleNamespace(_reconciliation_base=lambda state, c: frontmatter.build({"title": "Device"}, ORIGINAL))


def test_seed_refreshes_body_hash_on_later_repair_pass():
    from doclayer.curator.pipeline import Pipeline

    change = _seedable_change()
    state = SimpleNamespace(base_commit="a" * 40)
    Pipeline._seed_structural_baseline(_stub(), state, change)
    original = change.check_progress["structural_baseline"]["original_body"]
    change.check_progress["article_review"]["body_sha256"] = "1" * 64
    Pipeline._seed_structural_baseline(_stub(), state, change)
    seeded = change.check_progress["structural_baseline"]
    assert seeded["body_sha256"] == "1" * 64
    assert seeded["original_body"] == original
    assert "existing_preservation" not in change.check_progress


@pytest.mark.parametrize("review", [None, {}, {"body_sha256": ""}])
def test_seed_skips_without_article_review_hash(review):
    from doclayer.curator.pipeline import Pipeline

    change = _seedable_change()
    change.check_progress = {} if review is None else {"article_review": review}
    Pipeline._seed_structural_baseline(_stub(), SimpleNamespace(base_commit="a" * 40), change)
    assert "structural_baseline" not in change.check_progress


def test_seed_skips_without_change_base_commit():
    from doclayer.curator.pipeline import Pipeline

    change = _seedable_change()
    change.base_commit = ""
    Pipeline._seed_structural_baseline(_stub(), SimpleNamespace(base_commit="a" * 40), change)
    assert "structural_baseline" not in change.check_progress


def test_seed_skips_when_real_base_blob_mismatches(monkeypatch, tmp_path):
    from doclayer.curator import gittools
    from doclayer.curator.pipeline import Pipeline

    monkeypatch.setattr(gittools, "blob_hash", lambda checkout, spec: "c" * 40)
    stub = SimpleNamespace(cfg=SimpleNamespace(paths=SimpleNamespace(brain_checkout=str(tmp_path))))
    stub._reconciliation_base = lambda state, c: Pipeline._reconciliation_base(stub, state, c)
    change = _seedable_change()
    Pipeline._seed_structural_baseline(stub, SimpleNamespace(base_commit="a" * 40), change)
    assert "structural_baseline" not in change.check_progress
