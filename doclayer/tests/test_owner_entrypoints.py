"""Every curator entry point decides local vs ssh with the same owner-host rule."""

from __future__ import annotations

import asyncio
import socket

import pytest

from doclayer.curator import cli, mcp_server, transport
from doclayer.settings import Settings, is_host


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for name in ("DOC_LAYER_GAP_OWNER", "DOC_LAYER_GAP_OWNER_HOST", "BRAIN_CURATOR_LOCAL"):
        monkeypatch.delenv(name, raising=False)
    # The owner host is deployment config; an empty one means "this machine".
    monkeypatch.setenv("DOC_LAYER_GAP_OWNER_HOST", "worker1")
    config = tmp_path / "curator-config.yaml"
    config.write_text("ssh: {host: worker1}\n")
    monkeypatch.setenv("BRAIN_CURATOR_CONFIG", str(config))


@pytest.fixture
def calls(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        transport.LocalTransport, "call", lambda self, op, payload, **kw: seen.append("local") or {"ok": True}
    )
    monkeypatch.setattr(
        transport.SshTransport, "call", lambda self, op, payload, **kw: seen.append("ssh") or {"ok": True}
    )
    return seen


def _host(monkeypatch, name):
    monkeypatch.setattr(socket, "gethostname", lambda: name)


@pytest.mark.parametrize(
    ("hostname", "configured", "expected"),
    [
        ("worker1", "worker1", True),
        ("WORKER1", "worker1", True),
        ("worker1", "worker1.lan", True),
        ("worker1.lan", "worker1", True),
        ("worker1.lan", "worker1.lan", True),
        ("Worker1.Example.Org", "worker1.lan", True),
        ("ws1", "worker1", False),
        ("ws1.lan", "worker1.lan", False),
        ("worker1", "", False),
    ],
)
def test_is_host_compares_the_first_dns_label_or_the_exact_fqdn(monkeypatch, hostname, configured, expected):
    _host(monkeypatch, hostname)
    assert is_host(configured) is expected


def test_gap_owner_is_local_when_the_configured_owner_is_an_fqdn(monkeypatch):
    _host(monkeypatch, "worker1")
    monkeypatch.setenv("DOC_LAYER_GAP_OWNER_HOST", "worker1.lan")
    assert Settings().gap_owner == "local"


def test_cli_uses_local_on_the_owner_host(monkeypatch, calls):
    _host(monkeypatch, "worker1")
    cli.main(["status", "--job", "job-1"])
    assert calls == ["local"]


def test_cli_uses_ssh_off_the_owner_host(monkeypatch, calls):
    _host(monkeypatch, "ws1")
    cli.main(["status", "--job", "job-1"])
    assert calls == ["ssh"]


def test_cli_local_flag_and_explicit_env_still_win(monkeypatch, calls):
    _host(monkeypatch, "ws1")
    cli.main(["--local", "status", "--job", "job-1"])
    monkeypatch.setenv("BRAIN_CURATOR_LOCAL", "1")
    cli.main(["status", "--job", "job-1"])
    _host(monkeypatch, "worker1")
    monkeypatch.setenv("BRAIN_CURATOR_LOCAL", "0")
    cli.main(["status", "--job", "job-1"])
    assert calls == ["local", "local", "ssh"]


def _mcp_status(monkeypatch):
    server = mcp_server.build_server()
    asyncio.run(server.call_tool("job_status", {"job_id": "job-1"}))


def test_mcp_server_uses_local_on_the_owner_host(monkeypatch, calls):
    _host(monkeypatch, "worker1")
    _mcp_status(monkeypatch)
    assert calls == ["local"]


def test_mcp_server_uses_ssh_off_the_owner_host(monkeypatch, calls):
    _host(monkeypatch, "ws1")
    _mcp_status(monkeypatch)
    assert calls == ["ssh"]


def test_mcp_server_explicit_env_wins(monkeypatch, calls):
    _host(monkeypatch, "worker1")
    monkeypatch.setenv("BRAIN_CURATOR_LOCAL", "0")
    _mcp_status(monkeypatch)
    assert calls == ["ssh"]
