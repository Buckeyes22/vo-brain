"""Distinguish public product guidance from facts about a private installation.

A public product name does not make a question about its local use public.
An unknown subject is not permission to search the web for private facts.
"""

import re

_PRIVATE = re.compile(
    r"/(?:home|root|mnt|opt|etc|data|srv|var|usr|run|tmp)/|"
    r"\b(?:private|local|our|my|this|current)\s+(?:repo(?:sitory)?|project|codebase|"
    r"workstation|desktop|host|machine|server|installation|deployment|configuration|config|setup|account)\b|"
    r"\b(?:installed|deployed|hosted|running)\s+(?:on|in|at|here)\b|"
    r"\b(?:where|which|what)\s+(?:is|are|version|versions|host|hosts|branch|branches)\b[^?\n]*\b(?:installed|deployed|hosted|checkout|configuration paths|production location)\b|"
    r"\bhomelab\b|"
    r"\b(?:brain|doclayer|vo[- ]rag)\b|"
    r"\b(?:my|our)\s+(?:[a-z]+\s+){0,3}(?:report|reports|document|documents|files)\b|"
    r"\b(?:our|my)\s+(?:deployment|configuration|config|version|release|runtime|files|settings)\b",
    re.I,
)


#: An infrastructure need is documentation work (not an availability incident) when it
#: asks for documented, installed, deployed or configured facts. Word families, not
#: single spellings: "documented" qualifies as much as "documentation" (gap 3233cf07),
#: "install" as much as "installation".
_INFRASTRUCTURE_DOCUMENTATION = re.compile(
    r"\b(?:document(?:ed|ation|s)?|docs|guides?|references?|"
    r"install(?:ed|ation|s)?|deploy(?:ed|ment|ments|s)?|hosted|access routes|"
    r"configur(?:ed|ation|ations)|config|storage|filesystems?|ssh|corpus audit)\b",
    re.I,
)


def infrastructure_documentation_need(question: str) -> bool:
    """True when an infrastructure-class question asks for documentation, not an incident."""
    return bool(_INFRASTRUCTURE_DOCUMENTATION.search(question or ""))


#: Host-name alternation per recognized host set (the set is cached upstream).
_HOST_PATTERNS: dict[frozenset, re.Pattern[str] | None] = {}


def _host_pattern(hosts) -> re.Pattern[str] | None:
    """One word-bounded alternation over the recognized hosts; hyphens and spaces interchange."""
    key = frozenset(str(h) for h in hosts if str(h).strip())
    if key not in _HOST_PATTERNS:
        if len(_HOST_PATTERNS) > 32:
            _HOST_PATTERNS.clear()
        names = sorted(key, key=len, reverse=True)
        parts = [r"[- ]?".join(re.escape(w) for w in re.split(r"[- ]+", n.strip())) for n in names]
        _HOST_PATTERNS[key] = re.compile(r"\b(?:" + "|".join(parts) + r")\b", re.IGNORECASE) if parts else None
    return _HOST_PATTERNS[key]


def _names_private(text: str) -> bool:
    """Generic private markers, recognized hosts, or the operator's local terms."""
    if _PRIVATE.search(text):
        return True
    from doclayer import applicability
    from doclayer.local_profiles import ProfileError, load_profiles
    from doclayer.settings import settings

    try:
        hosts = applicability.known_identities().hosts
    except Exception:  # noqa: BLE001 - recognition degrades to the generic markers
        hosts = frozenset()
    pattern = _host_pattern(hosts)
    if pattern is not None and pattern.search(text):
        return True
    try:
        terms = load_profiles(settings.local_profiles_path).local_terms
    except (ProfileError, OSError):
        terms = None
    return bool(terms is not None and terms.search(text))


def private_question(question: str, *, subject=None, entry=None) -> bool:
    if _names_private(question):
        return True
    if entry is not None:
        if entry.kind in ("host", "host-inventory", "account", "install"):
            return True
        if entry.local_first and not re.search(
            r"\b(?:official|public)(?: [a-z0-9_-]+){0,3} (?:documentation|docs)\b|"
            r"\b(?:documentation|docs) for\b",
            question,
            re.I,
        ):
            return True
    return bool(subject and _names_private(str(subject)))


def public_subject(gap, catalog=None) -> str:
    """Unknown capture identities need a named request or known public entry."""
    canonical = gap.get("canonical_subject")
    identity = str(gap.get("identity") or "")
    subject = str(canonical or identity).removeprefix("unknown:")
    question = str(gap.get("representative_question") or "")
    entry = catalog.subjects.get(subject) if catalog else None
    if private_question(question, subject=subject, entry=entry):
        return ""
    if canonical or (subject and not identity.startswith("unknown:")):
        return subject
    if entry is not None:
        return subject
    from doclayer.routing import uncovered_candidate

    candidate = uncovered_candidate(question) or ""
    # An incomplete noun phrase ending in a function word is not a named
    # product. This validates extraction syntax, not a retained ledger label.
    if re.search(r"\b(?:of|for|about|with|any|some)\s*$", candidate, re.I):
        return ""
    return candidate


def public_discovery_allowed(gap, catalog=None) -> bool:
    subject = public_subject(gap, catalog)
    question = str(gap.get("representative_question") or "")
    entry = catalog.subjects.get(subject) if catalog else None
    if gap.get("need_class") in ("needs_local_evidence", "ambiguous"):
        return False
    if private_question(question, subject=subject, entry=entry):
        return False
    # An attached public documentation seed can identify an otherwise unknown
    # public topic. It still cannot answer a private question.
    attached = any(s.get("url") and s.get("authority") == "official" for s in gap.get("source_candidates", []))
    if subject in ("", "unidentified"):
        # A stale discovered URL does not identify what an ambiguous private
        # question refers to. Require an explicit public documentation request.
        return attached and bool(
            re.search(
                r"\b(?:official|public)(?: [a-z0-9_-]+){0,3} (?:documentation|docs)\b",
                question,
                re.I,
            )
        )
    return True
