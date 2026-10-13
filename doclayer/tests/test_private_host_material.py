"""Exact host/tool needs select the matching profile."""

from types import SimpleNamespace

import pytest

from doclayer.local_material import local_profile


@pytest.fixture(autouse=True)
def _fictional_profiles(local_profiles):
    local_profiles()


def record(question):
    return SimpleNamespace(
        identity="unknown:unidentified",
        canonical_subject=None,
        representative_question=question,
        source_candidates=[],
    )


@pytest.mark.parametrize(
    "question,profile",
    [
        ("What is the existing gpu1 capture runtime?", "gpu-capture"),
        ("Where is the network controller MCP and why was startup failing?", "network-controller"),
        ("What is the search stack VPN/mTLS setup on svc1 versus nas1?", "search-stack"),
        ("Document the Orca AT-SPI graphical test route", "accessibility"),
        ("Local workstation OSK alternative: keyboard extraction", "desktop-osk"),
        ("What causes GNOME desktop logouts on ws1?", "desktop-crash"),
        ("Example CMS publishing and storage configuration", "example-cms"),
        ('look for documentation about any sort of "code search" system', "code-search"),
    ],
)
def test_exact_need_selects_the_matching_profile(question, profile):
    row = record(question + "; $(touch /tmp/not-a-collector-command)")
    assert local_profile(row) == profile


def test_stop_profile_and_dormant_profiles_select_nothing(local_profiles, tmp_path):
    from doclayer.local_material import is_local_need

    stopped = record("Which analytics desktop deployment is installed?")
    stopped.need_class = "missing_coverage"
    assert local_profile(stopped) is None
    local_profiles(tmp_path / "absent.yaml")
    row = record("What causes GNOME desktop logouts on ws1?")
    row.need_class = "missing_coverage"
    assert local_profile(row) is None and not is_local_need(row)


def test_always_local_profiles_and_local_terms_decide_local_need():
    from doclayer.local_material import is_local_need

    always = record("Where is the code search pipeline documented?")
    always.need_class = "missing_coverage"
    assert local_profile(always) == "code-search" and is_local_need(always)
    plain = record("What colours does the example-app logo use?")
    plain.need_class = "missing_coverage"
    assert local_profile(plain) == "example-app" and not is_local_need(plain)
    term_only = record("What configuration does the svc1 reverse proxy use?")
    term_only.need_class = "missing_coverage"
    assert local_profile(term_only) is None and is_local_need(term_only)
