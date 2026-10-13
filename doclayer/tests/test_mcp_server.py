"""MCP protocol smoke tests for the doclayer server: resolve (with faked
sources), the cheap `route` tool, and the durable `record_gap` tool — over a
real stdio session."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

RUNNER = textwrap.dedent(
    """
    import json, sys
    sys.path.insert(0, %(src)r)
    from unittest import mock
    from doclayer import resolver
    from doclayer.mcp_server import build_server

    from pathlib import Path as _P
    class Repo:
        root = _P(%(repo)r)
        def markdown_paths(self): return []
        def read_page(self, path): raise AssertionError("no pages")
        def source_paths(self, subject): return []
        def has_path(self, path): return False
        def head_commit(self): return %(head)r

    async def search(question, top_k=8):
        return [], None

    async def docs(question, subject=None):
        return {"error": "fixture unavailable"}

    resolver.resolve = _original = resolver.__dict__["resolve"]

    from doclayer.brainrepo import BrainRepo
    real_brain_repo = BrainRepo

    def construct(root, *, snapshot=False):
        return Repo()

    resolver.BrainRepo = construct

    import asyncio
    real_resolve = _original

    async def resolve(question, **kwargs):
        kwargs.setdefault("repo", Repo())
        return await real_resolve(question, **kwargs)

    async def patched(question, product=None, live=None, force=None):
        return await real_resolve(question, product=product, live=live, force=force, repo=Repo())

    resolver.resolve = patched
    resolver.query_vo_rag = search
    resolver.query_context7 = docs
    resolver._safe_index_lag = lambda repo: []
    resolver.stale_observation_flags = lambda repo: []
    build_server().run(transport="stdio")
    """
)


@pytest.fixture()
def mcp_env(tmp_path: Path) -> dict[str, str]:
    runner = tmp_path / "runner.py"
    runner.write_text(
        RUNNER
        % {
            "src": str(Path(__file__).resolve().parents[1] / "src"),
            "repo": str(tmp_path),
            "head": "a" * 40,
        }
    )
    env = dict(os.environ)
    env["DOC_LAYER_NEGATIVE_CACHE"] = str(tmp_path / "nc.json")
    env["DOC_LAYER_GAPS_DIR"] = str(tmp_path / "gaps")
    env["DOC_LAYER_CATALOG_CACHE_DIR"] = str(tmp_path / "catalog")
    env["DOC_LAYER_GAP_CAPTURE"] = "1"
    env["DOC_LAYER_BRAIN_REPO"] = str(tmp_path)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["_DOCLAYER_TEST_RUNNER"] = str(runner)
    return env


class McpSession:
    """A real stdio MCP client session (newline-delimited JSON-RPC)."""

    def __init__(self, env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(  # noqa: S603
            [sys.executable, env["_DOCLAYER_TEST_RUNNER"]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        self._id = 0

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        message = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            message["params"] = params
        self.proc.stdin.write(json.dumps(message) + "\n")  # type: ignore[union-attr]
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()  # type: ignore[union-attr]
        assert line.strip(), "MCP server closed the session: " + self.proc.stderr.read()  # type: ignore[union-attr]
        return json.loads(line)

    def initialize(self) -> dict:
        reply = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "doclayer-test", "version": "0"},
            },
        )
        self.proc.stdin.write(  # type: ignore[union-attr]
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        self.proc.stdin.flush()
        return reply

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        try:
            self.proc.stdin.close()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        self.proc.wait(timeout=10)


def test_doclayer_mcp_tools_are_small_and_useful(mcp_env, tmp_path) -> None:
    session = McpSession(mcp_env)
    try:
        init = session.initialize()
        assert init["result"]["serverInfo"]["name"] == "doclayer"
        tools = session.request("tools/list")
        names = {t["name"] for t in tools["result"]["tools"]}
        assert {"resolve", "route", "record_gap", "attach_source"} <= names

        result = session.call_tool("resolve", {"question": "What is the frobnicator bandwidth?"})
        text = result["result"]["content"][0]["text"]
        assert "Dispositions" in text

        result = session.call_tool("route", {"question": "What is running on worker1?"})
        text = result["result"]["content"][0]["text"]
        assert "intent:" in text and "coverage:" in text

        result = session.call_tool(
            "record_gap",
            {
                "question": "we answered from external docs for htmx",
                "subject": None,
                "need": "enrichment",
                "detail": "answered externally; page should exist",
            },
        )
        text = result["result"]["content"][0]["text"]
        assert "recorded" in text or "updated" in text
        records = list((tmp_path / "gaps" / "gaps").glob("*/record.json"))
        # resolve captured the frobnicator miss (the fixture's public-docs
        # outage makes it infrastructure) and record_gap recorded the
        # enrichment need: distinct durable gaps with their own classes.
        need_classes = {json.loads(r.read_text())["need_class"] for r in records}
        assert "enrichment" in need_classes
    finally:
        session.close()
