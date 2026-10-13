"""Optional source admission and editorial incorporation have separate proof requirements."""

import pytest

from doclayer.curator.coverage import assess_coverage, frontier_context
from doclayer.curator.curate import SOURCE_SUPPORT_VERSION
from doclayer.curator.state import JobState


def pending_state():
    state = JobState("job-optional123", "optional", "Device")
    state.evidence_selection["wiki/device.md"] = {
        "pending_optional": ["fact-1"],
        "pending_optional_proofs": {
            "fact-1": {"source_binding": "source-current", "support_version": SOURCE_SUPPORT_VERSION},
        },
        "frontier_context": frontier_context(state),
    }
    return state


def proof(**changes):
    return dict(
        supported=True,
        status="supported",
        source_binding="source-current",
        support_version=SOURCE_SUPPORT_VERSION,
        **changes,
    )


def optional_gaps(state):
    return [gap for gap in assess_coverage(state).current_gaps if "optional source claims" in gap]


def test_legacy_draft_without_placement_frontier_keeps_optional_backlog_visible():
    state = pending_state()
    assert assess_coverage(state).coverage == "incomplete"
    assert optional_gaps(state)
    assert state.coverage == "complete"  # assessment does not rewrite history


def test_current_source_proof_resolves_source_gap_but_keeps_editorial_backlog():
    state = pending_state()
    state.evidence_verification["fact-1"] = proof()
    assert not optional_gaps(state)
    assert assess_coverage(state).coverage == "incomplete"
    assert any("await editorial disposition" in gap for gap in assess_coverage(state).current_gaps)


def test_old_generation_changed_source_and_pending_proofs_cannot_retire_optional_work():
    for updates in (
        {"source_binding": "old-source"},
        {"support_version": SOURCE_SUPPORT_VERSION - 1},
        {"verification_pending": True},
    ):
        state = pending_state()
        state.evidence_verification["fact-1"] = {**proof(), **updates}
        assert optional_gaps(state)


def test_changed_policy_invalidates_optional_selection_until_recomputed():
    state = pending_state()
    state.evidence_verification["fact-1"] = proof()
    state.source_policy = {"global": {"applicability": {"edition": "Community"}}}
    assert optional_gaps(state)


@pytest.mark.parametrize("status", ["excluded", "rejected"])
def test_fresh_trusted_disposition_retires_old_optional_snapshot(status):
    state = pending_state()
    state.source_policy = {"exclude": [{"domain": "unneeded.example"}]}
    disposition = {
        "status": status,
        "binding": "current-policy-decision",
        "support_version": SOURCE_SUPPORT_VERSION,
        "source_binding": "source-current",
    }
    state.evidence_selection["placement"] = {
        "ledger": {
            "frontier_context": frontier_context(state),
            "claims": {"fact-1": {"status": status, "disposition": disposition}},
        }
    }
    if status == "rejected":
        state.evidence_verification["fact-1"] = {**proof(), "supported": False, "status": "unsupported"}
    assert not optional_gaps(state)
    assert assess_coverage(state).coverage == "complete"


def test_stale_exclusion_cannot_retire_optional_work_after_policy_change():
    state = pending_state()
    state.evidence_selection["placement"] = {
        "ledger": {
            "frontier_context": frontier_context(state),
            "claims": {
                "fact-1": {
                    "status": "excluded",
                    "disposition": {"status": "excluded", "binding": "old-policy-decision"},
                }
            },
        }
    }
    state.source_policy = {"global": {"applicability": {"edition": "Community"}}}
    assert optional_gaps(state)


def test_actual_publication_retires_current_supported_optional_claim():
    from doclayer.curator.state import PageChange

    state = pending_state()
    state.evidence_verification["fact-1"] = proof()
    state.changes.append(
        PageChange(
            group_id="group-device", path="wiki/device.md", action="update", status="published", claim_ids=["fact-1"]
        )
    )
    assert assess_coverage(state).coverage == "complete"
    state.evidence_verification["fact-1"]["source_binding"] = "obsolete"
    assert assess_coverage(state).coverage == "incomplete"


def test_covered_requires_current_complete_placement_proof():
    from doclayer.curator.placement import VERSION

    state = pending_state()
    state.evidence_verification["fact-1"] = proof()
    state.base_commit = "canonical-revision"
    row = {
        "status": "covered",
        "coverage": {
            "version": VERSION,
            "binding": "page-and-claim-hash",
            "path": "wiki/device.md",
            "anchor": "source-backed fact",
            "checked": ["page", "section"],
        },
    }
    state.evidence_selection["placement"] = {
        "ledger": {
            "frontier_context": frontier_context(state),
            "version": VERSION,
            "canonical_commit": state.base_commit,
            "claims": {"fact-1": row},
        }
    }
    assert assess_coverage(state).coverage == "complete"
    row["coverage"]["checked"] = []
    assert assess_coverage(state).coverage == "incomplete"
    row["coverage"]["checked"] = ["page", "section"]
    state.source_policy = {"scope": "changed"}
    assert assess_coverage(state).coverage == "incomplete"


def test_known_unsupported_optional_claim_still_requires_editorial_rejection():
    state = pending_state()
    state.evidence_verification["fact-1"] = {**proof(), "supported": False, "status": "unsupported"}
    assert not optional_gaps(state)
    assert assess_coverage(state).coverage == "incomplete"


def test_legacy_covered_row_without_recorded_canonical_receipt_remains_pending():
    from doclayer.curator.placement import VERSION

    state = pending_state()
    state.evidence_verification["fact-1"] = proof()
    state.evidence_selection["placement"] = {
        "ledger": {
            "frontier_context": frontier_context(state),
            "version": VERSION,
            "claims": {
                "fact-1": {
                    "status": "covered",
                    "coverage": {
                        "version": VERSION,
                        "binding": "hash",
                        "path": "wiki/device.md",
                        "anchor": "literal",
                        "checked": ["whole-page"],
                    },
                }
            },
        }
    }
    assert assess_coverage(state).coverage == "incomplete"


def test_global_legacy_covered_row_cannot_hide_without_optional_snapshot():
    from doclayer.curator.placement import VERSION

    state = pending_state()
    state.evidence_selection.pop("wiki/device.md")
    state.evidence_verification["fact-1"] = proof()
    ledger = {
        "claims": {
            "fact-1": {
                "status": "covered",
                "coverage": {
                    "binding": "page-hash",
                    "path": "wiki/device.md",
                    "anchor": "literal",
                    "checked": ["whole-page"],
                },
            }
        }
    }
    state.evidence_selection["placement"] = {"ledger": ledger}
    assert assess_coverage(state).coverage == "incomplete"
    state.base_commit = "actual-fetched-revision"
    ledger.update(version=VERSION, canonical_commit=state.base_commit, frontier_context=frontier_context(state))
    ledger["claims"]["fact-1"]["coverage"]["version"] = VERSION
    assert assess_coverage(state).coverage == "complete"
    state.evidence_verification["fact-1"]["support_version"] -= 1
    assert assess_coverage(state).coverage == "incomplete"
