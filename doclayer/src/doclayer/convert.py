"""HTML to Markdown conversion for fetched doc pages.

Finds the page's main content node (the Alibaba Cloud help portal renders the
article inside `div.icms-help-docs-content`; other sites fall back to
`article`/`main`/body) and converts it with markdownify. Scripts, styles, and
navigation chrome are dropped before conversion.
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

_CONTENT_SELECTORS = [
    ("div", {"class": "icms-help-docs-content"}),
    ("article", {}),
    ("main", {}),
]

_DROP_TAGS = ("script", "style", "noscript", "nav", "form", "iframe")


def _main_content(soup: BeautifulSoup):
    for tag, attrs in _CONTENT_SELECTORS:
        node = soup.find(tag, attrs or None)
        if node is not None:
            return node
    return soup.body or soup


def html_to_markdown(html: str) -> str:
    """Convert a fetched HTML page to Markdown, keeping only real content."""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(_DROP_TAGS):
        node.decompose()
    content = _main_content(soup)
    converter = MarkdownConverter(heading_style="ATX", bullets="-")
    markdown = converter.convert_soup(content)
    # Collapse runs of blank lines left by dropped chrome.
    lines = [line.rstrip() for line in markdown.splitlines()]
    out: list[str] = []
    for line in lines:
        if line == "" and (not out or out[-1] == ""):
            continue
        out.append(line)
    return "\n".join(out).strip() + "\n"


def page_title(html: str) -> str | None:
    """The <title> of the page, or None."""
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        return " ".join(soup.title.string.split())
    return None
