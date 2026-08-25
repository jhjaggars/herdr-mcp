"""In-memory MCP client tests against a stub herdr binary.

These tests never touch a live Herdr session: HERDR_BIN points at a throwaway
script that echoes canned JSON envelopes and records the argv it received.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from mcp import Client
from herdr_mcp.server import mcp

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _write_stub(path: Path, body: str) -> Path:
    script = path / "herdr-stub"
    script.write_text("#!/usr/bin/env python3\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


@pytest.fixture
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Stub herdr that records argv and prints a per-test canned stdout."""
    argv_log = tmp_path / "argv.jsonl"
    payload_file = tmp_path / "payload.json"

    body = f"""
import json, sys
argv_log = {str(argv_log)!r}
payload_file = {str(payload_file)!r}
with open(argv_log, "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
spec = json.load(open(payload_file))
sys.stdout.write(spec.get("stdout", ""))
sys.stderr.write(spec.get("stderr", ""))
sys.exit(spec.get("code", 0))
"""
    script = _write_stub(tmp_path, body)
    monkeypatch.setenv("HERDR_BIN", str(script))

    class Stub:
        def respond(self, stdout: str = "", stderr: str = "", code: int = 0) -> None:
            payload_file.write_text(
                json.dumps({"stdout": stdout, "stderr": stderr, "code": code})
            )

        def envelope(self, ident: str, result: object) -> None:
            self.respond(json.dumps({"id": ident, "result": result}))

        def calls(self) -> list[list[str]]:
            if not argv_log.exists():
                return []
            return [json.loads(line) for line in argv_log.read_text().splitlines()]

        def last_call(self) -> list[str]:
            return self.calls()[-1]

    s = Stub()
    s.respond(json.dumps({"id": "cli:stub", "result": {}}))
    return s


EXPECTED_TOOLS = {
    "list_workspaces",
    "list_tabs",
    "list_panes",
    "list_agents",
    "get_agent",
    "read_pane",
    "prompt_agent",
    "wait_agent",
    "send_text",
    "send_keys",
    "run_command",
    "wait_for_output",
    "split_pane",
    "close_pane",
}


async def test_lists_all_tools(stub) -> None:
    async with Client(mcp) as client:
        listed = await client.list_tools()
    names = {tool.name for tool in listed.tools}
    assert names == EXPECTED_TOOLS
    assert len(names) == 14


async def test_list_panes_returns_structured_result(stub) -> None:
    panes = {"panes": [{"pane_id": "w17:p1", "workspace_id": "w17"}]}
    stub.envelope("cli:pane:list", panes)
    async with Client(mcp) as client:
        result = await client.call_tool("list_panes", {})
    assert result.is_error is False
    assert result.structured_content == panes
    assert stub.last_call() == ["pane", "list"]


async def test_read_pane_returns_plain_text(stub) -> None:
    stub.respond(stdout="hello from the pane\n")
    async with Client(mcp) as client:
        result = await client.call_tool(
            "read_pane", {"pane": "w17:p1", "lines": 5, "source": "visible"}
        )
    assert result.is_error is False
    assert "hello from the pane" in result.content[0].text
    assert stub.last_call() == [
        "pane",
        "read",
        "w17:p1",
        "--format",
        "text",
        "--source",
        "visible",
        "--lines",
        "5",
    ]


async def test_cli_failure_surfaces_stderr(stub) -> None:
    stub.respond(stderr="herdr: socket not found\n", code=2)
    async with Client(mcp) as client:
        result = await client.call_tool("list_agents", {})
    assert result.is_error is True
    assert "socket not found" in result.content[0].text


async def test_envelope_error_is_tool_error(stub) -> None:
    stub.respond(
        stderr=json.dumps(
            {
                "id": "cli:pane:read",
                "error": {"code": "pane_not_found", "message": "pane w99:p99 not found"},
            }
        ),
        code=1,
    )
    async with Client(mcp) as client:
        result = await client.call_tool("read_pane", {"pane": "w99:p99"})
    assert result.is_error is True
    assert "pane_not_found" in result.content[0].text


async def test_result_error_key_without_exit_code(stub) -> None:
    stub.respond(
        stdout=json.dumps({"id": "cli:agent:get", "error": {"code": "agent_not_found"}})
    )
    async with Client(mcp) as client:
        result = await client.call_tool("get_agent", {"target": "nope"})
    assert result.is_error is True
    assert "agent_not_found" in result.content[0].text


async def test_send_keys_and_run_command_pass_argv_through(stub) -> None:
    stub.envelope("cli:pane:send-keys", {"ok": True})
    async with Client(mcp) as client:
        await client.call_tool("send_keys", {"pane": "w17:p1", "keys": ["ctrl-c", "esc"]})
        assert stub.last_call() == ["pane", "send-keys", "w17:p1", "ctrl-c", "esc"]

        await client.call_tool(
            "run_command", {"pane": "w17:p2", "command": ["git", "status", "-s"]}
        )
        assert stub.last_call() == ["pane", "run", "w17:p2", "git", "status", "-s"]


async def test_wait_tools_pass_timeout_and_until(stub) -> None:
    stub.envelope("cli:agent:wait", {"agent_status": "idle"})
    async with Client(mcp) as client:
        result = await client.call_tool(
            "wait_agent",
            {"target": "w17:p1", "until": ["idle", "done"], "timeout_ms": 1500},
        )
    assert result.is_error is False
    assert stub.last_call() == [
        "agent",
        "wait",
        "w17:p1",
        "--until",
        "idle",
        "--until",
        "done",
        "--timeout",
        "1500",
    ]


async def test_wait_for_output_uses_match_or_regex(stub) -> None:
    stub.envelope("cli:pane:wait-output", {"matched": True})
    async with Client(mcp) as client:
        await client.call_tool(
            "wait_for_output", {"pane": "w17:p1", "pattern": "done", "timeout_ms": 1000}
        )
        assert stub.last_call()[:5] == ["pane", "wait-output", "w17:p1", "--match", "done"]

        await client.call_tool(
            "wait_for_output",
            {"pane": "w17:p1", "pattern": "^done$", "regex": True, "timeout_ms": 1000},
        )
        assert stub.last_call()[:5] == [
            "pane",
            "wait-output",
            "w17:p1",
            "--regex",
            "^done$",
        ]


async def test_prompt_agent_only_waits_when_asked(stub) -> None:
    stub.envelope("cli:agent:prompt", {"submitted": True})
    async with Client(mcp) as client:
        await client.call_tool("prompt_agent", {"target": "w17:p1", "text": "hi"})
        assert stub.last_call() == ["agent", "prompt", "w17:p1", "hi"]

        await client.call_tool(
            "prompt_agent",
            {"target": "w17:p1", "text": "hi", "wait": True, "timeout_ms": 2000},
        )
        assert stub.last_call() == [
            "agent",
            "prompt",
            "w17:p1",
            "hi",
            "--wait",
            "--timeout",
            "2000",
        ]


async def test_split_pane_validates_direction(stub) -> None:
    stub.envelope("cli:pane:split", {"pane_id": "w17:p9"})
    async with Client(mcp) as client:
        ok = await client.call_tool(
            "split_pane", {"pane": "w17:p1", "direction": "down", "ratio": 0.3}
        )
        assert ok.is_error is False
        assert stub.last_call() == [
            "pane",
            "split",
            "w17:p1",
            "--direction",
            "down",
            "--ratio",
            "0.3",
        ]

        bad = await client.call_tool("split_pane", {"pane": "w17:p1", "direction": "up"})
    assert bad.is_error is True
    assert "direction must be one of" in bad.content[0].text


async def test_missing_binary_is_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERDR_BIN", "/nonexistent/herdr-binary")
    async with Client(mcp) as client:
        result = await client.call_tool("list_workspaces", {})
    assert result.is_error is True
    assert "herdr binary not found" in result.content[0].text


def test_no_prints_in_server_module() -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "herdr_mcp" / "server.py"
    assert "print(" not in source.read_text()


def test_stub_env_isolation(stub) -> None:
    assert os.environ["HERDR_BIN"].endswith("herdr-stub")
