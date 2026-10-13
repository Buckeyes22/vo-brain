"""Real owning operations preserve attachments and immutable dispatch attempts."""

import copy

import httpx
from test_discovery import GAP, _catalog
from test_gap_contract_recovery import ready, setup, worker_env

from doclayer import discovery, dispatcher
from doclayer.dispatcher import GapDispatcher, attempt_key
from doclayer.gap_budget import ResourceBudget
from doclayer.gap_ops import handle

INDEX = "https://example.org/a-index"
PRIMARY = "https://example.org/z-extract"
SECOND = "https://example.org/y-runtime"


def candidates(ledger, gap_id, rows):
    ledger.update(gap_id, lambda r: setattr(r, "source_candidates", rows))


def attach(ledger, gap_id, url):
    return handle(
        "attach_source",
        {
            "gap_id": gap_id,
            "source": {
                "url": url,
                "authority": "official",
                "note": "Task-specific primary documentation",
            },
        },
        ledger,
    )


def test_owning_attachment_order_survives_refresh_and_real_submit(tmp_path, monkeypatch):
    ledger, ops, d = setup(tmp_path)
    r = ready(ledger)
    candidates(
        ledger,
        r.gap_id,
        [{"url": INDEX, "authority": "official", "origin": "discovered"}],
    )
    attach(ledger, r.gap_id, PRIMARY)
    attach(ledger, r.gap_id, SECOND)
    attach(ledger, r.gap_id, PRIMARY)  # Revalidation/reattachment cannot reorder.
    worker = worker_env(monkeypatch, ledger, ops)
    ledger.transition(r.gap_id, "candidate")
    fresh = [discovery.Seed(u, "attached", "revalidated", source_hash="b" * 64) for u in (SECOND, INDEX, PRIMARY)]
    monkeypatch.setattr(
        worker,
        "discover",
        lambda *a, **k: discovery.DiscoveryResult(r.gap_id, "discovered", seeds=fresh),
    )
    worker.run_cycle(ledger)
    current = ledger.load(r.gap_id)
    assert current.submission["payload"]["urls"] == [PRIMARY, SECOND, INDEX]
    assert len(ops.store.list_ids()) == 1
    assert [c["url"] for c in current.source_candidates] == [INDEX, PRIMARY, SECOND]
    assert all(c["source_hash"] == "b" * 64 for c in current.source_candidates)
    assert [c["attachment"] for c in current.source_candidates[1:]] == ["task-documentation-v1"] * 2
    assert ops.calls[-1][1]["urls"] == [PRIMARY, SECOND, INDEX]
    assert current.submission["payload"]["limits"]["max_model_calls"] == 50


def test_authority_and_explicit_provenance_are_separate(tmp_path):
    ledger, ops, d = setup(tmp_path)
    r = ready(ledger)
    candidates(
        ledger,
        r.gap_id,
        [
            {
                "url": PRIMARY,
                "authority": "official",
                "origin": "attached",
                "attachment": "foreign",
            },
            {"url": INDEX, "authority": "official"},
            {
                "url": SECOND,
                "authority": "unverified",
                "attachment": "task-documentation-v1",
            },
            {
                "url": "https://example.org/x-official",
                "authority": "official",
                "origin": "task-documentation",
            },
        ],
    )
    assert d.dispatch(r.gap_id).action == "submitted"
    assert ops.calls[-1][1]["urls"] == [
        "https://example.org/x-official",
        INDEX,
        PRIMARY,
    ]


def test_lost_reply_policy_change_replays_immutable_payload_reservation(tmp_path, monkeypatch):
    ledger, ops, d = setup(tmp_path)
    r = ready(ledger)
    attach(ledger, r.gap_id, PRIMARY)
    ops.lose = True
    assert d.dispatch(r.gap_id).action == "skipped"
    old = copy.deepcopy(ledger.load(r.gap_id).submission)
    monkeypatch.setattr(dispatcher, "SEED_PRIORITY_POLICY", "future-v2")
    attach(ledger, r.gap_id, SECOND)
    assert GapDispatcher(ledger, transport=ops).dispatch(r.gap_id).action == "submitted"
    new = ledger.load(r.gap_id).submission
    assert [p for op, p in ops.calls if op == "submit"] == [
        old["payload"],
        old["payload"],
    ]
    assert new["attempt_id"] == old["attempt_id"]
    assert new["reservation"] == old["reservation"]
    assert new["seed_priority_policy"] == old["seed_priority_policy"]
    assert len(ops.store.list_ids()) == 1
    assert ResourceBudget(ledger.root).load(old["reservation"]["day"])["reserved_calls"] == 50


def test_staged_policy_change_cannot_rebuild_payload(tmp_path, monkeypatch):
    ledger, ops, d = setup(tmp_path)
    r = ready(ledger)
    (ledger.root / "curator-ready").unlink()
    assert d.dispatch(r.gap_id).action == "dry-run"
    old = copy.deepcopy(ledger.load(r.gap_id).submission)
    monkeypatch.setattr(dispatcher, "SEED_PRIORITY_POLICY", "future-v2")
    attach(ledger, r.gap_id, PRIMARY)
    (ledger.root / "curator-ready").touch()
    assert d.dispatch(r.gap_id).action == "submitted"
    assert ops.calls[-1][1] == old["payload"]


def test_terminal_new_policy_advances_once_without_old_job_mutation(tmp_path, monkeypatch):
    ledger, ops, d = setup(tmp_path)
    r = ready(ledger)
    candidates(
        ledger,
        r.gap_id,
        [
            {"url": INDEX, "authority": "official"},
            {"url": PRIMARY, "authority": "official", "origin": "attached"},
        ],
    )
    monkeypatch.setattr(dispatcher, "SEED_PRIORITY_POLICY", "earlier-policy")
    first = d.dispatch(r.gap_id)
    assert ops.calls[-1][1]["urls"] == [INDEX, PRIMARY]
    job = ops.store.load(first.job_id)
    job.status = "failed"
    ops.store.save(job)
    d.dispatch(r.gap_id)
    old_job = ops.store.state_path(first.job_id).read_bytes()
    old = copy.deepcopy(ledger.load(r.gap_id).submission)
    assert old["reservation"]["calls"] == 50
    monkeypatch.setattr(dispatcher, "SEED_PRIORITY_POLICY", "explicit-attachment-order-v1")
    attach(ledger, r.gap_id, PRIMARY)
    ledger.transition(r.gap_id, "ready_for_curation")
    second = d.dispatch(r.gap_id)
    assert second.action == "submitted"
    assert ops.calls[-1][1]["urls"] == [PRIMARY, INDEX]
    current = ledger.load(r.gap_id)
    assert current.attempts == 2 and current.submission_history == [old]
    assert current.submission["input_signature"] != old["input_signature"]
    assert current.submission["attempt_id"] != old["attempt_id"]
    assert ops.store.state_path(first.job_id).read_bytes() == old_job
    assert current.submission["payload"]["limits"]["max_model_calls"] == 50
    assert ResourceBudget(ledger.root).load(current.submission["reservation"]["day"])["reserved_calls"] == 100
    job = ops.store.load(second.job_id)
    job.status = "failed"
    ops.store.save(job)
    d.dispatch(r.gap_id)
    ledger.transition(r.gap_id, "ready_for_curation")
    assert d.dispatch(r.gap_id).action == "skipped"
    assert len(ops.store.list_ids()) == 2


def test_future_attempt_key_binds_order_without_changing_legacy_helper():
    assert attempt_key("gap", 1, [INDEX, PRIMARY]) == attempt_key("gap", 1, [PRIMARY, INDEX])
    assert attempt_key("gap", 1, [INDEX, PRIMARY], priority_policy="v1") != attempt_key(
        "gap", 1, [PRIMARY, INDEX], priority_policy="v1"
    )


def test_discovery_fetches_explicit_primary_first_and_retains_automatic_links():
    primary = "https://docs.widget.example/z-retries"
    index = "https://docs.widget.example/"
    linked = "https://docs.widget.example/runtime"
    requested = []
    pages = {
        primary: "<html>widget prod retries <a href='/runtime'>runtime guide</a></html>",
        index: "<html>widget prod official guide</html>",
        linked: "<html>widget prod retries runtime guide</html>",
    }

    def response(request):
        requested.append(str(request.url))
        return httpx.Response(200, text=pages.get(str(request.url), ""))

    gap = dict(
        GAP,
        source_candidates=[
            {"url": index, "authority": "official", "origin": "discovered"},
            {"url": primary, "authority": "official", "origin": "task-documentation"},
        ],
    )
    result = discovery.discover(gap, _catalog(), client=httpx.Client(transport=httpx.MockTransport(response)))
    assert requested[0] == primary
    assert {s.url for s in result.seeds} >= {primary, index, linked}
