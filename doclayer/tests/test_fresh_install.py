"""A fresh install (empty HOME, no env, no config files) starts every entry
point's first step without the network, and an unreachable inference endpoint is
reported through the documented outcome rather than a stray exception."""

from __future__ import annotations

import asyncio
import json
import os
import socket

import httpx
import pytest

from doclayer.curator import cli, transport
from doclayer.curator.config import CuratorConfig, load_config
from doclayer.curator.model import ModelClient, endpoint_answers
from doclayer.curator.state import Usage


@pytest.fixture(autouse=True)
def fresh_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in list(os.environ):
        if name.startswith(("DOC_LAYER_", "BRAIN_CURATOR_", "CURATOR_")):
            monkeypatch.delenv(name)

    def no_network(*args, **kwargs):
        raise AssertionError("fresh-install startup must not touch the network or ssh")

    monkeypatch.setattr("subprocess.run", no_network)
    return home


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_mcp_server_object_builds_without_config():
    from doclayer import mcp_server

    server = mcp_server.build_server()
    assert server.name


def test_curator_cli_status_listing_uses_local_transport_on_empty_store(fresh_home, capsys):
    cfg = load_config()
    assert isinstance(transport.transport_for(cfg), transport.LocalTransport)
    rc = cli.main(["list"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Traceback" not in out
    assert json.loads(out) == {"ok": True, "jobs": []}


def test_curator_mcp_server_builds_and_picks_local_transport():
    from doclayer.curator import mcp_server

    assert mcp_server.build_server().name == "brain-curator"
    assert isinstance(transport.transport_for(), transport.LocalTransport)


def test_gap_worker_once_runs_on_an_empty_ledger(fresh_home, capsys):
    """The real entry point, `doclayer-gap-worker --once`, completes with no
    network or ssh (subprocess.run is patched to fail) and reports the empty
    ledger."""
    from doclayer import gap_worker

    assert gap_worker.main(["--once"]) == 0
    out = capsys.readouterr().out
    assert "0 record(s), 0 outstanding" in out
    assert "dispatch is dry-run" in out
    from doclayer.settings import settings

    assert f"knowledge gaps @ {settings.gap_state_dir}" in out
    assert "quarantined" not in out


def test_unreachable_default_inference_endpoint_is_reported_cleanly():
    url = f"http://127.0.0.1:{_unused_port()}/v1"
    assert endpoint_answers(url, None) is False

    cfg = CuratorConfig()
    cfg.inference.url = url
    client = ModelClient(cfg, Usage())
    assert asyncio.run(client._endpoint_reachable(None)) is False


def test_probe_targets_the_shipped_default_url_and_unreachable_is_false(monkeypatch):
    """No real connection: the probe's HTTP client is replaced by one that
    records the URL and refuses the connection."""
    seen: list[str] = []

    class Refusing:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers=None):
            seen.append(url)
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("doclayer.curator.model.httpx.Client", Refusing)
    cfg = CuratorConfig()
    assert cfg.inference.url == "http://localhost:8000/v1"
    assert endpoint_answers(cfg.inference.url, None) is False
    assert seen == ["http://localhost:8000/v1/models"]
