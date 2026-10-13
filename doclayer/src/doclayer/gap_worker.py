"""Background knowledge-gap worker: a five-minute cycle.

Each cycle, with one dispatcher claim held at a time:

1. re-check candidates against current committed coverage,
2. verify publications through normal indexing and answering, and resume
   (bounded) or release every stopped curator job,
3. dispatch ready gaps to free dispatcher slots under the durable budgets
   (dry-run until curator readiness is proven),
4. research uncovered needs (least-recently-claimed first; every eligible need,
   since by default there is no time budget; bounded only if
   `gap_worker.cycle_budget_s` is configured) with authoritative-source
   discovery or live private evidence, dispatching each need research makes
   ready while a slot is free, then dispatch what is still ready,
5. prune verbose resolved-attempt detail and report a concise digest.

Run under a timer (`doclayer-gap-worker --once` every five minutes) or with
`--loop`. Manual pause disables new automatic submissions only.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from doclayer import private_writer, verification
from doclayer.coverage import load_catalog, ssh_host_aliases
from doclayer.curator.config import load_config
from doclayer.curator.jobstore import now_iso
from doclayer.discovery import discover
from doclayer.dispatcher import RETIREMENT_CATEGORIES, GapDispatcher, publication_started
from doclayer.gaps import (
    CLOSED_STATES,
    NOT_A_DOCUMENTATION_NEED,
    GapLedger,
    explicitly_attached_source,
    is_chat_remark,
    normalize_subject_identity,
)
from doclayer.local_material import DIRECT_AUTHORING_REASON, is_local_need
from doclayer.question_scope import public_discovery_allowed, public_subject
from doclayer.settings import settings

DEFAULT_INTERVAL_S = 300


def _backoff_passed(record) -> bool:
    if not record.backoff_until:
        return True
    try:
        return datetime.now(UTC) >= datetime.fromisoformat(record.backoff_until)
    except ValueError:
        return True


def _is_covered(catalog, record) -> bool:
    if record.canonical_subject:
        return catalog.subjects.get(record.canonical_subject) is not None and (
            catalog.subjects[record.canonical_subject].pages or catalog.subjects[record.canonical_subject].source_copies
        )
    return False


def _research_one(ledger: GapLedger, record, catalog, hosts, notes: list[str]) -> None:
    """Resolve identity, recheck coverage, gather seeds."""

    gap_id = record.gap_id
    ledger.transition(gap_id, "researching", reason="worker claimed the candidate")
    # Identity resolution: a previously unknown subject may now be canonical.
    if not record.canonical_subject and record.identity.startswith("unknown:"):
        bare = record.identity.removeprefix("unknown:")
        if bare == "unidentified":
            bare = public_subject(record.to_dict(), catalog)
        matches = [
            entry
            for entry in catalog.subjects.values()
            if bare in {normalize_subject_identity(name) for name in [entry.slug, entry.title, *entry.aliases]}
        ]
        entry = matches[0] if len(matches) == 1 else None
        if entry is not None:
            record.canonical_subject = entry.slug
            ledger.update(gap_id, lambda r: setattr(r, "canonical_subject", entry.slug))
    if _awaits_first_evidence(record) and is_local_need(record, catalog):
        # Existing pages were already judged not to answer it; gathering live
        # evidence comes before any further judgement of the same pages.
        if private_writer.prepare_private_gap(ledger, ledger.load(gap_id), catalog, notes):
            return
        ledger.transition(
            gap_id,
            "needs_local_evidence",
            reason=DIRECT_AUTHORING_REASON,
            backoff_until=_backoff_iso(86400),
        )
        notes.append(f"{gap_id}: private need; direct authoring required")
        return
    # Catalog membership only supplies candidates; closure uses the same full
    # canonical-generation and delivered semantic proof as published jobs.
    checked_existing = _is_covered(catalog, record) or record.need_class == "retrieval_defect"
    if checked_existing:
        proof = verification.verify_existing_gap(gap_id, ledger)
        if proof.outcome == "resolved" or proof.outcome in CLOSED_STATES:
            notes.append(f"existing evidence verification {gap_id}: {proof.outcome}")
            return
        if proof.outcome == "retrieval_defect":
            # The question has a canonical answer; another import cannot repair
            # delivery. Preserve the reproducible defect and its specific reason.
            notes.append(f"existing evidence verification {gap_id}: retrieval_defect; delivery repair required")
            ledger.transition_open(
                gap_id,
                "retrieval_defect",
                reason="; ".join(proof.notes),
                backoff_until=_backoff_iso(3600),
            )
            return
        if proof.outcome == "verifying" or (
            proof.outcome == "awaiting_index" and any("hashes/generations" in note for note in proof.notes)
        ):
            waits = ledger.load(gap_id).proof_wait_attempts + 1
            ledger.transition_open(
                gap_id,
                "retry_wait",
                reason="existing canonical answer proof pending; no duplicate import",
                backoff_until=_backoff_iso(_proof_pending_backoff_s(waits)),
                proof_wait_attempts=waits,
            )
            return
        if ledger.load(gap_id).proof_wait_attempts:
            ledger.update(gap_id, lambda r: setattr(r, "proof_wait_attempts", 0))
        if record.status == "retrieval_defect" and not is_local_need(record, catalog):
            ledger.transition_open(
                gap_id,
                "retrieval_defect",
                reason="; ".join(proof.notes)
                or "Existing answer could not be verified; delivery repair still required",
                backoff_until=_backoff_iso(3600),
            )
            return
    if is_local_need(record, catalog):
        if not checked_existing:
            # A paraphrase the lexical coverage check missed may already be
            # answered by a canonical page; only a proven answer is recorded.
            proof = verification.verify_existing_gap(gap_id, ledger, resolve_only=True)
            if proof.outcome == "resolved":
                notes.append(f"existing evidence verification {gap_id}: resolved before parking")
                return
        if private_writer.prepare_private_gap(ledger, ledger.load(gap_id), catalog, notes):
            return
        ledger.transition(
            gap_id,
            "needs_local_evidence",
            reason=DIRECT_AUTHORING_REASON,
            backoff_until=_backoff_iso(86400),
        )
        notes.append(f"{gap_id}: private need; direct authoring required")
        return
    if not public_discovery_allowed(record.to_dict(), catalog):
        ledger.transition(
            gap_id,
            "needs_local_evidence",
            reason="Subject or local context needs identification; public search cannot supply private facts",
            backoff_until=_backoff_iso(3600),
        )
        return
    result = discover(record.to_dict(), catalog, state_dir=ledger.root)
    record = ledger.load(gap_id)

    def gathered(r):
        for seed in result.seeds:
            current = seed.to_dict()
            previous = r.source_candidates
            attached = next(
                (c for c in previous if c.get("url") == current["url"] and explicitly_attached_source(c)),
                None,
            )
            if attached is not None:
                current["attachment"] = "task-documentation-v1"
                if attached.get("origin") == "task-documentation":
                    current["origin"] = attached["origin"]
                if "note" in attached:
                    current["note"] = attached["note"]
            position = (
                next((i for i, c in enumerate(previous) if c is attached), len(previous))
                if attached is not None
                else next(
                    (i for i, c in enumerate(previous) if c.get("url") == current["url"]),
                    len(previous),
                )
            )
            position = sum(c.get("url") != current["url"] for c in previous[:position])
            r.source_candidates = [c for c in previous if c.get("url") != current["url"]]
            r.source_candidates.insert(position, current)
        if not (result.status == "unresolved" and result.outage):
            r.research_attempts += 1

    record = ledger.update(gap_id, gathered)
    notes.append(
        f"discovery for {gap_id}: {result.status} "
        f"({len(result.seeds)} seeds, {result.requests_used} requests, "
        f"{result.model_calls} model calls)"
    )
    if result.status == "unresolved" and result.outage:
        # The model endpoint was unreachable, so nothing was judged: a short fixed
        # wait (OUTAGE_BACKOFF_S, 5 min), and the attempt is not counted against the
        # research retry ladder (GapLedger.backoff_state: 5 min, 30 min, 2 h, then
        # daily). Private-page attempts use their own ladder
        # (private_writer.PRIVATE_RETRY_BACKOFF_S: 1 h, 6 h, 24 h, then daily).
        ledger.transition(
            gap_id,
            "retry_wait",
            reason=OUTAGE_REASON,
            backoff_until=_backoff_iso(OUTAGE_BACKOFF_S),
        )
        return
    if result.status == "unresolved":
        ledger.transition(
            gap_id,
            "retry_wait",
            reason="; ".join(result.notes)[:400] or "no seeds found",
            backoff_until=_backoff_iso(ledger.backoff_state(record.research_attempts)),
        )
        return
    ledger.transition(
        gap_id,
        "ready_for_curation",
        reason=f"{len(result.seeds)} authoritative seed(s) gathered",
    )


#: Discovery found nothing only because the inference endpoint was unreachable.
OUTAGE_REASON = "discovery paused: inference endpoint unreachable"
OUTAGE_BACKOFF_S = 300


def _backoff_iso(seconds: float) -> str:
    from datetime import timedelta

    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


_JOBLESS_REVERIFY_STATES = ("verifying", "retrieval_defect", "needs_local_evidence")
_ACTIVE_SUBMISSION_STATES = ("staged", "reserved", "submitting", "uncertain", "accepted")
#: Outcomes reached without an inference call; the next gap may still be tried.
_NO_MODEL_OUTCOMES = ("awaiting_index",)
_REVERIFY_BASE_S = 300
_REVERIFY_CAP_S = 6 * 3600
#: A proof-pending existing answer waits 5 min, doubling, capped at 6 h.
_PROOF_PENDING_BASE_S = 300
_PROOF_PENDING_CAP_S = 6 * 3600
#: No soft research budget: a cycle works until no eligible need remains. The
#: systemd unit's TimeoutStartSec=8h only kills a hung run.
DEFAULT_CYCLE_BUDGET_S = math.inf


def _proof_pending_backoff_s(waits: int) -> float:
    """5 minutes, doubling with every consecutive proof-pending wait, capped at 6 h."""
    return float(min(_PROOF_PENDING_BASE_S * 2 ** max(0, waits - 1), _PROOF_PENDING_CAP_S))


def _awaits_first_evidence(record) -> bool:
    """A parked private need that has never had live evidence gathered.

    Re-judging the same existing pages cannot change their verdict; such a gap
    must reach the private writer before any further jobless re-verification.
    """
    return (
        record.status == "needs_local_evidence"
        and record.private_attempts == 0
        and not record.job_links
        and record.submission.get("state") not in _ACTIVE_SUBMISSION_STATES
    )


def cycle_budget_s(cfg=None) -> float:
    """Research time limit per cycle: unlimited (`math.inf`) by default, bounded
    only if `gap_worker.cycle_budget_s` is configured."""
    try:
        cfg = cfg or load_config()
    except Exception:  # noqa: BLE001 — an unreadable config keeps the default
        return DEFAULT_CYCLE_BUDGET_S
    value = getattr(getattr(cfg, "gap_worker", None), "cycle_budget_s", None)
    if value is None:
        return DEFAULT_CYCLE_BUDGET_S
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_CYCLE_BUDGET_S
    return value if value > 0 else DEFAULT_CYCLE_BUDGET_S


def _reverify_backoff_s(attempts: int) -> float:
    """5 minutes, doubling with every non-resolved re-verification, capped at 6 h."""
    return float(min(_REVERIFY_BASE_S * 2 ** max(0, attempts - 1), _REVERIFY_CAP_S))


def _next_reverify_attempt(record) -> int:
    """Attempt number of the re-verification about to run (persisted counter + 1)."""
    return int(record.resolution_verification.get("reverify_attempts", 0)) + 1


def _defer_reverification(ledger: GapLedger, gap_id: str, attempts: int, *, floor_s: float = 0.0) -> None:
    """The one backoff every non-resolved re-verification path uses.

    Persists the attempt counter and waits 5 min doubling to 6 h (never less
    than `floor_s`). Call after the verifier ran: it replaces
    `resolution_verification`, so the counter is written afterwards.
    """
    wait = max(_reverify_backoff_s(attempts), floor_s)

    def retain(r):
        r.resolution_verification["reverify_attempts"] = attempts
        r.backoff_until = _backoff_iso(wait)

    ledger.update_open(gap_id, retain)


def _no_candidate_floor_s(record, proof) -> float:
    """Minimum wait when no model ran (no candidate page, or index lag)."""
    if any("hashes/generations" in note for note in proof.notes):
        return 0.0  # index lag: the escalating backoff alone applies
    return 86400.0 if record.status == "needs_local_evidence" else 3600.0


def _requeue_uncovered(ledger, gap_id, outcome, reason, **updates):
    """Send a gap whose existing or published pages do not answer it back to research.

    A private partial was already parked by verification as needs_local_evidence (direct
    authoring from local evidence); it keeps that status. Everything else goes to candidate.
    """
    if outcome == "partial" and ledger.load(gap_id).status == "needs_local_evidence":
        return
    ledger.transition_open(gap_id, "candidate", reason=reason, **updates)


def _reverify_jobless(ledger: GapLedger, notes: list[str]) -> set[str]:
    """Re-verify gaps with no curator job that wait in a verifier-owned state.

    Without a job `_verify_published` has nothing to poll, so these would sit
    forever. Eligible gaps (backoff passed) go oldest-backoff first so one gap
    that stays `verifying` cannot starve the rest. Every non-resolved outcome
    bumps a persisted attempt counter and backs off 5 min doubling to 6 h. A
    cheap outcome (no candidate page, index lag) does not stop the sweep; the
    first outcome that used the model ends it, bounding inference per cycle.
    Returns the gaps judged this cycle; research must not judge them again.
    """
    done: set[str] = set()
    eligible = [
        row
        for row in ledger.list_records()
        if row.status in _JOBLESS_REVERIFY_STATES
        and not row.job_links
        and row.submission.get("state") not in _ACTIVE_SUBMISSION_STATES
        and _backoff_passed(row)
        # Evidence is gathered before the same pages are judged again.
        and not _awaits_first_evidence(row)
    ]
    eligible.sort(key=lambda r: (r.backoff_until or "", r.gap_id))
    for row in eligible:
        attempts = _next_reverify_attempt(row)
        done.add(row.gap_id)
        try:
            proof = verification.verify_existing_gap(row.gap_id, ledger)
        except Exception as exc:  # noqa: BLE001 — durable uncertainty/worker isolation
            proof = None
            notes.append(f"existing evidence verification {row.gap_id} unavailable: {type(exc).__name__}")
        else:
            notes.append(f"existing evidence verification {row.gap_id}: {proof.outcome}")
        if proof is not None and proof.outcome == "resolved":
            break
        floor = _no_candidate_floor_s(row, proof) if proof is not None and proof.outcome in _NO_MODEL_OUTCOMES else 0.0
        if proof is not None and proof.outcome in ("partial", "failed"):
            # Back to research on its backoff; the cycle does not judge it twice.
            _requeue_uncovered(
                ledger,
                row.gap_id,
                proof.outcome,
                "Existing pages do not answer this entire question; research remaining need",
            )
        _defer_reverification(ledger, row.gap_id, attempts, floor_s=floor)
        if proof is None or proof.outcome not in _NO_MODEL_OUTCOMES:
            break
    return done


def _verify_published(ledger: GapLedger, dispatcher: GapDispatcher, notes: list[str], *, catalog=None) -> set[str]:
    from doclayer.curator_reply import parse_job_reply

    attempted = set()
    for record in ledger.list_records():
        delivery_pending = (
            record.status in ("retry_wait", "retrieval_defect")
            and record.resolution_verification.get("outcome") == "retrieval_defect"
            and record.resolution_verification.get("publication_commit")
            and record.job_links
            and record.submission.get("job_id") == record.job_links[-1].get("job_id")
            and record.submission.get("remote_status") in ("published", "partial", "covered")
        )
        if record.status not in ("awaiting_index", "curating", "verifying") and not delivery_pending:
            continue
        if delivery_pending:
            attempted.add(record.gap_id)
        # A legacy research backoff must not hide a published delivery defect.
        # After that one recovery, use the ordinary delivery-repair backoff.
        if delivery_pending and record.status == "retrieval_defect" and not _backoff_passed(record):
            continue
        latest = record.job_links[-1] if record.job_links else None
        if not latest:
            continue
        if record.status == "verifying" and not _backoff_passed(record):
            continue  # a pending verdict waits out its escalating backoff
        try:
            reply = dispatcher.client().call("status", {"job_id": latest["job_id"]}, timeout_s=60)
            job = parse_job_reply(reply)
            if job["job_id"] != latest["job_id"]:
                raise ValueError("foreign job receipt")
        except Exception as exc:  # noqa: BLE001 — durable uncertainty/worker isolation
            notes.append(f"job status unavailable for {record.gap_id}: {type(exc).__name__}")
            continue
        dispatcher.observe(record.gap_id, job)
        status = job["status"]
        if status in ("published", "partial", "covered"):
            # The record came from a listing taken before the status poll; a
            # suppress/supersede landing meanwhile must not be overwritten.
            if (
                ledger.transition_open(
                    record.gap_id,
                    "awaiting_index",
                    reason=f"curator terminal {status}; semantic verification required",
                )
                is None
            ):
                notes.append(f"gap {record.gap_id} closed during status poll; not verified")
                continue
            attempted.add(record.gap_id)
            attempts = _next_reverify_attempt(record)
            proof = verification.verify_gap(record.gap_id, ledger)
            if proof.outcome == "verifying":
                _defer_reverification(ledger, record.gap_id, attempts)
            if proof.outcome == "retrieval_defect":
                ledger.transition_open(
                    record.gap_id,
                    "retrieval_defect",
                    reason="; ".join(proof.notes),
                    backoff_until=_backoff_iso(3600),
                )
            if proof.outcome in ("partial", "failed") and is_local_need(record, catalog):
                _requeue_uncovered(
                    ledger,
                    record.gap_id,
                    proof.outcome,
                    "Published page did not answer the local need; collect current local evidence before another submission",
                )
            notes.append(f"verification attempted for {record.gap_id}")
        elif status in ("failed", "cancelled"):
            current = ledger.load(record.gap_id)
            if current.submission.get("payload", {}).get("private") is True:
                # Never failed only for having tried: gather again on a growing backoff.
                ledger.transition_open(
                    record.gap_id,
                    "needs_local_evidence",
                    reason=f"private curator job {status}",
                    backoff_until=_backoff_iso(private_writer.private_retry_backoff_s(current.private_attempts)),
                )
            else:
                ledger.transition_open(record.gap_id, "failed", reason=f"curator job {status}")
        elif status == "retryable":
            # Every stop either recovers (bounded resume) or releases its slot
            # (allowance retired, gap back on a backoff); none holds it silently.
            # A job that has started publication is never retired or replaced:
            # the same job is resumed on a growing backoff and keeps its slot.
            if publication_started(job):
                outcome = dispatcher.resume_publication(record.gap_id, job)
                notes.append(f"curator job {latest['job_id']} {outcome.action}: {outcome.detail}; active slot retained")
                continue
            if job.get("error") == "limit reached: model_calls" or job.get("automatic_stop"):
                outcome = dispatcher.retire_exhausted(record.gap_id, job)
                if outcome.action != "parked":
                    notes.append(f"curator job {latest['job_id']} {outcome.action}: {outcome.detail}")
                    continue
            outcome = dispatcher.resume_outage(record.gap_id, job)
            retained = outcome.action not in RETIREMENT_CATEGORIES
            notes.append(
                f"curator job {latest['job_id']} {outcome.action}: {outcome.detail}"
                + ("; active slot retained" if retained else "")
            )

    return attempted


_RESEARCH_STATES = ("candidate", "retry_wait", "needs_local_evidence", "retrieval_defect")
#: Statuses a captured chat remark may hold when it is suppressed.
_REMARK_OPEN_STATES = _RESEARCH_STATES + ("failed",)


_INFRA_DOC_WORDS = re.compile(
    r"\b(documentation|documented|document|docs|guide|reference|installed|installation|install|deployment|deployed|hosted|access routes|configuration|config|storage|filesystem|ssh|corpus audit)\b",
    re.I,
)


def _infrastructure_documentation_need(question: str) -> bool:
    """Whether an infrastructure question asks for documentation, not availability.

    Lane 3's `question_scope.infrastructure_documentation_need` owns the rule;
    this word list stands in only until that function is present.
    """
    from doclayer import question_scope

    rule = getattr(question_scope, "infrastructure_documentation_need", None)
    if rule is not None:
        return bool(rule(question))
    return bool(_INFRA_DOC_WORDS.search(question or ""))


def _research_order(record) -> tuple:
    """Never-claimed needs first, then the least recently claimed; recurrence breaks ties."""
    return (record.last_claimed_at or "", -record.recurrence, record.gap_id)


def _research_eligible(record, catalog) -> bool:
    if record.need_class == "ambiguous":
        return False
    if record.need_class == "infrastructure" and not _infrastructure_documentation_need(record.representative_question):
        return False  # An availability incident is not new documentation.
    if record.status not in _RESEARCH_STATES or record.submission.get("state") in _ACTIVE_SUBMISSION_STATES:
        return False
    if not _backoff_passed(record) and not (
        record.status == "retry_wait"
        and is_local_need(record, catalog)
        and record.attempts == 0
        and record.proof_wait_attempts == 0  # a proof-pending wait is honoured
        and not record.submission
        and not record.job_links
    ):
        return False
    return True


def _suppress_chat_remarks(ledger: GapLedger, notes: list[str]) -> None:
    """A captured operator chat remark is not a documentation need: suppress it once."""
    for row in ledger.list_records():
        if (
            row.status in _REMARK_OPEN_STATES
            and not row.job_links
            and row.submission.get("state") not in _ACTIVE_SUBMISSION_STATES
            and is_chat_remark(row.representative_question)
        ):
            if (
                ledger.transition_open(row.gap_id, "suppressed", reason=NOT_A_DOCUMENTATION_NEED, backoff_until=None)
                is not None
            ):
                notes.append(f"{row.gap_id}: suppressed; {NOT_A_DOCUMENTATION_NEED}")


def _run_cycle(ledger: GapLedger, *, worker: str = "cycle", budget_s: float | None = None) -> dict[str, Any]:
    """One full worker cycle. Safe to run concurrently; one dispatch claim.

    Research handles every eligible need, least-recently-claimed first. By
    default there is no time limit (`cycle_budget_s()` is `math.inf`); a
    configured `budget_s` / `gap_worker.cycle_budget_s` bounds it.
    """
    started = time.monotonic()
    budget = cycle_budget_s() if budget_s is None else budget_s
    catalog = load_catalog(settings.brain_repo, settings.catalog_cache_dir, settings.routing_policy_path)
    hosts = ssh_host_aliases(Path("~/.ssh/config").expanduser(), settings.catalog_cache_dir)
    dispatcher = GapDispatcher(ledger, policy_version=catalog.policy_version, catalog=catalog)
    notes: list[str] = []
    if hasattr(ledger, "reconcile_duplicates"):
        ledger.reconcile_duplicates()

    # A grouped question never inherits another question's successful proof.
    # Retry its own canonical/delivered verification, including index lag, even
    # though it deliberately has no duplicate curator job to poll.
    reverified: set[str] = set()
    for row in ledger.list_records():
        if (
            getattr(row, "supersession", {}).get("verification_required")
            and row.status not in ("superseded", "resolved", "suppressed", "failed")
            and not row.job_links
            and _backoff_passed(row)
        ):
            attempts = _next_reverify_attempt(row)
            proof = verification.verify_existing_gap(row.gap_id, ledger)
            reverified.add(row.gap_id)
            notes.append(f"grouped need verification {row.gap_id}: {proof.outcome}")
            if proof.outcome in ("partial", "failed"):

                def uncovered(r):
                    r.supersession["verification_required"] = False
                    r.supersession["independent_outcome"] = proof.outcome

                ledger.update_open(row.gap_id, uncovered)
                _requeue_uncovered(
                    ledger,
                    row.gap_id,
                    proof.outcome,
                    "Grouped publication does not answer this entire question; research remaining need",
                    backoff_until=None,
                )
            elif proof.outcome == "retrieval_defect":
                ledger.transition_open(
                    row.gap_id,
                    "retrieval_defect",
                    reason="; ".join(proof.notes),
                    backoff_until=_backoff_iso(3600),
                )
            if proof.outcome not in (
                "resolved",
                "failed",
                "partial",
                "retrieval_defect",
            ):
                ledger.transition_open(row.gap_id, "verifying", reason="; ".join(proof.notes))
                _defer_reverification(ledger, row.gap_id, attempts)
            break
    jobless_verified = _reverify_jobless(ledger, notes) if not reverified else set()
    if ledger.spool_degraded():
        notes.append("capture degraded: spool exceeds its cap; inspect and drain the spool")

    # Recover only expired local work; remote uncertainty is recovered by its
    # immutable submission, never by generating another attempt.
    for row in ledger.list_records():
        lease = row.lease or {}
        if lease.get("expires_at", "") > now_iso():
            continue
        if row.status == "researching":
            ledger.transition(
                row.gap_id,
                "candidate",
                reason="expired research lease recovered",
                lease=None,
            )
        elif row.status == "awaiting_curator_readiness" and dispatcher.curator_ready():
            ledger.transition(row.gap_id, "ready_for_curation", reason="curator readiness activated")
    verified_this_cycle = _verify_published(ledger, dispatcher, notes, catalog=catalog)
    verified_this_cycle |= jobless_verified

    # Ready/parked submissions and remote uncertainty have priority over new
    # research: they are dispatched first. Research then handles every eligible
    # need, least-recently-claimed first (unbounded unless a budget is
    # configured), and a need it makes ready is dispatched at once while a slot
    # is free, so curation never idles behind a long research phase. Research
    # is skipped only when every slot is busy and a ready backlog already waits.
    with dispatcher_lock(ledger):
        free_before_research = _slot_free(dispatcher)
    if free_before_research:
        _dispatch_ready(ledger, dispatcher, worker, notes)
    with dispatcher_lock(ledger):
        saturated = not _slot_free(dispatcher) and any(_awaits_dispatch(r) for r in ledger.list_records())
    _suppress_chat_remarks(ledger, notes)
    research = [] if saturated else ledger.list_records()
    claimed_this_cycle = 0
    for listed in sorted(research, key=_research_order):
        if time.monotonic() - started >= budget:
            notes.append(f"research budget of {budget:.0f}s spent after {claimed_this_cycle} need(s)")
            break
        if listed.gap_id in verified_this_cycle:
            continue
        try:
            # Earlier needs in this cycle may have changed this record.
            record = ledger.load(listed.gap_id)
        except FileNotFoundError:
            continue
        if not _research_eligible(record, catalog):
            continue
        claimed = ledger.claim(record.gap_id, worker, ttl_s=600)
        if claimed is None:
            continue
        claimed_this_cycle += 1
        try:
            _research_one(ledger, claimed, catalog, hosts, notes)
        except Exception as exc:  # noqa: BLE001 — durable uncertainty/worker isolation
            error_name = type(exc).__name__

            def retry(r, error_name=error_name):
                r.research_attempts += 1
                r.status = "retry_wait"
                r.backoff_until = _backoff_iso(ledger.backoff_state(r.research_attempts))
                r.failure_reason = f"research unavailable: {error_name}"

            ledger.update(record.gap_id, retry)
            notes.append(f"research retained {record.gap_id}: {type(exc).__name__}")
        finally:
            ledger.release_lease(record.gap_id, worker)
        # A need research just made ready goes to a free slot now, not after
        # the whole research phase.
        with dispatcher_lock(ledger):
            if ledger.load(record.gap_id).status == "ready_for_curation" and _slot_free(dispatcher):
                outcome = dispatcher.dispatch(record.gap_id, worker=worker)
                notes.append(f"dispatch {record.gap_id}: {outcome.action} {outcome.detail}".strip())

    # Then dispatch whatever is still ready while the dispatcher has free slots.
    _dispatch_ready(ledger, dispatcher, worker, notes)

    # Publications were verified (step 2) before any dispatch, so one cycle
    # cannot verify twice.

    # 5. Housekeeping.
    pruned = ledger.prune_resolved_detail()
    if pruned:
        notes.append(f"pruned verbose detail from {pruned} resolved gap(s)")

    summary = ledger.summary()
    summary["notes"] = notes
    summary["dispatch_paused"] = dispatcher.paused()
    summary["curator_ready"] = dispatcher.curator_ready()
    return summary


def _awaits_dispatch(record) -> bool:
    """Ready, staged, or holding an unfinished submission the dispatcher owns."""
    return record.status in (
        "ready_for_curation",
        "queued",
        "awaiting_curator_readiness",
    ) or record.submission.get("state") in ("staged", "reserved", "submitting", "uncertain")


def _slot_free(dispatcher: GapDispatcher) -> bool:
    """A real submission could start now: curator ready, not paused, a slot open."""
    return (
        dispatcher.curator_ready()
        and not dispatcher.paused()
        and len(dispatcher.active_all()) < dispatcher.max_active_jobs()
    )


def _dispatch_ready(ledger: GapLedger, dispatcher: GapDispatcher, worker: str, notes: list[str]) -> None:
    """Offer ready gaps (recovering submissions first) to the dispatcher's free slots."""
    with dispatcher_lock(ledger):
        ready = [
            record
            for record in ledger.list_records()
            if record.status in ("ready_for_curation", "queued")
            or (
                record.status == "retry_wait"
                and record.submission.get("state") in ("staged", "reserved", "submitting", "uncertain")
                and _backoff_passed(record)
            )
        ]
        ready.sort(
            key=lambda r: (
                0
                if r.submission.get("state") in ("reserved", "submitting", "uncertain")
                else 1
                if r.submission.get("state") == "staged"
                else 2,
                -r.recurrence,
                r.gap_id,
            )
        )
        # At most `max_active_jobs` candidates are offered per pass; the
        # dispatcher itself enforces the slot limit (one slot by default).
        for record in ready[: dispatcher.max_active_jobs()]:
            outcome = dispatcher.dispatch(record.gap_id, worker=worker)
            notes.append(f"dispatch {record.gap_id}: {outcome.action} {outcome.detail}".strip())
            if outcome.action in ("dry-run", "paused"):
                break


def run_cycle(
    ledger: GapLedger | None = None, *, worker: str = "cycle", budget_s: float | None = None
) -> dict[str, Any]:
    ledger = ledger or GapLedger(settings.gap_state_dir)
    with ledger.lock("worker-cycle"):
        return _run_cycle(ledger, worker=worker, budget_s=budget_s)


def dispatcher_lock(ledger: GapLedger):
    return ledger.lock("dispatcher")


def digest(summary: dict[str, Any]) -> str:
    """One concise periodic digest — never the whole ledger."""
    lines = [
        (
            f"knowledge gaps @ {summary.get('state_dir', '')}: "
            f"{summary.get('total', 0)} record(s), {summary.get('outstanding', 0)} outstanding"
        ),
    ]
    by_status = summary.get("by_status", {})
    if by_status:
        lines.append("  statuses: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    by_need = summary.get("by_need", {})
    if by_need:
        lines.append("  needs: " + ", ".join(f"{k}={v}" for k, v in sorted(by_need.items())))
    if summary.get("spool_degraded"):
        lines.append("  ⚠ capture degraded: spool over cap")
    if summary.get("quarantined"):
        lines.append(f"  ⚠ {summary['quarantined']} quarantined record(s)")
    if summary.get("resource_exhausted"):
        lines.append(f"  {summary['resource_exhausted']} unresolved need(s) parked at their automatic allowance")
    if summary.get("model_response_unresolved"):
        lines.append(f"  {summary['model_response_unresolved']} unresolved need(s) parked after a model task failed")
    if summary.get("publication_pending"):
        lines.append(f"  {summary['publication_pending']} gap job(s) hold their slot while publication retries")
    if summary.get("stopped_unresolved"):
        lines.append(f"  {summary['stopped_unresolved']} need(s) retrying after a stopped curator job")
    if summary.get("dispatch_paused"):
        lines.append("  dispatch paused (manual)")
    if not summary.get("curator_ready"):
        lines.append("  dispatch is dry-run: curator readiness not proven")
    for note in summary.get("notes", [])[:8]:
        lines.append(f"  - {note}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Knowledge-gap worker: a five-minute cycle of coverage rechecks, verification and dispatch"
    )
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--loop", action="store_true", help="run cycles on a timer")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_S)
    args = parser.parse_args(argv)

    if args.once:
        print(digest(run_cycle()))
        return 0
    if not args.loop:
        parser.error("choose --once or --loop")
    while True:
        try:
            print(digest(run_cycle()), flush=True)
        except Exception as exc:  # noqa: BLE001 — the worker restarts cleanly
            print(f"cycle failed: {exc}", file=sys.stderr, flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
