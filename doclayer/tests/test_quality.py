"""Tests for junk detection (quality.py)."""

from doclayer.quality import classify

GOOD_HTML = """<html><head><title>Token Plan (Personal Edition)</title></head>
<body><div class="icms-help-docs-content"><h1>Token Plan</h1><p>%s</p></div></body></html>
""" % ("Token Plan Personal Edition uses Credits. " * 40)

SOFT_404_HTML = (
    "<html><head><title>404 error page</title></head><body>"
    '<script>window.globalData = { notfound: "true", statusCode: "404" };</script>'
    "</body></html>"
)

LOGIN_HTML = (
    "<html><head><title>Sign in to your account</title></head>"
    "<body><form><input name='user'/></form><p>Please sign in to continue.</p></body></html>"
)


def test_good_page_passes():
    good, reason = classify(200, GOOD_HTML, "# Token Plan\n\n" + "Credits. " * 200)
    assert good and reason == "ok"


def test_http_error_is_junk():
    good, reason = classify(503, "<html></html>", "")
    assert not good and reason == "junk:http_503"


def test_soft_404_is_junk():
    good, reason = classify(200, SOFT_404_HTML, "x" * 500)
    assert not good and reason == "junk:soft_404"


def test_login_page_is_junk():
    good, reason = classify(200, LOGIN_HTML, "Please sign in to continue.")
    assert not good and reason == "junk:login_page"


def test_empty_body_is_junk():
    good, reason = classify(200, GOOD_HTML, "")
    assert not good and reason == "junk:too_short"
