from pathlib import Path

from doclayer.curator.config import CuratorConfig, load_config

EXAMPLE = next(Path(__file__).resolve().parents[2].joinpath("deploy").glob("*/config.example.yaml"))
HOSTS = ["ws1", "app1", "gpu1", "svc1", "worker1", "nas1"]


def test_private_writer_defaults():
    pw = CuratorConfig().private_writer
    assert pw.hosts == [] and pw.host_notes == {}
    assert (pw.max_commands, pw.max_job_calls) == (30, 200)
    assert not hasattr(pw, "max_attempts")
    assert not hasattr(pw, "daily_cap")
    assert (pw.command_timeout_s, pw.output_cap_bytes) == (60, 131072)


def test_private_writer_example_config_declares_hosts_and_roles():
    pw = load_config(EXAMPLE).private_writer
    assert pw.hosts == HOSTS
    assert set(pw.host_notes) == set(HOSTS) and "workstation" in pw.host_notes["ws1"]
    assert "model" in pw.host_notes["gpu1"]


def test_private_writer_yaml_overrides(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("private_writer:\n  hosts: [ws1]\n  max_commands: 12\n")
    pw = load_config(path).private_writer
    assert pw.hosts == ["ws1"] and pw.max_commands == 12
