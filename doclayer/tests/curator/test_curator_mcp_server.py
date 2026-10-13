"""MCP protocol smoke test: a real stdio session against brain-curator-mcp —
initialize, list tools, call one over JSON-RPC (newline-delimited)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def mcp_env(tmp_path: Path, checkout: Path) -> dict[str, str]:
    import yaml

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    config_path = cfg_dir / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "paths": {
                    "brain_checkout": str(checkout),
                    "jobs": str(tmp_path / "jobs"),
                    "worktrees": str(tmp_path / "worktrees"),
                    "cache": str(tmp_path / "cache"),
                },
                "inference": {"url": "http://inference.test/v1"},
            }
        )
    )
    env = dict(os.environ)
    env["BRAIN_CURATOR_CONFIG"] = str(config_path)
    env["BRAIN_CURATOR_LOCAL"] = "1"  # the documented worker-host mode
    return env


class McpSession:
    """A real stdio MCP client session (newline-delimited JSON-RPC)."""

    def __init__(self, env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "doclayer.curator.mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=Path(__file__).resolve().parents[2],
        )
        self._id = 0

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        message = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            message["params"] = params
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        assert line.strip(), "MCP server closed the session"
        return json.loads(line)

    def initialize(self) -> dict:
        reply = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "curator-test", "version": "0"},
            },
        )
        # the client must answer the initialized notification
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.proc.stdin.flush()
        return reply

    def call_tool(self, name: str, arguments: dict) -> dict:
        reply = self.request("tools/call", {"name": name, "arguments": arguments})
        return reply

    def close(self) -> None:
        try:
            self.proc.stdin.close()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        self.proc.wait(timeout=10)


def test_mcp_server_lists_and_calls_curator_tools(mcp_env) -> None:
    session = McpSession(mcp_env)
    try:
        init = session.initialize()
        assert init["result"]["serverInfo"]["name"] == "brain-curator"

        tools = session.request("tools/list")
        names = {t["name"] for t in tools["result"]["tools"]}
        assert {
            "submit_import",
            "job_status",
            "job_result",
            "job_resume",
            "job_cancel",
            "job_correct",
            "job_inspect",
        } <= names

        # a real tool call end to end: submit through the MCP protocol
        call = session.call_tool(
            "submit_import",
            {
                "intent": "mcp protocol smoke test",
                "text": "MCP-submitted material. " * 30,
                "text_label": "mcp-note",
            },
        )
        content = call["result"]["content"]
        payload = json.loads(content[0]["text"])
        assert payload["ok"] is True and payload["created"] is True
        job_id = payload["job_id"]

        status = session.call_tool("job_status", {"job_id": job_id})
        status_payload = json.loads(status["result"]["content"][0]["text"])
        assert status_payload["ok"] and status_payload["job"]["status"] == "queued"

        result = session.call_tool("job_result", {"job_id": job_id})
        result_payload = json.loads(result["result"]["content"][0]["text"])
        assert result_payload["ok"] and "digest" in result_payload
    finally:
        session.close()


def test_mcp_tool_error_is_structured_not_a_crash(mcp_env) -> None:
    session = McpSession(mcp_env)
    try:
        session.initialize()
        call = session.call_tool("job_status", {"job_id": "job-missing00"})
        payload = json.loads(call["result"]["content"][0]["text"])
        assert payload["ok"] is False
        assert "unknown job" in payload["error"]
    finally:
        session.close()


def test_mcp_continuation_surface(mcp_env):
    session = McpSession(mcp_env)
    try:
        session.initialize()
        tools = session.request("tools/list")["result"]["tools"]
        continuation = next(tool for tool in tools if tool["name"] == "job_continue")
        assert {"job_id", "source_ids", "review_omitted", "discover", "source_policy"} <= set(
            continuation["inputSchema"]["properties"]
        )
        result = session.call_tool("job_continue", {"job_id": "job-1234567890", "discover": False})
        payload = json.loads(result["result"]["content"][0]["text"])
        assert not payload["ok"] and "unknown job" in payload["error"]
    finally:
        session.close()


@pytest.mark.asyncio
async def test_fresh_native_sdk_continuation_url_schema_and_forwarding(mcp_env):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from doclayer.curator.config import load_config
    from doclayer.curator.jobstore import JobStore
    from doclayer.curator.state import JobState

    cfg = load_config(mcp_env["BRAIN_CURATOR_CONFIG"])
    store = JobStore(cfg.jobs_dir())
    parent = store.create(JobState("", "mcp-url-parent", "Batch execution", status="partial"))[0]
    original = store.state_path(parent.job_id).read_bytes()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "doclayer.curator.mcp_server"],
        env=mcp_env,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            continuation = next(tool for tool in tools.tools if tool.name == "job_continue")
            assert "urls" in continuation.inputSchema["properties"]
            assert "urls" not in continuation.inputSchema.get("required", [])
            result = await session.call_tool(
                "job_continue", {"job_id": parent.job_id, "urls": ["https://docs.test/batch#reference"]}
            )
            response = json.loads(result.content[0].text)
            assert response["ok"] and response["created"]
            child = store.load(response["job_id"])
            assert child.continuation_of == parent.job_id and child.stage == "intake"
            assert child.sources[0].url == "https://docs.test/batch"
            assert child.sources[0].submitted and child.sources[0].role == "primary"
            invalid = await session.call_tool(
                "job_continue", {"job_id": parent.job_id, "urls": ["https://docs.test/batch"], "discover": False}
            )
            error = json.loads(invalid.content[0].text)
            assert not error["ok"] and "require discovery" in error["error"]
            replay = await session.call_tool(
                "job_continue", {"job_id": parent.job_id, "urls": ["https://docs.test/batch"]}
            )
            repeated = json.loads(replay.content[0].text)
            assert not repeated["created"] and repeated["job_id"] == child.job_id
            assert store.state_path(parent.job_id).read_bytes() == original
