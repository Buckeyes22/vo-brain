"""Private factual needs never become public documentation questions or imports."""

import httpx
import pytest
from test_resolver_routing import Repo
from test_resolver_routing import counters as counters  # noqa: PLC0414

from doclayer import discovery, gap_worker, resolver
from doclayer.coverage import Catalog, SubjectEntry
from doclayer.curator.transport import Transport
from doclayer.dispatcher import GapDispatcher
from doclayer.gaps import GapLedger

PRIVATE = [
    ("unidentified", "What does /home/user/git/private-project/config.py configure?"),
    ("unidentified", "Inspect /srv/secret-app/current deployment settings."),
    (
        "keystatic",
        "What Keystatic storage and deployed branch applies in /home/user/git/example-cms?",
    ),
    (
        "keystatic",
        "Which CMS event placeholders are published in our example-app installation?",
    ),
    ("unidentified", "Where is my unpublished quarterly report configured?"),
    ("unidentified", "What reports are available?"),
    ("keystatic", "What does /root/private-app/config.py configure for Keystatic?"),
]


@pytest.fixture(autouse=True)
def _private_project_terms(tmp_path, local_profiles):
    """The operator declares the private project name as a local need term."""
    path = tmp_path / "local-profiles.yaml"
    path.write_text("local_need_terms: '\\bexample[- ]app\\b'\n")
    local_profiles(path)


def catalog():
    cat = Catalog(status="ok", revision="a" * 40, policy_version=1)
    for name in ("keystatic", "pypdf"):
        cat.subjects[name] = SubjectEntry(
            slug=name,
            title=name,
            kind="product",
            public_docs={"base_url": f"https://docs.{name}.example/"},
        )
    return cat


@pytest.mark.parametrize("subject,question", PRIVATE)
def test_private_discovery_makes_zero_external_requests(subject, question, monkeypatch):
    requests = []

    def forbidden(request):
        requests.append(request)
        raise AssertionError("private question reached external HTTP")

    monkeypatch.setattr(
        discovery,
        "_qwen_available",
        lambda: (_ for _ in ()).throw(AssertionError("private semantic lookup")),
    )
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        outcome = discovery.discover(
            {
                "gap_id": "gap-private",
                "canonical_subject": subject,
                "identity": subject,
                "representative_question": question,
                "source_candidates": []
                if question == "What reports are available?"
                else [{"url": "https://docs.keystatic.example/", "authority": "official"}],
            },
            catalog(),
            client=client,
            search_client=client,
        )
    assert requests == []
    assert outcome.requests_used == outcome.model_calls == 0
    assert not outcome.seeds
    assert outcome.notes


@pytest.mark.parametrize(
    "subject,question",
    [
        (
            "keystatic",
            "How do I configure Keystatic storage using the official documentation?",
        ),
        ("pypdf", "Does official pypdf documentation say it performs OCR?"),
        (
            "pypdf",
            "What dependencies must be installed to use pypdf according to the official documentation?",
        ),
    ],
)
def test_public_documentation_still_fetches_known_official_source(subject, question):
    requested = []

    def fetch(request):
        requested.append(str(request.url))
        return httpx.Response(
            200,
            text=f"{subject} storage configuration documentation OCR guide dependencies installed",
        )

    with httpx.Client(transport=httpx.MockTransport(fetch)) as client:
        outcome = discovery.discover(
            {
                "gap_id": "gap-public",
                "canonical_subject": subject,
                "identity": subject,
                "representative_question": question,
            },
            catalog(),
            client=client,
            search_client=client,
            use_semantics=False,
        )
    assert requested == [f"https://docs.{subject}.example/"]
    assert outcome.requests_used == 1
    assert outcome.seeds


@pytest.mark.parametrize("subject,question", PRIVATE[:4])
def test_worker_private_question_never_calls_public_discovery(tmp_path, monkeypatch, subject, question):
    ledger = GapLedger(tmp_path)
    row, _ = ledger.submit_event(need_class="missing_coverage", subject=subject, representative_question=question)
    monkeypatch.setattr(
        gap_worker,
        "discover",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("public discover")),
    )
    gap_worker._research_one(ledger, row, catalog(), {}, [])
    current = ledger.load(row.gap_id)
    assert current.status == "needs_local_evidence"
    assert not current.job_links and not current.submission
    from doclayer.local_material import DIRECT_AUTHORING_REASON

    assert DIRECT_AUTHORING_REASON in ledger.events_path(row.gap_id).read_text()


class TransportForbidden(Transport):
    def call(self, *a, **kw):
        raise AssertionError("private public-only payload submitted")


@pytest.mark.parametrize("subject,question", PRIVATE[:3])
def test_dispatcher_refuses_private_need_with_only_official_urls(tmp_path, subject, question):
    ledger = GapLedger(tmp_path)
    row, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject=subject,
        representative_question=question,
        source_candidates=[{"url": "https://docs.keystatic.example/", "authority": "official"}],
    )
    ledger.transition(row.gap_id, "ready_for_curation")
    (tmp_path / "curator-ready").write_text("ready\n")
    result = GapDispatcher(ledger, catalog=catalog(), transport=TransportForbidden()).dispatch(row.gap_id)
    assert result.action != "submitted"
    assert not ledger.load(row.gap_id).job_links
    assert not ledger.load(row.gap_id).submission


@pytest.mark.asyncio
@pytest.mark.parametrize("subject,question", PRIVATE)
async def test_resolver_force_cannot_send_private_question_to_context7(
    tmp_path, monkeypatch, counters, subject, question
):
    monkeypatch.setattr(resolver, "_route_inputs", lambda root: (catalog(), []))
    answer = await resolver.resolve(
        question,
        product=None if subject == "unidentified" else subject,
        repo=Repo(tmp_path, []),
        live=False,
        capture=False,
        force=True,
    )
    assert counters["public_docs"] == 0
    assert answer.source_calls.get("public_docs", 0) == 0
    assert "answered" not in answer.dispositions
    assert answer.skipped_sources.get("public_docs")


@pytest.mark.asyncio
async def test_resolver_public_product_docs_remain_allowed(tmp_path, monkeypatch, counters):
    monkeypatch.setattr(resolver, "_route_inputs", lambda root: (catalog(), []))
    await resolver.resolve(
        "How do I configure Keystatic storage?",
        product="keystatic",
        repo=Repo(tmp_path, []),
        live=False,
        capture=False,
        force=True,
    )
    assert counters["public_docs"] == 1


@pytest.mark.parametrize("state", ["staged", "uncertain"])
def test_preexisting_private_public_payload_is_held_without_rewriting(tmp_path, state):
    import copy

    ledger = GapLedger(tmp_path)
    question = "What Keystatic storage configuration applies to /srv/private-app?"
    row, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="keystatic",
        representative_question=question,
        source_candidates=[{"url": "https://docs.keystatic.example/", "authority": "official"}],
    )
    payload = {
        "urls": ["https://docs.keystatic.example/"],
        "intent": question,
        "idempotency_key": "old-immutable-private",
        "limits": {"max_model_calls": 50},
    }
    row.status = "ready_for_curation"
    row.submission = {"state": state, "payload": copy.deepcopy(payload), "attempt": 1}
    ledger.save(row)
    (tmp_path / "curator-ready").write_text("ready\n")
    result = GapDispatcher(ledger, catalog=catalog(), transport=TransportForbidden()).dispatch(row.gap_id)
    assert result.action != "submitted"
    assert ledger.load(row.gap_id).submission["payload"] == payload
    assert not ledger.load(row.gap_id).job_links


def test_unknown_report_without_subject_or_source_is_not_public():
    from doclayer.question_scope import public_discovery_allowed

    assert not public_discovery_allowed(
        {
            "identity": "unknown:unidentified",
            "representative_question": "Where is the unpublished report configured?",
        },
        catalog(),
    )


def test_public_api_endpoint_and_installation_instructions_are_not_private_paths():
    from doclayer.question_scope import private_question, public_discovery_allowed

    question = "What does the official Keystatic API /api/storage endpoint configure?"
    assert not private_question(question, subject="keystatic", entry=catalog().subjects["keystatic"])
    assert public_discovery_allowed(
        {"canonical_subject": "keystatic", "representative_question": question},
        catalog(),
    )


@pytest.mark.asyncio
async def test_force_does_not_promote_generic_product_doc_to_private_configuration(tmp_path, monkeypatch, counters):
    from test_resolver_routing import page

    cat = catalog()
    monkeypatch.setattr(resolver, "_route_inputs", lambda root: (cat, []))
    repo = Repo(
        tmp_path,
        [
            page(
                "wiki/keystatic.md",
                "# Keystatic\n\nKeystatic supports GitHub storage for content repositories.\n",
                title="Keystatic",
            )
        ],
    )
    answer = await resolver.resolve(
        "What Keystatic storage is configured in our example-app installation?",
        product="keystatic",
        repo=repo,
        live=False,
        capture=False,
        force=True,
    )
    assert counters["public_docs"] == 0
    assert "answered" not in answer.dispositions
    # Generic support may be retained as explicitly scoped reference evidence;
    # it cannot become a claimed observation of this private installation.
    assert not any("GitHub" in str(f.get("value", "")) and f.get("_scope", "") == "" for f in answer.git_facts)


class RecordingSubmit(Transport):
    def __init__(self):
        self.calls = []

    def call(self, op, payload, **kw):
        self.calls.append((op, payload))
        return {"ok": True, "job_id": "job-1111111111", "created": True}


def test_private_local_need_is_never_submitted_even_with_attributed_text_and_official_urls(
    tmp_path,
):
    from local_sources import local_source

    from doclayer.local_material import DIRECT_AUTHORING_REASON

    ledger = GapLedger(tmp_path)
    material = local_source(
        "Committed private configuration@" + "a" * 40,
        "Observed declaration: this private installation stores content locally.",
    )
    row, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="keystatic",
        representative_question="What storage is configured in /srv/private-app?",
        source_candidates=[
            material,
            {"url": "https://docs.keystatic.example/", "authority": "official"},
        ],
    )
    ledger.transition(row.gap_id, "ready_for_curation")
    (tmp_path / "curator-ready").write_text("ready\n")
    transport = RecordingSubmit()
    dispatcher = GapDispatcher(ledger, catalog=catalog(), transport=transport)
    outcome = dispatcher.dispatch(row.gap_id)
    assert outcome.action == "skipped" and outcome.detail == DIRECT_AUTHORING_REASON
    assert not [op for op, _ in transport.calls if op == "submit"]
    current = ledger.load(row.gap_id)
    assert current.status == "needs_local_evidence" and not current.submission


def test_unsafe_staged_payload_remains_held_even_after_local_evidence_arrives(tmp_path):
    import copy

    from local_sources import local_source

    ledger = GapLedger(tmp_path)
    row, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="keystatic",
        representative_question="What is configured in /srv/private-app?",
        source_candidates=[local_source("Local declaration", "The application stores content locally.")],
    )
    row.status = "ready_for_curation"
    row.submission = {
        "state": "uncertain",
        "payload": {
            "urls": ["https://docs.keystatic.example/"],
            "intent": "private old question",
        },
        "reservation": {"calls": 50, "attempt_id": "old-attempt"},
    }
    row.submission_history = [{"attempt": 0, "payload": {"intent": "historical"}}]
    ledger.save(row)
    old_submission = copy.deepcopy(row.submission)
    old_history = copy.deepcopy(row.submission_history)
    result = GapDispatcher(ledger, catalog=catalog(), transport=TransportForbidden()).dispatch(row.gap_id)
    assert result.action == "skipped"
    current = ledger.load(row.gap_id)
    assert current.status == "needs_local_evidence"
    assert current.submission == old_submission
    assert current.submission_history == old_history


def test_existing_accepted_private_job_can_be_observed_without_resubmission(tmp_path):
    import json

    ledger = GapLedger(tmp_path)
    row, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="keystatic",
        representative_question="What is configured in /srv/private-app?",
    )
    old_payload = {
        "urls": ["https://docs.keystatic.example/"],
        "intent": "private old question",
    }
    row.status = "curating"
    row.attempts = 1
    row.submission = {
        "state": "accepted",
        "payload": old_payload,
        "attempt_id": "immutable-attempt",
        "job_id": "job-1111111111",
    }
    ledger.save(row)
    (tmp_path / "active-submission.json").write_text(
        json.dumps(
            {
                "gap_id": row.gap_id,
                "attempt_id": "immutable-attempt",
                "job_id": "job-1111111111",
                "calls": 50,
            }
        )
    )
    called = []

    class StatusOnly(Transport):
        def call(self, op, payload, **kw):
            called.append((op, payload))
            assert op == "status"
            return {
                "ok": True,
                "job": {"job_id": "job-1111111111", "status": "processing"},
            }

    result = GapDispatcher(ledger, catalog=catalog(), transport=StatusOnly()).dispatch(row.gap_id)
    assert result.action == "skipped"
    assert called == [("status", {"job_id": "job-1111111111"})]
    assert ledger.load(row.gap_id).submission["payload"] == old_payload
    assert (tmp_path / "active-submissions" / f"{row.gap_id}.json").exists()


def test_dispatcher_keeps_observing_accepted_private_attempt(tmp_path):
    import copy
    import json

    from local_sources import local_source

    ledger = GapLedger(tmp_path)
    row, _ = ledger.submit_event(
        need_class="needs_local_evidence",
        subject="brain",
        representative_question="Where is the worker1 brain curator installed?",
        source_candidates=[local_source("Committed brain notes", "The curator runs on worker1.")],
    )
    payload = GapDispatcher(ledger, transport=TransportForbidden()).build_payload(ledger.load(row.gap_id), [], 1)
    submission = {
        "state": "accepted",
        "attempt": 1,
        "attempt_id": payload["idempotency_key"],
        "payload": payload,
        "job_id": "job-0000000001",
        "reservation": {"calls": 50, "attempt_id": payload["idempotency_key"]},
    }

    def seed(r):
        r.status = "queued"
        r.attempts = 1
        r.submission = copy.deepcopy(submission)

    ledger.update(row.gap_id, seed)
    (tmp_path / "curator-ready").write_text("ready\n")
    (tmp_path / "active-submission.json").write_text(
        json.dumps(
            {
                "gap_id": row.gap_id,
                "attempt_id": payload["idempotency_key"],
                "job_id": "job-0000000001",
                "calls": 50,
            }
        )
    )
    calls = []

    class StatusOnly(Transport):
        def call(self, op, payload, **kw):
            calls.append(op)
            return {
                "ok": True,
                "job": {"job_id": "job-0000000001", "status": "processing"},
            }

    outcome = GapDispatcher(ledger, transport=StatusOnly()).dispatch(row.gap_id)
    assert outcome.action == "skipped" and outcome.job_id == "job-0000000001"
    assert calls == ["status"]
    current = ledger.load(row.gap_id)
    assert current.status != "needs_local_evidence"
    assert current.submission == {**submission, "job_id": "job-0000000001"}


def test_dispatcher_private_gap_without_active_attempt_gets_direct_authoring_reason(tmp_path):
    from local_sources import local_source

    from doclayer.local_material import DIRECT_AUTHORING_REASON

    ledger = GapLedger(tmp_path)
    (tmp_path / "curator-ready").write_text("ready\n")
    plain, _ = ledger.submit_event(
        need_class="needs_local_evidence",
        subject="brain",
        representative_question="Where is the worker1 brain curator installed?",
    )
    texted, _ = ledger.submit_event(
        need_class="needs_local_evidence",
        subject="keystatic",
        representative_question="What storage is configured in /srv/private-app?",
        source_candidates=[local_source("Committed private config", "Stores content locally.")],
    )
    for row in (plain, texted):
        ledger.transition(row.gap_id, "ready_for_curation")
        outcome = GapDispatcher(ledger, catalog=catalog(), transport=TransportForbidden()).dispatch(row.gap_id)
        assert outcome.action == "skipped" and outcome.detail == DIRECT_AUTHORING_REASON
        assert ledger.load(row.gap_id).status == "needs_local_evidence"
        events = ledger.events_path(row.gap_id).read_text()
        assert DIRECT_AUTHORING_REASON in events
        assert "attributed local evidence" not in events


@pytest.mark.parametrize("state", ["staged", "reserved", "uncertain"])
def test_inflight_private_text_attempt_replays_exact_payload_not_parked(tmp_path, state):
    from local_sources import local_source
    from test_gap_contract_recovery import RealOps

    from doclayer.dispatcher import LOCAL_SOURCE_POLICY
    from doclayer.private_writer import PRIVATE_ORIGIN

    ledger = GapLedger(tmp_path / "gaps")
    ledger.root.mkdir()
    (ledger.root / "curator-ready").touch()
    ops = RealOps(tmp_path)
    dispatcher = GapDispatcher(ledger, transport=ops)
    source = local_source(
        "Committed Widget setup",
        "Widget's configured edition is Community; runtime edition remains unknown.",
        # Staged material that is not the private writer's is never sent.
        **({} if state == "staged" else {"origin": PRIVATE_ORIGIN}),
    )
    row, _ = ledger.submit_event(
        need_class="needs_local_evidence",
        subject="widget",
        representative_question="Which Widget edition is configured?",
        source_candidates=[source],
    )
    payload = dispatcher.build_payload(ledger.load(row.gap_id), [], 1)
    assert payload["source_policy"] == LOCAL_SOURCE_POLICY and payload["urls"] == []
    submission = {
        "state": state,
        "attempt": 1,
        "attempt_id": payload["idempotency_key"],
        "payload": payload,
        "policy_version": dispatcher.policy_version,
        "input_sources": [{"text_label": source["label"], "source_hash": source["source_hash"]}],
        "ordered_urls": [],
    }
    if state != "staged":
        submission["reservation"] = {"calls": 50, "attempt_id": payload["idempotency_key"]}

    def seed(r):
        r.submission = submission
        r.status = "retry_wait" if state == "uncertain" else "ready_for_curation"
        r.attempts = 0 if state == "staged" else 1

    ledger.update(row.gap_id, seed)
    result = dispatcher.dispatch(row.gap_id)
    if state == "staged":
        # Prepared but never sent: the curator never receives private material.
        from doclayer.local_material import DIRECT_AUTHORING_REASON

        assert result.action == "skipped"
        assert not [c for c in ops.calls if c[0] == "submit"]
        assert len(ops.store.list_ids()) == 0
        parked = ledger.load(row.gap_id)
        assert parked.status == "needs_local_evidence"
        assert DIRECT_AUTHORING_REASON in ledger.events_path(row.gap_id).read_text()
        assert parked.submission["state"] == "abandoned"
        assert parked.submission["abandoned_reason"] == DIRECT_AUTHORING_REASON
        assert parked.submission["payload"] == payload  # retained, not deleted
        assert dispatcher.active() is None
        return
    assert result.action == "submitted"
    submits = [p for op, p in ops.calls if op == "submit"]
    assert len(submits) == 1 and len(ops.store.list_ids()) == 1
    assert submits[0]["idempotency_key"] == payload["idempotency_key"]
    assert submits[0]["texts"] == payload["texts"] and submits[0]["urls"] == []
    assert submits[0]["source_policy"] == LOCAL_SOURCE_POLICY
    current = ledger.load(row.gap_id)
    assert current.status == "curating"
    assert current.attempts == 1 and len(current.job_links) == 1
    assert current.submission["attempt_id"] == payload["idempotency_key"]
    assert not current.submission_history


def test_accepted_private_attempt_without_active_slot_idempotently_resubmits(tmp_path):
    """An accepted attempt whose job receipt was lost (no job_id, no slot file)
    is neither parked nor sent as a new job: the same key replays."""
    from local_sources import local_source
    from test_gap_contract_recovery import RealOps

    from doclayer.dispatcher import LOCAL_SOURCE_POLICY
    from doclayer.private_writer import PRIVATE_ORIGIN

    ledger = GapLedger(tmp_path / "gaps")
    ledger.root.mkdir()
    (ledger.root / "curator-ready").touch()
    ops = RealOps(tmp_path)
    dispatcher = GapDispatcher(ledger, transport=ops)
    source = local_source(
        "Committed Widget setup",
        "Widget's configured edition is Community; runtime edition remains unknown.",
        origin=PRIVATE_ORIGIN,
    )
    row, _ = ledger.submit_event(
        need_class="needs_local_evidence",
        subject="widget",
        representative_question="Which Widget edition is configured?",
        source_candidates=[source],
    )
    payload = dispatcher.build_payload(ledger.load(row.gap_id), [], 1)
    submission = {
        "state": "accepted",
        "attempt": 1,
        "attempt_id": payload["idempotency_key"],
        "payload": payload,
        "policy_version": dispatcher.policy_version,
        "input_sources": [{"text_label": source["label"], "source_hash": source["source_hash"]}],
        "ordered_urls": [],
        "reservation": {"calls": 50, "attempt_id": payload["idempotency_key"]},
    }

    def seed(r):
        r.submission = submission
        r.status = "ready_for_curation"
        r.attempts = 1

    ledger.update(row.gap_id, seed)
    assert not (ledger.root / "active-submission.json").exists()
    result = dispatcher.dispatch(row.gap_id)
    assert result.action == "submitted"
    submits = [p for op, p in ops.calls if op == "submit"]
    assert len(submits) == 1 and len(ops.store.list_ids()) == 1
    assert submits[0]["idempotency_key"] == payload["idempotency_key"]
    assert submits[0]["source_policy"] == LOCAL_SOURCE_POLICY and submits[0]["urls"] == []
    current = ledger.load(row.gap_id)
    assert current.status == "curating"
    assert current.attempts == 1 and len(current.job_links) == 1
    assert current.submission["attempt_id"] == payload["idempotency_key"]
    assert not current.submission_history


@pytest.mark.asyncio
async def test_public_dependency_guide_not_misclassified_as_installed_host_state(tmp_path, monkeypatch, counters):
    from doclayer.question_scope import private_question

    question = "What dependencies must be installed to use pypdf according to the official documentation?"
    assert not private_question(question, subject="pypdf", entry=catalog().subjects["pypdf"])
    monkeypatch.setattr(resolver, "_route_inputs", lambda root: (catalog(), []))
    await resolver.resolve(
        question,
        product="pypdf",
        repo=Repo(tmp_path, []),
        live=False,
        capture=False,
        force=True,
    )
    assert counters["public_docs"] == 1


def test_private_source_policy_is_valid_blocks_all_urls_and_allows_local_input(
    tmp_path,
):
    from local_sources import local_source

    from doclayer.curator import policy

    ledger = GapLedger(tmp_path)
    row, _ = ledger.submit_event(
        need_class="needs_local_evidence",
        subject="keystatic",
        representative_question="What is configured in /srv/private-app?",
        source_candidates=[
            local_source(
                "Local file",
                "See https://docs.keystatic.example/config for generic guidance.",
            )
        ],
    )
    payload = GapDispatcher(ledger, catalog=catalog()).build_payload(row, [], 1)
    boundary = policy.validate_policy(payload["source_policy"])
    for url in (
        "https://docs.keystatic.example/config",
        "http://arbitrary.example/other",
        "https://another.example/private",
    ):
        assert policy.url_decision(url, boundary)[0] is False
    assert policy.url_decision(None, boundary)[0] is True


@pytest.mark.parametrize(
    "old_policy",
    [
        {},
        {"exclude": [{"domain": "docs.keystatic.example"}]},
        {"exclude": [{"domain": "*"}], "exceptions": [{"domain": "leak.example"}]},
    ],
)
def test_private_text_attempt_without_universal_owner_boundary_held_immutable(tmp_path, old_policy):
    import copy

    from local_sources import local_source

    ledger = GapLedger(tmp_path)
    row, _ = ledger.submit_event(
        need_class="needs_local_evidence",
        subject="private-app",
        representative_question="What is configured in /srv/private-app?",
        source_candidates=[local_source("Local file", "See https://leak.example/config for reference.")],
    )
    row.status = "ready_for_curation"
    row.submission = {
        "state": "staged",
        "payload": {
            "urls": [],
            "texts": [{"label": "Local file", "text": "original"}],
            "source_policy": old_policy,
        },
    }
    ledger.save(row)
    before = copy.deepcopy(row.submission)
    assert (
        GapDispatcher(ledger, catalog=catalog(), transport=TransportForbidden()).dispatch(row.gap_id).action
        == "skipped"
    )
    after = ledger.load(row.gap_id).submission
    # The staged payload is retained byte-for-byte but recorded as abandoned.
    assert after["state"] == "abandoned" and after["abandoned_reason"]
    assert {k: v for k, v in after.items() if k not in ("state", "abandoned_reason")} == {
        k: v for k, v in before.items() if k != "state"
    }


@pytest.mark.asyncio
async def test_native_discovery_keeps_submitted_local_text_links_out_of_public_fetch(
    tmp_path,
):
    from doclayer.curator.config import CuratorConfig
    from doclayer.curator.intake import make_text_source
    from doclayer.curator.jobstore import JobStore
    from doclayer.curator.pipeline import Pipeline
    from doclayer.curator.state import JobState
    from doclayer.dispatcher import LOCAL_SOURCE_POLICY

    cfg = CuratorConfig()
    cfg.paths.cache = str(tmp_path / "cache")
    cfg.paths.jobs = str(tmp_path / "jobs")
    cfg.paths.brain_checkout = str(tmp_path / "brain")
    store = JobStore(cfg.paths.jobs)
    text = "Observed private configuration: local content storage. Generic reference https://docs.keystatic.example/config does not establish this deployment."
    source = make_text_source("Private configuration observation", text)
    source.status = "fetched"
    state = JobState(
        job_id="",
        idempotency_key="local-only-test",
        intent="Document /srv/private-app configuration",
        stage="discover",
        sources=[source],
        source_policy=LOCAL_SOURCE_POLICY,
        limits={"max_model_calls": 50},
    )
    state, _ = store.create(state)
    called = []

    def forbidden(request):
        called.append(str(request.url))
        raise AssertionError("private local import made external HTTP/inference request")

    pipeline = Pipeline(
        cfg,
        store,
        fetch_transport=httpx.MockTransport(forbidden),
        inference_transport=httpx.MockTransport(forbidden),
    )
    pipeline._store_extract(state, source, text)
    await pipeline._stage_discover(state)
    assert called == []
    assert state.usage.model_calls == 0
    assert state.sources == [source]
    assert source.url is None
    assert pipeline._extract_text(state, source) == text


@pytest.mark.parametrize("kind,local_first", [("account", False), ("install", False), ("product", True)])
def test_catalog_only_private_scope_controls_final_dispatch_and_payload(tmp_path, kind, local_first):
    cat = catalog()
    cat.subjects["harbor"] = SubjectEntry(slug="harbor", title="Harbor", kind=kind, local_first=local_first)
    ledger = GapLedger(tmp_path)
    row, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="harbor",
        catalog_subject=True,
        representative_question="Which storage does Harbor use?",
        source_candidates=[{"url": "https://docs.harbor.example/", "authority": "official"}],
    )
    ledger.transition(row.gap_id, "ready_for_curation")
    (tmp_path / "curator-ready").write_text("ready\n")
    dispatcher = GapDispatcher(ledger, catalog=cat, transport=TransportForbidden())
    payload = dispatcher.build_payload(row, ["https://docs.harbor.example/"], 1)
    assert payload["urls"] == []
    assert payload["source_policy"]["exclude"][0]["domain"] == "*"
    assert dispatcher.dispatch(row.gap_id).action == "skipped"
    assert ledger.load(row.gap_id).status == "needs_local_evidence"
    assert not ledger.load(row.gap_id).submission


def test_dispatcher_lazy_catalog_loaded_once_with_owning_paths(tmp_path, monkeypatch):
    from doclayer import dispatcher as module

    cat = catalog()
    cat.subjects["harbor"] = SubjectEntry(slug="harbor", title="Harbor", kind="account")
    loaded = []
    monkeypatch.setattr(module, "load_catalog", lambda *args: loaded.append(args) or cat)
    ledger = GapLedger(tmp_path)
    row, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject="harbor",
        representative_question="Which storage does Harbor use?",
    )
    dispatcher = GapDispatcher(ledger)
    assert dispatcher.build_payload(row, ["https://docs.harbor.example"], 1)["urls"] == []
    assert dispatcher.build_payload(row, ["https://docs.harbor.example"], 1)["urls"] == []
    assert loaded == [
        (
            module.settings.brain_repo,
            module.settings.catalog_cache_dir,
            module.settings.routing_policy_path,
        )
    ]


@pytest.mark.parametrize(
    "question,allowed",
    [
        ("What does official Forgejo documentation say about branch protection?", True),
        ("Which Forgejo plan or configuration is enabled?", False),
        (
            "What does official Forgejo documentation establish about /srv/private-forgejo configuration?",
            False,
        ),
    ],
)
def test_local_first_public_product_guidance_distinct_from_private_installation(question, allowed):
    from doclayer.question_scope import public_discovery_allowed

    cat = catalog()
    cat.subjects["forgejo"] = SubjectEntry(slug="forgejo", title="Forgejo", kind="product", local_first=True)
    assert (
        public_discovery_allowed({"canonical_subject": "forgejo", "representative_question": question}, cat) is allowed
    )


def test_stale_official_attachment_cannot_identify_ambiguous_reports_question():
    from doclayer.question_scope import public_discovery_allowed

    gap = {
        "identity": "unknown:unidentified",
        "representative_question": "What reports are available?",
        "source_candidates": [{"url": "https://docs.keystatic.example/", "authority": "official"}],
    }
    assert not public_discovery_allowed(gap, catalog())
    gap["representative_question"] = "docs for Keystatic"
    assert public_discovery_allowed(gap, catalog())
