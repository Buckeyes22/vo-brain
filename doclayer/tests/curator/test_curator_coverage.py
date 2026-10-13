"""Coverage recovery through durable job-state restart, without erasing diagnostics."""

import pytest

from doclayer.curator.coverage import assess_coverage
from doclayer.curator.state import JobState, SourceRecord


def reviewed():
    return SourceRecord(
        "manual",
        "text",
        "Device manual",
        status="fetched",
        extraction_complete=True,
        review_unit_count=1,
        reviewed_sections=["Complete source unit"],
    )


def restart(store, state):
    store.save(state)
    return store.load(state.job_id)


def job(store):
    state = store.create(JobState(job_id="", idempotency_key="", intent="Device"))[0]
    state.sources = [reviewed()]
    return state


@pytest.mark.parametrize("resolved_status", ["supported", "unsupported"])
def test_recovered_source_verification_replaces_current_gap_but_preserves_history(store, resolved_status):
    state = job(store)
    state.coverage = "incomplete"
    note = "source-support unknown claim-one: retained extract unavailable"
    state.coverage_gaps = [note]
    state.evidence_verification = {"claim-one": {"status": "unknown", "supported": False}}
    assert assess_coverage(restart(store, state)).coverage == "incomplete"
    state.evidence_verification["claim-one"] = {"status": resolved_status, "supported": resolved_status == "supported"}
    restored = restart(store, state)
    assessment = assess_coverage(restored)
    assert assessment.coverage == "complete" and not assessment.current_gaps
    assert assessment.historical_gaps == (note,)
    assert restored.coverage == "incomplete" and restored.coverage_gaps == [note]


def test_pending_and_new_material_verification_are_current_not_old_cached_success(store):
    state = job(store)
    state.evidence_verification = {
        "claim-one": {"status": "unknown", "verification_pending": True, "source_binding": "new-material"}
    }
    assert assess_coverage(restart(store, state)).coverage == "incomplete"
    state.evidence_verification["claim-one"] = {
        "status": "supported",
        "supported": True,
        "source_binding": "new-material",
    }
    assert assess_coverage(restart(store, state)).coverage == "complete"
    state.evidence_verification["claim-two"] = {
        "status": "unknown",
        "supported": False,
        "source_binding": "later-material",
    }
    assert assess_coverage(restart(store, state)).coverage == "incomplete"


def test_independent_frontier_remains_incomplete_after_support_recovers(store):
    state = job(store)
    state.coverage = "incomplete"
    state.coverage_gaps = ["source-support unknown claim-one: missing cache"]
    state.evidence_verification = {"claim-one": {"status": "supported", "supported": True}}
    state.sources.append(SourceRecord("later", "url", "Later guide", status="deferred"))
    assessment = assess_coverage(restart(store, state))
    assert assessment.coverage == "incomplete" and any("deferred" in gap for gap in assessment.current_gaps)
    assert assessment.historical_gaps == tuple(state.coverage_gaps)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda source: setattr(source, "extraction_complete", False),
        lambda source: setattr(source, "review_unit_count", 0),
        lambda source: setattr(source, "omitted_sections", ["Late section"]),
        lambda source: source.omitted_categories.update({"cache": "original extract absent"}),
        lambda source: setattr(source, "unknown_reviewed_sections", ["old-map-id"]),
        lambda source: (
            setattr(source, "review_unit_count", 2),
            setattr(source, "reviewed_sections", ["same", "same"]),
        ),
    ],
)
def test_current_review_omissions_cannot_be_erased_by_a_recovered_verdict(store, mutate):
    state = job(store)
    state.coverage = "incomplete"
    state.coverage_gaps = ["source-support unknown claim-one: missing cache"]
    state.evidence_verification = {"claim-one": {"status": "supported", "supported": True}}
    mutate(state.sources[0])
    assert assess_coverage(restart(store, state)).coverage == "incomplete"


def test_transfer_skips_are_incomplete_but_policy_exclusions_are_deliberate(store):
    state = job(store)
    skipped = SourceRecord("skipped", "url", "Other guide", status="skipped", error="job transfer limit exceeded")
    state.sources.append(skipped)
    assert assess_coverage(restart(store, state)).coverage == "incomplete"
    skipped.exclusions_hit = True
    skipped.error = "outside submitted applicability"
    assert assess_coverage(restart(store, state)).coverage == "complete"


@pytest.mark.parametrize(
    "note",
    [None, "Operator recorded incomplete import of private appendix", "Regulatory section omitted irrecoverably"],
)
def test_unexplained_legacy_and_irrecoverable_reasons_remain_incomplete(store, note):
    state = job(store)
    state.coverage = "incomplete"
    if note:
        state.coverage_gaps = [note]
    assessment = assess_coverage(restart(store, state))
    assert assessment.coverage == "incomplete" and assessment.current_gaps


def test_resolved_truncation_is_historical_after_actual_complete_unit_review(store):
    state = job(store)
    state.coverage = "incomplete"
    note = "Device manual: extraction output truncated; only part was reviewed"
    state.coverage_gaps = [note]
    state.sources[0].extraction_complete = False
    assert assess_coverage(restart(store, state)).coverage == "incomplete"
    state.sources[0].extraction_complete = True
    assessment = assess_coverage(restart(store, state))
    assert assessment.coverage == "complete" and assessment.historical_gaps == (note,)


def test_discovery_cache_recovery_requires_expansion_not_only_extraction(store):
    state = job(store)
    state.coverage = "incomplete"
    note = "Device manual: discovery cache unavailable"
    state.coverage_gaps = [note]
    assert assess_coverage(restart(store, state)).coverage == "incomplete"
    state.sources[0].relevance["links_expanded"] = True
    assert assess_coverage(restart(store, state)).coverage == "complete"


def test_current_limit_does_not_disappear_until_processing_completes(store):
    state = job(store)
    state.coverage = "incomplete"
    state.status = "retryable"
    state.stage = "validate"
    note = "stopped at configured limit (model_calls); work is preserved and resumable"
    state.coverage_gaps = [note]
    assert assess_coverage(restart(store, state)).coverage == "incomplete"
    state.status = "published"
    state.stage = "report"
    assessment = assess_coverage(restart(store, state))
    assert assessment.coverage == "complete" and assessment.historical_gaps == (note,)


def test_diagnostics_never_imply_missing_review_for_deliberately_rejected_claims(store):
    state = job(store)
    state.coverage_gaps = ["source-support rejection claim-one: contradicted by original source"]
    assessment = assess_coverage(restart(store, state))
    assert assessment.coverage == "complete" and assessment.historical_gaps == tuple(state.coverage_gaps)


def test_reported_effective_value_does_not_become_an_explicit_legacy_basis(store):
    state = job(store)
    state.coverage_basis = "complete"
    state.coverage = "incomplete"  # a previous report's derived result
    assert assess_coverage(state).coverage == "complete"
    state.coverage_basis = "incomplete"
    state.coverage = "complete"  # a raw public result cannot erase explicit basis
    assert assess_coverage(state).coverage == "incomplete"


def test_new_material_unknown_does_not_present_an_old_cache_failure_as_current(store):
    state = job(store)
    state.coverage = "incomplete"
    old = "source-support unknown claim-one: previous cache absent"
    state.coverage_gaps = [old]
    state.evidence_verification = {
        "claim-one": {
            "status": "unknown",
            "verification_pending": True,
            "reason": "new source revision not verified",
            "source_binding": "new-revision",
        }
    }
    assessment = assess_coverage(restart(store, state))
    assert assessment.coverage == "incomplete" and assessment.historical_gaps == (old,)
    assert any("new source revision" in gap for gap in assessment.current_gaps)
    assert not any("previous cache absent" in gap for gap in assessment.current_gaps)


@pytest.mark.parametrize("status", ["covered", "published"])
def test_placement_omissions_keep_editorial_coverage_open_despite_full_source_review(store, status):
    state = job(store)
    state.status = status
    state.stage = "report"
    note = (
        "placement evidence selection: 4 of 8 records used; remaining support retained, not proof of editorial coverage"
    )
    state.coverage_gaps = [note]
    state.evidence_selection = {"placement": {"selected": ["one", "two", "three", "four"], "omitted_count": 4}}
    assessment = assess_coverage(restart(store, state))
    assert assessment.coverage == "incomplete" and note in assessment.current_gaps
    assert any("no per-record disposition proof" in gap for gap in assessment.current_gaps)


def test_structured_placement_omissions_are_current_even_without_old_diagnostic(store):
    state = job(store)
    state.evidence_selection = {"placement": {"omitted_count": 2}}
    assert assess_coverage(restart(store, state)).coverage == "incomplete"


def test_legacy_placement_omission_is_not_proof_of_coverage_until_new_disposition_clears_it(store):
    state = job(store)
    state.coverage = "incomplete"
    note = (
        "placement evidence selection: 4 of 8 records used; remaining support retained, not proof of editorial coverage"
    )
    state.coverage_gaps = [note]
    assert assess_coverage(restart(store, state)).coverage == "incomplete"
    state.evidence_selection = {"placement": {"omitted_count": 0}}
    assessment = assess_coverage(restart(store, state))
    assert assessment.coverage == "complete" and assessment.historical_gaps == (note,)


def test_reported_incomplete_is_not_a_new_explicit_flag_after_restart(cfg, store):
    from doclayer.curator.ops import _status_view
    from doclayer.curator.pipeline import Pipeline

    state = job(store)
    state.evidence_verification = {"claim-one": {"status": "unknown", "reason": "temporary source outage"}}
    state.coverage_gaps = ["source-support unknown claim-one: temporary source outage"]
    runner = Pipeline(cfg, store)
    runner._finish_report(state)
    assert state.coverage == "incomplete" and state.coverage_basis == "complete"
    state = restart(store, state)
    state.evidence_verification["claim-one"] = {"status": "supported", "supported": True}
    runner._finish_report(state)
    state = restart(store, state)
    assert state.coverage == state.effective_coverage == "complete"
    status = _status_view(state, store)
    assert not status["coverage_gaps"]
    assert status["historical_coverage_gaps"] == state.coverage_gaps
    assert "## Current coverage gaps" not in state.summary
    assert "## Historical diagnostics / exclusions" in state.summary
    assert state.coverage_gaps[0] in state.summary


def test_report_preserves_unexplained_explicit_incomplete_basis(cfg, store):
    from doclayer.curator.pipeline import Pipeline

    state = job(store)
    state.coverage = "incomplete"
    Pipeline(cfg, store)._finish_report(state)
    restored = restart(store, state)
    assert restored.coverage_basis == "incomplete"
    assert restored.effective_coverage == "incomplete"
    assert "legacy or explicit incomplete" in restored.summary


def test_available_extract_recovers_only_cache_not_unreviewed_material(cfg, store):
    from doclayer.curator.pipeline import Pipeline

    state = job(store)
    source = state.sources[0]
    source.omitted_categories = {"cache": "retained extract unavailable", "Appendix": "not reviewed"}
    runner = Pipeline(cfg, store)
    assert runner._extract_text(state, source) is None
    assert "cache" in source.omitted_categories
    runner._store_extract(state, source, "# Manual\nActual retained source.\n")
    assert runner._extract_text(state, source) == "# Manual\nActual retained source.\n"
    assert source.omitted_categories == {"Appendix": "not reviewed"}
    assert state.effective_coverage == "incomplete"


@pytest.mark.parametrize("change_status", ["planned", "drafted", "validated", "failed", "conflict", "skipped"])
def test_placement_assignment_is_not_coverage_until_published(store, change_status):
    from doclayer.curator.state import PageChange

    state = job(store)
    state.evidence_selection = {
        "placement": {
            "omitted_count": 0,
            "ledger": {"claims": {"claim-one": {"status": "planned", "target": "wiki/device.md"}}},
        }
    }
    state.changes = [PageChange("g", "wiki/device.md", "update", status=change_status, claim_ids=["claim-one"])]
    assessment = assess_coverage(restart(store, state))
    assert assessment.coverage == "incomplete"
    assert any("claim-one" in gap and "not published" in gap for gap in assessment.current_gaps)


def test_published_assignment_covers_only_its_required_claims(store):
    from doclayer.curator.state import PageChange

    state = job(store)
    state.evidence_selection = {
        "placement": {
            "omitted_count": 0,
            "ledger": {"claims": {"claim-one": {"status": "planned"}, "claim-two": {"status": "planned"}}},
        }
    }
    state.changes = [PageChange("g", "wiki/device.md", "update", status="published", claim_ids=["claim-one"])]
    assessment = assess_coverage(restart(store, state))
    assert assessment.coverage == "incomplete"
    assert not any("claim-one" in gap for gap in assessment.current_gaps)
    assert any("claim-two" in gap for gap in assessment.current_gaps)
    state.changes[0].claim_ids.append("claim-two")
    assert assess_coverage(restart(store, state)).coverage == "complete"


def test_pending_claim_stays_unresolved_even_if_same_id_has_old_publication(store):
    from doclayer.curator.state import PageChange

    state = job(store)
    state.evidence_selection = {
        "placement": {"omitted_count": 0, "ledger": {"claims": {"claim-one": {"status": "pending"}}}}
    }
    state.changes = [PageChange("g", "wiki/device.md", "update", status="published", claim_ids=["claim-one"])]
    assert assess_coverage(restart(store, state)).coverage == "incomplete"


def test_continuation_publication_hint_requires_current_canonical_proof(store):
    from doclayer.curator import placement

    state = job(store)
    parent = {"claims": {"claim-one": {"status": "planned", "target": "wiki/device.md"}}, "plans": {}}
    ledger = placement.continuation(parent, published={"claim-one": "wiki/device.md"})
    state.evidence_selection = {"placement": {"ledger": ledger}}
    assert assess_coverage(restart(store, state)).coverage == "incomplete"
    assert parent["claims"]["claim-one"]["status"] == "planned"
    ledger["claims"]["claim-one"]["status"] = "covered"
    ledger["claims"]["claim-one"]["coverage"] = {"binding": "current-source-and-page", "anchor": "Actual fact"}
    state.evidence_selection["placement"]["ledger"] = ledger
    assert assess_coverage(restart(store, state)).coverage == "incomplete"
    # An inherited label/hash alone is not a current canonical/source receipt.
    from doclayer.curator.coverage import frontier_context
    from doclayer.curator.curate import SOURCE_SUPPORT_VERSION

    state.base_commit = "fresh-canonical-revision"
    ledger.update(
        version=placement.VERSION, canonical_commit=state.base_commit, frontier_context=frontier_context(state)
    )
    ledger["claims"]["claim-one"]["coverage"].update(
        version=placement.VERSION, path="wiki/device.md", checked=["whole-page"]
    )
    state.evidence_verification["claim-one"] = {
        "status": "supported",
        "supported": True,
        "support_version": SOURCE_SUPPORT_VERSION,
        "source_binding": "current-source",
    }
    assert assess_coverage(restart(store, state)).coverage == "complete"


def retired_frontier(state, identity, status="excluded", **proof):
    from doclayer.curator.coverage import frontier_context

    return {
        "frontier_context": frontier_context(state),
        "claims": {
            identity: {
                "status": status,
                "disposition": {
                    "status": status,
                    "binding": "current-eligibility",
                    "reason": "current disposition",
                    **proof,
                },
            }
        },
    }


def test_current_policy_exclusion_retires_only_its_unknown_with_history(store):
    state = job(store)
    state.coverage = "incomplete"
    note = "source-support unknown excluded: missing source"
    state.coverage_gaps = [note]
    state.evidence_verification = {
        "excluded": {"status": "unknown", "verification_pending": True},
        "other": {"status": "unknown", "reason": "independent missing cache"},
    }
    state.evidence_selection = {"placement": {"ledger": retired_frontier(state, "excluded")}}
    result = assess_coverage(restart(store, state))
    assert result.coverage == "incomplete"
    assert not any("unknown excluded:" in gap for gap in result.current_gaps)
    assert any("unknown other:" in gap for gap in result.current_gaps)
    assert result.historical_gaps == (note,)
    state.evidence_verification["other"] = {"status": "supported"}
    assert assess_coverage(restart(store, state)).coverage == "complete"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda state: state.source_policy.update({"global": {"applicability": {"edition": "new"}}}),
        lambda state: state.exclusions.append("new policy rule"),
        lambda state: setattr(state.sources[0], "role", "new-role"),
        lambda state: state.sources[0].relevance.update({"retained_extract_hash": "changed-source-bytes"}),
    ],
)
def test_obsolete_exclusion_cannot_suppress_unknown_before_new_frontier(store, mutate):
    state = job(store)
    state.evidence_verification = {"excluded": {"status": "unknown", "reason": "source unavailable"}}
    state.evidence_selection = {"placement": {"ledger": retired_frontier(state, "excluded")}}
    assert assess_coverage(state).coverage == "complete"
    mutate(state)
    result = assess_coverage(restart(store, state))
    assert result.coverage == "incomplete" and any("unknown excluded:" in gap for gap in result.current_gaps)


def test_rejection_requires_matching_current_original_source_verdict(store):
    from doclayer.curator.curate import SOURCE_SUPPORT_VERSION

    state = job(store)
    state.evidence_verification = {"rejected": {"status": "unknown", "reason": "source unavailable"}}
    state.evidence_selection = {
        "placement": {
            "ledger": retired_frontier(
                state,
                "rejected",
                status="rejected",
                source_binding="actual-source-binding",
                support_version=SOURCE_SUPPORT_VERSION,
            )
        }
    }
    assert assess_coverage(restart(store, state)).coverage == "incomplete"
    state.evidence_verification["rejected"] = {
        "status": "unsupported",
        "source_binding": "actual-source-binding",
        "support_version": SOURCE_SUPPORT_VERSION,
    }
    assert assess_coverage(restart(store, state)).coverage == "complete"


def test_obsolete_exclusion_needs_fresh_disposition_even_without_unknown_verdict(store):
    state = job(store)
    state.evidence_selection = {"placement": {"ledger": retired_frontier(state, "excluded")}}
    assert assess_coverage(state).coverage == "complete"
    state.exclusions.append("changed current scope")
    assert assess_coverage(restart(store, state)).coverage == "incomplete"


def test_rejection_with_different_original_source_binding_is_unresolved(store):
    from doclayer.curator.curate import SOURCE_SUPPORT_VERSION

    state = job(store)
    state.evidence_verification = {
        "rejected": {
            "status": "unsupported",
            "source_binding": "new-source-binding",
            "support_version": SOURCE_SUPPORT_VERSION,
        }
    }
    state.evidence_selection = {
        "placement": {
            "ledger": retired_frontier(
                state,
                "rejected",
                status="rejected",
                source_binding="old-source-binding",
                support_version=SOURCE_SUPPORT_VERSION,
            )
        }
    }
    assert assess_coverage(restart(store, state)).coverage == "incomplete"


def test_same_identity_applicability_refresh_invalidates_old_exclusion_before_placement(cfg, store):
    from doclayer.curator import curate
    from doclayer.curator.pipeline import Pipeline
    from doclayer.curator.state import Evidence

    state = job(store)
    state.source_policy = {"global": {"applicability": {"edition": "Enterprise"}}}
    source = state.sources[0]
    runner = Pipeline(cfg, store)
    runner._store_extract(state, source, "Device supports export.\n")
    record = Evidence(
        source.id,
        source.label,
        "Complete source unit",
        "quote",
        "Device supports export.",
        applicability={"edition": "Community"},
        quote="Device supports export.",
    )
    identity = curate.evidence_id(record)
    store.append_evidence(state.job_id, [record.to_dict()])
    state.evidence_verification[identity] = {"status": "unknown", "reason": "current source verification required"}
    frontier = {}
    assert not runner._evidence(state, frontier)
    assert frontier[identity]["status"] == "excluded"
    state.evidence_selection["placement"] = {"ledger": retired_frontier(state, identity)}
    assert assess_coverage(state).coverage == "complete"
    original_hash = state.evidence_selection["retained_evidence_hash"]
    record.applicability = {"edition": "Enterprise"}
    assert curate.evidence_id(record) == identity
    store.append_evidence(state.job_id, [record.to_dict()])
    refreshed = {}
    candidates = runner._evidence(state, refreshed)
    assert len(candidates) == 1 and refreshed[identity]["status"] == "pending"
    assert state.evidence_selection["retained_evidence_hash"] != original_hash
    assessment = assess_coverage(restart(store, state))
    assert assessment.coverage == "incomplete"
    assert any(identity in gap for gap in assessment.current_gaps)
