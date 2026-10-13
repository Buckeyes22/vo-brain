import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from doclayer.brainrepo import BrainRepo
from doclayer.settings import BrainRepoConfigurationError, Settings
from doclayer.vault_sync import sync_consumer


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def canonical(tmp_path):
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)], check=True, capture_output=True)
    writer = tmp_path / "writer"
    subprocess.run(["git", "clone", str(remote), str(writer)], check=True, capture_output=True)
    git(writer, "config", "user.email", "test@example.test")
    git(writer, "config", "user.name", "Test")
    (writer / "wiki").mkdir()
    (writer / "wiki/model.md").write_text("---\ntitle: Model\n---\noriginal knowledge\n")
    git(writer, "add", ".")
    git(writer, "commit", "-m", "initial")
    git(writer, "push", "origin", "main")
    return remote, writer


def pointer(home, value):
    path = home / ".config/doclayer/brain-repo"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    return path


def test_dynamic_pointer_and_env_precedence(monkeypatch, tmp_path, canonical):
    _, writer = canonical
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("DOC_LAYER_BRAIN_REPO", raising=False)
    settings = Settings()
    assert settings.brain_repo == Path("~/brain").expanduser()
    saved = pointer(tmp_path, str(writer) + "\n")
    assert settings.brain_repo == writer
    saved.write_text("relative/path")
    with pytest.raises(BrainRepoConfigurationError):
        _ = settings.brain_repo
    monkeypatch.setenv("DOC_LAYER_BRAIN_REPO", str(writer))
    assert settings.brain_repo == writer
    monkeypatch.delenv("DOC_LAYER_BRAIN_REPO")
    saved.unlink()
    assert settings.brain_repo == Path("~/brain").expanduser()


@pytest.mark.parametrize("value", ["", "relative/path", "/missing/reference", "{home}"])
def test_bad_pointer_never_reads_developer_tree(monkeypatch, tmp_path, value):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("DOC_LAYER_BRAIN_REPO", raising=False)
    pointer(tmp_path, value.format(home=tmp_path))
    with pytest.raises(BrainRepoConfigurationError, match="repair the pointer"):
        _ = Settings().brain_repo


@pytest.mark.asyncio
async def test_bad_pointer_preserves_network_answers(monkeypatch, tmp_path):
    from doclayer import resolver

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("DOC_LAYER_BRAIN_REPO", raising=False)
    pointer(tmp_path, "/missing/reference")
    monkeypatch.setattr(resolver, "settings", Settings())

    async def query(*args):
        return ([{"path": "wiki/model.md", "content": "API knowledge", "heading": "Model"}], None)

    async def docs(*args):
        return {"summary": "documentation"}

    monkeypatch.setattr(resolver, "query_vo_rag", query)
    monkeypatch.setattr(resolver, "query_context7", docs)
    monkeypatch.setattr(resolver, "route_facts", lambda: ([], []))
    monkeypatch.setattr(resolver, "index_lag_flag", lambda repo: [])
    answer = await resolver.resolve("model capabilities", product="model", live=False)
    assert answer.search_hits[0]["content"] == "API knowledge"
    assert answer.context7["summary"] == "documentation"
    assert not answer.doc_facts
    assert any("canonical brain reference unavailable" in flag for flag in answer.flags)


def test_snapshot_truth_survives_dirty_file_and_advance(canonical):
    _, writer = canonical
    repo = BrainRepo(writer, snapshot=True)
    captured = repo.head_commit()
    (writer / "wiki/model.md").write_text("unsupported dirty knowledge")
    assert "original knowledge" in repo.read_page("wiki/model.md").body
    (writer / "wiki/new.md").write_text("new page at later revision")
    git(writer, "add", ".")
    git(writer, "commit", "-m", "advance")
    assert git(writer, "rev-parse", "HEAD") != captured
    page = repo.read_page("wiki/model.md")
    assert page.commit == captured
    assert "unsupported" not in page.body
    assert "wiki/new.md" not in repo.markdown_paths()
    assert not repo.has_path("wiki/new.md")


@pytest.fixture
def installer():
    path = Path(__file__).parents[1] / "deploy/vault-sync/install-workstation.py"
    spec = importlib.util.spec_from_file_location("reference_installer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_install_preserves_other_consumers_and_backups_then_periodically_syncs(
    monkeypatch, tmp_path, canonical, installer
):
    remote, writer = canonical
    home = tmp_path / "home"
    executable = tmp_path / "sync"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    registry = home / ".config/brain-vault-sync/consumers.json"
    registry.parent.mkdir(parents=True)
    other = {"repo": "/operator/other", "remote": "https://example.test", "branch": "main"}
    registry.write_text(json.dumps({"consumers": [other], "operator": "preserve"}))
    registry.chmod(0o640)
    saved_pointer = pointer(home, "/prior/reference\n")
    saved_pointer.chmod(0o640)
    private = home / ".local/state/brain-vault-sync/operator.json"
    private.parent.mkdir(parents=True)
    private.write_text('{"private": "status"}')
    private.chmod(0o600)
    real_run = installer.run

    def invoke(*args):
        if args[0] == str(executable):
            for entry in json.loads(Path(args[-1]).read_text())["consumers"]:
                if entry["repo"] != other["repo"]:
                    assert (
                        sync_consumer(Path(entry["repo"]), Path(entry["state"]), expected_remote=entry["remote"])[
                            "status"
                        ]
                        == "current"
                    )
            return ""
        return real_run(*args)

    monkeypatch.setattr(installer, "run", invoke)
    backup = installer.install(
        home, Path(installer.__file__).parent, remote=str(remote), executable=executable, activate=False
    )
    config = json.loads(registry.read_text())
    assert config["consumers"][0] == other
    assert config["operator"] == "preserve"
    service = (home / ".config/systemd/user/brain-vault-sync.service").read_text()
    assert "PrivateTmp=false" in service
    assert "PrivateTmp=true" not in service
    assert "NoNewPrivileges=true" in service
    assert "UMask=0077" in service
    assert f"ExecStart={executable} --config {registry}" in service
    # worker1's independently working unit retains its existing policy.
    assert "PrivateTmp=true" in (Path(installer.__file__).parent / "brain-vault-sync.service").read_text()
    assert (backup / "brain-repo").read_text() == "/prior/reference\n"
    assert (backup / "brain-repo").stat().st_mode & 0o777 == 0o640
    assert (backup / "consumers.json").stat().st_mode & 0o777 == 0o640
    assert (backup / "state-operator.json").read_bytes() == private.read_bytes()
    repo = Path(saved_pointer.read_text().strip())
    assert git(repo, "remote", "get-url", "origin") == str(remote)
    (writer / "wiki/new.md").write_text("another publisher knowledge")
    git(writer, "add", ".")
    git(writer, "commit", "-m", "new knowledge")
    git(writer, "push", "origin", "main")
    entry = config["consumers"][1]
    assert sync_consumer(repo, Path(entry["state"]), expected_remote=str(remote))["status"] == "current"
    assert "another publisher" in BrainRepo(repo, snapshot=True).read_page("wiki/new.md").body
    installer.install(home, Path(installer.__file__).parent, remote=str(remote), executable=executable, activate=False)
    assert len(json.loads(registry.read_text())["consumers"]) == 2


def test_install_refuses_unrelated_path(tmp_path, canonical, installer):
    remote, _ = canonical
    executable = tmp_path / "sync"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    repo = tmp_path / ".local/share/doclayer/brain-reference"
    repo.mkdir(parents=True)
    (repo / "operator.txt").write_text("preserve")
    with pytest.raises(ValueError, match="unrelated"):
        installer.install(
            tmp_path, Path(installer.__file__).parent, remote=str(remote), executable=executable, activate=False
        )
    assert (repo / "operator.txt").read_text() == "preserve"
    assert not (tmp_path / ".config/doclayer/brain-repo").exists()


def test_installer_real_cli_and_failed_sync_do_not_replace_pointer(tmp_path, canonical, installer):
    remote, writer = canonical
    home = tmp_path / "home"
    executable = Path(__file__).parents[1] / ".venv/bin/brain-vault-sync"
    installer.install(home, Path(installer.__file__).parent, remote=str(remote), executable=executable, activate=False)
    saved = home / ".config/doclayer/brain-repo"
    repo = Path(saved.read_text().strip())
    saved.write_text("/previous/configuration\n")
    (repo / "wiki/model.md").write_text("operator correction")
    (writer / "wiki/model.md").write_text("canonical different content")
    git(writer, "add", ".")
    git(writer, "commit", "-m", "conflicting publication")
    git(writer, "push", "origin", "main")
    with pytest.raises(RuntimeError, match="failed"):
        installer.install(
            home, Path(installer.__file__).parent, remote=str(remote), executable=executable, activate=False
        )
    assert saved.read_text() == "/previous/configuration\n"
    assert (repo / "wiki/model.md").read_text() == "operator correction"


@pytest.mark.parametrize("mismatch", ["remote", "branch"])
def test_installer_refuses_unrelated_repository_registration(tmp_path, canonical, installer, mismatch):
    remote, _ = canonical
    executable = Path(__file__).parents[1] / ".venv/bin/brain-vault-sync"
    installer.install(
        tmp_path, Path(installer.__file__).parent, remote=str(remote), executable=executable, activate=False
    )
    repo = tmp_path / ".local/share/doclayer/brain-reference"
    if mismatch == "remote":
        git(repo, "remote", "set-url", "origin", "https://unrelated.example.test/brain")
    else:
        git(repo, "checkout", "-b", "operator-development")
    pointer_bytes = (tmp_path / ".config/doclayer/brain-repo").read_bytes()
    with pytest.raises(ValueError, match="unrelated origin|not on main"):
        installer.install(
            tmp_path, Path(installer.__file__).parent, remote=str(remote), executable=executable, activate=False
        )
    assert (tmp_path / ".config/doclayer/brain-repo").read_bytes() == pointer_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["listing", "blob"])
async def test_valid_pointer_later_git_failure_preserves_other_sources(monkeypatch, tmp_path, canonical, failure):
    from doclayer import resolver

    _, writer = canonical
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("DOC_LAYER_BRAIN_REPO", raising=False)
    pointer(tmp_path, str(writer))
    monkeypatch.setattr(resolver, "settings", Settings())

    async def query(*args):
        return ([{"path": "wiki/model.md", "content": "API knowledge", "heading": "Model"}], None)

    async def docs(*args):
        return {"summary": "documentation"}

    monkeypatch.setattr(resolver, "query_vo_rag", query)
    monkeypatch.setattr(resolver, "query_context7", docs)
    monkeypatch.setattr(resolver, "route_facts", lambda: ([], []))
    monkeypatch.setattr(resolver, "index_lag_flag", lambda repo: [])

    def fail(*args):
        raise RuntimeError("fixture reference I/O failure")

    monkeypatch.setattr(BrainRepo, "markdown_paths" if failure == "listing" else "read_text", fail)
    # Explicit product skips preliminary guessing; failure happens during Git
    # collection after the independent API/doc requests have succeeded.
    answer = await resolver.resolve("model capabilities", product="model", live=False)
    assert answer.search_hits[0]["content"] == "API knowledge"
    assert answer.context7["summary"] == "documentation"
    assert not answer.git_facts and not answer.doc_facts
    assert any("fixture reference I/O failure" in flag for flag in answer.flags)


@pytest.mark.asyncio
@pytest.mark.parametrize("pointer_change", ["remove", "replace"])
async def test_pointer_race_cannot_downgrade_selected_committed_reference(
    monkeypatch, tmp_path, canonical, pointer_change
):
    from doclayer import resolver

    _, writer = canonical
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("DOC_LAYER_BRAIN_REPO", raising=False)
    saved = pointer(tmp_path, str(writer))
    configured = Settings()
    select = configured.brain_repo_selection
    calls = []

    def select_then_change():
        selected = select()
        calls.append(selected)
        if pointer_change == "remove":
            saved.unlink()
        else:
            saved.write_text("/missing/replacement")
        return selected

    monkeypatch.setattr(configured, "brain_repo_selection", select_then_change)
    monkeypatch.setattr(resolver, "settings", configured)
    (writer / "wiki/model.md").write_text("uncommitted unsupported knowledge")
    observed = []

    def construct(root, *, snapshot):
        repo = BrainRepo(root, snapshot=snapshot)
        observed.append((root, snapshot, repo.read_page("wiki/model.md").body))
        return repo

    monkeypatch.setattr(resolver, "BrainRepo", construct)

    async def query(*args):
        return ([], None)

    async def docs(*args):
        return {"summary": "documentation"}

    monkeypatch.setattr(resolver, "query_vo_rag", query)
    monkeypatch.setattr(resolver, "query_context7", docs)
    monkeypatch.setattr(resolver, "route_facts", lambda: ([], []))
    monkeypatch.setattr(resolver, "index_lag_flag", lambda repo: [])
    await resolver.resolve("model knowledge", product="model", live=False)
    assert calls == [(writer, True)]
    assert observed == [(writer, True, "original knowledge\n")]


def test_selection_preserves_explicit_override_and_legacy_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("DOC_LAYER_BRAIN_REPO", raising=False)
    settings = Settings()
    assert settings.brain_repo_selection() == (Path("~/brain").expanduser(), False)
    monkeypatch.setenv("DOC_LAYER_BRAIN_REPO", "/explicit/environment")
    assert settings.brain_repo_selection() == (Path("/explicit/environment"), False)
    settings.brain_repo = "/explicit/caller"
    assert settings.brain_repo_selection() == (Path("/explicit/caller"), False)


def test_existing_https_to_ssh_instead_of_clone_sync_and_installer_rerun(monkeypatch, tmp_path, canonical, installer):
    import shlex

    from doclayer.vault_sync import validate_origin

    remote, writer = canonical
    global_config = tmp_path / "gitconfig"
    global_config.write_text(
        '[url "ssh://git@canonical.example.test:2224/"]\n\tinsteadOf = https://canonical.example.test/\n'
    )
    transport = tmp_path / "ssh-test-transport"
    transport.write_text("#!/bin/sh\nexec git-upload-pack " + shlex.quote(str(remote)) + "\n")
    transport.chmod(0o700)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_SSH_COMMAND", shlex.quote(str(transport)))
    monkeypatch.setenv("GIT_SSH_VARIANT", "ssh")
    declared = "https://canonical.example.test/Example-org/brain"
    home = tmp_path / "home"
    executable = Path(__file__).parents[1] / ".venv/bin/brain-vault-sync"
    artifacts = Path(installer.__file__).parent
    installer.install(home, artifacts, remote=declared, executable=executable, activate=False)
    repo = home / ".local/share/doclayer/brain-reference"
    assert git(repo, "config", "--get", "remote.origin.url") == declared
    assert git(repo, "remote", "get-url", "origin") == "ssh://git@canonical.example.test:2224/Example-org/brain"
    validate_origin(repo, declared)
    (writer / "wiki/new.md").write_text("knowledge through existing authentication route")
    git(writer, "add", ".")
    git(writer, "commit", "-m", "another publisher")
    git(writer, "push", "origin", "main")
    installer.install(home, artifacts, remote=declared, executable=executable, activate=False)
    assert (repo / "wiki/new.md").read_text().startswith("knowledge through existing")
    assert global_config.read_text().startswith('[url "ssh://git@canonical.example.test:2224/"]')
    # Same declared URL with an unrelated effective host must still refuse,
    # without network mutation or activating another pointer.
    before = git(repo, "rev-parse", "HEAD")
    global_config.write_text(
        '[url "ssh://git@unrelated.example.test:2224/"]\n\tinsteadOf = https://canonical.example.test/\n'
    )
    with pytest.raises(ValueError, match="unrelated origin or effective rewrite"):
        installer.install(home, artifacts, remote=declared, executable=executable, activate=False)
    result = sync_consumer(repo, home / "refused.json", expected_remote=declared)
    assert result["status"] == "error"
    assert "effective origin rewrite differs" in result["error"]
    assert git(repo, "rev-parse", "HEAD") == before
