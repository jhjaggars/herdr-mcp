# herdr-mcp

An MCP server that gives a coding agent control of a running
[Herdr](https://github.com/herdrdev/herdr) terminal session: list workspaces,
tabs, panes and agents, read pane output, prompt other agents, send keys, run
commands, wait for state or output, and split or close panes.

It is a thin **stdio** bridge. Every tool shells out to the `herdr` CLI, which
already speaks Herdr's socket API and answers with JSON envelopes. There is no
daemon, no port, no config file, and no auth.

## Requirements

- `herdr` installed and on `PATH` (or set `HERDR_BIN` to its full path).
- A running Herdr session. The server talks to whatever session the `herdr` CLI
  resolves from the environment (`HERDR_SOCKET_PATH` / `HERDR_SESSION`).
- Python 3.12+ and [uv](https://docs.astral.sh/uv/).

## Run

From a checkout:

```bash
uv run herdr-mcp
```

Straight from GitHub, no checkout:

```bash
uvx --from git+https://github.com/aelaguiz/herdr-mcp herdr-mcp
```

The process serves MCP over stdio, so it prints nothing to stdout; logs go to
stderr (`HERDR_MCP_LOG_LEVEL=DEBUG` for argv tracing).

## Client configuration

Claude Code:

```bash
claude mcp add herdr -- uvx --from git+https://github.com/aelaguiz/herdr-mcp herdr-mcp
```

Generic `mcpServers` JSON (Claude Desktop, Cursor, Codex, and friends):

```json
{
  "mcpServers": {
    "herdr": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/aelaguiz/herdr-mcp", "herdr-mcp"],
      "env": {}
    }
  }
}
```

From a local checkout instead:

```json
{
  "mcpServers": {
    "herdr": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/herdr-mcp", "herdr-mcp"]
    }
  }
}
```

## Tools

Pane ids look like `w17:p1`, tab ids like `w17:t1`, workspace ids like `w17`.
Agent tools accept a pane id or an agent name. Every tool returns the parsed
`result` object from the herdr JSON envelope; `read_pane` returns plain text.

| Tool | What it does |
| --- | --- |
| `list_workspaces` | List workspaces with ids, labels, and aggregate agent status. |
| `list_tabs` | List tabs across the session. |
| `list_panes` | List panes with ids, workspace, tab, cwd, title, agent status. |
| `list_agents` | List panes running a detected agent, with kind and status. |
| `get_agent` | Show one agent's current state (pane id only). |
| `read_pane` | Read pane output as text (`source`: visible, recent, recent-unwrapped, detection). |
| `prompt_agent` | Submit a prompt to an agent, optionally waiting until it settles. |
| `wait_agent` | Wait until an agent reaches idle/working/blocked/done/unknown. |
| `send_text` | Send literal text to a pane without Enter. |
| `send_keys` | Send key presses, e.g. `["ctrl-c"]`, `["esc"]`, `["enter"]`. |
| `run_command` | Run a command in a pane (text + Enter). |
| `wait_for_output` | Wait for a literal substring or Rust regex in pane output. |
| `split_pane` | Split a pane right or down and return the new pane id. |
| `close_pane` | Close a pane and whatever is running in it. |

Failures from the CLI (unknown pane, timeout, dead socket) come back as MCP tool
errors carrying herdr's own error code and message, so the model can correct
itself.

## Security

This fork is **scoped and fail-closed**. No auth, local stdio only, but:

- `HERDR_MCP_WORKSPACES` is required; the server refuses to start without it.
  It lists workspace ids (`wS`) and/or labels (`openclaw`) the client may touch.
- Listings (`list_*`) are filtered to those workspaces.
- Every pane argument is re-checked against a fresh `pane list` on every call and
  must exactly equal an in-scope `pane_id`. Agent names, terminal ids and other
  aliases are rejected, so the string passed to herdr is always one we just
  listed. Moving a pane out of the workspace revokes access immediately.
- Only the `read` tool group is registered by default. Opt in to more with
  `HERDR_MCP_TOOLS` (`read,prompt,input,layout`). `input` (send_text, send_keys,
  run_command) is arbitrary code execution in the in-scope panes.

Labels are not unique in herdr; every workspace carrying an allowed label is in
scope. This is not a sandbox against the herdr socket itself: anything that can
reach the socket directly bypasses it, so don't mount the socket into the client.

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `HERDR_BIN` | `herdr` | Path to the herdr binary. |
| `HERDR_MCP_WORKSPACES` | *(required)* | Comma-separated workspace ids or labels in scope. |
| `HERDR_MCP_TOOLS` | `read` | Comma-separated tool groups: `read`, `prompt`, `input`, `layout`. |
| `HERDR_MCP_LOG_LEVEL` | `WARNING` | Python log level (stderr). |

Everything else in the environment is passed through untouched, which is how the
CLI finds the right session socket.

## Development

```bash
uv sync
uv run pytest -q
```

Unit tests drive the server through the SDK's in-memory client against a stub
`herdr` script, so they never touch a live session.
