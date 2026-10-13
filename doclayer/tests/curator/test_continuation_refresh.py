"""Explicit refresh creates a new fetched generation without altering history."""

import hashlib

import pytest
from test_continuation_frontier import fetch_wire, parent_job
from test_curator_mcp_server import mcp_env  # noqa: F401 -- pytest fixture registration

from doclayer.curator import cli, curate
from doclayer.curator.ops import OpsError, _op_continue
from doclayer.curator.pipeline import Pipeline
from doclayer.curator.state import Evidence


@pytest.mark.asyncio
async def test_refresh_fetches_same_url_retains_obligations_and_invalidates_current_proof(cfg, store):
    parent, cache = parent_job(cfg, store)
    old = parent.sources[0]
    old.reviewed_sections = ["# Old"]
    old.review_unit_count = 1
    old.omitted_categories = {"historical-reviewed": "prior gap"}
    evidence = Evidence.from_dict(store.read_evidence(parent.job_id)[0])
    evidence.url = old.url
    # Persist real current source binding, rather than a fake cache marker.
    (store.job_dir(parent.job_id) / "evidence.jsonl").write_text("")
    store.append_evidence(parent.job_id, [evidence.to_dict()])
    parent.evidence_verification[evidence.claim_id] = {
        "status": "supported",
        "supported": True,
        "support_version": curate.SOURCE_SUPPORT_VERSION,
        "source_binding": curate.source_support_binding(evidence, {old.id: cache.read_text()}),
        "excerpt": evidence.quote,
    }
    store.save(parent)
    before = store.state_path(parent.job_id).read_bytes()
    old_cache = cache.read_bytes()
    old_evidence = store.read_evidence(parent.job_id)
    payload = {"job_id": parent.job_id, "refresh_source_ids": [old.id]}
    first = _op_continue(payload, cfg, store)
    child = store.load(first["job_id"])
    refreshed = child.sources[0]
    assert child.stage == "intake" and refreshed.status == "pending" and refreshed.submitted
    assert (
        refreshed.reviewed_sections == []
        and refreshed.unknown_reviewed_sections == []
        and not refreshed.extraction_complete
    )
    assert refreshed.content_hash is None and refreshed.extract_path is None
    assert refreshed.relevance["refresh_history"][0]["source"]["reviewed_sections"] == ["# Old"]
    assert child.evidence_verification == parent.evidence_verification
    assert store.read_evidence(child.job_id) == old_evidence
    seen = []
    pipeline = Pipeline(cfg, store, fetch_transport=fetch_wire(seen))
    assert await pipeline._stage_intake(child) == "discover"
    assert seen == [old.url] and refreshed.status == "fetched"
    new_text = pipeline._extract_text(child, refreshed)
    assert new_text != cache.read_text() and refreshed.content_hash == hashlib.sha256(new_text.encode()).hexdigest()
    current = pipeline._evidence(child)
    assert [item.claim_id for item in current] == [evidence.claim_id] and current[0].support_status == "unknown"
    assert child.evidence_verification == parent.evidence_verification
    assert store.state_path(parent.job_id).read_bytes() == before and cache.read_bytes() == old_cache
    assert store.read_evidence(parent.job_id) == old_evidence
    replay = _op_continue(payload, cfg, store)
    assert replay["job_id"] == child.job_id and not replay["created"]


def test_refresh_request_identity_is_distinct_and_normalized(cfg, store):
    parent, _ = parent_job(cfg, store)
    source = parent.sources[0]
    ordinary = _op_continue({"job_id": parent.job_id}, cfg, store)
    fresh = _op_continue({"job_id": parent.job_id, "refresh_source_ids": [source.id]}, cfg, store)
    replay = _op_continue(
        {"job_id": parent.job_id, "refresh_source_ids": [source.id, source.id]},
        cfg,
        store,
    )
    assert ordinary["job_id"] != fresh["job_id"] == replay["job_id"] and not replay["created"]


@pytest.mark.parametrize(
    "mutation",
    [
        {"discover": False},
        {"refresh_source_ids": ["missing"]},
        {"refresh_source_ids": "not-a-list"},
        {"source_policy": {"exclude": [{"url": "https://docs.test/old"}]}},
    ],
)
def test_refresh_rejects_invalid_or_currently_excluded_targets_without_child(cfg, store, mutation):
    parent, _ = parent_job(cfg, store)
    before = store.state_path(parent.job_id).read_bytes()
    payload = {
        "job_id": parent.job_id,
        "refresh_source_ids": [parent.sources[0].id],
        **mutation,
    }
    with pytest.raises(OpsError):
        _op_continue(payload, cfg, store)
    assert store.list_ids() == [parent.job_id] and store.state_path(parent.job_id).read_bytes() == before


def test_refresh_cannot_target_nonfetched_source(cfg, store):
    parent, _ = parent_job(cfg, store)
    parent.sources[0].status = "failed"
    store.save(parent)
    with pytest.raises(OpsError, match="fetched URL"):
        _op_continue(
            {"job_id": parent.job_id, "refresh_source_ids": [parent.sources[0].id]},
            cfg,
            store,
        )


def test_cli_refresh_forwards_explicit_target(cfg, monkeypatch):
    captured = []

    class Wire:
        def __init__(self, cfg):
            pass

        def call(self, op, payload):
            captured.append((op, payload))
            return {"ok": True}

    monkeypatch.setattr(cli, "LocalTransport", Wire)
    monkeypatch.setattr(cli, "load_config", lambda _: cfg)
    assert (
        cli.main(
            [
                "--local",
                "continue",
                "--job",
                "job-refresh000",
                "--refresh-source",
                "src-original",
            ]
        )
        == 0
    )
    assert captured[0][0] == "continue" and captured[0][1]["refresh_source_ids"] == ["src-original"]


@pytest.mark.asyncio
async def test_actual_mcp_sdk_refresh_schema_forwarding_and_replay(mcp_env):  # noqa: F811 -- imported fixture
    import json
    import os
    import sys
    from pathlib import Path

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from doclayer.curator.config import load_config
    from doclayer.curator.jobstore import JobStore

    cfg = load_config(mcp_env["BRAIN_CURATOR_CONFIG"])
    store = JobStore(cfg.jobs_dir())
    parent, _ = parent_job(cfg, store)
    source = parent.sources[0]
    original = store.state_path(parent.job_id).read_bytes()
    # Select the same source tree under review even when the peer test runner
    # belongs to a different editable-install environment.
    server_env = dict(mcp_env)
    source_root = str(Path(__file__).resolve().parents[2] / "src")
    server_env["PYTHONPATH"] = os.pathsep.join(part for part in (source_root, server_env.get("PYTHONPATH")) if part)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "doclayer.curator.mcp_server"],
        env=server_env,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool = next(t for t in tools.tools if t.name == "job_continue")
            assert "refresh_source_ids" in tool.inputSchema["properties"]
            payload = {"job_id": parent.job_id, "refresh_source_ids": [source.id]}
            result = await session.call_tool("job_continue", payload)
            reply = json.loads(result.content[0].text)
            child = store.load(reply["job_id"])
            assert child.stage == "intake" and child.sources[0].status == "pending" and child.sources[0].submitted
            result = await session.call_tool("job_continue", payload)
            replay = json.loads(result.content[0].text)
            assert replay["job_id"] == reply["job_id"] and not replay["created"]
            result = await session.call_tool("job_continue", {**payload, "discover": False})
            error = json.loads(result.content[0].text)
            assert not error["ok"] and "requires discovery" in error["error"]
    assert store.state_path(parent.job_id).read_bytes() == original


def test_refresh_preserves_now_excluded_raw_obligation_with_explicit_disposition(cfg, store):
    parent, _ = parent_job(cfg, store)
    source = parent.sources[0]
    excluded = Evidence(
        source.id,
        "Old",
        "# Old",
        "old literal",
        "Legacy bounded feature.",
        url="https://docs.test/excluded",
        claim_id="original-excluded",
    )
    store.append_evidence(parent.job_id, [excluded.to_dict()])
    raw = store.read_evidence(parent.job_id)
    original = store.state_path(parent.job_id).read_bytes()
    reply = _op_continue(
        {
            "job_id": parent.job_id,
            "refresh_source_ids": [source.id],
            "source_policy": {"exclude": [{"url": excluded.url}]},
        },
        cfg,
        store,
    )
    child = store.load(reply["job_id"])
    dispositions = {}
    Pipeline(cfg, store)._evidence(child, dispositions)
    assert store.read_evidence(child.job_id) == raw
    assert dispositions[excluded.claim_id]["status"] == "excluded"
    assert store.state_path(parent.job_id).read_bytes() == original and store.read_evidence(parent.job_id) == raw
