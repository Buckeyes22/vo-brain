"""Pending explicit needs group without deleting raw records or job history."""

import copy
import json

import pytest

from doclayer.gaps import GapLedger

FACETS = ["Library does not perform OCR", "Existing OCR text layer can be extracted"]


def capture(
    ledger,
    question="Does Library perform OCR?",
    cls="missing_coverage",
    subject="library",
    facets=None,
    **kw,
):
    return ledger.submit_event(
        need_class=cls,
        subject=subject,
        representative_question=question,
        facets=FACETS if facets is None else facets,
        **kw,
    )[0]


def test_exact_cross_class_preserves_records_events_and_prefers_local(tmp_path):
    ledger = GapLedger(tmp_path)
    public = capture(
        ledger,
        event_key="public",
        source_candidates=[{"url": "https://example.org/source", "authority": "official"}],
    )
    local = capture(ledger, cls="needs_local_evidence", event_key="local")
    before = copy.deepcopy(public.to_dict())
    events = ledger.events_path(public.gap_id).read_bytes()
    grouped = ledger.reconcile_duplicates()
    assert grouped["grouped"] == [{"gap_id": public.gap_id, "primary": local.gap_id}]
    duplicate = ledger.load(public.gap_id)
    assert duplicate.status == "superseded" and duplicate.superseded_by == local.gap_id
    for field in [
        "representative_question",
        "facets",
        "checked_sources",
        "source_candidates",
        "seen_events",
        "recurrence",
        "first_seen",
    ]:
        assert duplicate.to_dict()[field] == before[field]
    assert ledger.events_path(public.gap_id).read_bytes() == events
    primary = ledger.load(local.gap_id)
    assert primary.need_class == "needs_local_evidence"
    assert primary.source_candidates == public.source_candidates
    assert primary.related_records[0]["question"] == public.representative_question
    assert ledger.summary()["outstanding"] == 1 and ledger.summary()["superseded"] == 1
    bytes_before = {p: ledger.record_path(p).read_bytes() for p in ledger.list_ids()}
    assert ledger.reconcile_duplicates()["grouped"] == []
    assert {p: ledger.record_path(p).read_bytes() for p in ledger.list_ids()} == bytes_before


def test_paraphrase_with_identical_full_facets_and_known_alias_groups(tmp_path):
    ledger = GapLedger(tmp_path)
    unknown = capture(ledger, "Can Library OCR scans?", cls="retrieval_defect")
    known = capture(
        ledger,
        "Does Library read an existing text layer without OCR?",
        catalog_subject=True,
    )
    ledger.reconcile_duplicates()
    primary = ledger.load(unknown.gap_id)
    assert primary.canonical_subject == "library" and primary.identity == "library"
    assert primary.supersession["identity_history"][0]["identity"] == "unknown:library"
    assert ledger.load(known.gap_id).superseded_by == primary.gap_id


@pytest.mark.parametrize("difference", ["facets", "scope", "subject", "unidentified", "empty_facets"])
def test_unproven_different_needs_never_group(tmp_path, difference):
    ledger = GapLedger(tmp_path)
    a_kw = {}
    b_kw = {}
    a_question = "Can Library OCR scans?"
    b_question = "Does Library read existing text layers?"
    if difference == "facets":
        b_kw["facets"] = ["Library extracts images"]
    if difference == "scope":
        b_kw["applicability"] = [{"dimension": "version", "value": "2"}]
    if difference == "subject":
        b_kw["subject"] = "other-library"
    if difference == "unidentified":
        a_kw["subject"] = b_kw["subject"] = None
    if difference == "empty_facets":
        a_kw["facets"] = b_kw["facets"] = []
    capture(ledger, a_question, **a_kw)
    capture(ledger, b_question, **b_kw)
    before = {p: ledger.record_path(p).read_bytes() for p in ledger.list_ids()}
    assert ledger.reconcile_duplicates()["grouped"] == []
    assert {p: ledger.record_path(p).read_bytes() for p in ledger.list_ids()} == before
    assert ledger.summary()["outstanding"] == 2


def test_unidentified_exact_question_can_group_without_inventing_subject(tmp_path):
    ledger = GapLedger(tmp_path)
    a = capture(ledger, subject=None)
    b = capture(ledger, cls="enrichment", subject=None)
    ledger.reconcile_duplicates()
    assert ledger.load(b.gap_id).superseded_by == a.gap_id
    assert ledger.load(a.gap_id).canonical_subject is None


@pytest.mark.parametrize(
    "protected",
    [
        "resolved",
        "curating",
        "researching",
        "awaiting_index",
        "verifying",
        "staged",
        "terminal",
        "lease",
        "active_file",
    ],
)
def test_active_submitted_resolved_or_leased_needs_keep_exact_history(tmp_path, protected):
    ledger = GapLedger(tmp_path)
    a = capture(ledger)
    b = capture(ledger, cls="enrichment")
    if protected in ["staged", "terminal"]:
        b.submission = {
            "state": protected,
            "payload": {"limits": {"max_model_calls": 48}},
        }
        b.job_links = [{"job_id": "job-original0001"}]
    elif protected == "lease":
        b.lease = {"owner": "worker"}
    elif protected == "active_file":
        (ledger.root / "active-submission.json").write_text(json.dumps({"gap_id": b.gap_id}))
    else:
        b.status = protected
    ledger.save(b)
    before = ledger.record_path(b.gap_id).read_bytes()
    assert ledger.reconcile_duplicates()["grouped"] == []
    assert ledger.record_path(b.gap_id).read_bytes() == before
    assert ledger.load(a.gap_id).status == "candidate"


def test_partial_link_restart_recovers_observations_without_recounting(tmp_path, monkeypatch):
    ledger = GapLedger(tmp_path)
    a = capture(ledger)
    b = capture(
        ledger,
        cls="enrichment",
        source_candidates=[{"url": "https://example.org/source"}],
    )
    save = ledger.save

    def fail_primary(record):
        if record.gap_id == a.gap_id:
            raise OSError("power loss before primary copy")
        save(record)

    monkeypatch.setattr(ledger, "save", fail_primary)
    with pytest.raises(OSError):
        ledger.reconcile_duplicates()
    fresh = GapLedger(tmp_path)
    assert fresh.load(b.gap_id).status == "superseded"
    fresh.reconcile_duplicates()
    assert fresh.load(a.gap_id).source_candidates == b.source_candidates
    assert fresh.load(a.gap_id).recurrence == 1 and fresh.load(b.gap_id).recurrence == 1
    capture(fresh, cls="enrichment", checked_sources=[{"path": "wiki/library.md"}])
    fresh.reconcile_duplicates()
    assert fresh.load(a.gap_id).checked_sources == [{"path": "wiki/library.md"}]
    assert fresh.load(b.gap_id).recurrence == 2


def test_resolved_primary_requires_independent_original_need_verification(tmp_path):
    ledger = GapLedger(tmp_path)
    a = capture(ledger)
    b = capture(ledger, cls="enrichment")
    ledger.reconcile_duplicates()
    ledger.transition(a.gap_id, "resolved")
    result = ledger.reconcile_duplicates()
    assert result["verification_required"] == [b.gap_id]
    child = ledger.load(b.gap_id)
    assert child.status == "verifying" and child.superseded_by == a.gap_id
    assert child.resolution_verification == {} and not child.job_links
    assert ledger.summary()["outstanding"] == 1
    assert ledger.reconcile_duplicates()["verification_required"] == []


def test_new_stricter_primary_retargets_existing_related_history(tmp_path):
    ledger = GapLedger(tmp_path)
    a = capture(ledger)
    b = capture(ledger, cls="enrichment")
    ledger.reconcile_duplicates()
    c = capture(ledger, cls="needs_local_evidence")
    ledger.reconcile_duplicates()
    assert ledger.load(a.gap_id).superseded_by == c.gap_id
    assert ledger.load(b.gap_id).superseded_by == c.gap_id
    assert ledger.load(b.gap_id).supersession["prior_links"] == [a.gap_id]
    assert len(ledger.load(c.gap_id).related_records) == 2


@pytest.mark.parametrize("active", [{}, [], {"gap_id": None}, {"gap_id": "foreign"}])
def test_invalid_active_owner_keeps_all_pending_records_unchanged(tmp_path, active):
    ledger = GapLedger(tmp_path)
    capture(ledger)
    capture(ledger, cls="enrichment")
    (ledger.root / "active-submission.json").write_text(json.dumps(active))
    before = {p: ledger.record_path(p).read_bytes() for p in ledger.list_ids()}
    result = ledger.reconcile_duplicates()
    assert result["skipped"] == ["active submission unavailable"]
    assert {p: ledger.record_path(p).read_bytes() for p in ledger.list_ids()} == before


def test_case_sensitive_literal_facets_are_not_semantically_normalized(tmp_path):
    ledger = GapLedger(tmp_path)
    a = capture(ledger, facets=["Fetch /USER endpoint"])
    b = capture(ledger, cls="enrichment", facets=["Fetch /user endpoint"])
    assert ledger.reconcile_duplicates()["grouped"] == []
    assert ledger.load(a.gap_id).status == ledger.load(b.gap_id).status == "candidate"


def test_ambiguous_need_cannot_hide_actionable_pending_work(tmp_path):
    ledger = GapLedger(tmp_path)
    public = capture(ledger)
    ambiguous = capture(ledger, cls="ambiguous")
    before = ledger.record_path(ambiguous.gap_id).read_bytes()
    assert ledger.reconcile_duplicates()["grouped"] == []
    assert ledger.load(public.gap_id).status == "candidate"
    assert ledger.record_path(ambiguous.gap_id).read_bytes() == before
    assert ledger.summary()["outstanding"] == 2


def test_same_question_without_facets_groups_and_adopts_the_facets(tmp_path):
    # The resolver records the bare question; the asking agent records it again
    # with facets. One need, checked against the declared facets.
    ledger = GapLedger(tmp_path)
    bare = capture(ledger, facets=[], event_key="resolver")
    faceted = capture(ledger, event_key="agent")
    grouped = ledger.reconcile_duplicates()["grouped"]
    assert len(grouped) == 1
    primary = ledger.load(grouped[0]["primary"])
    assert sorted(primary.facets) == sorted(FACETS)
    assert {bare.gap_id, faceted.gap_id} == {grouped[0]["gap_id"], primary.gap_id}
    assert ledger.reconcile_duplicates()["skipped"] == []


def test_bare_question_does_not_merge_two_different_facet_sets(tmp_path):
    ledger = GapLedger(tmp_path)
    capture(ledger, facets=[], event_key="resolver")
    capture(ledger, facets=["Fetch /USER endpoint"], event_key="a")
    capture(ledger, facets=["Fetch /user endpoint"], event_key="b")
    ledger.reconcile_duplicates()
    open_records = [r for r in ledger.list_records() if r.status != "superseded"]
    assert sorted(sorted(r.facets) for r in open_records) == [["Fetch /USER endpoint"], ["Fetch /user endpoint"]]
