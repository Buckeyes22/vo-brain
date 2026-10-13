"""KnownPages.resolve_wikilink must resolve every link form the curator's
pages actually use to the existing page, and still return None for a page
that does not exist."""

from __future__ import annotations

import pytest

from doclayer.curator.validate import KnownPages


def known() -> KnownPages:
    return KnownPages(
        pages={
            "wiki/page.md": {},
            "infra/fleet-hosts.md": {},
            "wiki/gpt-6-astra.md": {},
        },
        published_paths={"wiki/just-published.md"},
    )


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("wiki/page", "wiki/page.md"),
        ("page", "wiki/page.md"),
        ("wiki/page.md", "wiki/page.md"),
        ("page.md", "wiki/page.md"),
        ("page|alias", "wiki/page.md"),
        ("page#Heading", "wiki/page.md"),
        ("wiki/page#Heading|alias", "wiki/page.md"),
        ("infra/fleet-hosts", "infra/fleet-hosts.md"),
        ("infra/fleet-hosts.md", "infra/fleet-hosts.md"),
        ("fleet-hosts", "infra/fleet-hosts.md"),
        ("gpt-6-astra", "wiki/gpt-6-astra.md"),
        ("  page  ", "wiki/page.md"),
        ("just-published", "wiki/just-published.md"),
    ],
)
def test_existing_page_resolves(link: str, expected: str) -> None:
    assert known().resolve_wikilink(link) == expected


@pytest.mark.parametrize(
    "link",
    [
        "missing",
        "wiki/missing",
        "wiki/missing.md",
        "missing|alias",
        "missing#Heading",
        "infra/page",  # right stem, wrong directory: a mistaken path
        "wiki/fleet-hosts",
        "pag",  # prefix of a stem is not a match
    ],
)
def test_missing_page_resolves_to_none(link: str) -> None:
    assert known().resolve_wikilink(link) is None


# -- check_links: only links new in the draft may fail ----------------------------- #

from doclayer.curator.state import PageChange  # noqa: E402
from doclayer.curator.validate import blockers, check_links  # noqa: E402


def _update(body: str) -> PageChange:
    return PageChange(group_id="g", path="wiki/page.md", action="update", draft_body=body)


def test_preexisting_wrong_directory_link_is_not_flagged() -> None:
    existing = "See [[wiki/fleet-hosts]].\n"
    draft = "See [[wiki/fleet-hosts]]. More text.\n"
    failures = check_links(_update(draft), known(), set(), existing)
    assert failures == []


def test_new_wrong_directory_link_is_flagged() -> None:
    existing = "Nothing linked.\n"
    draft = "See [[wiki/fleet-hosts]].\n"
    failures = check_links(_update(draft), known(), set(), existing)
    hard = blockers(failures)
    assert len(hard) == 1 and "wiki/fleet-hosts" in hard[0].detail


def test_new_wrong_directory_link_flagged_without_existing_body() -> None:
    failures = check_links(_update("See [[wiki/fleet-hosts]].\n"), known(), set())
    assert len(blockers(failures)) == 1


def test_new_correct_bare_link_is_not_flagged() -> None:
    existing = "Nothing linked.\n"
    draft = "See [[page]] and [[page|alias]] and [[page#Heading]].\n"
    assert check_links(_update(draft), known(), set(), existing) == []


def test_preexisting_link_with_alias_is_not_flagged_but_new_one_is() -> None:
    existing = "See [[wiki/fleet-hosts|hosts]].\n"
    draft = "See [[wiki/fleet-hosts]] and [[wiki/other-hosts]].\n"
    hard = blockers(check_links(_update(draft), known(), set(), existing))
    assert [("other-hosts" in f.detail) for f in hard] == [True]


def test_run_deterministic_checks_passes_existing_body_to_link_check() -> None:
    from conftest import valid_meta

    from doclayer.curator.config import CuratorConfig
    from doclayer.curator.validate import run_deterministic_checks

    cfg = CuratorConfig()
    cfg.publish.allowed_prefixes = ["wiki/", "infra/"]
    body = "# T\n\nSee [[wiki/fleet-hosts]]. [source: a]\n"
    change = PageChange(
        group_id="g",
        path="wiki/page.md",
        action="update",
        draft_meta=valid_meta(),
        draft_body=body,
    )
    failures = run_deterministic_checks(change, cfg, known(), valid_meta(), body, [])
    assert [f for f in failures if f.check == "links"] == []
    created = PageChange(
        group_id="g",
        path="wiki/new-page.md",
        action="create",
        draft_meta=valid_meta(),
        draft_body=body,
    )
    failures = run_deterministic_checks(created, cfg, known(), None, None, [])
    assert len([f for f in failures if f.check == "links"]) == 1
