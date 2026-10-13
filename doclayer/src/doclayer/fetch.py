"""The doc fetcher: refresh `sources/<product>/` from a per-product recipe.

Usage: `uv run doclayer-fetch [recipe_path]` (default: doclayer/recipes/alibaba-model-studio.yaml
in the brain repo).

For every page in the recipe it fetches the URL, classifies the result, and
only writes good copies (an HTTP error, a soft-404, a login page, or an empty
article is never stored; the previous good copy is kept). On failure it
records `last_attempt` and `last_error` on the recipe so answers can say the
copy is stale. The file also carries `url` and `fetched_at` in frontmatter;
Git history is the record of every previous copy.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from doclayer import frontmatter, quality
from doclayer.convert import html_to_markdown, page_title

DEFAULT_TIMEOUT_S = 30.0
ATTEMPTS_PER_PAGE = 2

USER_AGENT = "doclayer/0.1 (+https://github.com/Buckeyes22/vo-brain)"


def load_recipe(path: Path) -> dict[str, Any]:
    recipe = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(recipe, dict) or "product" not in recipe or "pages" not in recipe:
        raise ValueError(f"{path}: recipe must be a mapping with 'product' and 'pages'")
    return recipe


def repo_root_for(recipe_path: Path) -> Path:
    """The brain repo root: a recipe lives at <root>/doclayer/recipes/<name>.yaml."""
    root = recipe_path.resolve().parents[2]
    if not (root / "doclayer").is_dir():
        raise ValueError(f"{recipe_path} does not look like <brain-repo>/doclayer/recipes/*.yaml")
    return root


def _copy_page(slug: str, url: str, html: str, product: str, version: str, fetched_at: str) -> str:
    title = page_title(html) or slug
    body = f"# {title}\n\n{html_to_markdown(html)}"
    meta = {
        "schema_version": 1,
        "privacy": "internal",
        "source_origin": "external",
        "source_lineage": [f"url:{url}"],
        "tags": ["doclayer", "source", product],
        "updated": fetched_at[:10],
        "title": slug,
        "url": url,
        "fetched_at": fetched_at,
        "product": product,
        "version": version,
    }
    return frontmatter.build(meta, body)


async def _fetch(client: httpx.AsyncClient, url: str) -> tuple[int, str]:
    last_exc: Exception | None = None
    for attempt in range(ATTEMPTS_PER_PAGE):
        try:
            response = await client.get(url)
            return response.status_code, response.text
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < ATTEMPTS_PER_PAGE - 1:
                await asyncio.sleep(1.0 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


async def refresh(
    recipe_path: Path,
    dest: Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, str]]:
    """Fetch every recipe page. Returns one report row per page; updates the recipe."""
    recipe = load_recipe(recipe_path)
    product = recipe["product"]
    version = str(recipe.get("docs", {}).get("version", "current"))
    dest = dest or (repo_root_for(recipe_path) / "sources" / product)
    dest.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, str]] = []
    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_S,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
        transport=transport,
    ) as client:
        for page in recipe["pages"]:
            slug, url = page["slug"], page["url"]
            try:
                status, html = await _fetch(client, url)
                good, reason = quality.classify(status, html, html_to_markdown(html))
            except httpx.HTTPError as exc:
                good, reason = False, f"error:{type(exc).__name__}"
                status, html = 0, ""
            if good:
                fetched_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                text = _copy_page(slug, url, html, product, version, fetched_at)
                (dest / f"{slug}.md").write_text(text, encoding="utf-8")
            rows.append({"slug": slug, "url": url, "status": str(status), "result": reason})

    failures = [f"{row['slug']}: {row['result']}" for row in rows if row["result"] != "ok"]
    recipe["last_attempt"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    recipe["last_error"] = "; ".join(failures) if failures else None
    recipe_path.write_text(
        yaml.safe_dump(recipe, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )
    return rows


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    land = "--land" in args
    args = [a for a in args if a != "--land"]
    recipe_path = (
        Path(args[0]) if args else Path(__file__).resolve().parents[2] / "recipes" / ("alibaba-model-studio.yaml")
    )
    dest = Path(args[1]) if len(args) > 1 else None
    rows = asyncio.run(refresh(recipe_path, dest))
    for row in rows:
        print(f"{row['slug']}: HTTP {row['status']} {row['result']}")
    ok = all(row["result"] == "ok" for row in rows)
    print(f"{sum(r['result'] == 'ok' for r in rows)}/{len(rows)} pages stored")
    if land:
        from doclayer.landing import land_refresh

        result = land_refresh(recipe_path, rows)
        print(
            f"landing: {result.action}"
            + (f" PR #{result.pr_number} {result.pr_url}" if result.pr_number else "")
            + (f" ({'; '.join(result.reasons)})" if result.reasons else "")
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
