"""Ops protocol: real subprocess JSON-on-stdin/stdout checks, idempotent
submit, malformed-input rejection, cancel/resume semantics, inspection."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import run_git

from doclayer.curator.config import CuratorConfig
from doclayer.curator.jobstore import JobStore


def run_ops(op: str, payload: dict, env: dict[str, str]) -> tuple[int, dict]:
    """Run the ops entry point as a real subprocess with JSON on stdin."""

    proc = subprocess.run(
        [sys.executable, "-m", "doclayer.curator.ops", op],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert proc.stdout.strip(), proc.stderr
    reply = json.loads(proc.stdout.strip().splitlines()[-1])
    return proc.returncode, reply


@pytest.fixture()
def local_cfg(tmp_path: Path, checkout: Path) -> CuratorConfig:
    """A real config file (exercises the YAML loader), tmp paths throughout."""
    import yaml

    cfg = CuratorConfig()
    cfg.paths.brain_checkout = str(checkout)
    cfg.paths.jobs = str(tmp_path / "jobs")
    cfg.paths.worktrees = str(tmp_path / "worktrees")
    cfg.paths.cache = str(tmp_path / "cache")
    cfg.inference.url = "http://inference.test/v1"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "paths": {
                    "brain_checkout": cfg.paths.brain_checkout,
                    "jobs": cfg.paths.jobs,
                    "worktrees": cfg.paths.worktrees,
                    "cache": cfg.paths.cache,
                },
                "inference": {"url": cfg.inference.url, "model": cfg.inference.model},
            }
        )
    )
    cfg._test_config_path = str(config_path)  # type: ignore[attr-defined]
    return cfg


@pytest.fixture()
def env(local_cfg: CuratorConfig) -> dict[str, str]:
    import os

    env = dict(os.environ)
    env["BRAIN_CURATOR_CONFIG"] = str(local_cfg._test_config_path)  # type: ignore[attr-defined]
    env["BRAIN_CURATOR_LOCAL"] = "1"
    return env


def test_submit_creates_durable_job_via_real_protocol(local_cfg, env) -> None:
    code, reply = run_ops("submit", {"intent": "model study", "urls": ["https://docs.test/m"]}, env)
    assert code == 0 and reply["ok"] and reply["created"]
    store = JobStore(local_cfg.jobs_dir())
    state = store.load(reply["job_id"])
    assert state.status == "queued"
    assert state.sources[0].url == "https://docs.test/m"


def test_uncertain_submission_retry_returns_same_job(local_cfg, env) -> None:
    """The same content-derived submission must never enqueue a duplicate."""
    payload = {"intent": "model study", "urls": ["https://docs.test/m"]}
    code1, r1 = run_ops("submit", payload, env)
    code2, r2 = run_ops("submit", payload, env)
    assert r1["job_id"] == r2["job_id"]
    assert r2["created"] is False
    store = JobStore(local_cfg.jobs_dir())
    assert len([j for j in store.list_ids()]) == 1


def test_malformed_input_is_rejected_structurally(local_cfg, env) -> None:
    os = __import__("os")
    proc = subprocess.run(
        [sys.executable, "-m", "doclayer.curator.ops", "submit"],
        input="{ this is not json",
        capture_output=True,
        text=True,
        env={**os.environ},
        cwd=Path(__file__).resolve().parents[2],
    )
    reply = json.loads(proc.stdout.strip().splitlines()[-1])
    assert reply["ok"] is False
    assert "json" in reply["error"].lower()
    code, reply = run_ops("submit", {"no_intent": True}, env)
    assert reply["ok"] is False
    code, reply = run_ops("status", {"job_id": "job-doesnotx1"}, env)
    assert reply["ok"] is False and "unknown job" in reply["error"]


def test_status_result_and_list_roundtrip(local_cfg, env) -> None:
    _, r = run_ops("submit", {"intent": "round trip", "texts": [{"label": "n", "text": "note " * 50}]}, env)
    job_id = r["job_id"]
    _, status = run_ops("status", {"job_id": job_id}, env)
    assert status["ok"] and status["job"]["status"] == "queued"
    _, result = run_ops("result", {"job_id": job_id}, env)
    assert result["ok"] and "digest" in result
    _, listing = run_ops("list", {}, env)
    assert listing["ok"] and listing["jobs"][0]["job_id"] == job_id


def test_cancel_queued_job_is_immediate(local_cfg, env) -> None:
    _, r = run_ops("submit", {"intent": "cancel me", "texts": [{"label": "n", "text": "note " * 50}]}, env)
    job_id = r["job_id"]
    _, cancel = run_ops("cancel", {"job_id": job_id}, env)
    assert cancel["ok"] and cancel["status"] == "cancelled"
    _, status = run_ops("status", {"job_id": job_id}, env)
    assert status["job"]["status"] == "cancelled"
    # resume of a cancelled job stays cancelled
    _, resume = run_ops("resume", {"job_id": job_id}, env)
    assert resume["ok"] is False


def test_resume_extends_limits_explicitly(local_cfg, env) -> None:
    _, r = run_ops("submit", {"intent": "extend me", "texts": [{"label": "n", "text": "note " * 50}]}, env)
    job_id = r["job_id"]
    store = JobStore(local_cfg.jobs_dir())
    state = store.load(job_id)
    state.limits = {"max_model_calls": 10}
    state.status = "retryable"
    store.save(state)
    _, resume = run_ops("resume", {"job_id": job_id, "extend": {"max_model_calls": 50}}, env)
    assert resume["ok"]
    state = store.load(job_id)
    assert state.status == "queued"
    assert state.limits["max_model_calls"] == 60  # extended, never reset


def test_inspect_returns_draft_diff_and_evidence(local_cfg, env, checkout) -> None:
    _, r = run_ops("submit", {"intent": "inspect me", "texts": [{"label": "n", "text": "note " * 50}]}, env)
    job_id = r["job_id"]
    store = JobStore(local_cfg.jobs_dir())
    state = store.load(job_id)
    from conftest import valid_meta

    from doclayer.curator.state import PageChange

    state.changes = [
        PageChange(
            group_id="g1",
            path="wiki/new-inspect.md",
            action="create",
            draft_meta=valid_meta(cid="c-aaaa0100"),
            draft_body="# Inspect\n\nContent. [source: n]\n",
        )
    ]
    state.base_commit = run_git(checkout, "rev-parse", "HEAD")
    store.save(state)
    _, diff = run_ops("inspect", {"job_id": job_id, "what": "diff", "path": "wiki/new-inspect.md"}, env)
    assert diff["ok"] and "+# Inspect" in diff["diff"]
    _, evidence = run_ops("inspect", {"job_id": job_id, "what": "evidence"}, env)
    assert evidence["ok"] and evidence["total"] == 0


def test_correct_records_correction_and_creates_job(local_cfg, env, checkout) -> None:
    code, reply = run_ops(
        "correct",
        {
            "page": "wiki/existing-model.md",
            "claim": "the wrong claim that must not return",
            "instruction": "state that the context window is 128k, not unlimited",
        },
        env,
    )
    assert code == 0 and reply["ok"]
    store = JobStore(local_cfg.jobs_dir())
    corrections = store.corrections(page="wiki/existing-model.md")
    assert len(corrections) == 1
    assert corrections[0]["instruction"].startswith("state that the context window")
    job = store.load(reply["job_id"])
    assert job.correction_of == "direct"
    assert job.changes[0].path == "wiki/existing-model.md"
    assert job.changes[0].action == "update"
    assert job.stage == "draft"


@pytest.mark.parametrize("stage,done", [("report", []), ("validate", ["report"])])
def test_resume_completed_failed_report_is_immutable(local_cfg, env, stage, done):
    _, created = run_ops(
        "submit",
        {"intent": "Completed failure", "texts": [{"label": "Source", "text": "Retained source facts. " * 20}]},
        env,
    )
    store = JobStore(local_cfg.jobs_dir())
    state = store.load(created["job_id"])
    state.status = "failed"
    state.stage = stage
    state.stages_done = done
    state.error = "Final completed failure"
    state.usage.model_calls = 7
    state.limits = {"max_model_calls": 50}
    store.save(state)
    before = state.to_dict()
    _, reply = run_ops("resume", {"job_id": state.job_id, "extend": {"max_model_calls": 20}}, env)
    assert reply["ok"] and reply["status"] == "failed" and "already finished" in reply["note"]
    assert store.load(state.job_id).to_dict() == before
