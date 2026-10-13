"""Tests for the fetcher: good copies are written, junk is never stored."""

import httpx
import pytest

from doclayer.fetch import refresh

GOOD_BODY = (
    "<html><head><title>Doc page</title></head><body>"
    "<div class='icms-help-docs-content'><h1>Doc page</h1><p>%s</p></div></body></html>"
) % ("Real documentation content. " * 40)

SOFT_404 = (
    "<html><head><title>404</title></head><body><script>"
    'window.globalData = { notfound: "true" };</script></body></html>'
)

PAGES = {
    "https://docs.test/ok": (200, GOOD_BODY),
    # HTTP 200 but a 404 error shell: must never be stored.
    "https://docs.test/soft404": (200, SOFT_404),
    "https://docs.test/down": None,  # connection error
}


def _transport(handler):
    return httpx.MockTransport(handler)


def _handler(request: httpx.Request) -> httpx.Response:
    entry = PAGES.get(str(request.url))
    if entry is None:
        raise httpx.ConnectError("refused", request=request)
    status, text = entry
    return httpx.Response(status, text=text)


@pytest.fixture()
def recipe(tmp_path):
    dest = tmp_path / "dest"
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text(
        "product: test-product\n"
        "docs: {version: current}\n"
        "pages:\n"
        "  - {slug: ok, url: https://docs.test/ok}\n"
        "  - {slug: soft404, url: https://docs.test/soft404}\n"
        "  - {slug: down, url: https://docs.test/down}\n"
        "last_attempt: null\n"
        "last_error: null\n",
        encoding="utf-8",
    )
    return recipe, dest


async def test_good_pages_stored_and_junk_skipped(recipe, tmp_path):
    recipe_path, dest = recipe
    rows = await refresh(recipe_path, dest, transport=_transport(_handler))
    results = {row["slug"]: row["result"] for row in rows}
    assert results == {
        "ok": "ok",
        "soft404": "junk:soft_404",
        "down": "error:ConnectError",
    }
    stored = {p.name for p in dest.glob("*.md")}
    assert stored == {"ok.md"}
    copy = (dest / "ok.md").read_text(encoding="utf-8")
    assert "url: https://docs.test/ok" in copy
    assert "product: test-product" in copy
    assert "fetched_at:" in copy


async def test_recipe_records_last_attempt_and_error(recipe):
    recipe_path, dest = recipe
    await refresh(recipe_path, dest, transport=_transport(_handler))
    import yaml

    state = yaml.safe_load(recipe_path.read_text(encoding="utf-8"))
    assert state["last_attempt"]
    assert "soft404: junk:soft_404" in state["last_error"]
    assert "down: error:ConnectError" in state["last_error"]


async def test_junk_never_overwrites_a_good_copy(recipe):
    recipe_path, dest = recipe
    await refresh(recipe_path, dest, transport=_transport(_handler))
    good_before = (dest / "ok.md").read_text(encoding="utf-8")

    # Flip the good page to junk; the stored copy must survive untouched.
    original = PAGES["https://docs.test/ok"]
    PAGES["https://docs.test/ok"] = (200, SOFT_404)
    try:
        await refresh(recipe_path, dest, transport=_transport(_handler))
    finally:
        PAGES["https://docs.test/ok"] = original
    assert (dest / "ok.md").read_text(encoding="utf-8") == good_before
