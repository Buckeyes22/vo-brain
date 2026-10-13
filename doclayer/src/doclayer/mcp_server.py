"""The doc layer's MCP server: resolve, plus small routing/gap operations.

Run with: `uv run doclayer-mcp` (stdio transport; not registered anywhere
global — add it to your own client config if you want it).

- `resolve`: one question in, one markdown answer out (unchanged contract,
  plus optional `force` to bypass negative-cache suppression).
- `route`: the cheap deterministic coverage/route decision — no source calls
  (no separate round trip should be needed just to decide).
- `record_gap`: record an unresolved documentation need or an external-tool
  answer.
- `attach_source`: attach documentation already found during the task.
"""

from __future__ import annotations

import mcp.server.fastmcp

from doclayer import resolver as _resolver


def build_server() -> mcp.server.fastmcp.FastMCP:
    server = mcp.server.fastmcp.FastMCP("doclayer", instructions=None)

    @server.tool()
    async def resolve(
        question: str,
        product: str | None = None,
        live: bool | None = None,
        force: bool | None = None,
    ) -> str:
        """Ask the brain's doc layer one question about a system; get one answer.

        Joins how the product works (Context7 + local doc copies) with how it
        is set up here (the brain), with a source and date on every fact.
        Flags declared-vs-running disagreements, stale items, and index lag.
        `product` narrows the subject (e.g. "alibaba-model-studio"); `live`
        forces the read-only live checks (default: run when the question
        depends on what is currently running); `force` retries even when a
        recent lookup was suppressed by the negative cache.
        """
        answer = await _resolver.resolve(question, product=product, live=live, force=force)
        return answer.markdown()

    @server.tool()
    async def route(question: str, explicit: bool | None = None) -> str:
        """Cheap deterministic routing decision for a prompt (no source calls).

        Returns the intent, identified subjects, selected sources, coverage
        status/revision, gap-capture eligibility, and any visible
        negative-cache suppression status.
        """
        import pathlib

        from doclayer import routing
        from doclayer.coverage import ssh_host_aliases

        settings = _resolver.settings
        try:
            catalog = routing.load_catalog(
                settings.brain_repo,
                settings.catalog_cache_dir,
                settings.routing_policy_path,
            )
            hosts = ssh_host_aliases(
                pathlib.Path("~/.ssh/config").expanduser(),
                settings.catalog_cache_dir,
            )
        except Exception as exc:  # noqa: BLE001 — degrade to unknown coverage
            catalog = routing.Catalog(status="unknown", revision=None, policy_version=0, reason=str(exc))
            hosts = []
        result = routing.route_prompt(question, catalog, hosts, explicit=bool(explicit))
        lines = [
            f"intent: {result.intent}",
            f"subjects: {', '.join(result.subjects) or 'none'}",
            f"sources: {', '.join(result.selected_sources) or 'none'}",
            f"coverage: {result.coverage_status} @ {result.coverage_revision or 'unknown'}",
            f"gap capture eligible: {result.gap_capture_eligible}",
            f"reason: {result.reason}",
        ]
        if result.uncovered_subject:
            lines.append(f"uncovered subject: {result.uncovered_subject}")
        if result.suppression:
            lines.append(f"cache: {result.suppression}")
        return "\n".join(lines)

    @server.tool()
    async def record_gap(
        question: str,
        subject: str | None = None,
        need: str = "missing_coverage",
        detail: str | None = None,
        facets: list[str] | None = None,
    ) -> str:
        """Record an unresolved documentation need in the durable gap ledger.

        Use when a request was answered through external tools, or a genuine
        unresolved need arose mid-task. `need` is one of: missing_coverage,
        enrichment, retrieval_defect, infrastructure, needs_local_evidence,
        ambiguous. Bounded technical detail only — never transcripts or
        secrets.
        """
        from doclayer.gap_client import GapClient

        settings = _resolver.settings
        if not settings.gap_capture_enabled:
            return "gap capture is disabled; the need was not recorded"
        try:
            ledger = GapClient(settings)
            import asyncio

            record, created = await asyncio.to_thread(
                ledger.submit_event,
                need_class=need,
                subject=subject,
                representative_question=question,
                facets=facets or [],
                detail=detail,
            )
        except Exception as exc:  # noqa: BLE001 — report, never raise
            return f"failed to record the gap: {exc}"
        state = "recorded" if created else "existing gap updated"
        return f"{state}: {record.gap_id} ({record.need_class}, {record.status}, seen {record.recurrence}x)"

    @server.tool()
    async def attach_source(
        subject: str | None,
        url: str,
        note: str | None = None,
        question: str | None = None,
        gap_id: str | None = None,
        authority: str = "unverified",
    ) -> str:
        """Attach documentation already found during the task.

        Marks the need as an enrichment candidate and carries the source URL
        so background curation can reuse it instead of rediscovering. Set
        authority="official" only after verifying that the project owns the
        source; otherwise retain the default unverified authority.
        """
        from doclayer.gap_client import GapClient

        settings = _resolver.settings
        if not settings.gap_capture_enabled:
            return "gap capture is disabled; the source was not recorded"
        try:
            ledger = GapClient(settings)
            if authority not in ("official", "unverified"):
                return "authority must be official or unverified"
            if gap_id:
                import asyncio

                reply = await asyncio.to_thread(
                    ledger.operation,
                    "attach_source",
                    {
                        "gap_id": gap_id,
                        "source": {
                            "url": url,
                            "note": (note or "")[:500],
                            "authority": authority,
                            "origin": "task-documentation",
                        },
                    },
                )
                return f"source attached to {reply['record']['gap_id']}"
            import asyncio

            record, created = await asyncio.to_thread(
                ledger.submit_event,
                need_class="enrichment",
                subject=subject,
                representative_question=question or f"documentation for {subject or 'unknown subject'}",
                source_candidates=[
                    {
                        "url": url,
                        "note": (note or "")[:500],
                        "authority": authority,
                        "origin": "task-documentation",
                    }
                ],
                detail=note,
            )
        except Exception as exc:  # noqa: BLE001 — report, never raise
            return f"failed to attach the source: {exc}"
        return f"source attached to {record.gap_id} ({record.status}, seen {record.recurrence}x)"

    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
