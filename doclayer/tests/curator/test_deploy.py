"""Run deployment against temporary repos and fake host commands, never SSH."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import run_git

DEPLOY_ROOT = Path(__file__).resolve().parents[2] / "deploy"
# The worker's deploy directory is whichever one carries deploy.sh.
DEPLOY = next(d for d in sorted(DEPLOY_ROOT.iterdir()) if (d / "deploy.sh").is_file()) / "deploy.sh"


def test_deploy_files_hard_code_no_home_directory():
    """Units use %h and scripts resolve the target's home at run time."""
    for path in sorted(DEPLOY_ROOT.rglob("*")):
        if path.is_file() and path.suffix in {".sh", ".service", ".timer", ".py", ".json", ".yaml"}:
            assert not re.search(r"/home/[A-Za-z0-9_.-]+/", path.read_text()), path


@pytest.fixture
def deployment(tmp_path, checkout, origin):
    real_git = shutil.which("git")
    dest = checkout / "doclayer" / "deploy" / DEPLOY.parent.name
    dest.mkdir(parents=True)
    for file in DEPLOY.parent.iterdir():
        if file.is_file():
            shutil.copyfile(file, dest / file.name)
    (checkout / ".gitignore").write_text(".venv/\n")
    run_git(checkout, "add", "doclayer/deploy", ".gitignore")
    run_git(checkout, "commit", "-qm", "test deployment artifacts")
    rev = run_git(checkout, "rev-parse", "HEAD")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    remote_home = tmp_path / "remote-home"
    log = tmp_path / "commands.log"
    scripts = {
        "ssh": f"""#!{sys.executable}
import os, subprocess, sys
args = sys.argv[2:]
is_prepare = args[:2] == ['bash', '-s']
if is_prepare and os.environ.get('PREP_FAIL'): sys.exit(23)
if not is_prepare and 'bundle=' in ' '.join(args) and os.environ.get('BUNDLE_FAIL'): sys.exit(24)
home = os.environ['TEST_REMOTE_HOME']
remote_env = dict(os.environ, HOME=home)  # the remote user's home, not this host's
data = sys.stdin.buffer.read()
if is_prepare:
    cmd = args
else:
    cmd = ['bash', '-c', ' '.join(args)]
sys.exit(subprocess.run(cmd, input=data, env=remote_env).returncode)
""",
        "git": """#!/bin/sh
case "$*" in
  *'push origin HEAD:refs/heads/curator-impl'*)
    if [ "${PUSH_FAIL:-0}" = 1 ]; then exit 1; fi ;;
esac
exec "$TEST_REAL_GIT" "$@"
""",
        "uv": """#!/bin/sh
set -eu
mkdir -p .venv/bin
for name in brain-curator brain-curator-worker brain-curator-mcp; do
    printf '#!/bin/sh\nexit 0\n' > ".venv/bin/$name"
    chmod +x ".venv/bin/$name"
done
""",
        "systemctl": """#!/bin/sh
printf '%s\n' "$*" >> "$TEST_COMMAND_LOG"
exit 0
""",
        "sleep": "#!/bin/sh\nexit 0\n",
    }
    for name, script in scripts.items():
        path = bin_dir / name
        path.write_text(script)
        path.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        TEST_REAL_GIT=real_git,
        TEST_REMOTE_HOME=str(remote_home),
        TEST_COMMAND_LOG=str(log),
        BRAIN_REMOTE=str(origin),
    )

    def execute(**overrides):
        return subprocess.run(
            ["bash", str(DEPLOY), "fake-host", str(checkout)],
            env=dict(env, **overrides),
            capture_output=True,
            text=True,
        )

    return execute, checkout, remote_home, rev, log


@pytest.mark.parametrize("push_fail", ["0", "1"])
def test_deploy_installs_exact_clean_revision_on_normal_and_fallback_paths(deployment, push_fail):
    execute, checkout, home, rev, log = deployment
    result = execute(PUSH_FAIL=push_fail)
    assert result.returncode == 0, result.stdout + result.stderr
    target = home / "git" / "brain-curator"
    assert run_git(target, "rev-parse", "HEAD") == rev
    assert run_git(target, "status", "--porcelain") == ""
    assert (home / ".config/systemd/user/brain-curator.service").is_file()
    assert "restart brain-curator.service" in log.read_text()
    assert not list((home / ".cache/brain-curator").glob("deploy-*.bundle"))


def test_unexpected_remote_preparation_failure_never_starts_service(deployment):
    execute, checkout, home, rev, log = deployment
    result = execute(PREP_FAIL="1")
    assert result.returncode != 0
    assert "rc=23" in result.stderr
    assert not log.exists()


def test_failed_bundle_transfer_never_starts_service(deployment):
    execute, checkout, home, rev, log = deployment
    result = execute(PUSH_FAIL="1", BUNDLE_FAIL="1")
    assert result.returncode != 0
    assert not log.exists()


def test_dirty_source_is_refused_before_host_commands(deployment):
    execute, checkout, home, rev, log = deployment
    (checkout / "operator-note.txt").write_text("leave alone")
    result = execute()
    assert result.returncode != 0
    assert "implementation checkout is dirty" in result.stderr
    assert not home.exists()


def test_dirty_worker_checkout_is_preserved_and_service_not_restarted(deployment):
    execute, checkout, home, rev, log = deployment
    assert execute().returncode == 0
    target = home / "git" / "brain-curator"
    note = target / "operator-note.txt"
    note.write_text("leave alone")
    before = log.read_text()
    result = execute(PUSH_FAIL="1")
    assert result.returncode != 0
    assert "dirty worker checkout" in result.stderr
    assert note.read_text() == "leave alone"
    assert log.read_text() == before
