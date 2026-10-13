"""Selective routing: one deterministic route decision per prompt.

The route result carries the intent, identified subjects, selected sources,
reason, coverage revision/status, and gap-capture eligibility. Classification
is deterministic local matching over the coverage catalog — no classification
model call, no network, no curator. Generic phrases and filename fragments do
not trigger lookups; multi-subject questions produce a union of justified
source routes; conflicting aliases stay ambiguous.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from doclayer.coverage import Catalog, SubjectEntry, load_catalog, ssh_host_aliases
from doclayer.question_scope import private_question

# Intent categories.
INTENT_LOCAL = "local_environment"
INTENT_PUBLIC = "public_docs"
INTENT_MIXED = "mixed_local_public"
INTENT_CODE = "code_task"
INTENT_EXPLICIT = "explicit_brain"
INTENT_AMBIGUOUS = "ambiguous"

# Source selectors the resolver maps onto actual source calls.
SRC_BRAIN_GIT = "brain_git"
SRC_BRAIN_INDEX = "brain_index"
SRC_DOC_COPIES = "doc_copies"
SRC_PUBLIC_DOCS = "public_docs"
SRC_LIVE_CHECKS = "live_checks"

MAX_PROMPT_CHARS = 20_000


def routing_text(prompt: str) -> str:
    """Route the request, not fenced/quoted material attached to it.

    Recognized pasted reports are context for inspection. Their host names,
    earlier questions and running-state words are not fresh lookup requests.
    Ordinary explicit requests outside the attachment remain routable.
    """
    text = re.sub(r"(?ms)^\s*(```|~~~).*?^\s*\1[^\n]*$", "", prompt)
    text = re.sub(r"(?m)^\s*>.*$", "", text)
    marker = re.search(
        r"(?im)^.*\b(?:finished with (?:this|the following) output|pasted (?:report|output)|report follows)\s*:",
        text,
    )
    if marker:
        text = text[: marker.start()] + " review completed implementation report"
    text = re.sub(r"https?://[^\s]+", "", text)
    return text[:MAX_PROMPT_CHARS]


@dataclass
class RouteResult:
    intent: str
    subjects: list[str] = field(default_factory=list)
    selected_sources: list[str] = field(default_factory=list)
    reason: str = ""
    coverage_revision: str | None = None
    coverage_status: str = "unknown"
    gap_capture_eligible: bool = False
    uncovered_subject: str | None = None
    suppression: str | None = None  # visible negative-cache status, if any

    @property
    def wants_lookup(self) -> bool:
        """Whether the route justifies consulting brain evidence now."""
        return self.intent in (
            INTENT_LOCAL,
            INTENT_PUBLIC,
            INTENT_MIXED,
            INTENT_EXPLICIT,
        ) and bool(self.selected_sources)


# --------------------------------------------------------------------------- #
# Deterministic signals
# --------------------------------------------------------------------------- #

_DOC_NEED = re.compile(
    r"\b(docs?|documentation|reference|guides?|tutorial|getting started|examples?"
    r"|how (do|does|to|can|should|would)( [a-z]+){0,3} (use|using|configure|set up|call|deploy|migrate|install|integrate)"
    r"|api usage|usage|install(ation|ing|ed)?|upgrade|migration|configure|error"
    r"|release notes|what'?s new|capabilities?|parameters?|quotas?|limits?|pricing|models? (are|does|can|is)|supported)\b",
    re.IGNORECASE,
)

_RUN_STATE = re.compile(
    r"\b(running|current|currently|deployed|deployed right now|live|latest|actual"
    r"|right now|status|health|reachable|up)\b",
    re.IGNORECASE,
)

_LOCAL_CONTEXT = re.compile(
    r"\b(my|our|installed|deployment|deployments?|fleet|here|this host|this machine"
    r"|account|accounts?|tier|subscription|configuration|config|homelab)\b",
    re.IGNORECASE,
)

_TASK = re.compile(
    r"^\s*(please\s+)?(update|fix|refactor|add|remove|delete|write|implement|change|edit|create|move|rename|run|restart|reload|stop|start|deploy|reboot|set up|clean|bump|revert)\b",
    re.IGNORECASE,
)

_CODE = re.compile(
    r"\b(argument|arguments?|function|method|class|variable|code|repo\b|repository|this file"
    r"|tests?|bug|traceback|exception|import error|type error|lint|compile|passed|returned?)\b",
    re.IGNORECASE,
)

_INSPECT = re.compile(
    r"\b(inspect|show me|check|look at|what('s| is) (on|in|running)|which (host|machine|service)|where (is|does|are)|tell me about)\b",
    re.IGNORECASE,
)

_QUESTION = re.compile(
    r"\?|^\s*(what|which|how|why|when|where|who|is|are|does|do|can|could|should|will|has|have)\b",
    re.IGNORECASE,
)

_FOLLOWUP = re.compile(
    r"\b(previous answer|your answer|earlier|above|you (just )?(said|mentioned)|explain (that|this|what you))\b",
    re.IGNORECASE,
)

#: "docs for X" style phrases after which an uncovered subject candidate follows.
_DOCS_FOR = re.compile(
    r"\b(docs?|documentation|references?|guides?|manual)\s+(for|on|of|about|regarding)\s+([a-z0-9][a-z0-9 .+_-]{0,48})",
    re.IGNORECASE,
)

_HOW_USE = re.compile(
    r"\bhow (do|to|can|should|would)\b.{0,32}?\buse (?:the )?([a-z0-9][a-z0-9 .+_-]{0,40})",
    re.IGNORECASE,
)

# Explicit product syntax also identifies public needs without "docs for".
_PRODUCT_FACT = re.compile(r"\b[Hh]ow does ([A-Z][A-Za-z0-9.+_-]{1,48})\s+(?:use|work|handle|support|configure)\b")
_PRODUCT_DOCS = re.compile(
    r"\b(?:official|public)\s+([a-z0-9][a-z0-9.+_-]{1,48})\s+(?:documentation|docs)\b|"
    r"\bwhat (?:does|do)\s+([a-z0-9][a-z0-9.+_-]{1,48})\s+(?:documentation|docs)\b",
    re.I,
)

_FOLLOW_CANDIDATE_STOP = re.compile(r"[.?!;:,]|$")


def _clean_candidate(raw: str) -> str | None:
    text = re.sub(r"\s+", " ", raw).strip()
    # Drop leading filler and trailing clause spillover.
    words = text.split(" ")
    filler = {"a", "an", "the", "my", "our", "this", "that", "some", "any"}
    while words and words[0].lower() in filler:
        words = words[1:]
    if not words:
        return None
    candidate = " ".join(words).lower().strip(" .,;:!?-")
    # Conservative normalized identity: hyphens/underscores are spaces.
    candidate = re.sub(r"[-_]+", " ", candidate).strip()
    if not candidate or len(candidate) < 2:
        return None
    return candidate


def uncovered_candidate(prompt: str) -> str | None:
    """The subject an explicit documentation request names, when the catalog
    cannot identify it. Deterministic extraction; never a guessed identity."""
    for pattern in (_DOCS_FOR, _HOW_USE):
        match = pattern.search(prompt[:MAX_PROMPT_CHARS])
        if match:
            tail = match.group(match.lastindex)
            cut = _FOLLOW_CANDIDATE_STOP.search(tail)
            candidate = _clean_candidate(tail[: cut.start()] if cut else tail)
            if candidate:
                return candidate
    for pattern in (_PRODUCT_FACT, _PRODUCT_DOCS):
        match = pattern.search(prompt[:MAX_PROMPT_CHARS])
        if match:
            candidate = _clean_candidate(next(g for g in match.groups() if g))
            if candidate and candidate not in {"this", "that", "it", "the", "our", "my"}:
                return candidate
    return None


def has_docs_need(text: str) -> bool:
    """Whether the text asks for documentation or product facts."""
    return bool(_DOC_NEED.search(text[:MAX_PROMPT_CHARS]))


def has_code_task(text: str) -> bool:
    """Whether the text is repository/code work rather than a docs need."""
    return bool(_CODE.search(text[:MAX_PROMPT_CHARS]))


def is_question(text: str) -> bool:
    return bool(_QUESTION.search(text[:MAX_PROMPT_CHARS]))


def has_local_context(text: str) -> bool:
    """Whether the text speaks of my/our installed account or deployment."""
    return bool(_LOCAL_CONTEXT.search(text[:MAX_PROMPT_CHARS]))


def covers_candidate(catalog: Catalog, candidate: str, hosts: list[str]) -> bool:
    normalized = re.sub(r"[-_]+", " ", candidate.lower()).strip()
    for slug, subject in catalog.subjects.items():
        for phrase in subject.match_phrases():
            if phrase == normalized or phrase in normalized or normalized in phrase:
                return True
    return any(h == normalized for h in hosts)


def _match_subjects(prompt: str, catalog: Catalog) -> tuple[dict[str, list[str]], list[str]]:
    """Complete-phrase subject matching. Returns ({subject: [phrases]}, conflicts)."""
    text = " " + re.sub(r"[-_]+", " ", prompt[:MAX_PROMPT_CHARS].lower()) + " "
    matched: dict[str, list[str]] = {}
    conflicts: list[str] = []
    phrase_owners: dict[str, list[str]] = {}
    for slug, subject in catalog.subjects.items():
        for phrase in subject.match_phrases():
            phrase_owners.setdefault(phrase, []).append(slug)
    for phrase, owners in sorted(phrase_owners.items(), key=lambda kv: (-len(kv[0]), kv[0])):
        if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text):
            if len(owners) > 1:
                conflicts.append(phrase)
                continue
            matched.setdefault(owners[0], []).append(phrase)
    return matched, conflicts


# --------------------------------------------------------------------------- #
# The route decision
# --------------------------------------------------------------------------- #


def classify(
    prompt: str,
    catalog: Catalog,
    host_aliases: list[str] | None = None,
    *,
    explicit: bool = False,
) -> RouteResult:
    """Classify one prompt deterministically. Never raises on odd input."""
    text = routing_text(prompt)
    result = RouteResult(
        intent=INTENT_AMBIGUOUS,
        coverage_revision=catalog.revision,
        coverage_status=catalog.status,
    )
    hosts = [
        h
        for h in (host_aliases or [])
        if re.search(
            rf"(?<!\w){re.escape(h)}(?!\w)",
            " " + re.sub(r"[-_]+", " ", text.lower()) + " ",
        )
    ]
    if catalog.status != "ok":
        result.reason = (
            f"coverage {catalog.status}: {catalog.reason or 'catalog unavailable'}; local investigation stays allowed"
        )
        if explicit:
            result.intent = INTENT_EXPLICIT
            result.selected_sources = [SRC_BRAIN_GIT, SRC_BRAIN_INDEX]
            if not (hosts or private_question(text)):
                result.selected_sources.append(SRC_PUBLIC_DOCS)
            result.reason = f"explicit request; {result.reason}"
        return result

    subjects, conflicts = _match_subjects(text, catalog)

    if conflicts:
        result.intent = INTENT_AMBIGUOUS
        result.reason = f"conflicting aliases match: {', '.join(sorted(conflicts))}"
        return result

    private = (
        bool(hosts)
        or private_question(text)
        or any(private_question(text, subject=slug, entry=catalog.subjects[slug]) for slug in subjects)
    )
    docs_need = bool(_DOC_NEED.search(text))
    run_state = bool(_RUN_STATE.search(text))
    inspect = bool(_INSPECT.search(text))
    task = bool(_TASK.match(text))
    task_only = task and not (run_state or inspect or _LOCAL_CONTEXT.search(text) and docs_need)
    code_only = bool(_CODE.search(text)) and not (docs_need or run_state)
    if (
        private
        and (explicit or not (task_only or code_only))
        and (explicit or docs_need or run_state or inspect or _QUESTION.search(text))
    ):
        result.intent = INTENT_EXPLICIT if explicit else INTENT_LOCAL
        result.subjects = sorted(subjects) + [h for h in hosts if h not in subjects]
        result.selected_sources = [SRC_BRAIN_GIT, SRC_BRAIN_INDEX]
        for slug in subjects:
            result.selected_sources.extend(f"{SRC_LIVE_CHECKS}:{check}" for check in catalog.subjects[slug].live_checks)
        result.reason = "private factual context; consult local declarations and evidence, not public documentation"
        return result

    if explicit:
        result.intent = INTENT_EXPLICIT
        result.subjects = sorted(subjects)
        result.selected_sources = _sources_for_subjects(catalog, subjects, hosts, text)
        result.reason = "explicit brain request" + (f" for {', '.join(result.subjects)}" if result.subjects else "")
        return result

    entries = [catalog.subjects[s] for s in subjects]

    # A clear documentation request naming an uncovered subject is the one
    # hook-capturable case.
    candidate = uncovered_candidate(text)
    known = bool(subjects or hosts)
    if candidate and not known:
        covered = covers_candidate(catalog, candidate, hosts)
        if not covered:
            result.intent = INTENT_PUBLIC
            result.selected_sources = [SRC_PUBLIC_DOCS]
            result.uncovered_subject = candidate
            result.gap_capture_eligible = True
            result.reason = (
                f"documentation request for uncovered subject '{candidate}'; "
                "external documentation applies, record a qualified coverage candidate"
            )
            return result

    if not subjects and not hosts:
        return _classify_no_subject(result, text)

    local_entries = [e for e in entries if e.kind in ("host", "host-inventory", "service", "account") or e.local_first]
    public_entries = [e for e in entries if e not in local_entries]
    question = bool(_QUESTION.search(text))
    run_state = bool(_RUN_STATE.search(text))
    docs_need = bool(_DOC_NEED.search(text))
    local_context = bool(_LOCAL_CONTEXT.search(text))
    task = bool(_TASK.match(text))
    inspect = bool(_INSPECT.search(text))
    followup = bool(_FOLLOWUP.search(text))

    if followup and not subjects:
        result.intent = INTENT_AMBIGUOUS
        result.reason = "follow-up about earlier evidence; reuse what was already consulted"
        return result

    if task and not (run_state or inspect or local_context and docs_need):
        result.intent = INTENT_CODE
        result.subjects = sorted(subjects)
        result.reason = "editing or task instruction; documentation evidence is not automatically needed"
        return result

    if local_entries and (question or run_state or inspect or docs_need or local_context):
        result.subjects = sorted(subjects) + [h for h in hosts if h not in subjects]
        if docs_need and question and not run_state and not local_context:
            # "How do I configure litellm retries?" — local facts and public
            # docs, separately attributed.
            result.intent = INTENT_MIXED
            result.selected_sources = _sources_for_subjects(catalog, subjects, hosts, text)
            if SRC_PUBLIC_DOCS not in result.selected_sources:
                result.selected_sources.append(SRC_PUBLIC_DOCS)
        else:
            result.intent = INTENT_LOCAL
            result.selected_sources = _sources_for_subjects(catalog, subjects, hosts, text)
        result.reason = _reason_for(local_entries, public_entries, hosts, run_state)
        return result

    if public_entries and (question or docs_need):
        result.intent = INTENT_MIXED if local_context else INTENT_PUBLIC
        result.subjects = sorted(subjects)
        result.selected_sources = _sources_for_subjects(catalog, subjects, hosts, text)
        result.reason = _reason_for(local_entries, public_entries, hosts, run_state)
        return result

    if (subjects or hosts) and (question or run_state or inspect or local_context) and not public_entries:
        # SSH aliases are known hosts even when the catalog carries no host
        # entry: a question about a known host is a local-environment question.
        result.intent = INTENT_LOCAL
        result.subjects = sorted(subjects) + [h for h in hosts if h not in subjects]
        result.selected_sources = _sources_for_subjects(catalog, subjects, hosts, text)
        result.reason = _reason_for(local_entries, public_entries, hosts, run_state)
        return result

    result.intent = INTENT_AMBIGUOUS
    result.subjects = sorted(subjects)
    result.reason = (
        "subject mentioned without a clear documentation need; no mandatory "
        "lookup — choose resolve when evidence would help, or record a gap"
    )
    return result


def _reason_for(
    local_entries: list[SubjectEntry],
    public_entries: list[SubjectEntry],
    hosts: list[str],
    run_state: bool,
) -> str:
    parts = []
    if local_entries or hosts:
        names = [e.title for e in local_entries] + hosts
        parts.append(f"known local subject {', '.join(sorted(set(names)))}")
        if run_state:
            parts.append("running-state question")
    if public_entries:
        parts.append(f"product documentation subject {', '.join(sorted(e.title for e in public_entries))}")
    return "; ".join(parts) or "matched"


def _sources_for_subjects(
    catalog: Catalog,
    subjects: dict[str, list[str]],
    hosts: list[str],
    text: str,
) -> list[str]:
    """Union of justified source routes for the matched subjects."""
    sources: list[str] = []
    entries = [catalog.subjects[s] for s in subjects]
    local_kinds = {"host", "host-inventory", "service", "account"}
    has_local = bool(hosts) or any(e.kind in local_kinds or e.local_first for e in entries)
    has_public = any(e.kind not in local_kinds for e in entries)
    public_signal = bool(_DOC_NEED.search(text) or _QUESTION.search(text))
    if subjects or hosts:
        sources.append(SRC_BRAIN_GIT)
    if has_local:
        sources.append(SRC_BRAIN_INDEX)
        for entry in entries:
            sources.extend(f"{SRC_LIVE_CHECKS}:{c}" for c in entry.live_checks)
    if has_public or (not subjects and not hosts and public_signal):
        sources.append(SRC_DOC_COPIES)
        sources.append(SRC_PUBLIC_DOCS)
    ordered = []
    for source in (
        SRC_BRAIN_GIT,
        SRC_DOC_COPIES,
        SRC_BRAIN_INDEX,
        SRC_PUBLIC_DOCS,
        SRC_LIVE_CHECKS,
    ):
        for item in sources:
            if item == source or item.startswith(f"{SRC_LIVE_CHECKS}:"):
                if item not in ordered:
                    ordered.append(item)
    return ordered


def _classify_no_subject(result: RouteResult, text: str) -> RouteResult:
    task = bool(_TASK.match(text))
    code = bool(_CODE.search(text))
    docs_need = bool(_DOC_NEED.search(text))
    question = bool(_QUESTION.search(text))
    local_context = bool(_LOCAL_CONTEXT.search(text))
    inspect = bool(_INSPECT.search(text))
    followup = bool(_FOLLOWUP.search(text))
    if followup:
        result.intent = INTENT_AMBIGUOUS
        result.reason = "follow-up about earlier evidence; reuse what was already consulted"
        return result
    if task or (code and not docs_need):
        result.intent = INTENT_CODE
        result.reason = "repository code or task work; no doclayer lookup"
        return result
    if inspect and local_context:
        result.intent = INTENT_LOCAL
        result.selected_sources = [SRC_BRAIN_GIT, SRC_BRAIN_INDEX]
        result.reason = "inspection of local deployment context; committed declarations apply"
        return result
    if question and local_context and not task and not code:
        # "How does this API fit our deployment?" — local facts and public
        # docs, separately attributed.
        result.intent = INTENT_MIXED
        result.selected_sources = [SRC_BRAIN_GIT, SRC_BRAIN_INDEX, SRC_PUBLIC_DOCS]
        result.reason = "question mixing local applicability with product documentation"
        return result
    if question or docs_need:
        result.intent = INTENT_AMBIGUOUS
        result.reason = (
            "no identifiable subject; no mandatory lookup — resolve when useful, or record the need as a gap"
        )
        return result
    result.intent = INTENT_AMBIGUOUS
    result.reason = "no documentation need identified"
    return result


# --------------------------------------------------------------------------- #
# Loader for hooks and callers
# --------------------------------------------------------------------------- #


def load_route_inputs(brain: Path, cache_dir: Path, ssh_config: Path | None = None) -> tuple[Catalog, list[str]]:
    """Catalog + host aliases, both cache-warm; safe on any input."""
    from doclayer.settings import settings

    policy = settings.routing_policy_path
    catalog = load_catalog(brain, cache_dir, policy)
    if ssh_config is None:
        ssh_config = Path("~/.ssh/config").expanduser()
    try:
        hosts = ssh_host_aliases(ssh_config, cache_dir)
    except OSError:
        hosts = []
    return catalog, hosts


def route_prompt(
    prompt: str,
    catalog: Catalog,
    hosts: list[str],
    *,
    explicit: bool = False,
) -> RouteResult:
    """Route one prompt; classify() wrapped so odd input degrades to no nudge."""
    try:
        return classify(prompt, catalog, hosts, explicit=explicit)
    except Exception:  # noqa: BLE001 — routing must never break a prompt
        return RouteResult(
            intent=INTENT_AMBIGUOUS,
            coverage_status=catalog.status if catalog else "unknown",
            reason="routing failed; no mandatory lookup",
        )
