"""Deterministic publication checks for curator drafts.

The model check (evidence/applicability) is a separate bounded pass; this
module enforces what ordinary code must: page schema, fresh/preserved
identities, destination scope, link coherence, scope discipline on updates,
and operator-correction replay protection. Model-proposed paths are
untrusted input: every check re-derives safety, never trusting the planner.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from doclayer.curator.config import CuratorConfig
from doclayer.curator.state import CONCEPT_ID_RE, SUBJECT_TYPES, PageChange

_REQUIRED_META = (
    "schema_version",
    "privacy",
    "source_origin",
    "source_lineage",
    "tags",
    "updated",
    "title",
    "concept_id",
)
_PRIVACY_VALUES = {"public", "internal", "sensitive", "credential"}
_ORIGIN_VALUES = {"operator", "agent", "external"}
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
_FILENAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(\.md)$")


@dataclass
class Failure:
    check: str
    detail: str
    severity: str = "blocker"  # blocker | warning
    # A typed category for failures the pipeline acts on (e.g. capacity.SCOPE_CITATION);
    # not part of the serialised record.
    code: str = field(default="", compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {"severity": self.severity, "check": self.check, "detail": self.detail}


@dataclass
class KnownPages:
    """Existing tracked pages (path -> meta) plus the in-batch set."""

    pages: dict[str, dict[str, Any]] = field(default_factory=dict)  # path -> meta
    new_ids: dict[str, str] = field(default_factory=dict)  # concept_id -> page path
    published_paths: set[str] = field(default_factory=set)

    def all_paths(self) -> set[str]:
        return set(self.pages) | self.published_paths

    def resolve_wikilink(self, target: str) -> str | None:
        """Resolve a [[link]] to a known page path, or None when dangling.

        Accepts full paths (with or without .md), bare stems, and the
        `|alias` / `#Heading` suffixes. A bare stem matches a page in any
        directory (the lexicographically first when several share it); a
        link that names a directory must match that exact path."""
        name = target.split("|", 1)[0].split("#", 1)[0].strip()
        if not name:
            return None
        path = name if name.endswith(".md") else f"{name}.md"
        known = self.all_paths()
        if path in known:
            return path
        if "/" in name:
            return None
        matches = sorted(p for p in known if p.rsplit("/", 1)[-1] == path)
        return matches[0] if matches else None


def check_path_allowed(path: str, cfg: CuratorConfig) -> Failure | None:
    """Destination scope: allowed prefixes, .md, safe name, librarian-owned
    files denied wherever they appear."""
    if not path.endswith(".md"):
        return Failure("path", f"{path}: destination must be a .md file")
    if not any(path.startswith(prefix) for prefix in cfg.publish.allowed_prefixes):
        return Failure(
            "path",
            f"{path}: outside allowed destinations {cfg.publish.allowed_prefixes}",
        )
    name = path.rsplit("/", 1)[-1]
    if name in cfg.publish.denied_names:
        return Failure("path", f"{path}: {name} is librarian/operator-owned")
    if path.count("/") >= 2 and name == "index.md":
        return Failure("path", f"{path}: area indexes are librarian-owned")
    parts = path.removesuffix(".md").split("/")
    if not _FILENAME_RE.match(name.lower()):
        return Failure("path", f"{path}: filename must be lowercase words joined by hyphens")
    if any(part in ("..", "") for part in parts):
        return Failure("path", f"{path}: unsafe path")
    return None


def check_frontmatter(
    meta: dict[str, Any],
    path: str,
    *,
    is_new: bool,
    existing_meta: dict[str, Any] | None,
    known: KnownPages,
) -> list[Failure]:
    failures: list[Failure] = []
    for key in _REQUIRED_META:
        if key not in meta or meta[key] in (None, "", []):
            failures.append(Failure("frontmatter", f"{path}: missing '{key}'"))
    if failures:
        return failures
    if meta.get("privacy") not in _PRIVACY_VALUES:
        failures.append(Failure("frontmatter", f"{path}: privacy must be one of {sorted(_PRIVACY_VALUES)}"))
    if meta.get("source_origin") not in _ORIGIN_VALUES:
        failures.append(Failure("frontmatter", f"{path}: source_origin must be one of {sorted(_ORIGIN_VALUES)}"))
    cid = str(meta.get("concept_id", ""))
    if not CONCEPT_ID_RE.match(cid):
        failures.append(Failure("identity", f"{path}: concept_id {cid!r} is not c-+8hex"))
    if not isinstance(meta.get("tags"), list) or not meta.get("tags"):
        failures.append(Failure("frontmatter", f"{path}: tags must be a non-empty list"))
    try:
        updated = meta.get("updated")
        if not isinstance(updated, (str, date)) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(updated)):
            raise ValueError("not a YYYY-MM-DD date")
        date.fromisoformat(str(updated))
    except ValueError:
        failures.append(Failure("frontmatter", f"{path}: updated must be YYYY-MM-DD"))
    if is_new:
        if meta.get("privacy") != "internal":
            failures.append(Failure("frontmatter", f"{path}: new curator pages use privacy: internal"))
        if any(cid == str(other.get("concept_id")) for other in known.pages.values()):
            failures.append(Failure("identity", f"{path}: concept_id {cid} reuses an existing page's id"))
        if cid in known.new_ids and known.new_ids[cid] != path:
            failures.append(Failure("identity", f"{path}: duplicate concept_id {cid} within the batch"))
        known.new_ids[cid] = path
        if meta.get("subject_type") and meta["subject_type"] not in SUBJECT_TYPES:
            failures.append(Failure("frontmatter", f"{path}: unknown subject_type {meta['subject_type']!r}"))
    else:
        existing_meta = existing_meta or {}
        existing_cid = str(existing_meta.get("concept_id", ""))
        if existing_cid and cid != existing_cid:
            failures.append(Failure("identity", f"{path}: update must preserve concept_id {existing_cid}, got {cid!r}"))
        if existing_meta.get("privacy") and meta.get("privacy") != existing_meta.get("privacy"):
            failures.append(Failure("frontmatter", f"{path}: update must preserve privacy unless explicitly intended"))
    return failures


def _heading_key(line: str) -> str:
    """A heading line compared as written, with CRLF, NBSP and runs of spaces normalised."""
    return re.sub(r"\s+", " ", line.replace("\xa0", " ")).strip()


def _unit_words(unit_text: str) -> list[str]:
    return _words_of("\n".join(unit_text.splitlines()[1:]))


def _match_sections(
    existing_body: str, draft_body: str, heading_renames: list[dict[str, str]] | None
) -> tuple[list, list]:
    """(base headed units with the draft heading index each keeps or None, draft headings).

    Sections are matched by identity, not order: a base section is kept by a
    draft section with the same level and heading (explicit anchored repair
    renames carry a heading to its new text); sections sharing a heading pair
    by best text alignment, then order. A moved section is kept."""
    from doclayer.curator.editing import page_units

    original = [unit for unit in page_units(existing_body) if unit.level]
    drafted = [unit for unit in page_units(draft_body) if unit.level]
    current = [(unit.level, _heading_key(unit.text.splitlines()[0])) for unit in drafted]
    renames = list(heading_renames or [])
    keys = []
    for unit in original:
        unit_id, level, heading = unit.id, unit.level, _heading_key(unit.text.splitlines()[0])
        expected = heading
        seen = {expected}
        for _ in range(len(renames)):
            index = next(
                (
                    index
                    for index, row in enumerate(renames)
                    if _heading_key(str(row.get("old", ""))) == expected and row.get("unit_id", unit_id) == unit_id
                ),
                None,
            )
            if index is None:
                break
            row = renames.pop(index)
            candidate = _heading_key(str(row.get("new", "")))
            candidate_units = page_units(candidate + "\n")
            if len(candidate_units) != 1 or candidate_units[0].level != level or candidate in seen:
                break
            expected = candidate
            seen.add(expected)
        keys.append((level, expected))
    # Same-key sections pair by best text alignment, then by order: repeated
    # headings ("### Example") keep their own text wherever they moved.
    pairs = sorted(
        (
            (-_ratio(_unit_words(original[b].text), _unit_words(drafted[d].text)), b, d)
            for b, key in enumerate(keys)
            for d, other in enumerate(current)
            if other == key
        )
    )
    match_of = {}
    taken = set()
    for _, b, d in pairs:
        if b not in match_of and d not in taken:
            match_of[b] = d
            taken.add(d)
    return [(unit, match_of.get(b)) for b, unit in enumerate(original)], current


def _section_name(unit) -> str:
    return re.sub(r"^#{1,6}\s+", "", _heading_key(unit.text.splitlines()[0]))


def _check_update_scope_in_order(
    existing_body: str, draft_body: str, path: str, heading_renames: list[dict[str, str]] | None
) -> list[Failure]:
    """check_update_scope exactly as it was before the deterministic repairs:
    every heading in base order. Correction jobs keep it unchanged."""
    failures: list[Failure] = []
    from doclayer.curator.editing import page_units

    original = [(unit.id, unit.level, unit.text.splitlines()[0]) for unit in page_units(existing_body) if unit.level]
    current = [(unit.level, unit.text.splitlines()[0]) for unit in page_units(draft_body) if unit.level]
    renames = list(heading_renames or [])
    cursor = 0
    for unit_id, level, heading in original:
        expected = heading
        seen = {expected}
        for _ in range(len(renames)):
            index = next(
                (
                    index
                    for index, row in enumerate(renames)
                    if row.get("old") == expected and row.get("unit_id", unit_id) == unit_id
                ),
                None,
            )
            if index is None:
                break
            row = renames.pop(index)
            candidate = row.get("new", "")
            candidate_units = page_units(candidate + "\n")
            if len(candidate_units) != 1 or candidate_units[0].level != level or candidate in seen:
                break
            expected = candidate
            seen.add(expected)
        match = next((index for index in range(cursor, len(current)) if current[index] == (level, expected)), None)
        if match is None:
            failures.append(
                Failure(
                    "scope",
                    f"{path}: update drops existing section '{re.sub(r'^#{1,6} +', '', heading)}' (unrelated removals are not allowed)",
                )
            )
        else:
            cursor = match + 1
    return failures


def check_update_scope(
    existing_meta: dict[str, Any],
    existing_body: str,
    draft_meta: dict[str, Any],
    draft_body: str,
    path: str,
    heading_renames: list[dict[str, str]] | None = None,
    *,
    correction_job: bool = False,
) -> list[Failure]:
    """An update must not silently remove unrelated material: every section
    heading/occurrence of the existing page survives, wherever the draft moved
    it. Explicit anchored repair renames preserve the same structural slot and
    require final evidence checks. A correction job is checked as before the
    deterministic repairs (in base order) and gets none of them."""
    if correction_job:
        return _check_update_scope_in_order(existing_body, draft_body, path, heading_renames)
    matched, _ = _match_sections(existing_body, draft_body, heading_renames)
    return [
        Failure(
            "scope",
            f"{path}: update drops existing section '{_section_name(unit)}' (unrelated removals are not allowed)",
        )
        for unit, match in matched
        if match is None
    ]


# A new heading in a dropped section's slot is its rename when the two
# sections' text (below the heading) aligns at least this closely. When either
# text is near-empty (under NEAR_EMPTY_WORDS words) the text proves nothing and
# the headings must align this closely too; otherwise the new heading is a
# genuinely new section and the base one is re-inserted beside it.
RENAMED_SECTION_RATIO = 0.6
NEAR_EMPTY_WORDS = 5


def _words_of(text: str) -> list[str]:
    return re.findall(r"\w+", re.sub(r"https?://\S+", " ", text).lower())


def _ratio(a: list[str], b: list[str]) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() if a or b else 1.0


def _rename_score(base_section: str, draft_section: str) -> float:
    """How surely draft_section is base_section renamed: its text alignment, or 0."""
    base_lines, draft_lines = base_section.splitlines() or [""], draft_section.splitlines() or [""]
    base_body, draft_body = _words_of("\n".join(base_lines[1:])), _words_of("\n".join(draft_lines[1:]))
    if min(len(base_body), len(draft_body)) < NEAR_EMPTY_WORDS:
        heading = _ratio(
            _words_of(re.sub(r"^#{1,6}\s+", "", base_lines[0])), _words_of(re.sub(r"^#{1,6}\s+", "", draft_lines[0]))
        )
        if heading < RENAMED_SECTION_RATIO:
            return 0.0
    return _ratio(base_body, draft_body)


def _replace_heading_line(body: str, offset: int, heading: str) -> str:
    """Replace the heading line starting at offset, keeping its own line ending."""
    end = body.find("\n", offset)
    end = len(body) if end < 0 else end
    if end > offset and body[end - 1] == "\r":
        end -= 1
    return body[:offset] + heading + body[end:]


def restore_dropped_sections(
    existing_body: str,
    draft_body: str,
    heading_renames: list[dict[str, str]] | None = None,
) -> tuple[str, list[str], list[str]]:
    """Put every base section check_update_scope finds dropped back, without a model.

    Returns (body, restored section names, "name: why" for each that cannot be
    restored). Never run in a correction job. A moved section is not dropped
    (sections match by identity).
    - The page title (a level-1 section) is renamed in place when the draft
      has a level-1 heading of its own: a page has exactly one H1. A draft
      with no H1 gets the base H1 line alone at the top. The preamble (text
      between the H1 and the first subsection) is never section-restored.
    - A new heading at the section's level anywhere in the draft, whose text
      aligns with the base section's (RENAMED_SECTION_RATIO; for near-empty
      sections the headings must align too) and which is not itself a base
      heading, is the drafter's rename (moved or not): the base heading line is
      put back in place and the section appears once.
    - Otherwise the base section is re-inserted verbatim, placed by its nearest
      surviving base neighbours as the draft orders them: right after the
      earlier neighbour and its deeper subsections, else before the later one.
    A section with no surviving neighbour on either side cannot be placed.
    """
    from doclayer.curator.editing import page_units

    base_keys = {
        (unit.level, _heading_key(unit.text.splitlines()[0])) for unit in page_units(existing_body) if unit.level
    }
    body = draft_body
    restored = []
    impossible = []
    skipped = set()
    while True:
        matched, current = _match_sections(existing_body, body, heading_renames)
        index = next((i for i, (unit, match) in enumerate(matched) if match is None and i not in skipped), None)
        if index is None:
            return body, restored, impossible
        unit = matched[index][0]
        name = _section_name(unit)
        every = page_units(body)
        units = [u for u in every if u.level]
        offsets = []
        offset = 0
        for u in every:
            if u.level:
                offsets.append(offset)
            offset += len(u.text)
        kept = {match for _, match in matched if match is not None}
        new_heads = [i for i, key in enumerate(current) if i not in kept and key not in base_keys]
        before = next((match for _, match in reversed(matched[:index]) if match is not None), None)
        after = next((match for _, match in matched[index + 1 :] if match is not None), None)
        heading_line = unit.text.splitlines()[0]
        candidate = None
        if unit.level == 1:
            titles = [i for i in new_heads if current[i][0] == 1]
            if titles:
                candidate = _replace_heading_line(body, offsets[titles[0]], heading_line)
            elif any(level == 1 for level, _ in current):
                skipped.add(index)
                impossible.append(f"{name}: the draft's page title is another base section's")
                continue
            else:
                candidate = heading_line + "\n\n" + body.lstrip("\n")
        else:
            renamed = max(
                ((_rename_score(unit.text, units[i].text), i) for i in new_heads if current[i][0] == unit.level),
                default=(0.0, None),
            )
            if renamed[0] >= RENAMED_SECTION_RATIO:
                candidate = _replace_heading_line(body, offsets[renamed[1]], heading_line)
            elif before is None and after is None:
                skipped.add(index)
                impossible.append(f"{name}: no base section around it survives in the draft")
                continue
            else:
                low = 0 if before is None else before + 1
                high = after if after is not None and after >= low else len(current)
                position = next((i for i in range(low, high) if current[i][0] <= unit.level), high)
                text = unit.text.rstrip("\n") + "\n"
                if position < len(current):
                    start = offsets[position]
                    candidate = body[:start] + text + "\n" + body[start:]
                else:
                    head = body.rstrip("\n")
                    candidate = (head + "\n\n" if head else "") + text
        if _match_sections(existing_body, candidate, heading_renames)[0][index][1] is None:
            skipped.add(index)
            impossible.append(f"{name}: re-inserting it between its neighbours did not restore its place")
            continue
        body = candidate
        restored.append(name)


def check_links(
    change: PageChange,
    known: KnownPages,
    published: set[str],
    existing_body: str | None = None,
) -> list[Failure]:
    """Wikilinks in the draft must resolve against existing pages, the
    in-batch published set, or be intentional path-less dangling markers.
    A link with an explicit path (or .md suffix) that resolves nowhere is a
    mistaken path, not a dangling marker. Links already present in the
    existing canonical body are not the draft's doing and are skipped."""
    failures: list[Failure] = []
    all_known = KnownPages(pages=dict(known.pages), new_ids=dict(known.new_ids))
    all_known.published_paths = known.published_paths | published | {change.path}
    preexisting = {t.strip() for t in _WIKILINK_RE.findall(existing_body or "")}
    for target in _WIKILINK_RE.findall(change.draft_body):
        if target.strip() in preexisting:
            continue
        resolved = all_known.resolve_wikilink(target)
        if resolved is not None:
            continue
        looks_like_path = "/" in target or target.strip().endswith(".md")
        if looks_like_path:
            failures.append(Failure("links", f"{change.path}: [[{target}]] names a path that does not exist"))
        else:
            failures.append(
                Failure(
                    "links",
                    f"{change.path}: [[{target}]] has no page yet (intentional dangling "
                    "links are allowed; record it in the digest)",
                    severity="warning",
                )
            )
    return failures


def normalize_claim(text: str) -> str:
    lowered = text.lower()
    lowered = re.sub(r"[\*_`>#\[\]]", "", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def check_corrections_replay(change: PageChange, corrections: list[dict[str, Any]]) -> list[Failure]:
    """Replaying unchanged source evidence must not restore an
    operator-corrected claim. Deterministic layer: exact normalized
    reinstatement of a corrected claim's text is a blocker."""
    failures: list[Failure] = []
    body = normalize_claim(change.draft_body)
    for correction in corrections:
        claim = normalize_claim(str(correction.get("claim", "")))
        if len(claim) < 25:
            continue  # too short for a reliable match; model check covers it
        if claim in body:
            failures.append(
                Failure(
                    "corrections",
                    f"{change.path}: reinstates operator-corrected claim: "
                    f'"{claim[:120]}…" ({correction.get("correction_id", "?")})',
                )
            )
    return failures


def check_citations(change: PageChange) -> list[Failure]:
    """Substantive additions need source support: the draft must carry at
    least one source citation marker for each substantive section. Curator
    drafts cite as `[source: <label or url>]` near the claim (model prompt
    contract); a draft with sources available but no citation anywhere is a
    blocker."""
    if re.search(r"\[\[?src-[0-9a-f]{10}", change.draft_body):
        return [Failure("support", f"{change.path}: unresolved source-id citation; use a source URL or label")]
    if not change.source_ids:
        return []
    if "[source:" not in change.draft_body and "https://" not in change.draft_body:
        return [
            Failure(
                "support",
                f"{change.path}: substantive additions carry no source citation (cite sources near the claims)",
            )
        ]
    return []


_MATERIAL = r"(?:source|sources|documentation|docs|material|materials|evidence|excerpt|excerpts)"
_QUAL = r"(?:the|these|those|this|provided|supplied|checked|available|cited|retrieved|committed|referenced|consulted|reviewed|given)"
_CONSULTED = r"(?:provided|supplied|checked|cited|retrieved|referenced|consulted|reviewed|given|available)"
_NEG = r"(?:(?:do|does|did)\s+not|doesn['’]t|don['’]t|didn['’]t|fails?\s+to|failed\s+to)"
_ADV = r"(?:\w+ly\s+)?"
_VERB = r"(?:mention|document|specify|state|include|list|describe|cover|provide|contain|say|confirm|address|explain|indicate|show|identify|establish|name|record)"
_ABSENCE = re.compile(
    rf"(?:^\s*|[.!?:;]\s+)(?:(?:[-*+>]|\d+\.)\s+)*(?:\*\*Note:\*\*\s+|Note:\s+)?{_MATERIAL}\s+{_NEG}\s+{_ADV}{_VERB}\b"
    rf"|\b{_QUAL}\s+(?:[\w./-]+\s+){{0,3}}?{_MATERIAL}\s+{_NEG}\s+{_ADV}{_VERB}\b"
    # "documents" is a material noun only directly after a qualifier that denotes
    # consultation; "the documents" / "MongoDB documents" product prose stays unblocked.
    rf"|\b{_CONSULTED}\s+documents?\s+{_NEG}\s+{_ADV}{_VERB}\b"
    rf"|\b{_MATERIAL}\s+(?:contains?|includes?|has|have|makes?)\s+no\s+(?:mention|reference|record|statement)\b"
    rf"|\b{_MATERIAL}\s+(?:says?|said)\s+nothing\s+(?:about|on)\b"
    rf"|\bno\s+mention\s+of\b[^.\n]{{0,80}}?\b(?:in|by)\s+(?:the|any)\s+(?:\w+\s+){{0,2}}?{_MATERIAL}\b"
    rf"|\bnot\s+(?:documented|specified|mentioned|stated|covered|available|present|found|included)\s+(?:in|by)\s+(?:the|any)\s+(?:\w+\s+){{0,2}}?{_MATERIAL}\b"
    rf"|\b{_MATERIAL}\s+(?:is|are|was|were)\s+silent\s+(?:on|about)\b"
    r"|^#{1,6}\s.*\bdocumentation gaps?\b",
    re.I | re.M,
)


_SETEXT_UNDERLINE = re.compile(r"^ {0,3}(=+|-{2,})[ \t]*$")
_NOT_SETEXT_TEXT = re.compile(r"^\s*(?:#|>|\||[*+-]\s|\d+[.)]\s|`{3,}|~{3,})|^ {4,}|^\t")


def normalize_setext(body: str) -> str:
    """Rewrite setext headings ("Title" over a line of === or ---) as ATX ("# Title", "## Title").

    Only a single plain text line directly above the underline counts, with a
    blank line, a heading or the page start before it. A --- line after a
    blank line is a thematic break, and leading frontmatter and fenced code
    are never touched, so H1 counting and section matching see every heading
    the way page_units does. CRLF line endings become LF first."""
    lines = body.replace("\r\n", "\n").split("\n")
    start = 0
    if lines and lines[0].strip() == "---":  # leading frontmatter block: YAML between --- lines
        closing = next((i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), None)
        if (
            closing is not None
            and closing > 1
            and all(re.match(r"^\s*(?:[\w.-]+\s*:|-\s|#)", line) or not line.strip() for line in lines[1:closing])
            and any(line.strip() for line in lines[1:closing])
        ):
            start = closing + 1
    out = lines[:start]
    fence = False
    index = start
    while index < len(lines):
        line = lines[index]
        if re.match(r"^ {0,3}(`{3,}|~{3,})", line):
            fence = not fence
        underline = lines[index + 1] if index + 1 < len(lines) else ""
        above = out[-1] if len(out) > start else ""
        if (
            not fence
            and line.strip()
            and _SETEXT_UNDERLINE.match(underline)
            and not _NOT_SETEXT_TEXT.match(line)
            and not _SETEXT_UNDERLINE.match(line)
            and (not above.strip() or above.lstrip().startswith("#"))
        ):
            out.append(("# " if underline.strip()[0] == "=" else "## ") + line.strip())
            index += 2
            continue
        out.append(line)
        index += 1
    return "\n".join(out)


def _title_key(text: str | None) -> str | None:
    """A title compared as read: CRLF, NBSP and runs of spaces normalised."""
    if text is None:
        return None
    key = re.sub(r"\s+", " ", str(text).replace("\xa0", " ")).strip()
    return key or None


def _h1_units(body: str) -> list[tuple[int, Any]]:
    """(offset, unit) of each level-1 heading, outside code fences."""
    from doclayer.curator.editing import page_units

    found, offset = [], 0
    for unit in page_units(body or ""):
        if unit.level == 1:
            found.append((offset, unit))
        offset += len(unit.text)
    return found


def h1_text(body: str) -> str | None:
    """The page title heading: the first level-1 heading's text, outside code fences."""
    units = _h1_units(body)
    return _title_key(re.sub(r"^#\s+", "", units[0][1].text.splitlines()[0])) if units else None


def expected_titles(
    change: PageChange, existing_meta: dict[str, Any] | None, existing_body: str | None
) -> tuple[str | None, str | None]:
    """(page H1, title) an update must carry: the curator never renames a page.

    An update keeps the base page's own H1 and frontmatter title (pages whose
    two already differ keep both as they are), and the plan's title follows
    the base title. A new page renames nothing: (None, None)."""
    if change.action == "create" or existing_body is None:
        return None, None
    base_h1 = h1_text(existing_body)
    base_title = _title_key((existing_meta or {}).get("title")) or base_h1
    return base_h1, base_title


def _title_rules(
    change: PageChange, existing_meta: dict[str, Any] | None, existing_body: str | None, plan: bool = True
) -> dict[str, tuple]:
    """{field: (found, expected)} for each title an update must keep.

    `plan=False` (publication): only the page's own H1 and frontmatter title,
    against the page actually being published over; the plan title is not a
    property of the published page."""
    want_h1, want_title = expected_titles(change, existing_meta, existing_body)
    rules = {
        "page H1": (h1_text(change.draft_body), want_h1),
        "frontmatter title": (_title_key((change.draft_meta or {}).get("title")), want_title),
    }
    if plan:
        rules["plan title"] = (_title_key(change.title), want_title)
    return {name: pair for name, pair in rules.items() if pair[1]}


def check_title(
    change: PageChange, existing_meta: dict[str, Any] | None, existing_body: str | None, *, plan: bool = True
) -> list[Failure]:
    """An update's H1, frontmatter title and plan title agree with the base page's
    (see expected_titles), and the page keeps exactly one H1."""
    failures = [
        Failure("title", f"{change.path}: {name} {have!r} differs from {want!r}; pages are never renamed")
        for name, (have, want) in _title_rules(change, existing_meta, existing_body, plan).items()
        if have != want
    ]
    if (
        change.action != "create"
        and existing_body is not None
        and len(_h1_units(existing_body)) <= 1
        and len(_h1_units(change.draft_body)) > 1
    ):
        failures.append(Failure("title", f"{change.path}: the page has more than one H1; a page has exactly one title"))
    return failures


def restore_titles(change: PageChange, existing_meta: dict[str, Any] | None, existing_body: str | None) -> list[str]:
    """Put the expected H1, frontmatter title and plan title back; names only what actually changed.

    The H1 is replaced in place on its own line (never prepended, never a line
    inside a code fence), keeping that line's ending."""
    fixed = []
    for name, (have, want) in _title_rules(change, existing_meta, existing_body).items():
        if have == want:
            continue
        if name == "page H1":
            units = _h1_units(change.draft_body)
            if not units:
                continue
            body = _replace_heading_line(change.draft_body, units[0][0], "# " + want)
            if body != change.draft_body:
                change.draft_body = body
                fixed.append(name)
        elif name == "frontmatter title":
            change.draft_meta = {**(change.draft_meta or {}), "title": want}
            fixed.append(name)
        else:
            change.title = want
            fixed.append(name)
    return fixed


def check_absence_claims(change: PageChange, existing_body: str | None) -> list[Failure]:
    """Omission from the checked sources is not evidence of absence. New text may
    not assert what a source does not say; the fact is left out instead."""
    before = set((existing_body or "").splitlines())
    return [
        Failure("absence", f"{change.path}: new text asserts source absence: {line.strip()[:160]}")
        for line in change.draft_body.splitlines()
        if line not in before and _ABSENCE.search(line)
    ]


def run_deterministic_checks(
    change: PageChange,
    cfg: CuratorConfig,
    known: KnownPages,
    existing_meta: dict[str, Any] | None,
    existing_body: str | None,
    corrections: list[dict[str, Any]],
    *,
    correction_job: bool = False,
    publication: bool = False,
) -> list[Failure]:
    """Every deterministic check for one change. `known` carries existing
    pages + already-accepted in-batch paths. At publication (after
    reconciliation onto the fresh page) the title rule checks only the page's
    H1 and frontmatter title against the fresh page."""
    failures: list[Failure] = []
    path_failure = check_path_allowed(change.path, cfg)
    if path_failure:
        return [path_failure]
    is_new = change.action == "create"
    meta = change.draft_meta or {}
    if not meta:
        return [Failure("frontmatter", f"{change.path}: draft has no frontmatter")]
    failures += check_frontmatter(meta, change.path, is_new=is_new, existing_meta=existing_meta, known=known)
    if existing_body is not None and not is_new:
        failures += check_update_scope(
            existing_meta or {},
            existing_body,
            meta,
            change.draft_body,
            change.path,
            change.draft_progress.get("heading_renames", [])
            if change.draft_progress.get("body_hash") == hashlib.sha256(change.draft_body.encode()).hexdigest()
            else [],
            correction_job=correction_job,
        )
    failures += check_links(change, known, set(), None if is_new else existing_body)
    if not correction_job:  # a correction job is checked as before the title rule
        failures += check_title(change, existing_meta, None if is_new else existing_body, plan=not publication)
    failures += check_corrections_replay(change, corrections)
    failures += check_citations(change)
    failures += check_absence_claims(change, existing_body)
    return failures


def blockers(failures: list[Failure]) -> list[Failure]:
    return [f for f in failures if f.severity == "blocker"]
