import json
import subprocess
from pathlib import Path

import pytest

from doclayer.vault_sync import sync_consumer


def run(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def fleet(tmp_path):
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)], check=True, capture_output=True)
    writer = tmp_path / "writer"
    subprocess.run(["git", "clone", str(remote), str(writer)], check=True, capture_output=True)
    run(writer, "config", "user.email", "fixture@example.test")
    run(writer, "config", "user.name", "Fixture")
    (writer / "wiki").mkdir()
    (writer / "wiki/page.md").write_text("old answer\n")
    (writer / "log.md").write_text("original log\n")
    run(writer, "add", "wiki/page.md", "log.md")
    run(writer, "commit", "-m", "initial")
    run(writer, "push", "origin", "main")
    consumer = tmp_path / "consumer"
    subprocess.run(["git", "clone", str(remote), str(consumer)], check=True, capture_output=True)
    state = tmp_path / "state" / "sync.json"
    return remote, writer, consumer, state


def publish(writer, path="wiki/new.md"):
    (writer / path).write_text("publisher-independent new knowledge\n")
    run(writer, "add", path)
    run(writer, "commit", "-m", "another publisher")
    run(writer, "push", "origin", "main")


def sync(fleet):
    remote, _, consumer, state = fleet
    return sync_consumer(consumer, state, expected_remote=str(remote))


def test_dirty_log_and_runtime_survive_periodic_publication(fleet):
    remote, writer, consumer, state = fleet
    log = consumer / "log.md"
    log.write_bytes(b"original log\noperator append\x00\n")
    log.chmod(0o640)
    runtime = consumer / "runtime.txt"
    runtime.write_text("local untracked state")
    before = log.read_bytes()
    publish(writer)
    result = sync(fleet)
    assert result["status"] == "current"
    assert run(consumer, "rev-parse", "HEAD") == run(writer, "rev-parse", "HEAD")
    assert (consumer / "wiki/new.md").read_text().startswith("publisher-independent")
    assert log.read_bytes() == before
    assert log.stat().st_mode & 0o777 == 0o640
    assert runtime.read_text() == "local untracked state"
    backup = Path(result["backup_path"])
    assert (backup / "files/log.md").read_bytes() == before
    assert (backup / "manifest.json").exists()
    assert run(consumer, "status", "--porcelain") == "M log.md\n?? runtime.txt"
    # Restart / missed notification: another publisher, same periodic interface.
    publish(writer, "wiki/second.md")
    assert sync(fleet)["status"] == "current"
    assert log.read_bytes() == before
    assert sync(fleet)["lag_commits"] == 0


def test_conflicting_dirty_page_is_not_stashed_or_overwritten(fleet):
    _, writer, consumer, state = fleet
    page = consumer / "wiki/page.md"
    page.write_text("operator correction\n")
    head = run(consumer, "rev-parse", "HEAD")
    publish(writer, "wiki/page.md")
    result = sync(fleet)
    assert result["status"] == "error"
    assert result["lag_commits"] == 1
    assert result["conflicting_paths"] == ["wiki/page.md"]
    assert page.read_text() == "operator correction\n"
    assert run(consumer, "rev-parse", "HEAD") == head
    assert json.loads(state.read_text())["error"]


def test_divergent_commits_require_manual_resolution(fleet):
    _, writer, consumer, _ = fleet
    run(consumer, "config", "user.email", "fixture@example.test")
    run(consumer, "config", "user.name", "Fixture")
    (consumer / "local.md").write_text("local commit")
    run(consumer, "add", "local.md")
    run(consumer, "commit", "-m", "local divergence")
    head = run(consumer, "rev-parse", "HEAD")
    publish(writer)
    assert "diverges" in sync(fleet)["error"]
    assert run(consumer, "rev-parse", "HEAD") == head
    assert run(consumer, "stash", "list") == ""


def test_outage_reports_and_recovers(fleet):
    remote, writer, consumer, state = fleet
    publish(writer)
    offline = remote.with_name("offline.git")
    remote.rename(offline)
    assert "fetch failed" in sync(fleet)["error"]
    assert not (consumer / "wiki/new.md").exists()
    offline.rename(remote)
    assert sync(fleet)["status"] == "current"
    assert json.loads(state.read_text())["error"] is None


def test_registration_and_busy_lock(fleet):
    import fcntl

    remote, _, consumer, state = fleet
    result = sync_consumer(consumer, state, expected_remote="https://wrong.example/repo")
    assert "origin differs" in result["error"]
    with (consumer / ".git/brain-vault-sync.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert "holds the lock" in sync(fleet)["error"]


def test_ignored_runtime_collision_is_not_overwritten(fleet):
    _, writer, consumer, _ = fleet
    (consumer / ".git/info/exclude").write_text("runtime.txt\n")
    (consumer / "runtime.txt").write_text("ignored runtime content")
    head = run(consumer, "rev-parse", "HEAD")
    publish(writer, "runtime.txt")
    assert sync(fleet)["status"] == "error"
    assert (consumer / "runtime.txt").read_text() == "ignored runtime content"
    assert run(consumer, "rev-parse", "HEAD") == head


def test_concurrent_append_detected_without_reverting_it(fleet, monkeypatch):
    import doclayer.vault_sync as module

    _, writer, consumer, _ = fleet
    log = consumer / "log.md"
    log.write_text("operator log\n")
    publish(writer)
    real_git = module.git

    def git_with_append(repo, *args):
        value = real_git(repo, *args)
        if "merge" in args:
            with log.open("a") as file:
                file.write("concurrent append\n")
        return value

    monkeypatch.setattr(module, "git", git_with_append)
    assert "changed during update" in sync(fleet)["error"]
    assert log.read_text() == "operator log\nconcurrent append\n"
    assert (consumer / "wiki/new.md").exists()


def test_staged_operator_edit_not_committed_or_lost(fleet):
    _, writer, consumer, _ = fleet
    (consumer / "log.md").write_text("staged operator append\n")
    run(consumer, "add", "log.md")
    staged = run(consumer, "show", ":log.md")
    publish(writer)
    result = sync(fleet)
    # Git may refuse to merge a staged edit. Both refusal and safe FF preserve it.
    assert result["status"] in ("error", "current")
    assert run(consumer, "show", ":log.md") == staged
    assert (consumer / "log.md").read_text() == "staged operator append\n"
    assert run(consumer, "diff", "--cached", "--name-only") == "log.md"


def test_active_git_operation_is_refused(fleet):
    _, writer, consumer, _ = fleet
    publish(writer)
    (consumer / ".git/MERGE_HEAD").write_text(run(writer, "rev-parse", "HEAD"))
    assert "operation in progress" in sync(fleet)["error"]
    assert not (consumer / "wiki/new.md").exists()


def test_staged_and_unstaged_net_cancellation_is_still_a_conflict(fleet):
    _, writer, consumer, _ = fleet
    page = consumer / "wiki/page.md"
    original = page.read_text()
    page.write_text("staged edit")
    run(consumer, "add", "wiki/page.md")
    page.write_text(original)
    publish(writer, "wiki/page.md")
    result = sync(fleet)
    assert result["conflicting_paths"] == ["wiki/page.md"]
    assert run(consumer, "show", ":wiki/page.md") == "staged edit"
    assert page.read_text() == original


def test_corrupt_status_does_not_block_reconciliation(fleet):
    _, writer, consumer, state = fleet
    state.parent.mkdir()
    state.write_text("{not json")
    publish(writer)
    assert sync(fleet)["status"] == "current"
    assert json.loads(state.read_text())["lag_commits"] == 0


def test_status_storage_error_is_actionable(fleet, monkeypatch):
    import doclayer.vault_sync as module

    remote, _, consumer, state = fleet

    def fail(*args):
        raise PermissionError("private filesystem details")

    monkeypatch.setattr(module, "atomic_json", fail)
    result = sync_consumer(consumer, state, expected_remote=str(remote))
    assert result["status"] == "error"
    assert "cannot persist" in result["error"]
    assert "private filesystem details" not in result["error"]


def test_cli_attempts_other_consumer_with_corrupt_status(fleet, tmp_path):
    import sys

    remote, writer, consumer, state = fleet
    second = tmp_path / "second-consumer"
    subprocess.run(["git", "clone", str(remote), str(second)], check=True, capture_output=True)
    state.parent.mkdir()
    state.write_text("{broken")
    config = tmp_path / "consumers.json"
    config.write_text(
        json.dumps(
            {
                "consumers": [
                    {"repo": str(consumer), "remote": str(remote), "state": str(state)},
                    {"repo": str(second), "remote": str(remote), "state": str(tmp_path / "second-state.json")},
                ]
            }
        )
    )
    publish(writer)
    process = subprocess.run(
        [sys.executable, "-m", "doclayer.vault_sync", "--config", str(config)], capture_output=True, text=True
    )
    assert process.returncode == 0, process.stderr
    assert all(item["status"] == "current" for item in json.loads(process.stdout)["consumers"])
    assert (second / "wiki/new.md").exists()


@pytest.mark.parametrize(
    "route",
    [
        "ssh://git@canonical.example.test:2224/Other/brain",
        "ssh://git@other.example.test:2224/Example-org/brain",
        "http://canonical.example.test/Example-org/brain",
        "https://canonical.example.test:444/Example-org/brain",
        "file:///tmp/unrelated-brain",
    ],
)
def test_effective_rewrite_requires_same_canonical_identity(fleet, route):
    from doclayer.vault_sync import SyncError, validate_origin

    _, _, consumer, _ = fleet
    declared = "https://canonical.example.test/Example-org/brain"
    run(consumer, "remote", "set-url", "origin", declared)
    run(consumer, "config", f"url.{route}.insteadOf", declared)
    with pytest.raises(SyncError, match="effective origin rewrite differs"):
        validate_origin(consumer, declared)


@pytest.mark.parametrize(
    "route",
    [
        "ssh://git@canonical.example.test:2224/Example-org/brain",
        "git@canonical.example.test:Example-org/brain.git",
    ],
)
def test_equivalent_secure_git_routes_are_supported(fleet, route):
    from doclayer.vault_sync import validate_origin

    _, _, consumer, _ = fleet
    declared = "https://canonical.example.test/Example-org/brain"
    run(consumer, "remote", "set-url", "origin", declared)
    run(consumer, "config", f"url.{route}.insteadOf", declared)
    validate_origin(consumer, declared)
