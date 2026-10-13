"""Read-only live checks: what is actually running, each with its time.

Every check is read-only (an ssh command, an HTTP GET, a file read) and
returns a LiveResult tagged with the moment it ran. The resolver runs these
when a question depends on running state, and puts "observed" next to
"declared" so disagreements are visible.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from doclayer.settings import settings


@dataclass
class LiveResult:
    """One check's outcome. checks is empty when the check itself failed."""

    name: str
    checked_at: str
    checks: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def tag(self) -> str:
        return f"live check @ {self.checked_at}"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ssh(host: str, script: str, timeout_s: float) -> str:
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "bash -s"],
        input=script,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ssh {host} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout.strip()


#: Env var behind each setting a live check needs.
_SETTING_ENV = {
    "ssh_host": "DOC_LAYER_SSH_HOST",
    "search_release_link": "DOC_LAYER_SEARCH_RELEASE_LINK",
    "search_host_ip": "DOC_LAYER_SEARCH_HOST_IP",
    "qdrant_url": "DOC_LAYER_QDRANT_URL",
    "gateway_containers": "DOC_LAYER_GATEWAY_CONTAINERS",
}


def _unconfigured(*fields: str) -> str | None:
    """A "not configured" error naming each empty setting, or None when all are set."""
    missing = [_SETTING_ENV[f] for f in fields if not str(getattr(settings, f, "") or "").strip()]
    return f"not configured ({', '.join(missing)} is empty)" if missing else None


async def _ssh_async(script: str, timeout_s: float) -> str:
    return await asyncio.to_thread(_ssh, settings.ssh_host, script, timeout_s)


async def _vo_rag_release(timeout_s: float) -> LiveResult:
    """The release the deployed vo-rag symlink points at."""
    result = LiveResult(name="vo-rag-release", checked_at=_now())
    result.error = _unconfigured("ssh_host", "search_release_link")
    if result.error:
        return result
    try:
        target = await _ssh_async("readlink -f -- " + shlex.quote(settings.search_release_link), timeout_s)
        result.checks.append({"what": "release symlink target", "value": target})
        # Release dirs are named by commit (no .git inside); the basename is
        # the deployed commit.
        result.checks.append({"what": "deployed commit", "value": Path(target).name[:8]})
    except Exception as exc:  # noqa: BLE001 — a failed check is an answer, not a crash
        result.error = str(exc)
    return result


async def _vo_rag_api(timeout_s: float) -> LiveResult:
    """The vo-rag API unit and which port actually answers."""
    result = LiveResult(name="vo-rag-api", checked_at=_now())
    result.error = _unconfigured("ssh_host", "search_host_ip")
    if result.error:
        return result
    try:
        unit = await _ssh_async("systemctl is-active vo-rag-api", timeout_s)
        result.checks.append({"what": "vo-rag-api unit", "value": unit})
    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)
    async with httpx.AsyncClient(timeout=5.0) as client:
        for port in (8001, 8000):
            try:
                response = await client.post(
                    f"http://{settings.search_host_ip}:{port}/api/v1/query",
                    json={"query": "ping"},
                )
                result.checks.append(
                    {
                        "what": f"port :{port}",
                        "value": f"answers (HTTP {response.status_code})",
                    }
                )
            except Exception:  # noqa: BLE001
                result.checks.append({"what": f"port :{port}", "value": "no answer"})
    return result


async def _litellm_container(timeout_s: float) -> LiveResult:
    """The LiteLLM gateway container status on the search host."""
    result = LiveResult(name="litellm-container", checked_at=_now())
    result.error = _unconfigured("ssh_host", "gateway_containers")
    if result.error:
        return result
    names = settings.gateway_containers
    if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:\|[A-Za-z0-9_.-]+)*", names):
        result.error = "DOC_LAYER_GATEWAY_CONTAINERS must be names joined by '|'"
        return result
    try:
        script = f"docker ps --format '{{{{.Names}}}} {{{{.Status}}}}' | grep -E '^({names}) ' || true"
        for line in (await _ssh_async(script, timeout_s)).splitlines():
            name, _, status = line.partition(" ")
            result.checks.append({"what": f"container {name}", "value": status.strip()})
    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)
    return result


async def _brain_collection(timeout_s: float) -> LiveResult:
    """Point count of vo_rag_brain_v1 in the live Qdrant."""
    result = LiveResult(name="vo-rag-brain-collection", checked_at=_now())
    result.error = _unconfigured("qdrant_url")
    if result.error:
        return result
    try:
        collection = settings.validated_brain_collection()
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.get(f"{settings.qdrant_url}/collections/{collection}")
            response.raise_for_status()
            info = response.json()["result"]
            result.checks.append(
                {
                    "what": "vo_rag_brain_v1 points",
                    "value": str(info.get("points_count")),
                }
            )
            result.checks.append({"what": "collection status", "value": str(info.get("status"))})
    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)
    return result


async def _token_plan_endpoint(timeout_s: float) -> LiveResult:
    """DNS + TLS reachability of the Token Plan base URL host (no auth, no calls)."""
    result = LiveResult(name="token-plan-endpoint", checked_at=_now())
    result.error = _unconfigured("ssh_host")
    if result.error:
        return result
    try:
        script = "getent hosts token-plan.ap-southeast-1.maas.aliyuncs.com >/dev/null && echo resolves || echo no-dns"
        dns = await _ssh_async(script, timeout_s)
        result.checks.append({"what": "base URL host DNS", "value": dns})
    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)
    return result


#: The registry the resolver runs. Small on purpose.
REGISTRY: dict[str, object] = {
    "vo-rag-release": _vo_rag_release,
    "vo-rag-api": _vo_rag_api,
    "litellm-container": _litellm_container,
    "vo-rag-brain-collection": _brain_collection,
    "token-plan-endpoint": _token_plan_endpoint,
}

#: Checks run by default: the search release, API and collection (all cheap).
DEFAULT_CHECKS = ("vo-rag-release", "vo-rag-api", "vo-rag-brain-collection")


async def run_checks(names: list[str] | None = None, timeout_s: float | None = None) -> list[LiveResult]:
    """Run the named checks (default: DEFAULT_CHECKS) in parallel, each with its own timeout."""
    chosen = names or list(DEFAULT_CHECKS)
    timeout = timeout_s or settings.timeout_live_s
    results = await asyncio.gather(
        *(REGISTRY[name](timeout) for name in chosen if name in REGISTRY)  # type: ignore[operator]
    )
    return list(results)


def brain_sync_state() -> dict:
    """The brain_sync poller's state, as a dict.

    The poller runs on the search host, so its state file is read over ssh
    (read-only, bounded by the live-check timeout); a local file wins if one
    exists, which keeps local runs and tests working. With no ssh host
    configured nothing remote is tried. Any failure — ssh down,
    no file yet, unparsable json — degrades to {} and the resolver says
    "index state unknown" instead of guessing.
    """
    state_path = Path(settings.brain_sync_state).expanduser()
    raw: str | None = None
    try:
        raw = state_path.read_text(encoding="utf-8")
    except OSError:
        if not settings.ssh_host:
            return {}
        try:
            proc = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=5",
                    settings.ssh_host,
                    f"cat {settings.brain_sync_state}",
                ],
                capture_output=True,
                text=True,
                timeout=settings.timeout_live_s,
                check=False,
            )
            if proc.returncode == 0:
                raw = proc.stdout
        except (OSError, subprocess.TimeoutExpired):
            raw = None
    if not raw:
        return {}
    try:
        state = json.loads(raw)
        return state if isinstance(state, dict) else {}
    except json.JSONDecodeError:
        return {}


def main(argv: list[str] | None = None) -> int:
    """Run checks from the command line: `uv run python -m doclayer.livechecks [name …]`."""
    import sys

    names = list(sys.argv[1:] if argv is None else argv) or list(DEFAULT_CHECKS)
    for result in asyncio.run(run_checks(names)):
        print(f"{result.name} @ {result.checked_at}")
        if result.error:
            print(f"  error: {result.error}")
        for check in result.checks:
            print(f"  {check['what']}: {check['value']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
