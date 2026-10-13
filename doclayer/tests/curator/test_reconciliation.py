"""Complete-page reconciliation at bounded model and durable progress boundaries."""

import hashlib
import re
from pathlib import Path

import pytest

from doclayer import frontmatter
from doclayer.curator import reconciliation
from doclayer.curator.editing import EDITOR_VERSION, page_units
from doclayer.curator.model import ModelError
from doclayer.curator.state import PageChange

LONG_PAGES = Path(__file__).resolve().parents[1] / "fixtures" / "long_pages"


def change(body):
    return PageChange("group", "wiki/subject.md", "update", title="Subject", draft_body=body)


def page(body):
    return frontmatter.build({"title": "Subject", "concept_id": "c-operator", "privacy": "internal"}, body)


class Never:
    async def chat_bounded_json(self, *args, **kwargs):
        raise AssertionError("nonoverlapping three-way merge must not invoke a model")


class Local:
    def __init__(self, decision="same-subject"):
        self.calls = []
        self.decision = decision

    async def chat_bounded_json(self, system, user, *, char_budget):
        assert len(system) + len(user) <= char_budget
        self.calls.append(user)
        if 'Return {"resolution"' in user:
            return {"resolution": self.decision, "explanation": "same applicability"}
        identity = re.search(r"Section ID: (\S+)", user).group(1)
        return {
            "section_id": identity,
            "edits": [],
            "append": "Additional supported detail [source: https://example.test].",
            "handled_claim_ids": [],
            "claim_dispositions": [],
        }


@pytest.mark.asyncio
async def test_long_page_nonoverlapping_operator_edits_and_bound_heading_repair():
    base = "# Subject\n\n## Unsupported factual heading\nOld fact.\n\n## Details\n" + "".join(
        f"Unique fact {i}.\n" for i in range(1200)
    )
    draft = base.replace("## Unsupported factual heading", "## Supported factual heading").replace(
        "Old fact.", "Supported fact [source: https://example.test]."
    )
    fresh = base.replace("Unique fact 1199.", "Operator unique late fact 1199.")
    c = change(draft)
    unit = page_units(base)[1]
    c.draft_progress = {
        "editor_version": EDITOR_VERSION,
        "body_hash": hashlib.sha256(draft.encode()).hexdigest(),
        "heading_renames": [
            {
                "unit_id": unit.id,
                "old": "## Unsupported factual heading",
                "new": "## Supported factual heading",
            }
        ],
        "completed_task_ids": ["old-body-editor-task"],
    }
    checkpoints = []
    result = await reconciliation.reconcile_stale(
        Never(),
        "topic",
        c,
        page(fresh),
        [],
        base_page=page(base),
        char_budget=7000,
        progress=checkpoints.append,
    )
    assert len(result["body"]) > 7000
    assert "Operator unique late fact 1199." in result["body"]
    assert "Supported fact [source: https://example.test]." in result["body"]
    assert "## Supported factual heading" in result["body"]
    assert result["frontmatter"]["concept_id"] == "c-operator"
    assert result["draft_progress"]["body_hash"] == hashlib.sha256(result["body"].encode()).hexdigest()
    assert result["draft_progress"]["heading_renames"][0]["unit_id"] == unit.id
    assert "completed_task_ids" not in result["draft_progress"]
    assert checkpoints


@pytest.mark.asyncio
async def test_unbound_heading_repair_is_never_reauthorized():
    base = "# Subject\n\n## Old heading\nFact.\n"
    c = change(base.replace("Old heading", "New heading"))
    c.draft_progress = {
        "body_hash": "stale-hash",
        "heading_renames": [{"old": "## Old heading", "new": "## New heading"}],
    }
    with pytest.raises(ModelError, match="bound repair proof"):
        await reconciliation.reconcile_stale(
            Never(),
            "topic",
            c,
            page(base + "Operator note.\n"),
            [],
            base_page=page(base),
            char_budget=7000,
        )


@pytest.mark.asyncio
async def test_true_overlap_keeps_fresh_operator_bytes_and_bounds_model_output():
    base = "# Subject\nOriginal assertion.\n"
    fresh = base.replace("Original assertion.", "Operator qualified assertion.")
    c = change(base.replace("Original assertion.", "Draft qualified assertion."))
    client = Local()
    result = await reconciliation.reconcile_stale(
        client, "topic", c, page(fresh), [], base_page=page(base), char_budget=7000
    )
    assert "Operator qualified assertion." in result["body"]
    assert "Additional supported detail" in result["body"]
    assert result["dropped"] == [] and result["notes"] == []  # ordinary merges are unchanged
    assert client.calls and all("CURRENT authoritative view:" in call for call in client.calls)


@pytest.mark.asyncio
async def test_collision_processes_late_views_and_resumes_without_replaying_calls():
    draft = "# Subject\n\n## Details\n" + "".join(
        f"Unique supported contribution {i} [source: https://example.test/{i}].\n" for i in range(100)
    )
    fresh = "# Subject\n\n## Details\nExisting operator statement.\n"
    c = change(draft)
    client = Local()
    checkpoints = []

    def save(row):
        checkpoints.append(row)
        c.reconcile_progress = row
        if "Section ID:" in client.calls[-1] and sum("Section ID:" in call for call in client.calls) == 2:
            raise RuntimeError("worker interrupted after acknowledged decision")

    with pytest.raises(RuntimeError):
        await reconciliation.reconcile_collision(client, "topic", c, page(fresh), char_budget=7000, progress=save)
    before = len(client.calls)
    result = await reconciliation.reconcile_collision(client, "topic", c, page(fresh), char_budget=7000)
    assert len(client.calls) > before
    combined = "\n".join(client.calls)
    assert "Unique supported contribution 99" in combined
    assert "Existing operator statement." in result["body"]
    assert sum("Unique supported contribution 0 [" in call and "Section ID:" in call for call in client.calls) == 1
    assert all(len(row["text"]) < 7000 for row in result["reconcile_progress"]["results"].values())
    assert result["frontmatter"]["concept_id"] == "c-operator"


@pytest.mark.asyncio
async def test_progress_invalidated_by_changed_fresh_bytes_and_corrections():
    c = change("# Subject\nDraft assertion.\n")
    client = Local()
    saved = []
    first = await reconciliation.reconcile_collision(
        client,
        "topic",
        c,
        page("# Subject\nFresh assertion.\n"),
        char_budget=7000,
        progress=saved.append,
    )
    c.reconcile_progress = first["reconcile_progress"]
    count = len(client.calls)
    await reconciliation.reconcile_collision(
        client,
        "topic",
        c,
        page("# Subject\nFresh edited assertion.\n"),
        [{"claim": "assertion", "instruction": "preserve qualifier"}],
        char_budget=7000,
    )
    assert len(client.calls) > count


@pytest.mark.asyncio
async def test_distinct_collision_retains_complete_long_draft_and_its_proof():
    c = change("# Distinct entity\n" + ("Original complete content.\n" * 1000))
    c.draft_meta = {"concept_id": "c-new"}
    c.draft_progress = {"body_hash": hashlib.sha256(c.draft_body.encode()).hexdigest()}
    result = await reconciliation.reconcile_collision(
        Local("distinct-subject"),
        "topic",
        c,
        page("# Other entity\nOperator content.\n"),
        char_budget=7000,
    )
    assert result["body"] == c.draft_body and result["draft_progress"] == c.draft_progress
    assert result["frontmatter"] == c.draft_meta


def test_three_way_partition_preserves_all_bytes_and_duplicate_occurrences():
    for base, fresh, draft in [
        ("a b c", "a operator b c", "a b draft c"),
        ("same same same", "same new same", "same same last"),
        ("abc", "", "xyz"),
    ]:
        parts = reconciliation._regions(base, fresh, draft)
        assert "".join(row[0] for row in parts) == base
        assert "".join(row[1] for row in parts) == fresh
        assert "".join(row[2] for row in parts) == draft


@pytest.mark.asyncio
async def test_operator_parent_rename_preserves_child_repair_and_future_structure():
    from doclayer.curator.editing import retain_edit_structure

    base = "# Subject\n\n## Old parent\nParent prose.\n\n### Unsupported child fact\nChild prose.\n"
    fresh = base.replace("## Old parent", "## Operator parent")
    draft = base.replace("### Unsupported child fact", "### Supported child fact")
    c = change(draft)
    old_child = page_units(base)[2]
    c.draft_progress = {
        "editor_version": EDITOR_VERSION,
        "body_hash": hashlib.sha256(draft.encode()).hexdigest(),
        "heading_renames": [
            {
                "unit_id": old_child.id,
                "old": "### Unsupported child fact",
                "new": "### Supported child fact",
            }
        ],
        "unit_identity": [{"id": u.id, "level": u.level} for u in page_units(base)],
    }
    result = await reconciliation.reconcile_stale(
        Never(), "topic", c, page(fresh), [], base_page=page(base), char_budget=7000
    )
    assert "## Operator parent" in result["body"] and "### Supported child fact" in result["body"]
    fresh_child = page_units(fresh)[2]
    assert result["draft_progress"]["heading_renames"][0]["unit_id"] == fresh_child.id
    retained = retain_edit_structure(result["draft_progress"], result["body"])
    assert retained == result["draft_progress"]
    assert retained["unit_identity"][2]["id"] == fresh_child.id


@pytest.mark.asyncio
async def test_early_section_append_preserves_later_operator_sections():
    fresh = "# Subject\n\n## Early\nExisting early fact.\n\n## Later\nOperator late fact.\n"
    c = change("# Subject\n\n## Early\nNew early supported contribution.\n")
    result = await reconciliation.reconcile_collision(Local(), "topic", c, page(fresh), char_budget=7000)
    assert "Operator late fact." in result["body"] and "Existing early fact." in result["body"]
    assert result["body"].index("Additional supported detail") < result["body"].index("## Later")


@pytest.mark.asyncio
async def test_collision_late_applicability_and_identity_metadata_are_assessed():
    class Scope(Local):
        async def chat_bounded_json(self, system, user, *, char_budget):
            if 'Return {"resolution"' in user and "Provider B edition" in user:
                assert "Section ID:" not in user
                self.calls.append(user)
                return {
                    "resolution": "distinct-subject",
                    "explanation": "different applicability",
                }
            return await super().chat_bounded_json(system, user, char_budget=char_budget)

    fresh = (
        "# Generic client guide\n"
        + "".join(f"Ordinary documentation item {i}.\n" for i in range(300))
        + "\n## Applicable edition\nProvider B edition only.\n"
    )
    c = change("# Generic client guide\nOrdinary introduction.\n")
    c.draft_meta = {
        "title": "Generic client guide",
        "platform": "Provider A",
        "edition": "Personal",
    }
    client = Scope()
    result = await reconciliation.reconcile_collision(client, "topic", c, page(fresh), char_budget=7000)
    assert result["resolution"] == "distinct-subject" and result["body"] == c.draft_body
    assert any("Provider B edition only." in call for call in client.calls)
    assert any('"platform": "Provider A"' in call for call in client.calls)
    assert not any("Section ID:" in call for call in client.calls)


@pytest.mark.asyncio
async def test_unresolved_subject_never_overwrites_existing_page():
    client = Local("unknown")
    c = change("# Generic\nShared introduction.\n")
    with pytest.raises(ModelError, match="remains unresolved"):
        await reconciliation.reconcile_collision(
            client,
            "topic",
            c,
            page("# Generic\nShared introduction.\n"),
            char_budget=7000,
        )


@pytest.mark.asyncio
async def test_merge_metadata_unions_lineage_without_overwriting_operator_identity():
    base = "# Subject\nOld fact.\n"
    fresh = base + "Operator addition.\n"
    c = change(base.replace("Old fact.", "Supported updated fact."))
    fresh_meta = {
        "concept_id": "c-operator",
        "privacy": "internal",
        "address": "a-original",
        "title": "Operator title",
        "source_origin": "operator",
        "platform": "Operator environment",
        "tags": ["operator"],
        "source_lineage": ["seed:operator"],
        "updated": "2026-10-02",
    }
    c.draft_meta = {
        "concept_id": "c-wrong",
        "privacy": "public",
        "address": "a-new",
        "title": "Draft title",
        "source_origin": "agent",
        "platform": "Draft environment",
        "tags": ["added"],
        "source_lineage": ["seed:operator", "curator:job-owned"],
        "updated": "2026-10-03",
    }
    result = await reconciliation.reconcile_stale(
        Never(),
        "topic",
        c,
        frontmatter.build(fresh_meta, fresh),
        [],
        base_page=page(base),
        char_budget=7000,
    )
    meta = result["frontmatter"]
    for field in (
        "concept_id",
        "privacy",
        "address",
        "title",
        "source_origin",
        "platform",
    ):
        assert meta[field] == fresh_meta[field]
    assert meta["tags"] == ["operator", "added"] and meta["source_lineage"] == [
        "seed:operator",
        "curator:job-owned",
    ]
    assert meta["updated"] == "2026-10-03"
    assert reconciliation._metadata({"updated": "2026-10-02"}, {"updated": "2026-10-03"}, False) == {
        "updated": "2026-10-02"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "model-system-card",
        "prompting-guidance",
        "openai-model-lineup",
        "openai-responses-api",
        "openai-api-tools",
        "openai-api-performance-modes",
    ],
)
async def test_long_pages_reconcile_without_full_page_model(name):
    raw = (LONG_PAGES / f"{name}.md").read_text()
    meta, base = frontmatter.parse(raw)
    first = base.splitlines()[0]
    fresh = base.replace(first + "\n", first + "\n\nOperator standing note.\n", 1)
    draft = base + "\nAdditional validated contribution [source: https://example.test].\n"
    c = change(draft)
    c.path = f"wiki/{name}.md"
    c.draft_meta = meta
    assert len(fresh) + len(draft) > 7000
    result = await reconciliation.reconcile_stale(
        Never(),
        "topic",
        c,
        frontmatter.build(meta, fresh),
        [],
        base_page=raw,
        char_budget=7000,
    )
    assert "Operator standing note." in result["body"]
    assert "Additional validated contribution [source: https://example.test]." in result["body"]
    assert base.rstrip() in result["body"].replace("\nOperator standing note.\n", "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "block",
    [
        "```python\nsetting = BASE\n```\n",
        "| Setting | Value |\n| --- | --- |\n| primary | BASE |\n",
    ],
)
async def test_overlap_keeps_complete_fenced_or_table_context(block):
    base = "# Subject\n\n## Settings\n" + block + "\n## Later\nOperator later facts.\n"
    fresh = base.replace("BASE", "OPERATOR")
    c = change(base.replace("BASE", "DRAFT"))
    result = await reconciliation.reconcile_stale(
        Local(), "topic", c, page(fresh), [], base_page=page(base), char_budget=7000
    )
    original = block.replace("BASE", "OPERATOR")
    assert original in result["body"]
    assert result["body"].index("Additional supported detail") > result["body"].index(original) + len(original) - 1
    assert result["body"].index("Additional supported detail") < result["body"].index("## Later")


@pytest.mark.asyncio
async def test_bound_heading_repair_survives_true_content_overlap():
    base = "# Subject\n\n## Unsupported heading\nOriginal factual sentence.\n"
    fresh = base.replace("Original factual sentence.", "Operator qualified factual sentence.")
    draft = base.replace("## Unsupported heading", "## Supported heading").replace(
        "Original factual sentence.", "Draft supported factual sentence."
    )
    c = change(draft)
    unit = page_units(base)[1]
    c.draft_progress = {
        "editor_version": EDITOR_VERSION,
        "body_hash": hashlib.sha256(draft.encode()).hexdigest(),
        "heading_renames": [
            {
                "unit_id": unit.id,
                "old": "## Unsupported heading",
                "new": "## Supported heading",
            }
        ],
    }
    result = await reconciliation.reconcile_stale(
        Local(), "topic", c, page(fresh), [], base_page=page(base), char_budget=7000
    )
    assert "## Supported heading" in result["body"]
    assert "Operator qualified factual sentence." in result["body"]
    assert result["draft_progress"]["heading_renames"][0]["old"] == "## Unsupported heading"


@pytest.mark.asyncio
async def test_model_cannot_remove_fresh_operator_material():
    class Destructive(Local):
        async def chat_bounded_json(self, system, user, *, char_budget):
            self.calls.append(user)
            identity = re.search(r"Section ID: (\S+)", user).group(1)
            return {
                "section_id": identity,
                "edits": [{"old": "Operator qualified fact.", "new": "Draft fact."}],
                "append": "",
                "handled_claim_ids": [],
                "claim_dispositions": [],
            }

    base = "# Subject\nOriginal fact.\n"
    fresh = base.replace("Original fact.", "Operator qualified fact.")
    c = change(base.replace("Original fact.", "Draft fact."))
    client = Destructive()
    # Exhausted ONLY by the protected-material check: the fresh section is kept
    # exactly and the draft contribution is dropped, never written over it.
    result = await reconciliation.reconcile_stale(
        client, "topic", c, page(fresh), [], base_page=page(base), char_budget=7000
    )
    assert len(client.calls) == 3
    assert result["body"] == fresh
    assert [row["text"] for row in result["dropped"]] == ["# Subject\nDraft fact.\n"]
    # A resumed pass reuses the checkpointed fallback without asking again.
    c.reconcile_progress = result["reconcile_progress"]
    again = await reconciliation.reconcile_stale(
        Never(), "topic", c, page(fresh), [], base_page=page(base), char_budget=7000
    )
    assert again["body"] == fresh and again["dropped"] == result["dropped"]


@pytest.mark.asyncio
async def test_protected_fallback_drops_only_its_section_and_merges_the_rest():
    class Overlap(Local):
        async def chat_bounded_json(self, system, user, *, char_budget):
            self.calls.append(user)
            identity = re.search(r"Section ID: (\S+)", user).group(1)
            if "Operator alpha." in user.split("DRAFT contribution view", 1)[0]:
                edits = [{"old": "Operator alpha.", "new": "Draft alpha."}]  # always protected
                return {
                    "section_id": identity,
                    "edits": edits,
                    "append": "",
                    "handled_claim_ids": [],
                    "claim_dispositions": [],
                }
            return await super().chat_bounded_json(system, user, char_budget=char_budget)

    base = "# Subject\nIntro.\n\n## Alpha\nOriginal alpha.\n\n## Middle\nOriginal middle.\n\n## Beta\nOriginal beta.\n"
    fresh = base.replace("Original alpha.", "Operator alpha.").replace("Original beta.", "Operator beta.")
    draft = (
        base.replace("Original alpha.", "Draft alpha.")
        .replace("Original beta.", "Draft beta.")
        .replace("Original middle.", "Draft middle.")
    )
    client = Overlap()
    result = await reconciliation.reconcile_stale(
        client, "topic", change(draft), page(fresh), [], base_page=page(base), char_budget=7000
    )
    body = result["body"]
    assert "## Alpha\nOperator alpha.\n" in body and "Draft alpha." not in body  # fresh kept exactly
    assert "Draft middle." in body  # non-overlapping draft change unchanged
    assert "Operator beta." in body and "Additional supported detail" in body  # other overlap merged
    assert [row["text"] for row in result["dropped"]] == ["## Alpha\nDraft alpha.\n\n"]
    assert result["notes"] == ["draft contribution superseded by fresh publication: Alpha"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mixed", [False, True])
async def test_reconciliation_errors_other_than_protected_material_still_fail(mixed):
    class Broken(Local):
        async def chat_bounded_json(self, system, user, *, char_budget):
            self.calls.append(user)
            identity = re.search(r"Section ID: (\S+)", user).group(1)
            old = "Operator qualified fact." if mixed and len(self.calls) != 2 else "No such anchor."
            return {
                "section_id": identity,
                "edits": [{"old": old, "new": "Draft fact."}],
                "append": "",
                "handled_claim_ids": [],
                "claim_dispositions": [],
            }

    base = "# Subject\nOriginal fact.\n"
    fresh = base.replace("Original fact.", "Operator qualified fact.")
    client = Broken()
    with pytest.raises(ModelError) as raised:
        await reconciliation.reconcile_stale(
            client,
            "topic",
            change(base.replace("Original fact.", "Draft fact.")),
            page(fresh),
            [],
            base_page=page(base),
            char_budget=7000,
        )
    assert len(client.calls) == 3
    # A mixed failure history is not "only protected material": it fails as before.
    assert ("changes fresh operator material" in str(raised.value)) == mixed


@pytest.mark.asyncio
async def test_yaml_date_metadata_and_identity_changes_have_stable_bindings():
    from datetime import date

    c = change("# Subject\nDraft contribution.\n")
    c.draft_meta = {"updated": date(2026, 10, 2), "version": date(2026, 9, 30)}
    raw = frontmatter.build(
        {"title": "Subject", "version": date(2026, 9, 30)},
        "# Subject\nOperator fact.\n",
    )
    client = Local()
    first = await reconciliation.reconcile_collision(client, "topic", c, raw, char_budget=7000)
    c.reconcile_progress = first["reconcile_progress"]
    count = len(client.calls)
    c.title = "Changed actual subject identity"
    second = await reconciliation.reconcile_collision(client, "topic", c, raw, char_budget=7000)
    assert second["reconcile_progress"]["binding"] != first["reconcile_progress"]["binding"]
    assert len(client.calls) > count


@pytest.mark.asyncio
async def test_inserted_duplicate_heading_cannot_take_original_repair_authority():
    base = "# Subject\n\n## Repeated factual heading\nFirst original facts.\n\n## Repeated factual heading\nSecond original facts.\n"
    inserted = "## Repeated factual heading\nNew operator section.\n\n"
    fresh = base.replace("# Subject\n\n", "# Subject\n\n" + inserted)
    draft = base.replace("## Repeated factual heading", "## Supported original heading", 1)
    c = change(draft)
    original = page_units(base)[1]
    c.draft_progress = {
        "editor_version": EDITOR_VERSION,
        "body_hash": hashlib.sha256(draft.encode()).hexdigest(),
        "heading_renames": [
            {
                "unit_id": original.id,
                "old": "## Repeated factual heading",
                "new": "## Supported original heading",
            }
        ],
    }
    result = await reconciliation.reconcile_stale(
        Local(), "topic", c, page(fresh), [], base_page=page(base), char_budget=7000
    )
    assert inserted in result["body"]
    assert "## Supported original heading\nFirst original facts." in result["body"]
    assert result["draft_progress"]["heading_renames"][0]["unit_id"] == page_units(fresh)[2].id


@pytest.mark.asyncio
async def test_overflowing_reconciliation_task_names_its_section_and_sizes():
    base = "# Subject\nOriginal assertion.\n"
    fresh = base.replace("Original assertion.", "Operator qualified assertion.")
    c = change(base.replace("Original assertion.", "Draft qualified assertion."))
    corrections = [
        {
            "page": "wiki/subject.md",
            "claim": "Original assertion.",
            "instruction": "Keep the binding qualification. " * 300,
            "correction_id": "corr-1",
        }
    ]
    with pytest.raises(ModelError) as raised:
        await reconciliation.reconcile_stale(
            Never(), "topic", c, page(fresh), corrections, base_page=page(base), char_budget=7000
        )
    message = str(raised.value)
    assert message.startswith("bounded reconciliation task exceeds context: wiki/subject.md section ")
    assert "corrections " in message and "with budget 7000" in message and not raised.value.transient


@pytest.mark.asyncio
async def test_collision_view_editing_fresh_material_falls_back_to_fresh():
    class Destructive(Local):
        async def chat_bounded_json(self, system, user, *, char_budget):
            if 'Return {"resolution"' in user:
                return await super().chat_bounded_json(system, user, char_budget=char_budget)
            self.calls.append(user)
            current = user.split("DRAFT contribution view", 1)[0]
            edits = (
                [{"old": "Existing operator statement.", "new": "Draft statement."}]
                if "Existing operator statement." in current
                else []
            )
            return {
                "section_id": re.search(r"Section ID: (\S+)", user).group(1),
                "edits": edits,
                "append": "",
                "handled_claim_ids": [],
                "claim_dispositions": [],
            }

    fresh = "# Subject\n\n## Details\nExisting operator statement.\n"
    client = Destructive()
    result = await reconciliation.reconcile_collision(
        client,
        "topic",
        change("# Subject\n\n## Details\nDraft statement.\n"),
        page(fresh),
        char_budget=7000,
    )
    assert result["body"] == fresh
    assert [row["text"] for row in result["dropped"]] == ["## Details\nDraft statement.\n"]


@pytest.mark.asyncio
async def test_saved_fallback_is_never_reused_against_a_newer_fresh_page():
    class Destructive(Local):
        async def chat_bounded_json(self, system, user, *, char_budget):
            self.calls.append(user)
            current = user.split("DRAFT contribution view", 1)[0]
            old = re.search(r"Operator \w+ fact\.", current).group(0)
            return {
                "section_id": re.search(r"Section ID: (\S+)", user).group(1),
                "edits": [{"old": old, "new": "Draft fact."}],
                "append": "",
                "handled_claim_ids": [],
                "claim_dispositions": [],
            }

    base = "# Subject\nOriginal fact.\n"
    c = change(base.replace("Original fact.", "Draft fact."))
    first = await reconciliation.reconcile_stale(
        Destructive(),
        "topic",
        c,
        page(base.replace("Original", "Operator first")),
        [],
        base_page=page(base),
        char_budget=7000,
    )
    assert first["dropped"]
    c.reconcile_progress = first["reconcile_progress"]
    newer = base.replace("Original", "Operator second")
    client = Destructive()
    second = await reconciliation.reconcile_stale(
        client, "topic", c, page(newer), [], base_page=page(base), char_budget=7000
    )
    # The binding hashes the complete fresh page: a newer page is asked again.
    assert len(client.calls) == 3 and second["body"] == newer
    assert second["reconcile_progress"]["binding"] != first["reconcile_progress"]["binding"]
