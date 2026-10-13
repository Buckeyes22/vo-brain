"""How workstation clients reach the worker's durable queue: SSH.

The CLI and MCP adapter both go through this module. A call stages any
supplied local files over SSH (tar pipe, argument-array subprocesses, no
shell interpolation of names or content), then sends one JSON request on
stdin to the fixed remote ops entry point and reads one JSON reply.

An SSH failure raises TransportError; it never fabricates a result. A
submission that times out mid-call is still durable on the worker — the
content-derived idempotency key makes a retry return the same job.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from doclayer.curator.config import CuratorConfig, load_config
from doclayer.settings import is_host


class TransportError(RuntimeError):
    pass


class Transport:
    def call(
        self,
        op: str,
        payload: dict[str, Any],
        *,
        files: list[Path] | None = None,
        directory: Path | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError


class LocalTransport(Transport):
    """Runs ops in-process (worker host usage and tests)."""

    def __init__(self, cfg: CuratorConfig) -> None:
        self.cfg = cfg

    def call(
        self,
        op: str,
        payload: dict[str, Any],
        *,
        files: list[Path] | None = None,
        directory: Path | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        from doclayer.curator import ops

        work_payload = dict(payload)
        if files or directory is not None:
            staging = Path(self.cfg.jobs_dir()) / "incoming" / _client_key(payload, self.cfg.pipeline.default_mode)
            staging.mkdir(parents=True, exist_ok=True)
            if files:
                for file_path in files:
                    _copy_file(file_path, staging / file_path.name)
                work_payload["files"] = [{"name": f.name, "size": f.stat().st_size} for f in files]
            if directory is not None and directory.is_dir():
                target_dir = staging / directory.name
                target_dir.mkdir(parents=True, exist_ok=True)
                for file_path in _walk_no_symlinks(directory):
                    rel = file_path.relative_to(directory)
                    dest = target_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    _copy_file(file_path, dest)
                work_payload["directory"] = directory.name
            work_payload["staging_dir"] = str(staging)
        return ops.handle(op, work_payload, cfg=self.cfg)


class SshTransport(Transport):
    """SSH to the worker host: files first (tar), then the ops call."""

    def __init__(self, cfg: CuratorConfig) -> None:
        self.cfg = cfg

    def _ssh_base(self) -> list[str]:
        return [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={int(self.cfg.ssh.connect_timeout_s)}",
            self.cfg.ssh.host,
        ]

    def _stage_paths(
        self,
        files: list[Path] | None,
        directory: Path | None,
        client_key: str,
    ) -> dict[str, Any]:
        """Tar supplied local paths and extract them into the remote staging
        dir for this submission key. Only config-derived strings and the hex
        idempotency key enter the remote command — never submitted content.
        Returns the payload annotations."""
        # The worker's ops entry point looks for staged inputs under
        # jobs/incoming/<idempotency key> — stage exactly there.
        remote_dir = str(Path(self.cfg.jobs_dir()) / "incoming" / client_key)
        payload_notes: dict[str, Any] = {}
        tar_args = ["tar", "-czf", "-"]
        for file_path in files or []:
            if not file_path.is_file():
                raise TransportError(f"not a file: {file_path}")
            tar_args += ["-C", str(file_path.parent), file_path.name]
            payload_notes.setdefault("files", []).append({"name": file_path.name, "size": file_path.stat().st_size})
        if directory is not None:
            if not directory.is_dir():
                raise TransportError(f"not a directory: {directory}")
            tar_args += ["-C", str(directory.parent), directory.name]
            payload_notes["directory"] = directory.name
        if len(tar_args) == 3:
            return payload_notes
        mkdir_cmd = f"mkdir -p {remote_dir} && tar -xzf - -C {remote_dir}"
        with (
            subprocess.Popen(tar_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as tar_proc,
            subprocess.Popen(
                [*self._ssh_base(), mkdir_cmd],
                stdin=tar_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as ssh_proc,
        ):
            assert tar_proc.stdout is not None
            tar_proc.stdout.close()
            _, ssh_err = ssh_proc.communicate(timeout=self.cfg.ssh.command_timeout_s)
            ssh_rc = ssh_proc.returncode
            _, tar_err = tar_proc.communicate(timeout=self.cfg.ssh.command_timeout_s)
            tar_rc = tar_proc.returncode
        if ssh_rc != 0:
            raise TransportError(f"file staging over ssh failed: {ssh_err.decode(errors='replace')[:300]}")
        if tar_rc != 0:
            raise TransportError(f"local tar failed: {tar_err.decode(errors='replace')[:300]}")
        payload_notes["staging_dir"] = remote_dir
        return payload_notes

    def call(
        self,
        op: str,
        payload: dict[str, Any],
        *,
        files: list[Path] | None = None,
        directory: Path | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        client_key = _client_key(payload, self.cfg.pipeline.default_mode)
        notes = self._stage_paths(files, directory, client_key)
        work_payload = dict(payload)
        work_payload.update(notes)
        request = json.dumps(work_payload, ensure_ascii=False).encode("utf-8")
        argv = [*self._ssh_base(), self.cfg.ssh.remote_command, op]
        try:
            proc = subprocess.run(
                argv,
                input=request,
                capture_output=True,
                timeout=timeout_s or self.cfg.ssh.command_timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise TransportError(
                f"remote ops call timed out after {timeout_s or self.cfg.ssh.command_timeout_s}s"
                " (the job stays durable on the worker; retry safely)"
            ) from exc
        except OSError as exc:
            raise TransportError(f"ssh failed to run: {exc}") from exc
        stdout = proc.stdout.decode("utf-8", errors="replace").strip()
        if not stdout:
            raise TransportError(
                f"remote ops returned no output (exit {proc.returncode}); "
                f"stderr: {proc.stderr.decode(errors='replace')[:300]}"
            )
        try:
            reply = json.loads(stdout.splitlines()[-1])
        except ValueError as exc:
            raise TransportError(f"remote ops returned non-JSON: {stdout[:300]}") from exc
        if proc.returncode != 0 and not reply.get("ok"):
            pass  # structured error reply; caller inspects "ok"
        return reply


def _walk_no_symlinks(directory: Path) -> list[Path]:
    out = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            continue
        if path.is_file() and path.suffix.lower() in (
            ".md",
            ".markdown",
            ".txt",
            ".pdf",
        ):
            out.append(path)
    return out


def _copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src.read_bytes())


def _client_key(payload: dict[str, Any], default_mode: str | None = None) -> str:
    from doclayer.curator.ops import derive_idempotency_key

    key = str(payload.get("idempotency_key") or derive_idempotency_key(payload, default_mode))
    return key


def transport_for(cfg: CuratorConfig | None = None) -> Transport:
    """Local when no ssh host is configured (this machine is the worker), on the
    worker host (hostname equals the configured ssh host) or when
    BRAIN_CURATOR_LOCAL=1; else SSH. An explicit BRAIN_CURATOR_LOCAL value
    always wins over the defaults."""
    cfg = cfg or load_config()
    explicit = os.environ.get("BRAIN_CURATOR_LOCAL", "")
    if explicit:
        local = explicit == "1"
    else:
        local = not cfg.ssh.host or is_host(cfg.ssh.host)
    return LocalTransport(cfg) if local else SshTransport(cfg)
