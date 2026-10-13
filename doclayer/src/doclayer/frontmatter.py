"""Split and build the brain's YAML frontmatter.

Doc copies in `sources/<product>/` carry the brain's standard frontmatter
(templates/page.md) plus `url`, `fetched_at`, `product`, and `version`. The
body follows the closing `---`. Parsing is deliberately naive: everything
between the first `---` line pair is YAML, the rest is the body.
"""

from __future__ import annotations

from typing import Any

import yaml

DELIM = "---"


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter dict, body) for a page. Missing frontmatter -> ({}, text)."""
    if not text.startswith(DELIM):
        return {}, text
    lines = text.splitlines(keepends=True)
    try:
        end = lines.index(DELIM + "\n", 1)
    except ValueError:
        return {}, text
    meta = yaml.safe_load("".join(lines[1:end])) or {}
    if not isinstance(meta, dict):
        return {}, text
    return meta, "".join(lines[end + 1 :]).lstrip("\n")


def build(meta: dict[str, Any], body: str) -> str:
    """Render frontmatter + body as one page string."""
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=100).strip()
    return f"{DELIM}\n{front}\n{DELIM}\n\n{body}"
