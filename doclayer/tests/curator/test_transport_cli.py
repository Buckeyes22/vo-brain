"""SSH transport and CLI: real subprocess plumbing with a fake `ssh` that
executes the remote commands locally — argv framing, JSON-on-stdin, tar file
staging, idempotent remote submit, malformed rejection. Live SSH to worker1
is a follow-up check and stays NOT RUN."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import make_pdf

from doclayer.curator.config import CuratorConfig
from doclayer.curator.jobstore import JobStore
from doclayer.curator.transport import SshTransport


@pytest.fixture()
def ssh_cfg(tmp_path: Path, checkout: Path, monkeypatch) -> CuratorConfig:
    """Config pointing SshTransport at a fake `ssh` that runs remote commands
    locally against the same tmp config (the worker 'host' is this machine)."""
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
    monkeypatch.setenv("BRAIN_CURATOR_CONFIG", str(config_path))

    doclayer_root = Path(__file__).resolve().parents[2]
    fake_ssh = tmp_path / "fake-ssh.py"
    fake_ssh.write_text(
        f"""#!/usr/bin/env python3
import os, re, subprocess, sys
args = sys.argv[1:]
remote = args[4:]  # ssh argv: ssh -o X -o Y host remote...
command = " ".join(remote)
if "tar -xzf - -C" in command:
    target = re.search(r"-C (\\S+)$", command).group(1)
    os.makedirs(target, exist_ok=True)
    subprocess.run(["tar", "-xzf", "-", "-C", target], stdin=sys.stdin, check=True)
    sys.exit(0)
# ops call: "<remote_command> <op>"
op = remote[-1]
subprocess.run(
    [sys.executable, "-m", "doclayer.curator.cli", "ops", op],
    stdin=sys.stdin,
    cwd={str(doclayer_root)!r},
    check=False,
)
"""
    )
    fake_ssh.chmod(fake_ssh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    # point "ssh" at the fake via a shim named ssh
    shim = tmp_path / "ssh"
    shim.write_text(f'#!/bin/sh\nexec {sys.executable} {fake_ssh} "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return cfg


def test_ssh_transport_roundtrip_and_file_staging(ssh_cfg: CuratorConfig, tmp_path: Path) -> None:
    transport = SshTransport(ssh_cfg)
    note = tmp_path / "submission-note.md"
    note.write_text("# Submitted note\n\n" + "Useful operator knowledge. " * 60)

    reply = transport.call(
        "submit",
        {"intent": "ssh round trip"},
        files=[note],
    )
    assert reply["ok"] and reply["created"]
    job_id = reply["job_id"]

    store = JobStore(ssh_cfg.jobs_dir())
    state = store.load(job_id)
    # the file physically arrived on the worker host staging area
    staged = store.staging_dir(job_id) / note.name
    assert staged.is_file()
    assert "operator knowledge" in staged.read_text()
    assert state.sources[0].local_path == str(staged)
    assert state.sources[0].kind == "markdown"

    status = transport.call("status", {"job_id": job_id})
    assert status["ok"] and status["job"]["job_id"] == job_id


def test_uncertain_ssh_submit_does_not_duplicate(ssh_cfg: CuratorConfig) -> None:
    transport = SshTransport(ssh_cfg)
    payload = {"intent": "idempotent over ssh", "urls": ["https://docs.test/one"]}
    first = transport.call("submit", payload)
    second = transport.call("submit", payload)
    assert first["job_id"] == second["job_id"]
    assert second["created"] is False
    store = JobStore(ssh_cfg.jobs_dir())
    assert len(store.list_ids()) == 1


def test_ssh_transport_surfaces_structured_errors(ssh_cfg: CuratorConfig) -> None:
    transport = SshTransport(ssh_cfg)
    reply = transport.call("status", {"job_id": "job-nothere00"})
    assert reply["ok"] is False and "unknown job" in reply["error"]
    rejected = transport.call("submit", {"intent": ""})
    assert rejected["ok"] is False
    assert "intent" in rejected["error"] or "inputs" in rejected["error"]


def test_cli_subprocess_end_to_end(ssh_cfg: CuratorConfig, tmp_path: Path, monkeypatch) -> None:
    """The real CLI binary flow: submit (SSH transport) → status → result →
    correct, all as user-facing argv commands."""
    doclayer_root = Path(__file__).resolve().parents[2]

    def cli(*args: str) -> tuple[int, dict]:
        proc = subprocess.run(
            [sys.executable, "-m", "doclayer.curator.cli", *args],
            capture_output=True,
            text=True,
            cwd=doclayer_root,
        )
        assert proc.stdout.strip(), proc.stderr
        try:
            return proc.returncode, json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise AssertionError(f"CLI printed non-JSON: {proc.stdout[:400]}")

    note = tmp_path / "cli-note.md"
    note.write_text("CLI submitted material. " * 40)
    code, reply = cli("--local", "submit", "--intent", "cli flow test", "--file", str(note))
    assert code == 0 and reply["ok"], reply
    job_id = reply["job_id"]

    code, status = cli("--local", "status", "--job", job_id)
    assert code == 0 and status["job"]["status"] == "queued"

    code, result = cli("--local", "result", "--job", job_id)
    assert code == 0 and "digest" in result

    code, listing = cli("--local", "list")
    assert code == 0 and any(j["job_id"] == job_id for j in listing["jobs"])

    code, correction = cli(
        "--local",
        "correct",
        "--page",
        "wiki/existing-model.md",
        "--claim",
        "some incorrect claim",
        "--instruction",
        "say the measured value instead",
    )
    assert code == 0 and correction["ok"]
    assert correction["job_id"].startswith("job-")


def test_directory_intake_reaches_the_job_staging(ssh_cfg: CuratorConfig, tmp_path: Path) -> None:
    """A directory import enumerates supported files once (no symlinks) and
    they all arrive as sources."""
    transport = SshTransport(ssh_cfg)
    docs = tmp_path / "import-batch"
    docs.mkdir()
    (docs / "one.md").write_text("first note " * 30)
    (docs / "two.txt").write_text("second note " * 30)
    make_pdf(docs / "card.pdf", ["PDF page in directory import"])
    (docs / "skip-me.png").write_bytes(b"not a document")
    link = docs / "linked.md"
    link.symlink_to(docs / "one.md")  # symlinks are never followed

    reply = transport.call("submit", {"intent": "directory import"}, directory=docs)
    assert reply["ok"] and reply["created"]
    store = JobStore(ssh_cfg.jobs_dir())
    state = store.load(reply["job_id"])
    labels = {s.label for s in state.sources}
    assert {"one.md", "two.txt", "card.pdf"} <= labels
    assert "skip-me.png" not in labels
    assert "linked.md" not in labels  # symlink skipped
    assert len(state.sources) == 3
