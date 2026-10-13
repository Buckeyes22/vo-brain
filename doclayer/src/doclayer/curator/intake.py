"""Curator intake: turn submitted material into inspected sources.

Accepted inputs: URLs, pasted text, UTF-8 Markdown/text files, text-based
PDFs, YAML batch manifests, and one-shot directory enumeration. Downloads stream with hard byte caps (counted on
decoded data), every redirect is validated, junk (login/soft-404/empty) is
never treated as a source, and PDFs get a page map so claims can cite
"page N".

Discovered links are normalized (fragment stripped for identity, query kept
so version parameters never collapse), deduplicated by URL and content
hash, and returned as candidates for relevance scoring. Nothing here trusts
source content: links are data, never instructions.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import yaml

from doclayer import quality
from doclayer.convert import html_to_markdown, page_title
from doclayer.curator.state import SourceRecord

USER_AGENT = "brain-curator/0.1 (brain wiki curator; single-user)"

#: Non-http(s) schemes and obvious non-document links never enter discovery.
_SKIP_SCHEMES = ("mailto:", "javascript:", "tel:", "ftp:", "data:")
_LINK_NOISE_RE = re.compile(
    r"(?i)(facebook\.com|twitter\.com|x\.com/|linkedin\.com|instagram\.com|"
    r"youtube\.com|/share\?|/login|/signin|/signup|/account|/cart|/pricing\?"
    r"|utm_|gclid=|/feed$|/rss$|\.svg$|\.png$|\.jpe?g$|\.gif$|\.zip$|\.tar"
    r"(?:\.gz)?$|\.exe$|\.dmg$)"
)
_MAX_REDIRECTS = 5


class IntakeError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# URL identity and links
# --------------------------------------------------------------------------- #


def normalize_url(url: str, base: str | None = None) -> str:
    """Resolve + normalize: absolute, fragment stripped, no trailing '#'.

    Query parameters are kept: version/API-version parameters are meaningful
    identity, and collapsing them would merge distinct editions.
    """
    if base:
        url = urljoin(base, url)
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise IntakeError(f"unsupported URL scheme: {url!r}")
    if not parts.netloc:
        raise IntakeError(f"URL has no host: {url!r}")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def url_anchor(url: str) -> str:
    """The fragment of the original URL (section reference), '' if none."""
    return urlsplit(url).fragment


def extract_links(markdown: str, base_url: str | None = None) -> list[tuple[str, str]]:
    """(absolute_url, anchor_text) pairs from inline/reference markdown links
    and bare http(s) URLs. Fragments are stripped from the returned URL but
    kept out of identity elsewhere; noise links (social, auth, assets) are
    dropped here.
    """
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(raw: str, anchor: str) -> None:
        raw = raw.strip()
        if not raw or any(raw.lower().startswith(s) for s in _SKIP_SCHEMES):
            return
        if raw.startswith("#"):
            return
        try:
            absolute = normalize_url(raw, base_url)
        except IntakeError:
            return
        if _LINK_NOISE_RE.search(absolute):
            return
        if absolute in seen:
            return
        seen.add(absolute)
        found.append((absolute, " ".join(anchor.split())[:200]))

    # Inline links, then reference-style definitions.
    for match in re.finditer(r"\[([^\]]{0,300})\]\(([^)\s]+)[^)]*\)", markdown):
        add(match.group(2), match.group(1))
    for match in re.finditer(r"^\s{0,3}\[([^\]]+)\]:\s*(\S+)", markdown, re.M):
        add(match.group(2), match.group(1))
    # Bare URLs in plain text.
    for match in re.finditer(r"(?<![(\[])\bhttps?://[^\s<>)\]]+", markdown):
        add(match.group(0), "")
    return found


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Section maps (chunking for extraction and long-document handling)
# --------------------------------------------------------------------------- #

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.M)


def section_map(markdown: str) -> list[tuple[str, str]]:
    """(locator, text) sections split on markdown headings. A preamble gets
    locator "(introduction)"."""
    headings = []
    fence = None
    offset = 0
    for line in markdown.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            run = marker.group(1)
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence):
                fence = None
        elif fence is None:
            match = _HEADING_RE.match(line.rstrip("\r\n"))
            if match:
                headings.append((offset, match.group(1), match.group(2).strip()))
        offset += len(line)
    if not headings:
        body = markdown.strip()
        return [("(introduction)", body)] if body else []
    sections = []
    preamble = markdown[: headings[0][0]].strip()
    if preamble:
        sections.append(("(introduction)", preamble))
    occurrences = {}
    hierarchy = []
    for index, (start, hashes, title) in enumerate(headings):
        level = len(hashes)
        hierarchy = [(depth, name) for depth, name in hierarchy if depth < level]
        hierarchy.append((level, title))
        name = " / ".join(name for _, name in hierarchy)
        occurrences[name] = occurrences.get(name, 0) + 1
        locator = "#" + name + (f" [occurrence {occurrences[name]}]" if occurrences[name] > 1 else "")
        end = headings[index + 1][0] if index + 1 < len(headings) else len(markdown)
        body = markdown[start:end].strip()
        # Empty parents supply context through hierarchical locators, not empty review units.
        if (
            body.strip() == (hashes + " " + title).strip()
            and index + 1 < len(headings)
            and len(headings[index + 1][1]) > level
        ):
            continue
        sections.append((locator, body))
    return sections


def _split_source_section(text: str, budget: int) -> list[str]:
    """Bound source text without cutting ordinary table rows/code blocks.

    Large tables repeat their column header; large fenced examples repeat
    their fence. Paragraphs split on line/word boundaries only as necessary.
    Locators remain numbered parts of the original hierarchical section.
    """
    lines = text.splitlines(keepends=True)
    blocks, index = [], 0
    while index < len(lines):
        line = lines[index]
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        table = index + 1 < len(lines) and "|" in line and re.match(r"^\s*\|?\s*:?-{3,}", lines[index + 1])
        if fence:
            end = index + 1
            while end < len(lines) and not re.match(
                r"^ {0,3}" + re.escape(fence.group(1)[0]) + "{" + str(len(fence.group(1))) + r",}\s*$", lines[end]
            ):
                end += 1
            has_closing = end < len(lines)
            end = min(len(lines), end + 1)
            block = "".join(lines[index:end])
            if len(block) <= budget:
                blocks.append(block)
            else:
                closing = fence.group(1) + "\n"
                content = lines[index + 1 : end - 1 if has_closing else end]
                group = []
                for row in content:
                    if group and len(line) + len(closing) + sum(map(len, group)) + len(row) > budget:
                        blocks.append(line + "".join(group) + closing)
                        group = []
                    group.append(row)
                if group:
                    blocks.append(line + "".join(group) + closing)
            index = end
        elif table:
            header = line + lines[index + 1]
            end = index + 2
            rows = []
            while end < len(lines) and "|" in lines[end] and lines[end].strip():
                row = lines[end]
                if rows and len(header) + sum(map(len, rows)) + len(row) > budget:
                    blocks.append(header + "".join(rows))
                    rows = []
                rows.append(row)
                end += 1
            blocks.append(header + "".join(rows))
            index = end
        else:
            blocks.append(line)
            index += 1
    pieces, current = [], ""
    for block in blocks:
        if len(block) > budget:
            # An individual pathological row/line still obeys the hard cap;
            # its part locator makes the partial context explicit.
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(block[start : start + budget] for start in range(0, len(block), budget))
        else:
            if current and len(current) + len(block) > budget:
                pieces.append(current)
                current = ""
            current += block
    if current:
        pieces.append(current)
    return pieces


def group_sections(sections: list[tuple[str, str]], char_budget: int) -> list[list[tuple[str, str]]]:
    """Group consecutive sections so each chunk stays within the prompt's
    conservative character budget; split oversized sections with part locators."""
    chunks: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    size = 0
    bounded = []
    for locator, text in sections:
        if len(text) <= char_budget:
            bounded.append((locator, text))
        else:
            bounded.extend(
                (f"{locator} (part {index + 1})", part)
                for index, part in enumerate(_split_source_section(text, char_budget))
            )
    for locator, text in bounded:
        if current and size + len(text) > char_budget:
            chunks.append(current)
            current, size = [], 0
        current.append((locator, text))
        size += len(text)
    if current:
        chunks.append(current)
    return chunks


# --------------------------------------------------------------------------- #
# PDF extraction (text-based PDFs only; OCR is out of scope)
# --------------------------------------------------------------------------- #


def extract_pdf_pages(pdf_path) -> list[tuple[int, str]]:
    """(page_number, text) for every page with text; pages with no
    extractable text yield '' so coverage reporting can name them."""
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path) if isinstance(pdf_path, Path) else pdf_path)
    pages: list[tuple[int, str]] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            pages.append((number, page.extract_text() or ""))
        except Exception:  # noqa: BLE001 - a broken page must not kill intake
            pages.append((number, ""))
    return pages


def pdf_to_markdown(pdf_path: Path) -> str:
    """Render page texts as markdown sections with page locators."""
    parts = ["# PDF text (page map)", ""]
    for number, text in extract_pdf_pages(pdf_path):
        parts.append(f"## Page {number}")
        parts.append("")
        parts.append(text.strip())
        parts.append("")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# HTTP fetching
# --------------------------------------------------------------------------- #

_JUNK_MARKERS_SOFT_404 = ('notfound: "true"', "statusCode: 404")


class FetchedPage:
    def __init__(
        self,
        url: str,
        status: int,
        markdown: str,
        title: str | None,
        final_url: str,
        bytes_read: int,
        redirect_chain: list[str] | None = None,
    ) -> None:
        self.url = url
        self.status = status
        self.markdown = markdown
        self.title = title
        self.final_url = final_url
        self.bytes_read = bytes_read
        self.redirect_chain = redirect_chain or [url, final_url]


class FetchFailure(Exception):
    def __init__(self, reason: str, *, transient: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.transient = transient


def _validate_redirect(from_url: str, to_url: str) -> str:
    """Validate one hop: absolute http(s), never a downgrade to plain http
    from https, and never a non-http scheme."""
    parts_to = urlsplit(to_url)
    if parts_to.scheme not in ("http", "https") or not parts_to.netloc:
        raise FetchFailure(f"redirect to non-http(s) target: {to_url}", transient=False)
    if urlsplit(from_url).scheme == "https" and parts_to.scheme == "http":
        raise FetchFailure("https->http redirect downgrade", transient=False)
    return urlunsplit((parts_to.scheme, parts_to.netloc, parts_to.path or "/", parts_to.query, ""))


async def fetch_url(client: httpx.AsyncClient, url: str, *, max_bytes: int, url_policy=None) -> FetchedPage:
    """Fetch one URL with validated redirects and a hard cap on decoded bytes.
    Raises FetchFailure (transient flag set) after the client's retry policy
    is exhausted by the caller."""
    current = url
    lineage = [url]
    for _hop in range(_MAX_REDIRECTS + 1):
        if url_policy is not None:
            allowed, reason = url_policy(current)
            if not allowed:
                raise FetchFailure(f"source policy: {reason}", transient=False)
        response = await client.send(client.build_request("GET", current), follow_redirects=False, stream=True)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            if not location:
                raise FetchFailure("redirect with no Location header", transient=False)
            current = _validate_redirect(current, urljoin(current, location))
            lineage.append(current)
            await response.aclose()
            continue
        break
    else:
        raise FetchFailure(f"more than {_MAX_REDIRECTS} redirects", transient=False)

    if response.status_code in (429,) or response.status_code >= 500:
        await response.aclose()
        raise FetchFailure(f"http_{response.status_code}", transient=True)
    if response.status_code != 200:
        await response.aclose()
        raise FetchFailure(f"http_{response.status_code}", transient=False)

    # Stream the decoded body with a hard byte cap (decompression included).
    chunks: list[bytes] = []
    read = 0
    async for chunk in response.aiter_bytes():
        read += len(chunk)
        if read > max_bytes:
            await response.aclose()
            raise FetchFailure(f"source exceeded {max_bytes} bytes", transient=False)
        chunks.append(chunk)
    await response.aclose()
    body = b"".join(chunks)
    if body.startswith(b"%PDF-") or "application/pdf" in response.headers.get("content-type", ""):
        from io import BytesIO

        try:
            markdown = pdf_to_markdown(BytesIO(body))
        except Exception as exc:
            raise FetchFailure(f"PDF extraction failed: {type(exc).__name__}", transient=False) from exc
        title = urlsplit(current).path.rsplit("/", 1)[-1]
    else:
        html = body.decode("utf-8", errors="replace")
        title = page_title(html)
        try:
            markdown = html_to_markdown(preserve_model_support_flags(html, current))
        except RecursionError as exc:
            raise FetchFailure("HTML nesting exceeds converter limit", transient=False) from exc
    return FetchedPage(
        url=url,
        status=response.status_code,
        markdown=markdown,
        title=title,
        final_url=current,
        bytes_read=read,
        redirect_chain=lineage,
    )


def preserve_model_support_flags(html: str, url: str) -> str:
    """Make the OpenAI model card's endpoint icon state survive text conversion.

    Endpoint lists include unsupported APIs too. Only the observed crossed-out
    SVG or supported icon background determines status; unknown markup remains
    explicitly unspecified rather than silently treating a list as support.
    """
    parsed = urlsplit(url)
    if parsed.hostname != "developers.openai.com" or not parsed.path.startswith("/api/docs/models/"):
        return html
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find(string=lambda s: bool(s and s.strip() == "Endpoints"))
    if heading is None:
        return html
    block = heading.parent.parent
    for row in block.select("div.flex.flex-row.gap-2"):
        label = row.select_one("div.text-sm.font-semibold")
        endpoint = row.select_one("div.text-xs")
        if label is None or endpoint is None:
            continue
        if row.find("mask", id="maskSubtract") is not None:
            status = "not supported by this model"
        elif row.select_one(".bg-primary-soft") is not None:
            status = "supported by this model"
        else:
            status = "model support unspecified in extracted markup"
        endpoint.append(" — " + status)
    return str(soup)


def classify_page(status: int, markdown: str) -> tuple[bool, str]:
    """Reuse the fetcher's junk detection, adapted: intake judges the
    converted markdown, not the raw HTML."""
    if status != 200:
        return False, f"junk:http_{status}"
    if len(markdown.strip()) < quality.MIN_BODY_CHARS:
        return False, "junk:too_short"
    lowered = markdown[:2000].lower()
    if any(marker in lowered for marker in ("sign in to continue", "log in to continue", "access denied")):
        return False, "junk:login_page"
    return True, "ok"


# --------------------------------------------------------------------------- #
# Source building
# --------------------------------------------------------------------------- #


def source_id_for(identity: str) -> str:
    return "src-" + hashlib.sha1(identity.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]


def make_text_source(label: str, text: str, *, role: str = "primary") -> SourceRecord:
    identity = f"text:{label}:{content_hash(text)[:16]}"
    return SourceRecord(
        id=source_id_for(identity),
        kind="text",
        label=label,
        role=role,
        content_hash=content_hash(text),
        bytes=len(text.encode("utf-8")),
        submitted=True,
    )


def make_file_source(path: Path, *, role: str = "primary") -> SourceRecord:
    suffix = path.suffix.lower()
    kind = "pdf" if suffix == ".pdf" else ("markdown" if suffix in (".md", ".markdown") else "text")
    identity = f"file:{path.resolve()}:{path.stat().st_size}"
    return SourceRecord(
        id=source_id_for(identity),
        kind=kind,
        label=path.name,
        role=role,
        url=None,
        content_hash=None,  # filled after reading
        bytes=path.stat().st_size,
        submitted=True,
    )


def make_url_source(url: str, *, role: str = "primary", parent: SourceRecord | None = None) -> SourceRecord:
    normalized = normalize_url(url)
    return SourceRecord(
        id=source_id_for(f"url:{normalized}"),
        kind="url",
        label=normalized,
        role=role,
        url=normalized,
        submitted=parent is None,
        parent_id=parent.id if parent else None,
    )


SUPPORTED_FILE_SUFFIXES = (".md", ".markdown", ".txt", ".pdf")


def enumerate_directory(directory: Path, *, recursive: bool = True) -> list[Path]:
    """Supported files in a directory, enumerated once. Symlinks are never
    followed (design: no symlink following, no directory watching)."""
    out: list[Path] = []
    iterator = directory.rglob("*") if recursive else directory.iterdir()
    for path in sorted(iterator):
        if path.is_symlink():
            continue
        if path.is_file() and path.suffix.lower() in SUPPORTED_FILE_SUFFIXES:
            out.append(path)
    return out


def load_manifest(path: Path) -> dict[str, Any]:
    """A batch manifest: topic/intent, exclusions, and inputs with roles."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise IntakeError(f"manifest {path} is not a mapping")
    return data


def build_sources_from_manifest(
    manifest: dict[str, Any], base_dir: Path | None = None
) -> tuple[str, list[str], list[SourceRecord]]:
    """(topic, exclusions, sources) from manifest inputs."""
    topic = str(manifest.get("topic") or manifest.get("intent") or "").strip()
    exclusions = [str(e) for e in (manifest.get("exclusions") or [])]
    sources: list[SourceRecord] = []
    for entry in manifest.get("sources") or manifest.get("inputs") or []:
        if not isinstance(entry, dict):
            raise IntakeError(f"manifest source entry is not a mapping: {entry!r}")
        role = str(entry.get("role", "primary"))
        if role not in ("primary", "supplementary", "reference"):
            raise IntakeError(f"manifest role not recognized: {role!r}")
        start = len(sources)
        if "url" in entry:
            sources.append(make_url_source(str(entry["url"]), role=role))
        elif "file" in entry:
            file_path = Path(str(entry["file"]))
            if base_dir is not None and not file_path.is_absolute():
                file_path = base_dir / file_path
            sources.append(make_file_source(file_path.expanduser(), role=role))
        elif "text" in entry:
            sources.append(make_text_source(str(entry.get("label", "pasted-text")), str(entry["text"]), role=role))
        elif "directory" in entry:
            dir_path = Path(str(entry["directory"]))
            if base_dir is not None and not dir_path.is_absolute():
                dir_path = base_dir / dir_path
            for file_path in enumerate_directory(dir_path.expanduser()):
                sources.append(make_file_source(file_path, role=role))
        else:
            raise IntakeError(f"manifest source entry has no url/file/text: {entry!r}")
        for source in sources[start:]:
            source.scope = {str(k): str(v) for k, v in (entry.get("applicability") or {}).items()}
    if not topic:
        raise IntakeError("manifest needs a topic")
    return topic, exclusions, sources


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def matches_exclusions(text: str, exclusions: list[str]) -> str | None:
    """Return the exclusion term that matches, or None. Used to keep
    excluded material (e.g. Bedrock usage docs) out of discovery."""
    lowered = text.lower()
    for term in exclusions:
        needle = term.strip().lower()
        if needle and re.search(rf"\b{re.escape(needle)}\b", lowered):
            return term
    return None
