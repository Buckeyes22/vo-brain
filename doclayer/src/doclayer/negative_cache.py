"""Negative-result cache: observed unsuccessful lookups, never absence facts.

Only an observed unsuccessful source/facet attempt is cached — never a
blanket "product has no docs". Entries bind to the canonical subject, the
normalized question/facets, requested edition/version/region, the route's
source selection, and the evidence generation (reference HEAD + index
acknowledgement + policy version), so a Personal Edition miss never suppresses
a Team Edition question and a public-document miss never suppresses a local
installation question.

Outages, index lag, ambiguous applicability and withheld evidence are never
cached: they are not observations about coverage. Suppression reads as
"recent lookup did not answer this request" with the date and the sources
that were checked, never as a fact about the product. Explicit force and a
required live-state check always bypass it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_TTL_S = 6 * 3600
MAX_ENTRIES = 512


def _now() -> datetime:
    return datetime.now(UTC)


def normalize_question(text: str) -> str:
    """Deterministic, conservative normalization: exact repeats share entries;
    uncertain paraphrases do not."""
    lowered = re.sub(r"\s+", " ", text.lower()).strip()
    lowered = re.sub(r"[-_]+", " ", lowered)
    lowered = re.sub(r"[?!.,;:\"']+$", "", lowered)
    return lowered


def question_key(question: str, subject: str | None) -> str:
    material = f"{subject or ''}\x00{normalize_question(question)}"
    return hashlib.sha256(material.encode()).hexdigest()[:24]


@dataclass
class NegativeHit:
    recorded_at: str
    checked_sources: list[str]
    outcome: str


class NegativeCache:
    """File-backed cache with time expiry and generation binding."""

    def __init__(self, path: Path, *, ttl_s: int = DEFAULT_TTL_S) -> None:
        self.path = Path(path)
        self.ttl_s = ttl_s

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _save(self, data: dict[str, Any]) -> None:
        entries = data.get("entries", {})
        # Bounded size: drop the oldest beyond the cap.
        if len(entries) > MAX_ENTRIES:
            ranked = sorted(entries.items(), key=lambda kv: kv[1].get("recorded_at", ""))
            for key, _ in ranked[: len(entries) - MAX_ENTRIES]:
                del entries[key]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(data))
            os.replace(tmp, self.path)
        except OSError:
            pass

    def check(
        self,
        question: str,
        subject: str | None,
        *,
        scopes: list[dict[str, str]] | None = None,
        sources: list[str] | None = None,
        generation: str = "",
    ) -> NegativeHit | None:
        data = self._load()
        entry = data.get("entries", {}).get(self._key(question, subject, scopes, sources, generation))
        if not isinstance(entry, dict):
            return None
        return self._entry_to_hit(entry)

    def check_any_sources(
        self, question: str, subject: str | None, *, generation: str | None = None
    ) -> NegativeHit | None:
        """Hook suppression requires the current attested evidence generation.
        Source subsets may differ; stale or missing generations never match."""
        if generation is None:
            return None  # unattested hook generations must not suppress refreshed evidence
        qkey = question_key(question, subject)
        data = self._load()
        for entry in data.get("entries", {}).values():
            if not isinstance(entry, dict) or entry.get("question_key") != qkey:
                continue
            if entry.get("generation") != generation:
                continue
            if subject and entry.get("subject") != subject:
                continue
            hit = self._entry_to_hit(entry)
            if hit:
                return hit
        return None

    def _entry_to_hit(self, entry: dict[str, Any]) -> NegativeHit | None:
        try:
            recorded = datetime.fromisoformat(entry["recorded_at"])
        except (KeyError, ValueError):
            return None
        if (_now() - recorded).total_seconds() > self.ttl_s:
            return None
        return NegativeHit(
            recorded_at=entry["recorded_at"],
            checked_sources=list(entry.get("checked_sources", [])),
            outcome=str(entry.get("outcome", "")),
        )

    def record(
        self,
        question: str,
        subject: str | None,
        *,
        scopes: list[dict[str, str]] | None = None,
        sources: list[str] | None = None,
        generation: str = "",
        outcome: str = "not_found_in_checked_sources",
    ) -> None:
        """Record one observed unsuccessful attempt. Callers must never pass
        outage/lag/withheld outcomes here — they are not coverage facts."""
        if outcome not in ("not_found_in_checked_sources", "partial"):
            return
        data = self._load()
        entries = data.setdefault("entries", {})
        entries[self._key(question, subject, scopes, sources, generation)] = {
            "recorded_at": _now().isoformat(timespec="seconds"),
            "question_key": question_key(question, subject),
            "question_normalized": normalize_question(question)[:200],
            "subject": subject,
            "scopes": scopes or [],
            "checked_sources": sources or [],
            "generation": generation,
            "outcome": outcome,
        }
        data["version"] = 1
        self._save(data)

    def invalidate_generation(self, generation: str) -> int:
        """Drop entries bound to one evidence generation (e.g. after index
        acknowledgement or a source refresh)."""
        data = self._load()
        entries = data.get("entries", {})
        stale = [k for k, v in entries.items() if v.get("generation") == generation]
        for key in stale:
            del entries[key]
        if stale:
            self._save(data)
        return len(stale)

    def invalidate_subject(self, subject: str | None, question: str | None = None) -> int:
        """Retire only misses for the refreshed need, never unrelated subjects."""
        data = self._load()
        entries = data.get("entries", {})
        key = question_key(question, subject) if question is not None else None
        stale = [
            k
            for k, v in entries.items()
            if isinstance(v, dict) and v.get("subject") == subject and (key is None or v.get("question_key") == key)
        ]
        for k in stale:
            del entries[k]
        if stale:
            self._save(data)
        return len(stale)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass

    def _key(
        self,
        question: str,
        subject: str | None,
        scopes: list[dict[str, str]] | None,
        sources: list[str] | None,
        generation: str,
    ) -> str:
        parts = [
            question_key(question, subject),
            subject or "",
            json.dumps(scopes or [], sort_keys=True),
            json.dumps(sorted(sources or [])),
            generation,
        ]
        return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:32]


def evidence_generation(head: str | None, index_commit: str | None, policy_version: int) -> str:
    """The evidence generation a cache entry binds to: committed reference
    HEAD, the acknowledged index commit, and the routing policy version."""
    return f"{head or 'no-head'}:{index_commit or 'no-index'}:v{policy_version}"
