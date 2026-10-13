"""The coverage catalog: committed brain pages as routable subjects.

The catalog is derived, never hand-inventoried: subjects come from committed
canonical page identities (``product:`` frontmatter, page stems, declared
aliases) plus the committed routing policy's explicit aliases and
subject-to-source relationships. It is rebuilt from the committed bytes of the
selected brain reference — never dirty working content — and cached keyed by
the reference HEAD and the policy version.

Catalog presence means potential evidence. It is never proof that a question
is answered: missing facet metadata means unknown coverage, not certified
absence. A corrupt or unavailable catalog yields ``coverage_unknown`` (with an
explicitly stale prior snapshot when one is usable), not an empty catalog that
would fabricate thousands of missing-document jobs.
"""

from __future__ import annotations

import glob as _globmod
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from doclayer import frontmatter

SCHEMA_VERSION = 1

#: Directories whose committed pages establish canonical subjects. Nested
#: pages are recognized (recursive walk); index pages never name a subject.
SUBJECT_DIRS = ("wiki", "infra")

#: Committed design/operations pages do not mint subjects, but a policy
#: subject without a page stays routable against them (targeted committed
#: evidence) with its no-page status recorded honestly.
SUPPORT_DIRS = ("doclayer",)

_IP = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

PROPOSAL_PATTERN = re.compile(r"\b(proposal|proposed|draft)\b", re.IGNORECASE)
HISTORICAL_PATTERN = re.compile(r"\b(historical|superseded|archived?)\b", re.IGNORECASE)


class CatalogUnavailable(RuntimeError):
    """The catalog could not be built; callers treat coverage as unknown."""


@dataclass
class CatalogPage:
    path: str
    concept_id: str | None
    authority: str = "current"  # current | historical | proposal


@dataclass
class SubjectEntry:
    slug: str
    kind: str  # product | account | service | host | model | model-family | procedure | host-inventory
    title: str
    aliases: list[str] = field(default_factory=list)
    facets: list[str] = field(default_factory=list)
    pages: list[CatalogPage] = field(default_factory=list)
    source_copies: list[str] = field(default_factory=list)
    public_docs: dict[str, str] = field(default_factory=dict)
    live_checks: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    local_first: bool = False
    #: "committed" when at least one canonical page backs the subject;
    #: "policy" when only the policy names it (targeted committed evidence is
    #: still searchable; presence is not proof of coverage).
    page_status: str = "committed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "kind": self.kind,
            "title": self.title,
            "aliases": self.aliases,
            "facets": self.facets,
            "pages": [p.__dict__ for p in self.pages],
            "source_copies": self.source_copies,
            "public_docs": self.public_docs,
            "live_checks": self.live_checks,
            "related": self.related,
            "local_first": self.local_first,
            "page_status": self.page_status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubjectEntry":
        return cls(
            slug=data["slug"],
            kind=data.get("kind", "product"),
            title=data.get("title", data["slug"]),
            aliases=list(data.get("aliases", [])),
            facets=list(data.get("facets", [])),
            pages=[CatalogPage(**p) for p in data.get("pages", [])],
            source_copies=list(data.get("source_copies", [])),
            public_docs=dict(data.get("public_docs", {})),
            live_checks=list(data.get("live_checks", [])),
            related=list(data.get("related", [])),
            local_first=bool(data.get("local_first", False)),
            page_status=data.get("page_status", "committed"),
        )

    def match_phrases(self) -> set[str]:
        """Complete names only: slug, title, declared and policy aliases.

        Canonical identities (slug, title) are always matchable. Aliases are
        dropped only when a single generic word ("model") could never justify
        a lookup; a complete phrase like "token plan" is explicit and kept.
        Filename fragments are never generated: "openai-api-tools"
        yields "openai api tools", not "tools" or "api".
        """
        phrases: set[str] = set()
        for phrase in (normalize(self.slug), normalize(self.title)):
            if phrase and (len(phrase.split()) > 1 or not _generic_only(phrase)):
                phrases.add(phrase)
        for alias in self.aliases:
            phrase = normalize(alias)
            if phrase and (len(phrase.split()) > 1 or not _generic_only(phrase)):
                phrases.add(phrase)
        return phrases


@dataclass
class Catalog:
    status: str  # ok | stale | unknown
    revision: str | None
    policy_version: int
    subjects: dict[str, SubjectEntry] = field(default_factory=dict)
    reason: str | None = None
    #: Projects too new to document: slug -> {"aliases": [...], "path": str|None}.
    early_projects: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "revision": self.revision,
            "policy_version": self.policy_version,
            "reason": self.reason,
            "subjects": {slug: s.to_dict() for slug, s in sorted(self.subjects.items())},
            "early_projects": self.early_projects,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Catalog":
        if data.get("schema_version") != SCHEMA_VERSION:
            raise CatalogUnavailable("catalog schema version mismatch")
        return cls(
            status=data.get("status", "unknown"),
            revision=data.get("revision"),
            policy_version=int(data.get("policy_version", 0)),
            reason=data.get("reason"),
            subjects={slug: SubjectEntry.from_dict(raw) for slug, raw in data.get("subjects", {}).items()},
            early_projects=dict(data.get("early_projects") or {}),
        )


def normalize(text: str) -> str:
    """Lower-case; hyphens/underscores count as spaces (worker-1 == worker 1)."""
    return re.sub(r"[-_]+", " ", text.lower()).strip()


_GENERIC_WORDS = frozenset(
    {
        "model",
        "studio",
        "plan",
        "token",
        "hub",
        "host",
        "hosts",
        "fleet",
        "page",
        "welcome",
        "my",
        "api",
        "docs",
        "cloud",
        "product",
        "index",
        "the",
        "a",
        "an",
    }
)


def _generic_only(phrase: str) -> bool:
    return all(word in _GENERIC_WORDS for word in phrase.split())


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise CatalogUnavailable(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def head_commit(brain: Path) -> str | None:
    if not brain.is_dir():
        return None
    try:
        return _git(brain, "rev-parse", "HEAD").strip()
    except CatalogUnavailable:
        return None


def _committed_pages(brain: Path, head: str, directories: tuple[str, ...]) -> list[str]:
    """Tracked Markdown paths under `directories` at `head`, committed bytes only."""
    out = _git(brain, "ls-tree", "-r", "--name-only", "-z", head)
    paths = sorted(p for p in out.split("\0") if p.endswith(".md"))
    selected = []
    for path in paths:
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] in directories and Path(path).name != "index.md":
            selected.append(path)
    return selected


def _page_authority(meta: dict[str, Any], path: str) -> str:
    status = str(meta.get("status", "")).lower()
    if "supersed" in status or "historical" in status or "archiv" in status:
        return "historical"
    if "proposal" in status or "draft" in status:
        return "proposal"
    if PROPOSAL_PATTERN.search(Path(path).stem):
        return "proposal"
    return "current"


def _load_policy(brain: Path, policy_path: Path | None) -> dict[str, Any]:
    path = policy_path or (brain / "doclayer" / "routing-policy.yaml")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CatalogUnavailable(f"routing policy unreadable: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CatalogUnavailable(f"routing policy is not a mapping: {path}")
    return data


def _policy_version(policy: dict[str, Any]) -> int:
    try:
        return int(policy.get("version", 1))
    except (TypeError, ValueError):
        return 1


def _subject_slug(page_path: str, meta: dict[str, Any]) -> str | None:
    """A page joins the subject named by ``product``, or by its own stem when
    it carries an explicit subject type/tag. Plain pages do not mint subjects;
    the policy or a later product declaration tracks them."""
    product = meta.get("product")
    if isinstance(product, str) and product.strip():
        return product.strip()
    tags = meta.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    subject_type = str(meta.get("subject_type", "")).strip()
    if subject_type or "product" in tags or "account" in tags or "service" in tags:
        return Path(page_path).stem
    return None


def _kind_for(meta: dict[str, Any], fallback: str = "product") -> str:
    declared = str(meta.get("subject_type", "")).strip()
    if declared:
        return declared
    tags = meta.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    for tag, kind in (
        ("account", "account"),
        ("service", "service"),
        ("product", "product"),
    ):
        if tag in tags:
            return kind
    return fallback


def _declared_aliases(meta: dict[str, Any]) -> list[str]:
    declared = meta.get("aliases") or []
    if isinstance(declared, str):
        declared = [declared]
    if not isinstance(declared, list):
        return []
    return [str(a) for a in declared if str(a).strip()]


def build_catalog(
    brain: Path,
    policy_path: Path | None = None,
    *,
    head: str | None = None,
) -> Catalog:
    """Build the catalog from committed bytes plus the committed policy."""
    head = head or head_commit(brain)
    if not head:
        raise CatalogUnavailable(f"brain reference has no HEAD: {brain}")
    policy = _load_policy(brain, policy_path)
    version = _policy_version(policy)
    exclusions = {str(e) for e in policy.get("exclusions", []) or []}
    local_first = {str(s) for s in policy.get("local_first", []) or []}
    policy_subjects = policy.get("subjects", {}) or {}
    if not isinstance(policy_subjects, dict):
        raise CatalogUnavailable("routing policy subjects must be a mapping")
    hosts = policy.get("hosts", {}) or {}

    subjects: dict[str, SubjectEntry] = {}

    def entry(slug: str, kind: str, title: str) -> SubjectEntry:
        return subjects.setdefault(slug, SubjectEntry(slug=slug, kind=kind, title=title or slug))

    # Canonical pages under wiki/ and infra/ mint subjects.
    pages_by_stem: dict[str, list[CatalogPage]] = {}
    for path in _committed_pages(brain, head, SUBJECT_DIRS):
        stem = Path(path).stem
        if stem in exclusions:
            continue
        try:
            meta, _ = frontmatter.parse(_git(brain, "show", f"{head}:{path}"))
        except Exception:  # noqa: BLE001 — an odd page keeps its filename match
            meta = {}
        page = CatalogPage(
            path=path,
            concept_id=meta.get("concept_id") if isinstance(meta.get("concept_id"), str) else None,
            authority=_page_authority(meta, path),
        )
        pages_by_stem.setdefault(stem, []).append(page)
        slug = _subject_slug(path, meta)
        if slug is None or slug in exclusions:
            continue
        subject = entry(slug, _kind_for(meta), str(meta.get("title") or slug))
        subject.pages.append(page)
        for alias in _declared_aliases(meta):
            if alias not in subject.aliases:
                subject.aliases.append(alias)

    # Policy relationships: aliases, facets, sources, live checks. Policy
    # aliases join an existing subject; a policy subject with no committed
    # page stays listed with page_status="policy" (never fabricated coverage).
    for slug, raw in policy_subjects.items():
        slug = str(slug)
        if not isinstance(raw, dict):
            continue
        subject = subjects.get(slug)
        if subject is None:
            subject = entry(slug, str(raw.get("kind", "service")), str(raw.get("title") or slug))
            # Legacy pages without adequate metadata get conservative
            # complete-name matching: a policy subject binds a committed page
            # only when the page stem equals the slug exactly.
            subject.pages = list(pages_by_stem.get(slug, []))
            subject.page_status = "committed" if subject.pages else "policy"
        else:
            subject.kind = str(raw.get("kind", subject.kind))
        for alias in raw.get("aliases", []) or []:
            alias = str(alias)
            if alias and alias not in subject.aliases:
                subject.aliases.append(alias)
        subject.facets = [str(f) for f in raw.get("facets", []) or []]
        subject.related = [str(r) for r in raw.get("related", []) or []]
        subject.live_checks = [str(c) for c in raw.get("live_checks", []) or []]
        docs = raw.get("public_docs") or {}
        if isinstance(docs, dict):
            subject.public_docs = {str(k): str(v) for k, v in docs.items()}

    # Committed source copies count only when usable material exists: an
    # empty folder is not coverage.
    try:
        tree = _git(brain, "ls-tree", "-r", "--name-only", "-z", head)
        committed = [p for p in tree.split("\0") if p.startswith("sources/") and p.endswith(".md")]
    except CatalogUnavailable:
        committed = []
    copies: dict[str, list[str]] = {}
    for path in committed:
        parts = path.split("/")
        if len(parts) >= 3 and parts[2] != "index.md":
            copies.setdefault(parts[1], []).append(path)
    for slug, paths in copies.items():
        subject = subjects.get(slug)
        if subject is None:
            subject = entry(slug, "product", slug)
            subject.page_status = "policy"
        subject.source_copies = paths

    # Policy-declared known hosts: known, never proof of documentation.
    for name in hosts:
        host = entry(str(name), "host", str(name))
        host.page_status = "policy"
        host.local_first = True

    for slug in local_first:
        if slug in subjects:
            subjects[slug].local_first = True

    return Catalog(
        status="ok",
        revision=head,
        policy_version=version,
        subjects=subjects,
        early_projects=_early_projects(policy),
    )


def _cache_key(brain: Path, head: str, policy_path: Path | None) -> tuple[str, str]:
    path = policy_path or (brain / "doclayer" / "routing-policy.yaml")
    try:
        stamp = path.read_bytes()
    except OSError:
        stamp = b""
    digest = hashlib.sha256(stamp + head.encode()).hexdigest()[:16]
    return f"catalog-{head[:12]}-{digest}.json", f"catalog-latest-{digest[:8]}.json"


def load_catalog(
    brain: Path,
    cache_dir: Path,
    policy_path: Path | None = None,
    *,
    head: str | None = None,
) -> Catalog:
    """Load the cached catalog for the reference HEAD, rebuilding at most once.

    Missing or corrupt cache triggers one rebuild; a failed rebuild yields
    ``coverage_unknown`` (or an explicitly stale prior snapshot), never an
    empty catalog. The prompt-critical path only ever reads cached JSON —
    rebuilds are atomic and happen outside it after the first miss.
    """
    head = head or head_commit(brain)
    if not head:
        return Catalog(
            status="unknown",
            revision=None,
            policy_version=0,
            reason=f"brain reference unavailable or has no HEAD: {brain}",
        )
    cache_name, latest_name = _cache_key(brain, head, policy_path)
    cache_path = cache_dir / cache_name
    try:
        return Catalog.from_dict(json.loads(cache_path.read_text(encoding="utf-8")))
    except (CatalogUnavailable, OSError, ValueError):
        pass  # corrupt or schema-mismatched cache: force one rebuild
    try:
        catalog = build_catalog(brain, policy_path, head=head)
    except CatalogUnavailable as exc:
        # Keep an explicitly stale prior snapshot if one is usable.
        for stale in sorted(cache_dir.glob("catalog-latest-*.json")):
            try:
                prior = Catalog.from_dict(json.loads(stale.read_text(encoding="utf-8")))
                prior.status = "stale"
                prior.reason = f"catalog rebuilt from stale snapshot: {exc}"
                return prior
            except (OSError, ValueError, CatalogUnavailable):
                continue
        return Catalog(status="unknown", revision=head, policy_version=0, reason=str(exc))
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(catalog.to_dict())
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(cache_path)
        (cache_dir / latest_name).write_text(payload, encoding="utf-8")
    except OSError:
        pass
    return catalog


# --------------------------------------------------------------------------- #
# SSH host aliases: cached separately, Include files handled deliberately
# --------------------------------------------------------------------------- #


def ssh_host_groups(ssh_config: Path, *, _depth: int = 0) -> list[frozenset[str]]:
    """Per ssh `Host` block with a `HostName`: its aliases plus that address.

    Aliases that share a HostName (for example `svc1` and `services`) are the same
    machine, so the blocks' groups are merged by the caller. Wildcard and
    negated patterns are skipped; a block without HostName names no address.
    """
    groups: list[frozenset[str]] = []
    seen: list[Path] = []

    def parse(path: Path, depth: int) -> None:
        if depth > 3 or path in seen:
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        seen.append(path)
        names: list[str] = []
        hostname = None

        def close() -> None:
            if names and hostname:
                groups.append(frozenset([*names, hostname]))

        for line in lines:
            parts = line.strip().split()
            if not parts or parts[0].startswith("#"):
                continue
            keyword = parts[0].lower()
            if keyword == "include" and len(parts) > 1:
                for pattern in parts[1:]:
                    expanded = Path(pattern).expanduser()
                    if not expanded.is_absolute():
                        expanded = path.parent / expanded
                    for included in sorted(_globmod.glob(str(expanded))):
                        parse(Path(included), depth + 1)
            elif keyword == "host":
                close()
                names = [a for a in parts[1:] if not any(c in a for c in "*?!")]
                hostname = None
            elif keyword == "hostname" and len(parts) > 1:
                hostname = parts[1]
        close()

    parse(Path(ssh_config).expanduser(), 0)
    return groups


_FLEET_ADDRESS = re.compile(r"\*\*([^*\n]+)\*\*\s*[\u2014-]+\s*`(\d{1,3}(?:\.\d{1,3}){3})`")


def fleet_host_addresses(text: str) -> list[tuple[str, str]]:
    """(host name, IP) pairs from the fleet-hosts page's `**Name** — `ip`` lines."""
    return [(m[1].strip(), m[2]) for m in _FLEET_ADDRESS.finditer(text)]


def fleet_host_blocks(text: str) -> list[tuple[str, str]]:
    """(host name, its text) per `- **Host** ...` bullet or heading of the fleet page.

    A block runs to the next bullet or heading; what the page says there is what
    it records about that host, including the services running on it.
    """
    starts = list(re.finditer(r"^\s*(?:[-*]\s+\*\*([^*\n]+)\*\*|#{2,6}\s+(.+?)\s*$)", text, re.M))
    blocks = []
    for index, match in enumerate(starts):
        name = (match.group(1) or match.group(2) or "").strip()
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        blocks.append((name, text[match.end() : end]))
    return blocks


def ssh_host_aliases(ssh_config: Path, cache_dir: Path | None = None, *, _depth: int = 0) -> list[str]:
    """Host alias tokens from the ssh config, including Include files.

    Wildcards, addresses and qualified names never count (they are not
    aliases). Aliases are known hosts — not proof that documentation exists.
    """
    aliases: list[str] = []
    seen_files: list[Path] = []

    def parse(path: Path, depth: int) -> None:
        if depth > 3 or path in seen_files:
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        seen_files.append(path)
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if parts[0].lower() == "include" and len(parts) > 1:
                for pattern in parts[1:]:
                    expanded = Path(pattern).expanduser()
                    if not expanded.is_absolute():
                        expanded = path.parent / expanded
                    if any(c in str(expanded) for c in "*?"):
                        matches = sorted(_globmod.glob(str(expanded)))
                    else:
                        matches = [str(expanded)]
                    for included in matches:
                        parse(Path(included), depth + 1)
                continue
            if len(parts) < 2 or parts[0].lower() != "host":
                continue
            for alias in parts[1:]:
                if any(c in alias for c in "*?!") or _IP.match(alias) or "." in alias:
                    continue
                normalized = normalize(alias)
                if normalized and normalized not in aliases:
                    aliases.append(normalized)

    parse(Path(ssh_config).expanduser(), 0)
    if cache_dir is not None:
        try:
            stamp = hashlib.sha256(
                (
                    "\0".join(str(p) for p in seen_files)
                    + "\0"
                    + "\0".join(str(int(p.stat().st_mtime)) for p in seen_files)
                    + "\0"
                    + "\0".join(aliases)
                ).encode()
            ).hexdigest()[:16]
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / f"ssh-hosts-{stamp}.json").write_text(json.dumps(aliases), encoding="utf-8")
            for old in cache_dir.glob("ssh-hosts-*.json"):
                if old.name != f"ssh-hosts-{stamp}.json":
                    old.unlink()
        except OSError:
            pass
    return aliases


def _early_projects(policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Policy-listed projects too new to have documentation."""
    raw = policy.get("early_projects", {}) or {}
    if not isinstance(raw, dict):
        raise CatalogUnavailable("routing policy early_projects must be a mapping")
    projects = {}
    for slug, spec in raw.items():
        spec = spec or {}
        aliases = [str(a) for a in spec.get("aliases", []) or []]
        path = spec.get("path")
        projects[str(slug)] = {"aliases": aliases, "path": str(path) if path else None}
    return projects


def early_project(catalog: Catalog, question: str) -> tuple[str, dict[str, Any]] | None:
    """The listed early-stage project a question names by name, alias or folder."""
    text = " " + normalize(question) + " "
    for slug, project in sorted(catalog.early_projects.items()):
        path = project.get("path")
        if path and path.rstrip("/") in question:
            return slug, project
        for name in (slug, *project.get("aliases", [])):
            if re.search(r"(?<![a-z0-9])" + re.escape(normalize(name)) + r"(?![a-z0-9])", text):
                return slug, project
    return None
