"""MCP server exposing a running Herdr terminal session.

Thin stdio bridge: every tool shells out to the ``herdr`` CLI, which talks to the
Herdr server over its unix socket and answers with JSON envelopes of the form
``{"id": "cli:pane:list", "result": {...}}``.

stdout is the MCP wire when running over stdio, so this module never prints.
Diagnostics go to ``logging`` (stderr).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

logger = logging.getLogger("herdr_mcp")

mcp = MCPServer("herdr")

#: Snapshot sources accepted by ``herdr pane read``.
READ_SOURCES = ("visible", "recent", "recent-unwrapped", "detection")
#: Snapshot sources accepted by ``herdr pane wait-output`` (no detection buffer).
WAIT_SOURCES = ("visible", "recent", "recent-unwrapped")
#: Agent states accepted by ``--until``.
AGENT_STATES = ("idle", "working", "blocked", "done", "unknown")
#: Split directions accepted by ``herdr pane split``.
SPLIT_DIRECTIONS = ("right", "down")

DEFAULT_TIMEOUT = 30.0
WAIT_MARGIN_SECONDS = 10.0


def _herdr_bin() -> str:
    return os.environ.get("HERDR_BIN", "herdr")


def _wait_timeout(timeout_ms: int) -> float:
    """Subprocess timeout for a tool that hands ``timeout_ms`` to herdr itself."""
    return max(timeout_ms, 0) / 1000.0 + WAIT_MARGIN_SECONDS


def _error_text(stdout: str, stderr: str) -> str:
    """Best-effort human/model readable failure text from a herdr invocation."""
    for stream in (stderr, stdout):
        blob = stream.strip()
        if not blob:
            continue
        try:
            payload = json.loads(blob)
        except json.JSONDecodeError:
            return blob[-2000:]
        if isinstance(payload, dict) and "error" in payload:
            return json.dumps(payload["error"])
        return blob[-2000:]
    return "herdr command failed with no output"


def _run(args: list[str], timeout: float) -> str:
    """Run the herdr CLI and return stdout, raising ToolError on any failure."""
    argv = [_herdr_bin(), *args]
    logger.debug("herdr argv: %s", argv)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ToolError(
            f"herdr binary not found ({argv[0]}); set HERDR_BIN or install herdr"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(
            f"herdr {' '.join(args)} timed out after {timeout:.0f}s"
        ) from exc
    if proc.returncode != 0:
        raise ToolError(_error_text(proc.stdout, proc.stderr))
    return proc.stdout


def _herdr(*args: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Run herdr and return the parsed ``result`` object of its JSON envelope."""
    stdout = _run(list(args), timeout)
    blob = stdout.strip()
    if not blob:
        return {"ok": True}
    try:
        envelope = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise ToolError(
            f"herdr {' '.join(args)} returned unparseable output: {blob[-2000:]}"
        ) from exc
    if isinstance(envelope, dict) and envelope.get("error") is not None:
        raise ToolError(json.dumps(envelope["error"]))
    if not isinstance(envelope, dict):
        return {"result": envelope}
    result = envelope.get("result", envelope)
    if not isinstance(result, dict):
        return {"result": result}
    return result


def _herdr_text(*args: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Run herdr and return raw stdout (used by text-mode pane reads)."""
    return _run(list(args), timeout)


def _one_of(name: str, value: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise ToolError(f"{name} must be one of {', '.join(allowed)}; got {value!r}")
    return value


# --------------------------------------------------------------------------
# Inspection
# --------------------------------------------------------------------------


@mcp.tool()
def list_workspaces() -> dict[str, Any]:
    """List Herdr workspaces with their ids, labels, and aggregate agent status.

    Workspace ids look like `w17`. Start here to orient in the session.
    """
    return _herdr("workspace", "list")


@mcp.tool()
def list_tabs() -> dict[str, Any]:
    """List tabs across the session. Tab ids look like `w17:t1`."""
    return _herdr("tab", "list")


@mcp.tool()
def list_panes() -> dict[str, Any]:
    """List every pane with its id, workspace, tab, cwd, title, and agent status.

    Pane ids look like `w17:p1` and are the target of every pane tool.
    """
    return _herdr("pane", "list")


@mcp.tool()
def list_agents() -> dict[str, Any]:
    """List panes running a detected coding agent, with agent kind and status.

    Status is one of idle, working, blocked, done, unknown. Agents are addressed
    by pane id (`w17:p1`) or by their agent name.
    """
    return _herdr("agent", "list")


@mcp.tool()
def get_agent(target: str) -> dict[str, Any]:
    """Show one agent's current state.

    target: a pane id like `w17:p1` or an agent name; get them from list_agents.
    """
    return _herdr("agent", "get", target)


@mcp.tool()
def read_pane(pane: str, lines: int | None = None, source: str = "recent") -> str:
    """Read a pane's terminal output as plain text.

    pane: pane id like `w17:p1` (see list_panes / list_agents).
    lines: keep only the last N lines of the snapshot.
    source: visible (what the user sees), recent (default scrollback tail),
    recent-unwrapped (recent without soft wrapping), detection (bottom buffer
    used for agent detection).
    Note: herdr may return an empty string for source=recent with lines set on
    panes with short scrollback; if that happens, retry without lines or with
    source="visible".
    """
    _one_of("source", source, READ_SOURCES)
    args = ["pane", "read", pane, "--format", "text", "--source", source]
    if lines is not None:
        if lines <= 0:
            raise ToolError("lines must be a positive integer")
        args += ["--lines", str(lines)]
    return _herdr_text(*args)


# --------------------------------------------------------------------------
# Control
# --------------------------------------------------------------------------


@mcp.tool()
def prompt_agent(
    target: str,
    text: str,
    wait: bool = False,
    timeout_ms: int = 120000,
) -> dict[str, Any]:
    """Submit a prompt to an agent pane (types the text and submits it).

    target: pane id like `w17:p1` or an agent name; see list_agents.
    wait: also wait for the agent to settle (idle, done, or blocked) afterwards.
    timeout_ms: only used with wait=True; the call fails if nothing matches in time.
    """
    args = ["agent", "prompt", target, text]
    timeout = DEFAULT_TIMEOUT
    if wait:
        args += ["--wait", "--timeout", str(timeout_ms)]
        timeout = _wait_timeout(timeout_ms)
    return _herdr(*args, timeout=timeout)


@mcp.tool()
def wait_agent(
    target: str,
    until: list[str] | None = None,
    timeout_ms: int = 60000,
) -> dict[str, Any]:
    """Wait until an agent reaches one of the requested states.

    target: pane id like `w17:p1` or an agent name.
    until: any of idle, working, blocked, done, unknown. Default: idle, done, blocked.
    timeout_ms: the call fails if no requested state is observed in time.
    """
    args = ["agent", "wait", target]
    for state in until or []:
        args += ["--until", _one_of("until", state, AGENT_STATES)]
    args += ["--timeout", str(timeout_ms)]
    return _herdr(*args, timeout=_wait_timeout(timeout_ms))


@mcp.tool()
def send_text(pane: str, text: str) -> dict[str, Any]:
    """Send literal text to a pane without pressing Enter.

    pane: pane id like `w17:p1`. Use run_command to send a command and Enter.
    """
    return _herdr("pane", "send-text", pane, text)


@mcp.tool()
def send_keys(pane: str, keys: list[str]) -> dict[str, Any]:
    """Send key presses to a pane, e.g. ["enter"], ["esc"], ["ctrl-c"].

    pane: pane id like `w17:p1`. Keys are sent in order; use `esc` for Escape.
    """
    if not keys:
        raise ToolError("keys must contain at least one key name")
    return _herdr("pane", "send-keys", pane, *keys)


@mcp.tool()
def run_command(pane: str, command: list[str]) -> dict[str, Any]:
    """Run a shell command in a pane (sends the command text and Enter).

    pane: pane id like `w17:p1`.
    command: argv-style words, e.g. ["git", "status"]. This does not wait for the
    command to finish; use wait_for_output or read_pane afterwards.
    """
    if not command:
        raise ToolError("command must contain at least one word")
    return _herdr("pane", "run", pane, *command)


@mcp.tool()
def wait_for_output(
    pane: str,
    pattern: str,
    regex: bool = False,
    timeout_ms: int = 30000,
    source: str = "recent",
) -> dict[str, Any]:
    """Wait until a pane's output matches a pattern.

    pane: pane id like `w17:p1`.
    pattern: literal substring, or a Rust regular expression when regex=True.
    The existing snapshot is searched first, then polled until timeout_ms.
    source: visible, recent (default), or recent-unwrapped.
    """
    _one_of("source", source, WAIT_SOURCES)
    flag = "--regex" if regex else "--match"
    args = [
        "pane",
        "wait-output",
        pane,
        flag,
        pattern,
        "--source",
        source,
        "--timeout",
        str(timeout_ms),
    ]
    return _herdr(*args, timeout=_wait_timeout(timeout_ms))


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------


@mcp.tool()
def split_pane(
    pane: str,
    direction: str = "right",
    ratio: float | None = None,
) -> dict[str, Any]:
    """Split a pane and return the new pane's id.

    pane: pane id like `w17:p1` to split.
    direction: right or down.
    ratio: fraction of the split given to the new pane, between 0 and 1.
    """
    _one_of("direction", direction, SPLIT_DIRECTIONS)
    args = ["pane", "split", pane, "--direction", direction]
    if ratio is not None:
        if not 0.0 < ratio < 1.0:
            raise ToolError("ratio must be between 0 and 1 (exclusive)")
        args += ["--ratio", str(ratio)]
    return _herdr(*args)


@mcp.tool()
def close_pane(pane: str) -> dict[str, Any]:
    """Close a pane and any process running in it. pane: pane id like `w17:p1`."""
    return _herdr("pane", "close", pane)


def main() -> None:
    """Entry point: serve MCP over stdio."""
    logging.basicConfig(level=os.environ.get("HERDR_MCP_LOG_LEVEL", "WARNING"))
    mcp.run()


if __name__ == "__main__":
    main()
