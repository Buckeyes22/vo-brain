"""The curator's MCP server: the same job operations, as agent tools.

Run with `brain-curator-mcp` (stdio). Kept as a separate server from the
resolver's `doclayer-mcp` so the existing resolver tool can never regress.
Tools call the same SSH transport as the CLI: submissions return a job id
after durable enqueueing; source bodies never enter the calling agent's
context — status/result return bounded views, with `include_digest` for the
completion digest.
"""

from __future__ import annotations

from pathlib import Path

import mcp.server.fastmcp

from doclayer.curator.config import load_config
from doclayer.curator.transport import transport_for

_MAX_TEXT = 20000


def _clip(value: str | None, limit: int = _MAX_TEXT) -> str:
    if value is None:
        return ""
    return value if len(value) <= limit else value[:limit] + f"\n…[clipped at {limit} chars]"


def build_server() -> mcp.server.fastmcp.FastMCP:
    server = mcp.server.fastmcp.FastMCP(
        "brain-curator",
        instructions=(
            "Submit material to the brain wiki curator (durable worker on the configured worker host; "
            "curation runs on the configured model endpoint and publishes automatically). "
            "Submit returns a job id immediately; poll job_status / job_result."
        ),
    )
    server._curator_cfg = None  # type: ignore[attr-defined]

    def _cfg():
        if server._curator_cfg is None:  # type: ignore[attr-defined]
            server._curator_cfg = load_config()  # type: ignore[attr-defined]
        return server._curator_cfg  # type: ignore[attr-defined]

    def _transport():
        return transport_for(_cfg())

    def _guarded(fn, *args, **kwargs) -> str:
        """Every tool answers with structured JSON, including failures — a
        rejected request is an answer, never a protocol-level crash."""
        import json as _json

        try:
            reply = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            reply = {"ok": False, "error": str(exc)[:500]}
        return _json.dumps(reply, ensure_ascii=False)

    @server.tool()
    async def submit_import(
        intent: str,
        urls: list[str] | None = None,
        text: str | None = None,
        text_label: str = "pasted-text",
        files: list[str] | None = None,
        directory: str | None = None,
        manifest: str | None = None,
        exclusions: list[str] | None = None,
        idempotency_key: str | None = None,
        source_policy: dict | None = None,
        mode: str | None = None,
        targets: list[str] | None = None,
    ) -> str:
        """Submit material for wiki curation. Starts the full automatic flow:
        intake, relevant-link discovery (multi-stage), evidence extraction,
        model curation, validation, and automatic publication. Returns a
        job id. The default mode is single-pass (draft, one validation,
        publish; additive only); mode="multi-stage" selects the original
        multi-stage pipeline.

        Args:
            intent: the topic or task instruction ("build out X knowledge", ...).
            urls: seed URLs to inspect and follow.
            text: pasted text (agent findings, notes) with text_label.
            files: local md/txt/pdf paths to transfer to the worker.
            directory: local directory of supported files (one-shot).
            manifest: local YAML batch manifest path (topic/sources/roles/exclusions).
            exclusions: material to leave out (e.g. "Bedrock usage documentation").
            idempotency_key: optional; retries with the same key never duplicate a job.
            mode: "single-pass" (default) or "multi-stage".
            targets: wiki/infra page paths the import should update or create.
        """
        payload: dict = {
            "intent": intent,
            "urls": list(urls or []),
            "texts": [{"label": text_label, "text": text}] if text else [],
            "exclusions": list(exclusions or []),
            "source_policy": source_policy or {},
        }
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        if mode:
            payload["mode"] = mode
        if targets:
            payload["targets"] = list(targets)
        file_paths = [Path(f).expanduser() for f in (files or [])]
        if manifest:
            manifest_path = Path(manifest).expanduser()
            file_paths.append(manifest_path)
            import hashlib

            payload["manifest_file"] = manifest_path.name
            payload["manifest_digest"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()[:16]
        directory_path = Path(directory).expanduser() if directory else None
        return _guarded(_transport().call, "submit", payload, files=file_paths or None, directory=directory_path)

    @server.tool()
    async def job_status(job_id: str) -> str:
        """Bounded status of one curator job: stage, per-page states, usage,
        publication evidence."""
        return _guarded(_transport().call, "status", {"job_id": job_id})

    @server.tool()
    async def job_result(job_id: str, include_digest: bool = True) -> str:
        """Completion digest for one curator job: created/updated pages,
        sources, gaps, conflicts, usage, and commit/PR evidence."""

        def _call():
            reply = _transport().call("result", {"job_id": job_id, "include_digest": include_digest})
            if include_digest and isinstance(reply.get("digest"), str):
                reply["digest"] = _clip(reply["digest"])
            return reply

        return _guarded(_call)

    @server.tool()
    async def job_resume(job_id: str, extend: dict[str, int] | None = None, operator_readmission: bool = False) -> str:
        """Resume a stopped or retryable curator job (preserving progress and
        budget counters). A retired automatic allowance requires explicit
        operator_readmission and a positive max_model_calls extension."""
        payload = {"job_id": job_id}
        if extend is not None:
            payload["extend"] = extend
        if operator_readmission:
            payload["operator_readmission"] = True
        return _guarded(_transport().call, "resume", payload)

    @server.tool()
    async def job_continue(
        job_id: str,
        source_ids: list[str] | None = None,
        review_omitted: bool = True,
        discover: bool = True,
        source_policy: dict | None = None,
        intent: str = "",
        urls: list[str] | None = None,
        refresh_source_ids: list[str] | None = None,
        mode: str | None = None,
        targets: list[str] | None = None,
    ) -> str:
        """Create a bounded linked import reusing evidence/cache; never replay
        the original PR. source_ids can review omitted sections without crawling.
        Additional primary URLs require discovery and preserve inherited evidence.
        mode ("single-pass" or "multi-stage") defaults to the parent's mode.
        targets (single-pass only) names the wiki pages to update, replacing the
        parent's targets; validated like submit_import targets."""
        payload = {
            "job_id": job_id,
            "source_ids": source_ids or [],
            "review_omitted": review_omitted,
            "discover": discover,
        }
        if source_policy is not None:
            payload["source_policy"] = source_policy
        if urls is not None:
            payload["urls"] = urls
        if refresh_source_ids is not None:
            payload["refresh_source_ids"] = refresh_source_ids
        if intent:
            payload["intent"] = intent
        if mode:
            payload["mode"] = mode
        if targets:
            payload["targets"] = targets
        return _guarded(_transport().call, "continue", payload)

    @server.tool()
    async def job_cancel(job_id: str) -> str:
        """Cancel one curator job at the next stage boundary."""
        return _guarded(_transport().call, "cancel", {"job_id": job_id})

    @server.tool()
    async def job_correct(page: str, instruction: str, claim: str = "") -> str:
        """Record an operator correction for a wiki page and enqueue the fix.
        `claim` is the corrected claim text (used to stop repeat imports from
        restoring it)."""
        return _guarded(
            _transport().call,
            "correct",
            {"page": page, "claim": claim, "instruction": instruction},
        )

    @server.tool()
    async def job_inspect(job_id: str, what: str = "diff", path: str = "") -> str:
        """Inspect one job: what="diff" (exact draft diff for a page path),
        "draft" (the draft page), or "evidence" (retained evidence records)."""
        payload: dict = {"job_id": job_id, "what": what}
        if path:
            payload["path"] = path

        def _call():
            reply = _transport().call("inspect", payload)
            if isinstance(reply.get("content"), str):
                reply["content"] = _clip(reply["content"])
            if isinstance(reply.get("diff"), str):
                reply["diff"] = _clip(reply["diff"])
            return reply

        return _guarded(_call)

    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
