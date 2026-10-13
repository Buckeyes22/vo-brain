"""Tests for frontmatter split/build."""

from doclayer.frontmatter import build, parse


def test_round_trip():
    meta = {
        "schema_version": 1,
        "privacy": "internal",
        "url": "https://example.test/a",
        "n": 3,
    }
    page = build(meta, "# Hello\n\nBody text.\n")
    parsed_meta, body = parse(page)
    assert parsed_meta == meta
    assert body.startswith("# Hello")


def test_parse_page_without_frontmatter():
    meta, body = parse("just a body\n")
    assert meta == {} and body == "just a body\n"
