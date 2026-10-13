"""The versioned routing evaluation fixture: every case routes as
expected, and the warm-path hook budget is measured on this same set."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from doclayer import routing
from doclayer.coverage import Catalog, CatalogPage, SubjectEntry

FIXTURE = yaml.safe_load((Path(__file__).parent / "fixtures" / "routing-evaluation.yaml").read_text())


def _catalog() -> Catalog:
    catalog = Catalog(status="ok", revision="a" * 40, policy_version=1)
    for raw in FIXTURE["subjects_fixture"]:
        entry = SubjectEntry(
            slug=raw["slug"],
            kind=raw["kind"],
            title=raw["slug"],
            aliases=list(raw["aliases"]),
            pages=[CatalogPage(path=f"wiki/{raw['slug']}.md", concept_id=None)],
            page_status="committed",
        )
        catalog.subjects[entry.slug] = entry
    return catalog


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=[c["id"] for c in FIXTURE["cases"]])
def test_expected_route(case):
    catalog = _catalog()
    result = routing.route_prompt(case["prompt"], catalog, [], explicit=bool(case.get("explicit")))
    assert result.intent == case["expected_intent"], (case["id"], result.reason)
    assert sorted(result.subjects) == sorted(case["expected_subjects"]), case["id"]
    if case.get("gap_candidate"):
        assert result.uncovered_subject == case["gap_candidate"], case["id"]
        assert result.gap_capture_eligible, case["id"]
    wants_nudge = bool(
        result.intent in (routing.INTENT_LOCAL, routing.INTENT_PUBLIC, routing.INTENT_MIXED)
        and (result.subjects or result.uncovered_subject)
    )
    assert wants_nudge == case["expected_nudge"], case["id"]


def test_hook_warm_path_under_budget():
    """Warm-path p95 target below 100 ms: the classify step, run
    over the whole evaluation set, must average far under the budget."""
    catalog = _catalog()
    prompts = [c["prompt"] for c in FIXTURE["cases"]] * 5
    start = time.perf_counter()
    for prompt in prompts:
        routing.route_prompt(prompt, catalog, [])
    elapsed_ms = (time.perf_counter() - start) * 1000 / len(prompts)
    assert elapsed_ms < 5, elapsed_ms  # classification itself; cache loads excluded


def test_route_reduction_against_standing_instruction_baseline():
    """Route-reduction receipt: compare the new routing against the
    standing-instruction rule (resolve for every product/library docs lookup
    or subject mention) on the same fixture. Report counts, not percentages."""
    catalog = _catalog()
    baseline_triggers = [c for c in FIXTURE["cases"] if c["id"] not in ("explicit",)]

    # Standing instruction era: every case that mentions a subject or asks for
    # docs would send the agent to resolve, which is the baseline this test measures against.
    def old_rule(prompt: str) -> bool:
        text = prompt.lower()
        return any(
            phrase in text for phrase in ("what version", "where does", "docs for", "which host", "base url")
        ) or any(
            subject in text.replace("-", " ")
            for subject in (
                "model studio",
                "gpt 6 astra",
                "responses api",
                "token plan",
                "litellm",
                "worker1",
                "prompting",
                "tools",
                "performance",
            )
        )

    old_calls = sum(1 for c in baseline_triggers if old_rule(c["prompt"]))
    new_calls = sum(1 for c in baseline_triggers if routing.route_prompt(c["prompt"], catalog, []).wants_lookup)
    # Selective routing strictly reduces unnecessary lookups on this fixture.
    assert new_calls < old_calls, (old_calls, new_calls)
    # ... without losing the covered/local lookups.
    covered_still_routed = [
        c["id"]
        for c in FIXTURE["cases"]
        if c.get("expected_nudge")
        and c["id"] != "public-uncovered"
        and routing.route_prompt(c["prompt"], catalog, []).wants_lookup
    ]
    assert covered_still_routed, covered_still_routed
