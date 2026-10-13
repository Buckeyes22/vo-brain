import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from doclayer.curator.single_pass import citation_faults
from doclayer.curator.state import Evidence
from doclayer.private_commands import (
    evidence_label,
    evidence_text,
    possible_secret,
    refusal,
    remote_string,
    run_command,
)

HOSTS = ["ws1", "App1", "worker1"]


@pytest.fixture(autouse=True)
def _worker_settings(monkeypatch):
    """Read roots and the worker host are deployment settings; these tests declare them."""
    from doclayer.settings import settings

    monkeypatch.setattr(
        settings, "private_read_roots", ("/home/user/git/", "/home/user/github/", "/home/user/Downloads/")
    )
    monkeypatch.setattr(settings, "gap_owner_host", "worker1")


def ok(argv, host="ws1", resolved=frozenset()):
    return refusal(host, argv, hosts=HOSTS, resolved=set(resolved))


@pytest.mark.parametrize(
    "argv",
    [
        ["dpkg-query", "-W", "solaar"],
        ["dpkg-query", "-W", "-f=${Package} ${Version} ${Status}\\n", "solaar", "openjdk-*"],
        ["dpkg-query", "-W", "--showformat=${Package} ${Version}\\n", "solaar"],
        ["apt-cache", "policy", "solaar", "openjdk-21-jdk"],
        ["snap", "list"],
        ["snap", "list", "android-studio"],
        ["flatpak", "list"],
        ["command", "-v", "solaar"],
        ["uname", "-a"],
        ["lsb_release", "-a"],
        ["hostname"],
        ["lsblk"],
        ["df", "-h"],
        ["df", "-h", "/mnt/fast"],
        ["findmnt"],
        ["findmnt", "/mnt/fast"],
        ["findmnt", "--target", "/home/user/git"],
        ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
        ["nvidia-smi", "--format=csv", "--query-gpu=name"],
        ["systemctl", "--user", "is-active", "brain-curator.service"],
        ["systemctl", "is-enabled", "docker.service"],
        ["systemctl", "list-units", "--no-pager", "doclayer*"],
        ["systemctl", "--user", "list-units", "--no-pager", "--all", "doclayer*"],
        ["docker", "ps", "--format", "{{.Names}} {{.Image}} {{.Status}} {{.HealthStatus}}"],
        ["docker", "ps", "-a", "--format", "table {{.Names}}"],
        ["docker", "inspect", "-f", "{{.State.Status}} {{.Config.Image}}", "app-db"],
        ["docker", "inspect", "--type", "container", "-f", "{{.RestartCount}}", "app-db"],
        ["git", "-C", "/home/user/git/example-tui", "rev-parse", "HEAD"],
        ["git", "-C", "/home/user/git/example-tui", "rev-parse", "--short", "HEAD"],
        ["git", "-C", "/home/user/git/example-tui", "log", "--oneline", "-n", "5"],
        ["git", "--no-optional-locks", "-C", "/home/user/git/example-tui", "status", "--short"],
        ["git", "-C", "/home/user/git/example-tui", "branch", "--show-current"],
        ["ls", "-la", "/home/user/Android/Sdk"],
        ["stat", "/usr/bin/solaar"],
        ["head", "-n", "200", "/home/user/git/example-tui/README.md"],
        ["head", "-n", "50", "/home/user/Downloads/pip/example-connector/notes.txt"],
    ],
)
def test_allowed_forms(argv):
    assert ok(argv) is None


def test_version_flags():
    assert ok(["solaar", "--version"]) is not None
    assert ok(["solaar", "--version"], resolved={"solaar"}) is None
    assert ok(["java", "-version"], resolved={"java"}) is None
    assert ok(["bash", "-version"], resolved={"bash"}) is not None


@pytest.mark.parametrize(
    "argv",
    [
        ["env"],
        ["printenv"],
        ["crontab", "-l"],
        ["ps", "aux"],
        ["ps"],
        ["docker", "inspect", "app-db"],
        ["docker", "inspect", "-f", "{{.Config.Env}}", "app-db"],
        ["docker", "inspect", "-f", "{{json .}}", "app-db"],
        ["docker", "inspect", "-f", "{{.State.Health.Status}}", "app-db"],
        ["docker", "ps", "--format", "{{.Labels}}"],
        ["docker", "ps", "--format", "{{.Mounts}}"],
        ["git", "-C", "/home/user/git/example-tui", "remote", "-v"],
        ["git", "-C", "/home/user/git/example-tui", "status", "--short"],
        ["git", "-C", "/home/user/git/example-tui", "rev-parse", "--short"],
        ["ls", "-la", "/home/user/.ssh"],
        ["stat", "/home/user/.gnupg"],
        ["ls", "-la", "/home/user/.config/rclone/credentials"],
        ["head", "-n", "20", "/home/user/git/app/.env"],
        ["head", "-n", "20", "/home/user/git/app/config.yaml"],
        ["head", "-n", "500", "/home/user/git/app/README.md"],
        ["head", "-n", "20", "/etc/README.md"],
        ["head", "-n", "50", "/home/user/git/../.ssh/id_ed25519.md"],
        ["head", "-n", "50", "/home/user/git/x/../../.config/secret.md"],
        ["ls", "-la", "relative/path"],
        ["ls", "-la", "~/git"],
        ["dpkg-query", "-W", "solaar;id"],
        ["ls", "-la", "/tmp|cat"],
        ["ls", "$(id)"],
        ["ls", "-la", "/tmp`id`"],
        ["dpkg-query", "-W", "-f=${HOME}", "x"],
        ["cat", "/etc/hostname"],
        ["bash", "-c", "ls"],
        ["systemctl", "status", "docker"],
        ["systemctl", "restart", "docker"],
        [],
        [""],
    ],
)
def test_refused_forms(argv):
    assert ok(argv) is not None


@pytest.mark.parametrize("host", ["192.0.2.16", "ws1.local", "ws1.lan", "gpu2", "evil"])
def test_unlisted_or_raw_hosts_refused(host):
    assert ok(["hostname"], host=host) is not None


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n",
        "db PASSWORD=hunter2 ok",
        "DB_PASSWORD=hunter2",
        "GITEA_TOKEN=abc123",
        "client_secret=zzz",
        "AWS_SECRET_ACCESS_KEY=abc",
        '{"password": "x"}',
        "api_key: sk-abc",
        "https://user:s3cret@git.example.org/repo.git",
        "token " + "ghp_" + "9aB8cD7eF6gH5iJ4kL3mN2oP1qR0sT9uV8wX",  # assembled at runtime: no token literal in source
        "hook http://localhost/hooks/aB3dE5gH7jK9mN1pQ3sT5vX7zA9cE1gI3kM5",
        "app-db postgres:16 Up\nweird aB3dE5gH7jK9mN1pQ3sT5vX7zA9cE1gI3kM5 Up\n",
    ],
)
def test_secret_scan_catches(text):
    assert possible_secret(text)


@pytest.mark.parametrize(
    "text",
    [
        "solaar 1.1.19-1 install ok installed\nopenjdk-21-jdk 21.0.8+9-1 install ok installed\n",
        "/home/user/Downloads/pip/example-connector\n/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1\n",
        "libreoffice-style-colibre-extensions-and-more 1:25.2\n",
        "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0\n",
        "token handling is described in docs\n",
    ],
)
def test_secret_scan_passes_ordinary_output(text):
    assert not possible_secret(text)


def test_labels_are_citation_safe():
    at = datetime(2026, 10, 7, 17, 30, tzinfo=UTC)
    argvs = [
        ["dpkg-query", "-W", "solaar"],
        ["head", "-n", "200", "/home/user/git/example-tui/README.md"],
        ["python3.12", "--version"],
        ["docker", "ps", "--format", "{{.Names}}"],
    ]
    for index, argv in enumerate(argvs, 1):
        label = evidence_label("ws1", argv, index, at)
        assert label.startswith("live check ws1 ") and label.endswith("@2026-10-07T17:30Z")
        assert "]" not in label and "/" not in label and "." not in label.split(" @")[0]
        record = Evidence(source_id=f"s{index}", label=label, locator="t", modality="quote", text="x", url=None)
        assert citation_faults(f"Solaar is installed [source: {label}].", [record]) is None


def test_evidence_text_keeps_both_streams_and_marks_silence():
    text = evidence_text("ws1", ["dpkg-query", "-W", "x"], 1, "", "dpkg-query: no packages found matching x\n")
    assert text.splitlines()[:3] == ["host: ws1", "command: dpkg-query -W x", "exit: 1"]
    assert "stdout:\n(no output)" in text and "stderr:\ndpkg-query: no packages found matching x" in text


def fake(returncode=0, stdout="", stderr="", exc=None, seen=None):
    def runner(argv, **kw):
        if seen is not None:
            seen.append((argv, kw))
        if exc:
            raise exc
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    return runner


def test_remote_command_uses_batchmode_ssh_with_double_dash_before_host():
    seen = []
    result = run_command(
        "ws1",
        ["dpkg-query", "-W", "solaar"],
        timeout_s=20,
        cap_bytes=16384,
        runner=fake(0, "solaar 1.1.19-1\n", seen=seen),
    )
    argv, kw = seen[0]
    assert argv == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--", "ws1", "dpkg-query -W solaar"]
    assert kw["timeout"] == 20 and kw["capture_output"] and kw["check"] is False
    assert kw["stdin"] == subprocess.DEVNULL and kw["encoding"] == "utf-8" and kw["errors"] == "replace"
    assert (result.status, result.exit_code, result.stdout) == ("ran", 0, "solaar 1.1.19-1\n")


def test_local_host_runs_through_sh_so_builtins_work():
    seen = []
    run_command(
        "worker1", ["command", "-v", "ls"], timeout_s=20, cap_bytes=16384, runner=fake(0, "/usr/bin/ls\n", seen=seen)
    )
    assert seen[0][0] == ["sh", "-c", "command -v ls"]


def test_local_builtin_really_works():
    result = run_command("worker1", ["command", "-v", "sh"], timeout_s=20, cap_bytes=16384)
    assert result.status == "ran" and result.exit_code == 0 and result.stdout.strip()


def test_stderr_is_kept_and_nonzero_exit_is_evidence():
    result = run_command(
        "ws1",
        ["dpkg-query", "-W", "x"],
        timeout_s=20,
        cap_bytes=16384,
        runner=fake(1, "", "dpkg-query: no packages found matching x\n"),
    )
    assert result.status == "ran" and result.exit_code == 1 and "no packages found" in result.stderr


def test_ssh_failure_and_launch_error_are_unreachable_but_timeout_is_evidence():
    assert run_command("ws1", ["hostname"], timeout_s=20, cap_bytes=16384, runner=fake(255)).status == "unreachable"
    assert (
        run_command("ws1", ["hostname"], timeout_s=20, cap_bytes=16384, runner=fake(exc=OSError("x"))).status
        == "unreachable"
    )
    timed = run_command(
        "ws1",
        ["df", "-h"],
        timeout_s=20,
        cap_bytes=16384,
        runner=fake(exc=subprocess.TimeoutExpired(cmd="ssh", timeout=20)),
    )
    assert timed.status == "ran" and timed.exit_code is None and "timed out after 20 s" in timed.note


def test_each_stream_is_cut_at_the_cap():
    result = run_command(
        "ws1", ["lsblk"], timeout_s=20, cap_bytes=10, runner=fake(0, "0123456789ABCDEF", "abcdefghijklmnop")
    )
    assert result.stdout == "0123456789" and result.stderr == "abcdefghij" and "truncated" in result.note


def test_secret_in_either_stream_withholds_everything():
    out = run_command("ws1", ["hostname"], timeout_s=20, cap_bytes=16384, runner=fake(0, "PASSWORD=hunter2\n"))
    err = run_command("ws1", ["hostname"], timeout_s=20, cap_bytes=16384, runner=fake(0, "ok\n", "GITEA_TOKEN=abc\n"))
    for result in (out, err):
        assert result.status == "withheld" and result.stdout == "" and result.stderr == ""


def test_head_runs_through_a_guard_that_refuses_symlink_escape(tmp_path):
    root = tmp_path / "git"
    (root / "repo").mkdir(parents=True)
    (root / "repo" / "README.md").write_text("hello\n")
    outside = tmp_path / "outside.md"
    outside.write_text("private key material\n")
    (root / "repo" / "link.md").symlink_to(outside)
    roots = (str(root) + "/",)
    good = subprocess.run(
        ["sh", "-c", remote_string(["head", "-n", "5", str(root / "repo" / "README.md")], read_roots=roots)],
        capture_output=True,
        text=True,
    )
    bad = subprocess.run(
        ["sh", "-c", remote_string(["head", "-n", "5", str(root / "repo" / "link.md")], read_roots=roots)],
        capture_output=True,
        text=True,
    )
    assert good.returncode == 0 and good.stdout == "hello\n"
    assert bad.returncode == 3 and bad.stdout == "" and "outside" in bad.stderr


def test_non_head_commands_are_plainly_joined():
    assert remote_string(["dpkg-query", "-W", "-f=${Package}\\n", "x"]) == "dpkg-query -W '-f=${Package}\\n' x"


def test_dpkg_format_newline_becomes_the_escape_dpkg_query_expands():
    from doclayer.private_commands import normalize

    argv = normalize(["dpkg-query", "-W", "-f=${Package} ${Version}\n", "solaar"])
    assert argv == ["dpkg-query", "-W", "-f=${Package} ${Version}\\n", "solaar"]
    assert ok(argv) is None
    assert normalize(["ls", "-la", "/tmp\n"]) == ["ls", "-la", "/tmp\n"]  # only dpkg-query formats change


@pytest.mark.parametrize(
    "argv",
    [
        ["id"],
        ["id", "user"],
        ["groups"],
        ["groups", "user"],
        ["head", "-n", "120", "/home/user/git/example-tui/app/build.gradle.kts"],
        ["head", "-n", "120", "/home/user/git/example-tui/build.gradle"],
        ["head", "-n", "80", "/home/user/git/example-tui/gradle/libs.versions.toml"],
        ["head", "-n", "80", "/home/user/git/brain/doclayer/pyproject.toml"],
        ["head", "-n", "80", "/home/user/git/app/package.json"],
        ["head", "-n", "40", "/home/user/git/tool/go.mod"],
    ],
)
def test_widened_forms_are_allowed(argv):
    assert ok(argv) is None


@pytest.mark.parametrize(
    "argv",
    [
        ["id", "-u", "user"],
        ["id", "user", "root"],
        ["groups", "Alex Smith"],
        ["sudo", "-l"],
        ["head", "-n", "80", "/home/user/git/example-tui/gradle.properties"],
        ["head", "-n", "80", "/home/user/git/example-tui/local.properties"],
        ["head", "-n", "80", "/home/user/git/app/package-lock.json"],
        ["head", "-n", "80", "/home/user/git/app/config.json"],
        ["head", "-n", "80", "/etc/app/pyproject.toml"],
    ],
)
def test_widened_forms_stay_bounded(argv):
    assert ok(argv) is not None


def test_head_guard_admits_build_manifests_and_refuses_properties(tmp_path):
    root = tmp_path / "git" / "app"
    root.mkdir(parents=True)
    (root / "build.gradle.kts").write_text("plugins {}\n")
    (root / "gradle.properties").write_text("signing.password=x\n")
    (root / "settings.gradle.kts").symlink_to(root / "gradle.properties")
    roots = (str(tmp_path / "git") + "/",)

    def run(name):
        return subprocess.run(
            ["sh", "-c", remote_string(["head", "-n", "5", str(root / name)], read_roots=roots)],
            capture_output=True,
            text=True,
        )

    assert run("build.gradle.kts").stdout == "plugins {}\n"
    linked = run("settings.gradle.kts")  # a manifest name that resolves to a properties file
    assert linked.returncode == 3 and linked.stdout == ""


# Read-only forms added by the assumption audit (2026-10-10, lane-c cause D):
# find, ps, free, ss and systemctl cat/show. Each is validated argument by argument.
@pytest.mark.parametrize(
    "argv",
    [
        ["find", "/etc/systemd/system", "-maxdepth", "2", "-name", "*.service"],
        ["find", "/opt/stacks", "-maxdepth", "3", "-type", "f", "-iname", "compose*.yaml"],
        ["find", "/etc/caddy", "/opt/stacks", "-type", "d"],
        ["find", "/home/user/git", "-maxdepth", "4", "-path", "*/dagster/*", "-type", "l"],
        ["find", "/var/log", "-maxdepth", "1", "-mtime", "-2"],
        ["find", "/mnt/fast", "-mtime", "+30", "-type", "f"],
        ["find", "/srv"],
        ["ps", "-eo", "pid,comm"],
        ["ps", "-eo", "pid,ppid,comm"],
        ["free", "-h"],
        ["free", "-m"],
        ["ss", "-ltn"],
        ["ss", "-lun"],
        ["ss", "-ltnu"],
        ["systemctl", "cat", "caddy.service"],
        ["systemctl", "--user", "cat", "doclayer-gap-worker.service"],
        ["systemctl", "show", "docker.service", "-p", "ActiveState,SubState,ExecMainStartTimestamp"],
        ["systemctl", "--user", "show", "brain-curator.service", "-p", "FragmentPath"],
    ],
)
def test_audit_read_only_forms_are_allowed(argv):
    assert ok(argv) is None


@pytest.mark.parametrize(
    "argv",
    [
        # find: actions, writers, formatters, symlink-following and unbounded values
        ["find", "/tmp", "-exec", "rm", "{}", ";"],
        ["find", "/tmp", "-name", "x", "-exec", "cat", "{}", "+"],
        ["find", "/tmp", "-execdir", "id", "+"],
        ["find", "/tmp", "-ok", "rm", "+"],
        ["find", "/tmp", "-okdir", "rm", "+"],
        ["find", "/tmp", "-delete"],
        ["find", "/tmp", "-name", "*.log", "-delete"],
        ["find", "/tmp", "-fprint", "/tmp/out"],
        ["find", "/tmp", "-fprint0", "/tmp/out"],
        ["find", "/tmp", "-fprintf", "/tmp/out", "%p"],
        ["find", "/tmp", "-fls", "/tmp/out"],
        ["find", "/tmp", "-printf", "%p"],
        ["find", "/tmp", "-ls"],
        ["find", "/tmp", "-print0"],
        ["find", "-L", "/tmp"],
        ["find", "/tmp", "-follow"],
        ["find", "/tmp", "-newer", "/etc/passwd"],
        ["find", "/tmp", "-o", "-name", "x"],
        ["find", "/tmp", "!", "-name", "x"],
        ["find"],
        ["find", "-name", "x"],
        ["find", "tmp"],
        ["find", "~/git"],
        ["find", "/home/user/../root"],
        ["find", "/tmp", "-maxdepth"],
        ["find", "/tmp", "-maxdepth", "two"],
        ["find", "/tmp", "-name", "-delete"],
        ["find", "/tmp", "-name", "a b"],
        ["find", "/tmp", "-type", "s"],
        ["find", "/tmp", "-type", "f,d"],
        ["find", "/tmp", "-mtime", "yesterday"],
        ["find", "/home/user/.ssh"],
        ["find", "/home/user", "-name", "id_ed25519"],
        ["find", "/tmp", "-name", "x;id"],
        ["find", "/tmp", "-name", "$(id)"],
        # ps: no args column (command lines may carry secrets), no other forms
        ["ps", "-eo", "pid,args"],
        ["ps", "-eo", "pid,comm,args"],
        ["ps", "-ef"],
        ["ps", "-eo", "pid,cmd"],
        ["ps", "-eo", "pid,comm", "-u", "root"],
        # free and ss: only the listed flag sets
        ["free"],
        ["free", "-h", "-s", "1"],
        ["free", "-g"],
        ["ss"],
        ["ss", "-ltnp"],
        ["ss", "-K"],
        ["ss", "-ltn", "dst", "1.2.3.4"],
        ["ss", "-a"],
        # systemctl: cat/show only, one unit, plain property list
        ["systemctl", "cat"],
        ["systemctl", "cat", "a.service", "b.service"],
        ["systemctl", "cat", "a.service;id"],
        ["systemctl", "show", "docker.service"],
        ["systemctl", "show", "docker.service", "-p"],
        ["systemctl", "show", "docker.service", "-p", "Environment=X"],
        ["systemctl", "show", "docker.service", "-p", "A,,B"],
        ["systemctl", "show", "docker.service", "--property=ActiveState"],
        ["systemctl", "edit", "docker.service"],
        ["systemctl", "set-property", "docker.service", "CPUQuota=1%"],
        ["systemctl", "stop", "docker.service"],
        ["systemctl", "daemon-reload"],
    ],
)
def test_audit_read_only_forms_refuse_everything_else(argv):
    assert ok(argv) is not None


def test_new_forms_output_still_passes_the_secret_scan():
    # A unit file with a credential in Environment= is withheld whole.
    unit = "[Service]\nEnvironment=DATABASE_URL=postgres://app:hunter2@db/app\nExecStart=/usr/bin/app\n"
    result = run_command(
        "ws1", ["systemctl", "cat", "app.service"], timeout_s=60, cap_bytes=131072, runner=fake(0, unit)
    )
    assert result.status == "withheld" and result.stdout == ""
    listing = run_command(
        "ws1",
        ["find", "/opt/stacks", "-name", "*.yaml"],
        timeout_s=60,
        cap_bytes=131072,
        runner=fake(0, "/opt/stacks/app/compose.yaml\n"),
    )
    assert listing.status == "ran" and listing.stdout == "/opt/stacks/app/compose.yaml\n"


def test_find_globs_reach_the_remote_shell_quoted():
    assert (
        remote_string(["find", "/etc", "-maxdepth", "1", "-name", "*.conf"]) == "find /etc -maxdepth 1 -name '*.conf'"
    )
