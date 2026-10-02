"""In-memory MCP client tests against a stub herdr binary.

These tests never touch a live Herdr session: HERDR_BIN points at a throwaway
script that answers the four ``list`` commands from a mutable JSON state file and
records every other argv it receives. Tools are registered at import time (per
HERDR_MCP_TOOLS), so each test builds its own server module via ``server``.
"""

from __future__ import annotations

import importlib
import json
import stat
from pathlib import Path

import pytest

from mcp import Client

pytestmark = pytest.mark.anyio

WORKSPACES = [
    {"workspace_id": "wA", "label": "openclaw"},
    {"workspace_id": "wB", "label": "private"},
    {"workspace_id": "wC", "label": "openclaw"},  # duplicate label, also in scope
]
PANES = [
    {"pane_id": "wA:p1", "workspace_id": "wA", "tab_id": "wA:t1"},
    {"pane_id": "wB:p1", "workspace_id": "wB", "tab_id": "wB:t1"},
    {"pane_id": "wC:p1", "workspace_id": "wC", "tab_id": "wC:t1"},
]
TABS = [{"tab_id": p["tab_id"], "workspace_id": p["workspace_id"]} for p in PANES]
AGENTS = [dict(p, agent="claude") for p in PANES]

STUB = """#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
state = json.load(open({state!r}))
key = " ".join(args)
if key in state:
    sys.stdout.write(json.dumps({{"id": "cli:stub", "result": state[key]}}))
    sys.exit(0)
with open({log!r}, "a") as fh:
    fh.write(json.dumps(args) + "\\n")
sys.stdout.write(json.dumps({{"id": "cli:stub", "result": {{"ok": True}}}}))
"""


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = tmp_path / "state.json"
    log = tmp_path / "argv.jsonl"
    script = tmp_path / "herdr-stub"
    script.write_text(STUB.format(state=str(state), log=str(log)))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("HERDR_BIN", str(script))

    class Stub:
        def set(self, **lists: list[dict]) -> None:
            data = {
                "workspace list": {"workspaces": WORKSPACES},
                "pane list": {"panes": PANES},
                "tab list": {"tabs": TABS},
                "agent list": {"agents": AGENTS},
            }
            data.update({f"{k} list": {f"{k}s": v} for k, v in lists.items()})
            state.write_text(json.dumps(data))

        def calls(self) -> list[list[str]]:
            if not log.exists():
                return []
            return [json.loads(line) for line in log.read_text().splitlines()]

    s = Stub()
    s.set()
    return s


@pytest.fixture
def server(stub, monkeypatch: pytest.MonkeyPatch):
    def build(workspaces: str | None = "openclaw", tools: str | None = None):
        for name, value in (
            ("HERDR_MCP_WORKSPACES", workspaces),
            ("HERDR_MCP_TOOLS", tools),
        ):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        import herdr_mcp.server as module

        return importlib.reload(module)

    return build


async def test_default_exposes_only_read_tools(server) -> None:
    async with Client(server().mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == {
        "list_workspaces",
        "list_tabs",
        "list_panes",
        "list_agents",
        "get_agent",
        "read_pane",
        "wait_agent",
        "wait_for_output",
    }


async def test_all_groups_expose_all_tools(server) -> None:
    mod = server(tools="read,prompt,input,layout")
    async with Client(mod.mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert len(names) == 14


def test_unknown_group_is_rejected(server) -> None:
    with pytest.raises(ValueError, match="unknown group"):
        server(tools="read,root")


def test_main_refuses_to_start_without_scope(server) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    mod = server(workspaces=None)
    with pytest.raises(ToolError, match="HERDR_MCP_WORKSPACES"):
        mod.main()


async def test_unset_scope_fails_closed_on_calls(server) -> None:
    async with Client(server(workspaces=None).mcp) as client:
        result = await client.call_tool("list_panes", {})
    assert result.is_error is True
    assert "HERDR_MCP_WORKSPACES" in result.content[0].text


async def test_listings_are_filtered_by_label(server) -> None:
    async with Client(server().mcp) as client:
        panes = (await client.call_tool("list_panes", {})).structured_content
        workspaces = (await client.call_tool("list_workspaces", {})).structured_content
        tabs = (await client.call_tool("list_tabs", {})).structured_content
        agents = (await client.call_tool("list_agents", {})).structured_content
    assert [p["pane_id"] for p in panes["panes"]] == ["wA:p1", "wC:p1"]
    assert [w["workspace_id"] for w in workspaces["workspaces"]] == ["wA", "wC"]
    assert [t["tab_id"] for t in tabs["tabs"]] == ["wA:t1", "wC:t1"]
    assert [a["pane_id"] for a in agents["agents"]] == ["wA:p1", "wC:p1"]


async def test_scope_matches_workspace_id_too(server) -> None:
    async with Client(server(workspaces="wB").mcp) as client:
        panes = (await client.call_tool("list_panes", {})).structured_content
    assert [p["pane_id"] for p in panes["panes"]] == ["wB:p1"]


@pytest.mark.parametrize(
    "tool,args",
    [
        ("read_pane", {"pane": "wB:p1"}),
        ("get_agent", {"target": "wB:p1"}),
        ("wait_agent", {"target": "wB:p1"}),
        ("wait_for_output", {"pane": "wB:p1", "pattern": "x"}),
        ("prompt_agent", {"target": "wB:p1", "text": "hi"}),
        ("send_text", {"pane": "wB:p1", "text": "hi"}),
        ("send_keys", {"pane": "wB:p1", "keys": ["enter"]}),
        ("run_command", {"pane": "wB:p1", "command": ["ls"]}),
        ("split_pane", {"pane": "wB:p1"}),
        ("close_pane", {"pane": "wB:p1"}),
    ],
)
async def test_out_of_scope_pane_is_rejected_without_reaching_herdr(
    server, stub, tool: str, args: dict
) -> None:
    mod = server(tools="read,prompt,input,layout")
    async with Client(mod.mcp) as client:
        result = await client.call_tool(tool, args)
    assert result.is_error is True
    assert "not in an allowed workspace" in result.content[0].text
    assert stub.calls() == []


@pytest.mark.parametrize("alias", ["claude", "term_1", "wA", "wA:p1 ", "wA:p2", ""])
async def test_aliases_and_near_misses_are_rejected(server, stub, alias: str) -> None:
    async with Client(server().mcp) as client:
        result = await client.call_tool("read_pane", {"pane": alias})
    assert result.is_error is True
    assert stub.calls() == []


async def test_in_scope_calls_pass_through(server, stub) -> None:
    mod = server(tools="read,prompt,input,layout")
    async with Client(mod.mcp) as client:
        await client.call_tool("read_pane", {"pane": "wC:p1", "lines": 5})
        await client.call_tool("prompt_agent", {"target": "wA:p1", "text": "hi"})
        await client.call_tool("send_keys", {"pane": "wA:p1", "keys": ["ctrl-c"]})
        await client.call_tool(
            "run_command", {"pane": "wA:p1", "command": ["git", "status"]}
        )
    assert stub.calls() == [
        ["pane", "read", "wC:p1", "--format", "text", "--source", "recent", "--lines", "5"],
        ["agent", "prompt", "wA:p1", "hi"],
        ["pane", "send-keys", "wA:p1", "ctrl-c"],
        ["pane", "run", "wA:p1", "git", "status"],
    ]


async def test_scope_is_rechecked_per_call(server, stub) -> None:
    """A pane moved out of an allowed workspace loses access immediately."""
    async with Client(server().mcp) as client:
        assert (await client.call_tool("read_pane", {"pane": "wA:p1"})).is_error is False
        stub.set(pane=[dict(PANES[0], workspace_id="wB")])
        result = await client.call_tool("read_pane", {"pane": "wA:p1"})
    assert result.is_error is True


def test_no_prints_in_server_module() -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "herdr_mcp" / "server.py"
    assert "print(" not in source.read_text()
