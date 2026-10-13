"""Preserve existing article meaning through bounded paired editorial review.

Exact retained structural units need no preservation inference. Changed material
is compared with its original context, actual evidence and binding corrections;
unknown support never authorizes deleting existing knowledge.
"""

from __future__ import annotations

import copy
import json
from collections import Counter

from doclayer.curator import curate
from doclayer.curator.editing import page_units
from doclayer.curator.state import Evidence

VERSION = 2


def changed_original_body(original, candidate):
    """Select whole changed units plus actual preceding governing ancestors.

    Occurrence counts and full ancestry prevent a copied section elsewhere from
    impersonating the original owner. Fences/tables stay intact through the
    shared structural parser and the bounded assertion checker.
    """
    available = Counter()
    current_ancestors = []
    for unit in page_units(candidate):
        current_ancestors = [parent for parent in current_ancestors if parent.level < unit.level]
        available[
            (
                unit.id,
                unit.label,
                unit.text,
                tuple((parent.id, parent.text) for parent in current_ancestors),
            )
        ] += 1
        current_ancestors.append(unit)
    units = page_units(original)
    chosen = set()
    ancestors = []
    for index, unit in enumerate(units):
        ancestors = [other for other in ancestors if units[other].level < unit.level]
        key = (
            unit.id,
            unit.label,
            unit.text,
            tuple((units[other].id, units[other].text) for other in ancestors),
        )
        if available[key]:
            available[key] -= 1
        else:
            chosen.update(ancestors)
            chosen.add(index)
        ancestors.append(index)
    return "".join(unit.text for index, unit in enumerate(units) if index in chosen)


def blocking_failures(result):
    """Only explicit warnings are nonblocking; legacy failures stay blocked."""
    return [failure for failure in result.get("failures", []) if failure.get("severity") != "warning"]


async def audit_existing_preservation(
    client,
    topic,
    change,
    records,
    corrections,
    *,
    original_meta,
    original_body,
    canonical_binding,
    char_budget,
    source_policy=None,
    source_binding=None,
    support_resolver=None,
    progress=None,
    namespace="existing_preservation",
):
    """Compare original and candidate sections without fabricated claim ledgers.

    Original text is a preservation baseline, not new source authority. New or
    changed assertions remain subject to the separate source-grounding review.
    """
    from doclayer.curator import editorial

    original_meta = json.loads(json.dumps(original_meta, ensure_ascii=False, default=str))
    previous = change.check_progress.get(namespace, {})
    if previous and not previous.get("editorial_version"):
        history = change.check_progress.setdefault(namespace + "_legacy_history", [])
        history.append(copy.deepcopy(previous))
        change.check_progress.pop(namespace, None)
        if progress is not None:
            progress(change.check_progress)

    context = {
        "preservation_version": VERSION,
        "source_support_version": curate.SOURCE_SUPPORT_VERSION,
        "canonical_binding": canonical_binding,
        "source_policy": source_policy or {},
        "source_binding": source_binding,
        "resolver_binding": getattr(support_resolver, "binding", None),
    }
    unchanged = _metadata_scope(original_meta) == _metadata_scope(change.draft_meta) and not changed_original_body(
        original_body, change.draft_body
    )
    if unchanged:
        # This certifies only retention. It cannot certify added source claims.
        change.check_progress[namespace] = {
            "editorial_version": editorial.VERSION,
            "preservation_version": VERSION,
            "mode": "preservation",
            "complete": True,
            "unchanged_literal": True,
            "original_body": original_body,
            "original_meta": original_meta,
            "canonical_binding": copy.deepcopy(canonical_binding),
            "candidate_body": change.draft_body,
            "candidate_meta": copy.deepcopy(change.draft_meta),
            "context": copy.deepcopy(context),
            "repair_records": {
                curate.evidence_id(record): record.to_dict()
                for record in records
                if record.support_status == "supported"
            },
        }
        if progress is not None:
            progress(change.check_progress)
        return {
            "failures": [],
            "conflicts": [],
            "preserved": [],
            "unsupported": [],
            "pending": [],
            "complete": True,
            "repair_records": existing_repair_records(change, namespace=namespace),
        }

    def checkpoint(state):
        baseline = state.get(namespace)
        if isinstance(baseline, dict):
            # Exact canonical retention baseline for ordinary omission repair;
            # it is not an incoming source or new factual authority.
            baseline["original_body"] = original_body
            baseline["original_meta"] = copy.deepcopy(original_meta)
            baseline["canonical_binding"] = copy.deepcopy(canonical_binding)
        if progress is not None:
            progress(state)

    result = await editorial.review(
        client,
        topic,
        change,
        records,
        corrections,
        char_budget=char_budget,
        progress=checkpoint,
        namespace=namespace,
        mode="preservation",
        original_body=original_body,
        original_meta=original_meta,
        context=context,
    )
    state = change.check_progress[namespace]
    state["preservation_version"] = VERSION
    # These are the actual current caller records, never synthetic old facts.
    state["repair_records"] = {
        curate.evidence_id(record): record.to_dict() for record in records if record.support_status == "supported"
    }
    complete = result.get("complete") is True and not blocking_failures(result)
    state["preservation_complete"] = complete
    checkpoint(change.check_progress)
    return {
        **result,
        "preserved": [],
        "unsupported": [],
        "pending": [] if complete else [namespace],
        "complete": complete,
        "repair_records": existing_repair_records(change, namespace=namespace),
    }


def existing_repair_records(change, *, namespace="existing_preservation"):
    """Return actual currently reviewed source records for ordinary repair."""
    state = change.check_progress.get(namespace, {})
    if state.get("preservation_version") != VERSION:
        return []
    return [
        Evidence.from_dict(record)
        for record in state.get("repair_records", {}).values()
        if record.get("support_status") == "supported" and not str(record.get("source_id", "")).startswith("canonical:")
    ]


def _literal_retained(span, original, candidate):
    """Unchanged literal assertion plus owner/ancestor bytes is not a deletion.

    This grants no source support and cannot bypass the mandatory whole-body
    checker. A changed owner heading or removed governing context fails closed.
    """
    original_units = page_units(original)
    candidates = {unit.id: unit for unit in page_units(candidate)}
    ancestors = []
    for unit in original_units:
        ancestors = [parent for parent in ancestors if parent.level < unit.level]
        if span in unit.text:
            current = candidates.get(unit.id)
            if (
                current is not None
                and unit.text in current.text
                and all(parent.id in candidates and parent.text in candidates[parent.id].text for parent in ancestors)
            ):
                return True
        ancestors.append(unit)
    return False


def _metadata_scope(metadata):
    """Keep complete structured governing scope, excluding root administration.

    Nested maps/lists retain their exact normalized structure; an array of
    versions/platforms is a condition, not permission to reuse literal proof.
    Only root bookkeeping keys are excluded: identical nested names can be
    meaningful domain fields and must remain part of scope.
    """
    administrative = {
        "schema_version",
        "privacy",
        "source_origin",
        "source_lineage",
        "tags",
        "updated",
        "title",
        "subject_type",
        "concept_id",
        "address",
        "url",
        "fetched_at",
        "publisher",
        "published_info",
        "inspected_at",
    }
    combined = {
        **metadata,
        **(metadata.get("applicability") if isinstance(metadata.get("applicability"), dict) else {}),
    }
    return json.loads(
        json.dumps(
            {key: value for key, value in combined.items() if key not in administrative},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
    )
