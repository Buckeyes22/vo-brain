import pytest
import yaml

from doclayer.curator.config import CuratorConfig, load_config
from doclayer.curator.ops import OpsError, handle
from doclayer.curator.state import JobState


def test_default_mode_is_single_pass_and_yaml_flips_it(tmp_path, monkeypatch):
    monkeypatch.delenv("BRAIN_CURATOR_MODE", raising=False)
    assert CuratorConfig().pipeline.default_mode == "single-pass"
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump({"pipeline": {"default_mode": "multi-stage"}}))
    assert load_config(path).pipeline.default_mode == "multi-stage"
    monkeypatch.setenv("BRAIN_CURATOR_MODE", "single-pass")
    assert load_config(path).pipeline.default_mode == "single-pass"
    sp = CuratorConfig().single_pass
    assert (
        sp.max_model_calls,
        sp.max_sources,
        sp.max_pages,
        sp.max_calls_per_page,
        sp.max_extract_calls,
        sp.max_grounding_chunks,
        sp.max_active_seconds,
    ) == (200, 50, 20, 10, 60, 10, 4 * 3600.0)
    assert not hasattr(sp, "max_claims_per_page")


def test_unknown_mode_in_env_or_yaml_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_CURATOR_MODE", "fast")
    with pytest.raises(ValueError):
        load_config(tmp_path / "missing.yaml")
    monkeypatch.delenv("BRAIN_CURATOR_MODE")
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump({"pipeline": {"default_mode": "fast"}}))
    with pytest.raises(ValueError):
        load_config(path)


def test_legacy_state_loads_multi_stage():
    raw = JobState(job_id="job-00000000aa", idempotency_key="k", intent="x").to_dict()
    raw.pop("mode")
    assert JobState.from_dict(raw).mode == "multi-stage"


def test_submit_records_mode_and_validates_targets(cfg, store):
    out = handle(
        "submit",
        {"intent": "guide import", "urls": ["https://docs.test/a"], "targets": ["wiki/existing-model.md"]},
        cfg,
        store,
    )
    state = store.load(out["job_id"])
    assert state.mode == "single-pass"
    assert state.evidence_selection["single_pass"]["targets"] == ["wiki/existing-model.md"]
    with pytest.raises(OpsError):
        handle("submit", {"intent": "bad", "urls": ["https://docs.test/b"], "targets": ["log.md"]}, cfg, store)
    with pytest.raises(OpsError):
        handle("submit", {"intent": "bad", "urls": ["https://docs.test/c"], "mode": "fast"}, cfg, store)
    legacy = handle(
        "submit", {"intent": "old path", "urls": ["https://docs.test/d"], "mode": "multi-stage"}, cfg, store
    )
    assert store.load(legacy["job_id"]).mode == "multi-stage"


def test_correction_jobs_stay_multi_stage(cfg, store):
    out = handle(
        "correct",
        {"page": "wiki/existing-model.md", "claim": "Known facts about the model here.", "instruction": "remove it"},
        cfg,
        store,
    )
    assert store.load(out["job_id"]).mode == "multi-stage"


def _finished_parent(store, mode, targets=None):
    from doclayer.curator.intake import make_url_source

    source = make_url_source("https://docs.test/parent")
    source.status = "fetched"
    source.extraction_complete = True
    parent = store.create(
        JobState("", f"parent-{mode}", "parent import", status="published", sources=[source], mode=mode)
    )[0]
    if targets:
        parent.evidence_selection["single_pass"] = {
            "version": 1,
            "targets": list(targets),
            "pages": {"wiki/x.md": {"calls": 2}},
        }
        store.save(parent)
    return parent


def test_continue_inherits_and_overrides_mode(cfg, store):
    parent = _finished_parent(store, "multi-stage")
    inherited = handle("continue", {"job_id": parent.job_id, "discover": False}, cfg, store)
    assert store.load(inherited["job_id"]).mode == "multi-stage"
    overridden = handle("continue", {"job_id": parent.job_id, "discover": False, "mode": "single-pass"}, cfg, store)
    assert overridden["created"] and overridden["job_id"] != inherited["job_id"]
    assert store.load(overridden["job_id"]).mode == "single-pass"
    with pytest.raises(OpsError):
        handle("continue", {"job_id": parent.job_id, "mode": "fast"}, cfg, store)


def test_continue_explicit_modes_give_distinct_jobs(cfg, store):
    parent = _finished_parent(store, "single-pass")
    first = handle("continue", {"job_id": parent.job_id, "discover": False}, cfg, store)
    multi = handle("continue", {"job_id": parent.job_id, "discover": False, "mode": "multi-stage"}, cfg, store)
    single = handle("continue", {"job_id": parent.job_id, "discover": False, "mode": "single-pass"}, cfg, store)
    # An explicit mode equal to the parent's is the inherited mode.
    assert multi["created"] and not single["created"] and single["job_id"] == first["job_id"]
    assert multi["job_id"] != first["job_id"]
    assert store.load(multi["job_id"]).mode == "multi-stage"
    assert store.load(single["job_id"]).mode == "single-pass"
    replay = handle("continue", {"job_id": parent.job_id, "discover": False, "mode": "multi-stage"}, cfg, store)
    assert replay["job_id"] == multi["job_id"] and not replay["created"]


def test_continue_without_mode_keeps_legacy_key(cfg, store):
    import hashlib
    import json

    parent = _finished_parent(store, "multi-stage")
    out = handle("continue", {"job_id": parent.job_id, "discover": False}, cfg, store)
    request = {
        "parent": parent.job_id,
        "targets": [],
        "review_omitted": True,
        "discover": False,
        "policy": parent.source_policy,
        "intent": parent.intent,
    }
    assert (
        store.load(out["job_id"]).idempotency_key
        == hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:24]
    )


def test_continue_explicit_parent_mode_hashes_like_omitted_mode(cfg, store):
    parent = _finished_parent(store, "multi-stage")
    omitted = handle("continue", {"job_id": parent.job_id, "discover": False}, cfg, store)
    same = handle("continue", {"job_id": parent.job_id, "discover": False, "mode": "multi-stage"}, cfg, store)
    assert same["job_id"] == omitted["job_id"] and not same["created"]
    other = handle("continue", {"job_id": parent.job_id, "discover": False, "mode": "single-pass"}, cfg, store)
    assert other["created"] and other["job_id"] != omitted["job_id"]


def test_single_pass_continuation_copies_parent_targets(cfg, store):
    parent = _finished_parent(store, "single-pass", targets=["wiki/existing-model.md"])
    child = store.load(handle("continue", {"job_id": parent.job_id, "discover": False}, cfg, store)["job_id"])
    assert child.evidence_selection["single_pass"] == {"version": 1, "targets": ["wiki/existing-model.md"], "pages": {}}
    multi = store.load(
        handle("continue", {"job_id": parent.job_id, "discover": False, "mode": "multi-stage"}, cfg, store)["job_id"]
    )
    assert "single_pass" not in multi.evidence_selection


def test_idempotency_key_normalizes_default_mode_and_keeps_legacy_keys():
    import hashlib
    import json

    from doclayer.curator.ops import derive_idempotency_key

    payload = {"intent": "guide import", "urls": ["https://docs.test/a"]}
    legacy_parts = [
        "guide import",
        json.dumps([]),
        json.dumps(["https://docs.test/a"]),
        json.dumps([]),
        json.dumps([]),
        "",
        "",
        json.dumps({}, sort_keys=True),
    ]
    legacy = hashlib.sha256("\x00".join(legacy_parts).encode("utf-8")).hexdigest()[:24]
    assert derive_idempotency_key(payload) == legacy
    assert derive_idempotency_key(payload, "single-pass") == legacy
    assert derive_idempotency_key({**payload, "mode": "single-pass"}, "single-pass") == legacy
    assert derive_idempotency_key({**payload, "mode": "multi-stage"}, "single-pass") != legacy
    assert derive_idempotency_key({**payload, "mode": "single-pass"}, "multi-stage") != legacy
    deduped = derive_idempotency_key({**payload, "targets": ["wiki/a.md", "wiki/b.md"]})
    assert derive_idempotency_key({**payload, "targets": ["wiki/a.md", "wiki/b.md", "wiki/a.md"]}) == deduped


def test_submit_with_default_mode_returns_omitted_mode_job(cfg, store):
    payload = {"intent": "guide import", "urls": ["https://docs.test/same"]}
    first = handle("submit", dict(payload), cfg, store)
    again = handle("submit", {**payload, "mode": cfg.pipeline.default_mode}, cfg, store)
    assert again["job_id"] == first["job_id"] and not again["created"]


def test_targets_dedupe_in_order_and_reject_multi_stage(cfg, store):
    cfg.single_pass.max_pages = 2
    out = handle(
        "submit",
        {
            "intent": "dupes",
            "urls": ["https://docs.test/e"],
            "targets": ["wiki/b.md", "wiki/a.md", "wiki/b.md", "wiki/a.md"],
        },
        cfg,
        store,
    )
    assert store.load(out["job_id"]).evidence_selection["single_pass"]["targets"] == ["wiki/b.md", "wiki/a.md"]
    with pytest.raises(OpsError, match="single-pass"):
        handle(
            "submit",
            {"intent": "multi", "urls": ["https://docs.test/f"], "mode": "multi-stage", "targets": ["wiki/a.md"]},
            cfg,
            store,
        )


def test_cli_mode_choices_come_from_config(capsys):
    from doclayer.curator import cli

    with pytest.raises(SystemExit):
        cli.main(["--local", "submit", "--intent", "x", "--mode", "fast"])
    assert "single-pass" in capsys.readouterr().err


def test_mcp_job_continue_forwards_mode(monkeypatch):
    import asyncio

    from doclayer.curator import mcp_server

    seen = []

    class FakeTransport:
        def call(self, op, payload):
            seen.append((op, payload))
            return {"ok": True}

    monkeypatch.setattr(mcp_server, "transport_for", lambda *a, **k: FakeTransport())
    server = mcp_server.build_server()
    server._curator_cfg = object()
    tool = next(t for t in asyncio.run(server.list_tools()) if t.name == "job_continue")
    assert "mode" in tool.inputSchema["properties"]
    asyncio.run(server.call_tool("job_continue", {"job_id": "job-1234567890", "mode": "multi-stage"}))
    assert seen and seen[-1][1]["mode"] == "multi-stage"
    asyncio.run(server.call_tool("job_continue", {"job_id": "job-1234567890"}))
    assert "mode" not in seen[-1][1]


def test_digest_marks_calls_that_predate_stage_accounting(cfg, store):
    from doclayer.curator.pipeline import Pipeline

    state = store.create(JobState("", "legacy-digest", "legacy import", status="published"))[0]
    state.usage.model_calls = 7
    state.usage.calls_by_stage = {"draft": 3, "extract": 2}
    digest = Pipeline(cfg, store).build_digest(state)
    assert "- calls by stage: draft 3, extract 2 (partial: 2 calls predate per-stage accounting)" in digest
    state.usage.model_calls = 5
    assert "partial" not in Pipeline(cfg, store).build_digest(state)


def test_explicit_mode_differing_from_a_replayed_job_creates_a_new_job(cfg, store):
    payload = {"intent": "guide import", "urls": ["https://docs.test/pre-deploy"]}
    cfg.pipeline.default_mode = "multi-stage"  # a job submitted before the deploy
    old = handle("submit", dict(payload), cfg, store)
    cfg.pipeline.default_mode = "single-pass"
    fresh = handle("submit", {**payload, "mode": "single-pass"}, cfg, store)
    assert fresh["created"] and fresh["job_id"] != old["job_id"]
    assert store.load(fresh["job_id"]).mode == "single-pass"
    again = handle("submit", {**payload, "mode": "single-pass"}, cfg, store)
    assert again["job_id"] == fresh["job_id"] and not again["created"]
    plain = handle("submit", dict(payload), cfg, store)  # an ordinary replay stays stable
    assert plain["job_id"] == old["job_id"] and not plain["created"]


def test_continue_targets_override_inherited_targets(cfg, store):
    parent = _finished_parent(store, "single-pass", targets=["wiki/existing-model.md"])
    child = store.load(
        handle("continue", {"job_id": parent.job_id, "discover": False, "targets": ["wiki/other-page.md"]}, cfg, store)[
            "job_id"
        ]
    )
    assert child.evidence_selection["single_pass"]["targets"] == ["wiki/other-page.md"]
    inherited = store.load(handle("continue", {"job_id": parent.job_id, "discover": False}, cfg, store)["job_id"])
    assert inherited.evidence_selection["single_pass"]["targets"] == ["wiki/existing-model.md"]
    assert inherited.job_id != child.job_id
    with pytest.raises(OpsError):
        handle("continue", {"job_id": parent.job_id, "discover": False, "targets": ["log.md"]}, cfg, store)
    with pytest.raises(OpsError, match="single-pass"):
        handle(
            "continue",
            {"job_id": parent.job_id, "discover": False, "mode": "multi-stage", "targets": ["wiki/other-page.md"]},
            cfg,
            store,
        )


def test_cli_continue_forwards_targets(monkeypatch, cfg):
    from doclayer.curator import cli

    seen = []

    class Transport:
        def __init__(self, cfg):
            pass

        def call(self, op, payload):
            seen.append((op, payload))
            return {"ok": True, "job_id": "job-1234567890"}

    monkeypatch.setattr(cli, "load_config", lambda _: cfg)
    monkeypatch.setattr(cli, "LocalTransport", Transport)
    assert (
        cli.main(["--local", "continue", "--job", "job-parent123", "--target", "wiki/a.md", "--target", "wiki/b.md"])
        == 0
    )
    assert seen[-1][1]["targets"] == ["wiki/a.md", "wiki/b.md"]
    assert cli.main(["--local", "continue", "--job", "job-parent123"]) == 0
    assert "targets" not in seen[-1][1]


def test_mcp_job_continue_forwards_targets(monkeypatch):
    import asyncio

    from doclayer.curator import mcp_server

    seen = []

    class FakeTransport:
        def call(self, op, payload):
            seen.append((op, payload))
            return {"ok": True}

    monkeypatch.setattr(mcp_server, "transport_for", lambda *a, **k: FakeTransport())
    server = mcp_server.build_server()
    server._curator_cfg = object()
    tool = next(t for t in asyncio.run(server.list_tools()) if t.name == "job_continue")
    assert "targets" in tool.inputSchema["properties"]
    asyncio.run(server.call_tool("job_continue", {"job_id": "job-1234567890", "targets": ["wiki/a.md"]}))
    assert seen[-1][1]["targets"] == ["wiki/a.md"]
    asyncio.run(server.call_tool("job_continue", {"job_id": "job-1234567890"}))
    assert "targets" not in seen[-1][1]
