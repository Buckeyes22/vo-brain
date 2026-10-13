"""Placement eligibility uses actual current source and policy proof."""

from doclayer.curator import curate
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import Evidence, JobState, SourceRecord


def fixture(cfg, store):
    state = store.create(JobState(job_id="", idempotency_key="", intent="Device"))[0]
    state.sources = [SourceRecord("manual", "text", "Manual")]
    record = Evidence(
        "manual",
        "Manual",
        "#Device",
        "quote",
        "Device outputs images.",
        quote="Device outputs images.",
        support_status="supported",
    )
    store.append_evidence(state.job_id, [record.to_dict()])
    runner = Pipeline(cfg, store)
    text = "# Device\nDevice outputs images.\n"
    runner._store_extract(state, state.sources[0], text)
    return state, record, runner, text


def test_raw_marker_is_pending_and_only_current_rejection_can_retire_claim(cfg, store):
    state, record, runner, text = fixture(cfg, store)
    identity = curate.evidence_id(record)
    frontier = {}
    assert runner._evidence(state, frontier)[0].support_status == "unknown"
    assert frontier[identity]["status"] == "pending"
    state.evidence_verification[identity] = {
        "status": "unsupported",
        "supported": False,
        "reason": "Wrong edition.",
        "support_version": curate.SOURCE_SUPPORT_VERSION,
        "source_binding": curate.source_support_binding(record, {"manual": text}),
    }
    frontier = {}
    assert not runner._evidence(state, frontier)
    assert frontier[identity]["status"] == "rejected"
    old_binding = frontier[identity]["binding"]
    runner._store_extract(state, state.sources[0], text + "Edition changed.\n")
    assert runner._evidence(state, frontier)[0].support_status == "unknown"
    assert frontier[identity]["status"] == "pending"
    assert frontier[identity]["binding"] != old_binding


def test_explicit_exclusion_is_reversible_without_inventing_source_truth(cfg, store):
    state, record, runner, text = fixture(cfg, store)
    identity = curate.evidence_id(record)
    state.source_policy = {"global": {"forbidden_claims": ["outputs images"]}}
    frontier = {}
    assert not runner._evidence(state, frontier)
    assert frontier[identity]["status"] == "excluded"
    old_binding = frontier[identity]["binding"]
    state.source_policy = {}
    assert runner._evidence(state, frontier)[0].support_status == "unknown"
    assert frontier[identity]["status"] == "pending"
    assert frontier[identity]["binding"] != old_binding


def test_pending_verification_cannot_be_admitted_as_supported(cfg, store):
    state, record, runner, text = fixture(cfg, store)
    identity = curate.evidence_id(record)
    state.evidence_verification[identity] = {
        "status": "supported",
        "supported": True,
        "verification_pending": True,
        "support_version": curate.SOURCE_SUPPORT_VERSION,
        "source_binding": curate.source_support_binding(record, {"manual": text}),
    }
    frontier = {}
    assert runner._evidence(state, frontier)[0].support_status == "unknown"
    assert frontier[identity]["status"] == "pending"


def test_append_only_refresh_uses_latest_provenance_without_duplicate_admission(cfg, store):
    state, record, runner, text = fixture(cfg, store)
    identity = curate.evidence_id(record)
    old_binding = curate.source_support_binding(record, {"manual": text})
    state.evidence_verification[identity] = {
        "status": "supported",
        "supported": True,
        "support_version": curate.SOURCE_SUPPORT_VERSION,
        "source_binding": old_binding,
    }
    record.quote = "Device outputs images. Only in laboratory installations."
    store.append_evidence(state.job_id, [record.to_dict()])
    frontier = {}
    records = runner._evidence(state, frontier)
    assert len(records) == 1 and len(frontier) == 1
    assert records[0].original_quote == record.quote
    assert records[0].support_status == "unknown"
    assert frontier[identity]["status"] == "pending"
