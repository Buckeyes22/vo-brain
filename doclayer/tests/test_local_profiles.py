import textwrap

import pytest

from doclayer.local_profiles import ProfileError, load_profiles


def write(tmp_path, body):
    p = tmp_path / "profiles.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def test_first_matching_profile_wins_and_require_is_conjunctive(tmp_path):
    ps = load_profiles(
        write(
            tmp_path,
            r"""
        profiles:
          - name: desktop-crash
            match: ['\b(crash|freeze)\b']
            require: ['\b(ws1|desktop)\b']
          - name: desktop
            match: ['\bdesktop\b']
    """,
        )
    )
    assert ps.select("why does the desktop crash").name == "desktop-crash"
    assert ps.select("desktop theme").name == "desktop"
    assert ps.select("app crash on app1") is None


def test_stop_profile_returns_none(tmp_path):
    ps = load_profiles(
        write(
            tmp_path,
            r"""
        profiles:
          - {name: forecast, match: ['\bforecast\b'], stop: true}
          - {name: app, match: ['\bapp1\b']}
    """,
        )
    )
    assert ps.select("forecast feed on app1") is None


def test_missing_profiles_file_is_dormant(tmp_path):
    ps = load_profiles(tmp_path / "absent.yaml")
    assert ps.profiles == () and ps.local_terms is None and ps.select("anything") is None


def test_bad_regex_names_profile(tmp_path):
    with pytest.raises(ProfileError, match=r"profiles\[0\] \(broken\)\.match"):
        load_profiles(write(tmp_path, "profiles:\n  - {name: broken, match: ['(']}\n"))


def test_shipped_example_and_fixture_load():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for path in (
        root / "examples" / "local-profiles.example.yaml",
        root / "tests" / "fixtures" / "local-profiles.yaml",
    ):
        ps = load_profiles(path)
        assert ps.profiles and ps.local_terms
        assert ps.select("what causes gnome desktop logouts on ws1").name == "desktop-crash"
        assert ps.select("analytics desktop") is None


@pytest.mark.parametrize(
    "body, field",
    [
        ("profiles: [\n", r"invalid YAML"),
        ("profiles:\n  - {name: a, match: ['x'], stop: 'false'}\n", r"profiles\[0\] \(a\)\.stop must be true or false"),
        (
            "profiles:\n  - {name: a, match: ['x'], always_local: 'no'}\n",
            r"profiles\[0\] \(a\)\.always_local must be true or false",
        ),
        ("profiles:\n  - {name: a}\n", r"profiles\[0\] \(a\)\.match needs at least one regex"),
        ("profiles:\n  - {name: a, match: []}\n", r"profiles\[0\] \(a\)\.match needs at least one regex"),
        (
            "profiles:\n  - {name: a, match: ['x']}\n  - {name: a, match: ['y']}\n",
            r"profiles\[1\] \(a\)\.name is already used",
        ),
        ("local_need_terms: ['x']\n", r"local_need_terms must be a single regex string"),
    ],
)
def test_malformed_profiles_raise_profile_error_naming_the_field(tmp_path, body, field):
    with pytest.raises(ProfileError, match=field):
        load_profiles(write(tmp_path, body))


@pytest.mark.parametrize(
    "key, value",
    [
        ("documents", "[wiki/a.md]"),
        ("observations", "[{host: ws1, script: 'uname -r'}]"),
        ("sections", "{wiki/a.md: [Rollout]}"),
    ],
)
def test_unsupported_profile_keys_are_rejected_by_name(tmp_path, key, value):
    body = f"profiles:\n  - {{name: a, match: ['x'], {key}: {value}}}\n"
    with pytest.raises(ProfileError, match=rf"profiles\[0\] \(a\)\.{key} is no longer supported"):
        load_profiles(write(tmp_path, body))


def test_top_level_context_key_is_rejected_by_name(tmp_path):
    body = "context: {name: c, subject: s, repo: r, allowed_paths: [reports/a.md]}\nprofiles:\n  - {name: a, match: ['x']}\n"
    with pytest.raises(ProfileError, match=r"^context is no longer supported; remove it$"):
        load_profiles(write(tmp_path, body))


def test_edited_file_reloads(tmp_path):
    path = write(tmp_path, "profiles:\n  - {name: a, match: ['x']}\n")
    assert load_profiles(path).select("x").name == "a"
    path.write_text("profiles:\n  - {name: renamed, match: ['x']}\n")
    assert load_profiles(path).select("x").name == "renamed"
