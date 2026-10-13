"""The curator pipeline: one import job from material to published pages.

Stages, persisted after each: intake → discover → extract → place → draft →
validate → publish → report. A job records its stage, so a restart resumes
there; finished fetches/extracts are reused. Cancellation is checked between
stages and between sources. Budgets (sources, bytes, model calls, active
time) are workload limits recorded in the job state: hitting one preserves
completed work, marks coverage incomplete, and leaves the job resumable —
it never silently resets or masks partial work.

A correction job (state.correction_of set, changes pre-seeded) starts at the
draft stage: the operator instruction is the change's intent, validated and
published like any other change.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from doclayer import frontmatter as fm
from doclayer.curator import (
    capacity,
    curate,
    degrade,
    gittools,
    intake,
    policy,
    preservation,
    single_pass,
)
from doclayer.curator.capacity import CAPACITY_GENERATION, ReviewCapacity
from doclayer.curator.config import CuratorConfig, stage_config
from doclayer.curator.editorial import RETAINED_NOTE, EditorialContextOverflow
from doclayer.curator.forgejo import ForgejoError, PublishNotConfigured
from doclayer.curator.gittools import GitError
from doclayer.curator.jobstore import JobStore
from doclayer.curator.model import (
    ModelClient,
    ModelError,
    OutputTruncated,
    ReplyRejected,
    UnitTooLarge,
)
from doclayer.curator.publish import Publisher, ReconcileFailed
from doclayer.curator.state import (
    STAGES,
    SUBJECT_TYPES,
    BudgetExceeded,
    Evidence,
    JobState,
    PageChange,
    SourceRecord,
)
from doclayer.curator.validate import (
    Failure,
    KnownPages,
    blockers,
    h1_text,
    normalize_setext,
    restore_dropped_sections,
    restore_titles,
    run_deterministic_checks,
)

# Bump when repair semantics change: saved repair contexts bound to an older
# generation are invalidated on resume instead of replaying stale failures.
REPAIR_BINDING_GENERATION = 1
# Deterministic scope/title repairs per page per validation loop: each must
# produce a draft state not seen before, and never more than this many.
MAX_DETERMINISTIC_PASSES = 20
# Dedicated model passes for a draft citing an excluded source, per page; they
# do not spend the general repair passes and stay within the model-call limit.
MAX_SCOPE_REPAIR_PASSES = 2
FETCH_RETRY_BACKOFF_S = 1.0  # seconds before the first retry of a transient fetch failure; grows linearly


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class SourceSupportUnavailable(ModelError):
    """Completed source-specific unknown, never an interrupted verification."""

    def __init__(self, identities: list[str]):
        self.identities = identities
        super().__init__("selected source support unavailable: " + ", ".join(identities), transient=False)


class CancelledByRequest(RuntimeError):
    pass


def hydrate_citations(body: str, evidence: list[Evidence]) -> str:
    references = {e.source_id: e.url or e.label for e in evidence}
    pattern = r"\[\[?(src-[0-9a-f]{10}(?:\s*,\s*src-[0-9a-f]{10})*)\]\]?"

    def replace(match):
        ids = [item.strip() for item in match.group(1).split(",")]
        if any(source not in references for source in ids):
            return match.group(0)
        return " ".join(
            f"[Source](<{references[source]}>)"
            if references[source].startswith(("https://", "http://"))
            else f"[source: {references[source]}]"
            for source in ids
        )

    return re.sub(pattern, replace, body)


def finalize_draft(change: PageChange, draft: dict[str, Any], evidence: list[Evidence]) -> None:
    """Hydrate citations without invalidating an internally bound edit checkpoint.

    Raw model progress is stripped by curate. A mismatched checkpoint is never
    rebound here: only the exact body produced by the bounded editor qualifies.
    Frozen task identities and views continue to describe the original tasks.
    """
    raw = str(draft.get("body", change.draft_body))
    progress = copy.deepcopy(draft.get("draft_progress") or {})
    body = hydrate_citations(raw, evidence)
    if progress.get("body_hash") == hashlib.sha256(raw.encode()).hexdigest():
        progress["body_hash"] = hashlib.sha256(body.encode()).hexdigest()
        replacements = set()
        for rename in progress.get("heading_renames", []):
            # The old anchor still names the unchanged canonical base; only
            # replacements (including intermediate chain anchors) hydrate.
            identity = rename.get("unit_id")
            if (identity, rename["old"]) in replacements:
                rename["old"] = hydrate_citations(rename["old"], evidence)
            replacements.add((identity, rename["new"]))
            rename["new"] = hydrate_citations(rename["new"], evidence)
    if "claim_anchors" in progress:
        from doclayer.curator.editing import rebind_claim_anchors

        progress["claim_anchors"] = rebind_claim_anchors(
            progress["claim_anchors"], raw, body, lambda anchor: hydrate_citations(anchor, evidence)
        )
    change.draft_body = body
    change.draft_progress = progress


MIN_ANCHOR = 20  # shorter claim anchors are too generic to attribute a dropped section


class Pipeline:
    def __init__(
        self,
        cfg: CuratorConfig,
        store: JobStore,
        *,
        fetch_transport: Any = None,
        inference_transport: Any = None,
        publisher: Publisher | None = None,
        clock=None,
    ) -> None:
        # every read through the pipeline (prompt packing, output caps, model) follows the curation stage
        self._base_cfg = cfg
        self.cfg = stage_config(cfg, "curation")
        self.store = store
        self._fetch_transport = fetch_transport
        self._inference_transport = inference_transport
        self._publisher = publisher
        self._clock = clock or time.monotonic
        self._active_last_tick: float | None = None

    # -- helpers --------------------------------------------------------- #

    def _persist(self, state: JobState, *, check_budget: bool = True) -> None:
        if state.coverage_basis is None:
            state.coverage_basis = state.coverage
        state.coverage = state.effective_coverage
        self._account_active_time(state)
        with self.store.control_lock(state.job_id):
            if self.store.exists(state.job_id):
                state.cancel_requested |= self.store.load(state.job_id).cancel_requested
            self.store.save(state)
        if (
            check_budget
            and self._active_last_tick is not None
            and state.status in ("processing", "validating", "publishing")
        ):
            self._check_time(state)

    def _account_active_time(self, state: JobState) -> None:
        if self._active_last_tick is not None:
            now = self._clock()
            state.usage.active_seconds += max(0.0, now - self._active_last_tick)
            self._active_last_tick = now

    def _skip_active_time(self) -> None:
        """Restart the active-time tick at now: time spent deferring to other
        users of a shared model is not processing time and must not exhaust
        max_active_seconds (a job that waits is deferred, never discarded)."""
        if self._active_last_tick is not None:
            self._active_last_tick = self._clock()

    def _check_cancel(self, state: JobState) -> None:
        if self.store.exists(state.job_id):
            state.cancel_requested |= self.store.load(state.job_id).cancel_requested
        if state.cancel_requested:
            raise CancelledByRequest(state.job_id)

    def _check_time(self, state: JobState) -> None:
        self._account_active_time(state)
        limit = float(state.limits.get("max_active_seconds", self.cfg.limits.max_active_seconds))
        if state.usage.active_seconds >= limit:
            raise BudgetExceeded("active_time")

    def _before_model_call(self, state: JobState) -> None:
        self._check_cancel(state)
        self._persist(state)  # account/check the active limit before every HTTP attempt
        self._check_cancel(state)  # persist may have observed concurrent cancellation
        self._check_time(state)

    def _admitted_model_call(self, state: JobState) -> None:
        by_stage = state.usage.calls_by_stage
        by_stage[state.stage] = by_stage.get(state.stage, 0) + 1
        self._persist(state, check_budget=False)
        # Admission stays charged, but observed cancellation stops dispatch.
        self._check_cancel(state)

    def _stage_cfg(self, stage: str) -> CuratorConfig:
        """The config a model-calling stage reads from: its inference profile
        and the prompt budget derived from it."""
        return self.cfg if stage == "curation" else stage_config(self._base_cfg, stage)

    def _model(self, state: JobState, stage: str = "curation") -> ModelClient:
        return ModelClient(
            self._stage_cfg(stage),
            state.usage,
            limits=state.limits,
            transport=self._inference_transport,
            before_call=lambda: self._before_model_call(state),
            admitted_call=lambda: self._admitted_model_call(state),
            check_cancel=lambda: self._check_cancel(state),
            before_idle_wait=lambda: self._account_active_time(state),
            after_idle_wait=self._skip_active_time,
            wait_out_outages=True,
        )

    def _cache_file(self, state: JobState, source: SourceRecord) -> Path:
        path = self.cfg.cache_dir() / state.job_id / f"{source.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _extract_text(self, state: JobState, source: SourceRecord) -> str | None:
        path = self._cache_file(state, source)
        if not path.exists() and state.continuation_of and source.extract_path:
            inherited = Path(source.extract_path).resolve()
            if inherited.is_relative_to(self.cfg.cache_dir().resolve()):
                path = inherited
        try:
            text = path.read_text(encoding="utf-8")
            # A readable retained extract resolves only the cache omission;
            # source review and discovery remain independently required.
            source.omitted_categories.pop("cache", None)
            source.relevance["retained_extract_hash"] = hashlib.sha256(text.encode()).hexdigest()
            return text
        except OSError:
            return None

    def _store_extract(self, state: JobState, source: SourceRecord, text: str) -> None:
        self._cache_file(state, source).write_text(text, encoding="utf-8")
        source.extract_path = str(self._cache_file(state, source))
        source.relevance["retained_extract_hash"] = hashlib.sha256(text.encode()).hexdigest()

    def _evidence(self, state: JobState, dispositions: dict | None = None) -> list[Evidence]:
        latest = {}
        for raw in self.store.read_evidence(state.job_id):
            record = Evidence.from_dict(raw)
            latest[curate.evidence_id(record)] = record
        records = list(latest.values())
        state.evidence_selection["retained_evidence_hash"] = hashlib.sha256(
            json.dumps(
                [latest[identity].to_dict() for identity in sorted(latest)], sort_keys=True, ensure_ascii=False
            ).encode()
        ).hexdigest()
        permitted = []
        sources = {s.id: s for s in state.sources}
        texts = {
            identity: self._extract_text(state, sources[identity]) if identity in sources else None
            for identity in {record.source_id for record in records}
        }
        source_hashes = curate.source_support_hashes(texts)
        # Single-pass jobs accept deterministic verbatim-quote support; every
        # other job requires the model-verified source support version.
        support_versions = {curate.SOURCE_SUPPORT_VERSION}
        if state.mode == "single-pass":
            support_versions.add(curate.SP_QUOTE_SUPPORT_VERSION)

        def disposition(record, status, reason):
            if dispositions is not None:
                source = sources.get(record.source_id)
                binding = hashlib.sha256(
                    json.dumps(
                        [
                            curate.SOURCE_SUPPORT_VERSION,
                            curate.source_support_binding(record, texts, source_hashes=source_hashes),
                            state.source_policy,
                            state.exclusions,
                            source.scope if source else {},
                            source.role if source else None,
                            source.url if source else None,
                            source.redirect_chain if source else [],
                        ],
                        sort_keys=True,
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
                dispositions[curate.evidence_id(record)] = {
                    "status": status,
                    "reason": reason,
                    "binding": binding,
                    "source_binding": curate.source_support_binding(record, texts, source_hashes=source_hashes),
                    "support_version": curate.SOURCE_SUPPORT_VERSION,
                }

        for evidence in records:
            if evidence.original_quote is None:
                evidence.original_quote = evidence.quote
            allowed, reason = policy.claim_decision(
                evidence.url, evidence.text, evidence.applicability, state.source_policy
            )
            if not allowed:
                disposition(evidence, "excluded", reason)
                continue
            _, _, rule = policy.url_decision(evidence.url, state.source_policy)
            source = sources.get(evidence.source_id)
            if source and (
                any(
                    not policy.claim_decision(location, evidence.text, evidence.applicability, state.source_policy)[0]
                    for location in source.redirect_chain
                )
                or any(
                    evidence.applicability.get(k) and str(evidence.applicability[k]).casefold() != str(v).casefold()
                    for k, v in source.scope.items()
                )
            ):
                disposition(evidence, "excluded", "redirect policy or explicit source applicability conflict")
                continue
            evidence.applicability = {
                **state.source_policy.get("global", {}).get("applicability", {}),
                **(source.scope if source else {}),
                **rule.get("applicability", {}),
                **evidence.applicability,
            }
            evidence.role = source.role if source else evidence.role
            allowed, reason = policy.claim_decision(
                evidence.url, evidence.text, evidence.applicability, state.source_policy
            )
            if not allowed:
                disposition(evidence, "excluded", reason)
                continue
            verification = state.evidence_verification.get(curate.evidence_id(evidence))
            if (
                verification
                and verification.get("support_version") in support_versions
                and verification.get("source_binding")
                == curate.source_support_binding(evidence, texts, source_hashes=source_hashes)
            ):
                evidence.support_status = (
                    "unknown"
                    if verification.get("verification_pending")
                    else verification.get("status", "supported" if verification.get("supported") else "unsupported")
                )
                if evidence.support_status == "supported":
                    evidence.quote = verification.get("excerpt", evidence.quote)
            else:
                # A stored editorial marker is never current source proof.
                # Missing or stale verifier bindings both require grounding.
                evidence.support_status = "unknown"
            if evidence.support_status == "unsupported":
                disposition(evidence, "rejected", verification.get("reason", "current source contradicts claim"))
                continue
            disposition(
                evidence,
                "supported" if evidence.support_status == "supported" else "pending",
                (verification or {}).get("reason", "current source verification required"),
            )
            permitted.append(evidence)
        return permitted

    def _support_resolver(self, state: JobState, client: ModelClient):
        # The article reviewer receives complete admitted records. It binds
        # retained source changes without launching hidden per-claim searches.
        hashes = curate.source_support_hashes(
            {source.id: self._extract_text(state, source) for source in state.sources}
        )
        material = {
            "raw": self.store.read_evidence(state.job_id),
            "sources": hashes,
            "policy": state.source_policy,
            "canonical_revision": state.base_commit,
            "source_version": curate.SOURCE_SUPPORT_VERSION,
            "source_metadata": [
                {
                    "id": source.id,
                    "kind": source.kind,
                    "url": source.url,
                    "role": source.role,
                    "scope": source.scope,
                    "redirect_chain": source.redirect_chain,
                }
                for source in state.sources
            ],
        }
        return SimpleNamespace(
            binding=hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        )

    def _page_records(self, change: PageChange, records: list[Evidence]) -> list[Evidence]:
        return [
            e
            for e in records
            if not change.source_ids or e.source_id in change.source_ids or curate.evidence_id(e) in change.claim_ids
        ]

    # -- editorial review capacity ------------------------------------------ #

    def _base_page(self, state: JobState, change: PageChange) -> tuple[dict[str, Any], str]:
        if change.action != "update":
            return {}, ""
        revision = change.base_commit or state.base_commit
        if not revision:
            return {}, ""
        try:
            raw = gittools.read_blob(Path(self.cfg.paths.brain_checkout).expanduser(), revision, change.path)
        except GitError:
            return {}, ""
        return fm.parse(raw) if raw else ({}, "")

    def _capacity(self, state: JobState) -> ReviewCapacity:
        return ReviewCapacity(
            self.cfg.limits.prompt_char_budget,
            state.intent,
            state.source_policy,
            self.__dict__.setdefault("_review_fit_cache", {}),
        )

    def _review_fits(
        self, state: JobState, change: PageChange, records: list[Evidence], *, grow: bool = False, actual: bool = False
    ) -> bool:
        return self._capacity(state).fits(change, self._base_page(state, change), records, grow=grow, actual=actual)

    def _select_page_evidence(self, state: JobState, change: PageChange, records: list[Evidence]) -> list[Evidence]:
        """The records drafting is given: assigned claims plus what the review can hold. Pure."""
        verified = [record for record in records if record.support_status == "supported"]
        selected = curate.select_page_evidence(verified, change, limit=max(80, len(set(change.claim_ids))))
        return self._capacity(state).bounded(change, self._base_page(state, change), verified, selected)

    def _review_evidence(self, state: JobState, change: PageChange, records: list[Evidence]) -> list[Evidence]:
        """Records the grounding review of an existing draft sees. Pure: no state or change is written."""
        everything = [e for e in self._page_records(change, records) if e.support_status == "supported"]
        return self._capacity(state).review_records(
            change,
            self._base_page(state, change),
            everything,
            self._select_page_evidence(state, change, records),
            state.evidence_selection.get("drafted:" + change.path),
        )

    def _require_page_capacity(self, state: JobState, change: PageChange, records: list[Evidence]) -> None:
        """A single assigned claim too large for this page's bounded drafting or
        review frame is dropped from the page (a current gap naming its sizes)."""
        while True:
            try:
                self._capacity(state).require_assigned(change, self._base_page(state, change), records)
                return
            except EditorialContextOverflow as exc:
                identity = (exc.diagnostic or {}).get("claim_id")
                if (exc.diagnostic or {}).get("operation") != "page-capacity" or not degrade.can_record():
                    raise
                degrade.degrade_unit(
                    f"{change.path}: assigned claim {identity}", UnitTooLarge(str(exc), diagnostic=exc.diagnostic)
                )
                change.claim_ids = [claim for claim in change.claim_ids if claim != identity]

    def _require_capacity_for_pending(self, state: JobState) -> None:
        """Every page about to be drafted is checked before any model call of the drafting phase."""
        pending = [change for change in state.changes if change.status == "pending"]
        if not pending:
            return
        evidence = self._evidence(state)
        for change in pending:
            self._require_page_capacity(state, change, evidence)

    def _page_evidence(self, state: JobState, change: PageChange, records: list[Evidence]) -> list[Evidence]:
        # Unknown optional facts are retained for future source admission or
        # claim-specific grounding; they are not prerequisites for editing.
        selected = self._select_page_evidence(state, change, records)
        _, omitted = curate.pack_evidence(selected, max(1000, self.cfg.limits.prompt_char_budget // 2 - 1500))
        change.missing_evidence = [
            claim for claim in change.claim_ids if claim not in {curate.evidence_id(e) for e in selected}
        ]
        # Snapshot the full authoritative eligible pool even when a caller
        # supplies only current supported records for candidate drafting.
        full_pool = self._evidence(state)
        optional = [
            record
            for record in self._page_records(change, full_pool)
            if curate.evidence_id(record) not in change.claim_ids
        ]
        pending = [record for record in optional if record.support_status != "supported"]
        sources = {source.id: source for source in state.sources}
        texts = {
            identity: self._extract_text(state, sources[identity]) if identity in sources else None
            for identity in {record.source_id for record in optional}
        }
        hashes = curate.source_support_hashes(texts)
        from doclayer.curator.coverage import frontier_context

        state.evidence_selection[change.path] = {
            "selected": [curate.evidence_id(record) for record in selected],
            "prompt_omitted": omitted,
            "missing_required": list(change.missing_evidence),
            "eligible_records": len(full_pool),
            # Source support and editorial disposition are distinct. A newly
            # verified optional fact remains in this frontier until placed,
            # independently proven covered, or explicitly retired by policy.
            "optional_claims": [curate.evidence_id(record) for record in optional],
            "optional_proofs": {
                curate.evidence_id(record): {
                    "source_binding": curate.source_support_binding(record, texts, source_hashes=hashes),
                    "support_version": curate.SOURCE_SUPPORT_VERSION,
                }
                for record in optional
            },
            "pending_optional": [curate.evidence_id(record) for record in pending],
            "pending_optional_proofs": {
                curate.evidence_id(record): {
                    "source_binding": curate.source_support_binding(record, texts, source_hashes=hashes),
                    "support_version": curate.SOURCE_SUPPORT_VERSION,
                }
                for record in pending
            },
            "frontier_context": frontier_context(state),
        }
        return selected

    def _retain_verification(self, state: JobState, results: dict) -> None:
        state.evidence_verification.update(results)
        for identity, result in results.items():
            if not result.get("supported") and not result.get("verification_pending"):
                note = f"source-support {'unknown' if result.get('status') == 'unknown' else 'rejection'} {identity}: {result.get('reason')}"
                if note not in state.coverage_gaps:
                    state.coverage_gaps.append(note)
        self._persist(state)
        self._check_cancel(state)

    @staticmethod
    def _drafted_claim(change: PageChange, record: Evidence) -> bool:
        """Whether the page's draft may carry this claim: it has a recorded
        anchor, or its claim text occurs in the drafted body."""
        if not change.draft_body:
            return False
        identity = curate.evidence_id(record)
        return identity in ((change.draft_progress or {}).get("claim_anchors") or {}) or " ".join(
            record.text.split()
        ) in " ".join(change.draft_body.split())

    async def _verify_selected(self, state: JobState, change: PageChange, client: ModelClient) -> None:
        # Placement obligations are exact identities. An eighty-record editorial
        # shortlist must not force unrelated source verification before an edit.
        required = set(change.claim_ids)
        while True:
            evidence = self._evidence(state)
            unknown = [
                record
                for record in evidence
                if curate.evidence_id(record) in required and record.support_status != "supported"
            ]
            if not unknown:
                self._page_evidence(state, change, evidence)
                return
            batch = unknown[:12]
            sources = {source.id: source for source in state.sources}
            texts = {
                source_id: text
                for source_id in {record.source_id for record in batch}
                if (source := sources.get(source_id)) and (text := self._extract_text(state, source)) is not None
            }
            await curate.verify_source_claims(
                client,
                batch,
                texts,
                char_budget=self.cfg.limits.prompt_char_budget,
                progress=lambda results: self._retain_verification(state, results),
                known=state.evidence_verification,
            )
            identities = {curate.evidence_id(record) for record in batch}
            unavailable = [
                identity
                for identity, result in state.evidence_verification.items()
                if identity in identities and (result.get("status") == "unknown" or result.get("verification_pending"))
            ]
            # A claim whose support review was dropped (a recorded current gap)
            # leaves this page's obligations and the page continues, but only
            # while the page's draft does not carry it: a drafted claim without
            # a verdict still isolates its page (fail closed).
            by_id = {curate.evidence_id(record): record for record in batch}
            rejected = [
                identity
                for identity in unavailable
                if state.evidence_verification[identity].get("unit_dropped")
                and not state.evidence_verification[identity].get("verification_pending")
                and not self._drafted_claim(change, by_id[identity])
            ]
            if rejected:
                change.claim_ids = [identity for identity in change.claim_ids if identity not in rejected]
                required -= set(rejected)
                unavailable = [identity for identity in unavailable if identity not in rejected]
            if unavailable:
                pending = [
                    identity
                    for identity in unavailable
                    if state.evidence_verification[identity].get("verification_pending")
                ]
                bindings = {
                    curate.evidence_id(record): curate.source_support_binding(record, texts) for record in batch
                }
                unsettled = [
                    identity
                    for identity in unavailable
                    if state.evidence_verification[identity].get("support_version") != curate.SOURCE_SUPPORT_VERSION
                    or state.evidence_verification[identity].get("source_binding") != bindings[identity]
                ]
                if pending or unsettled:
                    raise ModelError(
                        "selected source support unavailable: " + ", ".join(sorted(set(pending + unsettled))),
                        transient=True,
                    )
                raise SourceSupportUnavailable(sorted(unavailable))

    def _isolate_source_group(self, state: JobState, change: PageChange, exc: SourceSupportUnavailable) -> None:
        # A recorded commit cannot lose a blocked page: refuse its entire merge.
        paths = set(state.publication.committed_paths)
        blocks_commit = bool(state.publication.commit_sha) and (not paths or change.path in paths)
        change.status = "conflict" if blocks_commit else "failed"
        change.missing_evidence = list(dict.fromkeys(change.missing_evidence + exc.identities))
        change.error = str(exc)
        if blocks_commit:
            for member in state.changes:
                if member.status in ("pending", "drafted", "failed", "validated") and (
                    not paths or member.path in paths
                ):
                    member.status = "conflict"
                    member.error = "Existing unmerged batch commit contains a blocked source group"
        change.validation = [{"check": "source", "severity": "blocker", "detail": str(exc)}]
        note = "Required source support is unresolved; original obligations and bound source proofs retained for follow-up."
        if note not in change.notes:
            change.notes.append(note)
        self._persist(state)

    async def _verify_group_support(self, state: JobState, change: PageChange, client: ModelClient) -> bool:
        try:
            await self._verify_selected(state, change, client)
        except SourceSupportUnavailable as exc:
            self._isolate_source_group(state, change, exc)
            return False
        return True

    def _isolate_review_group(self, state: JobState, change: PageChange, failures: list[Failure]) -> None:
        """Isolate a completed editorial failure without altering an old commit."""
        paths = set(state.publication.committed_paths)
        blocks_commit = bool(state.publication.commit_sha) and (not paths or change.path in paths)
        change.status = "conflict" if blocks_commit else "failed"
        change.error = "; ".join(f"{f.check}: {f.detail[:150]}" for f in failures[:4])
        change.validation = [f.as_dict() for f in failures[:10]]
        if blocks_commit:
            for member in state.changes:
                if member.status in ("pending", "drafted", "failed", "validated") and (
                    not paths or member.path in paths
                ):
                    member.status = "conflict"
                    member.error = "Existing unmerged batch commit contains a blocked editorial group"
        note = (
            change.path
            + ": editorial review unresolved; drafts, source evidence and obligations retained for follow-up"
        )
        if note not in state.coverage_gaps:
            state.coverage_gaps.append(note)
        self._persist(state)

    def _enforce_group_coherence(self, state: JobState) -> None:
        while True:
            checkout = Path(self.cfg.paths.brain_checkout).expanduser()
            blocked = set()
            for item in state.changes:
                if item.status not in ("failed", "conflict"):
                    continue
                revision = item.base_commit or state.base_commit
                # An unresolved update does not delete the captured canonical
                # page. Links to its old facts remain usable; new facts remain
                # unresolved. Never infer existence from action or worktree.
                if not revision or gittools.read_blob(checkout, revision, item.path) is None:
                    blocked.add(item.path)
            changed = False
            for change in state.changes:
                if change.status != "validated":
                    continue
                for target in re.findall(r"\[\[([^\]|#]+)", change.draft_body):
                    target = target.strip()
                    stem = target.removesuffix(".md").rsplit("/", 1)[-1]
                    if any(
                        path == target
                        or path.removesuffix(".md") == target
                        or ("/" not in target and path.removesuffix(".md").rsplit("/", 1)[-1] == stem)
                        for path in blocked
                    ):
                        change.status = "conflict"
                        change.error = f"links to [[{target}]], which is blocked in this batch"
                        changed = True
                        break
            if not changed:
                return

    def _scope_failures(self, state: JobState, body: str, original_body: str | None = None) -> list[Failure]:
        return capacity.scope_failures(
            state.source_policy, [source.url for source in state.sources], body, original_body
        )

    def _restore_structure(
        self,
        state: JobState,
        change: PageChange,
        original_body: str | None,
        original_meta: dict[str, Any] | None = None,
    ) -> tuple[bool, list[Failure]]:
        """Restore dropped base sections and the base titles without a model.

        Base sections the draft dropped come back (validate.
        restore_dropped_sections), then the page H1, the frontmatter title and
        the plan title are put back to the base page's (validate.
        restore_titles): the curator never renames a page. Returns (whether
        the body, frontmatter or plan title actually changed, a failure for
        each dropped section that cannot be placed, for the model repair).
        The editor's heading-rename proof is rebound to the new body (its unit
        identity too while the headings are unchanged, and without a
        page-title rename the restore undid); its task progress, which
        described the old body, is discarded. Never called in a correction
        job.
        """
        from doclayer.curator.editing import (
            page_units,
            rebind_claim_anchors,
            retain_edit_structure,
        )

        structure = retain_edit_structure(change.draft_progress, change.draft_body)
        anchors = change.draft_progress.get("claim_anchors")
        unrestorable, sections = [], []
        update = change.action == "update" and original_body is not None
        previous = (change.draft_body, json.dumps(change.draft_meta, sort_keys=True, default=str), change.title)
        if update:
            body, sections, unplaced = restore_dropped_sections(
                original_body, change.draft_body, structure.get("heading_renames", [])
            )
            unrestorable = [
                Failure(
                    "scope", f"{change.path}: deterministic restore of section {why}; the model repair must restore it"
                )
                for why in unplaced
            ]
            change.draft_body = body
        titles = restore_titles(change, original_meta, original_body if update else None)
        if (change.draft_body, json.dumps(change.draft_meta, sort_keys=True, default=str), change.title) == previous:
            return False, unrestorable
        same_units = [(u.label, u.level) for u in page_units(previous[0])] == [
            (u.label, u.level) for u in page_units(change.draft_body)
        ]
        kept = (
            ("editor_version", "heading_renames", "unit_identity")
            if same_units
            else ("editor_version", "heading_renames")
        )
        progress = {key: structure[key] for key in kept if key in structure}
        if "page H1" in titles and progress.get("heading_renames"):
            progress["heading_renames"] = [
                row for row in progress["heading_renames"] if not re.match(r"#\s", str(row.get("old", "")))
            ]
        change.draft_progress = {**progress, "body_hash": self._draft_body_hash(change)} if structure else {}
        if anchors:
            change.draft_progress["claim_anchors"] = rebind_claim_anchors(anchors, previous[0], change.draft_body)
        if sections:
            change.notes.append(
                "scope repair: restored dropped base section(s) " + ", ".join(f"'{name}'" for name in sections)
            )
        if titles:
            change.notes.append(
                f"title repair: {', '.join(titles)} restored to '{change.title}'; pages are never renamed"
            )
        self._persist(state)
        return True, unrestorable

    @staticmethod
    def _cites_excluded(failure: Failure) -> bool:
        return failure.code == capacity.SCOPE_CITATION

    def _scope_instruction(
        self, state: JobState, change: PageChange, failures: list[Failure], original_body: str | None
    ) -> str:
        """The dedicated scope pass's instruction: each rejected URL and every new sentence citing it."""
        from doclayer.curator.capacity import (
            raw_sentences,
            sentences,
            url_cited,
            verbatim_form,
        )

        base = {verbatim_form(text) for text in sentences(original_body or "")}
        rows = []
        for url in dict.fromkeys(
            match.group(1) for failure in failures if (match := re.search(r"source (\S+) \(", failure.detail))
        ):
            citing = [
                text
                for text in raw_sentences(change.draft_body)
                if url_cited(text, url) and verbatim_form(text) not in base
            ]
            rows.append(
                f"- {url} is cited by: " + "; ".join(json.dumps(text[:300], ensure_ascii=False) for text in citing)
            )
        return (
            "Scope repair: these sources are excluded by the import's source policy.\n" + "\n".join(rows) + "\n"
            "Remove these citations; keep the sentence only if another accepted citation supports it; "
            "never remove base text."
        )

    @staticmethod
    def _correction_kwargs(state: JobState) -> dict[str, Any]:
        """run_deterministic_checks options: a correction job keeps the pre-repair checks."""
        return {"correction_job": True} if state.correction_of is not None else {}

    @staticmethod
    def _clear_isolation(state: JobState, change: PageChange) -> None:
        """A page that is validated or redrafted no longer reports an old isolation."""
        change.error = None
        note = (
            change.path
            + ": editorial review unresolved; drafts, source evidence and obligations retained for follow-up"
        )
        if note in state.coverage_gaps:
            state.coverage_gaps.remove(note)

    def _stale_draft(
        self, state: JobState, change: PageChange, evidence: list[Evidence]
    ) -> EditorialContextOverflow | None:
        """The review's refusal of a draft made before evidence was bounded, if it refuses."""
        try:
            self._review_evidence(state, change, evidence)
        except EditorialContextOverflow as exc:
            if (exc.diagnostic or {}).get("operation") == "redraft-required":
                return exc
        return None

    def _schedule_redraft(self, state: JobState, change: PageChange, reason: EditorialContextOverflow) -> bool:
        """Discard a stale-generation draft and send this page alone back to drafting.

        The draft stage then drafts it from the bounded evidence and stamps the
        current CAPACITY_GENERATION, like any draft and within the job's
        model-call limit. False (page isolated) when its assigned claims cannot
        fit the review, so the redraft itself cannot succeed. The new draft
        starts with a fresh repair-pass allowance.
        """
        try:
            self._require_page_capacity(state, change, self._evidence(state))
        except EditorialContextOverflow as exc:
            self._isolate_review_group(state, change, [Failure("editorial-context", str(exc))])
            return False
        change.status = "pending"
        change.draft_body, change.draft_meta, change.draft_progress = "", {}, {}
        change.check_progress, change.reconcile_check_progress, change.reconcile_progress = {}, {}, {}
        change.repair_context, change.validation = {}, []
        change.repair_passes = 0  # a new draft gets its own repair allowance, bounded by the job's call limit
        change.scope_repair_passes = 0
        self._clear_isolation(state, change)
        state.evidence_selection.pop("drafted:" + change.path, None)
        change.notes.append(
            "redrafted automatically: drafted before page evidence was bounded to the review "
            f"budget ({(reason.diagnostic or {}).get('records', '?')} source records)"
        )
        self._persist(state)
        return True

    # -- top level --------------------------------------------------------- #

    async def run(self, state: JobState, persist: Any = None) -> JobState:
        """Run (or resume) the job to a terminal state. persist() defaults to
        saving via the job store."""
        persist = persist or (lambda: self._persist(state))
        limit_snapshot = {
            "max_sources": self.cfg.limits.max_sources,
            "max_source_bytes": self.cfg.limits.max_source_bytes,
            "max_job_bytes": self.cfg.limits.max_job_bytes,
            "max_active_seconds": self.cfg.limits.max_active_seconds,
            "max_model_calls": self.cfg.limits.max_model_calls,
            "fetch_concurrency": self.cfg.limits.fetch_concurrency,
            "transient_retries": self.cfg.limits.transient_retries,
            "max_repair_passes": self.cfg.limits.max_repair_passes,
        }
        if state.mode == "single-pass":
            limit_snapshot.update(
                max_model_calls=self.cfg.single_pass.max_model_calls,
                max_active_seconds=self.cfg.single_pass.max_active_seconds,
                max_sources=self.cfg.single_pass.max_sources,
                max_repair_passes=0,
            )
        if state.status in ("published", "covered", "cancelled"):
            return state
        # A stop belongs to one execution, even if a later failure happens to
        # repeat the same error text. Preserve other inference evidence.
        state.inference_diagnostics = [
            item
            for item in state.inference_diagnostics
            if not (isinstance(item, dict) and item.get("operation") == "automatic-task-stop")
        ]
        # Partial submit overrides still use these runtime defaults. Persist
        # the effective caps so recovery can prove the remaining allowance,
        # without replacing any explicit or previously snapshotted limit.
        for key, value in limit_snapshot.items():
            state.limits.setdefault(key, value)
        self._active_last_tick = self._clock()
        with degrade.recording(state.coverage_gaps):
            return await self._run_bound(state, persist)

    async def _run_bound(self, state: JobState, persist: Any) -> JobState:
        """The job loop; a dropped unit records its gap on this job (degrade.py)."""
        try:
            if state.status == "queued":
                state.status = "processing"
                persist()
            while True:
                self._check_cancel(state)
                stage = state.stage
                # Recover already-landed publication receipts and finish their
                # report before charging/admitting any new bounded work.
                if stage not in ("publish", "report"):
                    self._check_time(state)
                if stage not in STAGES:
                    raise RuntimeError(f"unknown stage {stage!r}")
                state.status = (
                    "validating" if stage == "validate" else "publishing" if stage == "publish" else "processing"
                )
                next_stage = await self._run_stage(state)
                self._account_active_time(state)
                if next_stage is None:
                    break
                state.stages_done.append(stage)
                state.stage = next_stage
                persist()
            persist()
            return state
        except CancelledByRequest:
            state.status = "cancelled"
            self._finish_report(state)
            persist()
            return state
        except BudgetExceeded as exc:
            state.coverage_gaps.append(f"stopped at configured limit ({exc.limit}); work is preserved and resumable")
            state.status = "retryable"
            state.error = f"limit reached: {exc.limit}"
            self._finish_report(state)
            persist()
            return state
        except OutputTruncated as exc:
            state.status = "retryable"
            state.error = "model output truncated repeatedly; split the batch or raise the serving output limit"
            subject = getattr(exc, "subject", None)
            if subject:  # batches were already split down to this one item
                note = f"{subject}: model output truncated even alone; raise the serving output limit"
                if note not in state.coverage_gaps:
                    state.coverage_gaps.append(note)
            from doclayer.curator.stopped_task import record_task_stop

            record_task_stop(state, kind="output_truncated", subject=subject)
            self._finish_report(state)
            persist()
            return state
        except ModelError as exc:
            state.status = "retryable" if exc.transient else "failed"
            state.error = f"inference: {exc}"
            if exc.transient:
                from doclayer.curator.stopped_task import record_task_stop

                record_task_stop(state, kind="model_task", operation=(exc.diagnostic or {}).get("operation"))
            if exc.diagnostic:
                # Preserve the established most-recent task diagnostic while
                # retaining the separate typed scheduler stop beside it.
                state.inference_diagnostics = (state.inference_diagnostics + [exc.diagnostic])[-3:]
            self._finish_report(state)
            persist()
            return state
        except PublishNotConfigured as exc:
            # A configuration error, not an outage: retrying cannot succeed.
            state.status = "failed"
            state.error = f"publication: {exc}"
            self._finish_report(state)
            persist()
            return state
        except (GitError, ForgejoError, ReconcileFailed) as exc:
            # Publication-side operational failure; the durable publication
            # identity lets a resume continue the same attempt.
            state.status = "retryable"
            state.error = f"publication: {exc}"
            self._finish_report(state)
            persist()
            return state
        finally:
            # The last checkpoint survives interruptions; queued/offline time
            # is never inferred on resume. Charge failures as well as success.
            self._account_active_time(state)
            self._active_last_tick = None
            if self.store.exists(state.job_id):
                persist()  # retain cancellation merges and caller checkpoint semantics

    async def _run_stage(self, state: JobState) -> str | None:
        if state.mode == "single-pass" and state.stage in single_pass.STAGE_HANDLERS:
            return await single_pass.STAGE_HANDLERS[state.stage](self, state)
        handler = {
            "intake": self._stage_intake,
            "discover": self._stage_discover,
            "extract": self._stage_extract,
            "place": self._stage_place,
            "draft": self._stage_draft,
            "validate": self._stage_validate,
            "publish": self._stage_publish,
            "report": self._stage_report,
        }[state.stage]
        return await handler(state)

    def _advance_if_done(self, state: JobState, stage: str) -> str | None:
        """Next stage after `stage`, skipping to report for correction jobs."""
        if state.correction_of and stage in ("intake", "discover", "extract", "place"):
            return "draft"
        index = STAGES.index(stage)
        if index + 1 < len(STAGES):
            return STAGES[index + 1]
        return None

    # -- stage: intake ------------------------------------------------------ #

    async def _stage_intake(self, state: JobState) -> str | None:
        if state.correction_of:
            return self._advance_if_done(state, "intake")
        max_bytes_per = int(state.limits.get("max_source_bytes", self.cfg.limits.max_source_bytes))
        max_total = int(state.limits.get("max_job_bytes", self.cfg.limits.max_job_bytes))
        max_sources = int(state.limits.get("max_sources", self.cfg.limits.max_sources))
        seen_hashes = {s.content_hash: s.id for s in state.sources if s.content_hash and s.status == "fetched"}

        def limit_reached() -> bool:
            return state.usage.fetched_bytes >= max_total or self._source_count(state) >= max_sources

        def admissible(source: SourceRecord) -> bool:
            if source.status == "fetched":
                return False
            if limit_reached():
                source.status = "skipped"
                source.error = "job source/transfer limit already reached"
                return False
            allowed, reason, rule = policy.url_decision(source.url, state.source_policy)
            if not allowed:
                source.status, source.error, source.exclusions_hit = "skipped", reason, True
                return False
            source.scope.update(rule.get("applicability", {}))
            return True

        async def read_one(client: httpx.AsyncClient | None, source: SourceRecord):
            """The network/disk read of one source: (text, origin), or (the
            exception it failed with, None). No job accounting happens here, so
            reads may run concurrently."""
            try:
                if source.kind == "url":
                    assert client is not None and source.url
                    page = await self._fetch(state, client, source.url, max_bytes=max_bytes_per)
                    return page.markdown, page
                local = self._source_local_path(state, source)
                if source.kind == "pdf":
                    return intake.pdf_to_markdown(local), local
                return local.read_text(encoding="utf-8", errors="replace"), local
            except (httpx.HTTPError, intake.FetchFailure, OSError, IntakePathError) as exc:
                return exc, None

        def commit_one(source: SourceRecord, read) -> None:
            text, origin = read
            if isinstance(text, httpx.HTTPError):
                source.status, source.error = "failed", f"network: {type(text).__name__}"
                return
            if isinstance(text, intake.FetchFailure):
                source.status, source.error = "failed", text.reason
                return
            if isinstance(text, (OSError, IntakePathError)):
                source.status, source.error = "failed", f"unreadable: {text}"
                return
            if limit_reached():
                source.status = "skipped"
                source.error = "job source/transfer limit already reached"
                return
            if source.kind == "url":
                page = origin
                good, reason = intake.classify_page(page.status, page.markdown)
                if not good:
                    source.status = "failed"
                    source.error = reason
                    return
                source.redirect_chain = page.redirect_chain
                for location in source.redirect_chain:
                    source.scope.update(policy.url_decision(location, state.source_policy)[2].get("applicability", {}))
                source.title = page.title
                source.bytes = page.bytes_read
                source.publisher = re.sub(r"^www\.", "", _host(source.url))
            elif source.kind == "pdf":
                source.bytes = origin.stat().st_size
            else:  # text / markdown / text file
                source.bytes = len(text.encode("utf-8"))
            source.inspected_at = _now_iso()
            digest = intake.content_hash(text)
            source.content_hash = digest
            if digest in seen_hashes:
                source.status = "duplicate"
                source.error = f"same content as {seen_hashes[digest]}"
                return
            seen_hashes[digest] = source.id
            if state.usage.fetched_bytes + source.bytes > max_total:
                source.status = "skipped"
                source.error = "would exceed the job transfer limit"
                return
            state.usage.fetched_bytes += source.bytes
            state.usage.sources_fetched += 1
            self._store_extract(state, source, text)
            source.status = "fetched"
            source.section_count = len(intake.section_map(text))

        timeout = httpx.Timeout(
            connect=self.cfg.limits.connect_timeout_s,
            read=self.cfg.limits.fetch_timeout_s,
            write=self.cfg.limits.fetch_timeout_s,
            pool=self.cfg.limits.fetch_timeout_s,
        )
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": self.cfg.user_agent},
            transport=self._fetch_transport,
        ) as client:
            # Up to limits.fetch_concurrency reads run at once. Their results are
            # committed one by one in submission order, so the source/transfer
            # caps stay exact, and every checkpoint sits between whole batches.
            queue = [s for s in state.sources if s.submitted]
            width = self._fetch_width(state)
            while queue:
                self._check_cancel(state)
                self._check_time(state)
                batch: list[SourceRecord] = []
                while queue and len(batch) < min(width, max(1, max_sources - self._source_count(state))):
                    source = queue.pop(0)
                    if admissible(source):
                        batch.append(source)
                if batch:
                    reads = await asyncio.gather(
                        *(read_one(client if source.kind == "url" else None, source) for source in batch)
                    )
                    for source, read in zip(batch, reads):
                        commit_one(source, read)
                self._persist(state)
        self._persist(state)
        return self._advance_if_done(state, "intake")

    def _fetch_width(self, state: JobState) -> int:
        """How many source reads may run at once (limits.fetch_concurrency)."""
        return max(1, int(state.limits.get("fetch_concurrency", self.cfg.limits.fetch_concurrency)))

    async def _fetch(self, state: JobState, client: httpx.AsyncClient, url: str, *, max_bytes: int):
        """intake.fetch_url with limits.transient_retries retries of a transient
        failure (a network error, HTTP 429 or 5xx); a permanent failure or the
        last transient one is raised to the caller."""
        retries = max(0, int(state.limits.get("transient_retries", self.cfg.limits.transient_retries)))
        for attempt in range(retries + 1):
            try:
                return await intake.fetch_url(
                    client,
                    url,
                    max_bytes=max_bytes,
                    url_policy=lambda url: policy.url_decision(url, state.source_policy)[:2],
                )
            except (httpx.TransportError, intake.FetchFailure) as exc:
                if attempt >= retries or (isinstance(exc, intake.FetchFailure) and not exc.transient):
                    raise
            await asyncio.sleep(FETCH_RETRY_BACKOFF_S * (attempt + 1))
        raise AssertionError("unreachable")

    def _source_count(self, state: JobState) -> int:
        return state.usage.sources_fetched

    def _source_local_path(self, state: JobState, source: SourceRecord) -> Path:
        """Where a submitted file lives on the worker host. Only paths under
        the job's staging directory are honored — a submitted path is data,
        and intake never reads arbitrary host files."""
        staging = self.store.staging_dir(state.job_id)
        if source.local_path:
            candidate = Path(source.local_path)
            if candidate.is_file():
                return candidate
        name = source.label
        candidate = staging / name
        if candidate.is_file():
            return candidate
        raise IntakePathError(f"submitted file {name!r} not found in job staging")

    # -- stage: discover ----------------------------------------------------- #

    async def _stage_discover(self, state: JobState) -> str | None:
        from doclayer.curator.source_admission import (
            SourceAdmissionDeferred,
            source_budget,
        )

        if state.correction_of:
            return self._advance_if_done(state, "discover")
        client_model = self._model(state)
        max_sources = int(state.limits.get("max_sources", self.cfg.limits.max_sources))
        max_bytes_per = int(state.limits.get("max_source_bytes", self.cfg.limits.max_source_bytes))
        max_total = int(state.limits.get("max_job_bytes", self.cfg.limits.max_job_bytes))

        known_urls = {s.url for s in state.sources if s.url}

        def enqueue(parent: SourceRecord) -> None:
            if parent.relevance.get("links_expanded") or parent.status != "fetched" or not parent.url:
                return
            text = self._extract_text(state, parent)
            if text is None:
                state.coverage_gaps.append(f"{parent.label}: discovery cache unavailable")
                return
            for url, anchor in intake.extract_links(text, parent.url):
                if url in known_urls:
                    continue
                known_urls.add(url)
                child = intake.make_url_source(
                    url, role="supplementary" if parent.role == "primary" else parent.role, parent=parent
                )
                child.relevance = {"anchor": anchor, "why": "pending scoring", "priority": "normal"}
                allowed, reason, rule = policy.url_decision(url, state.source_policy)
                child.scope = dict(rule.get("applicability", {}))
                if not allowed or intake.matches_exclusions(url + " " + anchor, state.exclusions):
                    child.status, child.error, child.exclusions_hit = (
                        "rejected",
                        reason if not allowed else "matched batch exclusion: " + anchor,
                        True,
                    )
                state.sources.append(child)
            parent.relevance["links_expanded"] = True
            self._persist(state)

        for source in list(state.sources):
            enqueue(source)
        scouting_yielded = False
        timeout = httpx.Timeout(self.cfg.limits.fetch_timeout_s)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": self.cfg.user_agent},
            transport=self._fetch_transport,
        ) as http:
            while True:
                self._check_cancel(state)
                waiting = [s for s in state.sources if not s.submitted and s.status in ("pending", "scored")]
                if not waiting:
                    break
                if self._source_count(state) >= max_sources or state.usage.fetched_bytes >= max_total:
                    reason = "source" if self._source_count(state) >= max_sources else "transfer"
                    state.coverage_gaps.append(
                        f"discovery stopped: {reason} limit; {len(waiting)} candidates pending (durable)"
                    )
                    break
                # Scouting shares the job's allowance with source review and
                # publication. Retain every unscored candidate when it yields;
                # retries/JSON repairs are guarded per attempted HTTP request.
                pending = [s for s in waiting if s.status == "pending"]
                # Drain already-assessed relevant work before spending another
                # scouting call. A soft scouting stop must not strand it behind
                # an unassessed frontier on this run or a later restart.
                if pending and not scouting_yielded and not any(s.status == "scored" for s in waiting):
                    # Compare as much of this frontier as the scouting share
                    # permits before allocating scarce fetch slots by priority.
                    for start in range(0, len(pending), 12):
                        batch = pending[start : start + 12]
                        scouting_calls = min(20, client_model.max_calls // 10)
                        try:
                            with source_budget(
                                client_model, reserve=client_model.max_calls - scouting_calls
                            ) as scouting:
                                assessments = await curate.score_link_relevance(
                                    scouting,
                                    state.intent,
                                    state.exclusions + [f"Source policy: {state.source_policy}"],
                                    [(s.url or "", s.relevance.get("anchor", "")) for s in batch],
                                )
                        except SourceAdmissionDeferred:
                            scouting_yielded = True
                            state.coverage_gaps.append(
                                f"discovery yielded to preserve source review and publication budget; {len(waiting)} candidates pending (durable)"
                            )
                            self._persist(state)
                            break
                        except ModelError as exc:
                            # Drop this scoring batch: its candidates are not fetched.
                            note = degrade.degrade_unit(
                                f"discovery link scoring of {len(batch)} candidates from {batch[0].url or batch[0].id}",
                                exc,
                            )
                            assessments = {s.url or "": {"relevant": False, "why": note} for s in batch}
                        for source in batch:
                            assessment = assessments.get(source.url or "", {})
                            source.relevance.update(assessment)
                            if assessment.get("relevant"):
                                source.status = "scored"
                            else:
                                source.status, source.error = (
                                    "rejected",
                                    str(assessment.get("why", "not relevant to import")),
                                )
                        self._persist(state)
                ranked = sorted(
                    (s for s in waiting if s.status == "scored"),
                    key=lambda s: (
                        {"high": 0, "normal": 1, "low": 2}.get(s.relevance.get("priority"), 1),
                        s.url or s.id,
                    ),
                )
                if not ranked:
                    if not scouting_yielded and any(s.status == "pending" for s in waiting):
                        continue
                    break
                # The best-ranked candidates are read concurrently (up to
                # limits.fetch_concurrency, never past the source cap) and
                # committed one by one in rank order.
                batch = ranked[: min(self._fetch_width(state), max(1, max_sources - self._source_count(state)))]

                async def read(candidate: SourceRecord):
                    try:
                        page = await self._fetch(state, http, candidate.url or "", max_bytes=max_bytes_per)
                        good, reason = intake.classify_page(page.status, page.markdown)
                        if not good:
                            raise intake.FetchFailure(reason, transient=False)
                        return page
                    except (httpx.HTTPError, intake.FetchFailure) as exc:
                        return exc

                pages = await asyncio.gather(*(read(candidate) for candidate in batch))
                for source, page in zip(batch, pages):
                    if isinstance(page, httpx.HTTPError):
                        source.status, source.error = "failed", f"network: {type(page).__name__}"
                        self._persist(state)
                        continue
                    if isinstance(page, intake.FetchFailure):
                        source.status, source.error = "failed", page.reason
                        self._persist(state)
                        continue
                    if self._source_count(state) >= max_sources or state.usage.fetched_bytes >= max_total:
                        source.status = "scored"  # read but not admitted: stays a durable candidate
                        continue
                    source.bytes, source.title = page.bytes_read, page.title
                    source.redirect_chain = page.redirect_chain
                    for location in source.redirect_chain:
                        source.scope.update(
                            policy.url_decision(location, state.source_policy)[2].get("applicability", {})
                        )
                    source.content_hash = intake.content_hash(page.markdown)
                    # All transfers, including duplicates, spend the transfer budget.
                    state.usage.fetched_bytes += source.bytes
                    state.usage.sources_fetched += 1
                    if state.usage.fetched_bytes > max_total:
                        source.status, source.error = "skipped", "job transfer limit exceeded by fetched source"
                    elif any(
                        s is not source and s.status == "fetched" and s.content_hash == source.content_hash
                        for s in state.sources
                    ):
                        source.status, source.error = "duplicate", "content already fetched in this import"
                    else:
                        source.status = "fetched"
                        source.publisher = re.sub(r"^www\.", "", _host(source.url))
                        source.inspected_at = _now_iso()
                        source.section_count = len(intake.section_map(page.markdown))
                        self._store_extract(state, source, page.markdown)
                        enqueue(source)
                    self._persist(state)
        self._persist(state)
        return self._advance_if_done(state, "discover")

    # -- stage: extract -------------------------------------------------------- #

    async def _stage_extract(self, state: JobState) -> str | None:
        from doclayer.curator.source_admission import (
            SourceAdmissionDeferred,
            source_budget,
        )

        if state.correction_of:
            return self._advance_if_done(state, "extract")
        client = self._model(state, stage="extraction")
        budget = self._stage_cfg("extraction").limits.prompt_char_budget
        existing_evidence = self.store.read_evidence(state.job_id)
        have = {(e["source_id"], e["locator"], e["text"].strip()) for e in existing_evidence}

        order = sorted(
            (s for s in state.sources if s.status == "fetched"),
            # Python's stable sort keeps the declared order within each tier.
            # IDs identify sources; their hash order is not editorial priority.
            key=lambda s: (not (s.submitted and s.role == "primary"), not s.submitted),
        )
        for source_index, source in enumerate(order):
            self._check_cancel(state)
            allowed, reason, rule = policy.url_decision(source.url, state.source_policy)
            if not allowed:
                source.exclusions_hit = True
                source.error = reason
                continue
            source.scope.update(rule.get("applicability", {}))
            if source.extraction_complete:
                text = self._extract_text(state, source)
                if text is not None:
                    curate.map_source_structure(source, text, char_budget=budget)
                if not source.reviewed_sections and not source.omitted_sections:
                    source.omitted_categories["historical"] = "review map unavailable before structure schema upgrade"
                continue
            selection = state.evidence_selection.get("continuation_review", {})
            if (
                state.continuation_of
                and selection
                and source.id in state.inherited_source_ids
                and source.id not in selection.get("refresh_source_ids", [])
                and (
                    not selection.get("review_omitted", True)
                    or (selection.get("source_ids") and source.id not in selection["source_ids"])
                )
            ):
                source.omitted_categories["continuation-review"] = (
                    "Inherited unreviewed source retained; review not selected for this continuation"
                )
                continue
            source.omitted_categories.pop("continuation-review", None)
            text = self._extract_text(state, source)
            if text is None:
                state.coverage_gaps.append(f"{source.label}: cached extract missing; not re-fetched in this pass")
                continue

            def retain(claims):
                records = []
                for claim in claims:
                    if claim.get("excluded_topic"):
                        continue
                    allowed, reason = policy.claim_decision(
                        source.url, claim.get("claim", ""), claim.get("applicability", {}), state.source_policy
                    )
                    if not allowed:
                        state.coverage_gaps.append(f"{source.label}: excluded claim ({reason})")
                        continue
                    if any(
                        claim.get("applicability", {}).get(k)
                        and str(claim["applicability"][k]).casefold() != str(v).casefold()
                        for k, v in source.scope.items()
                    ):
                        state.coverage_gaps.append(f"{source.label}: incompatible source applicability claim excluded")
                        continue
                    if any(
                        not policy.claim_decision(
                            location, claim.get("claim", ""), claim.get("applicability", {}), state.source_policy
                        )[0]
                        for location in source.redirect_chain
                    ):
                        continue
                    claim["applicability"] = {
                        **state.source_policy.get("global", {}).get("applicability", {}),
                        **source.scope,
                        **claim.get("applicability", {}),
                    }
                    locator = claim.get("locator") or "(document)"
                    key = (source.id, locator, claim.get("claim", "").strip())
                    if key in have:
                        continue
                    have.add(key)
                    records.append(
                        Evidence(
                            source_id=source.id,
                            label=source.label,
                            locator=locator or "(document)",
                            modality=claim.get("modality", "paraphrase"),
                            text=claim.get("claim", ""),
                            doc_kind=source.doc_kind,
                            applicability=claim.get("applicability", {}),
                            published_info=source.published_info,
                            inspected_at=source.inspected_at,
                            url=source.url,
                            publisher=source.publisher,
                            role=source.role,
                            quote=claim.get("quote", ""),
                            original_quote=claim.get("original_quote", claim.get("quote", "")),
                            support_status=claim.get("support_status", "unknown"),
                            claim_id=curate.claim_identity(source.id, locator, claim.get("claim", "")),
                        ).__dict__
                        | {"quote": claim.get("quote", ""), "important": claim.get("important", True)}
                    )
                self.store.append_evidence(state.job_id, records)
                self._persist(state)

            single_pass = state.mode == "single-pass"
            reserve = None
            if single_pass:
                # Extraction may spend only what is left of the per-job cap.
                remaining = max(
                    0, self.cfg.single_pass.max_extract_calls - state.usage.calls_by_stage.get("extract", 0)
                )
                reserve = client.max_calls - (state.usage.model_calls + remaining)
            try:
                with source_budget(client, reserve=reserve) as admitted:
                    claims = await curate.extract_claims(
                        admitted,
                        source,
                        text,
                        state.intent,
                        state.exclusions + [f"Source constraints (binding): {rule}"],
                        char_budget=budget,
                        progress=retain,
                        verification_cache=state.evidence_verification,
                        verification_progress=lambda results: self._retain_verification(state, results),
                        verify=not single_pass,
                    )
            except SourceAdmissionDeferred:
                # No further model work fits this source allowance. Map the
                # untouched eligible frontier too, so later declared seeds are
                # visible to coverage and linked continuation, not silently lost.
                for deferred in order[source_index:]:
                    if deferred.extraction_complete or not policy.url_decision(deferred.url, state.source_policy)[0]:
                        continue
                    deferred_text = self._extract_text(state, deferred)
                    if deferred_text is not None:
                        curate.map_source_structure(deferred, deferred_text, char_budget=budget)
                    else:
                        deferred.omitted_categories["cached-extract"] = (
                            "Cached extract missing; not re-fetched in this pass"
                        )
                    deferred.omitted_categories["source-budget"] = (
                        "Source review yielded to preserve validation and publication budget; remaining units continue in a linked import"
                    )
                self._persist(state)
                break
            except OutputTruncated:
                state.coverage_gaps.append(
                    f"{source.label}: extraction output truncated; only part of the document was reviewed this pass"
                )
                continue
            retain(claims)
            source.extraction_complete = True
            if source.omitted_sections:
                state.coverage_gaps.append(
                    f"{source.label}: selective review; {len(source.omitted_sections)} "
                    "sections omitted (locators recorded in source state)"
                )
            self._persist(state)
            if source.doc_kind in ("system-card", "research-paper") and source.omitted_sections:
                state.coverage_gaps.append(
                    f"{source.label}: selective section review ({source.section_count or 'n/a'} "
                    "sections mapped); not every section was examined"
                )
        self._persist(state)
        return self._advance_if_done(state, "extract")

    # -- stage: place ------------------------------------------------------------ #

    async def _stage_place(self, state: JobState) -> str | None:
        if state.correction_of:
            return self._advance_if_done(state, "place")
        client = self._model(state)
        checkout = Path(self.cfg.paths.brain_checkout).expanduser()
        gittools.fetch(checkout, self.cfg.publish.remote)
        base = gittools.rev_parse(checkout, f"{self.cfg.publish.remote}/{self.cfg.publish.base_branch}")
        state.base_commit = base

        existing_pages = self._candidate_pages(state, checkout, base)
        # Durable target hints are independent of the discovery shortlist.
        # Omitting a previously published target can replay the same batch
        # forever instead of proving it covered and advancing the frontier.
        present = {path for path, _meta, _body in existing_pages}
        ledger = state.evidence_selection.get("placement", {}).get("ledger", {})
        targets = {
            row.get("target") or (row.get("coverage") or {}).get("path") for row in ledger.get("claims", {}).values()
        }
        for path in sorted(target for target in targets if isinstance(target, str)):
            if path not in present:
                raw = gittools.read_blob(checkout, base, path)
                if raw is not None:
                    meta, body = fm.parse(raw)
                    existing_pages.append((path, meta, body))
        evidence = self._evidence(state)
        # Admit bounded source work while keeping budget for a coherent group.
        # Settled IDs are scheduling hints; placement still checks fresh Git.
        if any(record.support_status != "supported" for record in evidence):
            from doclayer.curator.source_admission import review_sources

            sources = {source.id: source for source in state.sources}
            texts = {
                identity: text
                for identity in {record.source_id for record in evidence}
                if (source := sources.get(identity)) and (text := self._extract_text(state, source)) is not None
            }
            admission = await review_sources(
                client,
                state.intent,
                evidence,
                texts,
                char_budget=self.cfg.limits.prompt_char_budget,
                progress=lambda results: self._retain_verification(state, results),
                known=state.evidence_verification,
                settled_ids={
                    identity for identity, row in ledger.get("claims", {}).items() if row.get("status") == "covered"
                },
            )
            state.evidence_selection["source_admission"] = {
                key: value for key, value in admission.items() if key != "proofs"
            }
            self._persist(state)
        frontier = {}
        evidence = [record for record in self._evidence(state, frontier) if record.support_status == "supported"]
        pending = [
            (job.job_id, job.intent)
            for job in self.store.load_all()
            if job.job_id != state.job_id and job.in_flight and job.changes
        ]
        from doclayer.curator import placement

        def placement_checkpoint(ledger):
            from doclayer.curator.coverage import frontier_context

            ledger["frontier_context"] = frontier_context(state)
            ledger["canonical_commit"] = base
            state.evidence_selection.setdefault("placement", {})["ledger"] = ledger
            self._persist(state)
            self._check_cancel(state)

        plan = await placement.plan_batch(
            client,
            state.intent,
            state.exclusions,
            evidence,
            existing_pages,
            pending,
            char_budget=self.cfg.limits.prompt_char_budget,
            ledger=state.evidence_selection.get("placement", {}).get("ledger"),
            frontier=frontier,
            progress=placement_checkpoint,
        )
        from doclayer.curator.coverage import frontier_context

        plan["ledger"]["frontier_context"] = frontier_context(state)
        plan["ledger"]["canonical_commit"] = base
        state.evidence_selection["placement"] = {
            key: plan[key] for key in ("ledger", "selected", "remaining", "covered", "planned", "omitted_count")
        }
        if plan.get("already_covered") and not plan.get("pages"):
            state.stage = "report"
            state.coverage_gaps.append(f"placement: material already covered — {plan.get('explanation', '')[:300]}")
            self._persist(state)
            return "report"
        tracked = set(gittools.tracked_paths(checkout, base))
        for item in plan.get("pages", []):
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", "")).strip()
            action = item.get("action", "create")
            if action not in ("create", "update"):
                continue
            change = PageChange(
                group_id=f"g{len(state.changes) + 1}",
                path=path,
                action=action,
                title=str(item.get("title", ""))[:120],
                subject_type=str(item.get("subject_type", "general")),
                rationale=str(item.get("rationale", ""))[:600],
                related=[str(r) for r in (item.get("related") or [])][:12],
                source_ids=[str(s) for s in (item.get("source_ids") or [])][:20],
                claim_ids=[str(c) for c in (item.get("claim_ids") or [])][:80],
                base_commit=base,
            )
            change.base_blob = gittools.blob_hash(checkout, f"{base}:{path}")
            if path in tracked:
                change.action = "update"
            elif action == "update":
                change.action = "create"
                change.notes.append("planned update target does not exist; converted to create")
            previous = next((old for old in state.changes if old.path == change.path), None)
            if previous is None:
                state.changes.append(change)
            else:
                required = list(dict.fromkeys(previous.claim_ids + change.claim_ids))
                if required != previous.claim_ids:
                    previous.claim_ids = required
                    previous.source_ids = list(dict.fromkeys(previous.source_ids + change.source_ids))
                    previous.status = "pending"
                    previous.draft_body, previous.draft_meta = "", {}
                    previous.draft_progress, previous.check_progress, previous.reconcile_progress = {}, {}, {}
        # Persist page assignments before acknowledging the model plan. A lost
        # acknowledgement safely replays the exact plan rather than skipping it.
        self._persist(state)
        if plan.get("plan_id"):
            state.evidence_selection["placement"]["ledger"] = placement.acknowledge(plan["ledger"], plan["plan_id"])
        for note in plan.get("consolidation_opportunities", [])[:10]:
            state.consolidation_notes.append(str(note)[:400])
        self._persist(state)
        return self._advance_if_done(state, "place")

    def _candidate_pages(self, state: JobState, checkout: Path, base: str) -> list[tuple[str, dict[str, Any], str]]:
        """Existing pages, ranked by textual overlap with the topic; the
        shortlist bounds the placement prompt."""

        topic_terms = {t for t in re.split(r"[^a-z0-9]+", state.intent.lower()) if len(t) > 2}
        scored: list[tuple[float, str, dict[str, Any], str]] = []
        for path in gittools.tracked_paths(checkout, base):
            if not path.endswith(".md"):
                continue
            raw = gittools.read_blob(checkout, base, path) or ""
            meta, body = fm.parse(raw)
            haystack = " ".join(
                [
                    path.lower(),
                    str(meta.get("title", "")).lower(),
                    " ".join(str(t).lower() for t in (meta.get("tags") or [])),
                ]
            )
            score = sum(1 for term in topic_terms if term in haystack)
            if score:
                scored.append((float(score), path, meta, body))
        scored.sort(key=lambda item: -item[0])
        top = [item[1:] for item in scored[:14]]
        if not top:
            # nothing overlaps: show the wiki/ pages so the model sees the layout
            top = []
            for path in gittools.tracked_paths(checkout, base):
                if path.startswith("wiki/") and path.endswith(".md"):
                    raw = gittools.read_blob(checkout, base, path) or ""
                    meta, body = fm.parse(raw)
                    top.append((path, meta, body))
                    if len(top) >= 14:
                        break
        return top

    # -- stage: draft --------------------------------------------------------- #

    async def _stage_draft(self, state: JobState) -> str | None:
        self._require_capacity_for_pending(state)
        client = self._model(state)
        from doclayer.curator.plan_readmission import readmit_invalid_saved

        evidence = self._evidence(state)
        checkout = Path(self.cfg.paths.brain_checkout).expanduser()
        corrections_all = self.store.corrections()
        for change in state.changes:
            self._check_cancel(state)
            if change.status not in ("pending", "drafted", "failed", "validated"):
                continue
            try:
                await readmit_invalid_saved(self, state, client, only_change=change)
            except SourceSupportUnavailable as exc:
                self._isolate_source_group(state, change, exc)
                continue
            if change.status != "pending":
                continue  # copied original target still enters full validation
            self._retain_planned_type(change, {})
            if not await self._verify_group_support(state, change, client):
                continue
            evidence = self._evidence(state)
            self._require_page_capacity(state, change, evidence)
            page_evidence = self._page_evidence(state, change, evidence)
            existing_raw = None
            revision = change.base_commit or state.base_commit
            if change.action == "update" and revision:
                existing_raw = gittools.read_blob(checkout, revision, change.path)
                # record the exact base this draft is made against so
                # publication can detect genuinely intervening edits
                change.base_blob = gittools.blob_hash(checkout, f"{revision}:{change.path}")
            corrections = [c for c in corrections_all if c.get("page") == change.path]

            def draft_checkpoint(partial):
                change.draft_body = str(partial.get("body", ""))
                change.draft_meta = partial.get("frontmatter") or {}
                self._retain_planned_type(change, change.draft_meta)
                change.draft_progress = partial.get("draft_progress") or {}
                self._persist(state)
                self._check_cancel(state)

            if change.draft_body:
                existing_raw = fm.build(change.draft_meta, change.draft_body)
            try:
                draft = await curate.draft_page(
                    client,
                    state.intent,
                    change,
                    page_evidence,
                    existing_raw,
                    corrections,
                    char_budget=self.cfg.limits.prompt_char_budget,
                    today=_today(),
                    progress=draft_checkpoint,
                )
            except (OutputTruncated, ReplyRejected, UnitTooLarge):
                # Retain the complete existing page and every selected claim.
                # Bounded local edits reduce output and input per section, and
                # drop only a unit whose own reply or input cannot be handled.
                from doclayer.curator.editing import edit_page

                draft = await edit_page(
                    client,
                    state.intent,
                    change,
                    page_evidence,
                    existing_raw,
                    corrections,
                    char_budget=self.cfg.limits.prompt_char_budget,
                    today=_today(),
                    progress=draft_checkpoint,
                )
            raw_meta = draft.get("frontmatter")
            meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
            self._finalize_meta(state, change, existing_raw, page_evidence, meta)
            finalize_draft(change, draft, page_evidence)
            self._record_not_integrated(state, change)
            change.status = "drafted"
            state.evidence_selection["drafted:" + change.path] = {
                "capacity_generation": CAPACITY_GENERATION,
                "records": len(page_evidence),
            }
            for note in draft.get("notes", [])[:8]:
                change.notes.append(str(note)[:300])
            self._persist(state)
        return self._advance_if_done(state, "draft")

    def _finalize_meta(
        self,
        state: JobState,
        change: PageChange,
        existing_raw: str | None,
        page_evidence: list[Evidence],
        meta: dict[str, Any],
    ) -> dict[str, Any]:
        """The draft's frontmatter: the page's identity and lineage kept or minted,
        defaults filled. Sets and returns ``change.draft_meta``."""
        self._retain_planned_type(change, meta)
        meta.setdefault("schema_version", 1)
        meta.setdefault("subject_type", change.subject_type)
        if change.action == "update" and existing_raw:
            existing_meta, _ = fm.parse(existing_raw)
            for key in ("concept_id", "privacy", "source_origin", "address", "source_lineage"):
                if key in existing_meta:
                    meta[key] = existing_meta[key]
        if change.action == "create":
            meta["concept_id"] = "c-" + secrets.token_hex(4)
            meta["privacy"] = "internal"
        meta.setdefault("source_lineage", [f"curator:{state.job_id}"])
        meta["source_lineage"] = list(
            dict.fromkeys(
                [
                    *(
                        [x for x in meta["source_lineage"] if isinstance(x, str)]
                        if isinstance(meta["source_lineage"], list)
                        else []
                    ),
                    f"curator:{state.job_id}",
                    *(f"source:{e.url}" for e in page_evidence if e.url),
                ]
            )
        )
        if state.private:
            checks = [re.match(r"^live check (\S+) .* @(\d{4}-\d{2}-\d{2})T", e.label or "") for e in page_evidence]
            meta["source_lineage"] = list(
                dict.fromkeys(
                    [*meta["source_lineage"], *(f"live-check:{m.group(1)}@{m.group(2)}" for m in checks if m)]
                )
            )
        meta.setdefault("tags", ["wiki"])
        meta.setdefault("updated", _today())
        meta.setdefault("title", change.title or Path(change.path).stem)
        if change.subject_type:
            meta.setdefault("subject_type", change.subject_type)
        change.draft_meta = meta
        return meta

    @staticmethod
    def _record_not_integrated(state, change):
        """Claims the editor could not prove after their bounded re-dispatch are
        current coverage gaps and leave the page's claims; the page continues."""
        from doclayer.curator.coverage import not_integrated_gap

        missing = (change.draft_progress or {}).get("not_integrated") or {}
        for identity, row in sorted(missing.items()):
            note = not_integrated_gap(change.path, identity, row)
            if note not in state.coverage_gaps:
                state.coverage_gaps.append(note)
        # An unproven claim is no longer an obligation of this page, so it can
        # never count as published with it.
        change.claim_ids = [identity for identity in change.claim_ids if identity not in missing]

    @staticmethod
    def _retain_planned_type(change, meta):
        """A new page keeps the planner's validated classification, not a draft alias."""
        if change.action == "create":
            if change.subject_type not in SUBJECT_TYPES:
                raise ModelError("new page lacks a valid planned subject_type", transient=True)
            meta["subject_type"] = change.subject_type

    def _repair_binding(
        self, state: JobState, change: PageChange, evidence: list[Evidence], corrections: list[dict[str, Any]]
    ) -> str:
        """Bind an active pass to its actual material and policy generation."""
        records = self._page_records(change, evidence)
        source_ids = {record.source_id for record in records}
        material = {
            "version": 2,
            "repair_generation": REPAIR_BINDING_GENERATION,
            "preservation_version": preservation.VERSION,
            "page": change.path,
            "base_commit": change.base_commit or state.base_commit,
            "base_blob": change.base_blob,
            "intent": state.intent,
            "source_policy": state.source_policy,
            "corrections": corrections,
            "required": change.claim_ids,
            "missing": change.missing_evidence,
            "support_version": curate.SOURCE_SUPPORT_VERSION,
            "records": [record.to_dict() for record in sorted(records, key=curate.evidence_id)],
            "source_revisions": sorted(
                (source.id, source.content_hash) for source in state.sources if source.id in source_ids
            ),
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def _draft_body_hash(change: PageChange) -> str:
        return hashlib.sha256(change.draft_body.encode()).hexdigest()

    async def _preserve_existing(
        self,
        state,
        change,
        client,
        records,
        corrections,
        *,
        original_raw=None,
        canonical_binding=None,
        namespace="existing_preservation",
        checkpoint_change=None,
    ):
        """Audit old facts against retained sources, separately from incoming IDs."""
        if original_raw is None:
            if change.action == "create":
                return {"failures": [], "complete": True}
            original_raw = self._reconciliation_base(state, change)
            canonical_binding = {
                "commit": change.base_commit or state.base_commit,
                "blob": change.base_blob,
                "path": change.path,
            }
        original_meta, original_body = fm.parse(original_raw)
        resolver = self._support_resolver(state, client)

        def checkpoint(progress):
            change.check_progress = progress
            if checkpoint_change is not None:
                checkpoint_change.reconcile_check_progress = copy.deepcopy(progress)
            self._persist(state)
            self._check_cancel(state)

        return await preservation.audit_existing_preservation(
            client,
            state.intent,
            change,
            records,
            corrections,
            original_meta=original_meta,
            original_body=original_body,
            canonical_binding=canonical_binding,
            char_budget=self.cfg.limits.prompt_char_budget,
            source_policy=state.source_policy,
            source_binding=getattr(resolver, "binding", None),
            support_resolver=resolver,
            progress=checkpoint,
            namespace=namespace,
        )

    def _editorial_original(self, state, change):
        """Use recorded Git bytes to distinguish additions from retained knowledge."""
        if change.action == "create":
            return {"original_meta": {}, "original_body": ""}
        metadata, body = fm.parse(self._reconciliation_base(state, change))
        return {"original_meta": metadata, "original_body": body}

    @staticmethod
    def _record_review_conflicts(state, *reviews):
        for review in reviews:
            for conflict in review.get("conflicts", []):
                if str(conflict) not in state.conflicts:
                    state.conflicts.append(str(conflict))

    def _repair_evidence(self, state, change, evidence):
        records = self._page_evidence(state, change, evidence)
        records += preservation.existing_repair_records(change)
        records += preservation.existing_repair_records(change, namespace="existing_preservation_fresh")
        return list({curate.evidence_id(record): record for record in records}.values())

    # -- stage: validate -------------------------------------------------------- #

    async def _stage_validate(self, state: JobState) -> str | None:
        client = self._model(state)
        checkout = Path(self.cfg.paths.brain_checkout).expanduser()
        evidence = self._evidence(state)
        max_repairs = int(state.limits.get("max_repair_passes", self.cfg.limits.max_repair_passes))

        known_by_revision = {}

        def known_for(change):
            revision = change.base_commit or state.base_commit
            if revision not in known_by_revision:
                known = KnownPages()
                for path in gittools.tracked_paths(checkout, revision):
                    if path.endswith(".md"):
                        meta, _ = fm.parse(gittools.read_blob(checkout, revision, path) or "")
                        known.pages[path] = meta
                known.published_paths.update(c.path for c in state.changes if c.status in ("drafted", "validated"))
                known_by_revision[revision] = known
            return known_by_revision[revision]

        def existing_for(change: PageChange) -> tuple[dict[str, Any] | None, str | None]:
            if change.action == "create":
                return None, None
            raw = gittools.read_blob(checkout, change.base_commit or state.base_commit, change.path)
            if raw is None:
                return None, None
            meta, body = fm.parse(raw)
            return meta, body

        for change in state.changes:
            self._check_cancel(state)
            from doclayer.curator.plan_readmission import readmit_invalid_saved

            try:
                readmitted = await readmit_invalid_saved(self, state, client, only_change=change)
            except SourceSupportUnavailable as exc:
                self._isolate_source_group(state, change, exc)
                continue
            if readmitted:
                self._persist(state)
                return "draft" if any(item.status == "pending" for item in state.changes) else "validate"
            if change.status not in ("drafted", "failed"):
                continue
            self._retain_planned_type(change, change.draft_meta)
            corrections = [c for c in self.store.corrections() if c.get("page") == change.path]
            if not await self._verify_group_support(state, change, client):
                continue
            evidence = self._evidence(state)
            # A correction job keeps the base rules: a stale draft fails there, it is not redrafted.
            stale = self._stale_draft(state, change, evidence) if state.correction_of is None else None
            if stale is not None:
                # Drafted before evidence was bounded: redraft this page now
                # rather than fail it; the draft stage returns to validation.
                if self._schedule_redraft(state, change, stale):
                    return "draft"
                continue
            last_repair_failures = None  # deterministic failure set behind the previous completed pass
            deterministic_states: set = set()  # draft states the deterministic repair produced
            last_pass_kind = None  # "scope" or "general": the pass behind last_repair_failures
            while True:
                binding = self._repair_binding(state, change, evidence, corrections)
                if change.repair_context and (
                    change.repair_context.get("binding_hash") != binding
                    or change.repair_context.get("body_hash") != self._draft_body_hash(change)
                ):
                    # New material must not inherit completed work; the job's
                    # repair cap remains durable across changed generations.
                    change.repair_context = {}
                    from doclayer.curator.editing import retain_edit_structure

                    anchors = change.draft_progress.get("claim_anchors")
                    change.draft_progress = retain_edit_structure(change.draft_progress, change.draft_body)
                    if anchors:  # the same body: accepted anchors stand unchanged
                        change.draft_progress["claim_anchors"] = dict(anchors)
                    self._persist(state)
                deterministic_only = False
                if change.repair_context:
                    failures = [
                        Failure(str(item.get("check", "model")), str(item.get("detail", "")))
                        for item in change.repair_context["failures"]
                    ]
                else:
                    if not await self._verify_group_support(state, change, client):
                        break
                    evidence = self._evidence(state)
                    existing_meta, existing_body = existing_for(change)
                    if state.correction_of is None:
                        atx = normalize_setext(change.draft_body)
                        if atx != change.draft_body:
                            # CRLF becomes LF and setext headings become ATX before any
                            # check or restore reads the page's headings (a correction
                            # job keeps the base rules). The editor's heading-rename proof is
                            # carried to the normalised body, so a proven rename stands.
                            from doclayer.curator.editing import (
                                rebind_claim_anchors,
                                retain_edit_structure,
                            )

                            structure = retain_edit_structure(change.draft_progress, change.draft_body)
                            anchors = change.draft_progress.get("claim_anchors")
                            unnormalised = change.draft_body
                            lf = change.draft_body.replace("\r\n", "\n")
                            if lf != change.draft_body:
                                change.notes.append("normalised CRLF line endings to LF")
                            if lf != atx:
                                change.notes.append("normalised setext heading(s) to ATX")
                            change.draft_body = atx
                            change.draft_progress = (
                                {
                                    key: structure[key]
                                    for key in ("editor_version", "heading_renames")
                                    if key in structure
                                }
                                | {"body_hash": self._draft_body_hash(change)}
                                if structure
                                else {}
                            )
                            if anchors:
                                change.draft_progress["claim_anchors"] = rebind_claim_anchors(
                                    anchors,
                                    unnormalised,
                                    change.draft_body,
                                    lambda anchor: anchor.replace("\r\n", "\n"),
                                )
                            self._persist(state)
                    failures = run_deterministic_checks(
                        change,
                        self.cfg,
                        known_for(change),
                        existing_meta,
                        existing_body,
                        corrections,
                        **self._correction_kwargs(state),
                    )
                    if change.missing_evidence:
                        failures.append(
                            Failure(
                                "evidence", "required claim support unavailable: " + ", ".join(change.missing_evidence)
                            )
                        )
                    failures.extend(self._scope_failures(state, change.draft_body, existing_body))
                    if state.correction_of is None and any(f.check in ("scope", "title") for f in blockers(failures)):
                        # Dropped base sections and titles are restored without a model
                        # pass, whatever the pass budget; then the page revalidates. A
                        # correction job never gets these repairs.
                        snapshot = (
                            change.draft_body,
                            copy.deepcopy(change.draft_meta),
                            change.title,
                            copy.deepcopy(change.draft_progress),
                            list(change.notes),
                        )
                        changed, unrestorable = self._restore_structure(state, change, existing_body, existing_meta)
                        failures = unrestorable + failures
                        if changed:
                            # Progress is a draft state not seen before in this loop, within a
                            # hard cap. A pass that reaches nothing new ends deterministic repair
                            # and is undone, so the model repairs the draft the failures describe.
                            fingerprint = (
                                change.draft_body,
                                json.dumps(change.draft_meta, sort_keys=True, default=str),
                                change.title,
                            )
                            if (
                                fingerprint in deterministic_states
                                or len(deterministic_states) >= MAX_DETERMINISTIC_PASSES
                            ):
                                (
                                    change.draft_body,
                                    change.draft_meta,
                                    change.title,
                                    change.draft_progress,
                                    change.notes,
                                ) = snapshot
                                self._persist(state)
                                changed = False
                            deterministic_states.add(fingerprint)
                        if changed:
                            continue
                    # Blockers found before any model review are reproducible:
                    # the same draft state yields the same set on every pass.
                    deterministic_only = bool(blockers(failures))
                    if not blockers(failures):

                        def check_checkpoint(progress):
                            change.check_progress = progress
                            self._persist(state)
                            self._check_cancel(state)

                        try:
                            check = await curate.check_draft(
                                client,
                                state.intent + f"\nBinding source/applicability policy: {state.source_policy}",
                                change,
                                self._review_evidence(state, change, evidence),
                                corrections,
                                char_budget=self.cfg.limits.prompt_char_budget,
                                progress=check_checkpoint,
                                support_resolver=self._support_resolver(state, client),
                                source_policy=state.source_policy,
                                **self._editorial_original(state, change),
                            )
                        except EditorialContextOverflow as exc:
                            self._isolate_review_group(state, change, [Failure("editorial-context", str(exc))])
                            break
                        except OutputTruncated:
                            check = {
                                "failures": [
                                    {
                                        "check": "evidence",
                                        "detail": "model evidence check truncated; verification incomplete",
                                        "severity": "blocker",
                                    }
                                ],
                                "conflicts": [],
                            }
                        try:
                            previous = await self._preserve_existing(
                                state,
                                change,
                                client,
                                [e for e in self._page_records(change, evidence) if e.support_status == "supported"],
                                corrections,
                            )
                        except EditorialContextOverflow as exc:
                            self._isolate_review_group(state, change, [Failure("editorial-context", str(exc))])
                            break
                        check.setdefault("failures", []).extend(previous.get("failures", []))
                        check.setdefault("conflicts", []).extend(previous.get("conflicts", []))
                        model_blockers = [
                            Failure(str(f.get("check", "model")), str(f.get("detail", "")))
                            for f in preservation.blocking_failures(check)
                            if isinstance(f, dict)
                        ]
                        self._record_review_conflicts(state, check)
                        # Retained-under-budget notes describe the current draft only.
                        change.notes = [note for note in change.notes if not str(note).startswith(RETAINED_NOTE)]
                        for note in check.get("notes", []):
                            if note not in change.notes:
                                change.notes.append(note)
                        failures = blockers(failures + model_blockers)
                if not failures:
                    change.status = "validated"
                    if state.correction_of is None:  # a correction job keeps its recorded state
                        self._clear_isolation(state, change)  # a resumed failed page no longer reports its old failure
                    change.validation = [f.as_dict() for f in failures] or [
                        {"severity": "pass", "check": "all", "detail": "checks passed"}
                    ]
                    break
                # A draft citing an excluded source gets dedicated scope passes (at
                # most MAX_SCOPE_REPAIR_PASSES, within the job's model-call limit) that
                # do not spend the general repair passes. Not in a correction job.
                scope_cites = [f for f in failures if self._cites_excluded(f)]
                scope_pass = bool(
                    state.correction_of is None
                    and not change.repair_context
                    and scope_cites
                    and change.scope_repair_passes < MAX_SCOPE_REPAIR_PASSES
                )
                if not change.repair_context and not scope_pass and change.repair_passes >= max_repairs:
                    self._isolate_review_group(state, change, failures)
                    break
                signature = sorted({(f.check, f.detail) for f in failures}) if deterministic_only else None
                if not change.repair_context and signature is not None and last_repair_failures == signature:
                    # A pass that leaves the same failures will not fix them on
                    # the next attempt; stop instead of spending another pass.
                    stalled = (
                        f"scope pass {change.scope_repair_passes} left the identical scope blockers"
                        if last_pass_kind == "scope"
                        else f"repair pass {change.repair_passes} left the identical failure set"
                    )
                    self._isolate_review_group(
                        state,
                        change,
                        [
                            Failure(
                                "repair-stalled",
                                f"{stalled}; no further pass spent: "
                                + "; ".join(f"{check}: {detail[:200]}" for check, detail in signature[:4]),
                            )
                        ],
                    )
                    break
                if not change.repair_context and scope_pass:
                    change.scope_repair_passes += 1
                    instruction = self._scope_instruction(state, change, scope_cites, existing_body)
                    change.repair_context = {
                        "kind": "scope",
                        "failures": [{"severity": "blocker", "check": "scope", "detail": instruction}]
                        + [f.as_dict() for f in scope_cites],
                        "intent": state.intent + "\n" + instruction,
                        "binding_hash": self._repair_binding(state, change, evidence, corrections),
                        "body_hash": self._draft_body_hash(change),
                    }
                    self._persist(state)  # logical pass admission survives an interrupted request
                elif not change.repair_context:
                    change.repair_passes += 1
                    change.repair_context = {
                        "failures": [f.as_dict() for f in failures],
                        "intent": state.intent,
                        "binding_hash": self._repair_binding(state, change, evidence, corrections),
                        "body_hash": self._draft_body_hash(change),
                    }
                    self._persist(state)  # logical pass admission survives an interrupted request
                last_repair_failures = signature  # model-judged failures may differ on a re-ask
                last_pass_kind = change.repair_context.get("kind") or "general"

                def repair_checkpoint(partial):
                    change.draft_meta = partial.get("frontmatter") or change.draft_meta
                    self._retain_planned_type(change, change.draft_meta)
                    change.draft_body = str(partial.get("body", change.draft_body))
                    change.draft_progress = partial.get("draft_progress") or {}
                    change.repair_context["body_hash"] = self._draft_body_hash(change)
                    self._persist(state)
                    self._check_cancel(state)

                try:
                    self._seed_structural_baseline(state, change)
                    repaired = await curate.repair_draft(
                        client,
                        change.repair_context["intent"],
                        change,
                        change.repair_context["failures"],
                        self._repair_evidence(state, change, evidence),
                        corrections,
                        char_budget=self.cfg.limits.prompt_char_budget,
                        progress=repair_checkpoint,
                    )
                except OutputTruncated:
                    change.status = "failed"
                    change.error = "repair output truncated"
                    change.repair_context = {}
                    break
                meta = repaired.get("frontmatter") or change.draft_meta
                for key in ("concept_id", "privacy", "source_lineage"):
                    if key in change.draft_meta:
                        meta[key] = change.draft_meta[key]
                self._retain_planned_type(change, meta)
                change.draft_meta = meta
                finalize_draft(change, repaired, self._page_evidence(state, change, evidence))
                self._record_not_integrated(state, change)
                change.repair_context = {}
                self._persist(state)
            self._persist(state)

        self._enforce_group_coherence(state)
        self._persist(state)
        return self._advance_if_done(state, "validate")

    # -- stage: publish ------------------------------------------------------- #

    def _superseded_sections(self, state: JobState, change: PageChange, merged: dict[str, Any]) -> list[dict[str, Any]]:
        """Dropped draft sections as ``{"section", "digest", "claim_ids"}``.

        A claim belongs to a dropped section only when one of its recorded
        anchors (the drafting disposition anchor or its placement coverage
        anchor), at least ``MIN_ANCHOR`` characters long, occurs exactly once
        in that section's draft view; otherwise it stays assigned.
        """
        anchors: dict[str, list[str]] = {}
        for identity, anchor in (change.draft_progress.get("claim_anchors") or {}).items():
            anchors.setdefault(identity, []).append(anchor)
        ledger = state.evidence_selection.get("placement", {}).get("ledger", {}).get("claims", {})
        for identity in change.claim_ids:
            anchors.setdefault(identity, []).append((ledger.get(identity, {}).get("coverage") or {}).get("anchor"))
        rows = []
        for row in merged.get("dropped") or []:
            text = str(row.get("text", ""))
            rows.append(
                {
                    "section": str(row.get("view", "")),
                    "digest": str(row.get("digest", "")),
                    "claim_ids": [
                        identity
                        for identity in change.claim_ids
                        if any(
                            isinstance(anchor, str) and len(anchor) >= MIN_ANCHOR and text.count(anchor) == 1
                            for anchor in anchors.get(identity, [])
                        )
                    ],
                }
            )
        return rows

    def _reconciliation_base(self, state: JobState, change: PageChange) -> str:
        """Read the original page bound to the job, never substitute fresh main."""
        checkout = Path(self.cfg.paths.brain_checkout).expanduser()
        revision = change.base_commit or state.base_commit
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision or ""):
            raise ModelError("stale reconciliation lacks a recorded base revision", transient=False)
        recorded = gittools.blob_hash(checkout, f"{revision}:{change.path}")
        if not recorded or recorded != change.base_blob:
            raise ModelError("stale reconciliation base does not match the recorded page blob", transient=False)
        original = gittools.read_blob(checkout, revision, change.path)
        if original is None:
            raise ModelError("stale reconciliation original page is unavailable", transient=False)
        return original

    def _seed_structural_baseline(self, state: JobState, change: PageChange) -> None:
        """Structural heading repair needs the bound canonical body even when
        semantic preservation did not run; without it the repair is skipped."""
        if change.action != "update":
            return
        if isinstance(change.check_progress.get("existing_preservation", {}).get("original_body"), str):
            return
        review = change.check_progress.get("article_review") or {}
        body_sha256 = review.get("body_sha256")
        if not body_sha256 or not change.base_commit:
            return  # The repair would reject an unbindable baseline; leave it unseeded.
        seeded = change.check_progress.get("structural_baseline", {})
        if isinstance(seeded.get("original_body"), str):
            seeded["body_sha256"] = body_sha256  # Follow the current draft across repair passes.
            return
        try:
            original = self._reconciliation_base(state, change)
        except ModelError:
            return  # No bound base: repair keeps its existing refusal path.
        original_meta, original_body = fm.parse(original)
        change.check_progress["structural_baseline"] = {
            "original_body": original_body,
            "original_meta": json.loads(json.dumps(original_meta, default=str)),  # YAML dates must persist as JSON
            "body_sha256": body_sha256,
            "canonical_binding": {"commit": change.base_commit, "blob": change.base_blob, "path": change.path},
        }

    async def _stage_publish(self, state: JobState) -> str | None:
        validated = [c for c in state.changes if c.status == "validated"]
        publisher = self._publisher or Publisher(self.cfg, self.store)
        # Actual remote merge recovery is read-only and precedes inference or
        # new audits; an already-landed receipt cannot depend on Qwen uptime.
        if hasattr(publisher, "recover_existing_merge"):
            recovered = await asyncio.to_thread(
                publisher.recover_existing_merge, state, validated, lambda: self._persist(state, check_budget=False)
            )
            if recovered is not None:
                self._derive_status(state)
                return self._advance_if_done(state, "publish")
        if not state.publication.commit_sha:
            # A stopped publication check can leave a draft beside validated
            # peers. Finish the same saved work before creating a new batch.
            if any(c.status == "pending" for c in state.changes):
                return "draft"
            if any(c.status == "drafted" for c in state.changes):
                return "validate"
        if not validated:
            return self._advance_if_done(state, "publish")
        self._check_time(state)  # Fresh/open publication is still bounded work.
        self._evidence(state)
        checkout = Path(self.cfg.paths.brain_checkout).expanduser()
        client = self._model(state)

        from doclayer.curator.plan_readmission import readmit_invalid_saved

        for change in validated:
            if change.status != "validated":
                continue
            try:
                readmitted = await readmit_invalid_saved(self, state, client, only_change=change)
            except SourceSupportUnavailable as exc:
                self._isolate_source_group(state, change, exc)
                continue
            if readmitted:
                self._persist(state)
                return "draft" if any(item.status == "pending" for item in state.changes) else "validate"

        # Resumed pre-upgrade validated jobs must pass the new obligation too.
        # A blocked preservation check returns through normal bounded repair.
        retry_validation = False
        for change in validated:
            if change.status != "validated":
                continue
            self._retain_planned_type(change, change.draft_meta)
            corrections = [c for c in self.store.corrections() if c.get("page") == change.path]
            if not await self._verify_group_support(state, change, client):
                continue
            current = self._evidence(state)
            backing = [e for e in self._page_records(change, current) if e.support_status == "supported"]
            stale = (
                None
                if state.publication.commit_sha or state.correction_of is not None
                else self._stale_draft(state, change, current)
            )
            if stale is not None:
                # A recorded commit is immutable; otherwise redraft this page now.
                if self._schedule_redraft(state, change, stale):
                    return "draft"
                continue
            failed_check = bool(
                change.missing_evidence
                or self._scope_failures(
                    state, change.draft_body, self._editorial_original(state, change)["original_body"]
                )
            )
            checked = {}
            if not failed_check:

                def publication_checkpoint(progress):
                    change.check_progress = progress
                    self._persist(state)
                    self._check_cancel(state)

                try:
                    checked = await curate.check_draft(
                        client,
                        state.intent + f"\nBinding source/applicability policy: {state.source_policy}",
                        change,
                        self._review_evidence(state, change, current),
                        corrections,
                        char_budget=self.cfg.limits.prompt_char_budget,
                        progress=publication_checkpoint,
                        support_resolver=self._support_resolver(state, client),
                        source_policy=state.source_policy,
                        **self._editorial_original(state, change),
                    )
                    failed_check = bool(preservation.blocking_failures(checked))
                except EditorialContextOverflow as exc:
                    self._isolate_review_group(state, change, [Failure("editorial-context", str(exc))])
                    continue
                except OutputTruncated:
                    failed_check = True
            try:
                retained = await self._preserve_existing(state, change, client, backing, corrections)
            except EditorialContextOverflow as exc:
                self._isolate_review_group(state, change, [Failure("editorial-context", str(exc))])
                continue
            self._record_review_conflicts(state, checked, retained)
            if failed_check or preservation.blocking_failures(retained):
                if state.publication.commit_sha:
                    # An existing recorded commit is immutable work, not the
                    # current draft. Repair must never merge that stale commit.
                    change.status = "conflict"
                    change.error = (
                        "Unmerged committed publication failed current source/preservation review; requires a fresh job"
                    )
                    state.coverage_gaps.append(change.path + ": " + change.error)
                else:
                    change.status = "drafted"
                    change.repair_context = {}
                    retry_validation = True
        self._enforce_group_coherence(state)
        self._persist(state)
        if retry_validation:
            return "validate"
        if state.publication.commit_sha and any(c.status == "conflict" for c in validated):
            # Do not merge an existing batch commit containing a blocked page.
            for change in validated:
                if change.status == "validated":
                    change.status = "conflict"
                    change.error = "Existing unmerged batch commit contains a blocked page"
            self._persist(state)
            return self._advance_if_done(state, "publish")

        validated = [change for change in validated if change.status == "validated"]
        if not validated:
            return self._advance_if_done(state, "publish")

        def reconciler(change: PageChange, fresh_page: str, collision: bool) -> dict[str, Any]:
            """Runs in the publisher's thread: owns its own event loop for the
            bounded model calls."""
            corrections = [c for c in self.store.corrections() if c.get("page") == change.path]

            async def _run() -> dict[str, Any]:
                from doclayer.curator import reconciliation

                def reconcile_checkpoint(progress):
                    change.reconcile_progress = progress
                    self._persist(state)
                    self._check_cancel(state)

                if collision:
                    merged = await reconciliation.reconcile_collision(
                        client,
                        state.intent,
                        change,
                        fresh_page,
                        corrections,
                        char_budget=self.cfg.limits.prompt_char_budget,
                        progress=reconcile_checkpoint,
                        correction_job=state.correction_of is not None,
                    )
                else:
                    merged = await reconciliation.reconcile_stale(
                        client,
                        state.intent,
                        change,
                        fresh_page,
                        corrections,
                        base_page=self._reconciliation_base(state, change),
                        char_budget=self.cfg.limits.prompt_char_budget,
                        progress=reconcile_checkpoint,
                        correction_job=state.correction_of is not None,
                    )
                candidate = copy.deepcopy(change)
                # A draft section whose only failure was editing protected fresh
                # material was kept exactly as fresh; claims anchored in it are no
                # longer this publication's obligation. The publisher records the
                # sections with the reconciled body (publish.apply_superseded).
                superseded = self._superseded_sections(state, change, merged)
                dropped_ids = {identity for row in superseded for identity in row["claim_ids"]}
                candidate.claim_ids = [c for c in candidate.claim_ids if c not in dropped_ids]
                candidate.draft_body = str(merged.get("body", ""))
                candidate.draft_meta = merged.get("frontmatter") or {}
                candidate.draft_progress = merged.get("draft_progress") or {}
                candidate.reconcile_progress = merged.get("reconcile_progress") or {}
                # The checker binds every checkpoint to its assembled body,
                # metadata and backing. Reuse matching interrupted work; it
                # independently invalidates an old draft's mismatched cache.
                candidate.check_progress = copy.deepcopy(change.reconcile_check_progress or change.check_progress)
                await self._verify_selected(state, candidate, client)
                current_evidence = self._evidence(state)
                support = self._page_evidence(
                    state, candidate, [e for e in current_evidence if e.support_status == "supported"]
                )
                if candidate.missing_evidence:
                    raise ReconcileFailed("reconciled draft is missing required claim evidence")
                finalize_draft(candidate, merged, support)

                def check_checkpoint(progress):
                    candidate.check_progress = progress
                    change.reconcile_check_progress = copy.deepcopy(progress)
                    self._persist(state)
                    self._check_cancel(state)

                check = await curate.check_draft(
                    client,
                    state.intent + f"\nBinding source/applicability policy: {state.source_policy}",
                    candidate,
                    self._review_evidence(state, candidate, current_evidence),
                    corrections,
                    char_budget=self.cfg.limits.prompt_char_budget,
                    progress=check_checkpoint,
                    support_resolver=self._support_resolver(state, client),
                    source_policy=state.source_policy,
                    **self._editorial_original(state, candidate),
                )
                backing = [
                    e for e in self._page_records(candidate, current_evidence) if e.support_status == "supported"
                ]
                original_check = await self._preserve_existing(
                    state, candidate, client, backing, corrections, checkpoint_change=change
                )
                # A distinct-subject collision renamed to a new path must not
                # inherit facts from the other subject's canonical page.
                fresh_check = {"failures": []}
                if not collision or merged.get("resolution", "same-subject") == "same-subject":
                    fresh_check = await self._preserve_existing(
                        state,
                        candidate,
                        client,
                        backing,
                        corrections,
                        original_raw=fresh_page,
                        canonical_binding={
                            "path": candidate.path,
                            "fresh_sha256": hashlib.sha256(fresh_page.encode()).hexdigest(),
                        },
                        namespace="existing_preservation_fresh",
                        checkpoint_change=change,
                    )
                self._record_review_conflicts(state, check, original_check, fresh_check)
                change.reconcile_check_progress = copy.deepcopy(candidate.check_progress)
                self._persist(state)
                if any(preservation.blocking_failures(result) for result in (check, original_check, fresh_check)):
                    raise ReconcileFailed("reconciled draft fails evidence/applicability/preservation check")
                merged.update(
                    body=candidate.draft_body,
                    frontmatter=candidate.draft_meta,
                    draft_progress=candidate.draft_progress,
                    check_progress=candidate.check_progress,
                    reconcile_progress=candidate.reconcile_progress,
                    superseded=superseded,
                )
                return merged

            async def _recorded() -> dict[str, Any]:
                # The publisher's thread does not share the job loop's context.
                with degrade.recording(state.coverage_gaps):
                    return await _run()

            try:
                merged = asyncio.run(_recorded())
            except OutputTruncated as exc:
                raise ReconcileFailed(f"reconciliation output truncated: {exc}") from exc
            except SourceSupportUnavailable as exc:
                self._isolate_source_group(state, change, exc)
                raise ReconcileFailed(str(exc)) from exc
            except ModelError as exc:
                if exc.transient:
                    raise  # preserve retryable publication-stage support/outage work
                raise ReconcileFailed(f"reconciliation model failure: {exc}") from exc
            result: dict[str, Any] = {
                "meta": merged.get("frontmatter") or {},
                "body": str(merged.get("body", "")),
                "draft_progress": merged.get("draft_progress") or {},
                "check_progress": merged.get("check_progress") or {},
                "reconcile_progress": merged.get("reconcile_progress") or {},
                "notes": [str(n)[:300] for n in merged.get("notes", []) if isinstance(n, str)],
                "superseded": merged.get("superseded") or [],
            }
            if collision:
                result["resolution"] = merged.get("resolution", "same-subject")
                result["explanation"] = str(merged.get("explanation", ""))[:400]
            return result

        def revalidate(change: PageChange, fresh_meta: Any, fresh_body: Any) -> list[Failure]:
            return self._publication_revalidate(state, checkout, change, fresh_meta, fresh_body)

        outcome = await asyncio.to_thread(
            publisher.publish_changes,
            state,
            validated,
            reconciler,
            revalidate,
            lambda: self._persist(state),
        )
        for note in outcome.notes:
            state.coverage_gaps.append(note)
        if outcome.conflicted_changes:
            state.coverage_gaps.append("unpublished after reconciliation: " + ", ".join(outcome.conflicted_changes))
        self._persist(state)
        return self._advance_if_done(state, "publish")

    def _publication_revalidate(
        self, state: JobState, checkout: Path, change: PageChange, fresh_meta: Any, fresh_body: Any
    ) -> list[Failure]:
        """Deterministic checks of a draft reconciled onto the fresh page."""
        corrections = [c for c in self.store.corrections() if c.get("page") == change.path]
        known = KnownPages()
        for path in gittools.tracked_paths(checkout, f"{self.cfg.publish.remote}/{self.cfg.publish.base_branch}"):
            if path.endswith(".md") and path != change.path:
                raw = gittools.read_blob(
                    checkout,
                    f"{self.cfg.publish.remote}/{self.cfg.publish.base_branch}",
                    path,
                )
                if raw is not None:
                    meta, _ = fm.parse(raw)
                    known.pages[path] = meta
        known.published_paths = {c.path for c in state.changes if c.status == "validated"}
        existing_meta, existing_body = (fresh_meta, fresh_body) if fresh_body is not None else (None, None)
        options = self._correction_kwargs(state)
        if state.correction_of is None and existing_body is not None:
            # Reconciled onto the fresh page: its title is the page's title now (an
            # operator retitle since drafting stands), and only the page's own H1 and
            # frontmatter title are checked against it.
            change.title = str((existing_meta or {}).get("title") or h1_text(existing_body) or change.title)
            options = {"publication": True}
        failures = run_deterministic_checks(
            change,
            self.cfg,
            known,
            existing_meta,
            existing_body,
            corrections,
            **options,
        )
        failures.extend(self._scope_failures(state, change.draft_body, existing_body))
        return failures

    # -- stage: report ----------------------------------------------------------- #

    async def _stage_report(self, state: JobState) -> str | None:
        if state.coverage_basis is None:
            state.coverage_basis = state.coverage
        state.coverage = state.effective_coverage
        self._derive_status(state)
        digest = self.build_digest(state)
        state.summary = digest
        self.store.write_summary(state.job_id, digest)
        if state.status in ("published", "partial") and any(c.status == "published" for c in state.changes):
            self.store.record_published_input(
                state.input_digest,
                state.job_id,
                [c.path for c in state.changes if c.status == "published"],
            )
        self._persist(state)
        return None

    def _derive_status(self, state: JobState) -> None:
        if state.status in ("cancelled", "failed", "retryable"):
            return
        if state.correction_of:
            published = [c for c in state.changes if c.status == "published"]
            conflicts = [c for c in state.changes if c.status != "published"]
            state.status = "published" if published and not conflicts else "partial" if published else "retryable"
            return
        planned = [c for c in state.changes if c.path and not (state.mode == "single-pass" and c.status == "skipped")]
        published = [c for c in planned if c.status == "published"]
        conflicted = [c for c in planned if c.status != "published"]
        if not planned:
            state.status = "covered" if state.effective_coverage == "complete" else "partial"
        elif published and not conflicted:
            state.status = "published"
        elif published:
            state.status = "partial"
        else:
            state.status = "failed"

    def _finish_report(self, state: JobState) -> None:
        """Write a digest for jobs that stop early (cancel/failure/limits)."""
        self._account_active_time(state)
        if state.coverage_basis is None:
            state.coverage_basis = state.coverage
        state.coverage = state.effective_coverage
        digest = self.build_digest(state)
        state.summary = digest
        try:
            self.store.write_summary(state.job_id, digest)
        except OSError:
            pass

    # -- digest ------------------------------------------------------------------ #

    def build_digest(self, state: JobState) -> str:
        lines = [f"# Curator job {state.job_id}", ""]
        lines.append(f"- **Intent:** {state.intent}")
        lines.append(f"- **Status:** `{state.status}` (stage: {state.stage})")
        lines.append(f"- **Mode:** {state.mode}")
        if state.error:
            lines.append(f"- **Error:** {state.error}")
        if state.continuation_of:
            lines.append(f"- **Continuation of:** `{state.continuation_of}` (new publication identity)")
        if state.correction_of:
            lines.append("- **Correction job** (applies an operator instruction)")
        lines.append(f"- **Base revision:** `{state.base_commit or 'n/a'}`")
        lines.append(f"- **Coverage:** {state.effective_coverage}")
        lines.append("")
        if state.changes:
            lines.append("## Pages")
            for change in state.changes:
                link = f"{'created' if change.action == 'create' else 'updated'}"
                lines.append(
                    f"- `{change.path}` — {link} — status: **{change.status}**"
                    + (f" — {change.title}" if change.title else "")
                )
                if change.rationale:
                    lines.append(f"  - purpose: {change.rationale[:250]}")
                if change.error:
                    lines.append(f"  - failure: {change.error[:300]}")
                for note in change.notes[:4]:
                    lines.append(f"  - note: {note[:250]}")
            lines.append("")
        fetched = [s for s in state.sources if s.status == "fetched"]
        failed = [s for s in state.sources if s.status == "failed"]
        skipped = [s for s in state.sources if s.status == "skipped"]
        duplicate = [s for s in state.sources if s.status == "duplicate"]
        lines.append("## Sources")
        lines.append(
            f"- inspected: {len(fetched)}; failed: {len(failed)}; "
            f"skipped: {len(skipped)}; duplicates: {len(duplicate)}; "
            f"discovered: {sum(1 for s in fetched if not s.submitted)}"
        )
        pending = [s for s in state.sources if s.status in ("pending", "scored")]
        rejected = [s for s in state.sources if s.status == "rejected"]
        lines.append(f"- durable discovery: {len(pending)} pending/scored; {len(rejected)} rejected with reasons")
        for source in fetched[:25]:
            origin = (
                f" discovered via {next((p.label for p in state.sources if p.id == source.parent_id), '?')}"
                if source.parent_id
                else ""
            )
            lines.append(
                f"- [{source.role}] {source.label} ({source.doc_kind}){origin}"
                f" — substantive sections: {source.section_count}; review units: {source.review_unit_count}; "
                f"reviewed units: {len(source.reviewed_sections)}; omitted/unverified units: {len(source.omitted_sections)}; "
                f"unknown historical reviews: {len(source.unknown_reviewed_sections)}; obsolete parent markers: {len(source.obsolete_reviewed_sections)}"
            )
        for source in failed[:15]:
            lines.append(f"- FAILED {source.label}: {source.error}")
        for source in (skipped + duplicate)[:15]:
            lines.append(f"- {source.status.upper()} {source.label}: {source.error}")
        lines.append("")
        evidence_count = len(self.store.read_evidence(state.job_id))
        lines.append(f"## Evidence: {evidence_count} retained records")
        if state.conflicts:
            lines.append("")
            lines.append("## Conflicts kept visible")
            for conflict in state.conflicts[:10]:
                lines.append(f"- {conflict}")
        from doclayer.curator.coverage import assess_coverage

        assessment = assess_coverage(state)
        for title, gaps in (
            ("Current coverage gaps", assessment.current_gaps),
            ("Historical diagnostics / exclusions", assessment.historical_gaps),
        ):
            if gaps:
                lines.extend(["", f"## {title}"])
                lines.extend(f"- {gap}" for gap in gaps)
        if state.consolidation_notes:
            lines.append("")
            lines.append("## Consolidation opportunities (recorded, not acted on)")
            for note in state.consolidation_notes:
                lines.append(f"- {note}")
        lines.append("")
        lines.append("## Usage")
        usage = state.usage
        lines.append(
            f"- model calls: {usage.model_calls}"
            + (
                f" (reported tokens: {usage.model_input_tokens} in / {usage.model_output_tokens} out)"
                if usage.model_input_tokens or usage.model_output_tokens
                else ""
            )
        )
        if usage.calls_by_stage:
            unattributed = usage.model_calls - sum(usage.calls_by_stage.values())
            lines.append(
                "- calls by stage: "
                + ", ".join(f"{k} {v}" for k, v in sorted(usage.calls_by_stage.items()))
                + (f" (partial: {unattributed} calls predate per-stage accounting)" if unattributed > 0 else "")
            )
        lines.append(
            f"- sources fetched: {usage.sources_fetched}; bytes: {usage.fetched_bytes}; "
            f"active processing: {usage.active_seconds:.0f}s"
        )
        pub = state.publication
        lines.append("")
        lines.append("## Publication")
        if pub.merged:
            lines.append(f"- **merged** as `{pub.merge_commit}`")
            if pub.pr_url:
                lines.append(f"- PR: {pub.pr_url} (#{pub.pr_number})")
            lines.append("- merge/index/clone propagation tracked separately by vo-rag and consumers")
        elif pub.pr_number:
            lines.append(f"- PR open (NOT merged): {pub.pr_url}")
        else:
            lines.append("- not published")
        lines.append("")
        lines.append(f"_Digest generated {_now_iso()}; drafts and evidence live in the job directory._")
        return "\n".join(lines) + "\n"


class IntakePathError(RuntimeError):
    pass


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).netloc
