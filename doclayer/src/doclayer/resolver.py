"""The resolver: one question in, one sourced answer out.

Exact facts come from the brain pages in Git; everything else
from vo-rag (POST /api/v1/query on vo_rag_brain_v1) plus Context7, asked in
parallel with per-source timeouts. Every fact is tagged with its source
(``path@commit``, ``URL@fetch-date``, ``Context7``, ``live check @ time``).
Declared-vs-running disagreements, stale items, and index lag are flagged at
the top. A source being down or slow never blanks the answer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from doclayer import coverage, routing
from doclayer.applicability import (
    host_spans,
    known_identities,
    links_to_page,
    matches_identity,
    matches_scope,
    mentions_group,
    mentions_host,
    question_host_pages,
    question_hosts,
    question_service_pages,
    question_without_hosts,
    requested_scopes,
    source_scope,
    source_urls,
)
from doclayer.brainrepo import BrainPage, BrainRepo, UnavailableBrainRepo, fact_rows, is_brain_page
from doclayer.livechecks import LiveResult, brain_sync_state, run_checks
from doclayer.negative_cache import (
    NegativeCache,
    evidence_generation,
)
from doclayer.resolver_context import (
    ANSWER_EVIDENCE_CHARS,
    EVIDENCE_CHARS,
    authority_record,
    authority_text,
    evidence_cost,
    indexed_evidence,
    line_contexts,
)
from doclayer.resolver_context import (
    noncode_lines as _noncode_lines,
)
from doclayer.routing import INTENT_EXPLICIT
from doclayer.settings import BrainRepoConfigurationError, settings

LIVE_HINTS = (
    "running",
    "current",
    "currently",
    "latest",
    "deployed",
    "live",
    "actually",
    "right now",
    "release",
)


#: Longest fact value shown in an answer; longer values are clipped with "…".
FACT_CHARS = 400


@dataclass
class Answer:
    """The one answer: flags first, then facts with sources."""

    question: str
    subject: str | None
    flags: list[str] = field(default_factory=list)
    git_facts: list[dict[str, str]] = field(default_factory=list)
    doc_facts: list[dict[str, str]] = field(default_factory=list)
    route_facts: list[dict[str, str]] = field(default_factory=list)
    search_hits: list[dict[str, str]] = field(default_factory=list)
    search_error: str | None = None
    context7: dict[str, Any] = field(default_factory=dict)
    live_results: list[LiveResult] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    git_source_available: bool = True
    scope_constraints: list[dict[str, str]] = field(default_factory=list)
    #: Pages of a host the question names (its own and its recorded services').
    host_pages: set = field(default_factory=set)
    #: Structured outcome: what was planned, actually checked,
    #: skipped with reasons, and the resulting dispositions. Multiple
    #: dispositions may coexist; Markdown rendering appends them.
    source_calls: dict[str, int] = field(default_factory=dict)
    checked_sources: list[str] = field(default_factory=list)
    skipped_sources: dict[str, str] = field(default_factory=dict)
    dispositions: list[str] = field(default_factory=list)
    intent: str | None = None

    def __post_init__(self):
        owners = _requested_fact_owners(self.question)
        scope_subject = (
            None
            if owners and self.subject and not any(matches_identity(self.subject, owner) for owner in owners)
            else self.subject
        )
        self.scope_constraints = requested_scopes(self.question, scope_subject)
        # Only a question that asks about the host alone ("what does svc1 serve?")
        # gives the host's other pages a candidacy of their own.
        self.host_pages = question_host_pages(self.question) if _asks_only_about_host(self.question) else set()

    def markdown(self) -> str:
        parts = [f"# {self.subject or 'Answer'}: {self.question}", ""]
        if self.flags:
            parts += ["**Flags**", *[f"- ⚠ {flag}" for flag in self.flags], ""]
        parts += ["## Answer", ""]
        body = self._prose()
        parts += [body, ""] if body else ["Not answerable from the sources below.", ""]
        if self.unknowns:
            parts += [
                '**Unknown — unresolved automatic coverage** (may miss paraphrases; this is not "unsupported"):',
                *[f"- {u}" for u in self.unknowns],
                "",
            ]
        if self.search_hits or self.search_error:
            parts += ["## Search (vo-rag over vo_rag_brain_v1)", ""]
            if self.search_error:
                parts += [f"- unavailable: {self.search_error}"]
            for hit in self.search_hits:
                parts.append(
                    f"- {hit['path']} · {hit['heading']} · revision {hit.get('source_revision', hit['commit'])} · score {hit['score']}"
                    + (f" · {hit['authority_metadata']}" if hit.get("authority_metadata") else "")
                )
            parts.append("")
        if self.context7:
            parts += ["## Context7", ""]
            if self.context7.get("error"):
                parts.append(f"- unavailable: {self.context7['error']}")
            else:
                parts.append(f"- library: {self.context7.get('library', 'n/a')}")
                summary = (self.context7.get("summary") or "").strip()
                if self.context7.get("lacks_scope") or self.context7.get("lacks_facts"):
                    parts.append("- result withheld: requested applicability or facts are not established")
                else:
                    for line in summary.splitlines():
                        parts.append(f"- {line}")
                if self.context7.get("lacks_facts"):
                    parts.append(
                        "- ⚠ this Context7 result does not state the facts asked for "
                        "(no verified requested facts or page revision/version); "
                        "use the dated sources above"
                    )
            parts.append("")
        if self.live_results:
            parts += ["## Live checks", ""]
            for result in self.live_results:
                if not result.ok:
                    parts.append(f"- {result.name}: failed — {result.error} ({result.tag()})")
                for check in result.checks:
                    parts.append(f"- {check['what']}: {check['value']} ({result.tag()})")
            parts.append("")
        if self.dispositions or self.checked_sources or self.skipped_sources:
            parts += ["## Dispositions", ""]
            if self.dispositions:
                parts.append(f"- outcome: {', '.join(sorted(self.dispositions))}")
            if self.checked_sources:
                parts.append(f"- checked sources: {', '.join(self.checked_sources)}")
            for source, why in sorted(self.skipped_sources.items()):
                parts.append(f"- skipped {source}: {why}")
            parts.append("")
        return "\n".join(parts)

    def _prose(self) -> str:
        lines: list[str] = []
        for result in self.live_results:
            if result.ok:
                for check in result.checks:
                    lines.append(f"- **{check['what']}**: {check['value']} — {result.tag()}")
        used_context = set()
        context_chars = 0
        for fact in self.git_facts:
            context = fact.get("evidence_context", "")
            key = (fact["source"], context)
            if context and key not in used_context:
                if context_chars + len(context) + len(fact.get("authority_metadata", "")) > ANSWER_EVIDENCE_CHARS:
                    lines.append(
                        f"- **Unknown**: {fact['source']}: full governing context exceeds total delivery limit; fact withheld"
                    )
                    continue
                context_chars += len(context) + len(fact.get("authority_metadata", ""))
                used_context.add(key)
                quoted = "\n".join("> " + line for line in context.splitlines())
                lines.append(
                    f"**{fact['fact']} (source extract with full context) — {fact['source']}**\n"
                    + (f"{fact['authority_metadata']}\n" if fact.get("authority_metadata") else "")
                    + f"\n{quoted}"
                )
            if not context:
                lines.append(
                    f"- **{fact['fact']}**: {fact['value']} — {fact['source']}"
                    + (f" · {fact['authority_metadata']}" if fact.get("authority_metadata") else "")
                )
        if self.git_facts:
            lines.append("")
        for fact in self.doc_facts:
            context = fact.get("evidence_context", "")
            key = (fact["source"], context)
            if context:
                if key in used_context:
                    continue
                if (
                    len(context) + len(fact.get("authority_metadata", "")) > EVIDENCE_CHARS
                    or context_chars + len(context) + len(fact.get("authority_metadata", "")) > ANSWER_EVIDENCE_CHARS
                ):
                    lines.append(
                        f"- **Unknown**: {fact['source']}: full governing context exceeds delivery limit; fact withheld"
                    )
                    continue
                context_chars += len(context) + len(fact.get("authority_metadata", ""))
                used_context.add(key)
                quoted = "\n".join("> " + line for line in context.splitlines())
                lines.append(
                    f"**{fact['fact']} (source extract with full context) — {fact['source']}**\n"
                    + (f"{fact['authority_metadata']}\n" if fact.get("authority_metadata") else "")
                    + f"\n{quoted}"
                )
            else:
                lines.append(
                    f"- **{fact['fact']}**: {fact['value']} — {fact['source']}"
                    + (f" · {fact['authority_metadata']}" if fact.get("authority_metadata") else "")
                )
        if self.doc_facts:
            lines.append("")
        for fact in self.route_facts:
            lines.append(
                f"- **{fact['fact']}**: {fact['value']} — {fact['source']}"
                + (f" · {fact['authority_metadata']}" if fact.get("authority_metadata") else "")
            )
        if not lines:
            # When canonical Git is unavailable, indexed excerpts remain
            # available evidence, explicitly attributed to their index revision.
            for hit in self.search_hits:
                if (
                    hit.get("content")
                    and matches_scope(hit.get("_scope", ""), self.scope_constraints)
                    and _matches_requested_aspects(hit["content"], self.question)
                ):
                    lines.append(
                        f"- **Indexed passage ({hit.get('heading', 'search')})**: "
                        f"{hit['content']} — {hit.get('path', '?')}@{hit.get('source_revision', hit.get('commit', '?'))} "
                        + (f"· {hit['authority_metadata']} " if hit.get("authority_metadata") else "")
                        + (
                            "· indexed evidence (current Git source unavailable)"
                            if not self.git_source_available
                            else "· indexed evidence (not verified against selected current Git facts)"
                        )
                    )
                    if not hit.get("_context_attested"):
                        lines.append(
                            "  - governing context completeness is not attested by this legacy indexed passage"
                        )
                    for segment in hit.get("_context_citations", []):
                        lines.append(
                            f"  - context source: {segment.get('collection', '?')}/{segment.get('source_id', '?')}"
                            f" · point {segment.get('point_id', '?')} · revision {segment.get('source_revision', '?')}"
                            f" · {segment.get('heading', '')}"
                        )
                    if sum("Indexed passage" in line for line in lines) == 3:
                        break
            if (
                not lines
                and self.context7.get("summary")
                and not self.context7.get("error")
                and not self.context7.get("lacks_facts")
                and not self.context7.get("lacks_scope")
            ):
                lines.append(
                    f"- {self.context7['summary']} — Context7 ({self.context7.get('library', 'library unknown')}; page revision unknown)"
                )
        return "\n".join(lines).strip()


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #


async def query_vo_rag(question: str, top_k: int = 8) -> tuple[list[dict[str, str]], str | None]:
    """POST /api/v1/query on the brain collection. Returns (hits, error)."""
    try:
        collection = settings.validated_brain_collection()
        key = settings.vo_rag_api_key()
        headers = {"X-API-Key": key} if key else {}
        async with httpx.AsyncClient(timeout=settings.timeout_vo_rag_s) as client:
            async with client.stream(
                "POST",
                f"{settings.vo_rag_url}/api/v1/query",
                json={"query": question, "collections": [collection], "top_k": top_k},
                headers=headers,
            ) as response:
                response.raise_for_status()
                wire = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(wire) + len(chunk) > 1_048_576:
                        return [], "indexed response exceeds bounded 1 MiB delivery limit"
                    wire.extend(chunk)
                decoded = json.loads(wire)
                results = decoded.get("results", [])
                if not isinstance(results, list):
                    return [], "indexed results are not a list"
    except Exception as exc:  # noqa: BLE001 — a down source degrades, never fails
        return [], f"{type(exc).__name__}: {exc}"
    hits = []
    used = 0
    malformed = False
    foreign = False
    metadata_gap = False
    for result in results[:top_k]:
        if not isinstance(result, dict) or not isinstance(result.get("payload", {}), dict):
            malformed = True
            continue
        payload = result.get("payload", {})
        if result.get("collection") != collection or payload.get("collection", collection) != collection:
            foreign = True
            continue
        if not is_brain_page(str(payload.get("path", ""))):
            continue  # repository meta (README, docs/, doclayer/ ...) is never a brain page
        try:
            score = float(result.get("score", 0))
            if not math.isfinite(score):
                raise ValueError("nonfinite")
        except (ValueError, TypeError):
            malformed = True
            continue
        content, segments, context_error = indexed_evidence(payload, collection=collection)
        provenance = authority_record(
            str(payload.get("path", "?")),
            payload,
            collection=result["collection"],
            source_revision=str(payload.get("source_revision", payload.get("commit", "?"))),
        )
        metadata_text = authority_text(provenance)
        if len(metadata_text) > EVIDENCE_CHARS or used + len(metadata_text) > ANSWER_EVIDENCE_CHARS:
            metadata_gap = True
            continue
        if used + evidence_cost(content, segments) + len(metadata_text) > ANSWER_EVIDENCE_CHARS:
            content, context_error = "", "indexed evidence exceeds total delivery limit"
            segments = []
        used += evidence_cost(content, segments) + len(metadata_text)
        hits.append(
            {
                "path": str(payload.get("path", "?")),
                "heading": str(payload.get("heading", "")),
                "commit": str(payload.get("commit", "?"))[:8],
                "score": f"{score:.3f}",
                "kind": str(payload.get("kind", "?")),
                **provenance,
                "authority_metadata": metadata_text,
                "content": content,
                "_context_citations": segments,
                "_context_error": context_error,
                "_context_attested": payload.get("governing_context_complete") is True,
                "_scope": source_scope(
                    payload,
                    title=str(payload.get("title", "")),
                    heading=str(payload.get("heading", "")),
                    path=str(payload.get("path", "")),
                    urls=source_urls(str(payload.get("content", ""))) + [payload.get("url", "")],
                ),
            }
        )
    errors = []
    if foreign:
        errors.append(
            "indexed results outside the configured brain collection (or missing collection identity) were withheld"
        )
    if malformed:
        errors.append("malformed indexed results were withheld")
    if metadata_gap:
        errors.append("indexed authority metadata exceeds bounded delivery limit; affected results were withheld")
    return hits, "; ".join(errors) or None


async def query_context7(question: str, subject: str | None) -> dict[str, Any]:
    """Ask Context7 (public MCP endpoint, stateless JSON-RPC) in two calls."""
    async with httpx.AsyncClient(timeout=settings.timeout_context7_s) as client:
        try:
            resolve = await _context7_call(
                client,
                "resolve-library-id",
                {"libraryName": subject or question, "query": question},
            )
            libraries = _context7_libraries(resolve)
            if not libraries:
                return {"library": None, "summary": "", "lacks_facts": True}
            library = _pick_library(libraries, subject or question)
            docs = await _context7_call(
                client,
                "query-docs",
                {"libraryId": library["id"], "query": question},
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}
    text = docs.get("content", [{}])[0].get("text", "") if docs else ""
    return {
        "library": library["id"],
        "summary": text if len(text) <= EVIDENCE_CHARS else "",
        "context_error": "Context7 fragment exceeds bounded delivery limit; withheld"
        if len(text) > EVIDENCE_CHARS
        else None,
        "lacks_facts": not _context7_useful(text),
    }


def _pick_library(libraries: list[dict[str, str]], subject: str) -> dict[str, str]:
    """The library whose title/id shares the most words with the product name."""
    words = [w for w in re.findall(r"\w{3,}", subject.lower())]
    return max(
        libraries,
        key=lambda lib: sum(1 for word in words if word in f"{lib['title']} {lib['id']}".lower()),
    )


async def _context7_call(client: httpx.AsyncClient, method: str, arguments: dict[str, Any]) -> Any:
    response = await client.post(
        settings.context7_url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": method, "arguments": arguments},
        },
        headers={"Accept": "application/json, text/event-stream"},
    )
    response.raise_for_status()
    for line in response.text.splitlines():
        if line.startswith("data:"):
            payload = json.loads(line[5:])
            if "error" in payload:
                raise RuntimeError(f"context7 {method}: {payload['error'].get('message')}")
            return payload.get("result", {})
    raise RuntimeError(f"context7 {method}: no data line in response")


def _context7_libraries(result: dict[str, Any]) -> list[dict[str, str]]:
    text = result.get("content", [{}])[0].get("text", "")
    return [
        {"title": match.group(1).strip(), "id": match.group(2).strip()}
        for match in re.finditer(r"Title: (.+)\n- Context7-compatible library ID: (\S+)", text)
    ]


def _context7_useful(text: str) -> bool:
    """A Context7 'hit' is not an answer: does the text state anything usable?"""
    if len(text.strip()) < 200:
        return False
    lowered = text.lower()
    return "slide to verify" not in lowered and "slider" not in lowered


def route_facts() -> tuple[list[dict[str, str]], list[str]]:
    """The model-route fact from the configured routing config (that file only).

    Reads the working copy and the committed version so the answer can flag a
    config split. Returns (facts, flags).
    """
    facts: list[dict[str, str]] = []
    flags: list[str] = []
    path = settings.route_config
    if not path:
        return [], []
    try:
        working = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"routing config unreadable ({path}): {exc}"]
    providers = working.get("tier_aliases", {}).get("S", {}).get("providers", [])
    for provider in providers:
        if "alibaba" not in str(provider.get("provider", "")):
            continue
        facts.append(
            {
                "fact": f"Token Plan route ({provider.get('model')})",
                "value": (
                    f"tier S on LiteLLM -> {provider.get('api_base')} ({provider.get('model')}, Anthropic protocol)"
                ),
                "source": f"{path.name} (uncommitted working copy, note dated {_note_date(provider.get('note'))})",
            }
        )
    committed = _committed_route_providers()
    if committed is not None:
        committed_names = {p.get("provider") for p in committed}
        working_names = {p.get("provider") for p in providers}
        if working_names - committed_names:
            missing = ", ".join(sorted(working_names - committed_names))
            flags.append(
                f"config split: {missing} appears only in the UNCOMMITTED "
                f"{path.name}; the committed file (main) lists only "
                f"{', '.join(sorted(committed_names)) or 'nothing'} for tier S"
            )
    return facts, flags


def _note_date(note: str) -> str:
    match = re.search(r"(\d{4}-\d{2}-\d{2})", note or "")
    return match.group(1) if match else "date unknown"


def _committed_route_providers() -> list[dict[str, Any]] | None:
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(settings.route_config.parent.parent.parent),
                "show",
                f"HEAD:{settings.route_config.relative_to(settings.route_config.parent.parent.parent)}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        return data.get("tier_aliases", {}).get("S", {}).get("providers", [])
    except (OSError, ValueError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------- #
# Flags
# --------------------------------------------------------------------------- #


def doc_copy_facts(
    repo: BrainRepo, subject: str | None, question: str = "", cap: int = 12
) -> tuple[list[dict[str, str]], list[str]]:
    """Facts from the doc copies of the subject product, plus staleness flags.

    Only facts the question (or the plan-fact patterns) care about are kept —
    a whole docs site does not belong in one answer.
    """
    facts: list[dict[str, str]] = []
    flags: list[str] = []
    if subject is None:
        return facts, flags
    now = datetime.now(UTC)
    per_page: list[dict[str, str]] = []
    for path in repo.source_paths(subject):
        page = repo.read_page(path)
        per_page.extend(_doc_copy_facts(page))
        fetched = page.meta.get("fetched_at")
        fetched_at = _parse_ts(str(fetched)) if fetched else None
        if fetched_at and (now - fetched_at).days > settings.stale_copy_days:
            flags.append(
                f"stale doc copy: {page.path} fetched {fetched_at.date()} ({(now - fetched_at).days} days ago)"
            )
    constraints = requested_scopes(question, subject)
    facts = _select_relevant(
        [
            fact
            for fact in per_page
            if matches_scope(fact.get("_scope", ""), constraints)
            and _matches_requested_aspects(fact["fact"] + ": " + fact["value"], question)
        ],
        question,
        cap,
    )
    bounded = []
    for fact in facts:
        if len(fact.get("evidence_context", "")) > EVIDENCE_CHARS or len(fact["value"]) > EVIDENCE_CHARS:
            flags.append(f"{fact['source']}: full governing doc context exceeds delivery limit; fact withheld")
        else:
            bounded.append(fact)
    facts = bounded
    recipe = repo.root / "doclayer" / "recipes" / f"{subject}.yaml"
    if repo.has_path(recipe.relative_to(repo.root).as_posix()):
        recipe_text = repo.read_text(recipe.relative_to(repo.root).as_posix())
        if "last_error: null" not in recipe_text:
            match = re.search(r"last_error: (.+)", recipe_text)
            flags.append(f"last doc refresh recorded errors ({recipe.name}): {match.group(1) if match else 'yes'}")
    return facts, flags


#: Doc copies are full of "[text](url)" links; a fact name is the text.
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def _clean_cell(text: str) -> str:
    return _MD_LINK.sub(r"\1", text).strip()


def _doc_copy_facts(page: BrainPage) -> list[dict[str, str]]:
    """Pull the plan-relevant lines out of a doc copy (models, quotas, URLs)."""
    url = page.meta.get("url", page.path)
    fetched = page.meta.get("fetched_at", "date unknown")
    tag = f"{url}@{fetched}"
    facts: list[dict[str, str]] = []
    contexts, context_headings = line_contexts(page.body, with_headings=True)
    for index, line in _noncode_lines(page.body, numbered=True):
        stripped = line.strip()
        bullet = re.match(r"^-\s*\*\*(.+?)\*\*:\s*(.*)$", _clean_cell(stripped))
        if bullet:
            if bullet.group(1).strip() in ("", "---"):
                continue
            facts.append(
                {
                    "fact": bullet.group(1).strip(),
                    "value": bullet.group(2),
                    "source": tag,
                    "evidence_context": contexts[index],
                    "_heading": context_headings[index],
                }
            )
            continue
        if re.match(r"^\|\s*[`']?\w[\w'.+-]*`?", _clean_cell(stripped)):
            first_cell = _clean_cell(stripped).strip("| ").split("|")[0].strip()
            if 2 <= len(first_cell) < 120:
                facts.append(
                    {
                        "fact": first_cell.strip("`"),
                        "value": _row_value(_clean_cell(stripped), clip=False),
                        "source": tag,
                        "evidence_context": contexts[index],
                        "_heading": context_headings[index],
                    }
                )
    for fact in facts:
        fact.update(authority_record(page.path, page.meta, source_revision=page.commit))
        fact["authority_metadata"] = authority_text(authority_record(page.path, page.meta, source_revision=page.commit))
        fact["_scope"] = source_scope(
            page.meta, title=page.title, heading=fact.get("_heading", ""), path=page.path, urls=[url]
        )
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for fact in facts:
        if fact["fact"].lower() not in seen:
            seen.add(fact["fact"].lower())
            unique.append(fact)
    return unique


#: Words a doc-copy fact must touch to be worth quoting in an answer. Doc
#: pages are long; without this the answer drowns in pricing tips.
_FACT_RELEVANCE = re.compile(
    r"model|base\s?url|endpoint|quota|credit|edition|plan|concurr|region|window|limit|api\s?key",
    re.IGNORECASE,
)

#: A model id like "qwen3.8-max", "glm-5.3" — always worth quoting when the
#: question is about models.
_MODEL_ID = re.compile(r"^[a-z][a-z0-9.]*\d[\w.]*-[a-z0-9.+-]+$")


def _select_relevant(facts: list[dict[str, str]], question: str, cap: int) -> list[dict[str, str]]:
    """Select doc facts by the question, not a fixed pricing/plan vocabulary."""
    words = _terms(question)
    ranked = []
    for fact in facts:
        score = len(words & _terms(fact["fact"])) * 2 + len(words & _terms(fact["value"]))
        if "model" in words and _MODEL_ID.match(fact["fact"].lower()):
            score += 2
        if score:
            ranked.append((score, fact))
    selected, seen = [], set()
    for _, fact in sorted(ranked, key=lambda item: item[0], reverse=True):
        if fact["fact"].lower() not in seen:
            seen.add(fact["fact"].lower())
            selected.append(fact)
        if len(selected) >= cap:
            break
    return selected


def _row_value(line: str, *, clip: bool = True) -> str:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(cells) >= 2:
        return _clip(cells[1].strip("`*")) if clip else cells[1].strip("`*")
    value = re.sub(r"^[*-]\s*\*\*", "", line).strip()
    return _clip(value) if clip else value


def _parse_ts(value: str) -> datetime | None:
    cleaned = value.strip().rstrip("Zz")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def index_lag_flag(repo: BrainRepo) -> list[str]:
    """Is the vo-rag brain index behind Git?"""
    state = brain_sync_state()
    if not state:
        return ["index state unknown: brain_sync has never run or its state file is missing"]
    last_commit = state.get("last_commit")
    if state.get("last_error"):
        flags = [f"brain_sync last error: {state['last_error']}"]
    else:
        flags = []
    if last_commit:
        try:
            head = repo.head_commit()
            if last_commit != head:
                behind = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo.root),
                        "rev-list",
                        "--count",
                        f"{last_commit}..{head}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                count = behind.stdout.strip() or "?"
                flags.append(
                    f"index behind Git: brain_sync indexed {str(last_commit)[:8]}, "
                    f"Git HEAD is {head[:8]} ({count} commits ahead); "
                    f"last sync {state.get('last_sync_at', '?')}"
                )
        except RuntimeError:
            flags.append("index state unknown: could not read Git HEAD")
    return flags


def declared_vs_running_flags(repo: BrainRepo, live_results: list[LiveResult]) -> list[str]:
    """Compare live checks against declared values in the brain pages."""
    flags: list[str] = []
    all_text = ""
    for path in repo.markdown_paths():
        # Design, acceptance transcripts and proposals are evidence of earlier
        # states, not present deployment declarations.
        if not path.startswith(("infra/", "wiki/")):
            continue
        try:
            text = (
                repo.read_text(path) if hasattr(repo, "read_text") else (repo.root / path).read_text(encoding="utf-8")
            )
            historical_headings: list[tuple[int, bool]] = []
            for line in _noncode_lines(text):
                heading = re.match(r"^(#{1,6})\s+(.+)", line)
                if heading:
                    depth = len(heading[1])
                    historical_headings = [(level, old) for level, old in historical_headings if level < depth]
                    old = bool(
                        re.search(
                            r"\b(historical|archive|superseded|proposal|initial)\b",
                            heading[2],
                            re.I,
                        )
                    )
                    historical_headings.append((depth, old))
                if not any(old for _, old in historical_headings) and not re.search(
                    r"\b(previously|formerly|historical|superseded)\b", line, re.I
                ):
                    all_text += line + "\n"
        except OSError:
            continue
    for result in live_results:
        if result.name == "vo-rag-api":
            answering = {check["what"]: check["value"] for check in result.checks if "port" in check["what"]}
            running_port = next((port for port, value in answering.items() if "answers" in value), None)
            if running_port:
                running_number = running_port.replace("port :", "")
                declared_mismatch = re.findall(r"vo-rag[^.\n]{0,80}:8000\b", all_text, re.IGNORECASE)
                if declared_mismatch and running_number == "8001":
                    flags.append(
                        "declared vs running: the brain says the vo-rag backend is on "
                        f":8000 ({declared_mismatch[0].strip()[:90]}…), but the live check "
                        f"at {result.checked_at} answers on :8001 and not on :8000"
                    )
        if result.name == "vo-rag-release" and result.ok:
            deployed = next(
                (check["value"] for check in result.checks if check["what"] == "deployed commit"),
                None,
            )
            if deployed:
                flags.append(f"observed: vo-rag release {deployed} is running as of {result.checked_at}")
                # A release a brain page declares, if it disagrees with the
                # running one, is exactly the split this layer exists to catch.
                for declared in set(
                    re.findall(
                        r"vo-rag[^.\n]{0,60}release[^.\n]{0,20}`?([0-9a-f]{7,40})",
                        all_text,
                    )
                    + re.findall(r"release[^\n]{0,30}`([0-9a-f]{7,40})`", all_text)
                ):
                    if not deployed.startswith(declared[:7]):
                        flags.append(
                            f"declared vs running: a brain page declares vo-rag release "
                            f"{declared[:8]}, but the live check at {result.checked_at} shows "
                            f"{deployed} deployed"
                        )
    return flags


def stale_observation_flags(repo: BrainRepo) -> list[str]:
    """Page-recorded observations older than the staleness threshold."""
    flags: list[str] = []
    now = datetime.now(UTC)
    for path in repo.markdown_paths():
        try:
            page = repo.read_page(path)
        except OSError:
            continue
        for row in fact_rows(page):
            if row.observed in ("", "—"):
                continue
            match = re.search(r"(\d{4}-\d{2}-\d{2})", row.observed)
            if not match:
                continue
            observed_at = _parse_ts(match.group(1))
            if observed_at and (now - observed_at).days > settings.stale_copy_days:
                flags.append(
                    f"stale observation: '{row.fact}' on {page.path} was last checked "
                    f"{observed_at.date()} ({(now - observed_at).days} days ago) — "
                    f"run a fresh live check before trusting it"
                )
    return flags


def _clip(text: str, limit: int = FACT_CHARS) -> str:
    """Cut text to at most `limit` chars on a word boundary, marking any cut with …"""
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = head.rsplit(" ", 1)[0] if " " in head else head
    return cut.rstrip(" ,;:—-") + "…"


#: Question words used for an approximate coverage diagnostic, never absence proof.
_STOPWORDS = {
    "what",
    "which",
    "where",
    "when",
    "does",
    "with",
    "have",
    "from",
    "that",
    "this",
    "there",
    "here",
    "about",
    "into",
    "over",
    "under",
    "then",
    "than",
    "tell",
    "show",
    "give",
    "using",
    "used",
    "use",
    "running",
    "current",
    "actually",
    "right",
    "now",
    "plan",
    "plans",
    # how a question is phrased, not a fact it asks for
    "machine",
    "setup",
    "name",
    "tool",
    "please",
    "know",
}


#: Instructions to the agent ("don't change anything") and qualifiers of the
#: thing asked for ("supported existing route"); coverage only, not ranking.
_COVERAGE_FRAMING = {"anything", "something", "supported", "existing", "documented"}
#: A question word covered when a fact states one of these instead.
_COVERED_BY = {"access": ("ssh", "route")}


def unknown_flags(answer: Answer, search_hits: list[dict[str, str]]) -> list[str]:
    """Approximate unresolved coverage; lexical misses do not prove source absence."""
    # Trailing punctuation ("used.", "max-") isn't part of the word.
    # A sentence telling the agent what not to do asks for no fact.
    asked = re.sub(r"(?<=[.?!])\s*(?:do not|don't|never)\b[^.?!]*[.?!]?", " ", answer.question, flags=re.I)
    tokens = (t.rstrip(".+-") for t in re.findall(r"[a-z][a-z0-9.+-]{3,}", asked.lower()))
    words = {
        w
        for w in tokens
        if len(w) >= 4 and w not in _STOPWORDS and w not in _QUERY_FILLER and w not in _COVERAGE_FRAMING
    }
    live_statements = [
        f"{check['what']} {check['value']}" for result in answer.live_results if result.ok for check in result.checks
    ]
    covered = " ".join(
        [f"{f['fact']} {f['value']} {f.get('source', '')}" for f in answer.git_facts + answer.doc_facts]
        + ([] if answer.git_facts or answer.doc_facts else [f"{h['heading']} {h['content']}" for h in search_hits])
        + live_statements
    ).lower()
    requested = _question_aspects(answer.question)
    established = set().union(
        *[_matching_aspects(f"{f['fact']} {f['value']}", answer.question) for f in answer.git_facts + answer.doc_facts],
        *[_matching_aspects(text, answer.question) for text in live_statements],
    )
    missing_aspects = requested - established
    # Size/maximum/window express the measurement question, not independent
    # missing subjects. Labels may use equivalent terminology (disk/storage).
    words = {word for word in words if not (_terms(word) & requested)}
    if requested & _MEASURE_ASPECTS:
        words -= {"size", "capacity", "maximum", "minimum", "limit", "limits", "window"}
    # "served" is covered by "serve": compare on a 5-letter stem for longer words
    missing = [
        w
        for w in sorted(words)
        if (w[:5] if len(w) > 5 else w) not in covered
        and not any(re.search(r"\b" + alt + r"\b", covered) for alt in _COVERED_BY.get(w, ()))
    ]
    missing.extend(sorted(missing_aspects))
    if requested & _MEASURE_ASPECTS:
        values = " ".join(f["value"] for f in answer.git_facts + answer.doc_facts)
        for unit in re.findall(
            r"\b(?:in|as|measured in)\s+([kmgt]i?b|bytes?|tokens?|seconds?|milliseconds?|ms)\b", answer.question, re.I
        ):
            if not re.search(r"\b" + re.escape(unit) + r"\b", values, re.I):
                missing.append(
                    f"requested unit {unit} (source values retain their stated units; no conversion established)"
                )
    if missing and not (answer.git_facts or answer.doc_facts):
        return [
            f"automatic coverage unresolved for: {', '.join(missing)} (lexical/facet checks may miss paraphrases; not proof of source absence)"
        ]
    if missing:
        joined = ", ".join(missing[:6])
        return [
            f"automatic coverage unresolved for: {joined} (lexical/facet checks may miss paraphrases; not proof of source absence)"
        ]
    return []


# --------------------------------------------------------------------------- #
# The pipeline
# --------------------------------------------------------------------------- #


def guess_subject(repo: BrainRepo, question: str, product: str | None, catalog: Any = None) -> str | None:
    """Match complete distinctive aliases, never generic slug fragments."""
    if product:
        return product
    aliases: dict[str, set[str]] = {}
    for path in repo.markdown_paths():
        try:
            page = repo.read_page(path)
        except (OSError, RuntimeError, subprocess.SubprocessError, UnicodeError):
            continue  # pipeline records the affected source during collection
        if path.startswith("sources/"):
            slug = path.split("/")[1]
            aliases.setdefault(slug, set()).add(slug)
        elif {"product", "account"} & set(page.meta.get("tags") or []):
            slug = str(page.meta.get("product") or Path(path).stem)
            aliases.setdefault(slug, set()).update([slug, page.title])
            declared = page.meta.get("aliases") or []
            if isinstance(declared, str):
                declared = [declared]
            aliases[slug].update(str(alias) for alias in declared)
    # A source directory can exist before its first copy is written.
    sources_dir = repo.root / "sources"
    if sources_dir.is_dir():
        for directory in sources_dir.iterdir():
            if directory.is_dir():
                aliases.setdefault(directory.name, set()).add(directory.name)
    query = " " + " ".join(_raw_terms(question)) + " "
    matches = []
    for slug, names in aliases.items():
        for name in names:
            terms = _raw_terms(name)
            # Complete name, or a contiguous distinctive pair: Model Studio
            # matches Alibaba Model Studio; generic "model" alone does not.
            phrases = [terms] + [terms[i : i + 2] for i in range(len(terms) - 1)]
            for phrase in phrases:
                if not phrase or all(term in _GENERIC_SUBJECT for term in phrase):
                    continue
                if " " + " ".join(phrase) + " " in query:
                    matches.append((len(phrase), slug))
    # A committed subject of the current coverage catalog names the subject when
    # the question says its own slug or title, three or more words long ("brain
    # curator configuration"), and names no other subject outside that phrase.
    # Aliases ("api tools", "model lineup") and title fragments never do, and a
    # phrase beside another subject is that subject's object, not the subject.
    if catalog is not None and getattr(catalog, "status", "") == "ok":
        subjects = getattr(catalog, "subjects", None) or {}
        named = [
            (slug, phrase)
            for slug, entry in subjects.items()
            for phrase in (" ".join(_raw_terms(name)) for name in entry.match_phrases())
            if phrase and " " + phrase + " " in query
        ]
        named += [
            (slug, phrase)
            for slug, names in aliases.items()
            for phrase in (" ".join(_raw_terms(name)) for name in names)
            if phrase and " " + phrase + " " in query
        ]
        for slug, entry in subjects.items():
            if entry.page_status != "committed":
                continue
            for name in {coverage.normalize(entry.slug), coverage.normalize(entry.title)}:
                terms = _raw_terms(name)
                phrase = " ".join(terms)
                if len(terms) < 3 or " " + phrase + " " not in query:
                    continue
                # Other subjects are looked for in the question without this phrase.
                rest = query.replace(" " + phrase + " ", " ")
                if any(other != slug and " " + found + " " in rest for other, found in named):
                    continue
                matches.append((len(terms), slug))
    if not matches:
        return None
    best = max(length for length, _ in matches)
    winners = {slug for length, slug in matches if length == best}
    return next(iter(winners)) if len(winners) == 1 else None


def _safe_index_lag(repo: BrainRepo) -> list[str]:
    if isinstance(repo, UnavailableBrainRepo):
        return []
    try:
        return index_lag_flag(repo)
    except (OSError, RuntimeError, subprocess.SubprocessError, UnicodeError) as exc:
        return [f"canonical brain index status unavailable: {exc}"]


_LOCAL_KINDS = {"host", "host-inventory", "service", "account"}

_STRONG_DOCS = re.compile(
    r"\b(docs?|documentation|usage|tutorial|getting started|install(ation|ing)?"
    r"|upgrade|migrat(?:e|ion|ing)|configure|configuring)\b"
    r"|\bhow (?:do|does|to|can|should|would)(?: [a-z]+){0,3} "
    r"(?:use|using|configure|set up|call|deploy|migrate|install|integrate)\b",
    re.IGNORECASE,
)


def _declared_api_topic_scopes(repo, catalog, hosts, question, subject, scopes):
    """Disambiguate an initial API topic using committed catalog metadata.

    Unknown/conflicting identities remain constraints. These declarations give
    query roles, never factual authority or invented model-family aliases.
    """
    match = re.match(r"^For\s+(.+?)\s+API\b", question, re.I)
    if not match or catalog.status != "ok" or not repo.root:
        return scopes
    prefix = coverage.normalize(match[1])

    def leading_identity(name):
        # Keep the leading-host role: an API's trailing label is not its host.
        for length in range(1, len(prefix.split()) + 1):
            leading = " ".join(prefix.split()[:length])
            if matches_identity(name, leading) and matches_identity(leading, name):
                return True
        return False

    for name in hosts:
        name = coverage.normalize(name)
        if leading_identity(name):
            return scopes
    for entry in catalog.subjects.values():
        if entry.kind in {"host", "service", "account", "host-inventory", "product"}:
            if any(leading_identity(name) for name in entry.match_phrases()):
                return scopes
    try:
        snapshot = BrainRepo(repo.root, snapshot=True)
    except (OSError, RuntimeError, subprocess.SubprocessError, UnicodeError):
        return scopes
    topics, api_pages = set(), []
    for entry in catalog.subjects.values():
        if entry.page_status != "committed" or entry.kind not in {"model", "model-family", "product"}:
            continue
        for ref in entry.pages:
            if ref.authority != "current":
                continue
            try:
                page = snapshot.read_page(ref.path)
            except (OSError, RuntimeError, subprocess.SubprocessError, UnicodeError):
                continue
            tags = page.meta.get("tags", [])
            if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
                continue
            tags = {coverage.normalize(tag) for tag in tags}
            if page.meta.get("subject_type") in {"model", "model-family"}:
                # Only declared numeric tags; never trim an individual model
                # name into a hypothetical family tag.
                topics.update(tag for tag in tags if re.search(r"\d", tag))
                topics.update(entry.match_phrases())
            elif page.meta.get("subject_type") == "product" and "api" in tags:
                api_pages.append((entry, tags))
    for topic in topics:
        if prefix != topic and not prefix.startswith(topic + " "):
            continue
        suffix = prefix[len(topic) :].strip()
        for entry, tags in api_pages:
            if topic not in tags:
                continue
            if not subject or coverage.normalize(subject) not in tags | entry.match_phrases():
                continue
            if suffix and suffix + " api" not in tags:
                continue
            remaining = [
                item
                for item in scopes
                if not (item.get("dimension") == "platform" and coverage.normalize(item.get("value", "")) == prefix)
            ]
            # Deduplication may have merged an explicit on/via/provider clause
            # with the initial topic. Reparse only the untouched remainder to
            # preserve that independent usage constraint too.
            for item in requested_scopes(question[match.end() :], subject):
                if item not in remaining:
                    remaining.append(item)
            return remaining
    return scopes


def _route_inputs(brain_root: Path | None) -> tuple[routing.Catalog, list[str]]:
    """Coverage catalog + host aliases for source planning; never raises.

    Reads the already-selected canonical reference (the caller selected it
    once); never re-resolves the pointer on the answering path.
    """
    if brain_root is None:
        return (
            routing.Catalog(
                status="unknown",
                revision=None,
                policy_version=0,
                reason="canonical reference unavailable",
            ),
            [],
        )
    try:
        catalog = routing.load_catalog(brain_root, settings.catalog_cache_dir, settings.routing_policy_path)
    except Exception:  # noqa: BLE001 — routing context degrades to unknown
        catalog = routing.Catalog(
            status="unknown",
            revision=None,
            policy_version=0,
            reason="route inputs unavailable",
        )
    try:
        hosts = coverage.ssh_host_aliases(Path("~/.ssh/config").expanduser(), settings.catalog_cache_dir)
    except OSError:
        hosts = []
    return catalog, hosts


def _wants_public_docs(
    question: str,
    subject: str | None,
    entry: routing.SubjectEntry | None,
    local_subject: bool,
) -> bool:
    """Public documentation fanout only when the question can use it:
    local-only subjects avoid it unless the question explicitly asks about
    product usage/configuration."""
    from doclayer.question_scope import private_question

    if private_question(question, subject=subject, entry=entry):
        return False
    strong_docs = bool(_STRONG_DOCS.search(question))
    if local_subject:
        if entry is not None and entry.kind in ("host", "host-inventory"):
            return False
        return strong_docs
    if subject is not None:
        return True
    if bool(_wants_live_state(question)) and not strong_docs:
        return False
    # A subjectless question consults public docs when it asks a product-ish
    # documentation question, not for plain code or running-state asks.
    if routing.has_code_task(question):
        return False
    return bool(routing.uncovered_candidate(question))


def _wants_brain_index(
    question: str,
    subject: str | None,
    catalog: routing.Catalog,
    hosts: list[str],
) -> bool:
    """Semantic retrieval runs when needed. A clearly uncovered
    public subject — catalog ok, no subject matched, an explicit documentation
    request naming an uncovered thing — skips it entirely; an unknown or stale
    catalog never fabricates absence, so the bounded search still runs."""
    from doclayer.question_scope import private_question

    if private_question(question) or catalog.status != "ok" or subject is not None:
        return True
    candidate = routing.uncovered_candidate(question)
    if not candidate:
        return True
    if routing.covers_candidate(catalog, candidate, hosts):
        return True
    # Local-context questions may still be about an installed subject even
    # when the phrasing looks like a docs request.
    return routing.has_local_context(question) and not bool(_STRONG_DOCS.search(question))


async def resolve(
    question: str,
    *,
    product: str | None = None,
    live: bool | None = None,
    repo: BrainRepo | None = None,
    force: bool | None = None,
    capture: bool | None = None,
) -> Answer:
    """Answer one question. Never raises for a source being down.

    ``force`` bypasses negative-cache suppression for an explicit retry;
    ``capture=False`` disables knowledge-gap recording (verification requests
    must not generate their own demand).
    """
    configuration_flags = []
    if repo is None:
        try:
            root, snapshot = settings.brain_repo_selection()
            repo = BrainRepo(root, snapshot=snapshot)
        except (
            BrainRepoConfigurationError,
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
            UnicodeError,
        ) as exc:
            configuration_flags.append(str(exc))
            repo = UnavailableBrainRepo()
    catalog, hosts = _route_inputs(repo.root)
    from doclayer.question_scope import private_question

    uncovered = routing.uncovered_candidate(question)
    clearly_uncovered = bool(
        not private_question(question)
        and not product
        and uncovered
        and catalog.status == "ok"
        and not routing.covers_candidate(catalog, uncovered, hosts)
        and (not routing.has_local_context(question) or _STRONG_DOCS.search(question))
    )
    try:
        # The already-current catalog supplies this negative routing decision;
        # do not scan local page bodies or inspect the index for a public miss.
        subject = None if clearly_uncovered else guess_subject(repo, question, product, catalog)
    except (OSError, RuntimeError, subprocess.SubprocessError, UnicodeError) as exc:
        configuration_flags.append(f"canonical brain source unavailable: {exc}")
        repo = UnavailableBrainRepo()
        subject = product
    answer = Answer(
        question=question,
        subject=subject,
        git_source_available=not isinstance(repo, UnavailableBrainRepo),
    )
    answer.scope_constraints = _declared_api_topic_scopes(
        repo, catalog, hosts, question, subject, answer.scope_constraints
    )
    answer.flags.extend(configuration_flags)

    entry = catalog.subjects.get(subject) if subject else None
    local_subject = bool(entry and (entry.kind in _LOCAL_KINDS or entry.local_first)) or (
        subject is not None and re.sub(r"[-_]+", " ", subject.lower()).strip() in hosts
    )
    answer.intent = INTENT_EXPLICIT
    source_calls = answer.source_calls

    want_live = live if live is not None else _wants_live_state(question)
    # Context7 wants product names, not repo slugs.
    c7_subject = re.sub(r"[-_]+", " ", answer.subject).strip() if answer.subject else None
    planned = {
        "brain_index": _wants_brain_index(question, subject, catalog, hosts),
        "public_docs": _wants_public_docs(question, subject, entry, local_subject),
    }
    if not planned["public_docs"]:
        if private_question(question, subject=subject, entry=entry):
            answer.skipped_sources["public_docs"] = (
                "local/private question; public documentation cannot establish local facts"
            )
        elif local_subject:
            answer.skipped_sources["public_docs"] = "local-only subject; no public-doc fanout"
        elif bool(_wants_live_state(question)):
            answer.skipped_sources["public_docs"] = "running-state question; public documentation would not observe it"
        elif subject is None:
            answer.skipped_sources["public_docs"] = "unidentified subject; public documentation target not established"
    if not planned["brain_index"]:
        answer.skipped_sources["brain_index"] = "clearly uncovered public subject; bounded external lookup applies"
    # Negative cache: an observed recent miss may suppress the planned network
    # sources. Git reads and live-state checks are never suppressed.
    negative_cache = NegativeCache(settings.negative_cache_path)
    index_commit = None
    if planned["brain_index"]:
        try:
            index_commit = (brain_sync_state() or {}).get("last_commit")
        except Exception:  # noqa: BLE001 — cache binding degrades honestly
            index_commit = None
    generation = evidence_generation(
        (repo.head_commit() if hasattr(repo, "head_commit") else "") if planned["brain_index"] else catalog.revision,
        index_commit,
        catalog.policy_version,
    )
    suppression = None
    if not force and (planned["brain_index"] or planned["public_docs"]):
        try:
            suppression = negative_cache.check(
                question,
                subject,
                scopes=answer.scope_constraints,
                sources=[name for name, wanted in planned.items() if wanted],
                generation=generation,
            )
        except Exception:  # noqa: BLE001 — a cache failure must not block lookup
            suppression = None
    if suppression:
        for name in list(planned):
            if name in suppression.checked_sources:
                planned[name] = False
                answer.skipped_sources[name] = (
                    f"recent lookup did not answer this request ({suppression.recorded_at[:10]}); use force to retry"
                )
        answer.flags.append(
            f"recent lookup did not answer this request "
            f"({suppression.recorded_at[:10]}; checked "
            f"{', '.join(suppression.checked_sources)}); not a fact about the "
            "subject — force a retry when needed"
        )

    tasks = []
    if planned["brain_index"]:
        tasks.append(("brain_index", query_vo_rag(question)))
    else:
        tasks.append(("brain_index", asyncio.sleep(0, result=([], None))))
    if planned["public_docs"]:
        tasks.append(("public_docs", query_context7(question, c7_subject)))
    else:
        tasks.append(("public_docs", asyncio.sleep(0, result={})))
    tasks.append(
        (
            "route_config",
            asyncio.to_thread(route_facts) if _wants_routes(question, subject) else asyncio.sleep(0, result=([], [])),
        )
    )
    tasks.append(
        (
            "index_state",
            asyncio.to_thread(_safe_index_lag, repo) if planned["brain_index"] else asyncio.sleep(0, result=[]),
        )
    )
    live_names = _applicable_live_checks(question, subject) if want_live else []
    if live_names:
        tasks.append(("live_checks", run_checks(names=live_names)))
    elif want_live:
        answer.flags.append(
            f"live verification unavailable: no applicable implemented live check for {subject or 'the requested subject'}; "
            "dated source statements below are not a fresh observation of that subject"
        )
    gathered = await asyncio.gather(*(task for _, task in tasks))
    results = dict(zip([name for name, _ in tasks], gathered))
    vo_rag = results["brain_index"]
    context7 = results["public_docs"]
    route = results["route_config"]
    lag = results["index_state"]
    if planned["brain_index"]:
        source_calls["brain_index"] = source_calls.get("brain_index", 0) + 1
    if planned["public_docs"]:
        source_calls["public_docs"] = source_calls.get("public_docs", 0) + 1
    if live_names:
        source_calls["live_checks"] = source_calls.get("live_checks", 0) + 1
    answer.search_hits, answer.search_error = vo_rag
    for hit in answer.search_hits:
        if hit.get("_context_error"):
            answer.unknowns.append(f"{hit['path']}@{hit['commit']}: {hit['_context_error']}; passage withheld")
    answer.context7 = context7
    if context7.get("context_error"):
        answer.unknowns.append(context7["context_error"])
    answer.route_facts, route_flags = route
    answer.flags.extend(route_flags)
    answer.flags.extend(lag)
    if live_names:
        answer.live_results = [result for result in results["live_checks"] if result.name in live_names]
    try:
        if live_names:
            answer.flags.extend(declared_vs_running_flags(repo, answer.live_results))
        # Rank all committed candidates together. A search hit is discovery,
        # not authority, and cannot consume the exact-fact budget before Git.
        for path in [] if clearly_uncovered else repo.markdown_paths():
            if path == "log.md" or path.endswith("/log.md") or path.startswith("sources/"):
                continue
            try:
                _append_page_facts(answer, repo.read_page(path), question)
            except (
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
                UnicodeError,
            ) as exc:
                answer.flags.append(f"canonical brain page unavailable: {path}: {exc}")
        answer.git_facts = _rank_facts(answer.git_facts, question, cap=8)
        if not clearly_uncovered:
            source_calls["brain_git"] = 1
        if answer.subject:
            doc_facts, doc_flags = doc_copy_facts(repo, answer.subject, question=question)
            answer.doc_facts = doc_facts
            answer.flags.extend(doc_flags)
            source_calls["doc_copies"] = source_calls.get("doc_copies", 0) + 1

    except (OSError, RuntimeError, subprocess.SubprocessError, UnicodeError) as exc:
        answer.flags.append(f"canonical brain source unavailable: {exc}; check the reference sync status")
        answer.git_facts = _rank_facts(answer.git_facts, question, cap=8)
        answer.doc_facts = []
        answer.git_source_available = False
        repo = UnavailableBrainRepo()

    # With doc copies in hand, judge Context7 against the actual facts: does
    # its text state any of them? A long unrelated page is still not an answer
    # ("a Context7 hit is not the same as an answer").
    if answer.doc_facts and not answer.context7.get("error"):
        c7_text = str(answer.context7.get("summary", "")).lower()
        tokens = {fact["fact"].lower() for fact in answer.doc_facts}
        tokens.update(
            re.sub(r"^[`'<]*https?://([^/>`'>]+).*$", r"\1", fact["value"].lower())
            for fact in answer.doc_facts
            if "http" in fact["value"]
        )
        answer.context7["lacks_facts"] = not any(token in c7_text for token in tokens if token)

    try:
        selected_paths = {fact["source"].split("@", 1)[0] for fact in answer.git_facts}
        answer.flags.extend(
            flag
            for flag in (stale_observation_flags(repo) if selected_paths else [])
            if any(path in flag for path in selected_paths)
        )
    except (OSError, RuntimeError, subprocess.SubprocessError, UnicodeError) as exc:
        answer.flags.append(f"canonical brain source unavailable: {exc}; check the reference sync status")
    if answer.scope_constraints:
        scope = source_scope(
            {}, title=str(answer.context7.get("library", "")), urls=source_urls(str(answer.context7.get("source", "")))
        )
        if not matches_scope(scope, answer.scope_constraints):
            answer.context7["lacks_scope"] = True
        if answer.context7.get("summary") and not _matches_requested_aspects(answer.context7["summary"], question):
            answer.context7["lacks_facts"] = True
        indexed_applicable = any(
            hit.get("content")
            and matches_scope(hit.get("_scope", ""), answer.scope_constraints)
            and _matches_requested_aspects(hit["content"], question)
            for hit in answer.search_hits
        )
        context_applicable = answer.context7.get("summary") and not any(
            answer.context7.get(key) for key in ("lacks_scope", "lacks_facts", "error")
        )
        if not (answer.git_facts or answer.doc_facts or indexed_applicable or context_applicable):
            requested = ", ".join(f"{item['dimension']}={item['value']}" for item in answer.scope_constraints)
            answer.unknowns.append(
                f"requested applicability is unknown: {requested}; other platforms, editions or versions do not establish the requested facts"
            )
    if not (answer.git_facts or answer.doc_facts) and any(
        hit.get("content") and not hit.get("_context_attested") for hit in answer.search_hits
    ):
        answer.flags.append(
            "legacy indexed passage governing-context completeness is unverified; excerpts are not a current fully qualified canonical fact"
        )
    answer.unknowns.extend(unknown_flags(answer, answer.search_hits))

    # ------------------------------------------------------------------ #
    # Structured outcome: checked/skipped sources and dispositions.
    # ------------------------------------------------------------------ #
    answer.checked_sources = [
        name
        for name in ("brain_git", "brain_index", "public_docs", "route_config", "live_checks")
        if source_calls.get(name)
    ]
    subject_pages_exist = _subject_pages_exist(repo, subject) if not answer.scope_constraints else False
    answer.dispositions = _dispositions(answer, lag_flags=lag, called=planned, subject_pages_exist=subject_pages_exist)

    # Record observed unsuccessful attempts (never errors, lag or ambiguous
    # applicability).
    misses: list[str] = []
    if planned["brain_index"] and not answer.search_error and not answer.search_hits and not lag:
        misses.append("brain_index")
    if (
        planned["public_docs"]
        and not answer.context7.get("error")
        and (not answer.context7 or answer.context7.get("lacks_facts"))
    ):
        misses.append("public_docs")
    planned_network = [name for name, wanted in planned.items() if wanted]
    if misses and not suppression:
        try:
            negative_cache.record(
                question,
                subject,
                scopes=answer.scope_constraints,
                sources=planned_network,
                generation=generation,
            )
        except Exception:  # noqa: BLE001 — cache failures never break answers
            pass

    early = coverage.early_project(catalog, question)
    if early:
        # A project too new to document is not a knowledge gap: point at its folder.
        slug, project = early
        where = f"; its folder {project['path']} is the source of truth" if project.get("path") else ""
        answer.flags.append(
            f"{slug} is listed as an early-stage project (routing-policy.yaml early_projects), "
            f"so the brain does not document it yet{where}; no knowledge gap recorded"
        )
        return answer

    _maybe_capture_gap(
        answer,
        question=question,
        subject=subject,
        entry=entry,
        catalog=catalog,
        capture=capture,
        planned=planned,
        generation=generation,
    )
    return answer


def _subject_pages_exist(repo: Any, subject: str | None) -> bool:
    """Committed pages exist whose identity names the subject — the raw
    material for a retrieval-defect suspicion when nothing was established."""
    if not subject:
        return False
    terms = [t for t in _raw_terms(subject) if t not in _GENERIC_SUBJECT]
    if not terms:
        return False
    try:
        paths = repo.markdown_paths()
    except Exception:  # noqa: BLE001
        return False
    for path in paths:
        if path.startswith("sources/") or path == "log.md" or path.endswith("/log.md"):
            continue
        try:
            page = repo.read_page(path)
        except Exception:  # noqa: BLE001
            continue
        identity = _raw_terms(
            path
            + " "
            + page.title
            + " "
            + str(page.meta.get("product", ""))
            + " "
            + " ".join(str(t) for t in page.meta.get("tags", []) or [])
            + " "
            + str(page.meta.get("aliases", ""))
        )
        if all(term in identity for term in terms):
            return True
    return False


def requested_answer_facets(question: str) -> set[str]:
    facets = _question_aspects(question)
    if re.search(r"\b(?:what|which)\s+(?:[\w.-]+\s+){0,2}models?\b", question, re.I):
        facets.add("model")
    return facets


def answer_evidence(answer: Answer) -> list[dict]:
    """Complete applicable evidence actually eligible for answer delivery.

    Search membership is not a citation, headings are not factual assertions,
    and a category word or unrelated fact cannot establish a requested facet.
    This deterministic inventory is necessary, never semantic publication proof.
    """
    requested = requested_answer_facets(answer.question)
    result = []
    rendered = answer._prose()
    material = [("git", item) for item in answer.git_facts] + [("doc", item) for item in answer.doc_facts]
    if not material:
        material = [("index", hit) for hit in answer.search_hits if hit.get("_context_attested")]
    for kind, item in material:
        statement = str(item.get("value", item.get("content", "")))
        context = str(item.get("evidence_context", item.get("content", "")))
        source = str(item.get("source", ""))
        if kind == "index":
            source = str(item.get("path", "")) + "@" + str(item.get("source_revision", item.get("commit", "")))
        if not source or not statement.strip() or not context.strip() or len(context) > EVIDENCE_CHARS:
            continue
        quoted = "\n".join("> " + line for line in context.splitlines())
        if context not in rendered and quoted.rstrip() not in rendered:
            continue  # whole unit withheld or not actually selected for delivery
        if _wants_live_state(answer.question) and item.get("_historical") == "yes":
            continue
        if not matches_scope(item.get("_scope", ""), answer.scope_constraints):
            continue
        facets = _matching_aspects(str(item.get("fact", "")) + ": " + statement, answer.question)
        if _numeric_question(answer.question):
            for facet in requested & _MEASURE_ASPECTS:
                if not _has_quantity(statement, facet):
                    facets.discard(facet)
        requested_units = re.findall(
            r"\b(?:in|as|measured in)\s+([kmgt]i?b|bytes?|tokens?|seconds?|milliseconds?|ms)\b", answer.question, re.I
        )
        if requested_units and any(
            not re.search(r"\b" + re.escape(unit) + r"\b", statement, re.I) for unit in requested_units
        ):
            facets -= _MEASURE_ASPECTS
        if "model" in requested and _matches_requested_aspects(statement, answer.question):
            facets.add("model")
        if requested and not facets:
            continue
        # A complete delivered source can be useful without a recognized facet.
        # Empty facets never establish answered; semantic verification remains
        # necessary for generic needs.
        result.append(dict(kind=kind, source=source, statement=statement, context=context, facets=sorted(facets)))
    return result


def _dispositions(
    answer: Answer,
    *,
    lag_flags: list[str],
    called: dict[str, bool],
    subject_pages_exist: bool = False,
) -> list[str]:
    """Multiple dispositions may coexist. Lexical token misses alone
    never establish document absence or unsuccessful answering."""
    disp: set[str] = set()
    candidates = answer_evidence(answer)
    requested = requested_answer_facets(answer.question)
    covered = set().union(*(set(item["facets"]) for item in candidates)) if candidates else set()
    evidence = bool(requested and requested <= covered)
    indexed_applicable = False  # included only through complete answer_evidence
    c7_ok = False  # external snippets are never automatic closure proof
    live_ok = False  # live observation presence is not requested-facet proof
    if evidence:
        disp.add("answered")
    elif candidates:
        disp.add("partial")
    checked_anything = bool(answer.checked_sources) or bool(answer.skipped_sources) or any(called.values())
    if not (candidates or indexed_applicable or c7_ok or live_ok) and checked_anything:
        disp.add("not_found_in_checked_sources")
    if (
        answer.search_error
        or answer.context7.get("error")
        or any(not result.ok for result in answer.live_results)
        or not answer.git_source_available
    ):
        disp.add("source_unavailable")
    if lag_flags:
        disp.add("index_lag")
    if any("withheld" in u for u in answer.unknowns):
        disp.add("evidence_withheld")
    if answer.skipped_sources and not (candidates or indexed_applicable or c7_ok):
        disp.add("not_found_in_checked_sources")
    # Committed pages name the subject, yet nothing was established and the
    # index returned no applicable passage without an error: retrieval is
    # suspected, never proven.
    if (
        subject_pages_exist
        and not (evidence or indexed_applicable or c7_ok)
        and called.get("brain_index")
        and not answer.search_error
        and not answer.scope_constraints
    ):
        disp.add("retrieval_suspected")
    return sorted(disp)


def _maybe_capture_gap(
    answer: Answer,
    *,
    question: str,
    subject: str | None,
    entry: routing.SubjectEntry | None,
    catalog: routing.Catalog,
    capture: bool | None,
    planned: dict[str, bool],
    generation: str | None = None,
) -> None:
    """Record the resolver's actual unsuccessful/partial outcomes in the
    durable ledger. Never raises; never runs for verification requests."""
    if capture is False or not settings.gap_capture_enabled:
        return
    try:
        candidate = routing.uncovered_candidate(question)
        # Documentation needs only: plain code questions are not gaps.
        if not (routing.has_docs_need(question) or candidate or routing.is_question(question)):
            return
        if routing.has_code_task(question) and not candidate and not routing.has_docs_need(question):
            return
        answered = "answered" in answer.dispositions and "partial" not in answer.dispositions
        if answered:
            return
        # An explicitly named uncovered subject is missing coverage — the
        # routing call did not depend on any failed lookup. Outages, lag and
        # withheld evidence on their own are infrastructure.
        if candidate:
            need_class = "missing_coverage"
        elif "partial" in answer.dispositions:
            need_class = "missing_coverage"
        elif "retrieval_suspected" in answer.dispositions:
            need_class = "retrieval_defect"
        elif (
            "source_unavailable" in answer.dispositions
            or "index_lag" in answer.dispositions
            or "evidence_withheld" in answer.dispositions
        ):
            need_class = "infrastructure"
        elif answer.flags and any("no applicable implemented live check" in flag for flag in answer.flags):
            need_class = "needs_local_evidence"
        else:
            need_class = "missing_coverage"
        checked = [{"source": name, "outcome": "checked"} for name in answer.checked_sources]
        detail_bits = [f"question: {question[:400]}"]
        if answer.subject:
            detail_bits.append(f"subject: {answer.subject}")
        if answer.search_error:
            detail_bits.append(f"brain index error: {answer.search_error}")
        if answer.context7.get("error"):
            detail_bits.append(f"public docs error: {answer.context7['error']}")
        for unknown in answer.unknowns[:6]:
            detail_bits.append(f"unresolved: {unknown[:200]}")
        import uuid

        from doclayer.gap_client import GapClient

        ledger = GapClient()
        ledger.submit_event(
            need_class=need_class,
            subject=subject,
            representative_question=question,
            facets=sorted(requested_answer_facets(question)),
            catalog_subject=bool(subject and subject in catalog.subjects),
            applicability=[
                {"dimension": str(item["dimension"]), "value": str(item["value"])} for item in answer.scope_constraints
            ],
            checked_sources=checked,
            dispositions=list(answer.dispositions),
            coverage_revision=catalog.revision,
            evidence_generation=hashlib.sha256(generation.encode()).hexdigest() if generation else None,
            generation_attested=bool(catalog.status == "ok" and catalog.revision and generation),
            detail="; ".join(detail_bits)[:1900],
            event_key="evt-" + uuid.uuid4().hex,
        )
    except Exception:  # noqa: BLE001 — capture must never break answering
        pass


_FACT_ASPECTS = {
    "address",
    "port",
    "version",
    "approval",
    "prompt",
    "context",
    "output",
    "quota",
    "endpoint",
    "parameter",
    "storage",
    "memory",
    "connection",
    "bandwidth",
    "timeout",
    "rate",
    "latency",
    "throughput",
    "caching",
    "scheduling",
    "prefix",
}
_MEASURE_ASPECTS = {"context", "output", "quota", "storage", "memory", "connection", "bandwidth", "timeout", "rate"}


def _question_aspects(question: str) -> set[str]:
    aspects = _terms(question) & _FACT_ASPECTS
    if re.search(r"\b(?:paths?|director(?:y|ies))\b", question, re.I):
        aspects.add("path")
    if re.search(r"\bcontainer\s+names?\b", question, re.I):
        aspects.add("container")
    if re.search(r"\bread[ -]only\s+(?:checks?|commands?)\b", question, re.I):
        aspects.add("command")
    if re.search(r"\bconversation\s+history\b", question, re.I):
        aspects.add("context")
    if re.search(r"\b(?:generate|reply)\b", question, re.I):
        aspects.add("output")
    if re.search(r"\b(?:choose|select|compare|decide|pick)\b|\bchoice\s+between\b", question, re.I):
        aspects.add("selection")
    if re.search(
        r"\b(?:cache|cached|caching)\b|\b(?:repeated|reusable|shared)\s+(?:prompt\s+)?prefix(?:es)?\b", question, re.I
    ):
        aspects.add("caching")
    if re.search(r"\b(?:scheduling|scheduled|priority|queues?|queueing)\b", question, re.I):
        aspects.add("scheduling")
    return aspects


def _numeric_question(question: str) -> bool:
    return bool(
        re.search(
            r"\b(?:how much|how many|how long|size|capacity|capacities|count|max|min|maximum|minimum|limit|limits)\b",
            question,
            re.I,
        )
        or re.search(r"\b(?:what|which)\b.*\bwindow\b", question, re.I)
    )


def _requested_fact_owners(question: str) -> set[str]:
    """Explicit component targets, rather than every name in the question."""
    owners = set()
    pattern = (
        r"([\w.-]+(?:\s*(?:/|,|and)\s*[\w.-]+)*)\s+"
        r"(?:(?:runtime\s+)?versions?|deployment|compose\s+director(?:y|ies)|container\s+names?)\b"
    )
    for match in re.finditer(pattern, question, re.I):
        owners.update(re.split(r"\s*(?:/|,|\band\b)\s*", match[1].lower()))
    for match in re.finditer(r"\bversions?\s+of\s+([\w.-]+(?:\s*(?:/|,|and)\s*[\w.-]+)*)", question, re.I):
        owners.update(re.split(r"\s*(?:/|,|\band\b)\s*", match[1].lower()))
    return owners - {"current", "live", "runtime", "the", "their", "its", "our", "all"}


def _has_quantity(text: str, aspect: str) -> bool:
    # Numeric identifiers, dates and citation URLs do not establish quantities.
    text = re.sub(r"https?://[^\s\]>)]+", "", text)
    context_words = {
        "byte",
        "bytes",
        "size",
        "sizes",
        "sized",
        "free",
        "used",
        "avail",
        "available",
        "capacity",
        "block",
        "blocks",
        "count",
        "counts",
    }
    percent_words = context_words | {"full", "usage", "utilization", "utilisation"}
    # A percentage of another resource is not a size, whatever "usage" it names.
    other_resources = {
        "cpu",
        "gpu",
        "load",
        "network",
        "battery",
        "latency",
        "success",
        "error",
        "errors",
        "cache",
        "hit",
        "idle",
        "iowait",
        "steal",
        "packet",
        "loss",
    }
    time_units = {
        "s",
        "h",
        "hr",
        "hrs",
        "second",
        "seconds",
        "sec",
        "secs",
        "minute",
        "minutes",
        "min",
        "mins",
        "hour",
        "hours",
        "day",
        "days",
        "ms",
        "millisecond",
        "milliseconds",
        "week",
        "weeks",
        "month",
        "months",
        "year",
        "years",
    }
    link_words = {"is", "are", "was", "were", "of", "at", "about", "around", "approximately", "=", ":"}
    # What makes an attached k/M/G/T/P a size: the facet's own descriptive words.
    # The facet's name itself counts only when it is not the noun right after the number.
    own_words = {
        "storage": {
            "disk",
            "disks",
            "harddisk",
            "hdd",
            "ssd",
            "nvme",
            "volume",
            "volumes",
            "drive",
            "drives",
            "pool",
            "filesystem",
            "partition",
            "space",
            "lv",
        },
        "memory": {"ram", "mem", "heap", "swap", "vram", "dimm"},
        "quota": {"allowance", "limit", "limits", "cap", "budget"},
        "output": {"response", "completion"},
    }
    shared_words = {"size", "sizes", "sized", "used", "avail", "available", "capacity", "byte", "bytes"}
    # A table whose header or heading names one of the facet's own words makes its shorthand cells sizes.
    size_words = {"size", "sizes", "usage", "used", "avail", "available", "free", "capacity"}
    memory_words = {"ram", "mem", "memory", "heap", "rss", "swap"}
    storage_words = {"disk", "du", "df", "filesystem", "volume", "storage"}
    table_words = {
        "storage": size_words | storage_words | {"fs", "ssd", "hdd", "nvme", "drive"},
        "memory": memory_words,
        "quota": {"quota", "limit", "limits", "allowance"},
    }.get(aspect, size_words)
    # A table or heading that names the other resource, or is about a network or a tier, gives no size context.
    veto_words = {"storage": memory_words, "memory": storage_words}.get(aspect, set()) | {"network", "tier"}
    date_labels = {"date", "dated", "year", "since", "updated", "last", "modified", "created"}
    function_words = {
        "on",
        "of",
        "in",
        "for",
        "per",
        "at",
        "and",
        "with",
        "is",
        "was",
        "as",
        "by",
        "from",
        "to",
        "the",
        "a",
    }

    def not_a_quantity(match):
        # An address, version, hash, port or ticket number is an identifier, never a quantity.
        return " " + "x" * len(match.group(0)) + " " if match.group(0).strip() else match.group(0)

    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2}|:\d+)?(?!\d)", not_a_quantity, text)  # IPv4, CIDR, :port
    text = re.sub(r"\b\d+(?:\.\d+){2,}\b", not_a_quantity, text)  # x.y.z versions
    text = re.sub(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,}\b", not_a_quantity, text, flags=re.I)  # hashes
    bare = r"\d+(?!\d)(?!\s*(?:[kmgtp]i?b?|bytes?|tokens?|requests?|seconds?|secs?|ms|minutes?|hours?|s|h|%)\b)"
    text = re.sub(
        r"\bports?\b\s*[:=]?\s*" + bare + r"(?:\s*(?:,|/|&|\band\b|\bor\b)\s*" + bare + r")*",
        not_a_quantity,
        text,
        flags=re.I,
    )  # port(s)
    text = re.sub(
        r"(?:\b(?:PRs?|MRs?|pull requests?|issues?|tickets?|bugs?|CVE|RFC)\b[ \t]*[-:#]?[ \t]*"
        + bare
        + r"(?:-\d+)*|#"
        + bare
        + r")",
        not_a_quantity,
        text,
        flags=re.I,
    )  # PR / issue / ticket numbers
    text = re.sub(
        r"(?<=[A-Za-z0-9\])]):\d{1,5}\b",
        lambda m: (
            " " + "x" * len(m.group(0))
            if not re.search(
                r"\b(?:" + "|".join(sorted(context_words)) + r")$", text[max(0, m.start() - 12) : m.start()], re.I
            )
            else m.group(0)
        ),
        text,
    )  # host:port
    known = {
        "context": r"tokens?",
        "output": r"tokens?|bytes?|[kmgtp]i?b",
        "storage": r"bytes?|[kmgtp]i?b",
        "memory": r"bytes?|[kmgtp]i?b",
        "bandwidth": r"[kmgt]bps|bits?\s*/\s*s|bytes?\s*/\s*s",
        "timeout": r"seconds?|milliseconds?|minutes?|hours?|ms",
        "rate": r"rpm|tpm|rps|requests?\s*/\s*(?:s|second|minute)",
        "connection": r"connections?|sessions?",
        # A quota is a size or a count of units of work.
        "quota": r"tokens?|bytes?|[kmgtp]i?b|requests?|calls?|messages?|jobs?|connections?|sessions?|workers?|threads?|rpm|tpm|rps",
    }
    units = known.get(
        aspect, r"tokens?|bytes?|[kmgtp]i?b|requests?|connections?|sessions?|workers?|threads?|seconds?|rpm|tpm|rps"
    )
    # Sizes also read as shorthand attached to the number (931.5G, 16Gi) when the
    # facet's own words are beside it; a percentage or a bare byte count needs a
    # size word beside the number.
    sized = aspect in ("output", "storage", "memory", "quota") or aspect not in known
    counted = aspect in ("storage", "memory")
    percent_ok = aspect in ("storage", "memory", "quota")
    number = r"(?<![\w.-])\d[\d,]*(?:\.\d+)?"
    unit_header = re.compile(r"\b(?:" + units + r")\b", re.I)

    def tokens_beside(clause, found):
        def clean(chunk):
            return [t.strip(".,:;()[]`*|\"'").lower() for t in chunk.split()]

        return clean(clause[: found.start()])[-3:], clean(clause[found.end() :])[:3]

    def beside(clause, found, words):
        """A word of `words` at most three tokens from the number, in the same clause."""
        before, after = tokens_beside(clause, found)
        return any(token in words for token in before + after)

    def adjacent(clause, found, words):
        """A word of `words` right before the number (a link word such as "is" may sit between) or right after it."""
        before, after = tokens_beside(clause, found)
        before = [t for t in before if t]
        while before and before[-1] in link_words:
            before.pop()
        return bool(before and before[-1] in words) or bool(after and after[0] in words)

    def own_context(clause, found):
        before, after = tokens_beside(clause, found)
        near = set(before + after) & (shared_words | own_words.get(aspect, set()))
        # The facet's name right after the number is a modifier ("3k storage nodes").
        if near or aspect in before or aspect in after[1:]:
            return True
        # "free"/"total" beside the number count unless a noun follows it: "374G free" passes,
        # "5G free plan", "free 5G plan" and "a total of 3k nodes" do not.
        raw_after = clause[found.end() :].split()
        following = raw_after[0] if raw_after else ""
        if after and after[0] in ("free", "total"):
            following = raw_after[1] if len(raw_after) > 1 else ""
            beside_ok = True
        else:
            trimmed = [t for t in before if t]
            while trimmed and trimmed[-1] in link_words:
                trimmed.pop()
            beside_ok = bool(trimmed and trimmed[-1] in ("free", "total"))
        return beside_ok and (
            not following or not following[0].isalpha() or following.strip(".,;:").lower() in function_words
        )

    def number_in(clause, table_sized=False, cell=False):
        # `cell`: a header-less table row quoted with a size word or a path; `table_sized`: its header or heading names one.
        if re.search(number + r"(?:[kmbt])?[ \t]*[*|]*[ \t]*(?:" + units + r")\b", clause, re.I):
            return True
        if sized:
            for found in re.finditer(number + r"[kmgtp]i?b?(?![A-Za-z0-9])", clause, re.I):
                if table_sized or own_context(clause, found):
                    return True
            # A cell holding only a shorthand size, in a table quoted without its header.
            if (
                cell
                and aspect in ("storage", "memory")
                and re.fullmatch(number + r"[kmgtp]i?b?", clause.strip(" `*_"), re.I)
            ):
                return True
        if percent_ok:
            for found in re.finditer(number + r"[ \t]*%", clause):
                before, after = tokens_beside(clause, found)
                if beside(clause, found, percent_words) and not (set(before + after) & other_resources):
                    return True
        if counted:
            for found in re.finditer(r"(?<![\w.,-])(?:\d{1,3}(?:,\d{3})+|\d{4,})(?![\w-]|\.\d)", clause):
                digits = found.group(0)
                before, after = tokens_beside(clause, found)
                if after and after[0] in time_units:
                    continue  # a duration, not a size
                if re.fullmatch(r"(?:19|20)\d\d", digits):
                    if clause[: found.start()].rstrip().endswith(":"):
                        # "Last used: 2024" is a year, "Size of /data: 2048" and "Free blocks: 2048" are sizes.
                        if (
                            date_labels & set(before)
                            or re.search(r"\bas\s+of\b[^:]*:\s*$", clause[: found.start()], re.I)
                            or not set(before) & context_words
                        ):
                            continue
                    elif not adjacent(clause, found, context_words):
                        continue  # a bare year
                if beside(clause, found, context_words):
                    return True
        # A count's label supplies its unit; a naked number cannot establish bytes,
        # duration or token limits when its unit is absent.
        if aspect == "connection" and "connection" in _terms(clause):
            return bool(re.search(r"(?<![\w.-])\d[\d,]*(?![\w.-])", clause))
        return bool(re.search(r"\b(?:unlimited|unbounded|no finite limit)\b", clause, re.I))

    def quantity_in(segment, table_sized=False, cell=False):
        # A neighboring facet's number cannot supply this facet's missing value.
        clauses = re.split(
            r";|\.(?=\s+[A-Z])|\b(?:and|but|while)\b(?=\s+(?:(?:maximum|minimum|total|available)\s+)?(?:context|output|storage|memory|connections?|bandwidth|timeout|rate|quota)\b)",
            segment,
            flags=re.I,
        )
        if len(clauses) > 1:
            relevant = [clause for clause in clauses if aspect in _terms(clause)]
            if relevant:
                return any(number_in(clause, table_sized, cell) for clause in relevant)
        return number_in(segment, table_sized, cell)

    lines = text.splitlines()

    def cells_of(line):
        return [cell.strip() for cell in line.strip().strip("|").split("|")]

    def is_divider(row):
        return bool(row) and all(re.fullmatch(r":?-{2,}:?", cell) for cell in row if cell)

    def names_a_unit(header):
        if unit_header.search(header):
            return True
        # A percent column counts only when it is a share of a size ("Used %"), not of CPU or load.
        words = {t.strip(".,:;()[]`*|\"'").lower() for t in header.replace("%", " % ").split()}
        return (
            percent_ok
            and ("%" in words or "percent" in words)
            and bool(words & percent_words)
            and not words & other_resources
        )

    def words_of(text_):
        return {t.strip(".,:;()[]`*|\"'#").lower() for t in text_.split()}

    def names_size(text_):
        return bool(words_of(text_) & table_words)

    own_nouns = {"storage": storage_words | {"fs", "ssd", "hdd", "nvme", "drive"}, "memory": memory_words}.get(
        aspect, set()
    )

    def excluded(text_):
        # The other resource, a network or a tier is named.
        low = text_.lower()
        return bool(words_of(text_) & veto_words) or (aspect == "memory" and ("/mnt" in low or "/data" in low))

    def column_context(title, heading):
        """Size context of a cell: its own column header, else the nearest heading. Exclusion
        words cancel only inside the label that gave the context, and not when an own-resource
        noun sits beside them ("Storage tier", "Network storage"); other columns and other
        body cells never cancel it."""
        if title and excluded(title) and not words_of(title) & own_nouns:
            return False  # a column naming the other resource never takes its context from the heading
        for label in (title, heading):
            if names_size(label):
                return not excluded(label) or bool(words_of(label) & own_nouns)
        return False

    def real_path(text_):
        # starts with "/" and a segment of three or more characters, or two segments ("/a/b"): not "/mo" or "/s"
        return bool(re.search(r"(?<![\w.$/])/(?:[\w.-]{3,}|[\w.-]+/[\w.-]+)", text_))

    def lone_row(row):
        joined = " ".join(row)
        if aspect == "storage":
            return real_path(joined)
        return aspect == "memory" and bool(words_of(joined) & memory_words)

    def nearest_heading(before):
        # The nearest heading above, ignoring "# comment" lines inside code fences.
        found, mark = "", None
        for line in lines[:before]:
            fence = re.match(r"\s*(`{3,}|~{3,})", line)
            if fence:
                mark = None if mark and fence.group(1)[0] == mark[0] else (mark or fence.group(1))
            elif mark is None and line.lstrip().startswith("#"):
                found = line
        return found

    prose_lines, tables, index = [], [], 0
    while index < len(lines):
        if lines[index].count("|") < 2:
            prose_lines.append(lines[index])
            index += 1
            continue
        end = index
        while end < len(lines) and lines[end].count("|") >= 2:
            end += 1
        tables.append(([cells_of(line) for line in lines[index:end]], nearest_heading(index)))
        index = end

    segments = [(("\n".join(prose_lines)), False, False)]
    for rows, caption in tables:
        header = rows[0] if len(rows) > 1 and is_divider(rows[1]) else None
        body = rows[2:] if header is not None else [row for row in rows if not is_divider(row)]
        # A cell under a header that names a unit carries that unit.
        for column, title in enumerate(header or []):
            if names_a_unit(title):
                for row in body:
                    if column < len(row) and re.search(number + r"(?![\w-]|\.\d)", row[column]):
                        return True
        # The facet context of a cell: its own column header and the nearest heading. A header-less
        # row quoted alone also counts when it holds a real filesystem path (or a memory word).
        for row in body:
            for column, cell in enumerate(row):
                title = header[column] if header is not None and column < len(header) else ""
                context = column_context(title, caption)
                label = row[0] if len(row) > 1 else ""
                if label:
                    # The row's label cell is context for every cell of the row: it cancels the other
                    # resource ("Mem:" for storage, a "/dev/..." or path row for memory) and can give context.
                    if (excluded(label) and not words_of(label) & own_nouns) or (
                        aspect == "memory" and real_path(label)
                    ):
                        context = False
                    elif not context and header is not None:  # a header-less row needs a real path (lone_row)
                        context = names_size(label) or (aspect == "storage" and real_path(label))
                segments.append((cell, context, header is None and (context or lone_row(row))))
    # The prose is one text; each table cell is its own unit, so a neighbouring
    # cell never supplies this cell's number and the guard runs inside the cell.
    return any(quantity_in(segment, table_sized, cell) for segment, table_sized, cell in segments if segment.strip())


def _read_only_command(command: str) -> bool:
    # A safe shell prefix cannot establish that the complete operation is read-only.
    if re.search(r"[;&|<>`\n]|\$\(", command):
        return False
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    if not args:
        return False
    if args[0] == "curl":
        for index, arg in enumerate(args[1:], 1):
            if arg.startswith(
                ("--data", "--json", "--form", "--upload-file", "--output", "-d", "-F", "-T", "-o", "-O")
            ):
                return False
            if arg in {"-X", "--request"}:
                if index + 1 >= len(args) or args[index + 1].upper() not in {"GET", "HEAD", "OPTIONS"}:
                    return False
            elif arg.startswith("-X") and arg[2:].upper() not in {"GET", "HEAD", "OPTIONS"}:
                return False
            elif arg.startswith("--request=") and arg.split("=", 1)[1].upper() not in {"GET", "HEAD", "OPTIONS"}:
                return False
        return len(args) > 1
    return (
        len(args) > 1
        and args[0] == "ls"
        or len(args) > 1
        and args[0] == "docker"
        and args[1] in {"inspect", "ps"}
        or len(args) > 1
        and args[0] == "systemctl"
        and args[1] in {"show", "status", "is-active"}
        or len(args) > 1
        and args[0] == "git"
        and args[1] in {"show", "log", "rev-parse"}
    )


def _matching_aspects(text: str, question: str) -> set[str]:
    material = re.sub(r"\[source:[^\]]+\]|\[(?:Source|source)\]\([^\)]+\)", "", text)
    statement = re.sub(r"https?://[^\s\]>)]+", "", material)
    body = _terms(statement)
    if re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", statement):
        body.add("address")
    if re.search(r":\d{2,5}\b", statement):
        body.add("port")
    if body & {"address", "port", "url"}:
        body.add("endpoint")
    if re.search(r"^\W*image(?:\s+tag)?\s*:", statement, re.I) and re.search(r"`[^`]+`", statement):
        body.add("version")
    body.discard("path")
    body.discard("container")
    body.discard("command")
    if re.search(r"(?<![\w:/])/(?:[\w.-]+/)*[\w.-]+", statement):
        if not re.search(r"\bcompose\s+(?:director(?:y|ies)|paths?|files?)\b", question, re.I) or re.search(
            r"\bcompose\b", statement, re.I
        ):
            if not re.search(r"\bconfig(?:uration)?\s+paths?\b", question, re.I) or re.search(
                r"\b(?:config(?:uration)?|compose)\b", statement, re.I
            ):
                body.add("path")
    if re.search(r"\b(?:container\s+name|service)\s*[:|]", statement, re.I) and re.search(r"`[\w.-]+`", statement):
        body.add("container")
    if any(_read_only_command(command) for command in re.findall(r"`([^`]+)`", material)):
        body.add("command")
    if re.search(
        r"\b(?:recommended|recommendation|suitable|prefer|preferred|balance|tradeoffs?|trade-offs?|selection|compared|versus)\b",
        statement,
        re.I,
    ):
        body.add("selection")
    if re.search(r"\b(?:cache|cached|caching)\b", statement, re.I):
        body.add("caching")
    if re.search(
        r"\b(?:scheduling|scheduled|priority|queues?|queueing|processing)\b|\bservice[ _-]tier\b", statement, re.I
    ):
        body.add("scheduling")
    if re.search(r"\b(?:faster|slower)\b|\bresponse\s+times?\b", statement, re.I):
        body.add("latency")
    matches = _question_aspects(question) & body
    # A document's topic list does not itself explain the requested mechanism.
    # Keep ordinary factual wording open; source conditions still travel with it.
    if re.search(r"\bhow\s+(?:can|do|does|should|to)\b", question, re.I) and re.search(
        r"\b(?:this|the)\s+(?:page|document|guide|section)\s+(?:covers|describes|discusses|lists|introduces)\b",
        statement,
        re.I,
    ):
        matches.clear()
    if "version" in matches and re.search(
        r"\b(?:what|which|current|live|runtime)\b.*\b(?:versions?|release|revision|commit)\b", question, re.I
    ):
        if not re.search(
            r"\bv\d+(?:\.\d+)*\b|\b\d+(?:\.\d+)+(?:[-+][\w.-]+)?\b|\b[0-9a-f]{7,40}\b|\bversion\s+\d+\b",
            statement,
            re.I,
        ):
            matches.discard("version")
    if "endpoint" in matches and re.search(r"\b(?:what|which|where)\b", question, re.I):
        if not re.search(
            r"https?://[^\s\]>)]+|\b(?:\d{1,3}\.){3}\d{1,3}\b|:\d{2,5}\b|\bport\s*[|:=]?\s*\d{2,5}\b", material, re.I
        ):
            matches.discard("endpoint")
    if _numeric_question(question):
        matches = {aspect for aspect in matches if aspect not in _MEASURE_ASPECTS or _has_quantity(statement, aspect)}
    return matches


def _matches_requested_aspects(text: str, question: str) -> bool:
    """A compatible source must also describe the requested fact, not only its entity."""
    statement = re.sub(r"https?://[^\s\]>)]+", "", text)
    aspects = _question_aspects(question)
    # File names, citation paths and page labels are discovery handles, not
    # model identities or statements about serving/fallback behavior.
    semantic = re.sub(r"(?<!\w)(?:/?[\w.-]+/)+[\w.-]+", "", statement)
    body = _terms(semantic)
    model_requested = bool(re.search(r"\b(?:which|what)\s+(?:[\w.-]+\s+){0,2}models?\b", question, re.I))
    if model_requested and not aspects & {"parameter", "context", "prompt", "quota", "address", "port", "endpoint"}:
        aspects.add("model")
        # An explicit model declaration supplies its identifier even when the
        # name is lowercase. Predicate/status words do not name a model.
        descriptors = {
            "a",
            "an",
            "the",
            "model",
            "models",
            "llm",
            "available",
            "unavailable",
            "active",
            "inactive",
            "running",
            "local",
            "remote",
            "configured",
            "unset",
            "unknown",
            "enabled",
            "disabled",
            "online",
            "offline",
            "ready",
            "optional",
            "required",
            "supported",
            "unsupported",
            "external",
            "internal",
            "automatic",
            "not",
            "only",
            "being",
            "currently",
            "used",
            "served",
        }
        declarations = re.findall(
            r"\bmodels?\s*(?:is|are|declared:|observed:|:|=|\|)\s*(?:the\s+)?[`*]*([a-z][\w.-]*)", semantic, re.I
        )
        identity = any(value.rstrip(".-").lower() not in descriptors for value in declarations)
        identity = identity or bool(re.search(r"\b[a-z][\w.]*\d[\w.]*-[a-z0-9.+-]+\b", semantic, re.I))
        serving_names = re.findall(r"\b(?:local|served|serving|pinned)\s+([A-Z][\w.-]*)", semantic)
        identity = identity or any(value.rstrip(".-").lower() not in descriptors for value in serving_names)
        constraint = bool(
            re.search(r"\b(?:no|never|without|only|not)\b.*\b(?:paid|subscription|fallback)\b", semantic, re.I)
        )
        body.discard("model")  # Mentioning the category does not identify its value.
        if identity or constraint:
            body.add("model")
    if re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", statement):
        body.add("address")
    if re.search(r":\d{2,5}\b", statement):
        body.add("port")
    if "parameter" in aspects:
        return "parameter" in body
    if "model" in aspects:
        return "model" in body
    return not aspects or bool(_matching_aspects(statement, question))


# Shared lexical intent vocabulary: normalize concepts across questions,
# table labels and prose without hardcoding products or individual pages.
_EQUIVALENTS = {
    "ip": "address",
    "host": "host",
    "hosts": "host",
    "hostname": "host",
    "curated": "curation",
    "curate": "curation",
    "curator": "curation",
    "curating": "curation",
    "review": "approval",
    "approve": "approval",
    "approved": "approval",
    "publication": "publish",
    "publishing": "publish",
    "published": "publish",
    "prompting": "prompt",
    "prompts": "prompt",
    "prefixes": "prefix",
    "parameters": "parameter",
    "params": "parameter",
    "models": "model",
    "versions": "version",
    "release": "version",
    "releases": "version",
    "revision": "version",
    "commit": "version",
    "ports": "port",
    "imported": "import",
    "imports": "import",
    "connections": "connection",
    "disk": "storage",
    "disks": "storage",
    "outputs": "output",
}
_GENERIC_SUBJECT = {"model", "models", "plan", "my", "api", "docs", "cloud", "product"}
_QUERY_FILLER = {
    "is",
    "at",
    "was",
    "were",
    "has",
    "had",
    "will",
    "would",
    "could",
    "it",
    "do",
    "you",
    "me",
    "our",
    "their",
    "how",
    "can",
    "are",
    "all",
    "and",
    "for",
    "the",
    "its",
    "need",
    "needs",
    "should",
    "before",
    "handles",
    "policy",
    "recommend",
    "recommended",
    "running",
    "current",
    "currently",
    "latest",
    "live",
    "deployed",
    "now",
}


def _raw_terms(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _terms(text: str) -> set[str]:
    return {
        _EQUIVALENTS.get(term, term)
        for term in _raw_terms(text)
        if term not in _STOPWORDS and term not in _QUERY_FILLER and len(term) > 1
    }


_host_view_cache: dict = {}


def _host_markers(question: str) -> tuple[dict[str, frozenset], list[tuple[int, int]]]:
    """One ranking term per host group the question names, keyed by the group.

    The term stands for the host however it is spelled (name, alias, address or
    qualified name), so every spelling of a host question has the same terms. It
    carries the group's names, so a host whose name has a digit stays an anchor.
    """
    identities = known_identities()
    cached = _host_view_cache.get(question)
    if cached and cached[0] is identities:
        return cached[1]
    markers = {}
    spans = []
    for begin, end, keys in host_spans(question):
        if len(keys) > 1:
            names = sorted(key for key in keys if re.fullmatch(r"[a-z0-9]*[a-z][a-z0-9]*", key))
            markers["host:" + "+".join(names)] = keys
            spans.append((begin, end))
    if len(_host_view_cache) > 256:
        _host_view_cache.clear()
    _host_view_cache[question] = (identities, (markers, spans))
    return markers, spans


def _query_terms(question: str) -> set[str]:
    """The question's terms, with each named host as its group's one term."""
    markers, spans = _host_markers(question)
    if not markers:
        return _terms(question)
    kept, position = [], 0
    for begin, end in sorted(spans):
        if begin >= position:
            kept.append(question[position:begin])
        position = max(position, end)
    kept.append(question[position:])
    return _terms(" ".join(kept)) | set(markers)


def _host_term_words(keys: frozenset) -> int:
    """How many words the host's shortest written name has ("Dell R640" two, svc1
    one): its one ranking term weighs what those words weighed, in every spelling."""
    counts = [
        len(_raw_terms(host)) for host in known_identities().hosts if re.sub(r"[^a-z0-9]", "", host.lower()) in keys
    ]
    return max(1, min(counts, default=1))


def _host_terms(text: str, question: str) -> set[str]:
    """The question's host terms whose host the text names, by any name in its group:
    a declared name, an alias, an address or a wikilink to the host's own page."""
    markers, _ = _host_markers(question)
    return {marker for marker, keys in markers.items() if mentions_group(text, keys)}


def _wants_routes(question: str, subject: str | None) -> bool:
    terms = _terms(question)
    # This source currently describes Token Plan routes, not arbitrary model
    # endpoints. A known subject alone is insufficient to dump routing config.
    routing = bool(terms & {"route", "routing", "endpoint", "url", "tier", "protocol"})
    account = (
        subject in {"alibaba-model-studio", "token-plan"}
        or "token plan" in question.lower()
        or "tier s" in " ".join(_raw_terms(question))
    )
    return routing and account


def _wants_live_state(question: str) -> bool:
    return any(re.search(r"\b" + re.escape(hint) + r"\b", question, re.I) for hint in LIVE_HINTS)


def _applicable_live_checks(question: str, subject: str | None) -> list[str]:
    """Select existing source capabilities, not whichever service is reachable.

    The probes inspect deployment symlinks, service units/ports, container
    status, collection status/count and endpoint DNS. They do not inspect
    model specifications, inference routing, curation policy or host IPs.
    """
    raw = " " + " ".join(_raw_terms(question + " " + (subject or ""))) + " "
    terms = _terms(question)
    brain_collection = " brain collection " in raw or " vo rag brain v1 " in raw
    if " vo rag " in raw:
        if terms & {"indexer", "sync", "worker"} and "api" not in terms:
            return []
        # No installed probe observes vo-rag embedding/model configuration.
        if terms & {
            "model",
            "embedding",
            "embeddings",
            "prompt",
            "context",
        } and not terms & {"port", "unit", "status", "health"}:
            return []
        names = []
        if not brain_collection and terms & {
            "version",
            "deployment",
            "deployed",
            "revision",
            "commit",
        }:
            names.append("vo-rag-release")
        if (not brain_collection or terms & {"api", "port", "unit", "host"}) and terms & {
            "port",
            "api",
            "unit",
            "status",
            "health",
            "host",
            "endpoint",
            "url",
        }:
            names.append("vo-rag-api")
        if terms & {"collection", "qdrant", "points", "index", "indexed"}:
            if (brain_collection or "brain" in terms) and terms & {
                "points",
                "point",
                "count",
                "status",
                "health",
            }:
                names.append("vo-rag-brain-collection")
            elif not names:
                return []
        return names or ["vo-rag-release", "vo-rag-api"]
    if " litellm " in raw and not terms & {"model", "quota", "prompt", "context"}:
        return ["litellm-container"]
    if " token plan " in raw and terms & {
        "dns",
        "endpoint",
        "url",
        "reachability",
        "reachable",
    }:
        return ["token-plan-endpoint"]
    if brain_collection and terms & {
        "collection",
        "points",
        "status",
        "health",
    }:
        return ["vo-rag-brain-collection"]
    return []


_RECORDED_DATE_PATTERN = (
    r"\b20\d{2}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})|[ \t]+\d{2}:\d{2}(?::\d{2})?[ \t]+UTC)?"
)


def _historical_context(page: BrainPage, heading: str, text: str) -> tuple[bool, str]:
    context = f"{page.path} {page.title} {heading}".lower()
    historical = bool(
        re.search(
            r"\b(historic(?:al)?|archive|superseded|proposal|proposed|investigation|initial|baseline)\b",
            context,
        )
    )
    historical = historical or bool(re.search(r"\b(historical|formerly|previously|superseded)\b", text, re.I))
    historical = (
        historical
        or page.path.startswith("inbox/")
        or bool(
            re.search(
                r"acceptance|adoption-plan|implementation-plan|design draft",
                f"{page.path} {page.title}",
                re.I,
            )
        )
    )
    dates = [
        re.sub(r"\s+UTC$", "Z", raw).replace(" ", "T", 1)
        for raw in re.findall(_RECORDED_DATE_PATTERN, f"{heading} {text}")
    ]
    date = max(dates) if dates else str(page.meta.get("updated") or "date unknown")
    return historical, date


_LIST_ITEM = r"^\s*(?:[-*+]|\d+[.)])\s+"
#: Longest value a bare label is given from its items before it is cut ("…").
BARE_LABEL_CHARS = 1200


def _lead_in_label(text: str) -> str:
    """The bold label a statement opens with ("**Label:** value"), else ""."""
    match = re.match(r"^\s*(?:(?:[-*+]|\d+[.)])\s+)?\*\*([^*]+?)(?::\*\*|\*\*\s*:)", text)
    return match[1].strip() if match else ""


def _label_only(text: str) -> bool:
    """True when a statement is nothing but its bold label: a heading, not a fact."""
    if not _lead_in_label(text):
        return False
    rest = re.sub(r"^\s*(?:(?:[-*+]|\d+[.)])\s+)?\*\*[^*]+?(?::\*\*|\*\*\s*:)", "", text, count=1)
    rest = re.sub(r"\[source:[^\]]+\]|\[(?:Source|source)\]\([^\)]+\)", "", rest)
    return not re.search(r"\w", rest)


def _bare_label_value(label: str, items: list[str]) -> tuple[str, list[str]]:
    """A bare label joined with its items within BARE_LABEL_CHARS (ellipsis
    included), and the item texts as delivered. A first item that does not fit
    is cut and marked "…", never dropped; later items that do not fit become " …"."""
    text, delivered = label, []
    for position, item in enumerate(items):
        sep = " " if not delivered or text.endswith((".", "!", "?", ";", ":")) else "; "
        # Room for this item, keeping room for " …" when more items follow it.
        room = BARE_LABEL_CHARS - len(text) - len(sep) - (2 if position < len(items) - 1 else 0)
        if len(item) <= room:
            text = text + sep + item
            delivered.append(item)
            continue
        if not delivered:
            cut = item[: max(BARE_LABEL_CHARS - len(text) - len(sep) - 1, 0)].rstrip()
            text = text + sep + cut + "…"
            delivered.append(cut)
        else:
            text += " …"
        break
    return text, delivered


def _lead_in_blocks(body: str) -> dict[int, tuple[tuple[str, bool], ...]]:
    """The bold lead-in labels each body line sits under, outermost first,
    each with whether its line is a bare label (nothing but the label).

    A lead-in's block is its paragraph, the list or table that follows it, a
    sentence introducing that list (ending in ":"), indented paragraphs inside
    the list, and labelled items nested under it. A heading, the next
    unnested lead-in, a bold list item without a label, or ordinary prose
    after a blank line or a code fence ends it. HTML comments are neutral.
    """
    blocks: dict[int, tuple[tuple[str, bool], ...]] = {}
    # (label, indent, is_list_item, bare) from outermost to innermost.
    stack: list[tuple[str, int, bool, bool]] = []
    after_break, previous = False, None
    for index, line in _noncode_lines(body, numbered=True):
        stripped = line.strip()
        if previous is not None and index > previous + 1:
            after_break = True  # a code fence sat between the two lines
        previous = index
        if not stripped:
            after_break = True
            continue
        if re.match(r"^#{1,6}\s", stripped):
            stack, after_break = [], False
            continue
        if stripped.startswith("<!--"):
            continue
        indent = len(line) - len(line.lstrip())
        item = bool(re.match(_LIST_ITEM, line))
        label = _lead_in_label(stripped)
        if item:
            # A list item sits under a lead-in paragraph or a less-indented
            # labelled item, never under a sibling's label.
            stack = [entry for entry in stack if not entry[2] or entry[1] < indent]
        if label:
            stack = (stack if item else []) + [(label, indent, item, _label_only(stripped))]
        elif item and re.match(_LIST_ITEM + r"\*\*", line):
            stack = []  # a bold item without a label starts its own block
        elif after_break and not item and not stripped.startswith("|") and not indent and not stripped.endswith(":"):
            stack = []
        after_break = False
        blocks[index] = tuple((entry[0], entry[3]) for entry in stack)
    return blocks


def _append_page_facts(answer: Answer, page: BrainPage, question: str = "") -> None:
    """Collect relevant statements with context; ranking occurs across pages."""
    if not question or page.path.startswith("sources/"):
        return
    query = _query_terms(question)
    # A page of a host the question names (its own, or a service recorded on it) is
    # a candidate even when its words share nothing with the question; ranking
    # gives it only a bounded credit (_rank_facts).
    page_ref = re.sub(r"\.md$", "", page.path.lower())
    host_page = page_ref in answer.host_pages
    # A page in a named host's profile (its own pages, its services' pages) is
    # about that host: it satisfies an "on <host>" platform constraint unless the
    # statement declares a hosting scope or names another host, which then
    # decide as usual.
    profile_hosts = [
        constraint
        for constraint in answer.scope_constraints
        if constraint["dimension"] == "platform" and page_ref in question_host_pages(constraint["value"])
    ]
    asked_hosts = set().union(*(question_hosts(constraint["value"]) for constraint in profile_hosts))
    # The page of a service the question names, recorded on the host it names, is
    # about that service on that host: its statements carry the host's term.
    service_page = bool(profile_hosts) and page_ref in question_service_pages(question)
    identity = _terms(
        page.path
        + " "
        + page.title
        + " "
        + " ".join(str(t) for t in page.meta.get("tags", []) or [])
        + " "
        + str(page.meta.get("product", ""))
    )
    # One line per identity field: neighbouring tags are separate names, never one phrase.
    identity_text = "\n".join(
        [page.path, page.title, *(str(t) for t in page.meta.get("tags", []) or []), str(page.meta.get("product", ""))]
    )
    rows = fact_rows(page)
    contexts, context_headings = line_contexts(page.body, with_headings=True)
    statements: list[tuple] = []
    for row in rows:
        value = row.declared
        source = "declared"
        if row.observed not in ("", "—"):
            source += f" · observed: {row.observed}"
            if _wants_live_state(question):
                value = row.observed + f" (declared: {row.declared})"
        statements.append(
            (
                row.fact,
                value,
                context_headings[row.source_line] if row.source_line is not None else "Facts",
                source,
                contexts[row.source_line] if row.source_line is not None else "",
                (),
                None,
                None,
            )
        )
    headings: list[tuple[int, str]] = []
    lead_ins = _lead_in_blocks(page.body)
    body_lines = page.body.splitlines()
    # Bare label line -> (delivered value, its item lines).
    bare_labels: dict[int, tuple[str, set[int]]] = {}
    for index, line in _noncode_lines(page.body, numbered=True):
        stripped = line.strip()
        if re.search(r"resolve_sync\(|\bRun:\*?\*?|^\*\*Run|^uv run|^\$ |^>>>", stripped):
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)", stripped)
        if heading:
            depth = len(heading[1])
            headings = [(level, text) for level, text in headings if level < depth]
            headings.append((depth, heading[2]))
            continue
        if not stripped or stripped.startswith(("!", "<", "```", "|---", "| Fact |")):
            continue
        # Facts table already parsed with its observed/declared semantics.
        if rows and any(title.lower() == "facts" for _, title in headings):
            continue
        section = " / ".join(text for _, text in headings)
        # A bare label ("- **Canonical repos:**" over its items) states nothing
        # by itself. It is tested alone and with each single item it holds; the
        # first that is admitted delivers the label joined with those items
        # (nested under it, for a labelled item), which then are not repeated.
        if _label_only(stripped):
            own = lead_ins.get(index, ())
            indent = len(line) - len(line.lstrip()) if re.match(_LIST_ITEM, line) else -1
            items: list[tuple[int, str]] = []
            for following in sorted(i for i in lead_ins if i > index):
                text = body_lines[following]
                if (
                    lead_ins[following][: len(own)] != own
                    or not re.match(_LIST_ITEM, text)
                    or len(text) - len(text.lstrip()) <= indent
                ):
                    break
                items.append((following, re.sub(_LIST_ITEM, "", text).strip()))
            joined, delivered = _bare_label_value(stripped, [item for _, item in items])
            if not delivered:
                continue
            bare_labels[index] = (joined, {following for following, _ in items[: len(delivered)]})
            # Each item is tested as delivered: a cut item only on its kept text.
            for candidate in [stripped] + [stripped + " " + item for item in delivered]:
                statements.append((page.title, candidate, section, "page text", contexts[index], own, index, index))
            continue
        statements.append(
            (page.title, stripped, section, "page text", contexts[index], lead_ins.get(index, ()), index, None)
        )
    # A lead-in label names a statement's topic only on a page about what the
    # question asks about (its title or path shares a content word with it).
    page_names_subject = bool(
        _content_stems(
            page.title + " " + page.path.rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("-", " ").replace("_", " ")
        )
        & _content_stems(question)
    )
    delivered_labels: set[int] = set()
    absorbed_lines: set[int] = set()
    for name, value, heading, source, evidence_context, labels, line_index, bare in statements:
        if bare in delivered_labels or line_index in absorbed_lines:
            continue
        if (
            _wants_live_state(question)
            and _question_aspects(question) & {"version", "endpoint", "address", "port"}
            and not query
            & {
                "test",
                "tests",
                "testing",
                "scenario",
                "scenarios",
                "acceptance",
                "example",
                "examples",
                "hypothetical",
                "design",
            }
            and re.match(r"^\W*(?:given|suppose|imagine|hypothetical)\b", value, re.I)
        ):
            # A test's assumed release is not an observation of running state.
            # The scenario remains available for explicit design/test questions.
            continue
        statement = re.sub(r"\[source:[^\]]+\]|\[(?:Source|source)\]\([^\)]+\)", "", value)
        body_terms = _terms(statement if source == "page text" else name + " " + statement)
        if re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", value):
            body_terms.add("address")
        if re.search(r":\d{2,5}\b", value):
            body_terms.add("port")
        body_terms |= _host_terms(statement if source == "page text" else name + " " + statement, question)
        contextual = identity | _terms(heading) | _host_terms(identity_text + "\n" + heading, question)
        matched = query & body_terms
        subject_terms = set(_raw_terms(answer.subject or "")) - _GENERIC_SUBJECT
        available_identity = set(
            _raw_terms(
                page.path
                + " "
                + page.title
                + " "
                + heading
                + " "
                + name
                + " "
                + value
                + " "
                + str(page.meta.get("tags", ""))
                + " "
                + str(page.meta.get("product", ""))
                + " "
                + str(page.meta.get("aliases", ""))
            )
        )
        if subject_terms and not subject_terms <= available_identity:
            identity_contexts = [
                page.path,
                page.title,
                heading,
                name,
                value,
                str(page.meta.get("product", "")),
                str(page.meta.get("aliases", "")),
            ]
            if not matches_identity(answer.subject, identity_contexts):
                continue
        peripheral = _terms(heading) & {
            "pricing",
            "billing",
            "cost",
            "quota",
            "caching",
            "cache",
            "latency",
        }
        if peripheral and not peripheral & (query | _question_aspects(question)):
            continue
        relaxed = False
        if not matched and not (query & contextual and _matching_aspects(statement, question)):
            if not host_page:
                continue
            relaxed = True
        # Context cannot supply every requested aspect: a page about the host
        # is useful only when its statement says an address/version/etc.
        # A paragraph's bold lead-in label ("**Backing filesystem and WAL
        # headroom:** ...") names its topic the way a sub-heading would, for
        # the paragraph and the statements in its block (_lead_in_blocks).
        lead_in_heading = next(
            (
                candidate
                for candidate in (heading + " / " + label.replace(" / ", " ") for label, _ in labels)
                if page_names_subject and _heading_facets(candidate, query, set())
            ),
            "",
        )
        named_lead_in = bool(lead_in_heading)
        if not _matches_requested_aspects(
            statement if source == "page text" else name + ": " + statement, question
        ) and not (
            _heading_facets(heading, query, set())
            or named_lead_in
            or (
                # A statement under a "selection"/"approval" section answers that
                # categorical aspect even when its own words never say so, but
                # only on a page about what the question asks about.
                _heading_facets(heading, set(), _question_aspects(question)) and page_names_subject
            )
        ):
            continue
        semantic = statement if source == "page text" else name + ": " + statement
        if "version" in _matching_aspects(semantic, question) and not _version_owned_by_subject(
            page, answer.subject, name, statement, heading, source, question
        ):
            # A neighboring component name does not make this document's own
            # version the requested service's release.
            continue
        scope = source_scope(
            page.meta, title=page.title, heading=heading, path=page.path, urls=source_urls(value), statement=semantic
        )
        constraints = answer.scope_constraints
        host_anchor: set[str] = set()
        if (
            profile_hosts
            and not _declares_hosting(scope)
            and not _names_another_host(heading + "\n" + value, asked_hosts)
        ):
            constraints = [constraint for constraint in constraints if constraint not in profile_hosts]
            host_anchor = _terms(" ".join(constraint["value"] for constraint in profile_hosts)) | {
                marker for marker, keys in _host_markers(question)[0].items() if keys & asked_hosts
            }
            if service_page:
                body_terms |= host_anchor & query
        if not matches_scope(scope, constraints):
            continue
        historical, date = _historical_context(page, heading, value)
        historical = historical or _older_observation(answer, value, source)
        provenance = authority_record(page.path, page.meta, source_revision=page.commit)
        metadata_text = authority_text(provenance)
        if len(evidence_context) + len(metadata_text) > EVIDENCE_CHARS or len(value) > EVIDENCE_CHARS:
            gap = f"{page.ref}: full governing source context exceeds {EVIDENCE_CHARS}-character delivery limit; relevant fact withheld"
            if gap not in answer.unknowns:
                answer.unknowns.append(gap)
            continue
        if bare is not None:
            value, members = bare_labels[bare]
            delivered_labels.add(bare)
            absorbed_lines |= members
        answer.git_facts.append(
            {
                "fact": name,
                # Strip a list marker, never the first asterisk of a bold lead-in.
                "value": re.sub(r"^(?:-|\*(?!\*))\s*", "", value).strip("| "),
                "evidence_context": evidence_context,
                **provenance,
                "authority_metadata": metadata_text,
                "source": f"{page.ref} · {source}"
                + (f" · historical/proposed ({date})" if historical else f" · recorded {date}"),
                "_scope": scope,
                "_body": " ".join(sorted(body_terms)),
                "_context": " ".join(sorted(contextual)),
                "_heading": heading,
                "_lead_in_heading": lead_in_heading,
                "_title": page.title,
                "_historical": "yes" if historical else "no",
                "_date": date,
                "_host_page": "yes" if relaxed else "no",
                "_profile_page": "yes" if host_page else "no",
                "_host_anchor": " ".join(sorted(host_anchor)),
            }
        )


def _declares_hosting(scope: str) -> bool:
    """Whether a statement's scope names its own platform, service or offering."""
    explicit = json.loads(scope or "{}").get("explicit", {})
    return any(key in explicit for key in ("platform", "service", "offering"))


def _names_another_host(text: str, asked: set) -> bool:
    """Whether text names a known host outside the asked host's equivalence group:
    its name, an alias or address of its group, or a wikilink to one of its pages."""
    from doclayer import applicability

    identities = applicability.known_identities()
    hosts = {re.sub(r"[^a-z0-9]", "", str(host).lower()) for host in identities.hosts}
    others = (hosts | {key for group in identities.groups for key in group}) - asked
    other_pages = {ref for profile in identities.profiles if not profile.keys & asked for ref in profile.pages}
    # A DNS-qualified name (jammy.lan) names the host of its first label, as
    # settings.is_host compares short names with FQDNs.
    qualified = {
        re.sub(r"[^a-z0-9]", "", match[1].lower())
        for match in re.finditer(r"(?<![\w.-])([A-Za-z0-9][\w-]*)(?:\.[A-Za-z][\w-]*)+", text)
    }
    return bool(qualified & others) or mentions_host(text, others) or links_to_page(text, other_pages)


def _version_owned_by_subject(page, subject, name, statement, heading, source, question=""):
    if not subject:
        return True
    wanted = _raw_terms(subject)
    generic_owner = {
        "api",
        "installed",
        "deployed",
        "runtime",
        "client",
        "server",
        "application",
        "software",
        "package",
        "current",
        "observed",
        "declared",
        "the",
        "a",
        "an",
        "is",
        "its",
        "this",
        "our",
        "my",
        "canonical",
        "published",
        "latest",
        "live",
        "normal",
        "by",
        "and",
        "deployment",
        "observation",
        "check",
        "probe",
    }

    def mentions(text):
        terms = _raw_terms(text)
        return any(
            terms[index : index + len(wanted)] == wanted for index in range(len(terms) - len(wanted) + 1)
        ) or matches_identity(subject, text)

    # A host hint does not replace the explicitly requested component owners.
    # Only names in a version-target phrase qualify, never incidental neighbors.
    requested_owners = _requested_fact_owners(question)

    def requested_owner(text):
        actual = [term for term in _raw_terms(text) if term not in generic_owner]
        return any(actual == _raw_terms(owner) for owner in requested_owners if owner not in generic_owner)

    # An explicitly owned page, subsection or table label can supply the
    # subject of a compact "Version | v2" row.
    if source != "page text" and (mentions(name) or requested_owner(name)):
        return True
    if source != "page text":
        owner = re.split(r"\b(?:version|release|revision|commit)\b", name, maxsplit=1, flags=re.I)[0]
        if set(_raw_terms(owner)) - generic_owner:
            return False  # A different named component's version row.
    # An explicit clause owner is more specific than a shared page title.
    # Strip preceding dependency/usage predicates so "Worker uses Gateway
    # version v2" names Gateway, rather than treating Worker as the owner.
    explicit = []
    clauses = re.split(r";|\.(?=\s+(?:[A-Z*]|$))", statement)
    for clause in clauses:
        for match in re.finditer(r"([\w.-]+(?:\s+[\w.-]+){0,3})\s+(?:version|release|revision|commit)\b", clause, re.I):
            owner = re.split(
                r"\b(?:uses|using|runs|running|loads|bundles|with|supports|depends on)\b", match[1], flags=re.I
            )[-1]
            if set(_raw_terms(owner)) - generic_owner:
                explicit.append(mentions(owner) or requested_owner(owner))
    if explicit:
        return any(explicit)
    for clause in clauses:
        if mentions(clause) and _matching_aspects(clause, "What version?"):
            return True
    # The nearest named subsection owns an otherwise compact version claim.
    # Structural headings do not replace the enclosing component identity.
    structural = generic_owner | {
        "facts",
        "specification",
        "specifications",
        "overview",
        "configuration",
        "installation",
        "setup",
        "deployment",
        "release",
        "releases",
        "notes",
        "status",
        "observations",
        "version",
        "versions",
        "operations",
        "reference",
        "identity",
        "details",
        "build",
        "builds",
        "compatibility",
        "platform",
        "support",
    }
    for context in reversed(heading.split(" / ")):
        owner_words = {word for word in _raw_terms(context) if not re.fullmatch(r"v?\d+", word)} - structural
        if owner_words:
            return mentions(context) or requested_owner(context)
    aliases = page.meta.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    owner_context = " ".join([page.title, str(page.meta.get("product", "")), Path(page.path).stem, *map(str, aliases)])
    if mentions(owner_context):
        return True
    return False


def _older_observation(answer, value, source):
    if not _wants_live_state(answer.question):
        return False
    # Documentation dates do not by themselves make a policy/design obsolete.
    # Explicit stored observations are distinguishable from fresh live probes.
    if not re.search(r"\b(?:observed|observation|live|check|running as of)\b", value + " " + source, re.I):
        return False
    recorded = re.findall(_RECORDED_DATE_PATTERN, value)

    def timestamp(raw):
        try:
            normalized = re.sub(r"\s+UTC$", "+00:00", raw).replace(" ", "T", 1)
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo is not None else None
        except ValueError:
            return None

    fresh = [
        parsed for result in answer.live_results if result.ok and (parsed := timestamp(result.checked_at)) is not None
    ]
    if not recorded or not fresh:
        return False
    latest = max(fresh)
    # A day-only observation cannot be ordered against a probe on that same
    # day. Preserve explicit UTC wall times rather than silently dropping them.
    for raw in recorded:
        observed = timestamp(raw)
        if observed is not None:
            if observed >= latest:
                return False
        else:
            try:
                day = datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                return False
            if day >= latest.date():
                return False
    return True


#: Total evidence characters the expansion pass may add beyond the original selection.
EXPANSION_CHARS = 16_000

#: Categorical aspects a section heading can state on behalf of its statements.
_HEADING_ASPECTS = {"approval", "selection"}


def _stem(term: str) -> str:
    """Light suffix normalization so installation/installed/install and
    retention/retained/retain meet. Only used for heading matching."""
    for suffix, replacement in (
        ("ention", "ain"),
        ("ations", ""),
        ("ation", ""),
        ("ings", ""),
        ("ing", ""),
        ("ed", ""),
        ("s", ""),
    ):
        if term.endswith(suffix) and len(term) - len(suffix) + len(replacement) >= 4:
            return term[: -len(suffix)] + replacement
    return term


#: Function words, and generic vocabulary that names no section or page subject.
_FUNCTION_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "by",
    "do",
    "does",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "the",
    "this",
    "that",
    "to",
    "what",
    "where",
    "which",
    "with",
}
_HEADING_STOP = _FUNCTION_WORDS | {
    "access",
    "data",
    "details",
    "info",
    "information",
    "notes",
    "only",
    "overview",
    "read",
    "route",
    "routes",
    "source",
    "sources",
}


def _content_stems(text: str) -> set[str]:
    """Suffix-normalized terms without function or generic words."""
    return {_stem(term) for term in _terms(text) if term not in _HEADING_STOP and _stem(term) not in _HEADING_STOP}


def _heading_facets(heading: str, query: set[str], aspects: set[str], landed: bool = False) -> set[str]:
    """Facets a fact's own section heading states for the question.

    Only a nested section counts: the page title carries identity, not a topic.
    A heading names the question when it shares two or more content terms
    (suffix-normalized, function and generic words removed) with it, or states
    a categorical aspect (approval, selection); one shared word ("release",
    "settings") is too generic to pick a section.

    On a page the answer already landed on, the generic words count too ("Read-only
    access route" names a read-only access route question), and a heading also
    qualifies when every one of its terms appears in the question.
    """
    if " / " not in heading:
        return set()
    section = heading.rsplit(" / ", 1)[-1]
    terms = _terms(section)
    categorical = terms & aspects & _HEADING_ASPECTS
    if landed:
        wanted = {_stem(term) for term in query}
        terms = terms - _FUNCTION_WORDS
        shared = {term for term in terms if _stem(term) in wanted}
        if shared and (len(shared) >= 2 or shared == terms):
            return shared | categorical
        return categorical
    stems = _content_stems(" ".join(query))
    shared = {term for term in terms if term not in _HEADING_STOP and _stem(term) in stems}
    return shared | categorical if len(shared) >= 2 or categorical else set()


def _shares_subject(fact: dict[str, str], query: set[str]) -> bool:
    """The fact's page names something the question is about (title or path)."""
    path = fact["source"].split("@", 1)[0]
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("-", " ").replace("_", " ")
    return bool(_content_stems(fact.get("_title", "") + " " + stem) & _content_stems(" ".join(query)))


_HOST_PAGE_CREDIT = 0.7
#: Words of a "what does <host> serve/run/host?" question that name no subject.
_HOST_QUESTION_WORDS = {
    "serve",
    "serves",
    "serving",
    "served",
    "run",
    "runs",
    "running",
    "host",
    "hosts",
    "hosting",
    "hosted",
    "on",
    "service",
    "services",
    "use",
    "uses",
    "offer",
    "offers",
    "provide",
    "provides",
}


def _asks_only_about_host(question: str) -> bool:
    """True when, apart from the host it names, the question has no subject words."""
    return not (_terms(question_without_hosts(question)) - _HOST_QUESTION_WORDS)


def _rank_facts(facts: list[dict[str, str]], question: str, cap: int) -> list[dict[str, str]]:
    query = _query_terms(question)
    if not facts:
        return []
    # Rare subject terms matter more than shared generic vocabulary. Counts
    # use source pages, so long transcripts cannot overwhelm a short fact page.
    weights = {
        term: 1
        / (
            1
            + len(
                {
                    fact["source"].split("@", 1)[0]
                    for fact in facts
                    if term in set(fact.get("_body", "").split()) | set(fact.get("_context", "").split())
                }
            )
        )
        for term in query
    }
    for marker, keys in _host_markers(question)[0].items():
        if marker in weights:
            weights[marker] *= _host_term_words(keys)

    def score(fact: dict[str, str]) -> tuple[float, str]:
        body = set(fact.get("_body", "").split())
        context = set(fact.get("_context", "").split())
        relevance = sum(weights[term] * (2 if term in body else 1) for term in query if term in body | context)
        anchors = {term for term in query if any(char.isdigit() for char in term)}
        # A statement about the asked host (on its profile page) carries the host's
        # name as an anchor even when its own words do not repeat it.
        if anchors and not anchors <= (body | context | set(fact.get("_host_anchor", "").split())):
            relevance *= 0.2
        if "address" in query and re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", fact["value"]):
            relevance *= 2
        # Historical/proposed records remain searchable; they do not outrank
        # equally relevant current records solely through repeated vocabulary.
        if fact.get("_historical") == "yes" and not _terms(question) & {
            "history",
            "historical",
            "archive",
            "proposal",
            "investigation",
        }:
            relevance *= 0.35
        return relevance, fact.get("_date", "")

    # A page of a host the question names competes on a bounded credit: a share of
    # the best ordinary fact's relevance, never more than the best itself, so it
    # fills slots ordinary matches leave but cannot displace the leaders.
    ordinary = [fact for fact in facts if fact.get("_host_page") != "yes"]
    if len(ordinary) != len(facts):
        leader = max((score(fact)[0] for fact in ordinary), default=0.0)
        credit = _HOST_PAGE_CREDIT * leader
        plain_score = score

        def score(fact: dict[str, str]) -> tuple[float, str]:  # noqa: F811
            relevance, date = plain_score(fact)
            if fact.get("_host_page") == "yes":
                relevance = min(relevance + credit, 0.9 * leader) if leader else relevance
            return relevance, date

    ranked = sorted(facts, key=score, reverse=True)
    selected = []
    seen_values: set[str] = set()
    counts: dict[str, int] = {}
    # Reserve a sourced statement for each requested facet before the normal
    # diversity/score cutoff. A page may legitimately answer several quantities.
    required: list[dict[str, str]] = []
    remaining = _question_aspects(question)
    facets = {
        id(fact): (
            _matching_aspects(fact["fact"] + " " + fact["value"], question) | _matching_aspects(fact["value"], question)
        )
        for fact in ranked
    }
    for fact in ranked:
        # A "Selection" or "Approved ..." heading on a page about something
        # else does not answer this question's selection/approval facet.
        if _shares_subject(fact, query):
            facets[id(fact)] |= _heading_facets(fact.get("_heading", ""), set(), _question_aspects(question))
    owners = _requested_fact_owners(question)
    # A multi-component request needs each explicitly named owner's facts.
    # Credit only the selected statement's section, not shared page identity.
    owner_facets = set()
    for fact in ranked:
        heading = fact.get("_heading", "").split(" / ")[-1].strip().lower()
        if heading not in owners:
            continue
        for aspect in facets[id(fact)] & {"version", "path", "container"}:
            target = aspect + ":" + heading
            facets[id(fact)].add(target)
            owner_facets.add(target)
    if len(owners) > 1:
        remaining |= owner_facets
    if not _terms(question) & {"history", "historical", "archive", "proposal"}:
        current_facets = set().union(*(facets[id(fact)] for fact in ranked if fact.get("_historical") != "yes"))
        for fact in ranked:
            if fact.get("_historical") == "yes":
                facets[id(fact)] -= current_facets
    # Cover distinct facets together before filling with ordinary ranked facts.
    # A statement explaining both processing and latency beats a latency-only
    # neighbor for that reservation; ties keep the existing relevance order.
    while remaining and len(required) < cap:
        candidate = max(ranked, key=lambda fact: len(facets[id(fact)] & remaining))
        covered = facets[id(candidate)] & remaining
        if not covered:
            break
        required.append(candidate)
        remaining -= covered
    for fact in required + [fact for fact in ranked if fact not in required]:
        mandatory = fact in required
        if not mandatory and score(fact)[0] < score(ranked[0])[0] * 0.55:
            continue
        path = fact["source"].split("@", 1)[0]
        key = fact["value"].lower()
        if key in seen_values or (not mandatory and counts.get(path, 0) >= 2):
            continue
        seen_values.add(key)
        counts[path] = counts.get(path, 0) + 1
        selected.append(
            {key: value for key, value in fact.items() if not key.startswith("_") or key in {"_scope", "_historical"}}
        )
        if len(selected) >= cap:
            break
    # Statements beyond the two-per-page cap: the section that answers is often
    # not among a page's two best. A canonical statement is admitted when it
    # clears the ordinary relevance threshold or its section heading names the
    # question (two shared terms). The original selection above is never
    # displaced; expansion is ranked by relevance and shares one character
    # allowance across all pages, inside the renderer's own total budget.
    query_terms = _query_terms(question)
    question_aspects = _question_aspects(question)
    wants_history = bool(query_terms & {"history", "historical", "archive", "proposal"})
    limit = cap * (ANSWER_EVIDENCE_CHARS // EVIDENCE_CHARS)
    budget = {
        (fact["source"], fact.get("evidence_context", "")): len(fact.get("evidence_context", ""))
        + len(fact.get("authority_metadata", ""))
        for fact in selected
    }
    expansion_chars = 0
    landed = {fact["source"].split("@", 1)[0] for fact in selected}
    threshold = score(ranked[0])[0] * 0.55

    def expand(fact: dict[str, str]) -> bool:
        """Add one statement beyond the cap selection when the budgets allow it."""
        nonlocal expansion_chars
        slot = (fact["source"], fact.get("evidence_context", ""))
        # A new extract also costs its rendered heading line.
        cost = (
            0
            if slot in budget
            else (len(slot[1]) + len(fact.get("authority_metadata", "")) + len(fact["source"]) + len(fact["fact"]) + 64)
        )
        if (
            expansion_chars + cost > EXPANSION_CHARS
            or sum(budget.values()) + (cost and len(slot[1]) + len(fact.get("authority_metadata", "")))
            > ANSWER_EVIDENCE_CHARS
        ):
            return False
        expansion_chars += cost
        budget.setdefault(slot, len(slot[1]) + len(fact.get("authority_metadata", "")))
        seen_values.add(fact["value"].lower())
        selected.append(
            {
                name: value
                for name, value in fact.items()
                if not name.startswith("_") or name in {"_scope", "_historical"}
            }
        )
        return True

    for fact in ranked:
        if len(selected) >= limit:
            break
        path = fact["source"].split("@", 1)[0]
        key = fact["value"].lower()
        # Working notes (doclayer/, docs/) match a long question's vocabulary
        # everywhere; only canonical pages are expanded.
        if not path.startswith(("wiki/", "infra/")) or key in seen_values:
            continue
        if fact.get("_historical") == "yes" and not wants_history:
            continue
        # Eligible: a section the question names, or a facet match that also
        # clears the relevance threshold. Statements matching no facet keep
        # the two-per-page cap.
        relevant = bool(facets[id(fact)]) and score(fact)[0] >= threshold
        # On a page the selection landed on, a statement in a bold lead-in's
        # block also has that label as its innermost section (same rule).
        named = bool(
            _heading_facets(fact.get("_heading", ""), query_terms, question_aspects, path in landed)
            or (
                path in landed
                and _heading_facets(fact.get("_lead_in_heading", ""), query_terms, question_aspects, True)
            )
        )
        if not (named or relevant):
            continue
        # Outside the pages the cap-8 pass delivered, a heading alone does not
        # make an unrelated page relevant: the page must share a subject word
        # (title or path) with the question.
        if not relevant and path not in landed and not _shares_subject(fact, query_terms):
            continue
        expand(fact)
    # A host-only question ("what runs on <host>?") is answered by what the host's
    # profile pages record. Each such page the selection missed adds its best
    # statement on top of the selection, never in place of it, inside the budget.
    for fact in ranked:
        path = fact["source"].split("@", 1)[0]
        if len(selected) >= limit:
            break
        if (
            fact.get("_profile_page") != "yes"
            or path in {f["source"].split("@", 1)[0] for f in selected}
            or fact["value"].lower() in seen_values
            or (fact.get("_historical") == "yes" and not wants_history)
        ):
            continue
        expand(fact)
    return selected


def git_page_scan(repo: BrainRepo, question: str, skip: set[str] | None = None, cap: int = 3) -> list[str]:
    """Pages whose text contains the question's specific tokens, best first.

    A token is "specific" when it is rare in the vault (it names something:
    a host, a model id), so hits are weighted by inverse document frequency —
    a page containing the rare token outranks pages that merely say "host".
    """
    tokens = [w for w in dict.fromkeys(re.findall(r"[a-z0-9][a-z0-9.-]{2,}", question.lower())) if w not in _STOPWORDS]
    if not tokens:
        return []
    texts: dict[str, str] = {}
    for path in repo.markdown_paths():
        if path == "log.md" or path.rsplit("/", 1)[-1] == "log.md":
            continue  # the librarian log is not a fact source
        if skip and path in skip:
            continue
        try:
            texts[path] = repo.read_text(path).lower()
        except OSError:
            continue
    idf = {token: 1.0 / (1 + sum(1 for text in texts.values() if token in text)) for token in tokens}
    scored: list[tuple[float, int, str]] = []
    order = 0
    for path, text in texts.items():
        score = sum(weight for token, weight in idf.items() if token in text)
        if score:
            scored.append((score, order, path))
            order += 1
    scored.sort(reverse=True)
    return [path for _, _, path in scored[:cap]]


def resolve_sync(question: str, *, product: str | None = None, live: bool | None = None) -> Answer:
    return asyncio.run(resolve(question, product=product, live=live))
