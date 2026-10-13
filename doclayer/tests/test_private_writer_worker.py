import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from local_sources import local_source
from test_gap_worker import FakeCuratorTransport  # noqa: F401
from test_gap_worker import env as env

from doclayer import gap_worker, gaps_cli, private_writer
from doclayer.curator.config import CuratorConfig
from doclayer.dispatcher import LOCAL_SOURCE_POLICY, GapDispatcher
from doclayer.local_material import DIRECT_AUTHORING_REASON
from doclayer.private_writer import PRIVATE_ORIGIN, Gathered

NOW = datetime(2026, 10, 7, 17, 30, tzinfo=UTC)


def ready_source(
    text="host: ws1\ncommand: dpkg-query -W solaar\nexit: 0\nstdout:\nsolaar\t1.1.19-1\nstderr:\n(no output)\n",
):
    return {
        "kind": "text",
        "label": "live check ws1 dpkg-query #1 @2026-10-07T17:30Z",
        "text": text,
        "authority": "local-observation",
        "origin": PRIVATE_ORIGIN,
        "source_hash": hashlib.sha256(text.encode()).hexdigest(),
    }


def private_gap(ledger, question="How is Solaar installed and launched on the operator's current Ubuntu workstation?"):
    row, _ = ledger.submit_event(
        need_class="needs_local_evidence",
        subject="solaar",
        representative_question=question,
        facets=["local-installation"],
    )
    return row


def gathered(status, sources=(), reason="r"):
    trail = {
        "plan": [{"host": "ws1", "argv": ["hostname"], "why": "x"}],
        "refused": [],
        "withheld": [],
        "unreachable": [],
    }
    return lambda *a, **k: Gathered(status, list(sources), trail, reason)


@pytest.fixture
def cfg(monkeypatch):
    cfg = CuratorConfig()
    cfg.private_writer.hosts = ["ws1"]
    monkeypatch.setattr(private_writer, "load_config", lambda: cfg)
    monkeypatch.setattr(private_writer, "_model_client", lambda cfg: object())
    return cfg


def submits(transport):
    return [c["payload"] for c in transport.calls if c["op"] == "submit"]


def test_ready_evidence_moves_gap_to_curation_and_counts(env, real_private_writer, cfg, monkeypatch):
    _, ledger, catalog = env
    row = private_gap(ledger)
    monkeypatch.setattr(private_writer, "gather_evidence", gathered("ready", [ready_source()]))
    assert private_writer.prepare_private_gap(ledger, ledger.load(row.gap_id), catalog, [], now=NOW)
    current = ledger.load(row.gap_id)
    assert current.status == "ready_for_curation" and current.private_attempts == 1
    assert private_writer.private_sources(current) == [ready_source()]
    events = ledger.events_path(row.gap_id).read_text()
    assert "private_evidence" in events and "1.1.19-1" not in events


def test_empty_parks_with_growing_backoff_and_never_fails(env, real_private_writer, cfg, monkeypatch):
    _, ledger, catalog = env
    row = private_gap(ledger)
    monkeypatch.setattr(private_writer, "gather_evidence", gathered("empty", reason="no usable evidence: env refused"))
    private_writer.prepare_private_gap(ledger, ledger.load(row.gap_id), catalog, [], now=NOW)
    first = ledger.load(row.gap_id)
    assert first.status == "needs_local_evidence" and first.private_attempts == 1
    assert datetime.fromisoformat(first.backoff_until) == NOW + timedelta(hours=1)
    private_writer.prepare_private_gap(ledger, ledger.load(row.gap_id), catalog, [], now=NOW)
    second = ledger.load(row.gap_id)
    assert second.status == "needs_local_evidence" and second.private_attempts == 2
    assert datetime.fromisoformat(second.backoff_until) == NOW + timedelta(hours=6)
    assert "no usable evidence" in ledger.events_path(row.gap_id).read_text()


def test_outage_counts_no_attempt(env, real_private_writer, cfg, monkeypatch):
    _, ledger, catalog = env
    row = private_gap(ledger)
    monkeypatch.setattr(private_writer, "gather_evidence", gathered("outage", reason="hosts unreachable"))
    private_writer.prepare_private_gap(ledger, ledger.load(row.gap_id), catalog, [], now=NOW)
    current = ledger.load(row.gap_id)
    assert current.status == "needs_local_evidence" and current.private_attempts == 0 and current.backoff_until


def test_no_daily_cap_on_private_gaps(env, real_private_writer, cfg, monkeypatch):
    _, ledger, catalog = env
    monkeypatch.setattr(private_writer, "gather_evidence", gathered("ready", [ready_source()]))
    rows = [private_gap(ledger, f"Which Android SDK {n} is installed locally for example-tui?") for n in range(8)]
    for row in rows:
        assert private_writer.prepare_private_gap(ledger, ledger.load(row.gap_id), catalog, [], now=NOW)
    assert all(ledger.load(row.gap_id).status == "ready_for_curation" for row in rows)


def test_early_project_falls_through_to_park(env, real_private_writer, cfg, monkeypatch):
    _, ledger, catalog = env
    catalog.early_projects["example-connector"] = {"aliases": ["example connector"], "path": None}
    row = private_gap(ledger, "Where is the Example Connector design?")
    monkeypatch.setattr(private_writer, "gather_evidence", lambda *a, **k: pytest.fail("gathered for early project"))
    assert private_writer.prepare_private_gap(ledger, ledger.load(row.gap_id), catalog, [], now=NOW) is False


def test_no_allowed_hosts_skips_the_planner_and_counts_no_attempt(env, real_private_writer, cfg, monkeypatch):
    _, ledger, catalog = env
    cfg.private_writer.hosts = []
    row = private_gap(ledger)
    monkeypatch.setattr(private_writer, "_model_client", lambda cfg: pytest.fail("planner client constructed"))
    monkeypatch.setattr(private_writer, "gather_evidence", lambda *a, **k: pytest.fail("planner called"))
    assert private_writer.prepare_private_gap(ledger, ledger.load(row.gap_id), catalog, [], now=NOW) is False
    current = ledger.load(row.gap_id)
    assert current.private_attempts == 0 and current.private_plans == []
    assert private_writer.NO_HOSTS_REASON in ledger.events_path(row.gap_id).read_text()
    gap_worker.run_cycle(ledger)
    parked = ledger.load(row.gap_id)
    assert parked.status == "needs_local_evidence" and parked.private_attempts == 0


def test_worker_parks_as_before_when_the_writer_declines(env):
    _, ledger, _ = env
    row = private_gap(ledger)
    gap_worker.run_cycle(ledger)
    assert ledger.load(row.gap_id).status == "needs_local_evidence"
    assert DIRECT_AUTHORING_REASON in ledger.events_path(row.gap_id).read_text()


def test_private_only_record_dispatches_a_private_payload(env):
    _, ledger, catalog = env
    row = private_gap(ledger)
    ledger.update(
        row.gap_id, lambda r: r.source_candidates.extend([local_source("attached text", "old text"), ready_source()])
    )
    ledger.transition(row.gap_id, "ready_for_curation")
    transport = FakeCuratorTransport(changes=[])
    GapDispatcher(ledger, transport=transport, catalog=catalog).dispatch(row.gap_id)
    payload = submits(transport)[0]
    assert payload["private"] is True and payload["submitted_by"] == "gap-worker-private"
    assert payload["mode"] == "single-pass" and not payload.get("urls")
    assert payload["source_policy"] == LOCAL_SOURCE_POLICY
    assert payload["texts"] == [{"label": ready_source()["label"], "text": ready_source()["text"]}]
    from doclayer.curator.config import PrivateWriterConfig

    job_calls = PrivateWriterConfig().max_job_calls
    assert payload["limits"]["max_model_calls"] <= job_calls
    submission = ledger.load(row.gap_id).submission
    assert submission["reservation"]["calls"] <= job_calls
    assert payload["limits"]["max_model_calls"] == submission["reservation"]["calls"]
    assert payload["intent"].startswith("Document from live read-only evidence: How is Solaar")


def test_private_source_alone_is_enough_to_dispatch(env):
    _, ledger, catalog = env
    row = private_gap(ledger)
    ledger.update(row.gap_id, lambda r: r.source_candidates.append(ready_source()))
    ledger.transition(row.gap_id, "ready_for_curation")
    transport = FakeCuratorTransport(changes=[])
    outcome = GapDispatcher(ledger, transport=transport, catalog=catalog).dispatch(row.gap_id)
    assert submits(transport), outcome.detail


def test_stale_collector_text_alone_never_dispatches(env):
    _, ledger, catalog = env
    row = private_gap(ledger)
    ledger.update(row.gap_id, lambda r: r.source_candidates.append(local_source("attached text", "old text")))
    ledger.transition(row.gap_id, "ready_for_curation")
    transport = FakeCuratorTransport(changes=[])
    GapDispatcher(ledger, transport=transport, catalog=catalog).dispatch(row.gap_id)
    assert not submits(transport) and ledger.load(row.gap_id).status == "needs_local_evidence"


def test_second_attempt_with_new_evidence_is_submitted_again(env):
    _, ledger, catalog = env
    row = private_gap(ledger)
    ledger.update(row.gap_id, lambda r: r.source_candidates.append(ready_source()))
    ledger.transition(row.gap_id, "ready_for_curation")
    transport = FakeCuratorTransport(changes=[])
    dispatcher = GapDispatcher(ledger, transport=transport, catalog=catalog)
    dispatcher.dispatch(row.gap_id)
    job_id = ledger.load(row.gap_id).job_links[-1]["job_id"]
    dispatcher.observe(row.gap_id, {"job_id": job_id, "status": "failed"})

    def renew(r):
        r.source_candidates = [
            ready_source(
                "host: ws1\ncommand: command -v solaar\nexit: 0\nstdout:\n/usr/bin/solaar\nstderr:\n(no output)\n"
            )
        ]

    ledger.update(row.gap_id, renew)
    ledger.transition(row.gap_id, "ready_for_curation")
    dispatcher.dispatch(row.gap_id)
    assert len(submits(transport)) == 2


def test_private_failed_jobs_requeue_on_a_growing_backoff(env, monkeypatch):
    _, ledger, _ = env

    class StatusOnly:
        def call(self, operation, payload, **kwargs):
            assert operation == "status"
            return {"ok": True, "job": {"job_id": payload["job_id"], "status": "failed"}}

    monkeypatch.setattr(gap_worker, "load_config", lambda: CuratorConfig())
    for attempts, expected in ((1, "needs_local_evidence"), (2, "needs_local_evidence"), (5, "needs_local_evidence")):
        row = private_gap(ledger, f"private job attempt {attempts}")

        def submitted(record):
            record.private_attempts = attempts
            record.job_links = [{"job_id": f"job-fake000{attempts}", "attempt": 1}]
            record.status = "curating"
            record.submission = {"payload": {"private": True}}

        ledger.update(row.gap_id, submitted)
        gap_worker._verify_published(ledger, GapDispatcher(ledger, transport=StatusOnly()), [])
        current = ledger.load(row.gap_id)
        assert current.status == expected
        wait = (datetime.fromisoformat(current.backoff_until) - datetime.now(UTC)).total_seconds()
        assert (
            private_writer.private_retry_backoff_s(attempts) - 120
            < wait
            <= private_writer.private_retry_backoff_s(attempts)
        )


def test_retry_clears_private_attempts(env):
    tmp_path, ledger, _ = env
    row = private_gap(ledger)
    ledger.update(row.gap_id, lambda r: setattr(r, "private_attempts", 2))
    assert gaps_cli.main(["--state-dir", str(tmp_path / "gaps"), "retry", row.gap_id]) == 0
    assert ledger.load(row.gap_id).private_attempts == 0
