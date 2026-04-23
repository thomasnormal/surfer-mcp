# simvision-mcp

An MCP server that lets AI assistants drive [Cadence SimVision](https://www.cadence.com/en_US/home/tools/system-design-and-verification/simulation-and-testbench-verification/xcelium-simulator.html) through its Tcl command language. Part of the same repo as [surfer-mcp](README.md).

## What it can do

- **Open databases** — `.trn`/`.dsn` SST2, `.vcd`, and other SimVision-supported formats.
- **Browse hierarchy** — list scopes and signals matching glob patterns.
- **Control waveform windows** — create windows, add signals, set cursors and markers, zoom.
- **Run simulations** — `simcontrol run`, step, reset, stop (when connected to a live simulator).
- **Capture screenshots** — PNG/PDF of Waveform windows (via PostScript), or grab the whole GUI or any named window via X.
- **Raw Tcl escape hatch** — `tcl_eval` for anything the typed tools don't cover.

Multiple SimVision instances can run side-by-side as named **sessions**.

## How it works

1. Python writes a small Tcl bootstrap script that opens a TCP listener inside SimVision's own Tcl/Tk interpreter.
2. Python launches `simvision -nosplash -input <bootstrap>.tcl`.
3. When SimVision's Console is ready, it evaluates the bootstrap. Python connects over TCP and exchanges hex-framed Tcl commands and results.
4. By default the Python client spawns an **Xvfb** virtual framebuffer first and points SimVision at it, so no X forwarding or desktop is required. Headless boot is ~8 s vs. ~120 s on an ssh-forwarded display.

## Requirements

- Python 3.10+
- Cadence Xcelium (provides `simvision`) — your environment must have it on `PATH`.
- `Xvfb` for headless mode (`dnf install xorg-x11-server-Xvfb` / `apt install xvfb`).
- `gs` (Ghostscript) or `convert` (ImageMagick) for PNG waveform screenshots.
- For full-GUI screenshots (`screenshot_gui` / `list_sim_windows`): install the `screenshot` extra with `uv sync --extra screenshot` — pulls in `mss`, `python-xlib`, and `Pillow`. No sudo, no external binaries.

## Installation

```bash
git clone <this-repo> surfer-mcp
cd surfer-mcp
uv sync
```

Register with Claude Code:

```bash
claude mcp add --scope user simvision uv -- run --directory /absolute/path/to/surfer-mcp simvision-mcp
```

Or by editing `~/.claude.json`:

```json
{
  "mcpServers": {
    "simvision": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/surfer-mcp", "simvision-mcp"]
    }
  }
}
```

Claude Code will health-check it at startup — you should see `simvision: ✓ Connected` in `claude mcp list`.

## Tool overview

### Sessions

| Tool | Description |
|------|-------------|
| `create_session(name?, headless=True, geometry="1920x1200x24")` | Launch a SimVision instance. Auto-name if `name` omitted. |
| `close_session(name)` | Stop an instance, tear down its Xvfb. |
| `list_sessions()` | JSON list of active sessions with display / PID / mode. |

Every other tool takes a trailing `session: str | None = None` kwarg. Leave it `None` when exactly one session is active; SimVision is lazily created if you call a tool with no active session.

### Databases and hierarchy

| Tool | Description |
|------|-------------|
| `load_database(path, name?, overwrite=True)` | Open a `.vcd`/`.trn`/`.dsn`. Returns `{name, path, start, end, time_unit, scope_count, signal_count}`. |
| `close_database(name)` | Close one database. (Use `describe_session` to list.) |
| `list_scopes(parent?, pattern?)` | Recursive scope listing. Returns JSON array. |
| `list_signals(parent?, pattern?)` | Recursive signal listing. Buses appear as single entries. |
| `scope(new?)` | Get current scope (no arg) or set it (`new=...`). |

### Waveform window

| Tool | Description |
|------|-------------|
| `new_waveform_window(name?)` | Create a Waveform window. |
| `add_signals(signals[], scope?, window?)` | Add signals. Auto-normalizes `db::path` → `db:::path`. Returns qualified paths as JSON. |
| `add_signals_matching(pattern, parent?, window?)` | Glob-search + add in one call. |
| `clear_waveform(window?)` | Drop all signals. |
| `viewport(min_time?, max_time?, window?)` | Get current range (no args), set range (both given), or zoom-to-full (`min_time="full"`). |

### Cursors and markers

| Tool | Description |
|------|-------------|
| `set_cursor(time, cursor?)` | Place or move a cursor. Creates by name if the cursor doesn't exist, or picks up the first existing one. Returns the cursor name. |
| `delete_cursor(name)` | Delete a cursor. (Use `describe_session` to list cursors with their times.) |
| `new_marker(time, name?, color?)` / `delete_marker(name)` | Time markers. (Use `describe_session` to list.) |

### Simulation control (simulation mode only)

`sim_run(time?)`, `sim_stop()`, `sim_step(into=False)`, `sim_reset()`, `sim_showvalue(names[])`.

### Signal values

`signal_values(time, signals?, window?)` — read values at a specific time. Returns `{signal: value, ...}`. With no `signals` argument, reads all signals in the waveform window.

### Screenshots

| Tool | Description |
|------|-------------|
| `screenshot_waveform(output_path, window?)` | PNG/JPG/PDF/PS of a Waveform window (output format from extension). Landscape-oriented. **Returns the image inline** so the agent sees it directly. |
| `screenshot_gui(output_path, window_title?)` | PNG of the whole display or a specific X window by title substring. Useful for Schematic Tracer, Source Browser, UVM Sequence Viewer, etc. Also returns inline. |
| `list_sim_windows()` | Enumerate X window titles of the current SimVision session. |

### Diagnostics

| Tool | Description |
|------|-------------|
| `describe_session(session?)` | JSON snapshot: databases, waveform windows (with viewports), cursors (with times), markers, current scope. |
| `get_log_paths()` / `tail_log(kind, lines)` | Find and read the MCP/SimVision logs for self-debugging. |

### Escape hatch

| Tool | Description |
|------|-------------|
| `tcl_eval(command)` | Raw Tcl in SimVision's global scope. Full access to every SimVision command (`waveform`, `cursor`, `database`, `simcontrol`, `browser`, `schematic`, `simcompare`, …). |

## Headless use

Useful patterns:

- **CI pipelines** — regression jobs can open `.shm`s, position cursors at failure times, snapshot the waveform, and attach to PR artifacts. No desktop required.
- **Batch visual diffing** — reference PNGs checked in; regenerate with the same cursor/zoom settings and image-diff to flag unexpected waveform changes.
- **Remote debugging** — Claude connects to a remote build box, opens a test's waveform, walks the hierarchy, grabs a screenshot, and sends findings + image back. User never opens a GUI locally.
- **Multi-instance comparison** — two sessions running in parallel (e.g. `main` and `feature-branch` databases) and the agent diffs their values.

## SimVision gotchas

- **Tcl 8.4.** No `string reverse`, `dict`, `binary encode base64`, `lmap`. If you use `tcl_eval` with newer idioms, they will fail.
- **`return` at global scope raises.** In `tcl_eval`, use `set`/`expr`/bare command whose value you want.
- **Database translation files.** Opening a `.vcd` creates `.trn`/`.dsn` siblings. `load_database` defaults `overwrite=True` so repeated runs don't fail.
- **Startup latency.** Headless (Xvfb): ~8 s. ssh-X display: ~60–120 s. The client waits up to 180 s. Creating the first session in an agent loop is the expensive moment; everything after is fast.

## Testing

```bash
uv run --extra dev pytest tests/
```

Full suite runs in ~10 s thanks to Xvfb. The client spawns its own Xvfb — no outer `xvfb-run` needed.

## TODO: untapped SimVision surface

SimVision's Tcl API is deep. The current MCP tools cover a minimal vertical slice
(databases, scopes/signals, waveform windows, cursors/markers, screenshots).
Everything else is still reachable through `tcl_eval`, but a typed tool makes
any feature easier for an agent to discover. Rough backlog, roughly ordered by
agent-workflow value:

### Event-driven notifications (big unlock)

SimVision fires Tcl callbacks on cursor moves, marker changes, signal selection,
breakpoint hits, waveform state changes, UI clicks, and plug-in events
(`cursor notify add`, `marker notify`, `select notify`, `waveform callback`,
`window extensions notify`, `simcontrol breakpoint create`…). Today we're
strictly request/response; none of these fire to the agent.

Sketch for adding support:

1. **Reverse channel.** The bootstrap opens a second TCP socket dedicated to
   events. Tcl notify callbacks JSON-encode the event and write it there.
2. **Async queue.** `SimVisionClient` grows an `asyncio.Queue`, drained by a
   background reader task — keeps request/response traffic unblocked.
3. **Tools:**
     - `subscribe(events=[...])` — registers Tcl notify callbacks.
     - `poll_events(timeout, max)` — returns queued events. Long-polling within
       MCP's tool-call timeout is fine.
     - `unsubscribe()`.
4. **Event schema:** `{type, session, time, target, data}`.

Unlocks:
  - "Run simulation; wake me on the next assertion fail" (one `poll_events`).
  - Human-in-the-loop collab: react when the user clicks a signal, moves a
    cursor, or selects a scope in SimVision's GUI.
  - Breakpoint / watchpoint / condition callbacks surfaced in real time.

### Simulation control (live, not post-process)

Already exposed: `sim_run/stop/step/reset/showvalue`. Missing:
  - `force_signal(name, value, time?)` / `release_signal(name)` —
    `simcontrol force create / delete`. Lets the agent drive inputs directly.
  - `deposit_signal(name, value)` — `simcontrol deposit create`. One-shot
    without sticky force.
  - `add_probe(name, database?)` / `enable_probe/disable_probe/list_probes` —
    `simcontrol probe create/enable/disable/list`. Dynamic probe management.
  - `set_breakpoint(condition | signal | assertion)` — `simcontrol breakpoint
    create`. Essential for interactive bug hunts.

### Assertions

- `assertbrowser new`, `assertbrowser select`, `assertbrowser find` for the
  Assertion Browser. Agents could list failing assertions, inspect triggers,
  and walk the antecedent signals.
- `simcontrol breakpoint create assertion <name>` to halt sim on a specific
  assertion change.

### Schematic / source / FSM viewers

These are powerful but currently invisible to the agent:
  - `schematic` — Schematic Tracer. Connectivity walks ("which signals drive
    `top.dut.x`?"), fan-in/fan-out analysis.
  - `srcbrowser` — Source Browser. Open source file at a specific line,
    jump to a module definition.
  - `fsmviewer` — FSM visualizer.
  - `cycleview` — Simulation Cycle Debugger.
  - `register` / `memviewer` — Register and Memory windows for inspecting RAM
    / register file contents live.

`screenshot_gui` already works on these if you can open them via `tcl_eval`,
so the gap is just the open/configure step.

### UVM viewers (high value for UVM testbenches)

- `uvmregviewer` — Register model introspection.
- `uvmseqviewer` — Sequence hierarchy / activity.
- `uvmconfigviewer` — Config DB contents.

### Comparison and measurement

- `compare` (SimCompare Manager): diff two simulation databases. Huge for
  regression debugging — "why does `main` produce different outputs than
  this branch at t=500ns?". Could expose `compare_databases(a, b, signals)` →
  list of `(time, a_value, b_value)` mismatches.
- `measurement` — frequency, glitch-width, delta-time measurements. Expose
  `measure_frequency(signal, window)` / `measure_pulse_width(...)` for
  numeric agent queries.
- `condition new -expr <expr>` — compound named expressions. Lets the agent
  define a derived signal once and add it to waveforms, query values, set
  breakpoints against it.

### Display customization

- `highlight` — colour-code signals by value/condition. Useful for
  screenshot-driven debugging.
- `mmap` (mnemonic maps) — translate signal values to names (e.g. `0x5 →
  RUNNING`). One `apply_mnemonic_map(signal, map)` makes waveforms far more
  readable in screenshots.
- `waveform format` — per-signal radix (hex/dec/binary/ascii/mnemonic).

### Search (Design Search window / `dbfind`)

`dbfind` drives SimVision's Design Search window — a separate surface from
`browser find`, and considerably more powerful:

- **Multi-database, hierarchy-agnostic.** Searches across every open database
  and through the entire design tree simultaneously, so it works even before
  a Design Browser has been expanded.
- **Rich filters.** Name pattern (exact/glob/regex), direction (`inputs` /
  `outputs` / `inouts` / `internals` / `all`), strength, signal type
  (scalar/bus/analog/record), scope type, whether a signal has transitions,
  assertion targets, UVM sequence activity, etc. Pattern matching works on
  both the simple name and the full hierarchical path.
- **Saved searches / result recall.** `dbfind store`, `dbfind recall`,
  `dbfind merge` — useful when the same search seed keeps coming up
  ("every interface signal in the dut that toggled during test_vendor_005").
- **Result post-processing.** `dbfind select` to highlight matches in other
  SimVision windows; `dbfind send` to push results into a Waveform / Watch /
  Schematic window directly. An MCP wrapper could return the match list
  *and* stuff matches into a waveform in one call.

Proposed tool shape:

```
search_design(
  name_pattern, match="glob", direction=None, sigtype=None,
  has_transitions=None, scope=None, limit=100,
) -> JSON [{name, type, scope, database}]
```

Would subsume most `browser find` workflows and handle cross-database cases
we currently don't support at all.

### Plug-in surface

- `window extensions menu create` / `window extensions button create` /
  `action define` — lets the MCP server add custom menu items and buttons to
  SimVision's GUI. Could expose "mark this moment", "capture agent
  snapshot", "copy signal path to MCP" as one-click UI affordances for
  humans co-operating with the agent.

## License

MIT
