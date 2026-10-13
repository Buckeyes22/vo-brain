"""UserPromptSubmit hook: a reasoned, selective doclayer suggestion.

Reads the hook's JSON from stdin, classifies the prompt deterministically
against the coverage catalog (no model call, no network, no curator), and on a
justified route prints one line of additionalContext naming the subject, the
evidence/source route, and any visible negative-cache status. A clear
documentation request naming an uncovered subject records a lightweight gap
candidate. Never blocks: any error exits 0 with no output.
"""

from __future__ import annotations

import json
import sys
import uuid

from doclayer import routing
from doclayer.negative_cache import NegativeCache
from doclayer.settings import Settings

SSH_CONFIG = "~/.ssh/config"
CACHE_DIR = "~/.cache/doclayer"
MAX_PROMPT_CHARS = 20_000

SUGGESTION = (
    "doclayer: {subject} is a known {kind}; route: {sources}. {reason} Consider doclayer `resolve` before answering."
)
UNCOVERED = (
    "doclayer: '{candidate}' is not in the brain catalog — use external "
    "documentation and record the need with doclayer `record_gap`."
)


def _cache_status(prompt: str, route: routing.RouteResult) -> str | None:
    try:
        cache = NegativeCache(Settings().negative_cache_path)
        subject = route.subjects[0] if route.subjects else None
        hit = cache.check_any_sources(prompt, subject)
        if hit:
            return (
                "recent lookup did not answer this request "
                f"({hit.recorded_at[:10]}; checked {', '.join(hit.checked_sources)})"
            )
    except Exception:  # noqa: BLE001 — visible cache status is best-effort
        return None
    return None


def suggestion_for(prompt: str, route: routing.RouteResult, catalog=None) -> str | None:
    """One reasoned line, or None when no lookup is justified."""
    if route.intent not in (
        routing.INTENT_LOCAL,
        routing.INTENT_PUBLIC,
        routing.INTENT_MIXED,
    ):
        return None
    if route.uncovered_subject:
        line = UNCOVERED.format(candidate=route.uncovered_subject)
    elif route.subjects:
        kind = "subject"
        if catalog is not None:
            entry = catalog.subjects.get(route.subjects[0])
            if entry is not None:
                kind = entry.kind
        line = SUGGESTION.format(
            subject=route.subjects[0],
            kind=kind,
            sources=", ".join(route.selected_sources) or "brain evidence",
            reason=route.reason,
        )
    else:
        return None
    status = _cache_status(prompt, route)
    if status:
        line += f" {status}."
    return line


def _record_uncovered_candidate(prompt: str, route: routing.RouteResult, event_id: str | None = None) -> None:
    """Lightweight capture: a clear documentation request naming an uncovered
    subject. Best-effort; the hook never blocks on failures."""
    if not route.gap_capture_eligible or not route.uncovered_subject:
        return
    try:
        settings = Settings()
        if not settings.gap_capture_enabled:
            return
        from doclayer.gap_client import GapClient

        GapClient(settings).submit_event(
            defer=True,
            need_class="missing_coverage",
            subject=None,
            representative_question=prompt[:2000],
            detail=f"hook capture: uncovered subject '{route.uncovered_subject}'",
            event_key=event_id or "evt-" + uuid.uuid4().hex,
            identity_override=f"unknown:{route.uncovered_subject}",
        )
    except Exception:  # noqa: BLE001 — capture must never block a prompt
        pass


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        prompt = payload.get("prompt") if isinstance(payload, dict) else None
        if not isinstance(prompt, str) or prompt.lstrip().startswith("/"):
            sys.exit(0)
        if len(prompt) > MAX_PROMPT_CHARS:
            sys.exit(0)
        settings = Settings()
        from pathlib import Path

        from doclayer.coverage import ssh_host_aliases

        catalog = routing.load_catalog(
            settings.brain_repo,
            settings.catalog_cache_dir,
            settings.routing_policy_path,
        )
        hosts = ssh_host_aliases(Path(SSH_CONFIG).expanduser(), settings.catalog_cache_dir)
        route = routing.route_prompt(prompt, catalog, hosts)
        turn = payload.get("event_id") or payload.get("turn_id") or payload.get("prompt_id")
        event_id = None
        if isinstance(turn, str):
            event_id = f"hook:{payload.get('session_id', '')}:{turn}"
        _record_uncovered_candidate(routing.routing_text(prompt), route, event_id)
        line = suggestion_for(prompt, route, catalog)
        if line:
            out = {"hookEventName": "UserPromptSubmit", "additionalContext": line}
            print(json.dumps({"hookSpecificOutput": out}))
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 — the hook never blocks or delays a prompt
        pass
    sys.exit(0)
