"""Durable slot dispatch (up to max_active_jobs); uncertainty retains the exact attempt and allowance."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from doclayer.coverage import load_catalog
from doclayer.curator.config import load_config, require_count
from doclayer.curator.jobstore import atomic_write_json
from doclayer.curator.stopped_task import JOB_STOP
from doclayer.curator.transport import Transport, transport_for
from doclayer.curator_reply import parse_job_reply, parse_submit_reply
from doclayer.gap_budget import BudgetUnavailable, ResourceBudget, today
from doclayer.gaps import (
    ACTIVE_SLOTS_DIR,
    LEGACY_ACTIVE_FILE,
    GapError,
    GapLedger,
    GapRecord,
    explicitly_attached_source,
    normalize_subject_identity,
    priority_source_candidates,
)
from doclayer.private_writer import private_retry_backoff_s, private_sources
from doclayer.question_scope import public_discovery_allowed
from doclayer.settings import settings

SEED_PRIORITY_POLICY = "explicit-attachment-order-v1"
LOCAL_SOURCE_POLICY = {
    "exclude": [
        {
            "domain": "*",
            "reason": "Local factual import: URL sources and linked public documentation cannot establish private facts",
        }
    ]
}
JOBS_PER_DAY = None
_SLOT_ID = re.compile(r"[a-zA-Z0-9_-]+")
#: Per-job automatic model-call grant; configured by DOC_LAYER_GAP_MODEL_CALLS_JOB.
MAX_MODEL_CALLS_PER_JOB = settings.gap_model_calls_job
MAX_MODEL_CALLS_PER_DAY = None
# retryable is not terminal: the job is resumed (bounded) or its allowance
# retired, and keeps the slot until one of those happens.
ACTIVE_JOB_TERMINAL = {"published", "partial", "covered", "failed", "cancelled"}
#: Conditional resumes of one stopped gap job before its allowance is retired.
MAX_STOP_RESUMES = 3
RETIREMENT_CATEGORIES = ("resource_exhausted", "model_response_unresolved", JOB_STOP)
#: A job whose publication has started is resumed, never retired: 5 min,
#: doubling, capped at 6 h, with no limit on the number of tries.
PUBLICATION_RESUME_BASE_S = 300
PUBLICATION_RESUME_CAP_S = 6 * 3600
#: Active time a publication resume may add (the curator's own bound).
PUBLICATION_RESUME_ACTIVE_S = 1800
PUBLICATION_PENDING = "publication pending"


def _today() -> str:
    return today()


def _max_target_pages() -> int:
    """How many current pages a private job may update: the curator's `single_pass.max_pages`."""
    try:
        value = getattr(load_config().single_pass, "max_pages", 4)
    except Exception:  # noqa: BLE001 — an unreadable config keeps the default
        value = 4
    return value if type(value) is int and value > 0 else 4


def attempt_key(gap_id: str, attempt: int, seeds: list[str], *, priority_policy: str | None = None) -> str:
    material = "\x00".join([gap_id, str(attempt)] + ([priority_policy] + seeds if priority_policy else sorted(seeds)))
    return hashlib.sha256(material.encode()).hexdigest()[:24]


@dataclass
class DispatchOutcome:
    gap_id: str
    action: str
    job_id: str | None = None
    detail: str = ""


class DayLedger(ResourceBudget):
    """Compatibility facade; reservations are now keyed by immutable attempt/day."""

    def __init__(self, state_dir: Path, jobs_per_day: int | None = JOBS_PER_DAY):
        super().__init__(state_dir, max_jobs=jobs_per_day)

    def reserve(self, gap_id: str, calls: int) -> bool:
        return self.reserve_attempt(gap_id, gap_id, calls) is not None

    def reserved_calls(self) -> int:
        return self.load(today())["reserved_calls"]

    def release(self, gap_id, *, proven_unused=False) -> int:
        reservation = gap_id if isinstance(gap_id, dict) else {"id": gap_id, "day": today()}
        return super().release(reservation, proven_unused=proven_unused)


class GapDispatcher:
    def __init__(
        self,
        ledger: GapLedger,
        *,
        transport: Transport | None = None,
        policy_version: int = 1,
        catalog=None,
        jobs_per_day: int | None = None,
        max_calls_job: int | None = None,
        max_calls_day: int | None = None,
        max_active_jobs: int | None = None,
    ):
        self._max_active_jobs = max_active_jobs
        self.ledger, self.transport, self.policy_version = (
            ledger,
            transport,
            policy_version,
        )
        self._catalog = catalog
        self.day_ledger = ResourceBudget(
            ledger.root,
            max_jobs=jobs_per_day,
            max_calls_job=max_calls_job,
            max_calls_day=max_calls_day,
        )
        self.slots_dir = ledger.root / ACTIVE_SLOTS_DIR
        self.legacy_active_path = ledger.root / LEGACY_ACTIVE_FILE

    def max_active_jobs(self) -> int:
        """Concurrent active submissions: constructor override, else gap_worker.max_active_jobs.

        An unreadable config or invalid value raises; it never silently runs
        at one slot."""
        value = self._max_active_jobs
        if value is None:
            try:
                value = load_config().gap_worker.max_active_jobs
            except Exception as exc:  # noqa: BLE001 — surfaced, never a silent default
                raise GapError(f"curator config unreadable: {exc}") from exc
        try:
            return require_count(value, "gap_worker.max_active_jobs")
        except ValueError as exc:
            raise GapError(f"curator config unreadable: {exc}") from exc

    def slot_path(self, gap_id: str) -> Path:
        if not isinstance(gap_id, str) or not _SLOT_ID.fullmatch(gap_id):
            raise GapError("invalid active slot identity")
        return self.slots_dir / f"{gap_id}.json"

    @property
    def catalog(self):
        if self._catalog is None:
            self._catalog = load_catalog(
                settings.brain_repo,
                settings.catalog_cache_dir,
                settings.routing_policy_path,
            )
        return self._catalog

    def client(self):
        return self.transport or transport_for(load_config())

    def paused(self) -> bool:
        return (self.ledger.root / "dispatch-paused").exists()

    def set_paused(self, paused: bool) -> None:
        self.ledger.assert_owner_writable()
        flag = self.ledger.root / "dispatch-paused"
        if paused:
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text("paused\n")
        else:
            flag.unlink(missing_ok=True)

    def curator_ready(self) -> bool:
        return (self.ledger.root / "curator-ready").exists()

    def build_payload(self, record: GapRecord, seeds: list[str], attempt: int) -> dict[str, Any]:
        private_scope = not public_discovery_allowed(record.to_dict(), self.catalog)
        if private_scope:
            seeds = []
        intent = (
            f"Documentation question: {record.representative_question}\n"
            "Curate supported facts that answer this question. Preserve source "
            "qualifications and leave unsupported parts unresolved. Do not substitute "
            "unrelated configuration or operational facts for the requested answer. "
            "Update existing relevant pages first.\n"
            f"Subject: '{record.canonical_subject or record.identity}'; facets: "
            f"{', '.join(record.facets) or 'unspecified facet'}; applicability: "
            f"{json.dumps(record.applicability, sort_keys=True)}. "
            f"Tracking: knowledge gap {record.gap_id}, attempt {attempt}."
        )
        payload = {
            "intent": intent,
            "urls": seeds,
            "exclusions": ["account/login", "signup", "cart", "checkout", "cookie"],
            "submitted_by": "doclayer-gap-dispatcher",
            "limits": {"max_model_calls": self.day_ledger.max_calls_job},
            "idempotency_key": attempt_key(record.gap_id, attempt, seeds, priority_policy=SEED_PRIORITY_POLICY),
        }
        if private_scope:
            payload["source_policy"] = json.loads(json.dumps(LOCAL_SOURCE_POLICY))
        private_texts = private_sources(record)
        if private_texts:
            payload.update(
                intent=(
                    f"Document from live read-only evidence: {record.representative_question}\n"
                    f"Facets: {', '.join(record.facets) or 'unspecified facet'}. Every fact cites its live check label. "
                    "State only what the outputs show; an absence needs output that states it. "
                    f"Tracking: knowledge gap {record.gap_id}, attempt {attempt}."
                ),
                urls=[],
                texts=[{"label": s["label"], "text": s["text"]} for s in private_texts],
                private=True,
                submitted_by="gap-worker-private",
                mode="single-pass",
                source_policy=json.loads(json.dumps(LOCAL_SOURCE_POLICY)),
                idempotency_key=attempt_key(
                    record.gap_id,
                    attempt,
                    [s["source_hash"] for s in private_texts],
                    priority_policy=SEED_PRIORITY_POLICY,
                ),
            )
            entry = (
                self.catalog.subjects.get(record.canonical_subject)
                if self.catalog and record.canonical_subject
                else None
            )
            current = [page.path for page in (entry.pages if entry else []) if page.authority == "current"]
            if current:
                payload["targets"] = current[: _max_target_pages()]
            return payload
        return payload

    def active(self, gap_id: str | None = None) -> dict[str, Any] | None:
        """The active submission of one gap, or the oldest active submission."""
        if gap_id is not None and _SLOT_ID.fullmatch(str(gap_id)):
            self._migrate_legacy_slot()
            path = self.slot_path(gap_id)
            if path.exists():
                return self._load_slot(path)
            return next((a for a in self.active_all() if a["gap_id"] == gap_id), None)
        actives = self.active_all()
        return actives[0] if actives else None

    def active_all(self) -> list[dict[str, Any]]:
        """Every active submission, oldest first. A corrupt slot withholds all dispatch."""
        self._migrate_legacy_slot()
        loaded: list[tuple[int, str, dict[str, Any]]] = []
        for path in sorted(self.slots_dir.glob("*.json")) if self.slots_dir.is_dir() else []:
            try:
                stamp = path.stat().st_mtime_ns
            except FileNotFoundError:
                continue
            active = self._load_slot(path)
            if active is not None:
                loaded.append((stamp, active["gap_id"], active))
        loaded.sort(key=lambda item: (item[0], item[1]))
        actives = [item[2] for item in loaded]
        limit = self.max_active_jobs()
        if len(actives) >= limit:
            return actives
        # Recover crash after record checkpoint but before slot checkpoint,
        # including legacy jobs. More historical active jobs than slots block new work.
        slotted = {a["gap_id"] for a in actives}
        candidates = [
            r
            for r in self.ledger.list_records()
            if r.gap_id not in slotted
            and (
                r.submission.get("state") in ("reserved", "submitting", "uncertain", "accepted")
                or (not r.submission and r.status in ("queued", "curating"))
            )
        ]
        if len(actives) + len(candidates) > limit:
            raise GapError("multiple active legacy submissions; operator reconciliation required")
        for record in sorted(candidates, key=lambda r: r.gap_id):
            row = record.submission
            active = {
                "gap_id": record.gap_id,
                "attempt_id": row.get("attempt_id", f"legacy:{record.gap_id}"),
                "calls": row.get("reservation", {}).get("calls", self.day_ledger.max_calls_job),
                "job_id": row.get("job_id") or (record.job_links[-1]["job_id"] if record.job_links else None),
            }
            atomic_write_json(self.slot_path(record.gap_id), active)
            actives.append(active)
        return actives

    @staticmethod
    def _valid_active(active: Any) -> bool:
        return (
            isinstance(active, dict)
            and isinstance(active.get("gap_id"), str)
            and bool(_SLOT_ID.fullmatch(active["gap_id"]))
            and bool(active.get("attempt_id"))
            and type(active.get("calls")) is int
        )

    def _migrate_legacy_slot(self) -> None:
        """Move the pre-slot singleton file into its per-gap slot; remove it last."""
        if not self.legacy_active_path.exists():
            return
        try:
            active = json.loads(self.legacy_active_path.read_text())
            if not self._valid_active(active):
                raise ValueError("invalid active submission")
            atomic_write_json(self.slot_path(active["gap_id"]), active)
            self.legacy_active_path.unlink(missing_ok=True)
        except (ValueError, OSError) as exc:
            raise GapError("active submission corrupt; dispatcher withheld") from exc

    def _load_slot(self, path: Path) -> dict[str, Any] | None:
        """One slot's rules: validation, retirement receipt, terminal release."""
        try:
            active = json.loads(path.read_text())
            if not self._valid_active(active) or path.stem != active["gap_id"]:
                raise ValueError("invalid active submission")
            record = self.ledger.load(active["gap_id"])
            receipt = record.submission.get("retirement_receipt")
            if record.submission.get("state") == "terminal" and receipt:
                self._check_retirement_receipt(active, record.submission.get("retirement_intent", {}), receipt)
                self.day_ledger.account_active()
                path.unlink(missing_ok=True)
                return None
            return active
        except (ValueError, OSError) as exc:
            raise GapError("active submission corrupt; dispatcher withheld") from exc

    @staticmethod
    def _check_retirement_receipt(active, intent, receipt):
        category = intent.get("stop_category", "resource_exhausted") if isinstance(intent, dict) else None
        if (
            not isinstance(intent, dict)
            or not isinstance(receipt, dict)
            or type(active.get("calls")) is not int
            or not 0 < active["calls"]
            or intent.get("job_id") != active.get("job_id")
            or intent.get("attempt_id") != active.get("attempt_id")
            or type(intent.get("original_grant")) is not int
            or intent.get("original_grant") != active["calls"]
            or not isinstance(intent.get("expected_checkpoint"), str)
            or re.fullmatch(r"[0-9a-f]{64}", intent["expected_checkpoint"]) is None
            or category not in RETIREMENT_CATEGORIES
            or receipt.get("disposition") != category
            or receipt.get("job_id") != active.get("job_id")
            or receipt.get("attempt_id") != active.get("attempt_id")
            or type(receipt.get("original_grant")) is not int
            or receipt.get("original_grant") != active.get("calls")
            or receipt.get("checkpoint") != intent.get("expected_checkpoint")
            or type(receipt.get("model_calls")) is not int
            or receipt["model_calls"] < 0
            or (category == "resource_exhausted" and receipt["model_calls"] < active["calls"])
            or (category == "model_response_unresolved" and receipt["model_calls"] > active["calls"])
        ):
            raise GapError("retirement receipt does not match active allowance")

    def retire_exhausted(self, gap_id: str, job: dict[str, Any], *, stop_reason: str | None = None) -> DispatchOutcome:
        """Retire a proven stopped automatic allowance, preserving unresolved work.

        Without `stop_reason` only a model-call cap or a typed task stop
        qualifies. With it (JOB_STOP) any retryable stop of this job does: the
        slot is released and the gap returns to a backoff, never parked.
        """
        self.ledger.assert_owner_writable()
        if publication_started(job):
            # Never retired once published material exists; resumed instead.
            return self.resume_publication(gap_id, job)
        with self.ledger.lock("dispatcher"):
            active = self.active(gap_id)
            if not active or active["gap_id"] != gap_id or active.get("job_id") != job.get("job_id"):
                return DispatchOutcome(gap_id, "parked", detail="no matching active allowance")
            record = self.ledger.load(gap_id)
            intent = record.submission.get("retirement_intent")
            if intent is None:
                cap = job.get("limits", {}).get("max_model_calls") if isinstance(job.get("limits"), dict) else None
                used = job.get("usage", {}).get("model_calls") if isinstance(job.get("usage"), dict) else None
                marker = job.get("automatic_stop")
                category = (
                    JOB_STOP
                    if stop_reason is not None
                    else "model_response_unresolved"
                    if isinstance(marker, dict)
                    and marker.get("category") == "model_response_unresolved"
                    and marker.get("stage") == job.get("stage")
                    and marker.get("error") == job.get("error")
                    else "resource_exhausted"
                )
                if (
                    job.get("status") != "retryable"
                    or type(cap) is not int
                    or not 0 < cap
                    or cap != active["calls"]
                    or type(used) is not int
                    or used < 0
                    or (
                        category == "resource_exhausted"
                        and (job.get("error") != "limit reached: model_calls" or used < cap)
                    )
                    or (category == "model_response_unresolved" and used > cap)
                    or not isinstance(job.get("resume_checkpoint"), str)
                    or len(job["resume_checkpoint"]) != 64
                ):
                    return DispatchOutcome(gap_id, "parked", detail="not a proven automatic hard-cap stop")
                intent = {
                    "job_id": active["job_id"],
                    "attempt_id": active["attempt_id"],
                    "original_grant": cap,
                    "expected_checkpoint": job["resume_checkpoint"],
                }
                if category != "resource_exhausted":
                    intent["stop_category"] = category
                if category == JOB_STOP:
                    intent["stop_reason"] = stop_reason
                    intent["stop_error"] = str(job.get("error"))[:300]
                self.ledger.update(gap_id, lambda r: r.submission.update(retirement_intent=intent))
            try:
                reply = self.client().call("retire_allowance", intent, timeout_s=60)
                if not isinstance(reply, dict) or reply.get("ok") is not True:
                    raise GapError("retirement not acknowledged")
                receipt = reply.get("receipt")
                self._check_retirement_receipt(active, intent, receipt)
            except Exception as exc:  # noqa: BLE001 — durable uncertain retirement
                return DispatchOutcome(
                    gap_id,
                    "uncertain",
                    active["job_id"],
                    f"retirement withheld: {type(exc).__name__}",
                )
            self.day_ledger.account_active()
            category = receipt["disposition"]

            def retired(r):
                r.submission.update(
                    state="terminal",
                    remote_status="retryable",
                    retirement_receipt=receipt,
                )
                if category == JOB_STOP:
                    # Never a silent hold: the gap is retried after a visible backoff
                    # (a private gap gathers fresh evidence first).
                    private = r.submission.get("payload", {}).get("private") is True
                    r.status = "needs_local_evidence" if private else "retry_wait"
                    wait = (
                        private_retry_backoff_s(r.private_attempts)
                        if private
                        else self.ledger.backoff_state(max(1, r.attempts))
                    )
                    r.backoff_until = (datetime.now(UTC) + timedelta(seconds=wait)).isoformat(timespec="seconds")
                    r.failure_reason = (
                        f"stopped_unresolved: curator job {active['job_id']} stopped "
                        f"({intent.get('stop_error', 'unknown stop')}); "
                        f"{intent.get('stop_reason', 'not recoverable')}; allowance retired, "
                        "the job keeps its work for readmission; a new attempt follows the backoff"
                    )
                    return
                r.status = "failed"
                r.failure_reason = (
                    "resource_exhausted: immutable automatic model-call allowance exhausted; work remains unresolved"
                    if category == "resource_exhausted"
                    else "model_response_unresolved: curator stopped before publication; allowance retired and work remains unresolved"
                )
                r.backoff_until = None

            self.ledger.update(gap_id, retired)
            self.slot_path(gap_id).unlink(missing_ok=True)
            return DispatchOutcome(
                gap_id,
                category,
                active["job_id"],
                "allowance retired; unresolved work preserved",
            )

    def observe(self, gap_id: str, job: dict[str, Any]) -> None:
        """Only a verified remote terminal observation clears the singleton."""
        self.ledger.assert_owner_writable()
        with self.ledger.lock("dispatcher"):
            active = self.active(gap_id)
            if not active or active["gap_id"] != gap_id or active.get("job_id") != job.get("job_id"):
                return
            if job["status"] in ACTIVE_JOB_TERMINAL:
                self.day_ledger.account_active()

                def terminal(r):
                    r.submission.update(state="terminal", remote_status=job["status"])
                    if (r.failure_reason or "").startswith(PUBLICATION_PENDING):
                        r.failure_reason = None

                self.ledger.update(gap_id, terminal)
                self.slot_path(gap_id).unlink(missing_ok=True)

    def resume_outage(self, gap_id: str, job: dict[str, Any]) -> DispatchOutcome:
        """Bounded conditional continuation of this job, never another allowance.

        Every stopped job either recovers or releases its slot: a crash, a
        publication failure or an inference outage gets at most
        MAX_STOP_RESUMES conditional resumes; any other stop, an exhausted
        allowance, or a stop after the last resume retires the allowance
        (JOB_STOP). A stop with cancellation requested is cancelled.
        """
        self.ledger.assert_owner_writable()
        with self.ledger.lock("dispatcher"):
            active = self.active(gap_id)
            if not active or active["gap_id"] != gap_id or active.get("job_id") != job.get("job_id"):
                return DispatchOutcome(gap_id, "parked", detail="no matching active job")
            if self.paused() or not self.curator_ready():
                return DispatchOutcome(gap_id, "parked", detail="automatic work paused/not ready")
            if job.get("status") == "retryable" and job.get("cancel_requested") is True:
                return self._cancel_stopped(gap_id, job)
            if publication_started(job):
                return self.resume_publication(gap_id, job)
            if not _stopped_job(job):
                return DispatchOutcome(gap_id, "parked", detail="not a well-formed stopped job")
            caps, used = job["limits"], job["usage"]
            if type(active.get("calls")) is not int or caps["max_model_calls"] > active["calls"]:
                return DispatchOutcome(gap_id, "parked", detail="existing job allowance invalid")
            if used["model_calls"] >= caps["max_model_calls"] or used["active_seconds"] >= caps["max_active_seconds"]:
                return self.retire_exhausted(gap_id, job, stop_reason="its allowance is exhausted")
            if not _resumable_error(job["error"]):
                return self.retire_exhausted(gap_id, job, stop_reason="a resume would repeat the same stop")
            record = self.ledger.load(gap_id)
            recovery = record.submission.get("outage_resume", {})
            if not isinstance(recovery, dict):
                return DispatchOutcome(gap_id, "parked", detail="invalid recovery checkpoint")
            if type(recovery.get("attempts", 0)) is int and recovery.get("attempts", 0) >= MAX_STOP_RESUMES:
                return self.retire_exhausted(
                    gap_id,
                    job,
                    stop_reason=f"it stopped again after {MAX_STOP_RESUMES} bounded resumes",
                )
            checkpoint = job["resume_checkpoint"]
            now = datetime.now(UTC)
            if recovery.get("checkpoint") != checkpoint:
                recovery = {
                    "checkpoint": checkpoint,
                    "attempts": recovery.get("attempts", 0),
                    "next_at": (now + timedelta(seconds=300)).isoformat(),
                    "state": "waiting",
                }
                self.ledger.update(gap_id, lambda r: r.submission.update(outage_resume=recovery))
                return DispatchOutcome(gap_id, "waiting", detail="outage backoff scheduled")
            attempts = recovery.get("attempts")
            try:
                due = datetime.fromisoformat(recovery["next_at"])
                if due.tzinfo is None:
                    raise ValueError("backoff missing timezone")
            except (ValueError, KeyError, TypeError):
                return DispatchOutcome(gap_id, "parked", detail="invalid recovery checkpoint")
            if type(attempts) is not int or attempts < 0 or now < due:
                return DispatchOutcome(gap_id, "waiting", detail="bounded outage backoff/attempt cap")
            try:
                self.day_ledger.account_active()
                budget = self.day_ledger.load(today())
                if (
                    self.day_ledger.max_calls_day is not None
                    and budget["reserved_calls"] + budget["model_calls"] > self.day_ledger.max_calls_day
                ):
                    return DispatchOutcome(gap_id, "parked", detail="daily allowance unavailable")
            except BudgetUnavailable as exc:
                return DispatchOutcome(gap_id, "parked", detail=str(exc))
            recovery.update(
                state="uncertain",
                attempts=attempts + 1,
                next_at=(now + timedelta(seconds=300 * 2**attempts)).isoformat(),
            )
            self.ledger.update(gap_id, lambda r: r.submission.update(outage_resume=recovery))
            # Intent is durable BEFORE transport. A lost reply is observed on
            # the next cycle; the exact remote CAS makes replay non-destructive.
            try:
                reply = self.client().call(
                    "resume",
                    {"job_id": job["job_id"], "expected_checkpoint": checkpoint},
                    timeout_s=60,
                )
                if (
                    not isinstance(reply, dict)
                    or reply.get("ok") is not True
                    or reply.get("job_id") != job["job_id"]
                    or reply.get("status") != "queued"
                ):
                    raise ValueError("invalid conditional resume receipt")
            except Exception as exc:  # noqa: BLE001 — uncertain remote outcome retains identity
                return DispatchOutcome(gap_id, "uncertain", job["job_id"], type(exc).__name__)
            self.ledger.update(gap_id, lambda r: r.submission["outage_resume"].update(state="accepted"))
            return DispatchOutcome(gap_id, "resumed", job["job_id"], "same bounded job queued")

    def resume_publication(self, gap_id: str, job: dict[str, Any]) -> DispatchOutcome:
        """Resume the same publication-started job on a growing backoff, forever.

        It is never retired and never replaced by a new attempt (that would
        orphan its branch/PR and republish its material). It keeps its slot,
        and the gap's failure_reason says so. A publication resume costs no new
        model allowance: publication re-queries push/PR/merge state instead of
        repeating them, and a landed but unrecorded merge is recovered first.
        """
        self.ledger.assert_owner_writable()
        with self.ledger.lock("dispatcher"):
            active = self.active(gap_id)
            if not active or active["gap_id"] != gap_id or active.get("job_id") != job.get("job_id"):
                return DispatchOutcome(gap_id, "parked", detail="no matching active job")
            if self.paused() or not self.curator_ready():
                return DispatchOutcome(gap_id, "parked", detail="automatic work paused/not ready")
            if job.get("status") == "retryable" and job.get("cancel_requested") is True:
                return self._cancel_stopped(gap_id, job)
            if not _stopped_job(job):
                return DispatchOutcome(gap_id, "parked", detail="not a well-formed stopped job")
            recovery = self.ledger.load(gap_id).submission.get("publication_resume")
            recovery = recovery if isinstance(recovery, dict) else {}
            attempts = recovery.get("attempts", 0)
            attempts = attempts if type(attempts) is int and attempts >= 0 else 0
            checkpoint, error = job["resume_checkpoint"], job["error"]
            now = datetime.now(UTC)

            def pending(next_at: str, state: str, tries: int):
                record = {
                    "checkpoint": checkpoint,
                    "attempts": tries,
                    "next_at": next_at,
                    "state": state,
                    "error": error[:300],
                }

                def write(r):
                    r.submission["publication_resume"] = record
                    r.failure_reason = f"{PUBLICATION_PENDING}: {error[:300]}, next try {next_at}"

                self.ledger.update(gap_id, write)
                return record

            if recovery.get("checkpoint") != checkpoint:
                next_at = (now + timedelta(seconds=_publication_backoff_s(attempts))).isoformat(timespec="seconds")
                pending(next_at, "waiting", attempts)
                return DispatchOutcome(
                    gap_id,
                    "publication_pending",
                    job["job_id"],
                    f"{PUBLICATION_PENDING}: {error[:200]}; next try {next_at}",
                )
            try:
                due = datetime.fromisoformat(recovery["next_at"])
                if due.tzinfo is None:
                    raise ValueError("backoff missing timezone")
            except (ValueError, KeyError, TypeError):
                due = now
            if now < due:
                return DispatchOutcome(
                    gap_id,
                    "publication_pending",
                    job["job_id"],
                    f"{PUBLICATION_PENDING}: {error[:200]}; next try {recovery['next_at']}",
                )
            caps, used = job["limits"], job["usage"]
            request: dict[str, Any] = {
                "job_id": job["job_id"],
                "expected_checkpoint": checkpoint,
            }
            if used["active_seconds"] >= caps["max_active_seconds"] - 300:
                # Publication is still bounded work; grant it bounded time to finish.
                request["extend"] = {"max_active_seconds": PUBLICATION_RESUME_ACTIVE_S}
            next_at = (now + timedelta(seconds=_publication_backoff_s(attempts + 1))).isoformat(timespec="seconds")
            # Intent is durable before transport; a lost reply is re-observed.
            pending(next_at, "uncertain", attempts + 1)
            try:
                reply = self.client().call("resume", request, timeout_s=60)
                if (
                    not isinstance(reply, dict)
                    or reply.get("ok") is not True
                    or reply.get("job_id") != job["job_id"]
                    or reply.get("status") != "queued"
                ):
                    raise ValueError("invalid conditional resume receipt")
            except Exception as exc:  # noqa: BLE001 — uncertain remote outcome retains identity
                return DispatchOutcome(
                    gap_id,
                    "uncertain",
                    job["job_id"],
                    f"{PUBLICATION_PENDING}: resume unacknowledged ({type(exc).__name__})",
                )
            self.ledger.update(gap_id, lambda r: r.submission["publication_resume"].update(state="accepted"))
            return DispatchOutcome(
                gap_id,
                "publication_resumed",
                job["job_id"],
                f"{PUBLICATION_PENDING}: {error[:200]}; same job resumed (try {attempts + 1})",
            )

    def _cancel_stopped(self, gap_id: str, job: dict[str, Any]) -> DispatchOutcome:
        """A stopped job whose cancellation was requested is cancelled, not resumed;
        the next observation of `cancelled` releases the slot."""
        try:
            reply = self.client().call("cancel", {"job_id": job["job_id"]}, timeout_s=60)
            if not isinstance(reply, dict) or reply.get("ok") is not True:
                raise ValueError("cancel not acknowledged")
        except Exception as exc:  # noqa: BLE001 — retried next cycle
            return DispatchOutcome(gap_id, "uncertain", job["job_id"], f"cancel withheld: {type(exc).__name__}")
        return DispatchOutcome(
            gap_id,
            "cancelling",
            job["job_id"],
            f"cancellation requested; remote status {reply.get('status')}",
        )

    def dispatch(self, gap_id: str, *, worker: str = "manual") -> DispatchOutcome:
        self.ledger.assert_owner_writable()
        with self.ledger.lock("dispatcher"):
            try:
                return self._dispatch(gap_id)
            except (GapError, BudgetUnavailable) as exc:
                return DispatchOutcome(gap_id, "skipped", detail=str(exc))

    def _dispatch(self, gap_id: str) -> DispatchOutcome:
        self.ledger.assert_owner_writable()
        if self.paused():
            return DispatchOutcome(gap_id, "paused", detail="manual pause is active")
        slots = self.active_all()
        active = next((a for a in slots if a["gap_id"] == gap_id), None)
        if active is None and len(slots) >= self.max_active_jobs():
            return DispatchOutcome(
                gap_id,
                "skipped",
                detail="another durable active submission occupies the slot",
            )
        if active:
            if active.get("job_id"):
                try:
                    job = parse_job_reply(self.client().call("status", {"job_id": active["job_id"]}))

                    if job["job_id"] != active["job_id"]:
                        raise ValueError("foreign curator job receipt")

                    def recovered(r):
                        r.submission.update(job_id=active["job_id"])
                        r.status = "curating"
                        if not any(link.get("job_id") == active["job_id"] for link in r.job_links):
                            r.job_links.append(
                                {
                                    "job_id": active["job_id"],
                                    "attempt": r.attempts,
                                    "idempotency_key": active["attempt_id"],
                                }
                            )

                    self.ledger.update(gap_id, recovered)
                    self.observe(gap_id, job)
                except Exception:  # noqa: BLE001 — no remote failure clears an uncertain slot
                    return DispatchOutcome(
                        gap_id,
                        "skipped",
                        detail="active job status unavailable; slot retained",
                    )
                return DispatchOutcome(
                    gap_id,
                    "skipped",
                    job_id=active["job_id"],
                    detail="existing job observed; no new submission",
                )
        record = self.ledger.load(gap_id)
        if record.status not in (
            "ready_for_curation",
            "retry_wait",
            "queued",
            "awaiting_curator_readiness",
        ):
            return DispatchOutcome(gap_id, "skipped", detail=f"status {record.status}")
        submission = record.submission
        private_scope = not public_discovery_allowed(record.to_dict(), self.catalog)
        private_texts = private_sources(record)
        if private_scope and (
            not private_texts and (not submission or submission.get("state") in ("staged", "terminal", "abandoned"))
        ):
            from doclayer.local_material import DIRECT_AUTHORING_REASON

            if submission.get("state") == "staged":
                # Prepared but never sent: the curator receives private material
                # only as a gap-worker private job. Keep the payload on the record
                # as an abandoned attempt.
                self.ledger.update(
                    gap_id,
                    lambda r: r.submission.update(state="abandoned", abandoned_reason=DIRECT_AUTHORING_REASON),
                )
            self.ledger.transition(gap_id, "needs_local_evidence", reason=DIRECT_AUTHORING_REASON)
            return DispatchOutcome(gap_id, "skipped", detail=DIRECT_AUTHORING_REASON)
        if private_scope and (
            not private_texts
            or (
                submission.get("state") not in (None, "terminal", "abandoned")
                and (
                    submission.get("payload", {}).get("urls")
                    or submission.get("payload", {}).get("source_policy") != LOCAL_SOURCE_POLICY
                )
            )
        ):
            # Policy changes never rewrite a staged/lost-reply attempt. Hold its
            # exact payload, reservation and active slot for owning recovery.
            reason = "private factual need requires attributed local evidence; public-only payload withheld"
            self.ledger.transition(gap_id, "needs_local_evidence", reason=reason)
            return DispatchOutcome(gap_id, "skipped", detail=reason)
        if not submission or submission.get("state") in ("terminal", "abandoned"):
            official = [
                c
                for c in record.source_candidates
                if not private_scope and c.get("url") and c.get("authority") == "official"
            ]
            attached = [c for c in priority_source_candidates(official) if explicitly_attached_source(c)]
            remaining = sorted(
                (c for c in official if not explicitly_attached_source(c)),
                key=lambda c: str(c["url"]),
            )
            seeds = list(dict.fromkeys(str(c["url"]) for c in attached + remaining))
            private_texts = private_sources(record)
            if not seeds and not private_texts:
                return DispatchOutcome(gap_id, "skipped", detail="no authoritative seeds")
            source_inputs = sorted(
                [
                    {"url": str(c["url"]), "source_hash": c.get("source_hash", "")}
                    for c in record.source_candidates
                    if not private_scope and c.get("url") and c.get("authority") == "official"
                ],
                key=lambda c: (c["url"], c["source_hash"]),
            )
            source_inputs += [{"text_label": s["label"], "source_hash": s["source_hash"]} for s in private_texts]
            signature = hashlib.sha256(
                json.dumps(
                    {
                        "sources": source_inputs,
                        **({"source_policy": LOCAL_SOURCE_POLICY} if private_scope else {}),
                        "seed_priority_policy": SEED_PRIORITY_POLICY,
                        "ordered_urls": seeds,
                        "policy_version": self.policy_version,
                        "evidence_generation": record.evidence_generation,
                        "coverage_revision": record.coverage_revision,
                        "subject": normalize_subject_identity(
                            (record.canonical_subject or record.identity).removeprefix("unknown:")
                        ),
                        "question": record.representative_question,
                        "facets": record.facets,
                        "applicability": record.applicability,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if (
                submission.get("state") == "terminal"
                and submission.get("input_signature") == signature
                and not submission.get("manual_retry")
                # A stopped job's retired attempt is retried with the same material.
                and (submission.get("retirement_receipt") or {}).get("disposition") != JOB_STOP
            ):
                self.ledger.transition(
                    gap_id,
                    "retry_wait",
                    reason="terminal attempt has identical material; await refreshed sources or explicit retry",
                    backoff_until=self._backoff_from(4),
                )
                return DispatchOutcome(
                    gap_id,
                    "skipped",
                    detail="terminal identical-input attempt parked; no blind reimport",
                )
            attempt = record.attempts + 1
            payload = self.build_payload(record, seeds, attempt)
            submission = {
                "attempt": attempt,
                "attempt_id": payload["idempotency_key"],
                "payload": payload,
                "state": "staged",
                "policy_version": self.policy_version,
                "input_signature": signature,
                "input_sources": source_inputs,
                "seed_priority_policy": SEED_PRIORITY_POLICY,
                "ordered_urls": seeds,
                "input_evidence_generation": record.evidence_generation,
                "input_coverage_revision": record.coverage_revision,
            }

            def begin(r):
                if r.submission:
                    r.submission_history.append(json.loads(json.dumps(r.submission)))
                r.submission = submission

            self.ledger.update(gap_id, begin)
        if not self.curator_ready():
            self.ledger.spool_put(f"dispatch-{gap_id}-{submission['attempt']}", submission["payload"])
            self.ledger.transition(
                gap_id,
                "awaiting_curator_readiness",
                reason="curator readiness not proven",
            )
            return DispatchOutcome(gap_id, "dry-run", detail="immutable payload staged")
        reservation = submission.get("reservation")
        if not reservation:
            max_calls = self.day_ledger.max_calls_job
            if submission["payload"].get("private"):
                max_calls = min(max_calls, load_config().private_writer.max_job_calls)
            reservation = self.day_ledger.reserve_attempt(
                submission["attempt_id"],
                gap_id,
                max_calls,
                # No daily capacity is reserved for verification.
            )
            if not reservation:
                self.ledger.transition(
                    gap_id,
                    "retry_wait",
                    reason="daily allowance exhausted",
                    backoff_until=(datetime.now(UTC) + timedelta(days=1))
                    .replace(hour=0, minute=0, second=0, microsecond=0)
                    .isoformat(),
                )
                return DispatchOutcome(gap_id, "skipped", detail="daily budget exhausted")
            submission["reservation"] = reservation
            submission["payload"]["limits"]["max_model_calls"] = reservation["calls"]
            submission["state"] = "reserved"
            self.ledger.update(gap_id, lambda r: setattr(r, "submission", submission))
        active = {
            "gap_id": gap_id,
            "attempt_id": submission["attempt_id"],
            "calls": reservation["calls"],
            "job_id": None,
        }
        atomic_write_json(self.slot_path(gap_id), active)
        submission["state"] = "submitting"
        self.ledger.transition(gap_id, "queued", attempts=submission["attempt"], submission=submission)
        try:
            reply = parse_submit_reply(self.client().call("submit", submission["payload"]))
        except Exception as exc:  # noqa: BLE001 — durable uncertainty/worker isolation
            submission["state"] = "uncertain"
            self.ledger.transition(
                gap_id,
                "retry_wait",
                submission=submission,
                reason=f"uncertain submission: {type(exc).__name__}",
                backoff_until=self._backoff_from(submission["attempt"]),
            )
            return DispatchOutcome(
                gap_id,
                "skipped",
                detail=f"transport error or invalid reply: {type(exc).__name__}; exact attempt retained",
            )
        job_id = reply["job_id"]
        submission.update(state="accepted", job_id=job_id)
        active["job_id"] = job_id
        atomic_write_json(self.slot_path(gap_id), active)

        def accepted(r):
            r.submission = submission
            link = {
                "job_id": job_id,
                "attempt": submission["attempt"],
                "idempotency_key": submission["attempt_id"],
            }
            if link not in r.job_links:
                r.job_links.append(link)
            r.status = "curating"

        self.ledger.update(gap_id, accepted)
        self.ledger.spool_remove(f"dispatch-{gap_id}-{submission['attempt']}.json")
        return DispatchOutcome(
            gap_id,
            "submitted",
            job_id=job_id,
            detail="" if reply["created"] else "idempotent duplicate",
        )

    def _backoff_from(self, attempts: int) -> str:
        return (datetime.now(UTC) + timedelta(seconds=self.ledger.backoff_state(attempts))).isoformat(
            timespec="seconds"
        )


_OUTAGE_ERROR = re.compile(
    r"inference: inference (?:HTTP (?:408|409|425|429|500|502|503|504) \(transient\)|"
    r"unreachable: (?:ConnectError|ConnectTimeout|ReadError|ReadTimeout|WriteError|WriteTimeout|PoolTimeout|RemoteProtocolError|ProxyError))"
)


def _outage_error(error: Any) -> bool:
    return isinstance(error, str) and _OUTAGE_ERROR.fullmatch(error) is not None


def publication_started(job: dict[str, Any]) -> bool:
    """The job has published something: a commit, push, PR or merge."""
    pub = job.get("publication")
    return isinstance(pub, dict) and bool(
        pub.get("commit_sha")
        or pub.get("pushed")
        or pub.get("pr_number")
        or pub.get("merged")
        or pub.get("merge_commit")
        or pub.get("committed_paths")
    )


def _publication_backoff_s(attempts: int) -> float:
    """5 minutes, doubling with every publication resume, capped at 6 h."""
    return float(min(PUBLICATION_RESUME_BASE_S * 2 ** min(attempts, 32), PUBLICATION_RESUME_CAP_S))


def _resumable_error(error: Any) -> bool:
    """Stops a resume of the same job may cure: an inference outage, a worker
    crash, or a publication-side git/Forgejo failure (publication recovery is
    idempotent). Every other stop would recur, so it is retired instead."""
    return _outage_error(error) or (isinstance(error, str) and error.startswith(("worker crash: ", "publication: ")))


def _stopped_job(job: dict[str, Any]) -> bool:
    """A retryable, uncancelled job with a checkpoint and well-formed limits."""
    if job.get("status") != "retryable" or job.get("cancel_requested") is not False:
        return False
    if not isinstance(job.get("error"), str):
        return False
    if not isinstance(job.get("resume_checkpoint"), str) or not re.fullmatch(r"[0-9a-f]{64}", job["resume_checkpoint"]):
        return False
    caps, used = job.get("limits"), job.get("usage")
    if not isinstance(caps, dict) or not isinstance(used, dict):
        return False
    if (
        type(caps.get("max_model_calls")) is not int
        or caps["max_model_calls"] <= 0
        or type(used.get("model_calls")) is not int
        or used["model_calls"] < 0
    ):
        return False
    for value in (caps.get("max_active_seconds"), used.get("active_seconds")):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            return False
    return caps["max_active_seconds"] > 0
