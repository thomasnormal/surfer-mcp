# simvision-wcp

A standalone **WCP (Waveform Communication Protocol) server** that puts SimVision behind the same wire protocol [Surfer](https://surfer-project.org/) speaks. Any WCP-compatible client — an editor plugin, Surfer itself in client mode, a Python script, a CI runner — can drive SimVision without knowing Tcl.

**Spec fidelity:** all 17 commands + 4 events from Surfer's canonical [`surfer-wcp/src/proto.rs`](https://gitlab.com/surfer-project/surfer/-/blob/main/surfer-wcp/src/proto.rs) are implemented and covered by tests (see "Coverage" below).

## How it works

```
WCP client ──TCP + JSON+NUL──▶ simvision-wcp proxy ──Tcl socket──▶ SimVision
           ◀──responses───── (Python)              ◀─Tcl results──
           ◀──events──────── (menu / notify hooks) ◀─evt pushes───
```

1. `simvision-wcp` binds a TCP port and listens.
2. On startup it launches SimVision via the shared [`simvision_mcp.client.SimVisionClient`](src/simvision_mcp/client.py) — same Xvfb-by-default behaviour (~8 s boot), same Tcl bootstrap, same lifecycle handling.
3. Every incoming WCP command is translated to Tcl, forwarded, and the result is returned in the WCP response shape.
4. A background reader demultiplexes `evt` frames pushed from SimVision's Tcl (from `cursor notify`, custom menu clicks, etc.) and forwards the ones the client subscribed to.
5. On connect, the server injects three menu items into SimVision's Waveform window (**WCP → Goto Declaration**, **WCP → Add Drivers**, **WCP → Add Loads**); clicking any of them emits the corresponding WCP event.

## Install

Shared deps with the rest of the repo — one `uv sync`:

```bash
uv sync            # runtime
uv sync --extra dev  # + pytest/pytest-asyncio for the test suite
```

Runtime requirements:

- Python 3.10+
- Cadence Xcelium (for `simvision` on `PATH`)
- `Xvfb` for headless mode (`dnf install xorg-x11-server-Xvfb` / `apt install xvfb`)

## Run

```bash
# Default: headless Xvfb, port 8080
simvision-wcp

# Use the ambient $DISPLAY instead of spawning Xvfb
simvision-wcp --no-headless

# Bind a specific port
simvision-wcp --port 24550

# Attach to an already-running SimVision that's sourced the MCP bootstrap
# (skips the launch — useful for iteration without 8 s cold-starts).
simvision-wcp --attach 9876

# -v / -vv for debug logging
simvision-wcp -vv
```

Startup prints the chosen port to stdout once the listener is up:

```
simvision-wcp listening on 127.0.0.1:8080
```

## Use from any WCP-speaking client

Wire format is identical to Surfer's WCP — null-terminated JSON frames over TCP.

Handshake (client → server):

```json
{"type": "greeting", "version": "0", "commands": ["waveforms_loaded", "goto_declaration"]}
```

Server responds with the command list it supports (always all 17 plus `greeting`):

```json
{"type": "greeting", "version": "0", "commands": ["add_items", "add_markers", ...]}
```

Subsequent commands (`{"type": "command", "command": "load", "source": "/tmp/x.vcd"}`) get typed responses (`{"type": "response", "command": "load"}`) and errors (`{"type": "error", "command": "load", "message": "..."}`). Events arrive asynchronously and are only delivered if the client advertised the event name in its greeting.

## Use from Python

A first-class Python WCP client ships with this package:

```python
import asyncio
from simvision_wcp.client import WcpClient

async def main():
    async with await WcpClient.connect(
        port=8080,
        events=["waveforms_loaded", "goto_declaration"],
    ) as client:
        await client.call("load", source="/path/to/waves.vcd")
        added = await client.call(
            "add_scope", scope="tiny::tb", recursive=True,
        )
        print("added item refs:", added["ids"])

        await client.call("set_cursor", timestamp=500)
        await client.call("zoom_to_fit", viewport_idx=0)

        # Wait for the human to pick "WCP → Goto Declaration" on a signal.
        ev = await client.recv_event(timeout=60)
        print("goto", ev["variable"])

asyncio.run(main())
```

## Supported commands

All 17 commands from `proto.rs`:

| Command | Response | Notes |
|---|---|---|
| `load {source}` | ack | Opens a database. Emits `waveforms_loaded`. |
| `reload` | ack | Re-opens the last-loaded source. |
| `add_variables {variables}` | `{ids}` | |
| `add_scope {scope, recursive}` | `{ids}` | |
| `add_items {items, recursive}` | `{ids}` | Mixed scopes + signals. |
| `add_markers {markers}` | `{ids}` | Each marker: `{time, name?, move_focus}`. |
| `remove_items {ids}` | ack | |
| `clear` | ack | |
| `set_cursor {timestamp}` | ack | Creates a cursor if none exists. |
| `set_viewport_range {start, end}` | ack | |
| `set_viewport_to {timestamp}` | ack | Centers viewport on the timestamp. |
| `zoom_to_fit {viewport_idx}` | ack | `viewport_idx` ignored — SimVision has one viewport per window. |
| `focus_item {id}` | ack | |
| `set_item_color {id, color}` | ack | Named colours (`red`) or `#rrggbb`. |
| `get_item_list` | `{ids}` | Currently displayed item refs. |
| `get_item_info {ids}` | `{results}` | Each: `{name, type, id}`. |
| `shutdown` | ack | Tears down SimVision + Xvfb. |

## Supported events

| Event | Trigger |
|---|---|
| `waveforms_loaded {source}` | After successful `load` or `reload`. |
| `goto_declaration {variable}` | User clicks **WCP → Goto Declaration** on a signal. |
| `add_drivers {variable}` | User clicks **WCP → Add Drivers** on a signal. |
| `add_loads {variable}` | User clicks **WCP → Add Loads** on a signal. |

Events are only delivered to a client if that client named them in its greeting.

## Coverage

19 integration tests exercise every command and event against a real SimVision + Xvfb:

```bash
uv run --extra dev pytest tests/test_wcp_server.py -v
```

## Limitations / caveats

- **`viewport_idx` is ignored** — SimVision doesn't split waveform windows the way Surfer does.
- **`get_item_info.type`** is inferred from SimVision's signal width (`signal` vs `bus`). Richer types (`struct`, `vhdl_record`) are not distinguished.
- **`set_item_color`** accepts what SimVision's `highlight add -color` accepts. CSS `rgb()` / `rgba()` are rejected.
- **Multi-client** — multiple WCP clients can connect; all share a single SimVision and all receive events (if subscribed). No isolation.
- **Reconnection** — if SimVision dies mid-session, the WCP client is disconnected; no auto-restart.

## When to use this vs `simvision-mcp`

- **`simvision-mcp`** — for an LLM / agent driving SimVision through MCP tool calls. Bigger surface (~30 tools including signal-value queries, screenshots, simulation control, `tcl_eval` escape hatch).
- **`simvision-wcp`** — for any WCP-speaking tool. Smaller, fixed surface defined by Surfer's protocol. Good fit when you want editor / IDE / third-party integrations.

They're designed to coexist — both drive the same SimVision via the same Tcl bootstrap, just behind different protocols.

## License

MIT
