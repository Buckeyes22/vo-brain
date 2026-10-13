"""Junk detection for fetched docs pages.

The fetcher never proposes a junk copy: an HTTP error, a soft-404 (the
Alibaba Cloud help portal answers HTTP 200 with a 404 error page), a login
page, or an empty article. A junk verdict names its reason so the recipe can
record it.
"""

from __future__ import annotations

#: A converted article shorter than this is not a doc page worth keeping.
MIN_BODY_CHARS = 300

#: Strings that mark Alibaba Cloud help-portal soft-404 / error shells.
_SOFT_404_MARKERS = ('notfound: "true"', "statusCode: 404", "statusCode: '404'")

#: Title fragments that mark auth walls rather than documentation.
_LOGIN_TITLE_MARKERS = ("sign in", "log in", "login", "sign-in")


def classify(status_code: int, html: str, markdown: str) -> tuple[bool, str]:
    """Return (is_good, reason). reason is "ok" or "junk:<why>"."""
    if status_code != 200:
        return False, f"junk:http_{status_code}"
    for marker in _SOFT_404_MARKERS:
        if marker in html:
            return False, "junk:soft_404"
    title = ""
    from doclayer.convert import page_title

    extracted = page_title(html)
    if extracted:
        title = extracted.lower()
    if any(marker in title for marker in _LOGIN_TITLE_MARKERS):
        return False, "junk:login_page"
    if len(markdown.strip()) < MIN_BODY_CHARS:
        return False, "junk:too_short"
    return True, "ok"
