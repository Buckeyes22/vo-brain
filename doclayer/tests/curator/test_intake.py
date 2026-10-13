"""Intake: URL identity, links, junk, redirects, byte caps, PDF locators,
manifests, directories, exclusions."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from conftest import make_pdf

from doclayer.curator import intake
from doclayer.curator.intake import (
    FetchFailure,
    classify_page,
    extract_links,
    group_sections,
    make_url_source,
    matches_exclusions,
    normalize_url,
    section_map,
)


def page_body(text: str) -> str:
    return (
        "<html><head><title>Doc</title></head><body><article>"
        f"<h1>Doc</h1><p>{text * 30}</p>"
        '<p><a href="/related/model-card">model card</a></p>'
        '<p><a href="https://other.test/guide">a guide</a></p>'
        '<p><a href="https://social.test/share?x=1">share</a></p>'
        '<p><a href="#section">in-page</a></p>'
        "</article></body></html>"
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        headers={"User-Agent": "test"},
    )


# -- URL identity ----------------------------------------------------------- #


def test_normalize_url_strips_fragment_keeps_query() -> None:
    assert normalize_url("https://docs.test/guide?version=2#section") == "https://docs.test/guide?version=2"
    assert normalize_url("rel.pdf", base="https://docs.test/a/b") == "https://docs.test/a/rel.pdf"
    assert normalize_url("/rel", base="https://docs.test/a/b") == "https://docs.test/rel"


def test_normalize_url_rejects_non_http() -> None:
    with pytest.raises(intake.IntakeError):
        normalize_url("javascript:alert(1)")
    with pytest.raises(intake.IntakeError):
        normalize_url("file:///etc/passwd")


def test_url_identity_dedup() -> None:
    a = make_url_source("https://docs.test/x#intro")
    b = make_url_source("https://docs.test/x")
    assert a.id == b.id  # fragment is not identity
    c = make_url_source("https://docs.test/x?version=3")
    assert c.id != a.id  # version query is meaningful identity


# -- link extraction -------------------------------------------------------- #


def test_extract_links_resolves_relative_and_drops_noise() -> None:
    markdown = (
        "See the [model card](/related/model-card) and [guide](https://other.test/guide). "
        "Share at https://social.test/share?x=1 or [in-page](#section). "
        "Bare: https://plain.test/path"
    )
    links = extract_links(markdown, "https://docs.test/current/page")
    urls = [u for u, _ in links]
    assert "https://docs.test/related/model-card" in urls
    assert "https://other.test/guide" in urls
    assert "https://plain.test/path" in urls
    assert not any("social.test" in u for u in urls)
    assert not any(u.startswith("#") for u in urls)


def test_extract_links_dedupes_cycles() -> None:
    markdown = "[a](https://x.test/a) [b](https://x.test/a) [c](https://x.test/a)"
    assert len(extract_links(markdown)) == 1


# -- sections and chunking ---------------------------------------------------- #


def test_section_map_names_sections() -> None:
    text = "intro text\n## Alpha\nalpha body\n## Beta\nbeta body"
    sections = section_map(text)
    assert sections[0][0] == "(introduction)"
    assert sections[1][0] == "#Alpha"
    assert sections[2][0] == "#Beta"


def test_group_sections_respects_budget_and_splits_oversized_sections() -> None:
    sections = [("a", "x" * 50), ("b", "y" * 50), ("c", "z" * 120)]
    groups = group_sections(sections, 100)
    assert [len(g) for g in groups] == [2, 1, 1]
    assert all(sum(len(text) for _, text in group) <= 100 for group in groups)
    assert "".join(text for group in groups[1:] for _, text in group) == "z" * 120
    assert groups[2][0][0] == "c (part 2)"


def test_model_endpoint_support_survives_html_conversion():
    from doclayer.convert import html_to_markdown
    from doclayer.curator.intake import preserve_model_support_flags

    html = """<main><div><div>Endpoints</div><div>
      <div class="flex flex-row gap-2"><div class="bg-primary-soft"><svg></svg></div><div>
      <div class="text-sm font-semibold">Responses</div><div class="text-xs">v1/responses</div></div></div>
      <div class="flex flex-row gap-2"><div><svg><mask id="maskSubtract"></mask></svg></div><div>
      <div class="text-sm font-semibold text-gray-400">Realtime</div><div class="text-xs">v1/realtime</div></div></div>
      <div class="flex flex-row gap-2"><div class="text-sm font-semibold">Unknown</div><div class="text-xs">v1/unknown</div></div>
    </div></div></main>"""
    url = "https://developers.openai.com/api/docs/models/fixture"
    text = html_to_markdown(preserve_model_support_flags(html, url))
    assert "v1/responses — supported by this model" in text
    assert "v1/realtime — not supported by this model" in text
    assert "v1/unknown — model support unspecified" in text
    assert preserve_model_support_flags(html, "https://other.test/model") == html


# -- junk classification ------------------------------------------------------ #


def test_classify_page_junk_paths() -> None:
    ok, reason = classify_page(200, "x" * 500)
    assert ok and reason == "ok"
    ok, reason = classify_page(404, "x" * 500)
    assert not ok and reason == "junk:http_404"
    ok, reason = classify_page(200, "too short")
    assert not ok and reason == "junk:too_short"
    ok, reason = classify_page(200, "Please Sign in to continue " + "x" * 500)
    assert not ok and reason == "junk:login_page"


# -- fetching ------------------------------------------------------------------ #


def _respond(entries: dict[str, object]):
    def handler(request: httpx.Request) -> httpx.Response:
        entry = entries[str(request.url)]
        if isinstance(entry, httpx.Response):
            return entry
        if isinstance(entry, tuple):
            status, body = entry
            if isinstance(body, bytes):
                return httpx.Response(status, content=body)
            return httpx.Response(status, text=body)
        raise TypeError(entry)

    return handler


@pytest.mark.asyncio
async def test_fetch_follows_validated_redirects() -> None:
    entries = {
        "https://a.test/start": httpx.Response(302, headers={"location": "https://b.test/final"}),
        "https://b.test/final": (200, page_body("real content ")),
    }
    async with _client(_respond(entries)) as client:
        page = await intake.fetch_url(client, "https://a.test/start", max_bytes=1024 * 1024)
    assert page.final_url == "https://b.test/final"
    assert "model card" in page.markdown


@pytest.mark.asyncio
async def test_pdf_url_extracts_page_locators_without_html_conversion(tmp_path) -> None:
    pdf_fixture = make_pdf(tmp_path / "card.pdf", ["Test PDF source text"])
    async with _client(
        lambda request: httpx.Response(
            200, content=pdf_fixture.read_bytes(), headers={"content-type": "application/pdf"}
        )
    ) as client:
        page = await intake.fetch_url(client, "https://a.test/system.pdf", max_bytes=1024 * 1024)
    assert "## Page 1" in page.markdown
    assert "Test PDF" in page.markdown
    assert page.bytes_read == pdf_fixture.stat().st_size


@pytest.mark.asyncio
async def test_fetch_blocks_https_to_http_downgrade() -> None:
    entries = {
        "https://a.test/x": httpx.Response(301, headers={"location": "http://b.test/y"}),
    }
    async with _client(_respond(entries)) as client:
        with pytest.raises(FetchFailure) as excinfo:
            await intake.fetch_url(client, "https://a.test/x", max_bytes=1000)
    assert "downgrade" in excinfo.value.reason


@pytest.mark.asyncio
async def test_fetch_blocks_non_http_scheme_redirect() -> None:
    entries = {
        "https://a.test/x": httpx.Response(302, headers={"location": "ftp://b.test/y"}),
    }
    async with _client(_respond(entries)) as client:
        with pytest.raises(FetchFailure):
            await intake.fetch_url(client, "https://a.test/x", max_bytes=1000)


@pytest.mark.asyncio
async def test_fetch_enforces_streamed_byte_cap() -> None:
    big = b"x" * (700 * 1024)
    entries = {"https://a.test/big": (200, big)}
    async with _client(_respond(entries)) as client:
        with pytest.raises(FetchFailure) as excinfo:
            await intake.fetch_url(client, "https://a.test/big", max_bytes=64 * 1024)
    assert "exceeded" in excinfo.value.reason
    assert excinfo.value.transient is False


@pytest.mark.asyncio
async def test_fetch_marks_5xx_transient_and_4xx_permanent() -> None:
    entries = {"https://a.test/down": (503, "busy"), "https://a.test/gone": (410, "gone")}
    async with _client(_respond(entries)) as client:
        with pytest.raises(FetchFailure) as e503:
            await intake.fetch_url(client, "https://a.test/down", max_bytes=1000)
        with pytest.raises(FetchFailure) as e410:
            await intake.fetch_url(client, "https://a.test/gone", max_bytes=1000)
    assert e503.value.transient is True
    assert e410.value.transient is False


# -- PDF extraction --------------------------------------------------------- #


def test_pdf_extraction_produces_page_locators(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "card.pdf", ["Context window facts", "Safety evaluation results"])
    text = intake.pdf_to_markdown(pdf)
    assert "## Page 1" in text
    assert "## Page 2" in text
    assert "Context window facts" in text
    sections = section_map(text)
    locators = [loc for loc, _ in sections]
    assert any(loc.endswith(" / Page 1") for loc in locators)
    assert any(loc.endswith(" / Page 2") for loc in locators)
    pages = intake.extract_pdf_pages(pdf)
    assert pages[0][0] == 1 and "Context window" in pages[0][1]


# -- manifests and directories ------------------------------------------------- #


def test_manifest_parsing_roles_and_exclusions(tmp_path: Path) -> None:
    manifest = tmp_path / "batch.yaml"
    manifest.write_text(
        "topic: build model knowledge\n"
        "exclusions:\n  - Bedrock usage documentation\n"
        "sources:\n"
        "  - url: https://docs.test/model\n"
        "  - url: https://aws.test/bedrock-model-card\n"
        "    role: supplementary\n"
        "  - text: pasted note\n"
        "    label: operator-note\n"
    )
    topic, exclusions, sources = intake.build_sources_from_manifest(intake.load_manifest(manifest))
    assert topic == "build model knowledge"
    assert exclusions == ["Bedrock usage documentation"]
    roles = [s.role for s in sources]
    assert roles == ["primary", "supplementary", "primary"]
    assert sources[2].kind == "text"


def test_manifest_rejects_bad_role(tmp_path: Path) -> None:
    manifest = tmp_path / "bad.yaml"
    manifest.write_text("topic: t\nsources:\n  - url: https://x.test/\n    role: chatty\n")
    with pytest.raises(intake.IntakeError):
        intake.build_sources_from_manifest(intake.load_manifest(manifest))


def test_directory_enumeration_skips_symlinks(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("a")
    (tmp_path / "docs" / "b.txt").write_text("b")
    (tmp_path / "docs" / "image.png").write_bytes(b"png")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "c.md").write_text("c")
    (tmp_path / "docs" / "link.md").symlink_to(tmp_path / "elsewhere" / "c.md")
    (tmp_path / "docs" / "dirlink").symlink_to(tmp_path / "elsewhere")
    found = intake.enumerate_directory(tmp_path / "docs")
    names = {p.name for p in found}
    assert names == {"a.md", "b.txt"}


def test_exclusion_matching_word_boundary() -> None:
    exclusions = ["Bedrock usage documentation"]
    assert matches_exclusions("Guide to Bedrock usage documentation setup", exclusions)
    assert not matches_exclusions("Bedrockshire council minutes", exclusions)
