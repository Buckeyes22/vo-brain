"""Current source-review coverage, separated from durable historical diagnostics."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from doclayer.curator.degrade import GAP_MARK


@dataclass(frozen=True)
class CoverageAssessment:
    coverage: str
    current_gaps: tuple[str, ...]
    historical_gaps: tuple[str, ...]


def frontier_context(state) -> str:
    """Pure binding for trusted editorial eligibility under current sources."""
    from doclayer.curator.curate import SOURCE_SUPPORT_VERSION

    sources = [
        {
            key: getattr(source, key)
            for key in ("id", "url", "role", "scope", "redirect_chain", "content_hash", "status")
        }
        | {"retained_extract_hash": source.relevance.get("retained_extract_hash")}
        for source in state.sources
    ]
    material = [
        SOURCE_SUPPORT_VERSION,
        state.source_policy,
        state.exclusions,
        state.evidence_selection.get("retained_evidence_hash"),
        sorted(sources, key=lambda source: source["id"]),
    ]
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _reviewed(source) -> bool:
    return (
        source.status == "fetched"
        and source.extraction_complete
        and source.review_unit_count > 0
        and len(set(source.reviewed_sections)) >= source.review_unit_count
        and not (source.omitted_sections or source.omitted_categories or source.unknown_reviewed_sections)
    )


NOT_INTEGRATED_GAP = re.compile(r": claim \S+ not integrated \(")


def not_integrated_gap(path, identity, row):
    """The one wording for a claim the editor could not prove on a page."""
    reason = row.get("reason", "unproven") if isinstance(row, dict) else "unproven"
    return f"{path}: claim {identity} not integrated ({reason})"


def assess_coverage(state) -> CoverageAssessment:
    """Derive current coverage without rewriting flags, logs, or source state.

    Known transient diagnostics become historical only when their authoritative
    current state proves recovery. Unexplained legacy/explicit incomplete flags
    and unknown incomplete reasons remain conservative; they are never erased.
    """
    current: list[str] = []
    historical: list[str] = []
    recovery_explains_flag = False
    basis = getattr(state, "coverage_basis", None)
    if basis is None:
        basis = state.coverage

    def add(message):
        if message not in current:
            current.append(message)

    for source in state.sources:
        prefix = f"{source.label} [{source.id}]"
        if source.status in ("duplicate", "rejected") or (source.status == "skipped" and source.exclusions_hit):
            continue
        if source.status != "fetched":
            add(f"{prefix}: source {source.status}; {source.error or 'not reviewed'}")
            continue
        if source.omitted_sections:
            add(f"{prefix}: {len(source.omitted_sections)} source sections omitted")
        for category, reason in source.omitted_categories.items():
            add(f"{prefix}: omitted {category}: {reason}")
        if source.unknown_reviewed_sections:
            add(f"{prefix}: {len(source.unknown_reviewed_sections)} review identities unknown")
        if not source.extraction_complete:
            add(f"{prefix}: extraction review unfinished")
        if not source.review_unit_count:
            add(f"{prefix}: complete source review map unavailable")
        elif len(set(source.reviewed_sections)) < source.review_unit_count:
            add(f"{prefix}: {len(set(source.reviewed_sections))}/{source.review_unit_count} source units reviewed")

    placement = state.evidence_selection.get("placement", {})
    ledger = placement.get("ledger", {})
    current_frontier = ledger.get("frontier_context") == frontier_context(state)

    def retired(identity):
        if not current_frontier:
            return False
        row = ledger.get("claims", {}).get(identity, {})
        disposition = row.get("disposition", {})
        kind = row.get("status")
        if (
            kind not in ("excluded", "rejected")
            or disposition.get("status") != kind
            or not isinstance(disposition.get("binding"), str)
            or not disposition["binding"]
        ):
            return False
        if kind == "excluded":
            return True
        from doclayer.curator.curate import SOURCE_SUPPORT_VERSION

        verdict = state.evidence_verification.get(identity, {})
        return (
            verdict.get("status") == "unsupported"
            and not verdict.get("verification_pending")
            and verdict.get("support_version") == SOURCE_SUPPORT_VERSION
            and disposition.get("support_version") == SOURCE_SUPPORT_VERSION
            and bool(disposition.get("source_binding"))
            and verdict.get("source_binding") == disposition["source_binding"]
        )

    for identity, row in ledger.get("claims", {}).items():
        if row.get("status") in ("excluded", "rejected") and not retired(identity):
            add(
                f"placement claim {identity}: exclusion/rejection disposition is not proven under current source/policy"
            )
    placement_omissions = placement.get("omitted_count")
    if isinstance(placement_omissions, int) and placement_omissions > 0:
        add(
            f"placement editorial coverage unresolved: {placement_omissions} retained records omitted; no per-record disposition proof"
        )

    def canonically_covered(identity):
        from doclayer.curator.curate import SOURCE_SUPPORT_VERSION
        from doclayer.curator.placement import VERSION

        row = ledger.get("claims", {}).get(identity, {})
        proof = row.get("coverage", {})
        source = state.evidence_verification.get(identity, {})
        return (
            current_frontier
            and bool(state.base_commit)
            and ledger.get("canonical_commit") == state.base_commit
            and ledger.get("version") == VERSION
            and row.get("status") == "covered"
            and proof.get("version") == VERSION
            and all(proof.get(key) for key in ("binding", "path", "anchor", "checked"))
            and source.get("status") == "supported"
            and source.get("supported") is True
            and not source.get("verification_pending")
            and source.get("support_version") == SOURCE_SUPPORT_VERSION
            and bool(source.get("source_binding"))
        )

    # A claim the editor could not prove (draft_progress.not_integrated) is a
    # current gap and never counts as published, whatever page it was planned for.
    not_integrated = {}
    for change in state.changes:
        for identity, row in sorted(((change.draft_progress or {}).get("not_integrated") or {}).items()):
            not_integrated[identity] = change.path
            add(not_integrated_gap(change.path, identity, row))
    published = {
        identity
        for change in state.changes
        if change.status == "published"
        for identity in change.claim_ids
        if identity not in not_integrated
    }
    for identity, row in ledger.get("claims", {}).items():
        if row.get("status") == "covered" and not canonically_covered(identity):
            add(f"placement claim {identity}: current canonical coverage proof unavailable")
        elif row.get("status") == "pending":
            add(f"placement claim {identity}: editorial disposition pending")
        elif row.get("status") == "failed":
            add(f"placement claim {identity}: {row.get('reason') or 'placement failed'}")
        elif row.get("status") == "planned" and identity not in published:
            add(f"placement claim {identity}: assigned page not published")

    # Older jobs can resume directly at draft without the newer placement
    # frontier. Optional source work remains visible even though it is not an
    # obligation to review eighty unrelated records before editing a page.
    from doclayer.curator.curate import SOURCE_SUPPORT_VERSION

    current_context = frontier_context(state)
    for path, selection in state.evidence_selection.items():
        if not isinstance(selection, dict) or not (
            selection.get("optional_claims") or selection.get("pending_optional")
        ):
            continue
        bindings = selection.get("optional_proofs", selection.get("pending_optional_proofs", {}))
        unresolved = []
        editorial = []
        for identity in selection.get("optional_claims", selection.get("pending_optional", [])):
            if retired(identity):
                continue  # fresh policy/source disposition supersedes this snapshot
            expected = bindings.get(identity, {})
            actual = state.evidence_verification.get(identity, {})
            resolved = (
                selection.get("frontier_context") == current_context
                and expected.get("support_version") == SOURCE_SUPPORT_VERSION
                and actual.get("support_version") == SOURCE_SUPPORT_VERSION
                and bool(expected.get("source_binding"))
                and actual.get("source_binding") == expected["source_binding"]
                and not actual.get("verification_pending")
                and actual.get("status") in ("supported", "unsupported")
                and (actual.get("status") != "supported" or actual.get("supported") is True)
            )
            if not resolved:
                unresolved.append(identity)
            # Source admission does not prove editorial incorporation. Only a
            # current placement proof or actual publication retires this work.
            covered = canonically_covered(identity)
            if not (resolved and actual.get("supported") is True and (identity in published or covered)):
                editorial.append(identity)
        if editorial:
            add(f"{path}: {len(editorial)} optional claims await editorial disposition; retained for continuation")
        if unresolved:
            add(f"{path}: {len(unresolved)} optional source claims remain unverified; retained for continuation")

    verification = state.evidence_verification
    for identity, result in verification.items():
        if retired(identity):
            continue
        if result.get("verification_pending") or result.get("status") == "unknown":
            add(f"source-support unknown {identity}: {result.get('reason', 'verification pending')}")

    # A published page whose reconciliation kept fresh operator material over
    # a draft section did not publish that section's contribution.
    for change in getattr(state, "changes", None) or []:
        if change.status != "published":
            continue  # an unpublished reconciliation superseded nothing
        for row in getattr(change, "superseded_by_fresh", None) or []:
            claims = row.get("claim_ids") or []
            add(
                f"{change.path}: draft section {row.get('section', '')!r} (draft text sha256 "
                f"{str(row.get('digest', ''))[:16]}){' with claims ' + ', '.join(claims) if claims else ''} "
                "superseded by fresh publication; continue to re-draft against current page"
            )

    for note in state.coverage_gaps:
        note = str(note)
        support = re.match(r"^source-support unknown (\S+):", note)
        if support:
            result = verification.get(support[1])
            if retired(support[1]) or (
                result
                and not result.get("verification_pending")
                and result.get("status") in ("supported", "unsupported")
            ):
                historical.append(note)
                recovery_explains_flag = True
            elif result and result.get("reason") and note != f"source-support unknown {support[1]}: {result['reason']}":
                # A newer authoritative unknown/pending cause remains current;
                # do not repeat an earlier cache/error cause as present fact.
                historical.append(note)
            else:
                add(note)
            continue
        if (
            note.startswith("source-support rejection ")
            or "excluded claim (" in note
            or "incompatible source applicability claim excluded" in note
            or note.startswith("placement: material already covered")
        ):
            historical.append(note)
            continue
        if note.startswith("placement evidence selection:"):
            counts = re.match(r"placement evidence selection: (\d+) of (\d+) records used", note)
            legacy_omissions = bool(counts and int(counts[1]) < int(counts[2]))
            if placement_omissions == 0:
                historical.append(note)
                recovery_explains_flag = True
            elif (isinstance(placement_omissions, int) and placement_omissions > 0) or legacy_omissions:
                add(note)
            else:
                historical.append(note)
            continue
        if note.startswith("discovery stopped:"):
            waiting = any(
                source.status in ("pending", "scored", "deferred")
                or (source.status == "skipped" and not source.exclusions_hit)
                for source in state.sources
            )
            if waiting:
                add(note)
            else:
                historical.append(note)
                recovery_explains_flag = True
            continue
        if note.startswith("stopped at configured limit ("):
            if state.stage == "report" or state.status in ("published", "covered", "partial"):
                historical.append(note)
                recovery_explains_flag = True
            else:
                add(note)
            continue
        matching = [source for source in state.sources if note.startswith(source.label + ":")]
        if matching and any(
            phrase in note
            for phrase in (
                "cached extract missing",
                "extraction output truncated",
                "selective review;",
                "selective section review",
                "discovery cache unavailable",
            )
        ):
            resolved = all(_reviewed(source) for source in matching)
            if "discovery cache unavailable" in note:
                resolved = resolved and all(source.relevance.get("links_expanded") for source in matching)
            if resolved:
                historical.append(note)
                recovery_explains_flag = True
            else:
                add(note)
            continue
        if NOT_INTEGRATED_GAP.search(note):
            add(note)  # an unproven claim stays a current gap
            continue
        if GAP_MARK in note:
            add(note)  # a unit dropped for a rejected model reply stays a current gap
            continue
        if note.startswith("unpublished after reconciliation:"):
            if any(change.status in ("failed", "conflict", "skipped") for change in state.changes):
                add(note)
            else:
                historical.append(note)
            continue
        # The historical list also contains operational notes which were never
        # coverage failures. Preserve unknown explicit incomplete diagnostics.
        if basis == "incomplete" or re.search(
            r"\b(?:omitted|incomplete|unreviewed|unavailable|unreadable|unfetched)\b", note, re.I
        ):
            add(note)
        else:
            historical.append(note)

    if basis == "incomplete" and not current and not recovery_explains_flag:
        add("legacy or explicit incomplete coverage has no proven resolved reason")
    return CoverageAssessment("incomplete" if current else "complete", tuple(current), tuple(historical))
