"""Operator-declared local evidence profiles, loaded from YAML.

A profile names one kind of local question by regex (`match`, `require`) and
says how routing treats it (`stop`, `always_local`). The file also carries
`local_need_terms`. The keys `documents`, `observations` and `sections` (in a
profile) and `context` (at the top level) are not supported; the loader rejects
them by name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


class ProfileError(ValueError):
    """The profiles file is malformed; the message names the field."""


@dataclass(frozen=True)
class Profile:
    name: str
    match: tuple[re.Pattern[str], ...]
    require: tuple[re.Pattern[str], ...] = ()
    always_local: bool = False
    stop: bool = False

    def matches(self, text: str) -> bool:
        return any(p.search(text) for p in self.match) and all(p.search(text) for p in self.require)


@dataclass(frozen=True)
class Profiles:
    profiles: tuple[Profile, ...] = ()
    local_terms: re.Pattern[str] | None = None

    def select(self, text: str) -> Profile | None:
        for profile in self.profiles:
            if profile.matches(text):
                return None if profile.stop else profile
        return None


def _strings(raw, where: str, what: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        raise ProfileError(f"{where} must be a list of {what}")
    return raw


def _patterns(raw, where: str) -> tuple[re.Pattern[str], ...]:
    try:
        return tuple(re.compile(p, re.IGNORECASE) for p in _strings(raw, where, "regex strings"))
    except re.error as exc:
        raise ProfileError(f"{where}: invalid regex ({exc})") from exc


def _flag(raw: dict, key: str, where: str) -> bool:
    value = raw.get(key, False)
    if not isinstance(value, bool):
        raise ProfileError(f"{where}.{key} must be true or false, not {value!r}")
    return value


#: Unsupported profile keys; a file that still carries one fails loudly.
REMOVED_PROFILE_KEYS = ("documents", "observations", "sections")
#: Unsupported top-level keys, rejected the same way.
REMOVED_TOP_LEVEL_KEYS = ("context",)


def _profile(i: int, raw) -> Profile:
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not raw["name"]:
        raise ProfileError(f"profiles[{i}] needs a string name")
    where = f"profiles[{i}] ({raw['name']})"
    for key in REMOVED_PROFILE_KEYS:
        if key in raw:
            raise ProfileError(f"{where}.{key} is no longer supported; remove it")
    match = _patterns(raw.get("match"), f"{where}.match")
    if not match:
        raise ProfileError(f"{where}.match needs at least one regex")
    return Profile(
        name=raw["name"],
        match=match,
        require=_patterns(raw.get("require"), f"{where}.require"),
        always_local=_flag(raw, "always_local", where),
        stop=_flag(raw, "stop", where),
    )


#: Latest parse per file, keyed by (mtime_ns, size) so an edit reloads.
_cache: dict[Path, tuple[tuple[int, int], Profiles]] = {}


def load_profiles(path: Path) -> Profiles:
    """Profiles from `path`; a missing file leaves profile routing dormant."""
    path = Path(path).expanduser()
    if not path.is_file():
        return Profiles()
    stat = path.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _cache.get(path)
    if cached and cached[0] == key:
        return cached[1]
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ProfileError(f"{path}: invalid YAML ({exc})") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"{path}: top level must be a mapping")
    raw_profiles = data.get("profiles")
    if raw_profiles is not None and not isinstance(raw_profiles, list):
        raise ProfileError("profiles must be a list")
    for key in REMOVED_TOP_LEVEL_KEYS:
        if key in data:
            raise ProfileError(f"{key} is no longer supported; remove it")
    profiles = tuple(_profile(i, p) for i, p in enumerate(raw_profiles or []))
    seen = set()
    for i, profile in enumerate(profiles):
        if profile.name in seen:
            raise ProfileError(f"profiles[{i}] ({profile.name}).name is already used")
        seen.add(profile.name)
    terms = data.get("local_need_terms")
    if terms is not None and not isinstance(terms, str):
        raise ProfileError("local_need_terms must be a single regex string")
    result = Profiles(
        profiles=profiles,
        local_terms=_patterns([terms], "local_need_terms")[0] if terms else None,
    )
    _cache[path] = (key, result)
    return result
