"""Reusable import boundaries shared by intake, discovery and editorial evidence.

URL rules match normalized host/path or exact URL, never a substring of a host.
Explicit exceptions override exclusions; include rules constrain the remaining
allow-list. Semantic restrictions are attached to the same rule and carried
into claim extraction, checking and publication. Unknown applicability is not
silently generalized into a provider-wide fact.
"""

from __future__ import annotations

import fnmatch
import re
from urllib.parse import urlsplit

from doclayer.curator.intake import normalize_url


def validate_policy(policy: dict) -> dict:
    if not isinstance(policy, dict):
        raise ValueError("source_policy must be a mapping")
    global_rule = policy.get("global", {})
    if not isinstance(global_rule, dict):
        raise ValueError("source_policy.global must be a mapping")
    if not isinstance(global_rule.get("forbidden_claims", []), list) or any(
        not isinstance(value, str) for value in global_rule.get("forbidden_claims", [])
    ):
        raise ValueError("global forbidden_claims must be a list of patterns")
    for expression in global_rule.get("forbidden_claims", []):
        re.compile(expression, re.I)
    if not isinstance(global_rule.get("applicability", {}), dict):
        raise ValueError("global applicability must be a mapping")
    for key in ("include", "exclude", "exceptions"):
        rules = policy.get(key, [])
        if not isinstance(rules, list):
            raise ValueError(f"source_policy.{key} must be a list")
        for rule in rules:
            if not isinstance(rule, dict) or not any(rule.get(k) for k in ("url", "domain", "path")):
                raise ValueError(f"source_policy.{key} needs URL/domain/path rules")
            if rule.get("url"):
                normalize_url(str(rule["url"]))
            if not isinstance(rule.get("forbidden_claims", []), list) or any(
                not isinstance(value, str) for value in rule.get("forbidden_claims", [])
            ):
                raise ValueError("rule forbidden_claims must be a list of patterns")
            for expression in rule.get("forbidden_claims", []):
                re.compile(expression, re.I)
            if not isinstance(rule.get("applicability", {}), dict):
                raise ValueError("rule applicability must be a mapping")
    return policy


def matches(url: str, rule: dict) -> bool:
    parsed = urlsplit(normalize_url(url))
    if rule.get("url") and normalize_url(str(rule["url"])) != normalize_url(url):
        return False
    domain = str(rule.get("domain", "")).lower()
    if domain and not fnmatch.fnmatchcase(parsed.hostname or "", domain):
        return False
    return not rule.get("path") or fnmatch.fnmatchcase(parsed.path, str(rule["path"]))


def url_decision(url: str | None, policy: dict) -> tuple[bool, str, dict]:
    if not url:
        return True, "local input", {}
    for rule in policy.get("exceptions", []):
        if matches(url, rule):
            return True, str(rule.get("reason", "explicit source exception")), rule
    for rule in policy.get("exclude", []):
        if matches(url, rule):
            return False, str(rule.get("reason", "excluded URL policy")), rule
    includes = policy.get("include", [])
    if includes:
        for rule in includes:
            if matches(url, rule):
                return True, str(rule.get("reason", "included URL policy")), rule
        return False, "outside source inclusion policy", {}
    return True, "no restrictive URL rule", {}


def claim_decision(url: str | None, text: str, applicability: dict, policy: dict) -> tuple[bool, str]:
    allowed, reason, rule = url_decision(url, policy)
    if not allowed:
        return False, reason
    global_rule = policy.get("global", {})
    for expression in [*global_rule.get("forbidden_claims", []), *rule.get("forbidden_claims", [])]:
        if re.search(expression, text, re.I):
            return False, "claim violates semantic source restriction"
    for key, required in {**global_rule.get("applicability", {}), **rule.get("applicability", {})}.items():
        observed = applicability.get(key)
        if observed and str(observed).casefold() != str(required).casefold():
            return False, f"incompatible {key} applicability"
    return True, reason
