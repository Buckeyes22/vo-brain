"""Test isolation: routing/caching/ledger state never touches real user state.

The doclayer package reads its state locations from the settings singleton at
call time; tests get per-test temporary locations, and any test constructing a
fresh Settings() inherits session-scoped environment overrides set here before
the package is first imported.
"""

from __future__ import annotations

import os
import tempfile

_SESSION_TMP = tempfile.mkdtemp(prefix="doclayer-tests-")

# Set before doclayer.settings is imported anywhere: fresh Settings()
# instances (some tests build their own) pick these up.
os.environ.setdefault("DOC_LAYER_CATALOG_CACHE_DIR", os.path.join(_SESSION_TMP, "catalog"))
os.environ.setdefault("DOC_LAYER_NEGATIVE_CACHE", os.path.join(_SESSION_TMP, "session-negative-cache.json"))
os.environ.setdefault("DOC_LAYER_GAPS_DIR", os.path.join(_SESSION_TMP, "session-gaps"))
os.environ["DOC_LAYER_GAP_OWNER"] = "local"
# The machine's own ~/.config/brain-curator/config.yaml must never steer a test:
# load_config() reads this path first, and a missing file means the defaults.
# Tests that need a config pass a path or set the variable themselves.
os.environ["BRAIN_CURATOR_CONFIG"] = os.path.join(_SESSION_TMP, "no-curator-config.yaml")
# No test may read or write this user's real home (~/.config, ~/.cache, ...):
# HOME and the XDG roots point into the session temp directory.
_TEST_HOME = os.path.join(_SESSION_TMP, "home")
os.makedirs(_TEST_HOME, exist_ok=True)
os.environ["HOME"] = _TEST_HOME
for _xdg in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
    os.environ[_xdg] = os.path.join(_TEST_HOME, _xdg.lower())

import pytest  # noqa: E402

from doclayer.settings import settings  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_doclayer_state(tmp_path, monkeypatch):
    """Per-test routing/ledger/cache state; catalog cache stays session-wide."""
    monkeypatch.setattr(settings, "negative_cache_path", tmp_path / "negative-cache.json")
    monkeypatch.setattr(settings, "gap_state_dir", tmp_path / "gaps")
    monkeypatch.setattr(settings, "gap_capture_enabled", True)
    monkeypatch.setattr(settings, "local_profiles_path", tmp_path / "no-local-profiles.yaml")
    yield


@pytest.fixture
def local_profiles(monkeypatch):
    """Install the fictional-fleet profiles (or a given file) for one test."""
    from pathlib import Path

    def install(path=Path(__file__).parent / "fixtures" / "local-profiles.yaml"):
        monkeypatch.setattr(settings, "local_profiles_path", Path(path))

    return install


@pytest.fixture(autouse=True)
def _isolated_known_identities(monkeypatch):
    """Scope recognition never reads the real ssh config or brain catalog."""
    from doclayer import applicability, resolver

    monkeypatch.setattr(applicability, "known_identities", lambda: applicability.Identities())
    # resolver imported the function by name; follow whatever applicability currently serves.
    monkeypatch.setattr(resolver, "known_identities", lambda: applicability.known_identities())
    monkeypatch.setattr(applicability, "_identity_cache", {})
    yield


@pytest.fixture
def identities(monkeypatch):
    """Opt in to specific recognized hosts and catalog subjects: identities(hosts=[...], subjects=[...])."""
    from doclayer import applicability

    def install(hosts=(), subjects=(), groups=(), profiles=(), aliases=(), declared=()):
        keys = [applicability._group_keys(group) for group in groups]  # noqa: SLF001
        owners = [{applicability._identity_key(name) for name in names} for names in declared]  # noqa: SLF001
        found = applicability.Identities(
            frozenset(hosts),
            frozenset(subjects),
            applicability._merge_groups(keys, owners),  # noqa: SLF001
            tuple(profiles),
            frozenset(applicability._identity_key(a) for a in aliases),
        )  # noqa: SLF001
        monkeypatch.setattr(applicability, "known_identities", lambda: found)
        return found

    return install


@pytest.fixture(autouse=True)
def _no_implicit_live_gap_curator(monkeypatch):
    """A forgotten worker transport fixture must fail before a live submit."""
    from doclayer import dispatcher

    def unconfigured_transport(*args, **kwargs):
        raise AssertionError("Tests must supply a curator transport; live default forbidden")

    monkeypatch.setattr(dispatcher, "transport_for", unconfigured_transport)


from doclayer import private_writer as _private_writer  # noqa: E402

_REAL_PREPARE = _private_writer.prepare_private_gap


@pytest.fixture(autouse=True)
def _no_implicit_private_writer(monkeypatch):
    """The worker never plans or runs live commands unless a test opts in."""
    monkeypatch.setattr(_private_writer, "prepare_private_gap", lambda *a, **k: False)


@pytest.fixture
def real_private_writer(monkeypatch):
    monkeypatch.setattr(_private_writer, "prepare_private_gap", _REAL_PREPARE)
