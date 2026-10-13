"""Operations CLI for the knowledge-gap loop.

status / list / inspect / retry / suppress / force-refresh / discover /
dispatch-pause / dispatch-resume / verify / digest. Suppressing future
imports is distinct from deleting evidence or suppressing user access: it
only pauses a gap's automatic handling. MCP stays small; the CLI carries the
operational surface.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from doclayer.discovery import discover
from doclayer.dispatcher import GapDispatcher
from doclayer.gaps import GapLedger
from doclayer.settings import settings


def _ledger(args) -> GapLedger:
    return GapLedger(getattr(args, "state_dir", None) or settings.gap_state_dir)


def _catalog():
    from doclayer.coverage import load_catalog

    return load_catalog(settings.brain_repo, settings.catalog_cache_dir, settings.routing_policy_path)


def cmd_status(args) -> int:
    ledger = _ledger(args)
    summary = ledger.summary()

    dispatcher = GapDispatcher(ledger)
    summary["dispatch_paused"] = dispatcher.paused()
    summary["curator_ready"] = dispatcher.curator_ready()
    summary["daily_quotas_enabled"] = False
    summary["historical_planned_calls_today"] = _reserved(ledger)
    print(json.dumps(summary, indent=2))
    return 0


def _reserved(ledger: GapLedger) -> int:
    from doclayer.dispatcher import DayLedger

    return DayLedger(ledger.root).reserved_calls()


def cmd_list(args) -> int:
    ledger = _ledger(args)
    for record in ledger.list_records():
        print(
            f"{record.gap_id}  {record.status:<28} {record.need_class:<20} "
            f"{(record.canonical_subject or record.identity)[:32]:<32} "
            f"seen {record.recurrence}x  {record.last_seen[:10]}"
        )
    return 0


def cmd_inspect(args) -> int:
    ledger = _ledger(args)
    try:
        record = ledger.load(args.gap_id)
    except Exception as exc:  # noqa: BLE001
        print(f"inspect failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(record.to_dict(), indent=2))
    events = ledger.events_path(record.gap_id)
    if events.exists():
        print("--- event trail (tail) ---")
        lines = events.read_text().splitlines()
        for line in lines[-10:]:
            print(line)
    return 0


def cmd_retry(args) -> int:
    from doclayer.gap_ops import handle

    ledger = _ledger(args)
    record = handle("retry", {"gap_id": args.gap_id}, ledger)["record"]
    # gap_ops owns the retry transition; reset the private evidence budget in
    # the same CLI operation so an explicit retry starts a fresh private cycle.
    ledger.update(args.gap_id, lambda r: setattr(r, "private_attempts", 0))
    print(f"{record['gap_id']}: backoff cleared; the worker will pick it up")
    return 0


def cmd_suppress(args) -> int:
    """Suppress future automatic imports while preserving active attempts."""
    from doclayer.gap_ops import handle

    record = handle(
        "suppress",
        {"gap_id": args.gap_id, "reason": args.reason or "manual suppression"},
        _ledger(args),
    )["record"]
    print(f"{record['gap_id']}: suppressed — capture still records events")
    return 0


def cmd_force_refresh(args) -> int:
    """Clear suppression/backoff and re-run the representative question now."""
    ledger = _ledger(args)
    record = ledger.load(args.gap_id)
    import asyncio

    from doclayer.resolver import resolve

    answer = asyncio.run(resolve(record.representative_question, force=True, capture=False))
    print(answer.markdown())
    return 0


def cmd_discover(args) -> int:
    ledger = _ledger(args)
    record = ledger.load(args.gap_id)
    result = discover(record.to_dict(), _catalog(), state_dir=ledger.root)
    print(
        json.dumps(
            {
                "gap_id": result.gap_id,
                "status": result.status,
                "seeds": [s.to_dict() for s in result.seeds],
                "requests_used": result.requests_used,
                "model_calls": result.model_calls,
                "notes": result.notes,
            },
            indent=2,
        )
    )
    return 0


def cmd_dispatch_pause(args) -> int:
    GapDispatcher(_ledger(args)).set_paused(True)
    print("automatic dispatch paused; capture and answering continue")
    return 0


def cmd_dispatch_resume(args) -> int:
    GapDispatcher(_ledger(args)).set_paused(False)
    print("automatic dispatch resumed")
    return 0


_EXISTING_VERIFY_REFUSED = (
    "superseded",
    "suppressed",
    "resolved",
    "researching",
    "queued",
    "curating",
)
_ACTIVE_SUBMISSION_STATES = ("staged", "reserved", "submitting", "uncertain", "accepted")


def _existing_verify_refusal(record):
    """Why `verify --existing` must not touch this record, or None.

    `verifying` is the verifier's own state and may be re-run. An
    `awaiting_index` record is refused only while a curator job still owns it.
    """
    if record.status in _EXISTING_VERIFY_REFUSED:
        return f"status is {record.status}"
    if record.status == "awaiting_index" and (
        record.job_links or record.submission.get("state") in _ACTIVE_SUBMISSION_STATES
    ):
        return "status is awaiting_index with an active curator submission"
    return None


def cmd_verify(args) -> int:
    from doclayer import verification

    ledger = _ledger(args)
    if args.existing:
        record = ledger.load(args.gap_id)
        detail = _existing_verify_refusal(record)
        if detail:
            if record.status == "superseded":
                detail += f" (primary {record.superseded_by})"
            print(
                f"refusing verify --existing on {args.gap_id}: {detail}; the record was not changed",
                file=sys.stderr,
            )
            return 2
    verify = verification.verify_existing_gap if args.existing else verification.verify_gap
    result = verify(args.gap_id, ledger)
    print(json.dumps(result.__dict__, indent=2))
    return 0 if result.outcome in ("resolved",) else 1


def cmd_supersede(args) -> int:
    from doclayer.gap_ops import handle

    record = handle(
        "supersede",
        {"gap_id": args.gap_id, "by": args.by, "reason": args.reason},
        _ledger(args),
    )["record"]
    print(f"{record['gap_id']}: superseded by {record['superseded_by']}")
    return 0


def cmd_digest(args) -> int:
    from doclayer.gap_worker import digest, run_cycle

    print(digest(run_cycle(_ledger(args))))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="doclayer-gaps")
    parser.add_argument("--state-dir", default=None, help="override the knowledge-gap state root")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="ledger summary (JSON)")
    sub.add_parser("list", help="all gap records")
    p_inspect = sub.add_parser("inspect", help="one gap record + event trail")
    p_inspect.add_argument("gap_id")
    p_retry = sub.add_parser("retry", help="clear backoff; requeue a gap")
    p_retry.add_argument("gap_id")
    p_suppress = sub.add_parser("suppress", help="suppress future automatic imports")
    p_suppress.add_argument("gap_id")
    p_suppress.add_argument("--reason", default=None)
    p_refresh = sub.add_parser("force-refresh", help="re-run the question now (force)")
    p_refresh.add_argument("gap_id")
    p_discover = sub.add_parser("discover", help="run bounded discovery for one gap")
    p_discover.add_argument("gap_id")
    sub.add_parser("dispatch-pause", help="pause automatic dispatch")
    sub.add_parser("dispatch-resume", help="resume automatic dispatch")
    p_verify = sub.add_parser("verify", help="verify a published gap end to end")
    p_verify.add_argument("gap_id")
    p_verify.add_argument(
        "--existing",
        action="store_true",
        help="verify a directly authored canonical page (no curator job)",
    )
    p_sup = sub.add_parser("supersede", help="mark a duplicate gap superseded by its primary")
    p_sup.add_argument("gap_id")
    p_sup.add_argument("--by", required=True)
    p_sup.add_argument("--reason", default=None)
    sub.add_parser("digest", help="run one worker cycle and print the digest")
    sub.add_parser("flush", help="deliver local event outbox to the owning ledger")
    args = parser.parse_args(argv)
    if args.command == "flush":
        from doclayer.gap_client import GapClient

        print(json.dumps(GapClient().flush(), indent=2))
        return 0
    if settings.gap_owner == "ssh" and not args.state_dir:
        from doclayer.gap_client import GapClient

        if args.command == "force-refresh":
            import asyncio

            from doclayer.resolver import resolve

            record = GapClient().operation("inspect", {"gap_id": args.gap_id})["record"]
            answer = asyncio.run(resolve(record["representative_question"], force=True, capture=False))
            print(answer.markdown())
            return 0
        operation = {
            "status": "summary",
            "dispatch-pause": "pause",
            "dispatch-resume": "resume",
        }.get(args.command, args.command)
        if operation not in (
            "summary",
            "list",
            "inspect",
            "retry",
            "suppress",
            "supersede",
            "pause",
            "resume",
        ):
            parser.error("run discovery/verification/digest on the owning worker host")
        payload = {key: getattr(args, key) for key in ("gap_id", "reason", "by") if hasattr(args, key)}
        print(json.dumps(GapClient().operation(operation, payload), indent=2))
        return 0
    if args.state_dir:
        args.state_dir = Path(args.state_dir).expanduser()
    handlers = {
        "status": cmd_status,
        "list": cmd_list,
        "inspect": cmd_inspect,
        "retry": cmd_retry,
        "suppress": cmd_suppress,
        "force-refresh": cmd_force_refresh,
        "discover": cmd_discover,
        "dispatch-pause": cmd_dispatch_pause,
        "dispatch-resume": cmd_dispatch_resume,
        "verify": cmd_verify,
        "supersede": cmd_supersede,
        "digest": cmd_digest,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
