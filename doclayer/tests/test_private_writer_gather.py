import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from doclayer.curator.config import CuratorConfig, PrivateWriterConfig
from doclayer.curator.model import ModelClient
from doclayer.curator.state import Usage
from doclayer.gaps import GapLedger
from doclayer.private_writer import PLAN_SYSTEM, PRIVATE_ORIGIN, gather_evidence, private_sources

NOW = datetime(2026, 10, 7, 17, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _worker_host(monkeypatch):
    """The worker host is a deployment setting; these tests run on worker1."""
    from doclayer.settings import settings

    monkeypatch.setattr(settings, "gap_owner_host", "worker1")


def pw_config():
    """Hosts and roles are deployment config now; the tests declare them."""
    return PrivateWriterConfig(
        hosts=["ws1", "App1", "worker1"],
        host_notes={"ws1": "The operator's local workstation (Ubuntu desktop)"},
    )


def model(tmp_path, reply, status=200, retries=0):
    cfg = CuratorConfig()
    cfg.credentials_file = str(tmp_path / "no-credentials")
    cfg.inference.retries = retries
    usage = Usage()
    seen = []
    content = reply if isinstance(reply, str) else json.dumps(reply)

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(status, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})

    client = ModelClient(cfg, usage, limits={"max_model_calls": retries + 1}, transport=httpx.MockTransport(respond))
    return client, usage, seen


def gap(tmp_path, question="How is Solaar installed and launched on the operator's current Ubuntu workstation?"):
    ledger = GapLedger(tmp_path / "gaps")
    record, _ = ledger.submit_event(
        need_class="needs_local_evidence",
        subject="solaar",
        representative_question=question,
        facets=["local-installation"],
    )
    return record


def runner_for(outputs):
    def runner(argv, **kw):
        key = argv[-1]
        code, out, err = outputs.get(key, (0, "", ""))
        return SimpleNamespace(returncode=code, stdout=out, stderr=err)

    return runner


def test_plan_runs_allowed_commands_and_builds_labelled_sources(tmp_path):
    client, usage, seen = model(
        tmp_path,
        {
            "commands": [
                {"host": "ws1", "argv": ["dpkg-query", "-W", "solaar"], "why": "installed?"},
                {"host": "ws1", "argv": ["command", "-v", "solaar"], "why": "launcher"},
                {"host": "ws1", "argv": ["solaar", "--version"], "why": "version"},
                {"host": "ws1", "argv": ["env"], "why": "nope"},
            ]
        },
    )
    runner = runner_for(
        {
            "dpkg-query -W solaar": (0, "solaar\t1.1.19-1\n", ""),
            "command -v solaar": (0, "/usr/bin/solaar\n", ""),
            "/usr/bin/solaar --version": (0, "solaar 1.1.19\n", ""),
        }
    )
    got = gather_evidence(gap(tmp_path), pw_config(), model_client=client, runner=runner, now=NOW)
    assert got.status == "ready" and usage.model_calls == 1
    prompt = json.dumps(seen[0])
    assert "The operator's local workstation" in prompt and "ws1" in prompt  # host roles reach the planner
    assert [s["label"] for s in got.sources] == [
        "live check ws1 dpkg-query #1 @2026-10-07T17:30Z",
        "live check ws1 command #2 @2026-10-07T17:30Z",
        "live check ws1 solaar #3 @2026-10-07T17:30Z",
    ]
    for source in got.sources:
        assert source["origin"] == PRIVATE_ORIGIN and source["kind"] == "text"
        assert source["source_hash"] == hashlib.sha256(source["text"].encode()).hexdigest()
    assert got.trail["refused"] == [{"host": "ws1", "argv": ["env"], "reason": "env is not an allowed command"}]
    assert "1.1.19" not in json.dumps(got.trail)


def test_prompt_states_the_tool_rules():
    for rule in (
        "${Status}",
        "java --version",
        "apt-cache policy",
        "df -h PATH",
        "rev-parse --short HEAD",
        "--no-optional-locks",
        "grep, cat, tree",
        "-maxdepth N, -name, -iname, -type f|d|l, -path and -mtime",
        "-exec, -delete, -fprint",
        "ps -eo pid,ppid,comm",
        "free -m",
        "ss -ltnu",
        "systemctl show UNIT -p PROPS",
        "withheld if it looks like a secret",
        "appears earlier in the same plan",
    ):
        assert rule in PLAN_SYSTEM


def test_more_than_max_commands_are_trimmed(tmp_path):
    cap = pw_config().max_commands
    client, _, _ = model(
        tmp_path, {"commands": [{"host": "ws1", "argv": ["hostname"], "why": str(i)} for i in range(cap + 4)]}
    )
    got = gather_evidence(
        gap(tmp_path), pw_config(), model_client=client, runner=runner_for({"hostname": (0, "ws1\n", "")}), now=NOW
    )
    assert len(got.trail["plan"]) == cap and len(got.sources) == cap


def test_malformed_plan_runs_nothing_and_is_empty(tmp_path):
    client, usage, _ = model(tmp_path, "not json at all")
    calls = []
    got = gather_evidence(
        gap(tmp_path), pw_config(), model_client=client, runner=lambda *a, **k: calls.append(a), now=NOW
    )
    assert got.status == "empty" and calls == [] and "plan" in got.reason and usage.model_calls == 1


def test_model_outage_is_an_outage_even_with_retries(tmp_path):
    client, _, _ = model(tmp_path, {"commands": []}, status=503, retries=2)
    got = gather_evidence(gap(tmp_path), pw_config(), model_client=client, runner=runner_for({}), now=NOW)
    assert got.status == "outage"


def test_unreachable_host_is_an_outage(tmp_path):
    client, _, _ = model(tmp_path, {"commands": [{"host": "ws1", "argv": ["hostname"], "why": "x"}]})
    got = gather_evidence(
        gap(tmp_path), pw_config(), model_client=client, runner=runner_for({"hostname": (255, "", "")}), now=NOW
    )
    assert got.status == "outage"


def test_all_refused_or_withheld_is_empty_with_reasons(tmp_path):
    client, _, _ = model(
        tmp_path,
        {"commands": [{"host": "ws1", "argv": ["env"], "why": "x"}, {"host": "ws1", "argv": ["hostname"], "why": "y"}]},
    )
    got = gather_evidence(
        gap(tmp_path),
        pw_config(),
        model_client=client,
        runner=runner_for({"hostname": (0, "PASSWORD=x\n", "")}),
        now=NOW,
    )
    assert got.status == "empty" and got.trail["withheld"] and got.trail["refused"]


def test_private_sources_only_returns_valid_private_writer_entries():
    text = "host: ws1\ncommand: hostname\nexit: 0\nstdout:\nws1\nstderr:\n(no output)\n"
    good = {
        "kind": "text",
        "label": "live check ws1 hostname #1 @2026-10-07T17:30Z",
        "text": text,
        "origin": PRIVATE_ORIGIN,
        "source_hash": hashlib.sha256(text.encode()).hexdigest(),
    }
    record = SimpleNamespace(
        source_candidates=[good, {**good, "origin": "attached-local-text"}, {**good, "text": text + "x"}, "junk"]
    )
    assert private_sources(record) == [good]


def test_version_check_runs_only_a_resolved_system_executable_by_path(tmp_path):
    client, _, _ = model(
        tmp_path,
        {
            "commands": [
                {"host": "ws1", "argv": ["command", "-v", "helper"], "why": "function"},
                {"host": "ws1", "argv": ["helper", "--version"], "why": "version"},
                {"host": "ws1", "argv": ["command", "-v", "tool"], "why": "home bin"},
                {"host": "ws1", "argv": ["tool", "--version"], "why": "version"},
                {"host": "ws1", "argv": ["command", "-v", "solaar"], "why": "launcher"},
                {"host": "ws1", "argv": ["solaar", "--version"], "why": "version"},
            ]
        },
    )
    ran = []
    outputs = {
        "command -v helper": (0, "helper\n", ""),
        "command -v tool": (0, "/home/user/bin/tool\n", ""),
        "command -v solaar": (0, "/usr/bin/solaar\n", ""),
        "/usr/bin/solaar --version": (0, "solaar 1.1.19\n", ""),
    }

    def runner(argv, **kw):
        ran.append(argv[-1])
        code, out, err = outputs.get(argv[-1], (0, "", ""))
        return SimpleNamespace(returncode=code, stdout=out, stderr=err)

    got = gather_evidence(gap(tmp_path), pw_config(), model_client=client, runner=runner, now=NOW)
    assert [r["argv"][0] for r in got.trail["refused"]] == ["helper", "tool"]
    assert "/usr/bin/solaar --version" in ran and not any(
        r.endswith("--version") and not r.startswith("/") for r in ran
    )
    assert "command: /usr/bin/solaar --version" in got.sources[-1]["text"]
