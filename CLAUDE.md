# Repo notes for Claude

This repo contains two sibling MCP servers under `src/`:

- `surfer_mcp` — controls the Surfer waveform viewer via WCP (TCP, JSON framing).
- `simvision_mcp` — controls Cadence SimVision via its Tcl command language. See `README-simvision.md` for the full surface.

## simvision_mcp architecture

- **Bootstrap:** Python writes a temporary Tcl script that opens a TCP server inside SimVision's own Tcl/Tk interpreter (`_BOOTSTRAP_TCL` in `src/simvision_mcp/client.py`). SimVision is launched with `-nosplash -input <that script>`. Once the Console window is realized, it sources the script and the listener comes up.
- **Wire protocol:** newline-delimited, hex-encoded UTF-8 — `<hex-cmd>\n` → `<status> <hex-result>\n`. Hex framing is used instead of base64 because SimVision ships Tcl 8.4, which has no `binary encode base64`.
- **Headless by default:** `SimVisionClient(headless=True)` spawns its own Xvfb on an unused display (via `Xvfb -displayfd`), points SimVision at it, and tears it down in `stop()`. Headless boot is ~8 s. An ssh-forwarded display takes ~60–120 s because SimVision waits for the window manager to realize the Console before evaluating `-input`.
- **Sessions:** `server.py` keeps a `_sessions: dict[str, SimVisionClient]`. Every tool takes a trailing `session: str | None = None`. `None` resolves to the sole active session, auto-creates a `"default"` one if none exist, or errors if >1 are active.
- **Screenshots:** `waveform print -file tmp.ps` → Ghostscript / ImageMagick / `ps2pdf`. For non-Waveform windows, `screenshot_gui` uses `xdotool` + `import`/`xwd` against the session's own display.

## SimVision gotchas worth remembering

- **Tcl 8.4.** No `string reverse`, no `dict`, no `binary encode base64`, no `lmap`. Stick to 8.4-safe commands. The hex framing uses `binary scan H*` / `binary format H*` for exactly this reason.
- **`return` at global scope raises an error.** When sending an expression through `tcl_eval`, use `set`/`expr`/bare command — not `return …`.
- **`database list` doesn't exist.** The right command is `database find` for post-processing mode, or `simcontrol database list` for simulation mode.
- **`browser find` needs a Design Browser window.** `list_scopes`/`list_signals` open one implicitly (`browser new -name mcp-browser` if none exists).
- **`cursor set -time` requires an existing cursor.** `set_cursor` dispatches to `cursor new` on a fresh session and `cursor set` thereafter.
- **VCD translation artifacts.** Opening a `.vcd` creates `.trn`/`.dsn` siblings; a second open fails unless `-overwrite` is passed. `load_database` sets it by default.
- **X11 auth is fragile over ssh.** Every `ssh -X` reconnect rotates the cookie in `~/.Xauthority`. `xauth add` from a stale entry can clobber the working cookie. Fix by reconnecting ssh — or by using headless mode, which sidesteps the problem entirely.

## Screenshots inline vs. on-disk

`screenshot_waveform` and `screenshot_gui` return a two-part MCP result: a text summary containing the saved path, followed by an inline `Image` payload. The agent sees the image directly in context, no separate `Read` call, and the file is still on disk for later reuse. Built on `mcp.server.fastmcp.utilities.types.Image`; FastMCP accepts `[str, Image]` lists as tool return values.

## Test layout

- `tests/test_quoters.py` — pure-Python Tcl quoting, no SimVision.
- `tests/test_rasterize.py` — `_rasterize_postscript` with a hand-crafted PS file, no SimVision.
- `tests/test_sessions.py` — session resolution, multi-session independence, `list_sim_windows`, `screenshot_gui`. Some tests are gated on `xdotool`/`xwd`/`import`.
- `tests/test_simvision_live.py` — raw Tcl round-trips (expr, strings, errors).
- `tests/test_tools_e2e.py` — full tool flow against `tests/data/tiny.vcd` — load, list, waveform window, cursor, zoom, screenshot.

`tests/conftest.py` provides a session-scoped `sv` fixture that creates a `SimVisionClient(headless=True)` and registers it as `_sessions["test"]`. No monkeypatching — tools find the client via the ordinary session-resolution path.

## Running tests

```bash
uv run --extra dev pytest tests/
```

(screenshot deps — `mss`, `python-xlib`, `Pillow` — are in the default dependency list, no extra needed.)

Full suite finishes in ~10 s under Xvfb. No outer `xvfb-run` wrapper needed; the client handles Xvfb internally.

## Debugging tips

- Per-session logs at `~/.simvision-mcp/simvision-mcp.log` (Python side) and `simvision-stderr.log` (SimVision side).
- `SIMVISION_MCP_PORT=<port>` attaches to an already-running SimVision that has sourced the bootstrap on that port — lets you iterate without paying the boot cost at all.
- If a tool is failing: try the same Tcl command through `tcl_eval` first to isolate whether the problem is in the tool wrapper or in SimVision.
