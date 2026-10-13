"""Lossless, bounded evidence delivery; context is evidence, not a summary."""

from __future__ import annotations

import hashlib
import json
import re

# Presentation limits, independent of any model's context or embedding limit.
EVIDENCE_CHARS = 65_536
ANSWER_EVIDENCE_CHARS = 262_144
CONTEXT_SEGMENTS = 64


def line_contexts(body: str, *, with_headings: bool = False):
    """Full local section plus the active ancestors' preambles per source line.

    Occurrence and source order determine ancestry, never a heading-name lookup.
    Sibling sections are excluded. All text (including code) remains literal.
    """
    lines = body.splitlines(keepends=True)
    units: list[dict] = []
    start, level, stack = 0, 0, []
    label = ""
    marker = None
    length = 0
    for index, line in enumerate(lines):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if marker:
            if fence and fence[1][0] == marker and len(fence[1]) >= length and not fence[2].strip():
                marker = None
            continue
        if fence and not (fence[1][0] == "`" and "`" in fence[2]):
            marker, length = fence[1][0], len(fence[1])
            continue
        heading = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line.rstrip("\r\n"))
        if not heading:
            continue
        units.append({"start": start, "end": index, "level": level, "parents": list(stack), "heading": label})
        if index > start:
            # The section just closed can govern descendants, but not siblings.
            stack = [(depth, unit) for depth, unit in stack if depth < len(heading[1])]
            if level < len(heading[1]):
                stack.append((level, len(units) - 1))
        else:
            stack = [(depth, unit) for depth, unit in stack if depth < len(heading[1])]
        start, level, label = index, len(heading[1]), heading[2]
    units.append({"start": start, "end": len(lines), "level": level, "parents": list(stack), "heading": label})
    contexts = [""] * len(lines)
    headings = [""] * len(lines)
    for unit in units:
        indexes = [i for _, i in unit["parents"]]
        chunks = ["".join(lines[units[i]["start"] : units[i]["end"]]) for i in indexes]
        chunks.append("".join(lines[unit["start"] : unit["end"]]))
        context = "".join(chunk for chunk in chunks if chunk.strip())
        for index in range(unit["start"], unit["end"]):
            contexts[index] = context
            headings[index] = " / ".join(units[i]["heading"] for i in indexes if units[i]["heading"])
            headings[index] = " / ".join(filter(None, (headings[index], unit["heading"])))
    return (contexts, headings) if with_headings else contexts


def indexed_evidence(payload: dict, *, collection: str | None = None) -> tuple[str, list[dict], str | None]:
    """Accept full legacy passages or complete hydrated context, never trim.

    Hydration is the owning reader's job. A resolver cannot reconstruct an
    unresolved reference from a sibling hit or from its heading label.
    """
    content = payload.get("content", "")
    if not isinstance(content, str):
        return "", [], "indexed content is not text"
    segments = payload.get("governing_context", [])
    versioned = "governing_context_version" in payload or "governing_context_complete" in payload
    if versioned and "governing_context" not in payload:
        return "", [], "indexed governing context manifest is missing"
    if versioned and payload.get("governing_context_complete") is not True:
        return "", [], "indexed governing context is unresolved or incomplete"
    if (
        not isinstance(segments, list)
        or len(segments) > CONTEXT_SEGMENTS
        or any(not isinstance(s, dict) or not isinstance(s.get("text"), str) for s in segments)
    ):
        return "", [], "indexed governing context has unresolved references"
    if versioned and (
        type(payload.get("governing_context_version")) is not int or payload["governing_context_version"] != 1
    ):
        return "", [], "indexed governing context version is unsupported"
    if versioned:
        revision = payload.get("source_revision", payload.get("commit"))
        source_id = payload.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip():
            return "", [], "indexed local source identity is missing"
        if not isinstance(revision, str) or not revision.strip():
            return "", [], "indexed local source revision is missing"
        if "commit" in payload and "source_revision" in payload and payload["commit"] != payload["source_revision"]:
            return "", [], "indexed local source revisions contradict the captured snapshot"
        for segment in segments:
            required = ("source_id", "collection", "point_id", "source_revision", "content_hash")
            if any(not isinstance(segment.get(key), str) or not segment[key].strip() for key in required):
                return "", [], "indexed governing context provenance is incomplete"
            occurrence = segment.get("section_occurrence", segment.get("occurrence"))
            if type(occurrence) is not int or occurrence < 0:
                return "", [], "indexed governing context occurrence is missing"
            if (
                segment["source_revision"] != revision
                or collection is not None
                and segment["collection"] != collection
                or source_id is not None
                and segment["source_id"] != source_id
                or segment["content_hash"] != hashlib.sha256(segment["text"].encode()).hexdigest()
            ):
                return "", [], "indexed governing context provenance does not match the source snapshot"
    # Even when a service supplies local content separately, all actual context
    # segments must survive delivery. Preserve bytes; avoid repeating segments
    # already present verbatim in the complete content.
    missing = [s["text"] for s in segments if s["text"] and s["text"] not in content]
    full = "\n".join([*missing, content])
    if evidence_cost(full, segments) > EVIDENCE_CHARS:
        return "", [], f"indexed governing context exceeds {EVIDENCE_CHARS}-character delivery limit"
    return full, segments, None


def evidence_cost(content: str, segments: list[dict]) -> int:
    """Evidence bytes represented as text plus displayed citation metadata."""
    return len(content) + sum(
        sum(
            len(str(segment.get(key, "?")))
            for key in ("collection", "source_id", "point_id", "source_revision", "heading")
        )
        for segment in segments
    )


def noncode_lines(text: str, *, numbered: bool = False):
    """Yield prose with CommonMark fence marker/length semantics.

    Headings inside a fence cannot change the surrounding section context.
    Inline code remains prose, and a shorter or different marker is content,
    not a closing delimiter.
    """
    marker: str | None = None
    length = 0
    for index, line in enumerate(text.splitlines()):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker is not None:
            if fence and fence[1][0] == marker and len(fence[1]) >= length and not fence[2].strip():
                marker = None
            continue
        if fence and not (fence[1][0] == "`" and "`" in fence[2]):
            marker, length = fence[1][0], len(fence[1])
            continue
        yield (index, line) if numbered else line


def authority_record(
    path: str, metadata: dict, *, collection: str | None = None, source_revision: str | None = None
) -> dict:
    """Directory describes a record's role, never proves acceptance or truth.

    Explicit origin, lineage and lifecycle fields remain source declarations.
    Headings, query terms and indexed scores cannot promote their authority.
    """
    area = path.split("/", 1)[0] if "/" in path else "root"
    labels = {
        "inbox": "inbox proposal/work item; acceptance not established",
        "sources": "source reference; source statements require applicability",
        "wiki": "curated wiki record; current applicability requires verification",
        "infra": "canonical configuration record; running state requires observation",
    }
    record = {
        "path_area": area,
        "authority": labels.get(area, "committed brain record; authority not established by location"),
    }
    if collection is not None:
        record["collection"] = collection
    if source_revision is not None:
        record["source_revision"] = source_revision
    for field in ("kind", "source_origin", "source_lineage", "status", "updated", "url", "fetched_at"):
        if field in metadata:
            record[field] = metadata[field]
    return record


def authority_text(record: dict) -> str:
    """Lossless explicit metadata, rendered separately from factual evidence."""
    return "source authority metadata " + json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
