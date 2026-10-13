"""Supported old facts survive generic edits; missing sources are not deletion proof."""

import copy
import json

import pytest

from doclayer.curator import curate
from doclayer.curator.model import ModelError
from doclayer.curator.preservation import (
    audit_existing_preservation,
    changed_original_body,
)
from doclayer.curator.state import Evidence, PageChange


class Model:
    """Explicit article fixtures; no synthetic original-claim proof maps."""

    def __init__(self, fail_after=None):
        self.calls = 0
        self.fail_after = fail_after
        self.requests = []

    async def chat_json(self, system, user):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise ModelError("inference outage", transient=True)
        self.requests.append(user)
        assert len(system) + len(user) <= 12000
        assert user.startswith("ARTICLE EDITORIAL REVIEW\n")
        frame = json.loads(user.split("\n", 1)[1])
        body = frame["body"]
        originals = frame["original_sections"]
        excerpts = frame.get("source_excerpts", {})
        evidence = [record.get("quote", excerpts.get(record.get("quote_ref"), "")) for record in frame["evidence"]]
        replacements = {
            "Under laboratory conditions, Device found 17 severe defects.": "In laboratory tests, Device identified seventeen severe defects.",
            "Device supports native image output.": "Device outputs text; an image tool produces images.",
            "Current Device capacity is 64 units.": "Current Device capacity is 128 units.",
        }
        failures = []

        def failed(section_id, detail):
            failures.append(
                {
                    "severity": "blocker",
                    "check": "existing-preservation",
                    "detail": detail,
                    "section_id": section_id,
                    "anchor": None,
                }
            )

        if frame["mode"] == "preservation":
            if frame["metadata"] != frame["original_metadata"]:
                failed(
                    frame["target_section_ids"][0],
                    "Governing metadata changed without support.",
                )
            for section in originals:
                for line in section["text"].splitlines():
                    if not line.strip() or line.startswith("#") or line in body:
                        continue
                    replacement = replacements.get(line)
                    if (
                        replacement
                        and replacement in body
                        and (line.startswith("Under laboratory") or replacement in evidence)
                    ):
                        continue
                    failed(
                        section["id"]
                        if section["id"] in frame["target_section_ids"]
                        else frame["target_section_ids"][0],
                        "Restore the complete original fact and governing qualifications: " + line,
                    )
        else:
            for identity in frame["required_claim_ids"]:
                record = next(record for record in frame["evidence"] if record["id"] == identity)
                quote = record.get("quote", excerpts.get(record.get("quote_ref"), ""))
                if quote not in body and not (replacements.get(quote) and replacements[quote] in body):
                    failures.append(
                        {
                            "severity": "blocker",
                            "check": "required-contribution",
                            "detail": "Assigned source assertion is absent.",
                            "section_id": frame["target_section_ids"][0],
                            "anchor": None,
                            "claim_id": identity,
                        }
                    )
        return {"complete": True, "failures": failures, "conflicts": []}


def fact(text):
    return Evidence(
        "manual",
        "Actual source",
        "Metrics",
        "quote",
        text,
        quote=text,
        support_status="supported",
    )


def change(body):
    return PageChange("g", "wiki/device.md", "update", draft_meta={"title": "Device"}, draft_body=body)


def resolver():
    async def resolve(claim, examined):
        return {"records": [], "examined": [], "exhausted": True}

    resolve.binding = "complete-current-retained-source-policy"
    return resolve


async def audit(model, target, original, records, **kw):
    return await audit_existing_preservation(
        model,
        "Device",
        target,
        records,
        [],
        original_meta={"title": "Device"},
        original_body=original,
        canonical_binding="recorded-git-base-blob",
        char_budget=12000,
        source_binding="actual-extracted-source-hash",
        support_resolver=resolver(),
        **kw,
    )


@pytest.mark.asyncio
async def test_generic_incoming_summary_cannot_erase_two_supported_old_metrics():
    metrics = [
        "Under laboratory conditions, Device found 17 severe defects.",
        "Under laboratory conditions, Device found three previously unknown defects.",
    ]
    old = "# Device\n## Benchmark\n" + "\n".join(metrics) + "\n"
    target = change("# Device\n## Benchmark\nDevice completed the June evaluation.\n")
    records = [fact(text) for text in metrics] + [fact("Device completed the June evaluation.")]
    target.claim_ids = [curate.evidence_id(records[-1])]
    target.draft_progress = {"handled_claim_ids": target.claim_ids}
    before = await curate.check_draft(
        Model(),
        "Device",
        target,
        records,
        [],
        char_budget=12000,
        support_resolver=resolver(),
    )
    assert not before["failures"]  # Existing assertion/new-ID gates miss old omissions.
    result = await audit(Model(), target, old, records)
    assert len(result["failures"]) == 2 and not result["complete"]
    assert all(row["check"] == "existing-preservation" for row in result["failures"])
    assert target.claim_ids == [curate.evidence_id(records[-1])]
    assert {r.text for r in result["repair_records"]} >= set(metrics)
    assert all(not r.source_id.startswith("canonical:") for r in result["repair_records"])


@pytest.mark.asyncio
async def test_supported_qualifications_cannot_disappear_or_match_other_entity_numbers():
    old_fact = "Under laboratory conditions, Device found 17 severe defects."
    target = change(
        "# Device\n## Benchmark\nDevice found 17 severe defects.\n"
        "OtherDevice found 17 severe defects under laboratory conditions.\n"
    )
    result = await audit(Model(), target, "# Device\n## Benchmark\n" + old_fact + "\n", [fact(old_fact)])
    assert result["failures"] and not result["complete"]


@pytest.mark.asyncio
async def test_supported_semantic_paraphrase_keeps_metrics_and_conditions():
    original = "# Device\n## Benchmark\nUnder laboratory conditions, Device found 17 severe defects.\n"
    target = change("# Device\n## Benchmark\nIn laboratory tests, Device identified seventeen severe defects.\n")
    result = await audit(
        Model(),
        target,
        original,
        [fact("Under laboratory conditions, Device found 17 severe defects.")],
    )
    assert result["complete"] and result["failures"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "old_fact,new_fact",
    [
        (
            "Device supports native image output.",
            "Device outputs text; an image tool produces images.",
        ),
        (
            "Current Device capacity is 64 units.",
            "Current Device capacity is 128 units.",
        ),
    ],
)
async def test_current_sources_may_correct_unsupported_or_obsolete_old_claim(old_fact, new_fact):
    target = change("# Device\n## Specification\n" + new_fact + "\n")
    result = await audit(
        Model(),
        target,
        "# Device\n## Specification\n" + old_fact + "\n",
        [fact(new_fact)],
    )
    assert result["complete"] and not result["unsupported"]
    assert target.check_progress["existing_preservation"]["editorial_version"] == 1


@pytest.mark.asyncio
async def test_sparse_unknown_support_is_not_authority_to_remove_old_fact():
    target = change("# Device\n## Benchmark\nNew unrelated fact.\n")
    result = await audit_existing_preservation(
        Model(),
        "Device",
        target,
        [],
        [],
        original_meta={"title": "Device"},
        original_body="# Device\n## Benchmark\nDevice found 17 defects.\n",
        canonical_binding="git-base",
        char_budget=12000,
    )
    assert result["pending"] and result["failures"] and not result["complete"]


@pytest.mark.asyncio
async def test_editorial_outage_keeps_original_baseline_and_resumes_same_bound_task():
    old = "# Device\n## Benchmark\nDevice found 17 defects.\n"
    target = change("# Device\n## Benchmark\nDevice completed its evaluation.\n")
    with pytest.raises(ModelError, match="inference outage"):
        await audit(Model(fail_after=0), target, old, [])
    state = copy.deepcopy(target.check_progress["existing_preservation"])
    assert state["original_body"] == old and not state.get("complete")
    restored = PageChange.from_dict(copy.deepcopy(target.to_dict()))
    model = Model()
    result = await audit(model, restored, old, [])
    assert result["failures"] and model.calls == 1
    assert restored.check_progress["existing_preservation"]["binding"] == state["binding"]


@pytest.mark.asyncio
async def test_candidate_change_revokes_coverage_but_reuses_bound_original_support():
    original = "# Device\n## Benchmark\nDevice found 17 defects.\n"
    target = change(original + "Device completed an evaluation.\n")
    records = [fact("Device found 17 defects.")]
    await audit(Model(), target, original, records)
    model = Model()
    target.draft_body = "# Device\n## Benchmark\nDevice completed an evaluation.\n"
    result = await audit(model, target, original, records)
    assert result["failures"]
    assert not any(user.startswith("DRAFT CLAIM ENUMERATION") for user in model.requests)


@pytest.mark.asyncio
async def test_same_candidate_reuses_durable_all_proofs_without_calls():
    original = "# Device\n## Benchmark\nDevice found 17 defects.\n"
    target = change(original + "Device completed an evaluation.\n")
    records = [fact("Device found 17 defects.")]
    await audit(Model(), target, original, records)
    model = Model()
    result = await audit(model, PageChange.from_dict(copy.deepcopy(target.to_dict())), original, records)
    assert result["complete"] and model.calls == 0


def test_unchanged_whole_fenced_table_units_skip_but_changed_child_keeps_ancestor():
    original = "# Device\nLaboratory installations only.\n## Code\n```py\n# not a heading\nx=1\n```\n## Metrics\n| Name | Count |\n|---|---|\n| defects | 17 |\n"
    candidate = original.replace("| defects | 17 |", "| defects | unknown |")
    selected = changed_original_body(original, candidate)
    assert "Laboratory installations only." in selected and "| defects | 17 |" in selected
    assert "x=1" not in selected
    assert changed_original_body(original, original) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", ["source", "policy", "base"])
async def test_current_generation_changes_revoke_old_support(mutate):
    original = "# Device\n## Benchmark\nDevice found 17 defects.\n"
    target = change(original + "New evaluation.\n")
    records = [fact("Device found 17 defects.")]
    await audit(Model(), target, original, records)
    model = Model()
    kw = {"source_policy": {"provider": "new"}} if mutate == "policy" else {}
    if mutate == "source":
        records = [fact("Device found 128 defects.")]
    if mutate == "base":
        original = original.replace("17", "42")
    await audit(model, target, original, records, **kw)
    assert any(user.startswith("ARTICLE EDITORIAL REVIEW") for user in model.requests)


@pytest.mark.asyncio
async def test_complete_but_unrelated_source_review_cannot_authorize_deletion():
    old = "# Device\n## Benchmark\nDevice found 17 defects.\n"
    result = await audit(
        Model(),
        change("# Device\n## Benchmark\nDevice completed testing.\n"),
        old,
        [fact("OtherDevice stores settings locally.")],
    )
    assert result["pending"] and result["failures"] and not result["unsupported"]


@pytest.mark.asyncio
async def test_original_parent_qualification_cannot_be_lost_by_atomic_enumeration():
    old = "# Device\nOnly laboratory installations support this capability.\n## Capability\nDevice outputs images.\n"
    target = change(
        "# Device\nOnly laboratory installations support this capability.\n## Capability\nDevice completed an evaluation.\n"
    )
    records = [fact("Under laboratory conditions, Device outputs images.")]
    result = await audit(Model(), target, old, records)
    assert result["pending"] and result["failures"]
    state = target.check_progress["existing_preservation"]
    assert "Only laboratory installations support this capability." in state["original_body"]


@pytest.mark.asyncio
async def test_restored_literal_old_fact_and_parent_scope_are_not_removal_permission():
    old = "# Device\nOnly laboratory installations support this capability.\n## Capability\nDevice outputs images.\n"
    target = change(old + "A June evaluation was completed.\n")
    # Empty selected evidence never becomes source support; unchanged statements
    # remain the independent whole-candidate checker's obligation.
    result = await audit(Model(), target, old, [])
    assert result["complete"] and not result["unsupported"]
    assert "claims" not in target.check_progress["existing_preservation"]


def test_literal_shortcut_cannot_borrow_another_repeated_owner_or_drop_local_condition():
    from doclayer.curator.preservation import _literal_retained

    original = "# Device\n## Repeated\nOnly laboratory installations.\nDevice outputs images.\n## Repeated\nOnly field installations.\nDevice outputs images.\n"
    changed = original.replace(
        "Only laboratory installations.\nDevice outputs images.",
        "Only laboratory installations.\nCapability omitted.",
    )
    assert not _literal_retained("Only laboratory installations.", original, changed)
    dropped_condition = original.replace("Only laboratory installations.\n", "")
    assert not _literal_retained(
        "Device outputs images.",
        original.split("## Repeated", 2)[0] + "## Repeated\nOnly laboratory installations.\nDevice outputs images.\n",
        dropped_condition,
    )


@pytest.mark.asyncio
async def test_base_and_fresh_namespaces_keep_independent_proof_and_repair_sources():
    from doclayer.curator.preservation import existing_repair_records

    old = "# Device\n## Metrics\nDevice found 17 defects.\n"
    fresh = old.replace("17", "42")
    target = change("# Device\n## Metrics\nDevice completed testing.\n")
    await audit(Model(), target, old, [fact("Device found 17 defects.")])
    original_binding = target.check_progress["existing_preservation"]["binding"]
    await audit(
        Model(),
        target,
        fresh,
        [fact("Device found 42 defects.")],
        namespace="existing_preservation_fresh",
    )
    assert target.check_progress["existing_preservation"]["binding"] == original_binding
    assert [r.text for r in existing_repair_records(target)] == ["Device found 17 defects."]
    assert [r.text for r in existing_repair_records(target, namespace="existing_preservation_fresh")] == [
        "Device found 42 defects."
    ]


def test_changed_parent_prose_invalidates_unchanged_descendant_metric_scope_and_repeats():
    original = "# Device\nLaboratory installations only.\n## Metric\nDevice found 17 defects.\n## Metric\nDevice found three unknown defects.\n"
    changed = original.replace("Laboratory installations only.", "All installations.")
    selected = changed_original_body(original, changed)
    assert "Laboratory installations only." in selected
    assert "Device found 17 defects." in selected
    assert "Device found three unknown defects." in selected
    assert selected.count("## Metric") == 2


@pytest.mark.asyncio
async def test_governing_metadata_change_cannot_skip_unchanged_body_obligation():
    old = "# Device\n## Capability\nDevice stores settings locally.\n"
    target = change(old)
    target.draft_meta["edition"] = "community"
    result = await audit_existing_preservation(
        Model(),
        "Device",
        target,
        [fact("Device stores settings locally.")],
        [],
        original_meta={"title": "Device", "edition": "enterprise"},
        original_body=old,
        canonical_binding="recorded-base",
        char_budget=12000,
        support_resolver=resolver(),
    )
    assert not result["complete"] and result["failures"]
    assert target.check_progress["existing_preservation"]["original_body"] == old


@pytest.mark.asyncio
async def test_unknown_metadata_scope_change_is_not_unchanged_literal_authority():
    old = "# Device\n## Capability\nDevice stores settings locally.\n"
    target = change(old)
    target.draft_meta["edition"] = "community"
    result = await audit_existing_preservation(
        Model(),
        "Device",
        target,
        [],
        [],
        original_meta={"title": "Device", "edition": "enterprise"},
        original_body=old,
        canonical_binding="recorded-base",
        char_budget=12000,
        support_resolver=resolver(),
    )
    assert not result["complete"] and result["pending"] and result["failures"]
    assert not target.check_progress["existing_preservation"].get("unchanged_literal")


@pytest.mark.asyncio
async def test_unquoted_canonical_iso_date_has_stable_checker_and_retirement_wire_types():
    import datetime

    from doclayer import frontmatter

    metadata, old = frontmatter.parse(
        "---\nupdated: 2026-10-02\ntitle: Device\n---\n# Device\n## Specification\nCurrent Device capacity is 64 units.\n"
    )
    assert metadata["updated"] == datetime.date(2026, 10, 2)
    target = change("# Device\n## Specification\nCurrent Device capacity is 128 units.\n")
    result = await audit_existing_preservation(
        Model(),
        "Device",
        target,
        [fact("Current Device capacity is 128 units.")],
        [],
        original_meta=metadata,
        original_body=old,
        canonical_binding="recorded-base",
        char_budget=12000,
        support_resolver=resolver(),
    )
    assert result["complete"] and not result["unsupported"]
    model = Model()
    normalized = {**metadata, "updated": "2026-10-02"}
    again = await audit_existing_preservation(
        model,
        "Device",
        target,
        [fact("Current Device capacity is 128 units.")],
        [],
        original_meta=normalized,
        original_body=old,
        canonical_binding="recorded-base",
        char_budget=12000,
        support_resolver=resolver(),
    )
    assert again["complete"] and model.calls == 0
    assert metadata["updated"] == datetime.date(2026, 10, 2)  # Caller metadata untouched.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before,after",
    [
        ({"platforms": ["local", "remote"]}, {"platforms": ["local"]}),
        (
            {"applicability": {"versions": ["v1", "v2"]}},
            {"applicability": {"versions": ["v2"]}},
        ),
        (
            {"applicability": {"deployment": {"environments": ["lab", "field"], "updated": "one"}}},
            {"applicability": {"deployment": {"environments": ["lab", "field"], "updated": "two"}}},
        ),
    ],
)
async def test_structured_governing_metadata_changes_block_unknown_literal_shortcut(before, after):
    old = "# Device\n## Capability\nDevice stores settings locally.\n"
    target = change(old)
    target.draft_meta.update(after)
    result = await audit_existing_preservation(
        Model(),
        "Device",
        target,
        [],
        [],
        original_meta={"title": "Device", **before},
        original_body=old,
        canonical_binding="recorded-base",
        char_budget=12000,
        support_resolver=resolver(),
    )
    assert not result["complete"] and result["pending"]
    assert not target.check_progress["existing_preservation"].get("unchanged_literal")
