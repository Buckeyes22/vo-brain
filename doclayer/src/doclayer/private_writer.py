"""Automatic private pages: plan allowlisted read-only commands
with the curator's configured model, run them, and hand the outputs to a private single-pass job."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from doclayer.coverage import early_project
from doclayer.curator.config import load_config
from doclayer.private_commands import (
    evidence_label,
    evidence_text,
    normalize,
    read_roots_text,
    refusal,
    resolved_binary,
    run_command,
)

PRIVATE_ORIGIN = "private-writer-v1"
#: Why a private need is parked without planning: no host is allowed.
NO_HOSTS_REASON = "private_writer.hosts is empty; private pages not configured"

PLAN_SYSTEM = (
    "You plan read-only shell observations that answer a question about the operator's own machines. "
    'Return JSON {"commands": [{"host": ALIAS, "argv": [..], "why": TEXT}]} with at most {max_commands} entries. '
    "Pick hosts by their roles in host_notes; use only those aliases. "
    "Each argv is ONE command as a list of strings: no pipes, redirects, ';', '&&', backticks or $(...). Paths are absolute. "
    "Allowed: dpkg-query -W -f=${Package} ${Version} ${Status}\\n PKG.. (include ${Status}: removed packages with "
    "leftover config still print a version); apt-cache policy PKG.. (empty output means apt does not know the package, "
    "not that it is uninstalled); snap list [NAME]; flatpak list; command -v NAME; NAME --version runs only when "
    "command -v NAME appears earlier in the same plan (use java --version, not -version); uname -a; lsb_release -a; hostname; df -h PATH (which filesystem holds a path); "
    "findmnt [MOUNTPOINT]; lsblk; nvidia-smi --query-gpu=FIELDS --format=csv,noheader; "
    "systemctl [--user] is-active|is-enabled UNIT..; systemctl [--user] list-units --no-pager [--all] [PATTERN]; "
    "systemctl cat UNIT; systemctl show UNIT -p PROPS; "
    "find PATH with only -maxdepth N, -name, -iname, -type f|d|l, -path and -mtime; "
    "ps -eo pid,comm; ps -eo pid,ppid,comm; free -h; free -m; ss -ltn; ss -lun; ss -ltnu; "
    "docker ps [-a] --format '{{.Names}} {{.Image}} {{.Status}} {{.HealthStatus}}'; "
    "docker inspect --type container -f '{{.State.Status}} {{.Config.Image}} {{.RestartCount}}' NAME; "
    "git -C DIR rev-parse HEAD; git -C DIR rev-parse --short HEAD; git -C DIR log --oneline -n N; "
    "git --no-optional-locks -C DIR status --short; git -C DIR branch --show-current; "
    "id [USER]; groups [USER] (group membership such as sudo); ls -la PATH; stat PATH; head -n N PATH (N<=200) "
    "for .md, .txt, .gradle, .gradle.kts, .toml, package.json or go.mod files under {read_roots} "
    "(project build files show the configured SDK, JDK and dependency versions). Only these forms run: grep, cat, tree, ls -R, sudo, "
    "find with any other option (-exec, -delete, -fprint ...) and anything else unlisted are refused. "
    "Every command's output is still withheld if it looks like a secret. "
    "Never read configuration, environment, keys or credentials. "
    "If previous_attempt_notes list missing facets, plan only for those. "
    "previous_plans lists your earlier plans for this question with the commands that were refused or withheld "
    "and why: never repeat a refused or withheld command, and never return a plan identical to an earlier one; "
    "observe something different that could answer what is still missing."
)

#: Retry waits after the Nth unsuccessful private attempt: 1 h, 6 h, 24 h, then daily.
PRIVATE_RETRY_BACKOFF_S = (3600.0, 6 * 3600.0, 24 * 3600.0)
#: Earlier plans kept on the record and shown to the planner.
PLAN_HISTORY = 5


def private_retry_backoff_s(attempts: int) -> float:
    """Wait after `attempts` unsuccessful private attempts; never longer than daily."""
    if attempts <= 0:
        return PRIVATE_RETRY_BACKOFF_S[0]
    return PRIVATE_RETRY_BACKOFF_S[min(attempts, len(PRIVATE_RETRY_BACKOFF_S)) - 1]


def plan_system(cfg) -> str:
    return PLAN_SYSTEM.replace("{max_commands}", str(cfg.max_commands)).replace("{read_roots}", read_roots_text())


def _trail_plan(plan) -> list[dict]:
    return [{"host": c.get("host"), "argv": c.get("argv"), "why": str(c.get("why", ""))[:200]} for c in plan]


def _plan_key(plan) -> list:
    return sorted(json.dumps([str(c.get("host", "")), c.get("argv")], sort_keys=True) for c in plan)


@dataclass
class Gathered:
    status: str  # ready | empty | outage
    sources: list[dict] = field(default_factory=list)
    trail: dict = field(default_factory=dict)
    reason: str = ""


def private_sources(record) -> list[dict]:
    result = []
    for source in getattr(record, "source_candidates", []):
        if not isinstance(source, dict):
            continue
        text = source.get("text")
        if (
            source.get("origin") == PRIVATE_ORIGIN
            and source.get("kind") == "text"
            and isinstance(text, str)
            and text
            and source.get("source_hash") == hashlib.sha256(text.encode()).hexdigest()
        ):
            result.append(source)
    return result


def _plan_input(record, cfg) -> str:
    attached = [
        s.get("text", "")[:2000]
        for s in record.source_candidates
        if isinstance(s, dict) and s.get("origin") != PRIVATE_ORIGIN and s.get("text")
    ]
    return json.dumps(
        {
            "question": record.representative_question,
            "facets": record.facets,
            "host_notes": {host: cfg.host_notes.get(host, "") for host in cfg.hosts},
            "attached_evidence": attached[:4],
            "previous_attempt_notes": list((record.resolution_verification or {}).get("notes", []))[:6],
            "previous_plans": [
                {key: entry.get(key) for key in ("status", "reason", "plan", "refused", "withheld")}
                for entry in getattr(record, "private_plans", [])[-PLAN_HISTORY:]
            ],
        },
        indent=1,
    )


def _plan(model_client, record, cfg):
    """(commands, None) or ([], Gathered) when the plan call itself decides the outcome."""
    from doclayer.curator.model import ModelError, ReplyRejected
    from doclayer.curator.state import BudgetExceeded

    try:
        reply = asyncio.run(
            model_client.chat_json(plan_system(cfg), _plan_input(record, cfg), repair_attempts=0, json_object=True)
        )
    except ReplyRejected as exc:  # unparseable JSON: a real (failed) attempt, though flagged transient
        return [], Gathered("empty", trail={"plan": []}, reason=f"plan rejected: {exc}")
    except (ModelError, BudgetExceeded) as exc:
        if isinstance(exc, BudgetExceeded) or getattr(exc, "transient", False):
            return [], Gathered("outage", reason=f"planner unavailable: {exc}")
        return [], Gathered("empty", trail={"plan": []}, reason=f"plan rejected: {exc}")
    commands = reply.get("commands") if isinstance(reply, dict) else None
    if not isinstance(commands, list):
        return [], Gathered("empty", trail={"plan": []}, reason="plan rejected: no commands list")
    plan = [c for c in commands if isinstance(c, dict)][: cfg.max_commands]
    earlier = [entry.get("plan") or [] for entry in getattr(record, "private_plans", [])]
    if plan and any(_plan_key(plan) == _plan_key(previous) for previous in earlier):
        # The same observations cannot answer what they did not answer before.
        return [], Gathered("empty", trail={"plan": _trail_plan(plan)}, reason="plan identical to an earlier attempt")
    return plan, None


def gather_evidence(record, cfg, *, model_client, runner=None, now=None) -> Gathered:
    now = now or datetime.now(UTC)
    plan, decided = _plan(model_client, record, cfg)
    if decided:
        return decided
    trail = {"plan": _trail_plan(plan), "refused": [], "withheld": [], "unreachable": []}
    resolved: dict[str, dict[str, str]] = {}  # host -> name -> executable path
    sources, reached = [], 0
    for command in plan:
        host, argv = str(command.get("host", "")), command.get("argv")
        argv = normalize(argv) if isinstance(argv, list) else []
        known = resolved.get(host, {})
        reason = refusal(host, argv, hosts=cfg.hosts, resolved=set(known))
        if reason:
            trail["refused"].append({"host": host, "argv": argv, "reason": reason})
            continue
        # A version check runs the resolved path, so no alias or function stands in for it.
        run_argv = (
            [known[argv[0]], *argv[1:]] if argv[0] in known and argv[1:] in (["--version"], ["-version"]) else argv
        )
        result = run_command(
            host, run_argv, timeout_s=cfg.command_timeout_s, cap_bytes=cfg.output_cap_bytes, runner=runner
        )
        if result.status == "unreachable":
            trail["unreachable"].append({"host": host, "argv": argv, "reason": result.note})
            continue
        reached += 1
        if result.status == "withheld":
            trail["withheld"].append({"host": host, "argv": argv, "reason": result.note})
            continue
        path = (
            resolved_binary(argv[2], result.stdout) if argv[:2] == ["command", "-v"] and result.exit_code == 0 else None
        )
        if path:
            resolved.setdefault(host, {})[argv[2]] = path
        text = evidence_text(host, run_argv, result.exit_code, result.stdout, result.stderr, result.note)
        sources.append(
            {
                "kind": "text",
                "label": evidence_label(host, argv, len(sources) + 1, now),
                "text": text,
                "authority": "local-observation",
                "origin": PRIVATE_ORIGIN,
                "source_hash": hashlib.sha256(text.encode()).hexdigest(),
            }
        )
    if sources:
        return Gathered("ready", sources, trail, f"{len(sources)} outputs")
    if trail["unreachable"] and not reached:
        return Gathered(
            "outage", [], trail, "hosts unreachable: " + "; ".join(u["reason"] for u in trail["unreachable"][:3])
        )
    reasons = [f"{r['argv'][:1]} refused: {r['reason']}" for r in trail["refused"][:3]] + [
        f"{w['argv'][:1]} {w['reason']}" for w in trail["withheld"][:3]
    ]
    return Gathered("empty", [], trail, "no usable evidence: " + ("; ".join(reasons) or "plan had no commands"))


def _model_client(cfg):
    from doclayer.curator.config import stage_config
    from doclayer.curator.model import ModelClient
    from doclayer.curator.state import Usage

    cfg = stage_config(cfg, "planning")
    return ModelClient(cfg, Usage(), limits={"max_model_calls": cfg.inference.retries + 1})


def prepare_private_gap(ledger, record, catalog, notes, *, cfg=None, model_client=None, runner=None, now=None) -> bool:
    """Gather allowlisted evidence for a private need; True when the gap's state was decided here."""
    now = now or datetime.now(UTC)
    curator_cfg = cfg or load_config()
    pw = curator_cfg.private_writer
    gap_id = record.gap_id
    if early_project(catalog, record.representative_question):
        return False
    if not pw.hosts:
        # No host may be queried, so no plan could run: no model call, no counted attempt.
        ledger.append_event(
            gap_id,
            {"at": now.isoformat(), "kind": "private_evidence", "status": "not-configured", "reason": NO_HOSTS_REASON},
        )
        return False
    with ledger.lock("private-writer"):
        got = gather_evidence(
            record, pw, model_client=model_client or _model_client(curator_cfg), runner=runner, now=now
        )
        ledger.append_event(
            gap_id,
            {
                "at": now.isoformat(),
                "kind": "private_evidence",
                "status": got.status,
                "reason": got.reason[:500],
                **got.trail,
            },
        )
        if got.status == "outage":
            ledger.transition(
                gap_id,
                "needs_local_evidence",
                reason=got.reason[:500],
                backoff_until=(now + timedelta(hours=1)).isoformat(),
            )
            return True
        attempts = record.private_attempts + 1

        def remember(current):
            current.private_plans = [
                *current.private_plans,
                {
                    "at": now.isoformat(),
                    "status": got.status,
                    "reason": got.reason[:500],
                    **{key: got.trail.get(key, []) for key in ("plan", "refused", "withheld")},
                },
            ][-PLAN_HISTORY:]

        ledger.update(gap_id, remember)
        if got.status == "empty":
            # Never failed only for having tried: retry on a growing backoff with a different plan.
            ledger.transition(
                gap_id,
                "needs_local_evidence",
                reason=got.reason[:500],
                private_attempts=attempts,
                backoff_until=(now + timedelta(seconds=private_retry_backoff_s(attempts))).isoformat(),
            )
            return True

        def store(current):
            current.source_candidates = [
                s for s in current.source_candidates if not (isinstance(s, dict) and s.get("origin") == PRIVATE_ORIGIN)
            ]
            current.source_candidates.extend(got.sources)

        ledger.update(gap_id, store)
        ledger.transition(
            gap_id, "ready_for_curation", reason=f"private evidence gathered: {got.reason}", private_attempts=attempts
        )
        notes.append(f"{gap_id}: private evidence ready ({len(got.sources)} outputs)")
        return True
