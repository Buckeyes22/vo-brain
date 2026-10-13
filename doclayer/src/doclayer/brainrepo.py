"""Git-backed access to the brain: exact facts come from pages, not vectors."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from doclayer.resolver_context import noncode_lines

_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n", re.DOTALL)

#: Repository files and trees that document or build the code, not the vault.
REPO_META_FILES = frozenset({"README.md", "CLAUDE.md", "LICENSE"})
REPO_META_DIRS = ("docs/", "doclayer/", ".github/")


def is_brain_page(path: str) -> bool:
    """True unless `path` (repo-relative POSIX) is repository meta, never a brain page."""
    return path not in REPO_META_FILES and not path.startswith(REPO_META_DIRS)


@dataclass
class BrainPage:
    """One brain page read from the working tree (Git is the source of truth)."""

    path: str  # repo-relative POSIX path
    meta: dict[str, Any]
    body: str
    commit: str

    @property
    def title(self) -> str:
        title = self.meta.get("title")
        return title if isinstance(title, str) else Path(self.path).stem

    @property
    def concept_id(self) -> str | None:
        value = self.meta.get("concept_id")
        return value if isinstance(value, str) else None

    @property
    def ref(self) -> str:
        """The path@commit tag every fact from this page carries."""
        suffix = self.commit[:8] if self.commit else "working-tree"
        return f"{self.path}@{suffix}"


@dataclass
class FactRow:
    """One row of a page's Facts table (declared vs. observed)."""

    fact: str
    declared: str
    observed: str
    page: BrainPage
    extras: dict[str, str] = field(default_factory=dict)
    source_line: int | None = None


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


class BrainRepo:
    """Reads brain pages from a brain repo working copy."""

    def __init__(self, root: Path | str, *, snapshot: bool = False) -> None:
        self.root = Path(root)
        self._commit = _git(self.root, "rev-parse", "HEAD").strip() if snapshot else None
        self._texts: dict[str, str] = {}
        self._paths: list[str] | None = None

    def head_commit(self) -> str:
        """HEAD of the working copy, or "" when Git is unavailable (rare; the
        page tags then say `working-tree` instead of lying about a commit)."""
        if self._commit is not None:
            return self._commit
        try:
            return _git(self.root, "rev-parse", "HEAD").strip()
        except RuntimeError:
            return ""

    def branch(self) -> str:
        return _git(self.root, "rev-parse", "--abbrev-ref", "HEAD").strip()

    def markdown_paths(self) -> list[str]:
        """Every tracked brain page (.md), excluding repository meta (see is_brain_page)."""
        if self._commit is not None and self._paths is not None:
            return list(self._paths)
        if self._commit is not None:
            out = _git(self.root, "ls-tree", "-r", "--name-only", "-z", self._commit)
        else:
            out = _git(self.root, "ls-files", "-z", "*.md")
        paths = sorted(p for p in out.split("\0") if p and p.endswith(".md") and is_brain_page(p))
        if self._commit is not None:
            self._paths = paths
        return list(paths)

    def has_path(self, path: str) -> bool:
        if self._commit is None:
            return (self.root / path).exists()
        try:
            _git(self.root, "cat-file", "-e", f"{self._commit}:{path}")
            return True
        except RuntimeError:
            return False

    def source_paths(self, subject: str) -> list[str]:
        if self._commit is not None:
            return [path for path in self.markdown_paths() if Path(path).parent == Path("sources") / subject]
        return [
            path.relative_to(self.root).as_posix() for path in sorted((self.root / "sources" / subject).glob("*.md"))
        ]

    def read_text(self, path: str) -> str:
        if self._commit is None:
            return (self.root / path).read_text(encoding="utf-8")
        if path not in self._texts:
            self._texts[path] = _git(self.root, "show", f"{self._commit}:{path}")
        return self._texts[path]

    def read_page(self, path: str) -> BrainPage:
        text = self.read_text(path)
        match = _FRONTMATTER.match(text)
        meta: dict[str, Any] = {}
        body = text
        if match is not None:
            try:
                loaded = yaml.safe_load(match.group(1)) or {}
                if isinstance(loaded, dict):
                    meta = loaded
                    body = text[match.end() :]
            except yaml.YAMLError:
                pass
        return BrainPage(path=path, meta=meta, body=body, commit=self.head_commit())

    def find_page(self, needle: str) -> BrainPage | None:
        """Find a page by repo path, page stem, or concept_id."""
        needle = needle.strip().strip("[]")
        for path in self.markdown_paths():
            stem = Path(path).stem
            if needle in (path, stem, f"{Path(path).parent / stem}"):
                return self.read_page(path)
        for path in self.markdown_paths():
            page = self.read_page(path)
            if page.concept_id == needle:
                return page
        return None

    def pages_for_product(self, product: str) -> list[BrainPage]:
        """Pages whose title, path, or tags mention the product name.

        Matching is plain word overlap on the product slug: "alibaba-model-studio"
        matches pages that say "alibaba", "model", "studio" or carry the tag.
        """
        words = [w for w in re.split(r"[-_\s]+", product.lower()) if len(w) > 2]
        hits: list[BrainPage] = []
        for path in self.markdown_paths():
            page = self.read_page(path)
            haystack = " ".join(
                [
                    path.lower(),
                    page.title.lower(),
                    " ".join(str(t).lower() for t in page.meta.get("tags", []) or []),
                ]
            )
            if words and all(word in haystack for word in words):
                hits.append(page)
        return hits


_FACTS_TABLE_ROW = re.compile(r"^\|(.+)\|\s*$")
_HEADING = re.compile(r"^#{1,6} (.+?)\s*#*\s*$")


def fact_rows(page: BrainPage) -> list[FactRow]:
    """Parse the page's Facts tables (Fact · Declared · Observed) into rows.

    Only tables under a heading named "Facts" count — a doc copy's pricing
    tables or the architecture's piece table are not declared/observed facts.
    """
    rows: list[FactRow] = []
    in_facts = False
    for source_line, line in noncode_lines(page.body, numbered=True):
        heading = _HEADING.match(line)
        if heading is not None:
            in_facts = heading.group(1).strip().lower() == "facts"
            continue
        if not in_facts:
            continue
        table = _FACTS_TABLE_ROW.match(line)
        if table is None:
            continue
        cells = [c.strip() for c in table.group(1).split("|")]
        if len(cells) == 3 and cells[0] not in ("Fact", "---", ":---", ""):
            rows.append(
                FactRow(
                    fact=cells[0],
                    declared=cells[1],
                    observed=cells[2],
                    page=page,
                    source_line=source_line,
                )
            )
        elif len(cells) > 3 and cells[0] not in ("Fact",):
            rows.append(
                FactRow(
                    fact=cells[0],
                    declared=cells[1],
                    observed=cells[-1],
                    page=page,
                    source_line=source_line,
                    extras={f"col{i}": c.strip() for i, c in enumerate(cells[2:-1], start=2)},
                )
            )
    return rows


class UnavailableBrainRepo(BrainRepo):
    """Empty source adapter used after a reference configuration error."""

    def __init__(self) -> None:
        self.root = Path("/doclayer-unavailable-canonical-reference")
        self._commit = None

    def head_commit(self) -> str:
        return ""

    def markdown_paths(self) -> list[str]:
        return []

    def source_paths(self, subject: str) -> list[str]:
        return []

    def has_path(self, path: str) -> bool:
        return False
