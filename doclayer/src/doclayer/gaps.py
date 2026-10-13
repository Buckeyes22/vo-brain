"""Durable knowledge-gap ledger: one record per unresolved documentation need.

Filesystem-backed records with an append-only bounded event trail, using the
curator's proven atomic-write/lock patterns. The ledger captures:

- hook candidates (a clear documentation request naming an uncovered subject),
- the resolver's actual unsuccessful/partial outcomes,
- agent-facing ``record_gap`` submissions and structured source observations.

Records are deduplicated by subject, facet/applicability and need class; event
replays (unique event IDs) never double-count recurrence. Leases and atomic
updates make worker restarts safe. Corrupt records are quarantined visibly
while foreground answering continues. A capped local spool holds dispatch
payloads that still have to reach the curator queue on the owning gap-ledger host, with a
visible capture-degraded status instead of silent loss.

Need classes: missing_coverage, enrichment, retrieval_defect,
infrastructure, needs_local_evidence, ambiguous.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import MISSING, dataclass, field
from pathlib import Path
from typing import Any

from doclayer.curator.jobstore import (
    FileLock,
    atomic_write_json,
    atomic_write_text,
    now_iso,
)

SCHEMA_VERSION = 2
_HELD_LOCKS = threading.local()

#: Forward states and the side states a record can hold.
ACTIVE_STATES = (
    "candidate",
    "researching",
    "ready_for_curation",
    "queued",
    "curating",
    "awaiting_index",
    "verifying",
)
TERMINAL_OK = "resolved"
SIDE_STATES = (
    "retry_wait",
    "needs_local_evidence",
    "retrieval_defect",
    "awaiting_curator_readiness",
    "suppressed",
    "failed",
    "superseded",
)
ALL_STATES = ACTIVE_STATES + SIDE_STATES + (TERMINAL_OK,)
#: Statuses no verification outcome may overwrite.
CLOSED_STATES = (TERMINAL_OK, "suppressed", "superseded")

NEED_CLASSES = (
    "missing_coverage",
    "enrichment",
    "retrieval_defect",
    "infrastructure",
    "needs_local_evidence",
    "ambiguous",
)

#: Verbose per-attempt detail in resolved records is pruned after this many
#: days; identity, outcome and links are always retained.
RESOLVED_DETAIL_TTL_DAYS = 30

SPOOL_CAP_BYTES = 64 * 1024 * 1024


def explicitly_attached_source(source: dict[str, Any]) -> bool:
    """Only owning attachment provenance grants priority, never URL/authority."""
    return source.get("attachment") == "task-documentation-v1" or source.get("origin") == "task-documentation"


def priority_source_candidates(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable attachment order followed by all other observed candidates."""
    return [s for s in sources if explicitly_attached_source(s)] + [
        s for s in sources if not explicitly_attached_source(s)
    ]


#: One file per active curator submission; the pre-slot singleton file is migrated into it.
ACTIVE_SLOTS_DIR = "active-submissions"
LEGACY_ACTIVE_FILE = "active-submission.json"


def active_slot_paths(root: Path) -> list[Path]:
    """Every active-submission file under a ledger root: slot files, then an unmigrated legacy file."""
    slots = root / ACTIVE_SLOTS_DIR
    paths = sorted(slots.glob("*.json")) if slots.is_dir() else []
    legacy = root / LEGACY_ACTIVE_FILE
    if legacy.exists():
        paths.append(legacy)
    return paths


class GapError(ValueError):
    pass


#: Suppression reason for a captured operator chat remark (not a question).
NOT_A_DOCUMENTATION_NEED = "not a documentation need"

# An instruction or complaint addressed to the agent, not a documentation
# question. Real needs are questions or subject titles ("What ...", "How ...",
# "Langfuse ClickHouse storage ..."); remarks open by addressing the agent.
_CHAT_REMARK_OPENERS = re.compile(
    r"^(?:i\s+(?:need|want)\s+you\b|you\s|(?:can|could|would|will)\s+you\b|please\b|"
    r"make\s+sure\b|ok(?:ay)?\b|so\s|god\s+forbid\b|let'?s\b|go\s+ahead\b|"
    r"thanks?\b|thank\s+you\b|why\s+(?:did|didn'?t|are|can'?t|cant|won'?t)\s+you\b|"
    r"then\s+we\s+need\b|now\s+(?:do|go|run|fix|make)\b)",
    re.I,
)
_CHAT_REMARK_MARKERS = re.compile(
    r"\b(?:sonnet|opus|haiku|luna|codex)\s+subagents?\b|\bsubagents?\s+(?:to\s+)?(?:look|identify|find|check)\b|"
    r"\byou\s+(?:just|can'?t|cant|should(?:n'?t)?|need\s+to|have\s+to)\b",
    re.I,
)


def is_chat_remark(question: str | None) -> bool:
    """True when a captured "question" is an operator chat remark to the agent.

    Such text cannot be answered by documentation; retrying it daily forever
    only spends discovery work. Questions about a subject never match.
    """
    text = " ".join((question or "").split())
    if not text:
        return False
    return bool(_CHAT_REMARK_OPENERS.match(text) or _CHAT_REMARK_MARKERS.search(text))


def normalize_subject_identity(subject: str | None) -> str:
    """Conservative normalized identity. Unknown subjects keep an explicit
    ``unknown:`` prefix until they are safely linked to a canonical slug."""
    text = re.sub(r"\s+", " ", (subject or "").strip().lower())
    text = re.sub(r"[-_]+", " ", text)
    if not text:
        return "unknown:unidentified"
    return text


def canonical_identity(subject: str | None, catalog_subject: bool) -> str:
    text = normalize_subject_identity(subject)
    if catalog_subject:
        return text
    return f"unknown:{text}" if not text.startswith("unknown:") else text


def gap_id_for(
    identity: str,
    need_class: str,
    facets: list[str],
    applicability: list[dict[str, str]] | None = None,
    question: str = "",
) -> str:
    """Stable deduplicated identity: subject + need class + facets."""
    material = "\x00".join(
        [
            identity,
            need_class,
            ",".join(sorted(facets)),
            json.dumps(
                sorted(applicability or [], key=lambda x: json.dumps(x, sort_keys=True)),
                sort_keys=True,
            ),
            normalize_subject_identity(question),
        ]
    )
    return "gap-" + hashlib.sha256(material.encode()).hexdigest()[:20]


def event_id_for(*parts: str) -> str:
    material = "\x00".join(parts)
    return "evt-" + hashlib.sha256(material.encode()).hexdigest()[:20]


@dataclass
class GapRecord:
    gap_id: str
    identity: str
    canonical_subject: str | None
    need_class: str
    status: str
    representative_question: str
    facets: list[str] = field(default_factory=list)
    applicability: list[dict[str, str]] = field(default_factory=list)
    checked_sources: list[dict[str, str]] = field(default_factory=list)
    source_candidates: list[dict[str, str]] = field(default_factory=list)
    job_links: list[dict[str, str]] = field(default_factory=list)
    dispositions: list[str] = field(default_factory=list)
    coverage_revision: str | None = None
    evidence_generation: str | None = None
    publication_revision: str | None = None
    index_revision: str | None = None
    resolution_verification: dict[str, Any] = field(default_factory=dict)
    recurrence: int = 1
    first_seen: str = field(default_factory=now_iso)
    last_seen: str = field(default_factory=now_iso)
    attempts: int = 0
    backoff_until: str | None = None
    lease: dict[str, Any] | None = None
    suppressed_reason: str | None = None
    failure_reason: str | None = None
    updated_at: str = field(default_factory=now_iso)
    revision: int = 0
    research_attempts: int = 0
    private_attempts: int = 0
    #: Earlier private-evidence plans and their refusals (newest last), so a
    #: retry is planned differently instead of repeating the same commands.
    private_plans: list[dict[str, Any]] = field(default_factory=list)
    #: Consecutive "existing canonical answer proof pending" waits (backoff grows).
    proof_wait_attempts: int = 0
    #: When a worker last claimed this need; research runs least-recently-claimed first.
    last_claimed_at: str | None = None
    submission: dict[str, Any] = field(default_factory=dict)
    submission_history: list[dict[str, Any]] = field(default_factory=list)
    resolution_history: list[dict[str, Any]] = field(default_factory=list)
    seen_events: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    supersession: dict[str, Any] = field(default_factory=dict)
    related_records: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GapRecord:
        known = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in known}
        for name in known - set(filtered):
            definition = cls.__dataclass_fields__[name]
            if definition.default_factory is not MISSING:
                filtered[name] = definition.default_factory()
            elif definition.default is not MISSING:
                filtered[name] = definition.default
            else:
                raise GapError(f"missing required field: {name}")
        record = cls(**filtered)
        for name in (
            "attempts",
            "revision",
            "research_attempts",
            "private_attempts",
            "proof_wait_attempts",
            "recurrence",
        ):
            if type(getattr(record, name)) is not int or getattr(record, name) < 0:
                raise GapError(f"invalid counter: {name}")
        if (
            not isinstance(record.submission, dict)
            or not isinstance(record.seen_events, list)
            or not all(isinstance(e, str) for e in record.seen_events)
        ):
            raise GapError("invalid durable attempt/event state")
        if (
            not isinstance(record.supersession, dict)
            or not isinstance(record.related_records, list)
            or not all(isinstance(r, dict) for r in record.related_records)
            or (
                record.superseded_by is not None
                and (
                    not isinstance(record.superseded_by, str)
                    or not re.fullmatch(r"gap-[a-f0-9]{20}", record.superseded_by)
                )
            )
            or (record.status == "superseded" and (not record.superseded_by or record.superseded_by == record.gap_id))
        ):
            raise GapError("invalid durable supersession state")
        if record.status not in ALL_STATES:
            raise GapError(f"unknown gap status: {record.status}")
        if record.need_class not in NEED_CLASSES:
            raise GapError(f"unknown need class: {record.need_class}")
        return record


class GapLedger:
    """The durable ledger at one state root."""

    def __init__(self, state_dir: Path | str) -> None:
        self.root = Path(state_dir)
        self.gaps_dir = self.root / "gaps"
        self.spool_dir = self.root / "spool"
        self.quarantine_dir = self.root / "quarantine"
        self.locks_dir = self.root / "locks"

    def assert_owner_writable(self) -> None:
        """A retired owner remains readable, but can never become a second writer."""
        marker = self.root / "owner-migrated.json"
        if marker.exists() or marker.is_symlink():
            raise GapError(
                "knowledge-gap owner moved; this ledger is read-only; "
                "use the configured GapClient and consult " + str(marker)
            )

    # -- core record access ------------------------------------------------ #

    def record_path(self, gap_id: str) -> Path:
        if not isinstance(gap_id, str) or not re.fullmatch(r"gap-[a-f0-9]{20}", gap_id):
            raise GapError("invalid gap identity")
        return self.gaps_dir / gap_id / "record.json"

    def events_path(self, gap_id: str) -> Path:
        return self.gaps_dir / gap_id / "events.jsonl"

    @contextmanager
    def lock(self, gap_id: str):
        self.assert_owner_writable()
        if not isinstance(gap_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", gap_id):
            raise GapError("invalid ledger lock identity")
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        key = str((self.locks_dir / f"{gap_id}.lock").resolve())
        held = getattr(_HELD_LOCKS, "paths", {})
        _HELD_LOCKS.paths = held
        if key in held:
            yield
            return
        with FileLock(Path(key)):
            held[key] = True
            try:
                yield
            finally:
                del held[key]

    def load(self, gap_id: str) -> GapRecord:
        path = self.record_path(gap_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return GapRecord.from_dict(data)
        except FileNotFoundError:
            raise
        except (OSError, ValueError, TypeError, GapError) as exc:
            if not (self.root / "owner-migrated.json").exists():
                self.quarantine(gap_id, str(exc))
            raise GapError(f"gap record quarantined: {gap_id}: {exc}") from exc

    def save(self, record: GapRecord) -> None:
        """Compare-and-save under the capture lock; stale workers cannot erase events."""
        self.assert_owner_writable()
        with self.lock(record.gap_id):
            path = self.record_path(record.gap_id)
            if not path.exists() and record.revision != 0:
                raise GapError("gap removed or quarantined; reload before updating")
            if path.exists():
                current = json.loads(path.read_text())
                if current.get("revision", 0) != record.revision:
                    raise GapError("gap changed concurrently; reload before updating")
            record.updated_at = now_iso()
            record.revision += 1
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(path, record.to_dict())

    def update(self, gap_id: str, operation) -> GapRecord:
        self.assert_owner_writable()
        with self.lock(gap_id):
            record = self.load(gap_id)
            operation(record)
            self.save(record)
            return record

    def quarantine(self, gap_id: str, reason: str) -> None:
        """Move corrupt data somewhere visible; foreground answering continues."""
        self.assert_owner_writable()
        try:
            src = self.record_path(gap_id)
            if not src.is_file():
                return
            self.quarantine_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            atomic_write_text(
                self.quarantine_dir / f"{gap_id}-{stamp}.json",
                json.dumps(
                    {
                        "gap_id": gap_id,
                        "reason": reason[:500],
                        "record": src.read_text(encoding="utf-8")[:20000],
                    }
                ),
            )
            src.unlink()
        except OSError:
            pass

    def quarantine_entries(self) -> list[dict[str, str]]:
        out = []
        try:
            for path in sorted(self.quarantine_dir.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                out.append(
                    {
                        "file": path.name,
                        "gap_id": data.get("gap_id", "?"),
                        "reason": data.get("reason", "")[:200],
                    }
                )
        except OSError:
            pass
        return out

    def list_ids(self) -> list[str]:
        try:
            return sorted(p.parent.name for p in self.gaps_dir.glob("*/record.json"))
        except OSError:
            return []

    def list_records(self) -> list[GapRecord]:
        records = []
        for gap_id in self.list_ids():
            try:
                records.append(self.load(gap_id))
            except (GapError, FileNotFoundError):
                continue
        return records

    # -- capture and dedup -------------------------------------------------- #

    def submit_event(self, **event) -> tuple[GapRecord, bool]:
        """A client event ID identifies immutable bytes across ALL gap identities."""
        self.assert_owner_writable()
        event = dict(event)
        if event.get("event_key") is None:
            event["event_key"] = "evt-" + uuid.uuid4().hex
        event_id = event["event_key"]
        if not isinstance(event_id, str) or not event_id or len(event_id) > 200:
            raise GapError("event key must be a nonempty bounded client identity")
        fingerprint = hashlib.sha256(event_id.encode()).hexdigest()
        binding = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
        path = self.root / "event-receipts" / f"{fingerprint}.json"
        with self.lock(f"event-{fingerprint}"):
            if path.exists():
                try:
                    receipt = json.loads(path.read_text())
                    if receipt["binding"] != binding:
                        raise GapError("event ID reused with different payload; original event retained")
                    return self.load(receipt["gap_id"]), False
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    if isinstance(exc, GapError):
                        raise
                    raise GapError("event replay receipt corrupt; capture withheld") from exc
            record, created = self._submit_event(**event)
            atomic_write_json(path, {"binding": binding, "gap_id": record.gap_id})
            return record, created

    def _submit_event(self, **event) -> tuple[GapRecord, bool]:
        identity = event.get("identity_override") or canonical_identity(
            event.get("subject"), event.get("catalog_subject", False)
        )
        # Serialize canonical/legacy-alias captures of the same exact stored need.
        logical = gap_id_for(
            normalize_subject_identity(identity.removeprefix("unknown:")),
            event.get("need_class", ""),
            event.get("facets") or [],
            event.get("applicability"),
            event.get("representative_question", ""),
        )
        with self.lock(f"capture-{logical}"):
            return self._capture_event(**event)

    def _capture_event(
        self,
        *,
        need_class: str,
        subject: str | None,
        representative_question: str,
        facets: list[str] | None = None,
        applicability: list[dict[str, str]] | None = None,
        checked_sources: list[dict[str, str]] | None = None,
        source_candidates: list[dict[str, str]] | None = None,
        dispositions: list[str] | None = None,
        coverage_revision: str | None = None,
        evidence_generation: str | None = None,
        generation_attested: bool = False,
        event_key: str | None = None,
        detail: str | None = None,
        identity_override: str | None = None,
        catalog_subject: bool = False,
    ) -> tuple[GapRecord, bool]:
        """Record one capture event; dedup into the matching gap record.

        Returns (record, created). ``event_key`` makes replay safe: the same
        key (unique per client event) never increments recurrence twice.
        """
        self.assert_owner_writable()
        if type(generation_attested) is not bool:
            raise GapError("generation attestation must be an explicit boolean")
        if need_class not in NEED_CLASSES:
            raise GapError(f"unknown need class: {need_class}")
        if (
            not isinstance(representative_question, str)
            or not representative_question.strip()
            or len(representative_question) > 2000
        ):
            raise GapError("representative question must be complete, nonempty and at most 2000 characters")
        if event_key is not None and (not isinstance(event_key, str) or not event_key or len(event_key) > 200):
            raise GapError("event key must be a nonempty bounded client identity")
        identity = identity_override or canonical_identity(subject, catalog_subject=catalog_subject)
        facets = [str(f) for f in (facets or [])]
        gap_id = gap_id_for(identity, need_class, facets, applicability, representative_question)
        event_id = event_key or "evt-" + uuid.uuid4().hex
        if not self.record_path(gap_id).exists():
            # v1 IDs omitted the question/applicability. Reuse ONLY a uniquely
            # matching complete recorded need, keeping its ID, jobs and history.
            # Missing historical second questions cannot be reconstructed.
            matches = [
                r
                for r in self.list_records()
                if normalize_subject_identity((r.canonical_subject or r.identity).removeprefix("unknown:"))
                == normalize_subject_identity(identity.removeprefix("unknown:"))
                and gap_id_for(
                    "same",
                    r.need_class,
                    r.facets,
                    r.applicability,
                    r.representative_question,
                )
                == gap_id_for("same", need_class, facets, applicability, representative_question)
            ]
            if len(matches) == 1:
                gap_id = matches[0].gap_id
        with self.lock(gap_id):
            try:
                record = self.load(gap_id)
                created = False
            except FileNotFoundError:
                record = GapRecord(
                    gap_id=gap_id,
                    identity=identity,
                    canonical_subject=subject if not identity.startswith("unknown:") else None,
                    need_class=need_class,
                    status="candidate",
                    representative_question=representative_question,
                    facets=facets,
                    applicability=list(applicability or []),
                    checked_sources=list(checked_sources or []),
                    source_candidates=list(source_candidates or []),
                    dispositions=list(dispositions or []),
                    coverage_revision=coverage_revision,
                    evidence_generation=evidence_generation,
                )
                created = True
            except GapError:
                raise
            replay = (
                (self.record_path(gap_id).parent / "receipts" / hashlib.sha256(event_id.encode()).hexdigest()).exists()
                or event_id in record.seen_events
                or self._seen_event(gap_id, event_id)
            )
            if not replay:
                attested = (
                    generation_attested
                    and isinstance(evidence_generation, str)
                    and re.fullmatch(r"[a-f0-9]{64}", evidence_generation) is not None
                    and isinstance(coverage_revision, str)
                    and bool(coverage_revision.strip())
                )
                unresolved = bool(
                    set(dispositions or [])
                    & {
                        "not_found_in_checked_sources",
                        "partial",
                        "retrieval_defect_suspected",
                        "retrieval_suspected",
                    }
                ) and "answered" not in (dispositions or [])
                if (
                    record.status == "resolved"
                    and attested
                    and unresolved
                    and isinstance(record.evidence_generation, str)
                    and re.fullmatch(r"[a-f0-9]{64}", record.evidence_generation) is not None
                    and evidence_generation != record.evidence_generation
                ):
                    record.resolution_history.append(
                        {
                            "at": now_iso(),
                            "event_id": event_id,
                            "evidence_generation": record.evidence_generation,
                            "resolution_verification": json.loads(json.dumps(record.resolution_verification)),
                            "index_revision": record.index_revision,
                            "publication_revision": record.publication_revision,
                            "reason": "fresh unresolved observation of a changed attested generation",
                        }
                    )
                    record.status = "candidate"
                    record.backoff_until = None
                if catalog_subject and subject and not identity.startswith("unknown:"):
                    record.canonical_subject = subject
                    record.identity = identity
                if not created:
                    record.recurrence += 1
                record.seen_events.append(event_id)
                for name, incoming in (
                    ("checked_sources", checked_sources),
                    ("source_candidates", source_candidates),
                    ("dispositions", dispositions),
                ):
                    existing = getattr(record, name)
                    for item in incoming or []:
                        if item not in existing:
                            existing.append(item)
                if coverage_revision is not None:
                    record.coverage_revision = coverage_revision
                if evidence_generation is not None and (record.status != "resolved" or attested):
                    record.evidence_generation = evidence_generation
            record.last_seen = now_iso()
            if detail:
                capture_event = {
                    "event_id": event_id,
                    "at": now_iso(),
                    "kind": "observation",
                    "detail": str(detail)[:2000],
                    "replay": replay,
                }
            else:
                capture_event = {
                    "event_id": event_id,
                    "at": now_iso(),
                    "kind": "capture",
                    "replay": replay,
                }
            if created:
                record.first_seen = record.last_seen
            self.save(record)
            self.append_event(gap_id, capture_event)
        return record, created and not replay

    def _seen_event(self, gap_id: str, event_id: str) -> bool:
        try:
            with self.events_path(gap_id).open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        if json.loads(line).get("event_id") == event_id:
                            return True
                    except ValueError:
                        continue
        except OSError:
            pass
        return False

    # -- event trail --------------------------------------------------------- #

    def append_event(self, gap_id: str, event: dict[str, Any]) -> None:
        self.assert_owner_writable()
        with self.lock(gap_id):
            path = self.events_path(gap_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(event, sort_keys=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._bound_events(path)

    def _bound_events(self, path: Path, max_lines: int = 500) -> None:
        try:
            with path.open(encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            return
        if len(lines) > max_lines:
            atomic_write_text(path, "".join(lines[-max_lines:]))

    def prune_resolved_detail(self) -> int:
        """Prune only unchanged resolved detail, without forgetting replay IDs."""
        self.assert_owner_writable()
        from datetime import UTC, datetime

        pruned = 0
        cutoff = time.time() - RESOLVED_DETAIL_TTL_DAYS * 86400
        for gap_id in self.list_ids():
            with self.lock(gap_id):
                try:
                    record = self.load(gap_id)
                    if (
                        record.status != TERMINAL_OK
                        or datetime.fromisoformat(record.updated_at).replace(tzinfo=UTC).timestamp() >= cutoff
                    ):
                        continue
                    path = self.events_path(gap_id)
                    lines = path.read_text().splitlines()
                except (OSError, ValueError, GapError):
                    continue
                kept = []
                for line in lines:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("kind") != "observation" or not event.get("detail"):
                        kept.append(line)
                if len(kept) != len(lines):
                    atomic_write_text(path, "".join(line + "\n" for line in kept))
                    pruned += 1
        return pruned

    def reconcile_duplicates(self) -> dict[str, Any]:
        """Group explicit pending needs, preserving every original record.

        Full facet/scope equality is mandatory. Distinct questions are related
        only when they declare the same nonempty facets and named subject;
        unidentified subjects need the same complete question. No submitted,
        leased, resolved or independently verifying work is redirected.
        """

        def text(value):
            return " ".join(str(value).split())

        def subject(record):
            return normalize_subject_identity((record.canonical_subject or record.identity).removeprefix("unknown:"))

        def facets(record):
            return sorted({text(f) for f in record.facets if text(f)})

        def scope(record):
            return sorted(json.dumps(x, sort_keys=True) for x in record.applicability)

        def equivalent(a, b):
            same_question = text(a.representative_question) == text(b.representative_question)
            # The same question captured once without facets (resolver) and once
            # with them (an agent's record_gap) is one need; the facets merge.
            unfaceted = same_question and not (facets(a) and facets(b))
            if (facets(a) != facets(b) and not unfaceted) or scope(a) != scope(b):
                return False
            sa, sb = subject(a), subject(b)
            if sa != sb:
                return False
            if sa == "unidentified":
                return same_question
            return same_question or bool(facets(a))

        allowed = {
            "candidate",
            "ready_for_curation",
            "retry_wait",
            "needs_local_evidence",
            "retrieval_defect",
        }
        result = {"grouped": [], "skipped": [], "verification_required": []}
        with self.lock("dispatcher"):
            # Claim paths use this same owner lock; captures use one record lock
            # and cannot race any of the fresh decisions below.
            ids = self.list_ids()
            with ExitStack() as stack:
                for gap_id in ids:
                    stack.enter_context(self.lock(gap_id))
                records = self.list_records()
                active_ids: set[str] = set()
                try:
                    for active_path in active_slot_paths(self.root):
                        active_id = json.loads(active_path.read_text()).get("gap_id")
                        if not isinstance(active_id, str) or not re.fullmatch(r"gap-[a-f0-9]{20}", active_id):
                            raise ValueError("active identity unavailable")
                        active_ids.add(active_id)
                except (OSError, ValueError, AttributeError):
                    return {
                        "grouped": [],
                        "skipped": ["active submission unavailable"],
                        "verification_required": [],
                    }

                def eligible(r):
                    return (
                        r.status in allowed
                        and r.need_class != "ambiguous"
                        and not r.submission
                        and not r.job_links
                        and not r.lease
                        and r.gap_id not in active_ids
                    )

                pending = [r for r in records if eligible(r)]
                # Local constraints must not become a public import through dedup.
                priorities = {
                    "needs_local_evidence": 0,
                    "infrastructure": 1,
                    "retrieval_defect": 2,
                    "ambiguous": 3,
                    "missing_coverage": 4,
                    "enrichment": 5,
                }
                pending.sort(
                    key=lambda r: (
                        priorities[r.need_class],
                        r.canonical_subject is None,
                        r.first_seen,
                        r.gap_id,
                    )
                )
                primaries = []
                for duplicate in pending:
                    primary = next((r for r in primaries if equivalent(r, duplicate)), None)
                    if primary is None:
                        primaries.append(duplicate)
                        continue
                    history = duplicate.supersession.get("history", [])
                    if duplicate.supersession:
                        history = history + [{k: v for k, v in duplicate.supersession.items() if k != "history"}]
                    duplicate.supersession = {
                        "method": 1,
                        "at": now_iso(),
                        "original_status": duplicate.status,
                        "history": history,
                        "reason": "same explicit subject, facets and applicability",
                    }
                    duplicate.superseded_by = primary.gap_id
                    duplicate.status = "superseded"
                    # Save the link first: a crash before observation copying is
                    # recoverable from this untouched source record next cycle.
                    self.save(duplicate)
                    if not primary.facets and duplicate.facets:
                        # Adopt at once, so a differently faceted capture of the
                        # same question stays a separate need.
                        primary.facets = list(duplicate.facets)
                        self.save(primary)
                    result["grouped"].append({"gap_id": duplicate.gap_id, "primary": primary.gap_id})
                by_id = {r.gap_id: r for r in records}
                # Recover partial linking and propagate later captured observations
                # without recounting recurrence or erasing either event trail.
                for duplicate in records:
                    if duplicate.status != "superseded":
                        continue
                    primary = by_id.get(duplicate.superseded_by)
                    seen = {duplicate.gap_id}
                    while primary is not None and primary.status == "superseded" and primary.gap_id not in seen:
                        seen.add(primary.gap_id)
                        primary = by_id.get(primary.superseded_by)
                    if primary is None or primary.gap_id in seen or not equivalent(primary, duplicate):
                        result["skipped"].append(duplicate.gap_id)
                        continue
                    if primary.status == "resolved":
                        # Shared need structure is not a new semantic verdict for
                        # this record; verify its original question independently.
                        duplicate.status = "verifying"
                        duplicate.supersession["verification_required"] = True
                        self.save(duplicate)
                        result["verification_required"].append(duplicate.gap_id)
                        continue
                    if duplicate.superseded_by != primary.gap_id:
                        duplicate.supersession.setdefault("prior_links", []).append(duplicate.superseded_by)
                        duplicate.superseded_by = primary.gap_id
                        self.save(duplicate)
                    if not eligible(primary):
                        continue
                    changed = False
                    link = {
                        "gap_id": duplicate.gap_id,
                        "question": duplicate.representative_question,
                        "need_class": duplicate.need_class,
                        "identity": duplicate.identity,
                        "facets": duplicate.facets,
                        "applicability": duplicate.applicability,
                    }
                    if link not in primary.related_records:
                        primary.related_records.append(link)
                        changed = True
                    for name in (
                        "source_candidates",
                        "checked_sources",
                        "dispositions",
                        "facets",
                    ):
                        dest = getattr(primary, name)
                        for item in getattr(duplicate, name):
                            if item not in dest:
                                dest.append(item)
                                changed = True
                    if not primary.canonical_subject and duplicate.canonical_subject:
                        primary.supersession.setdefault("identity_history", []).append(
                            {
                                "identity": primary.identity,
                                "source_record": duplicate.gap_id,
                            }
                        )
                        primary.identity = duplicate.identity
                        primary.canonical_subject = duplicate.canonical_subject
                        changed = True
                    if changed:
                        self.save(primary)
        return result

    # -- status transitions -------------------------------------------------- #

    def transition(self, gap_id: str, status: str, *, reason: str | None = None, **updates: Any) -> GapRecord:
        self.assert_owner_writable()
        if status not in ALL_STATES:
            raise GapError(f"unknown status: {status}")
        with self.lock(gap_id):
            record = self.load(gap_id)
            record.status = status
            if status == "suppressed":
                record.suppressed_reason = reason
            if status == "failed":
                record.failure_reason = reason
            for key, value in updates.items():
                if hasattr(record, key):
                    setattr(record, key, value)
            self.save(record)
            self.append_event(
                gap_id,
                {
                    "at": now_iso(),
                    "kind": "transition",
                    "status": status,
                    "reason": (reason or "")[:500],
                },
            )
        return record

    def transition_open(
        self, gap_id: str, status: str, *, reason: str | None = None, **updates: Any
    ) -> GapRecord | None:
        """transition(), unless the gap was superseded, suppressed or resolved meanwhile.

        Writers that record a verification outcome after a long model call
        reload the record under the gap lock; a closed gap is never reopened.
        """
        with self.lock(gap_id):
            if self.load(gap_id).status in CLOSED_STATES:
                return None
            return self.transition(gap_id, status, reason=reason, **updates)

    def update_open(self, gap_id: str, operation) -> GapRecord | None:
        """update(), unless the gap was superseded, suppressed or resolved meanwhile."""
        with self.lock(gap_id):
            if self.load(gap_id).status in CLOSED_STATES:
                return None
            return self.update(gap_id, operation)

    # -- leases (worker claims) ---------------------------------------------- #

    def claim(self, gap_id: str, worker: str, ttl_s: float = 900.0) -> GapRecord | None:
        """Claim a record for background work; expired leases are reclaimable."""
        self.assert_owner_writable()
        with self.lock(gap_id):
            try:
                record = self.load(gap_id)
            except FileNotFoundError:
                return None
            lease = record.lease or {}
            if lease and lease.get("expires_at", "") > now_iso() and lease.get("worker") != worker:
                return None
            acquired = now_iso()
            record.lease = {
                "worker": worker,
                "acquired_at": acquired,
                "expires_at": _iso_plus(ttl_s),
            }
            record.last_claimed_at = acquired
            self.save(record)
            return record

    def release_lease(self, gap_id: str, worker: str) -> None:
        self.assert_owner_writable()
        with self.lock(gap_id):
            try:
                record = self.load(gap_id)
            except FileNotFoundError:
                return
            if record.lease and record.lease.get("worker") == worker:
                record.lease = None
                self.save(record)

    # -- retry backoff -------------------------------------------------------- #

    BACKOFF_STEPS_S = (300, 1800, 7200)
    #: The longest any research retry waits: once per day, never longer.
    BACKOFF_CAP_S = 86400.0

    def backoff_state(self, attempts: int) -> float:
        """Seconds to wait before attempt N+1; afterwards at most once per day."""
        if attempts <= 0:
            return 0.0
        if attempts <= len(self.BACKOFF_STEPS_S):
            return float(min(self.BACKOFF_STEPS_S[attempts - 1], self.BACKOFF_CAP_S))
        return self.BACKOFF_CAP_S

    # -- spool (durable dispatch payloads) ------------------------------------ #

    def spool_put(self, key: str, payload: dict[str, Any]) -> Path:
        """Durably stage one payload for delivery. Idempotent by key."""
        self.assert_owner_writable()
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", key)[:80]
        path = self.spool_dir / f"{safe}.json"
        with self.lock("spool"):
            size = len((json.dumps(payload, indent=2, sort_keys=True) + "\n").encode())
            old = path.stat().st_size if path.exists() else 0
            if self.spool_bytes() - old + size > SPOOL_CAP_BYTES:
                raise GapError("spool capacity exhausted; payload not accepted")
            atomic_write_json(path, payload)
        return path

    def spool_list(self) -> list[tuple[str, dict[str, Any]]]:
        out = []
        try:
            for path in sorted(self.spool_dir.glob("*.json")):
                try:
                    out.append((path.name, json.loads(path.read_text(encoding="utf-8"))))
                except (OSError, ValueError):
                    continue
        except OSError:
            pass
        return out

    def spool_remove(self, name: str) -> None:
        self.assert_owner_writable()
        try:
            (self.spool_dir / name).unlink()
        except OSError:
            pass

    def spool_bytes(self) -> int:
        try:
            return sum(p.stat().st_size for p in self.spool_dir.glob("*.json") if p.is_file())
        except OSError:
            return 0

    def spool_degraded(self) -> bool:
        return self.spool_bytes() > SPOOL_CAP_BYTES

    # -- digests -------------------------------------------------------------- #

    def summary(self) -> dict[str, Any]:
        records = self.list_records()
        retired = {
            r.gap_id: r.submission["retirement_receipt"].get("disposition")
            for r in records
            if r.status == "failed"
            and isinstance(r.submission.get("retirement_receipt"), dict)
            and r.submission["retirement_receipt"].get("disposition")
            in ("resource_exhausted", "model_response_unresolved")
        }
        by_status: dict[str, int] = {}
        for record in records:
            by_status[record.status] = by_status.get(record.status, 0) + 1
        return {
            "state_dir": str(self.root),
            "total": len(records),
            "by_status": by_status,
            "by_need": {
                cls: sum(1 for r in records if r.need_class == cls)
                for cls in NEED_CLASSES
                if any(r.need_class == cls for r in records)
            },
            "outstanding": sum(1 for r in records if r.status not in (TERMINAL_OK, "suppressed", "superseded")),
            "superseded": by_status.get("superseded", 0),
            "resource_exhausted": sum(v == "resource_exhausted" for v in retired.values()),
            "model_response_unresolved": sum(v == "model_response_unresolved" for v in retired.values()),
            # Gap jobs holding their slot while their started publication is resumed.
            "publication_pending": sum(
                1
                for r in records
                if isinstance(r.submission.get("publication_resume"), dict) and r.submission.get("state") != "terminal"
            ),
            # Open needs whose curator job stopped and was retired; they retry on a backoff.
            "stopped_unresolved": sum(
                1
                for r in records
                if r.status not in (TERMINAL_OK, "suppressed", "superseded", "failed")
                and isinstance(r.submission.get("retirement_receipt"), dict)
                and r.submission["retirement_receipt"].get("disposition") == "stopped_unresolved"
            ),
            "spool_bytes": self.spool_bytes(),
            "spool_degraded": self.spool_degraded(),
            "quarantined": len(self.quarantine_entries()),
        }


def _iso_plus(seconds: float) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")
