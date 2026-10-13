"""Replan absent legacy claim identities without guessing or losing valid work."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from doclayer import frontmatter
from doclayer.curator import curate, gittools, placement
from doclayer.curator.model import ModelError
from doclayer.curator.state import Evidence, PageChange


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


async def readmit_invalid_saved(pipeline, state, client, *, only_change=None):
    """Only identities absent from retained evidence invalidate a saved plan.

    Source rejection, policy exclusion and unreviewed support are different:
    those known obligations cannot disappear through replanning. Replacement
    placement is bounded, source-backed, durable and per group. Other groups
    and the job's original publication/base identity are never rewritten.
    """
    if state.correction_of:
        return False
    raw = {curate.evidence_id(Evidence.from_dict(row)): row for row in pipeline.store.read_evidence(state.job_id)}
    checkout = Path(pipeline.cfg.paths.brain_checkout).expanduser()
    changed = False
    for change in list(state.changes):
        if change.status not in ("pending", "drafted", "failed", "validated") or (
            only_change is not None and change is not only_change
        ):
            continue
        absent = [identity for identity in change.claim_ids if identity not in raw]
        if not absent:
            continue
        groups = state.evidence_selection.setdefault("plan_readmission", {})
        saved = groups.setdefault(
            change.group_id,
            {
                "original": copy.deepcopy(change.to_dict()),
                "invalid_claims": {
                    identity: {
                        "disposition": "absent retained identity; never corrected by spelling",
                        "original_path": change.path,
                    }
                    for identity in absent
                },
                "phase": "invalid-plan",
            },
        )
        note = "Invalid saved placement: absent retained claim IDs " + ", ".join(absent)
        if note not in change.notes:
            change.notes.append(note)
        pipeline._persist(state)
        pipeline._check_cancel(state)
        if state.publication.commit_sha or state.publication.pushed or state.publication.merged:
            raise ModelError("immutable publication prevents invalid saved placement readmission", transient=True)
        original = saved["original"]
        valid = [identity for identity in original["claim_ids"] if identity in raw]
        available = {curate.evidence_id(record): record for record in pipeline._evidence(state)}
        unavailable = [identity for identity in valid if identity not in available]
        if unavailable:
            saved["phase"] = "known-obligation-unavailable"
            saved["unavailable"] = unavailable
            pipeline._persist(state)
            raise ModelError(
                "known required source obligations unavailable during placement readmission: " + ", ".join(unavailable),
                transient=True,
            )
        allowed_sources = set(original["source_ids"]) or {available[identity].source_id for identity in valid}
        if not allowed_sources or any(available[identity].source_id not in allowed_sources for identity in valid):
            raise ModelError("invalid saved placement lacks authoritative allowed source scope", transient=False)
        probe = copy.deepcopy(change)
        probe.claim_ids = valid
        await pipeline._verify_selected(state, probe, client)
        frontier = {}
        records = [
            record
            for record in pipeline._evidence(state, frontier)
            if record.source_id in allowed_sources and record.support_status == "supported"
        ]
        supported = {curate.evidence_id(record) for record in records}
        if any(identity not in supported for identity in valid):
            raise ModelError(
                "known required source obligations remain unverified during placement readmission", transient=True
            )
        if not records:
            raise ModelError("no source-supported material available for placement readmission", transient=True)
        gittools.fetch(checkout, pipeline.cfg.publish.remote)
        base = gittools.rev_parse(checkout, f"{pipeline.cfg.publish.remote}/{pipeline.cfg.publish.base_branch}")
        pages = pipeline._candidate_pages(state, checkout, base)
        original_raw = gittools.read_blob(checkout, base, original["path"])
        if original_raw is not None and original["path"] not in {path for path, _, _ in pages}:
            meta, body = frontmatter.parse(original_raw)
            pages.append((original["path"], meta, body))
        context = _hash(
            [
                original,
                base,
                state.source_policy,
                state.exclusions,
                [record.to_dict() for record in records],
                sorted(allowed_sources),
            ]
        )
        if saved.get("binding") != context:
            saved.update(binding=context, canonical_commit=base, ledger={}, replacement_pages=[], phase="replanning")
            pipeline._persist(state)

        def retain(ledger):
            saved["ledger"] = ledger
            pipeline._persist(state)
            pipeline._check_cancel(state)

        # Every original real obligation must have an exact placement or an
        # independently checked canonical coverage proof before admission.
        while True:
            ledger = saved["ledger"]
            settled = {
                identity
                for identity, row in ledger.get("claims", {}).items()
                if row.get("status") in ("planned", "covered")
            }
            if valid and set(valid) <= settled:
                break
            plan = await placement.plan_batch(
                client,
                state.intent
                + "\nReplan invalid saved group for "
                + original["path"]
                + ". Preserve its legitimate required material and audit the original target. Original purpose: "
                + original["rationale"],
                state.exclusions,
                records,
                pages,
                [],
                char_budget=pipeline.cfg.limits.prompt_char_budget,
                ledger=ledger,
                frontier={identity: value for identity, value in frontier.items() if identity in supported},
                progress=retain,
            )
            saved["ledger"] = plan["ledger"]
            for page in plan.get("pages", []):
                previous = next((item for item in saved["replacement_pages"] if item["path"] == page["path"]), None)
                if previous is None:
                    saved["replacement_pages"].append(copy.deepcopy(page))
                else:
                    for key in ("claim_ids", "source_ids", "related"):
                        previous[key] = list(dict.fromkeys(previous.get(key, []) + page.get(key, [])))
            if plan.get("plan_id"):
                saved["ledger"] = placement.acknowledge(saved["ledger"], plan["plan_id"])
            pipeline._persist(state)
            # A claim whose placement was dropped alone (rejected or too large) is a recorded gap.
            valid = [
                identity
                for identity in valid
                if saved["ledger"].get("claims", {}).get(identity, {}).get("reason") not in placement.DROPPED_ALONE
            ]
            new_settled = {
                identity
                for identity, row in saved["ledger"].get("claims", {}).items()
                if row.get("status") in ("planned", "covered")
            }
            if not valid or set(valid) <= new_settled:
                break
            if new_settled == settled:
                raise ModelError("placement readmission has unresolved legitimate required identities", transient=True)
        ledger = saved["ledger"]
        targets = {
            identity: (row.get("target") or row.get("coverage", {}).get("path"))
            for identity, row in ledger.get("claims", {}).items()
            if identity in valid
        }
        if any(not targets.get(identity) for identity in valid):
            raise ModelError("placement readmission lacks an exact destination for required material", transient=True)
        replacement = saved["replacement_pages"]
        # Check all collisions before mutating the affected group or siblings.
        if len({item["path"] for item in replacement}) != len(replacement):
            raise ModelError("placement readmission produced conflicting replacement targets", transient=True)
        if any(item["path"] == other.path for item in replacement for other in state.changes if other is not change):
            raise ModelError("placement readmission conflicts with another preserved group", transient=False)
        original_meta, original_body = frontmatter.parse(original_raw or "")
        original_item = next((item for item in replacement if item["path"] == original["path"]), None)
        # Preserve an actual original target for the normal full assertion,
        # source, required-coverage and existing-fact audit even when the model
        # moves the incoming material or proves it already covered elsewhere.
        change.claim_ids = list(
            dict.fromkeys(
                (original_item or {}).get("claim_ids", [])
                + [identity for identity in valid if targets[identity] == original["path"]]
            )
        )
        change.source_ids = sorted(allowed_sources)
        change.base_commit = base
        change.base_blob = gittools.blob_hash(checkout, f"{base}:{change.path}")
        change.action = "update" if original_raw is not None else "create"
        change.draft_progress = {}
        change.check_progress = {}
        change.reconcile_progress = {}
        change.reconcile_check_progress = {}
        change.missing_evidence = []
        change.error = None
        if original_item:
            change.title = original_item["title"]
            change.subject_type = original_item["subject_type"]
            change.rationale = original_item["rationale"]
            change.related = original_item.get("related", [])
            change.draft_body = ""
            change.draft_meta = {}
            change.status = "pending"
        elif original_raw is not None:
            change.draft_meta = json.loads(json.dumps(original_meta, default=str))
            change.draft_body = original_body
            change.status = "drafted"
        else:
            change.status = "skipped"
            change.notes.append("Invalid original target did not exist; explicit replacement targets retained below.")
        for item in replacement:
            if item["path"] == original["path"]:
                continue
            fresh = PageChange(
                group_id=change.group_id + ":readmit:" + _hash(item["path"])[:10],
                path=item["path"],
                action="update" if gittools.read_blob(checkout, base, item["path"]) is not None else "create",
                title=item["title"],
                subject_type=item["subject_type"],
                rationale=item["rationale"],
                claim_ids=list(item["claim_ids"]),
                source_ids=list(item["source_ids"]),
                related=list(item.get("related", [])),
                base_commit=base,
                base_blob=gittools.blob_hash(checkout, f"{base}:{item['path']}"),
            )
            fresh.notes.append(
                "Explicit replacement of invalid saved placement " + change.group_id + " at " + original["path"]
            )
            state.changes.append(fresh)
        saved.update(
            phase="admitted",
            required_targets=targets,
            replacement_targets=[item["path"] for item in replacement],
            original_target_preserved_for_audit=original_raw is not None,
        )
        change.notes.append(
            "Saved invalid placement replanned from source-supported material; original and replacement identities are recorded in plan_readmission."
        )
        pipeline._persist(state)
        changed = True
    return changed
