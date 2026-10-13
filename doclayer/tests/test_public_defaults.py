import dataclasses
import os
import re

import pytest

from doclayer.curator.config import CuratorConfig, load_config
from doclayer.curator.forgejo import CuratorForgejo
from doclayer.settings import Settings

# Brackets keep this pattern from matching its own text.
PRIVATE = re.compile(
    r"/home/chr[i]s|lateape[x]|192\.168\.|19216[8]"
    r"|\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]+\."
    r"|\b(e[d]8|mini-[g]9|z4[g]4|r34[0]|ubuntuserve[r]|inferencenod[e]|infernode[v]|unrai[d]"
    r"|dell-768[0]|mac-min[i]|[j]h|t760[0]|r73[0]|tanfer[n]os?-min[i])\b"
    r"|\bchri[s]\b|tanfern[o]|stormlen[s]|paddocklin[k]|crewchie[f]|justtrack[i]t|ctanwe[b]"
    r"|track-(db|form|coach|intelligence)|trackfor[m]|qbittorren[t]|huihu[i]|ablitera[t]"
    r"|z-?librar[y]|libge[n]|superpower[s]|astra-handof[f]|astra-gpt[6]|astra pilo[t]"
    r"|zcod[e]|pitwal[l]|sportsboo[k]|pregam[e]|\bpf[f]\b|\bcf[b]\b|colli[e]|herd[r]|gate-ws[2]|40187109[0]"
    r"|meridia[n]",
    re.I,
)


def _strings(obj):
    if dataclasses.is_dataclass(obj):
        for f in dataclasses.fields(obj):
            yield from _strings(getattr(obj, f.name))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)
    else:
        yield str(obj)


def _clean_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in list(os.environ):
        if k.startswith("DOC_LAYER_"):
            monkeypatch.delenv(k)


def test_curator_defaults_carry_no_private_values():
    assert not [s for s in _strings(CuratorConfig()) if PRIVATE.search(s)]


def test_settings_defaults_carry_no_private_values(monkeypatch, tmp_path):
    _clean_env(monkeypatch, tmp_path)
    # tmp_path carries the OS user name; only the defaults themselves are checked.
    values = [str(v).replace(str(tmp_path), "") for v in vars(Settings()).values()]
    assert not [s for s in values if PRIVATE.search(s)]
    assert not PRIVATE.search(str(Settings().brain_repo).replace(str(tmp_path), ""))


def test_settings_defaults_are_local_or_unset(monkeypatch, tmp_path):
    _clean_env(monkeypatch, tmp_path)
    s = Settings()
    assert s.vo_rag_url == "http://localhost:8001"
    assert s.qdrant_url == "http://localhost:6333"
    assert s.forgejo_url == ""
    assert s.ssh_host == ""
    assert s.search_host_ip == ""
    assert not hasattr(s, "ubuntu_server_ip")
    assert s.gap_owner_command == "doclayer-gap-ops"
    assert s.route_config is None
    assert s.brain_repo == tmp_path / "brain"


def test_search_host_ip_env(monkeypatch, tmp_path):
    _clean_env(monkeypatch, tmp_path)
    monkeypatch.setenv("DOC_LAYER_SEARCH_HOST_IP", "10.0.0.5")
    assert Settings().search_host_ip == "10.0.0.5"


def test_default_inference_is_localhost():
    assert CuratorConfig().inference.url == "http://localhost:8000/v1"
    assert CuratorConfig().inference.model == "Qwen/Qwen3-32B"


def test_force_merge_is_opt_in():
    assert CuratorConfig().publish.force_merge is False
    assert CuratorConfig().publish.forgejo_url == ""


def test_empty_gap_owner_host_means_local_owner(monkeypatch, tmp_path):
    _clean_env(monkeypatch, tmp_path)
    s = Settings()
    assert s.gap_owner_host == ""
    assert s.gap_owner == "local"


def test_gap_owner_host_naming_another_machine_means_ssh(monkeypatch, tmp_path):
    _clean_env(monkeypatch, tmp_path)
    monkeypatch.setenv("DOC_LAYER_GAP_OWNER_HOST", "no-such-host-xyz")
    assert Settings().gap_owner == "ssh"


def test_explicit_gap_owner_wins_over_empty_host(monkeypatch, tmp_path):
    _clean_env(monkeypatch, tmp_path)
    monkeypatch.setenv("DOC_LAYER_GAP_OWNER", "ssh")
    assert Settings().gap_owner == "ssh"


def test_legacy_config_still_loads(tmp_path):
    legacy = tmp_path / "config.yaml"
    legacy.write_text(
        "inference: {url: 'http://gpu1:9292/v1', model: m}\n"
        "private_writer: {hosts: [ws1], host_notes: {ws1: workstation}, retired_key: 1}\n"
        "publish: {force_merge: true, retired_key: x}\nssh: {host: worker1}\nunknown_section: {a: 1}\n"
    )
    cfg = load_config(legacy)
    assert cfg.inference.url == "http://gpu1:9292/v1" and cfg.publish.force_merge is True


def test_load_config_without_file_has_unconfigured_forgejo(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("BRAIN_CURATOR_CONFIG", raising=False)
    cfg = load_config()
    assert cfg.publish.forgejo_url == ""


def test_forgejo_client_requires_configured_url():
    with pytest.raises(ValueError, match="publish.forgejo_url is not configured"):
        CuratorForgejo("o", "r", token="t")
    with pytest.raises(ValueError, match="publish.forgejo_url is not configured"):
        CuratorForgejo.from_config(CuratorConfig(), "ssh://git@example.org/o/r", token="t")


def test_forgejo_client_uses_configured_url():
    cfg = CuratorConfig()
    cfg.publish.forgejo_url = "https://git.example.org/"
    client = CuratorForgejo.from_config(cfg, "ssh://git@git.example.org/o/r", token="t")
    assert client.base_url == "https://git.example.org"
    assert client.force_merge is False


def test_route_facts_without_route_config_is_empty(monkeypatch, tmp_path):
    from doclayer import resolver

    monkeypatch.setattr(resolver.settings, "route_config", None)
    assert resolver.route_facts() == ([], [])


def test_ssh_gap_owner_without_host_raises_naming_the_setting(monkeypatch, tmp_path):
    from doclayer.gap_client import GapClient

    _clean_env(monkeypatch, tmp_path)
    monkeypatch.setenv("DOC_LAYER_GAP_OWNER", "ssh")
    settings = Settings()
    assert settings.gap_owner_host == ""

    def no_ssh(*a, **k):
        raise AssertionError("ssh must not be started with an empty host")

    monkeypatch.setattr("doclayer.gap_client.subprocess.run", no_ssh)
    with pytest.raises(ValueError, match="DOC_LAYER_GAP_OWNER_HOST"):
        GapClient(settings).operation("status", {})


def test_src_has_no_private_markers():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    hits = [
        f"{p}:{i}"
        for p in src.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if PRIVATE.search(line) or re.search(r"\bChris\b", line)
    ]
    assert hits == []


def test_new_host_settings_defaults_are_public(monkeypatch, tmp_path):
    from pathlib import Path

    _clean_env(monkeypatch, tmp_path)
    s = Settings()
    assert s.searxng_url == ""
    assert s.searxng_cert_dir == tmp_path / ".config/doclayer/searxng"
    assert s.search_release_link == ""
    assert s.gateway_containers == "litellm|litellm-db"
    assert s.private_read_roots == (f"{tmp_path}/git/", f"{tmp_path}/src/")
    monkeypatch.setenv("DOC_LAYER_PRIVATE_READ_ROOTS", "~/a:/srv/b/")
    assert Settings().private_read_roots == (f"{tmp_path}/a/", "/srv/b/")
    assert isinstance(s.searxng_cert_dir, Path)


def _no_network(monkeypatch):
    import httpx

    def boom(*a, **k):
        raise AssertionError("no network or ssh when unconfigured")

    monkeypatch.setattr("doclayer.livechecks.subprocess.run", boom)
    monkeypatch.setattr(httpx.AsyncClient, "send", boom)
    monkeypatch.setattr(httpx, "request", boom)


@pytest.mark.parametrize(
    "check, field, env",
    [
        ("vo-rag-release", "ssh_host", "DOC_LAYER_SSH_HOST"),
        ("vo-rag-release", "search_release_link", "DOC_LAYER_SEARCH_RELEASE_LINK"),
        ("vo-rag-api", "ssh_host", "DOC_LAYER_SSH_HOST"),
        ("vo-rag-api", "search_host_ip", "DOC_LAYER_SEARCH_HOST_IP"),
        ("litellm-container", "ssh_host", "DOC_LAYER_SSH_HOST"),
        ("vo-rag-brain-collection", "qdrant_url", "DOC_LAYER_QDRANT_URL"),
        ("token-plan-endpoint", "ssh_host", "DOC_LAYER_SSH_HOST"),
    ],
)
def test_live_check_with_empty_setting_reports_not_configured(monkeypatch, check, field, env):
    import asyncio

    from doclayer import livechecks

    for name, value in (
        ("ssh_host", "svc1"),
        ("search_release_link", "/srv/releases/current"),
        ("search_host_ip", "10.0.0.5"),
        ("qdrant_url", "http://localhost:6333"),
    ):
        monkeypatch.setattr(livechecks.settings, name, value)
    monkeypatch.setattr(livechecks.settings, field, "")
    _no_network(monkeypatch)
    (result,) = asyncio.run(livechecks.run_checks([check], timeout_s=1))
    assert result.error and "not configured" in result.error and env in result.error


def test_brain_sync_state_without_ssh_host_never_runs_ssh(monkeypatch, tmp_path):
    from doclayer import livechecks

    monkeypatch.setattr(livechecks.settings, "ssh_host", "")
    monkeypatch.setattr(livechecks.settings, "brain_sync_state", str(tmp_path / "missing.json"))
    _no_network(monkeypatch)
    assert livechecks.brain_sync_state() == {}


def test_landing_without_forgejo_url_is_not_configured(monkeypatch, tmp_path):
    from doclayer import landing

    monkeypatch.setattr(landing.settings, "forgejo_url", "")
    _no_network(monkeypatch)
    monkeypatch.setattr(landing, "_git", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no git")))
    recipe = tmp_path / "repo" / "doclayer" / "recipes" / "prod.yaml"
    result = landing.land_refresh(recipe, [{"slug": "a", "result": "ok"}])
    assert result.action == "not-configured"
    assert "publishing not configured" in result.reasons[0]
    assert "DOC_LAYER_FORGEJO_URL" in result.reasons[0]
    with pytest.raises(RuntimeError, match="publishing not configured"):
        landing.ForgejoClient(token="t").create_pr("o", "r", title="t", head="h", base="b", body="")


def test_empty_searxng_url_skips_search_but_other_discovery_runs(monkeypatch):
    import httpx

    from doclayer import discovery, resolver
    from doclayer.coverage import Catalog

    monkeypatch.setattr(discovery.settings, "searxng_url", "")
    monkeypatch.setattr(
        discovery,
        "_search_candidates",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("searxng must be skipped")),
    )
    registry_calls = []

    async def registry(*args, **kwargs):
        registry_calls.append(args)
        return {"content": [{"text": "no libraries"}]}

    monkeypatch.setattr(resolver, "_context7_call", registry)
    real_client = httpx.Client
    monkeypatch.setattr(
        discovery.httpx,
        "Client",
        lambda **kw: real_client(
            transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError("no request"))),
            **kw,
        ),
    )
    gap = {
        "gap_id": "gap-public",
        "identity": "unknown:widget",
        "representative_question": "How does Widget configure retries?",
    }
    result = discovery.discover(gap, Catalog(status="ok", revision="a" * 40, policy_version=1), use_semantics=False)
    assert registry_calls, "Context7 discovery still runs"
    assert any("SearXNG" in n and "DOC_LAYER_SEARXNG_URL" in n for n in result.notes)
    assert result.status == "unresolved"


def test_gap_migration_retire_requires_owner(monkeypatch, tmp_path, capsys):
    from doclayer import gap_migration

    monkeypatch.setattr(
        "sys.argv", ["gap-migration", "--root", str(tmp_path), "--journal", str(tmp_path / "j"), "--apply"]
    )
    with pytest.raises(SystemExit) as exit_info:
        gap_migration.main()
    assert exit_info.value.code == 2
    assert "--owner is required with --apply" in capsys.readouterr().err


def test_gap_migration_inventory_needs_no_owner(monkeypatch, tmp_path, capsys):
    from doclayer import gap_migration

    monkeypatch.setattr(gap_migration, "inventory", lambda root: {"phase": "inventory", "root": str(root)})
    monkeypatch.setattr("sys.argv", ["gap-migration", "--root", str(tmp_path)])
    gap_migration.main()
    assert '"phase": "inventory"' in capsys.readouterr().out


def test_private_read_roots_drive_refusals_and_prompt(monkeypatch):
    from doclayer import private_commands
    from doclayer.curator.config import PrivateWriterConfig
    from doclayer.private_writer import plan_system

    monkeypatch.setattr(private_commands.settings, "private_read_roots", ("/srv/repos/",))
    refusal = private_commands.refusal(
        "ws1", ["head", "-n", "5", "/opt/other/README.md"], hosts=["ws1"], resolved=set()
    )
    assert refusal == "file reads only under /srv/repos"
    assert (
        private_commands.refusal("ws1", ["head", "-n", "5", "/srv/repos/app/README.md"], hosts=["ws1"], resolved=set())
        is None
    )
    assert "files under /srv/repos (project build files" in plan_system(PrivateWriterConfig())


def test_run_command_local_host_defaults_to_this_machine(monkeypatch):
    import socket

    from doclayer import private_commands

    monkeypatch.setattr(private_commands.settings, "gap_owner_host", "")
    seen = []

    def runner(argv, **kw):
        seen.append(argv)
        return type("Done", (), {"returncode": 0, "stdout": "x\n", "stderr": ""})()

    private_commands.run_command(socket.gethostname(), ["hostname"], timeout_s=5, cap_bytes=100, runner=runner)
    assert seen[0][:2] == ["sh", "-c"]


def test_question_scope_hosts_and_terms_come_from_configuration(identities, local_profiles):
    from doclayer.question_scope import private_question

    question = "Which Grafana version runs on svc1?"
    assert not private_question("Which Grafana version is current upstream?")
    assert not private_question("What does app1 run for Grafana?")
    identities(hosts=["app1"])
    assert private_question("What does app1 run for Grafana?")
    local_profiles()  # fixture terms: svc1 / analytics
    assert private_question(question)
    assert private_question("How is analytics wired?")


def test_every_vault_page_concept_id_matches_the_schema():
    from pathlib import Path

    from doclayer.curator.state import CONCEPT_ID_RE

    root = Path(__file__).resolve().parents[2]
    pages = [*root.glob("*.md")]
    for area in ("wiki", "infra", "inbox", "sources", "templates"):
        pages += (root / area).rglob("*.md")
    checked, bad = 0, []
    for page in pages:
        text = page.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            continue
        frontmatter = text.split("\n---", 1)[0]
        for line in frontmatter.splitlines():
            if line.startswith("concept_id:"):
                checked += 1
                value = line.split(":", 1)[1].strip()
                if not CONCEPT_ID_RE.match(value):
                    bad.append(f"{page.relative_to(root)}: {value}")
    assert checked and bad == []
