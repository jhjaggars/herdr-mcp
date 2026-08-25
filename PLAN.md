# herdr-mcp — Implementation Plan

MCP server that exposes control/inspection of a running Herdr session.
Design: a thin **stdio** MCP server that shells out to the `herdr` CLI, which
already returns JSON envelopes over Herdr's socket API. No auth, wide open,
as simple as possible. Local stdio only — no HTTP transport.

## Research: best practices applied (official MCP Python SDK v2 docs)

1. Use the official `mcp` Python SDK v2 (`mcp>=2,<3`): `from mcp.server import MCPServer`,
   `@mcp.tool()` decorators. Type hints are the argument contract; docstrings are the
   model-facing descriptions. No hand-written JSON schemas.
2. Transport: `mcp.run()` with default **stdio** under `if __name__ == "__main__":`.
   stdout is the wire — never print(); use `logging` (goes to stderr).
3. Small, task-oriented tool set (~14 tools), not a 1:1 mirror of every CLI flag.
4. Errors: raise `ToolError` (from `mcp.server.mcpserver.exceptions`) with the herdr
   CLI's stderr/JSON error text so the model can self-correct. Unexpected crashes
   stay crashes.
5. Return structured output: pass through the parsed `result` object from the herdr
   JSON envelope (`{"id": ..., "result": {...}}`) as the tool return value (dict).
6. Tests: SDK's in-memory `Client` transport (`async with Client(mcp)`), pytest +
   anyio. No subprocess, no ports. Fake `herdr` binary via `HERDR_BIN` env pointing
   at a stub script for unit tests; live smoke test against a real session separately.

## Repo layout

```
herdr-mcp/
  pyproject.toml           # uv project; dep: mcp>=2,<3; script: herdr-mcp
  README.md                # what/why, client config snippets, tool table
  PLAN.md                  # this file
  .gitignore               # .venv, __pycache__, dist, uv.lock kept
  src/herdr_mcp/__init__.py
  src/herdr_mcp/server.py  # everything: helper + tools + main()  (~250 lines)
  tests/test_server.py     # in-memory client tests against stub herdr
```

## Core helper

```python
def _herdr(*args, timeout=30.0) -> dict:
    # subprocess.run([HERDR_BIN, *args], capture_output=True, text=True, timeout=...)
    # HERDR_BIN = os.environ.get("HERDR_BIN", "herdr"); env passed through untouched
    # nonzero exit or unparseable stdout -> ToolError(stderr or stdout tail)
    # parse JSON envelope; if "error" in envelope -> ToolError(str(error))
    # return envelope.get("result", envelope)
```

Sync tools are fine (SDK runs them off the event loop). Wait-style tools pass their
own `--timeout` to herdr and use subprocess timeout = (ms/1000 + 10s margin);
indefinite waits are not allowed — default timeout_ms 60000.

## Tools (14)

All `pane`/`target` params accept herdr ids like `w17:p1` (agents also accept names).
Every tool returns the parsed `result` dict from the herdr JSON envelope unless noted.

Inspection:
1. `list_workspaces()` -> `herdr workspace list`
2. `list_tabs()` -> `herdr tab list`
3. `list_panes()` -> `herdr pane list`
4. `list_agents()` -> `herdr agent list`
5. `get_agent(target: str)` -> `herdr agent get <target>`
6. `read_pane(pane: str, lines: int | None = None, source: str = "recent")`
   -> `herdr pane read <pane> --format text [--lines N] [--source S]`
   source in {visible, recent, recent-unwrapped, detection}. Returns text (str).

Control:
7. `prompt_agent(target: str, text: str, wait: bool = False, timeout_ms: int = 120000)`
   -> `herdr agent prompt <target> <text> [--wait --timeout <ms>]`
8. `wait_agent(target: str, until: list[str] | None = None, timeout_ms: int = 60000)`
   -> `herdr agent wait <target> [--until s]... --timeout <ms>`
   until values: idle, working, blocked, done, unknown (default: idle/done/blocked).
9. `send_text(pane: str, text: str)` -> `herdr pane send-text <pane> <text>`
10. `send_keys(pane: str, keys: list[str])` -> `herdr pane send-keys <pane> <keys...>`
11. `run_command(pane: str, command: list[str])` -> `herdr pane run <pane> <command...>`
12. `wait_for_output(pane: str, pattern: str, timeout_ms: int = 30000)`
    -> `herdr pane wait-output` (check exact flag names with `--help` before coding)

Layout:
13. `split_pane(pane: str, direction: str = "right", ratio: float | None = None)`
    -> `herdr pane split <pane> --direction <d> [--ratio r]` (returns new pane id)
14. `close_pane(pane: str)` -> `herdr pane close <pane>`

Rules for the implementer:
- Verify every subcommand's exact flags with `herdr <cmd> <sub> --help` before writing
  the wrapper; the table above is intent, the CLI help is truth.
- Docstring each tool for the model: one line of what it does, plus id format hints
  (e.g. "pane ids look like w17:p1; get them from list_panes/list_agents").
- Module-level `mcp = MCPServer("herdr")`; `main()` calls `mcp.run()`; expose
  `[project.scripts] herdr-mcp = "herdr_mcp.server:main"`.
- No print() anywhere. No auth. No config beyond env `HERDR_BIN` passthrough.

## Tests

`tests/test_server.py`, pytest + anyio (asyncio backend fixture per SDK docs):
- Stub herdr: tmp_path script (chmod +x) that prints a canned JSON envelope for the
  argv it receives and exits 0; failure stub exits 2 with stderr text. Set
  monkeypatch.setenv("HERDR_BIN", str(stub)).
- Tests:
  1. list_tools: all 14 tools present with expected names.
  2. list_panes returns the stub's result dict as structured content.
  3. read_pane returns plain text.
  4. CLI failure -> result.is_error True, stderr text in content.
  5. envelope with "error" key -> is_error True.
  6. send_keys/run_command pass argv through correctly (stub records argv to a file).
- Dev deps: pytest, anyio. Run: `uv run pytest -q`. All green required.

## Live smoke test (manual, run once after unit tests pass)

Against the real running herdr session on this machine (HERDR_ENV=1):
1. `uv run python -c` client script (or reuse in-memory client with real HERDR_BIN)
   calling list_workspaces, list_panes, list_agents — expect real ids.
2. Do NOT prompt/close/split panes in the user's live session. For mutation smoke
   (split/run/read/close), create a throwaway named session:
   `herdr session` tooling / `HERDR_SESSION`-style isolation per the repo's
   herdr-throwaway-repro skill; or simply skip mutation smoke and leave it to the
   dispatcher's verification step. Read-only smoke is the requirement here.

## README contents

- One-paragraph description; requirements (herdr installed and a running session).
- Install/run: `uv run herdr-mcp` from checkout, and
  `uvx --from git+https://github.com/aelaguiz/herdr-mcp herdr-mcp`.
- Client config snippets (Claude Code `claude mcp add`, generic mcpServers JSON).
- Tool table (name, one-line description).
- Explicit note: no auth, local stdio only, full control of your terminal session.

## Publication (dispatcher does this after verification)

Public GitHub repo `aelaguiz/herdr-mcp`, push master. Conventional lowercase commits.

## Definition of done

1. `uv run pytest -q` green in the repo.
2. `uv run herdr-mcp` starts and serves stdio (verified via in-memory/live client).
3. Read-only live smoke against the real session returns real workspace/pane ids.
4. README + pyproject complete; repo pushed public under aelaguiz.
