"""Fixed JSON-on-stdin operations for the owning gap-ledger host."""

from __future__ import annotations

import json
import sys
from contextlib import ExitStack
from typing import Any

from doclayer.gaps import GapLedger, explicitly_attached_source
from doclayer.settings import settings


def _holds_attempt(record, submission, *, idle_verifying=False) -> bool:
    """True while an attempt owns the gap and it must recover through that attempt.

    `verifying` is the verifier's own state. With `idle_verifying`, a verifying
    gap that holds no lease and has no in-flight curator job is not owned by
    anything, so an operator may act on it (suppress, supersede).
    """
    if record.lease or submission.get("state") in ("reserved", "submitting", "uncertain", "accepted"):
        return True
    if record.status in ("queued", "curating", "awaiting_index"):
        return True
    if record.status == "verifying":
        # A staged submission or a linked job is in flight until its submission is
        # terminal or abandoned.
        return not idle_verifying or bool(
            submission.get("state") == "staged"
            or (record.job_links and submission.get("state") not in ("terminal", "abandoned"))
        )
    return False


def handle(operation: str, payload: dict[str, Any], ledger: GapLedger):
    if operation == "submit_event":
        record, created = ledger.submit_event(**payload)
        return {"ok": True, "record": record.to_dict(), "created": created}
    if operation == "summary":
        from doclayer.dispatcher import GapDispatcher
        from doclayer.gap_budget import today

        dispatcher = GapDispatcher(ledger)
        summary = ledger.summary()
        summary.update(
            dispatch_paused=dispatcher.paused(),
            curator_ready=dispatcher.curator_ready(),
            daily_quotas_enabled=False,
            daily_usage=dispatcher.day_ledger.load(today()),
        )
        return {"ok": True, "summary": summary}
    if operation == "list":
        return {"ok": True, "records": [r.to_dict() for r in ledger.list_records()]}
    if operation == "inspect":
        return {"ok": True, "record": ledger.load(payload["gap_id"]).to_dict()}
    if operation == "attach_source":
        from urllib.parse import urlsplit

        source = dict(payload["source"])
        source["attachment"] = "task-documentation-v1"
        if urlsplit(source.get("url", "")).scheme not in ("http", "https"):
            raise ValueError("source must be an HTTP documentation URL")
        with ledger.lock(payload["gap_id"]):
            record = ledger.load(payload["gap_id"])
            old = record.source_candidates
            prior = next(
                (i for i, c in enumerate(old) if c.get("url") == source["url"] and explicitly_attached_source(c)),
                None,
            )
            record.source_candidates = [c for c in old if c.get("url") != source["url"]]
            if prior is None:
                record.source_candidates.append(source)
            else:
                # Refreshing an attachment does not change the operator's order.
                position = sum(c.get("url") != source["url"] for c in old[:prior])
                record.source_candidates.insert(position, source)
            ledger.save(record)
        return {"ok": True, "record": record.to_dict()}
    if operation in ("pause", "resume"):
        from doclayer.dispatcher import GapDispatcher

        GapDispatcher(ledger).set_paused(operation == "pause")
        return {"ok": True}
    if operation in ("retry", "suppress"):
        with ledger.lock(payload["gap_id"]):
            record = ledger.load(payload["gap_id"])
            submission = getattr(record, "submission", {}) or {}
            if _holds_attempt(record, submission, idle_verifying=operation == "suppress"):
                raise ValueError("active gap must recover through its existing attempt")
            if operation == "retry":
                proof = record.resolution_verification
                remote = submission.get("remote_status")
                # Retrying delivery of a verified terminal publication keeps its
                # owning job; it is not permission to import the same sources again.
                verified_terminal = (
                    submission.get("state") == "terminal"
                    and record.job_links
                    and submission.get("job_id") == record.job_links[-1].get("job_id")
                    and proof.get("index_acknowledged") is True
                    and proof.get("verified_paths")
                    and (
                        (
                            remote in ("published", "partial")
                            and isinstance(record.publication_revision, str)
                            and len(record.publication_revision) == 40
                            and proof.get("publication_commit") == record.publication_revision
                        )
                        or (remote == "covered" and proof.get("basis") == "existing canonical evidence (covered no-op)")
                    )
                )
                if verified_terminal:
                    record = ledger.transition(
                        record.gap_id,
                        "awaiting_index",
                        backoff_until=None,
                        proof_wait_attempts=0,
                        reason="retry existing publication verification; no duplicate import",
                    )
                    return {"ok": True, "record": record.to_dict()}
                if record.submission.get("state") == "terminal":
                    record = ledger.update(record.gap_id, lambda r: r.submission.update(manual_retry=True))
                record = ledger.transition(
                    record.gap_id,
                    "queued" if submission.get("state") == "staged" else "candidate",
                    backoff_until=None,
                    proof_wait_attempts=0,
                )
            else:
                record = ledger.transition(record.gap_id, "suppressed", reason=payload.get("reason"))
        return {"ok": True, "record": record.to_dict()}
    if operation == "supersede":
        primary = payload["by"]
        if primary == payload["gap_id"]:
            raise ValueError("a gap cannot supersede itself")
        for gap_id in (payload["gap_id"], primary):
            if not ledger.record_path(gap_id).exists():
                raise ValueError(f"unknown gap: {gap_id}")
        if ledger.load(primary).status == "superseded":
            raise ValueError("primary is itself superseded; supersede into its primary instead")
        # Both gap locks, in sorted id order so concurrent opposite supersedes
        # cannot deadlock. Everything is re-checked under both: the primary can
        # only change while its lock is free, and the target's transition re-enters
        # its (thread-reentrant) lock.
        with ExitStack() as locks:
            for gap_id in sorted({payload["gap_id"], primary}):
                locks.enter_context(ledger.lock(gap_id))
            if not ledger.record_path(primary).exists():
                raise ValueError(f"unknown gap: {primary}")
            if ledger.load(primary).status == "superseded":
                raise ValueError("primary is itself superseded; supersede into its primary instead")
            record = ledger.load(payload["gap_id"])
            submission = getattr(record, "submission", {}) or {}
            if _holds_attempt(record, submission, idle_verifying=True):
                raise ValueError("active gap must recover through its existing attempt")
            record = ledger.transition(
                record.gap_id,
                "superseded",
                reason=payload.get("reason") or f"duplicate of {primary}",
                superseded_by=primary,
            )
        return {"ok": True, "record": record.to_dict()}
    raise ValueError("unsupported knowledge-gap operation")


def main():
    try:
        # Bound protocol input; source text is not a shell command.
        raw = sys.stdin.read(16_385)
        if len(raw) > 16_384:
            raise ValueError("request exceeds limit")
        data = json.loads(raw)
        reply = handle(
            data["operation"],
            data.get("payload", {}),
            GapLedger(settings.gap_state_dir),
        )
    except Exception as exc:
        reply = {"ok": False, "error": str(exc)[:300]}
    print(json.dumps(reply, ensure_ascii=False))
