"""On the gap-owner host the owner/transport default to local; env still wins."""

from __future__ import annotations

import socket

import pytest

from doclayer.curator import transport
from doclayer.curator.config import CuratorConfig
from doclayer.settings import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("DOC_LAYER_GAP_OWNER", "DOC_LAYER_GAP_OWNER_HOST", "BRAIN_CURATOR_LOCAL"):
        monkeypatch.delenv(name, raising=False)
    # The owner host is deployment config; an empty one means "this machine".
    monkeypatch.setenv("DOC_LAYER_GAP_OWNER_HOST", "worker1")


def _cfg():
    cfg = CuratorConfig()
    cfg.ssh.host = "worker1"
    return cfg


def _host(monkeypatch, name):
    monkeypatch.setattr(socket, "gethostname", lambda: name)


def test_gap_owner_is_local_on_the_owner_host(monkeypatch):
    _host(monkeypatch, "worker1")
    assert Settings().gap_owner == "local"


def test_gap_owner_is_local_for_fqdn_of_owner_host(monkeypatch):
    _host(monkeypatch, "worker1.lan")
    assert Settings().gap_owner == "local"


def test_gap_owner_stays_ssh_on_other_hosts(monkeypatch):
    _host(monkeypatch, "ws1")
    assert Settings().gap_owner == "ssh"


def test_explicit_gap_owner_env_overrides_hostname(monkeypatch):
    _host(monkeypatch, "worker1")
    monkeypatch.setenv("DOC_LAYER_GAP_OWNER", "ssh")
    assert Settings().gap_owner == "ssh"
    _host(monkeypatch, "ws1")
    monkeypatch.setenv("DOC_LAYER_GAP_OWNER", "local")
    assert Settings().gap_owner == "local"


def test_configured_owner_host_is_what_is_compared(monkeypatch):
    _host(monkeypatch, "other-box")
    monkeypatch.setenv("DOC_LAYER_GAP_OWNER_HOST", "other-box")
    assert Settings().gap_owner == "local"


def test_transport_is_local_on_the_owner_host(monkeypatch):
    _host(monkeypatch, "worker1")
    assert isinstance(transport.transport_for(_cfg()), transport.LocalTransport)


def test_transport_is_ssh_on_other_hosts(monkeypatch):
    _host(monkeypatch, "ws1")
    assert isinstance(transport.transport_for(_cfg()), transport.SshTransport)


def test_explicit_local_env_overrides_hostname(monkeypatch):
    _host(monkeypatch, "ws1")
    monkeypatch.setenv("BRAIN_CURATOR_LOCAL", "1")
    assert isinstance(transport.transport_for(_cfg()), transport.LocalTransport)


def test_explicit_non_local_env_overrides_owner_hostname(monkeypatch):
    _host(monkeypatch, "worker1")
    monkeypatch.setenv("BRAIN_CURATOR_LOCAL", "0")
    assert isinstance(transport.transport_for(_cfg()), transport.SshTransport)


def test_empty_ssh_host_means_local_transport(monkeypatch):
    _host(monkeypatch, "ws1")
    cfg = CuratorConfig()
    assert cfg.ssh.host == ""
    assert isinstance(transport.transport_for(cfg), transport.LocalTransport)


def test_explicit_non_local_env_beats_empty_ssh_host(monkeypatch):
    monkeypatch.setenv("BRAIN_CURATOR_LOCAL", "0")
    assert isinstance(transport.transport_for(CuratorConfig()), transport.SshTransport)
