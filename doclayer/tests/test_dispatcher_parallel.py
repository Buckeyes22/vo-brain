"""The dispatcher keeps up to `max_active_jobs` submissions, one slot file per gap."""

import json
import threading

import pytest
from test_gap_contract_recovery import RealOps, ready

from doclayer import dispatcher as dispatcher_module
from doclayer.dispatcher import GapDispatcher
from doclayer.gaps import GapError, GapLedger

BUSY = "another durable active submission occupies the slot"


def env(tmp_path, limit, names):
    ledger = GapLedger(tmp_path / "gaps")
    ledger.root.mkdir()
    (ledger.root / "curator-ready").touch()
    ops = RealOps(tmp_path)
    dispatcher = GapDispatcher(ledger, transport=ops, max_active_jobs=limit)
    return ledger, ops, dispatcher, [ready(ledger, name) for name in names]


def slot_files(ledger):
    return sorted(p.name for p in (ledger.root / "active-submissions").glob("*.json"))


def test_five_gaps_dispatch_into_five_slots_and_the_sixth_waits(tmp_path):
    ledger, ops, d, gaps = env(tmp_path, 5, [f"widget{i}" for i in range(6)])
    outcomes = [d.dispatch(g.gap_id) for g in gaps]
    assert [o.action for o in outcomes] == ["submitted"] * 5 + ["skipped"]
    assert outcomes[5].detail == BUSY
    assert slot_files(ledger) == sorted(f"{g.gap_id}.json" for g in gaps[:5])
    assert not (ledger.root / "active-submission.json").exists()
    for gap, outcome in zip(gaps[:5], outcomes):
        slot = json.loads((ledger.root / "active-submissions" / f"{gap.gap_id}.json").read_text())
        assert slot["gap_id"] == gap.gap_id and slot["job_id"] == outcome.job_id
    assert [a["gap_id"] for a in d.active_all()] == [g.gap_id for g in gaps[:5]]
    assert d.active()["gap_id"] == gaps[0].gap_id  # oldest
    assert d.active(gap_id=gaps[3].gap_id)["gap_id"] == gaps[3].gap_id
    assert d.active(gap_id=gaps[5].gap_id) is None
    assert len(ops.store.list_ids()) == 5
    assert ledger.load(gaps[5].gap_id).status == "ready_for_curation"
    # A gap that already owns a slot is observed, never submitted twice.
    again = d.dispatch(gaps[0].gap_id)
    assert again.action == "skipped" and again.job_id == outcomes[0].job_id
    assert len(ops.store.list_ids()) == 5


def test_default_limit_is_one_slot(tmp_path):
    ledger, ops, _, gaps = env(tmp_path, None, ["one", "two"])
    d = GapDispatcher(ledger, transport=ops)
    assert d.max_active_jobs() == 1
    assert d.dispatch(gaps[0].gap_id).action == "submitted"
    busy = d.dispatch(gaps[1].gap_id)
    assert busy.action == "skipped" and busy.detail == BUSY


def test_limit_is_read_from_the_gap_worker_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("gap_worker:\n  max_active_jobs: 3\n")
    monkeypatch.setenv("BRAIN_CURATOR_CONFIG", str(cfg))
    ledger = GapLedger(tmp_path / "gaps")
    assert GapDispatcher(ledger).max_active_jobs() == 3
    # An invalid or unreadable setting is an error, never a silent single slot.
    cfg.write_text("gap_worker:\n  max_active_jobs: 0\n")
    with pytest.raises(GapError, match="curator config unreadable"):
        GapDispatcher(ledger).max_active_jobs()
    cfg.write_text("gap_worker: [unclosed\n")
    with pytest.raises(GapError, match="curator config unreadable"):
        GapDispatcher(ledger).max_active_jobs()


@pytest.mark.parametrize("value", ["0", "-1", '"5"', "true", "2.5", "null"])
@pytest.mark.parametrize("key", ["gap_worker.max_active_jobs", "limits.parallel_jobs"])
def test_parallelism_settings_must_be_whole_numbers_of_at_least_one(tmp_path, key, value):
    from doclayer.curator.config import load_config

    section, name = key.split(".")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"{section}:\n  {name}: {value}\n")
    with pytest.raises(ValueError, match=key.replace(".", r"\.")):
        load_config(cfg)
    cfg.write_text(f"{section}:\n  {name}: 5\n")
    loaded = load_config(cfg)
    assert getattr(getattr(loaded, section), name) == 5


def test_legacy_single_slot_file_is_migrated(tmp_path, monkeypatch):
    ledger, ops, d, gaps = env(tmp_path, 2, ["legacy", "fresh"])
    assert d.dispatch(gaps[0].gap_id).action == "submitted"
    slot = ledger.root / "active-submissions" / f"{gaps[0].gap_id}.json"
    legacy = ledger.root / "active-submission.json"
    content = json.loads(slot.read_text())
    slot.rename(legacy)

    # The legacy file is removed only after the slot file is written.
    def failing_write(path, data):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(dispatcher_module, "atomic_write_json", failing_write)
        with pytest.raises(GapError, match="active submission corrupt"):
            GapDispatcher(ledger, transport=ops, max_active_jobs=2).active_all()
    assert legacy.exists() and not slot.exists()

    restarted = GapDispatcher(ledger, transport=ops, max_active_jobs=2)
    assert restarted.active_all() == [content]
    assert slot.exists() and not legacy.exists()
    assert json.loads(slot.read_text()) == content
    # The migrated slot counts against the limit and the second gap still fits.
    assert restarted.dispatch(gaps[1].gap_id).action == "submitted"
    assert len(slot_files(ledger)) == 2


def test_completed_slot_frees_capacity_for_the_next_gap(tmp_path):
    ledger, ops, d, gaps = env(tmp_path, 2, ["first", "second", "third"])
    first, second = d.dispatch(gaps[0].gap_id), d.dispatch(gaps[1].gap_id)
    assert d.dispatch(gaps[2].gap_id).detail == BUSY
    d.observe(gaps[0].gap_id, {"job_id": first.job_id, "status": "published"})
    assert slot_files(ledger) == [f"{gaps[1].gap_id}.json"]
    assert ledger.load(gaps[0].gap_id).submission["state"] == "terminal"
    assert d.active(gap_id=gaps[1].gap_id)["job_id"] == second.job_id
    third = d.dispatch(gaps[2].gap_id)
    assert third.action == "submitted"
    assert slot_files(ledger) == sorted(f"{g.gap_id}.json" for g in gaps[1:])


def test_corrupt_slot_withholds_without_touching_other_slots(tmp_path):
    ledger, ops, d, gaps = env(tmp_path, 3, ["alpha", "bravo", "charlie"])
    d.dispatch(gaps[0].gap_id)
    d.dispatch(gaps[1].gap_id)
    slots = ledger.root / "active-submissions"
    good, bad = slots / f"{gaps[1].gap_id}.json", slots / f"{gaps[0].gap_id}.json"
    before = good.read_text()
    bad.write_text("{not json")
    result = d.dispatch(gaps[2].gap_id)
    assert result.action == "skipped"
    assert result.detail == "active submission corrupt; dispatcher withheld"
    with pytest.raises(GapError, match="active submission corrupt"):
        d.active_all()
    assert bad.read_text() == "{not json" and good.read_text() == before
    assert len(ops.store.list_ids()) == 2
    assert ledger.load(gaps[2].gap_id).status == "ready_for_curation"


def test_legacy_recovery_accepts_up_to_max_active_jobs(tmp_path):
    ledger, ops, d, gaps = env(tmp_path, 3, ["alpha", "bravo", "charlie"])
    jobs = {g.gap_id: d.dispatch(g.gap_id).job_id for g in gaps}
    # Crash window: records say accepted, no slot files exist.
    for name in slot_files(ledger):
        (ledger.root / "active-submissions" / name).unlink()
    restarted = GapDispatcher(ledger, transport=ops, max_active_jobs=3)
    recovered = restarted.active_all()
    assert sorted(a["gap_id"] for a in recovered) == sorted(jobs)
    assert {a["gap_id"]: a["job_id"] for a in recovered} == jobs
    assert slot_files(ledger) == sorted(f"{g}.json" for g in jobs)

    for name in slot_files(ledger):
        (ledger.root / "active-submissions" / name).unlink()
    strict = GapDispatcher(ledger, transport=ops, max_active_jobs=2)
    with pytest.raises(GapError, match="multiple active legacy submissions"):
        strict.active_all()
    assert slot_files(ledger) == []


def test_concurrent_dispatch_never_exceeds_the_limit(tmp_path):
    ledger, ops, _, gaps = env(tmp_path, 2, [f"race{i}" for i in range(4)])
    barrier = threading.Barrier(len(gaps) * 2)
    outcomes, errors = [], []

    def worker(gap_id):
        try:
            dispatcher = GapDispatcher(ledger, transport=ops, max_active_jobs=2)
            barrier.wait()
            outcomes.append(dispatcher.dispatch(gap_id))
        except Exception as exc:  # noqa: BLE001 — surfaced by the assertion below
            errors.append(exc)

    # Every gap is dispatched twice at once: no gap may take two slots.
    threads = [threading.Thread(target=worker, args=(g.gap_id,)) for g in gaps for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert sum(o.action == "submitted" for o in outcomes) == 2
    assert len(slot_files(ledger)) == 2
    assert len(ops.store.list_ids()) == 2
    verifier = GapDispatcher(ledger, transport=ops, max_active_jobs=2)
    assert len({a["gap_id"] for a in verifier.active_all()}) == 2


def test_one_gap_worker_cycle_fills_the_free_slots(tmp_path, monkeypatch):
    from test_gap_contract_recovery import worker_env

    cfg = tmp_path / "config.yaml"
    cfg.write_text("gap_worker:\n  max_active_jobs: 3\n")
    monkeypatch.setenv("BRAIN_CURATOR_CONFIG", str(cfg))
    ledger, ops, _, gaps = env(tmp_path, None, [f"cycle{i}" for i in range(4)])
    worker_env(monkeypatch, ledger, ops).run_cycle(ledger)
    statuses = sorted(ledger.load(g.gap_id).status for g in gaps)
    assert statuses == ["curating"] * 3 + ["ready_for_curation"]
    assert len(ops.store.list_ids()) == 3 and len(slot_files(ledger)) == 3


def _candidate(ledger, name):
    record, _ = ledger.submit_event(
        need_class="missing_coverage",
        subject=name,
        facets=["configuration"],
        representative_question=f"How does official {name} documentation describe configuration?",
        source_candidates=[{"url": f"https://example.org/{name}", "authority": "official"}],
    )
    return record


def _cycle_env(tmp_path, monkeypatch, limit, ready_names, candidate_names):
    from test_gap_contract_recovery import worker_env

    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"gap_worker:\n  max_active_jobs: {limit}\n")
    monkeypatch.setenv("BRAIN_CURATOR_CONFIG", str(cfg))
    ledger, ops, d, ready_gaps = env(tmp_path, None, ready_names)
    candidates = [_candidate(ledger, name) for name in candidate_names]
    worker = worker_env(monkeypatch, ledger, ops)
    researched = []

    def research(led, record, *args):
        # Research finds seeds; at this moment record which gaps already curate.
        curating = sorted(r.gap_id for r in led.list_records() if r.status == "curating")
        researched.append((record.gap_id, curating))
        led.transition(record.gap_id, "ready_for_curation", reason="fixture seeds")

    monkeypatch.setattr(worker, "_research_one", research)
    return ledger, ops, d, worker, ready_gaps, candidates, researched


def test_research_runs_while_one_of_five_slots_is_busy(tmp_path, monkeypatch):
    ledger, ops, d, worker, busy, candidates, researched = _cycle_env(tmp_path, monkeypatch, 5, ["busy"], ["fresh"])
    assert d.dispatch(busy[0].gap_id).action == "submitted"
    worker.run_cycle(ledger)
    assert [gap for gap, _ in researched] == [candidates[0].gap_id]
    assert ledger.load(candidates[0].gap_id).status == "curating"
    assert len(slot_files(ledger)) == 2


def test_gap_made_ready_mid_research_is_dispatched_before_the_next_research(tmp_path, monkeypatch):
    ledger, ops, d, worker, _, candidates, researched = _cycle_env(tmp_path, monkeypatch, 5, [], ["first", "second"])
    worker.run_cycle(ledger)
    assert len(researched) == 2
    first, _ = researched[0]
    _, curating_at_second = researched[1]
    assert curating_at_second == [first]  # submitted in the same cycle, before more research
    assert all(ledger.load(c.gap_id).status == "curating" for c in candidates)


def test_single_slot_skips_research_only_when_busy_with_a_ready_backlog(tmp_path, monkeypatch):
    ledger, ops, d, worker, gaps, candidates, researched = _cycle_env(
        tmp_path, monkeypatch, 1, ["busy", "backlog"], ["fresh"]
    )
    assert d.dispatch(gaps[0].gap_id).action == "submitted"
    worker.run_cycle(ledger)
    assert researched == []  # the slot is busy and a ready gap already waits
    ledger.transition(gaps[1].gap_id, "suppressed", reason="fixture")
    worker.run_cycle(ledger)
    assert [gap for gap, _ in researched] == [candidates[0].gap_id]
    assert ledger.load(candidates[0].gap_id).status == "ready_for_curation"
    assert len(ops.store.list_ids()) == 1
