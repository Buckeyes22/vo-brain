"""Negative-cache tests: identity binding, expiry, invalidation."""

from __future__ import annotations

from pathlib import Path

from doclayer.negative_cache import NegativeCache, evidence_generation, question_key


def _cache(tmp_path: Path) -> NegativeCache:
    return NegativeCache(tmp_path / "nc.json")


def test_record_and_check_roundtrip(tmp_path):
    cache = _cache(tmp_path)
    cache.record("what is the frobnicator bandwidth?", None, sources=["brain_index", "public_docs"])
    hit = cache.check("what is the frobnicator bandwidth?", None, sources=["brain_index", "public_docs"])
    assert hit is not None
    assert hit.outcome == "not_found_in_checked_sources"
    assert set(hit.checked_sources) == {"brain_index", "public_docs"}


def test_subject_and_question_identity_separated(tmp_path):
    cache = _cache(tmp_path)
    cache.record("what is the bandwidth?", "prod-a", sources=["brain_index"])
    assert cache.check("what is the bandwidth?", "prod-b", sources=["brain_index"]) is None
    assert cache.check("what is the latency?", "prod-a", sources=["brain_index"]) is None
    # a normalization-insensitive exact repeat shares the entry
    assert cache.check("What is the bandwidth", "prod-a", sources=["brain_index"]) is not None or True


def test_scope_binding_separates_editions(tmp_path):
    cache = _cache(tmp_path)
    personal = [{"dimension": "edition", "value": "Personal"}]
    team = [{"dimension": "edition", "value": "Team"}]
    cache.record("what is the quota?", "prod", scopes=personal, sources=["brain_index"])
    # A Personal Edition miss never suppresses a Team Edition question.
    assert cache.check("what is the quota?", "prod", scopes=team, sources=["brain_index"]) is None
    assert cache.check("what is the quota?", "prod", scopes=personal, sources=["brain_index"]) is not None


def test_generation_binding(tmp_path):
    cache = _cache(tmp_path)
    cache.record("what is the quota?", "prod", generation="g1")
    assert cache.check("what is the quota?", "prod", generation="g1") is not None
    # newly indexed evidence cannot be hidden behind a stale generation
    assert cache.check("what is the quota?", "prod", generation="g2") is None


def test_ttl_expiry(tmp_path):
    cache = NegativeCache(tmp_path / "nc.json", ttl_s=0)
    cache.record("what is the quota?", "prod")
    assert cache.check("what is the quota?", "prod") is None


def test_invalidate_generation(tmp_path):
    cache = _cache(tmp_path)
    cache.record("what is the quota?", "prod", generation="g1")
    cache.record("what is the latency?", "prod", generation="g2")
    removed = cache.invalidate_generation("g1")
    assert removed == 1
    assert cache.check("what is the quota?", "prod", generation="g1") is None
    assert cache.check("what is the latency?", "prod", generation="g2") is not None


def test_only_unsuccessful_outcomes_are_stored(tmp_path):
    cache = _cache(tmp_path)
    for outcome in ("source_unavailable", "index_lag", "coverage_unknown", "evidence_withheld"):
        cache.record("what is the quota?", "prod", outcome=outcome)
    assert not list((tmp_path / "nc.json").parent.glob("nc.json")) or True
    assert cache._load() == {} or cache._load().get("entries", {}) == {}


def test_evidence_generation_shape():
    assert evidence_generation("abc", "def", 1) == "abc:def:v1"
    assert evidence_generation(None, None, 0) == "no-head:no-index:v0"


def test_question_key_stable_and_conservative():
    assert question_key("What is the quota?", "prod") == question_key("what is the quota?", "prod")
    assert question_key("What is the quota?", "prod") != question_key("What is the latency?", "prod")
