"""Curator job state: versioned, durable, serializable.

One import job = one directory with ``state.json`` (atomically replaced),
``summary.md``, drafts, and evidence records. The state carries enough to
resume after a restart, deduplicate submissions, and report partial
publication honestly — no database.

States (job level) follow the design: queued, processing, validating,
publishing, published, covered (already covered), partial, failed, retryable,
cancelled. There is deliberately no accepted/awaiting-human-review state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

STATE_SCHEMA_VERSION = 1

#: Job-level statuses. The first five (queued..publishing) are "in flight".
JOB_STATUSES = (
    "queued",
    "processing",
    "validating",
    "publishing",
    "published",
    "covered",  # already-covered: a successful no-op result
    "partial",  # some changes published, some failed/omitted
    "failed",  # terminal, non-retryable
    "retryable",  # terminal for now; resume may continue it
    "cancelled",
)

#: Per-change statuses.
CHANGE_STATUSES = (
    "pending",
    "drafted",
    "validated",
    "failed",  # failed validation/repair; reported, does not block others
    "conflict",  # could not reconcile with intervening canonical edits
    "published",
    "skipped",
)

#: Pipeline stages, in order. state.stage names the stage to (re)run.
STAGES = ("intake", "discover", "extract", "place", "draft", "validate", "publish", "report")

CONCEPT_ID_RE = re.compile(r"^c-[0-9a-f]{8}$")

SUBJECT_TYPES = (
    "model",
    "model-family",
    "product",
    "offering",
    "account",
    "install",
    "procedure",
    "decision",
    "general",
)

DOC_KINDS = (
    "model-card",
    "system-card",
    "prompting-guide",
    "api-reference",
    "release-notes",
    "research-paper",
    "operator-note",
    "other",
)

SOURCE_ROLES = ("primary", "supplementary", "reference")


class BudgetExceeded(RuntimeError):
    """A configured workload limit stopped the job (recorded, not an error)."""

    def __init__(self, limit: str) -> None:
        super().__init__(f"limit reached: {limit}")
        self.limit = limit


@dataclass
class SourceRecord:
    """One input or discovered source: identity, provenance, and status."""

    id: str
    kind: str  # url | text | markdown | pdf | manifest | directory
    label: str
    role: str = "primary"  # primary | supplementary | reference
    url: str | None = None  # normalized fetch identity (no fragment)
    parent_id: str | None = None  # discovery parent for discovered sources
    relevance: dict[str, Any] = field(default_factory=dict)  # {score, why}
    status: str = "pending"  # pending | fetched | failed | skipped | duplicate
    error: str | None = None
    title: str | None = None
    publisher: str | None = None
    inspected_at: str | None = None
    published_info: str | None = None  # publication/version info when available
    content_hash: str | None = None
    bytes: int = 0
    doc_kind: str = "other"
    extract_path: str | None = None  # cache-relative extracted text (temporary)
    local_path: str | None = None  # submitted file location on the worker host
    section_count: int = 0
    review_unit_count: int = 0
    unknown_reviewed_sections: list[str] = field(default_factory=list)
    obsolete_reviewed_sections: list[str] = field(default_factory=list)
    extraction_complete: bool = False
    reviewed_sections: list[str] = field(default_factory=list)
    omitted_sections: list[str] = field(default_factory=list)
    submitted: bool = True
    exclusions_hit: bool = False
    scope: dict[str, str] = field(default_factory=dict)
    redirect_chain: list[str] = field(default_factory=list)
    omitted_categories: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceRecord:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Evidence:
    """Selected supporting evidence retained beyond the temporary cache."""

    source_id: str
    label: str
    locator: str  # "page 7", "## Section", "https://…#section"
    modality: str  # quote | data | paraphrase
    text: str
    doc_kind: str = "other"
    applicability: dict[str, str] = field(default_factory=dict)
    published_info: str | None = None
    inspected_at: str | None = None
    url: str | None = None
    publisher: str | None = None
    claim_id: str = ""
    role: str = "primary"
    quote: str = ""
    support_status: str = "unknown"
    original_quote: str | None = None  # stable declared quote before verified excerpt hydration

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Evidence:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class PageChange:
    """One intended destination page and its draft."""

    group_id: str
    path: str  # repo-relative POSIX path (wiki/…)
    action: str  # create | update
    title: str = ""
    subject_type: str = "general"
    rationale: str = ""
    status: str = "pending"
    base_commit: str = ""  # revision the draft was made against
    base_blob: str = ""  # blob hash of the existing page at the base ("" = new)
    draft_meta: dict[str, Any] = field(default_factory=dict)
    draft_body: str = ""
    draft_progress: dict[str, Any] = field(default_factory=dict)
    check_progress: dict[str, Any] = field(default_factory=dict)
    reconcile_check_progress: dict[str, Any] = field(default_factory=dict)
    related: list[str] = field(default_factory=list)  # paths to link
    source_ids: list[str] = field(default_factory=list)
    claim_ids: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    validation: list[dict[str, Any]] = field(default_factory=list)
    repair_passes: int = 0
    scope_repair_passes: int = 0  # dedicated passes for citations of excluded sources
    repair_context: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    # Bounded three-way publication work survives a retry independently of
    # editorial task completion and whole-page evidence checking.
    reconcile_progress: dict[str, Any] = field(default_factory=dict)

    # Draft sections publication reconciliation kept exactly as fresh (their
    # contribution dropped), saved with the reconciled body they produced:
    # [{"section", "digest", "claim_ids"}]. A published page reports them as
    # current coverage gaps.
    superseded_by_fresh: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PageChange:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Publication:
    """Durable publication identity; persisted before it is needed."""

    branch: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    merge_commit: str | None = None
    merged: bool = False
    pushed: bool = False
    commit_sha: str | None = None
    committed_paths: list[str] = field(default_factory=list)
    worktree: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Publication:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Usage:
    """Observable usage counters; persisted so budgets survive restarts."""

    model_calls: int = 0
    model_input_tokens: int = 0  # reported by the server when it reports them
    model_output_tokens: int = 0
    fetched_bytes: int = 0
    sources_fetched: int = 0
    active_seconds: float = 0.0
    calls_by_stage: dict[str, int] = field(default_factory=dict)
    #: Seconds model calls spent waiting at the idle gate for the endpoint to
    #: be free of other users (extraction on a shared local model).
    idle_wait_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "calls_by_stage": dict(self.calls_by_stage)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Usage:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class JobState:
    """The whole durable job record."""

    job_id: str
    idempotency_key: str
    intent: str  # topic / task instruction from the submitter
    status: str = "queued"
    stage: str = "intake"  # next stage to run
    stages_done: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    submitted_by: str = "agent"
    exclusions: list[str] = field(default_factory=list)
    source_policy: dict[str, Any] = field(default_factory=dict)
    continuation_of: str | None = None
    inherited_source_ids: list[str] = field(default_factory=list)
    evidence_selection: dict[str, Any] = field(default_factory=dict)
    evidence_verification: dict[str, Any] = field(default_factory=dict)
    inference_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    sources: list[SourceRecord] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    changes: list[PageChange] = field(default_factory=list)
    coverage: str = "complete"  # complete | incomplete
    coverage_basis: str | None = None  # original explicit/legacy flag, not derived status
    coverage_gaps: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    consolidation_notes: list[str] = field(default_factory=list)
    publication: Publication = field(default_factory=Publication)
    usage: Usage = field(default_factory=Usage)
    limits: dict[str, Any] = field(default_factory=dict)
    base_commit: str = ""  # canonical revision curation drafted against
    input_digest: str = ""  # hash of submitted input identities+content
    cancel_requested: bool = False
    correction_of: str | None = None  # job_id this job corrects, if any
    mode: str = "multi-stage"  # multi-stage | single-pass; legacy jobs are multi-stage
    private: bool = False
    error: str | None = None
    summary: str = ""

    # -- serialization -------------------------------------------------- #

    @property
    def effective_coverage(self) -> str:
        """Current review coverage; recovered diagnostics remain historical."""
        from doclayer.curator.coverage import assess_coverage

        return assess_coverage(self).coverage

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "job_id": self.job_id,
            "idempotency_key": self.idempotency_key,
            "intent": self.intent,
            "status": self.status,
            "stage": self.stage,
            "stages_done": list(self.stages_done),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "submitted_by": self.submitted_by,
            "exclusions": list(self.exclusions),
            "source_policy": self.source_policy,
            "continuation_of": self.continuation_of,
            "inherited_source_ids": self.inherited_source_ids,
            "evidence_selection": self.evidence_selection,
            "evidence_verification": self.evidence_verification,
            "inference_diagnostics": self.inference_diagnostics,
            "sources": [s.to_dict() for s in self.sources],
            "evidence": [e.to_dict() for e in self.evidence],
            "changes": [c.to_dict() for c in self.changes],
            "coverage": self.coverage,
            "coverage_basis": self.coverage_basis,
            "coverage_gaps": list(self.coverage_gaps),
            "conflicts": list(self.conflicts),
            "consolidation_notes": list(self.consolidation_notes),
            "publication": self.publication.to_dict(),
            "usage": self.usage.to_dict(),
            "limits": dict(self.limits),
            "base_commit": self.base_commit,
            "input_digest": self.input_digest,
            "cancel_requested": self.cancel_requested,
            "correction_of": self.correction_of,
            "mode": self.mode,
            "private": self.private,
            "error": self.error,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobState:
        version = data.get("schema_version")
        if version != STATE_SCHEMA_VERSION:
            raise ValueError(f"unsupported state schema_version: {version!r}")
        return cls(
            job_id=data["job_id"],
            idempotency_key=data.get("idempotency_key", ""),
            intent=data.get("intent", ""),
            status=data.get("status", "queued"),
            stage=data.get("stage", "intake"),
            stages_done=list(data.get("stages_done", [])),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            submitted_by=data.get("submitted_by", "agent"),
            exclusions=list(data.get("exclusions", [])),
            source_policy=dict(data.get("source_policy", {})),
            continuation_of=data.get("continuation_of"),
            inherited_source_ids=list(data.get("inherited_source_ids", [])),
            evidence_selection=dict(data.get("evidence_selection", {})),
            evidence_verification=dict(data.get("evidence_verification", {})),
            inference_diagnostics=list(data.get("inference_diagnostics", [])),
            sources=[SourceRecord.from_dict(s) for s in data.get("sources", [])],
            evidence=[Evidence.from_dict(e) for e in data.get("evidence", [])],
            changes=[PageChange.from_dict(c) for c in data.get("changes", [])],
            coverage=data.get("coverage", "complete"),
            coverage_basis=data.get("coverage_basis"),
            coverage_gaps=list(data.get("coverage_gaps", [])),
            conflicts=list(data.get("conflicts", [])),
            consolidation_notes=list(data.get("consolidation_notes", [])),
            publication=Publication.from_dict(data.get("publication", {})),
            usage=Usage.from_dict(data.get("usage", {})),
            limits=dict(data.get("limits", {})),
            base_commit=data.get("base_commit", ""),
            input_digest=data.get("input_digest", ""),
            cancel_requested=bool(data.get("cancel_requested", False)),
            correction_of=data.get("correction_of"),
            mode=data.get("mode", "multi-stage"),
            private=bool(data.get("private", False)),
            error=data.get("error"),
            summary=data.get("summary", ""),
        )

    # -- derived -------------------------------------------------------- #

    @property
    def in_flight(self) -> bool:
        return self.status in ("queued", "processing", "validating", "publishing")

    @property
    def finished(self) -> bool:
        return self.status in ("published", "covered", "partial", "failed", "cancelled")

    def pending_changes(self) -> list[PageChange]:
        return [c for c in self.changes if c.status in ("pending", "drafted", "validated", "conflict")]

    def outcome_line(self) -> str:
        published = sum(1 for c in self.changes if c.status == "published")
        failed = sum(1 for c in self.changes if c.status in ("failed", "conflict"))
        return (
            f"status={self.status} stage={self.stage} "
            f"changes: {published} published, {failed} failed, "
            f"{len(self.changes)} planned; coverage={self.coverage}"
        )
