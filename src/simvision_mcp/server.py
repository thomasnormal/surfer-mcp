"""MCP server exposing Cadence SimVision (Xcelium) controls as tools.

Sessions
--------
Multiple SimVision processes can be driven concurrently. Each one is a
"session" with a string name. Sessions are created explicitly with
`create_session` or lazily on first tool call (a default headless session is
spawned if none exist). Every tool takes a trailing `session` kwarg — pass the
name, or leave it None when exactly one session is active.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image

from simvision_mcp.client import (
    LOG_DIR,
    SimVisionClient,
    SimVisionError,
    parse_tcl_list,
    tcl_brace,
    tcl_list,
)


# SimVision time format: "<int>(<seq>)<unit>" or "<int><unit>". E.g. "0(0)ns",
# "1500ns", "-5ps". Extract the scalar and unit; drop the sequence number.
_SV_TIME_RE = re.compile(
    r"^\s*(?P<val>-?\d+(?:\.\d+)?)(?:\(\d+\))?\s*(?P<unit>[a-zA-Z]*)\s*$"
)


def _parse_sv_time(s: str) -> tuple[int | float | None, str]:
    """Parse SimVision-formatted time into (value, unit).

    Returns (None, "") if it doesn't match the expected format — lets the
    caller fall back to the raw string.
    """
    if s is None:
        return (None, "")
    m = _SV_TIME_RE.match(s)
    if not m:
        return (None, "")
    val_s = m.group("val")
    val: int | float = float(val_s) if "." in val_s else int(val_s)
    return val, m.group("unit") or ""


def _normalize_signal_path(path: str) -> str:
    """Convert `db::scope.sig` to `db:::scope.sig`.

    SimVision's convention is 2 colons for scope paths (`db::scope`) and
    3 colons for signal paths (`db:::scope.sig`). Agents routinely get this
    wrong. This rewrites `<word>::<word>` (exactly 2 colons, not part of a
    longer `:::` run) to the triple-colon form.
    """
    if ":::" in path:
        return path
    # Match `<db>::` where what follows is not another colon. \w includes _.
    return re.sub(r"(\w+)::(?!:)", r"\1:::", path, count=1)

logger = logging.getLogger(__name__)

mcp = FastMCP("simvision")

# name -> client
_sessions: dict[str, SimVisionClient] = {}


def _resolve_session_name(session: str | None) -> str:
    """Pick the session name to target.

    Explicit name → that session (error if unknown).
    None + 0 sessions → create a default headless one.
    None + 1 session → use it.
    None + >1 sessions → error asking for explicit name.
    """
    if session is not None:
        if session not in _sessions:
            raise SimVisionError(
                f"unknown session {session!r}. Active: {list(_sessions)}"
            )
        return session
    if not _sessions:
        _sessions["default"] = SimVisionClient(headless=True)
        return "default"
    if len(_sessions) == 1:
        return next(iter(_sessions))
    raise SimVisionError(
        f"multiple sessions active ({list(_sessions)}); "
        f"pass session=<name> to disambiguate"
    )


async def _sv(session: str | None = None) -> SimVisionClient:
    name = _resolve_session_name(session)
    c = _sessions[name]
    if not c.running:
        await c.start()
    return c


def _using(window: str | None) -> str:
    return f" -using {tcl_brace(window)}" if window else ""


async def _ensure_waveform_window(client) -> None:
    # `waveform add` errors with "no waveform window name entered" when no
    # window is current. Auto-create one the first time we need it so callers
    # can `load_database` → `add_signals` without the GUI setup step.
    await client.send(
        "if {[catch {waveform using} __r]} {waveform new -name main}; set _ ok"
    )


async def _send_as_json_list(tcl: str, session: str | None = None) -> str:
    """Run a Tcl command expected to return a list, return it as a JSON array.

    Raw Tcl list output uses brace-quoting for items with special characters
    (e.g. `{foo[3]}`). Parsing that client-side is fragile; returning a JSON
    array is unambiguous for downstream consumers.
    """
    raw = await (await _sv(session)).send(tcl)
    return json.dumps(parse_tcl_list(raw))


# -----------------------------------------------------------------------------
# Session management
# -----------------------------------------------------------------------------

@mcp.tool()
async def create_session(
    name: str | None = None,
    headless: bool = True,
    geometry: str = "1920x1200x24",
) -> str:
    """Launch a new SimVision instance. Returns the session name.

    Args:
        name: Friendly handle. Auto-generated ("sv1", "sv2", …) if omitted.
        headless: True (default) spawns an Xvfb virtual display — boots in
            ~8s and requires no X forwarding. False uses the ambient $DISPLAY,
            which is ~120s on an ssh-forwarded X and fails if no display is set.
        geometry: Xvfb screen geometry, e.g. "1920x1200x24". Ignored if
            headless=False.

    After this, any other tool can refer to this session via `session=<name>`.
    If there is exactly one active session, `session=` can be omitted.
    """
    if name is None:
        i = 1
        while f"sv{i}" in _sessions:
            i += 1
        name = f"sv{i}"
    if name in _sessions and _sessions[name].running:
        raise SimVisionError(f"session {name!r} already exists and is running")

    client = SimVisionClient(headless=headless, geometry=geometry)
    await client.start()
    _sessions[name] = client
    logger.info("session %r started (headless=%s, display=%s, pid=%s)",
                name, headless, client.display, client.pid)
    return name


@mcp.tool()
async def close_session(name: str) -> str:
    """Stop a SimVision instance. Tears down Xvfb if we started one."""
    if name not in _sessions:
        return f"no session named {name!r}"
    await _sessions[name].stop()
    del _sessions[name]
    return f"closed session {name!r}"


@mcp.tool()
async def list_sessions() -> str:
    """Return a JSON list of active sessions with their display, PID, and mode."""
    out = []
    for n, c in _sessions.items():
        out.append({
            "name": n,
            "running": c.running,
            "headless": c.headless,
            "display": c.display,
            "pid": c.pid,
        })
    return json.dumps(out, indent=2)


@mcp.tool()
async def get_log_paths(session: str | None = None) -> str:
    """Return the paths of the MCP client and SimVision stderr logs.

    Useful when the agent needs to diagnose a tool failure — the returned
    JSON paths can be read with the standard file-read tool and grepped for
    errors.
    """
    return json.dumps({
        "mcp_log": os.path.join(LOG_DIR, "simvision-mcp.log"),
        "simvision_stderr": os.path.join(LOG_DIR, "simvision-stderr.log"),
        "log_dir": LOG_DIR,
    })


@mcp.tool()
async def tail_log(kind: str = "mcp", lines: int = 100) -> str:
    """Return the last N lines of a log file.

    Args:
        kind: `"mcp"` for `~/.simvision-mcp/simvision-mcp.log` (Python-side
            request/response log) or `"simvision_stderr"` for
            `~/.simvision-mcp/simvision-stderr.log` (SimVision's own
            complaints).
        lines: How many trailing lines to return (default 100).
    """
    paths = {
        "mcp": os.path.join(LOG_DIR, "simvision-mcp.log"),
        "simvision_stderr": os.path.join(LOG_DIR, "simvision-stderr.log"),
    }
    path = paths.get(kind)
    if path is None:
        return f"Error: unknown kind {kind!r}; pick one of {list(paths)}"
    if not os.path.isfile(path):
        return f"Error: log file not found: {path}"

    # Tail by reading the file backwards in chunks until we have enough
    # newlines. Cheaper than loading everything on a long-running process.
    want = max(1, lines)
    chunk = 8192
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        buf = b""
        while size > 0 and buf.count(b"\n") <= want:
            read = min(chunk, size)
            size -= read
            f.seek(size)
            buf = f.read(read) + buf
    text = buf.decode("utf-8", "replace")
    tail_lines = text.splitlines()[-want:]
    return "\n".join(tail_lines)


@mcp.tool()
async def describe_session(session: str | None = None) -> str:
    """One-shot snapshot of a session's state, including cursor positions
    and per-window viewports.

    Useful after a long conversation or restart, when the agent has lost track
    of what the session has open. Example output:

        {
          "name": "sv1", "display": ":12", "pid": 3042,
          "scope": "::",
          "databases": ["tiny"],
          "waveform_windows": [
              {"name": "main", "viewport": ["0", "100ns"]}
          ],
          "cursors": [
              {"name": "TimeA", "time": "55ns"}
          ],
          "markers": []
        }
    """
    client = await _sv(session)
    # One compound Tcl call — atomic snapshot with enriched structure.
    tcl = (
        "set __r {};"
        " if {[catch {database find} __x]} {set __x {}};"
        " lappend __r databases $__x;"
        # Enumerate waveform windows and their viewports.
        " set __ww {};"
        " foreach __w [window find] {"
        "   if {[catch {waveform get -using $__w -name} __n]} {continue};"
        "   set __vp {};"
        "   catch {set __vp [waveform xview limits -using $__w]};"
        "   lappend __ww [list name $__n viewport $__vp]"
        " };"
        " lappend __r waveform_windows $__ww;"
        # Cursors with their times.
        " set __cc {};"
        " if {[catch {cursor find} __cs]} {set __cs {}};"
        " foreach __c $__cs {"
        "   set __t {};"
        "   catch {set __t [cursor get -using $__c -time]};"
        "   lappend __cc [list name $__c time $__t]"
        " };"
        " lappend __r cursors $__cc;"
        " if {[catch {marker find} __x]} {set __x {}};"
        " lappend __r markers $__x;"
        " if {[catch {scope get -long} __x]} {set __x {}};"
        " lappend __r scope $__x;"
        " set __r"
    )
    raw = await client.send(tcl)
    parts = parse_tcl_list(raw)
    pairs = dict(zip(parts[0::2], parts[1::2]))

    def _dicts_from_key_val(tcl_list_of_lists: str) -> list[dict]:
        """Each inner element is itself a key/value Tcl list."""
        inner = parse_tcl_list(tcl_list_of_lists)
        out = []
        for entry in inner:
            kv = parse_tcl_list(entry)
            out.append(dict(zip(kv[0::2], kv[1::2])))
        return out

    out: dict = {
        "name": _resolve_session_name(session),
        "display": client.display,
        "pid": client.pid,
        "scope": pairs.get("scope", ""),
        "databases": parse_tcl_list(pairs.get("databases", "")),
        "waveform_windows": _dicts_from_key_val(pairs.get("waveform_windows", "")),
        "cursors": _dicts_from_key_val(pairs.get("cursors", "")),
        "markers": parse_tcl_list(pairs.get("markers", "")),
    }
    # Normalize viewport from Tcl-list string to [min, max] list.
    for w in out["waveform_windows"]:
        if "viewport" in w and isinstance(w["viewport"], str):
            w["viewport"] = parse_tcl_list(w["viewport"])
    return json.dumps(out, indent=2)


# -----------------------------------------------------------------------------
# Generic escape hatch
# -----------------------------------------------------------------------------

@mcp.tool()
async def tcl_eval(command: str, session: str | None = None) -> str:
    """Evaluate a raw Tcl command inside SimVision and return its result.

    Escape hatch for operations not covered by the typed tools. The command
    runs in the global scope with access to every SimVision Tcl command
    (`waveform`, `cursor`, `database`, `simcontrol`, `browser`, ...).

    Gotchas:
      - SimVision ships Tcl 8.4 — no `dict`, `lmap`, `string reverse`,
        `binary encode base64`.
      - `return <val>` at global scope raises. End with a bare expression
        or `set tmp <val>` to return a value instead.

    Useful recipes (substitute your own signal / time / database names):

        # Signal value(s) at a specific time. Signals must already be in
        # a Waveform window.
        waveform values -using "Waveform 1" -at 500ns db:::top.clk

        # Find the next rising edge of a 1-bit signal after a given time.
        # Returns the time or empty if none. The database "db" and signal
        # "top.clk" must already be opened/displayed.
        waveform search -using "Waveform 1" \\
            -forward -start 500ns -type posedge \\
            -signal db:::top.clk

        # Find next falling edge: -type negedge.
        # Find next value match:  -type value -value 0101

        # Get all transitions of a signal in a time window by scripting
        # `waveform search` in a loop. Pseudocode:
        #   set t 0; while {$t < $end} {
        #     set t [waveform search -forward -start $t -type edge -signal $sig]
        #     if {$t eq ""} break
        #     lappend xs [list $t [waveform values -at $t $sig]]
        #   }

        # Simulation time range of the current database.
        database get -limits

        # Zoom the current Waveform window to fit all data.
        waveform xview zoom -outfull

        # Read the current cursor time.
        cursor get -time

        # Apply a radix to a signal displayed in a waveform window.
        waveform format -using "Waveform 1" db:::top.bus -radix hex
    """
    return await (await _sv(session)).send(command)


# -----------------------------------------------------------------------------
# Databases
# -----------------------------------------------------------------------------

@mcp.tool()
async def load_database(
    path: str,
    name: str | None = None,
    overwrite: bool = True,
    session: str | None = None,
) -> str:
    """Open a simulation database (`.trn`/`.dsn`, `.vcd`, or supported format).

    Args:
        path: Absolute path to the database file (or directory with one).
        name: Optional logical name for the database. SimVision picks one
            otherwise. Used as the prefix when referencing signals, e.g.
            `<name>:::top.dut.clk`.
        overwrite: When the file is in a format that SimVision must translate
            (e.g. VCD → SST2), re-translate rather than failing if translated
            siblings exist. Defaults True.

    Returns a JSON object: `{name, path, start, end, scope_count, signal_count}`.
    """
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        return f"Error: path not found: {abs_path}"
    open_cmd = f"database open {tcl_brace(abs_path)}"
    if name:
        open_cmd += f" -name {tcl_brace(name)}"
    if overwrite:
        open_cmd += " -overwrite"
    client = await _sv(session)
    db_name = await client.send(open_cmd)
    # Collect summary in one roundtrip.
    summary_tcl = (
        f"set __r {{}};"
        f" lappend __r name {tcl_brace(db_name)};"
        f" lappend __r path {tcl_brace(abs_path)};"
        f" if {{![catch {{database get -using {tcl_brace(db_name)} -limits}} __lim]}} {{"
        f"   lappend __r start [lindex $__lim 0] end [lindex $__lim 1]"
        f" }};"
        f" lappend __r scope_count"
        f" [llength [::mcp::walk_hierarchy {tcl_brace(db_name + '::')} scope {{}}]];"
        f" lappend __r signal_count"
        f" [llength [::mcp::walk_hierarchy {tcl_brace(db_name + '::')} signal {{}}]];"
        f" set __r"
    )
    raw = await client.send(summary_tcl)
    parts = parse_tcl_list(raw)
    info: dict = dict(zip(parts[0::2], parts[1::2]))
    for k in ("scope_count", "signal_count"):
        if k in info:
            try:
                info[k] = int(info[k])
            except (TypeError, ValueError):
                pass
    # Normalize SimVision's native time format ("0(0)ns") into scalar + unit
    # pairs so the agent can do math without a parser.
    for bound in ("start", "end"):
        if bound in info:
            val, unit = _parse_sv_time(info[bound])
            if val is not None:
                info[bound] = val
                info.setdefault("time_unit", unit)
    return json.dumps(info, indent=2)


@mcp.tool()
async def close_database(name: str, session: str | None = None) -> str:
    """Close a simulation database by name."""
    return await (await _sv(session)).send(f"database close {tcl_brace(name)}")


# -----------------------------------------------------------------------------
# Scopes / signals discovery
# -----------------------------------------------------------------------------

@mcp.tool()
async def scope(new: str | None = None, session: str | None = None) -> str:
    """Get or set the default scope prefix for resolving bare signal names.

    - `new` omitted → returns the current scope's full path (e.g. `"::"` for
      the root, `"waves::top.dut"` for a specific module).
    - `new` given → sets it; subsequent tools can use bare signal names.

    e.g. `scope("waves::top.dut")` lets `add_signals(signals=["clk"])` work
    without the full `waves:::top.dut.clk` prefix.
    """
    client = await _sv(session)
    if new is None:
        return await client.send("scope get -long")
    return await client.send(f"scope set {tcl_brace(new)}")


@mcp.tool()
async def list_scopes(
    parent: str = "",
    pattern: str = "*",
    session: str | None = None,
) -> str:
    """List scope (module/instance) names in the design hierarchy.

    Args:
        parent: Full name of the parent scope to recurse into
            (e.g. `"tiny::"` for everything under database "tiny",
            or `"tiny::tb"` for a specific module). Empty = all open databases.
        pattern: Tcl glob filter applied to full names (`*`, `?`, `[abc]`).
            Default `"*"` returns everything under `parent`.

    Walks the hierarchy recursively via `scope show`, so no Design Browser
    window is required. Returns a JSON array of full scope names.
    """
    return await _send_as_json_list(
        f"::mcp::walk_hierarchy {tcl_brace(parent)} scope {tcl_brace(pattern)}",
        session,
    )


@mcp.tool()
async def list_signals(
    parent: str = "",
    pattern: str = "*",
    session: str | None = None,
) -> str:
    """List signal names in the design hierarchy.

    Args:
        parent: Full name of the parent scope to recurse into
            (e.g. `"tiny::tb"`). Empty = all open databases.
        pattern: Tcl glob filter applied to full names.

    Walks the hierarchy recursively via `scope show`. Returns a JSON array of
    full signal names. Multi-bit buses are returned as a single entry
    (e.g. `tiny::tb.counter`), not enumerated into bit slices.
    """
    return await _send_as_json_list(
        f"::mcp::walk_hierarchy {tcl_brace(parent)} signal {tcl_brace(pattern)}",
        session,
    )


# -----------------------------------------------------------------------------
# Waveform windows and signals
# -----------------------------------------------------------------------------

@mcp.tool()
async def new_waveform_window(
    name: str | None = None, session: str | None = None,
) -> str:
    """Create a new Waveform window. Returns the window name."""
    cmd = "waveform new"
    if name:
        cmd += f" -name {tcl_brace(name)}"
    return await (await _sv(session)).send(cmd)


@mcp.tool()
async def add_signals(
    signals: list[str],
    scope: str | None = None,
    window: str | None = None,
    session: str | None = None,
) -> str:
    """Add one or more signals to a Waveform window.

    Signal names may be fully qualified (`waves:::top.dut.clk`) or relative
    when `scope` is provided: `add_signals(scope="waves:::top.dut",
    signals=["clk", "counter"])` is equivalent to the fully-qualified form.

    Accepts both `db::path` (scope form) and `db:::path` (signal form) in
    inputs — if a signal doesn't resolve with only 2 colons, the path is
    auto-normalized to 3 colons (which SimVision needs for signal refs).

    Returns a JSON array of the qualified signal paths that were added.
    """
    qualified: list[str] = []
    for s in signals:
        full = f"{scope}.{s}" if scope else s
        qualified.append(_normalize_signal_path(full))
    client = await _sv(session)
    if window is None:
        await _ensure_waveform_window(client)
    await client.send(
        f"waveform add{_using(window)} -signals {tcl_list(qualified)}"
    )
    return json.dumps(qualified)


@mcp.tool()
async def add_signals_matching(
    pattern: str = "*",
    parent: str = "",
    window: str | None = None,
    session: str | None = None,
) -> str:
    """Search the design hierarchy and add every matching signal to a Waveform.

    Combines `list_signals(parent, pattern)` with `add_signals(...)` in one
    call. Useful for adding all signals in a scope, or every signal matching
    a glob (`*_en`, `*clk*`, etc.).

    Args:
        pattern: Tcl glob. Default `"*"` matches everything under `parent`.
        parent: Scope to search under (e.g. `"tiny::tb"`). Empty = all
            open databases.
        window: Target waveform window.

    Returns a JSON array of the signals that were added.
    """
    client = await _sv(session)
    raw = await client.send(
        f"::mcp::walk_hierarchy {tcl_brace(parent)} signal {tcl_brace(pattern)}"
    )
    names = parse_tcl_list(raw)
    if not names:
        return "[]"
    qualified = [_normalize_signal_path(n) for n in names]
    if window is None:
        await _ensure_waveform_window(client)
    await client.send(
        f"waveform add{_using(window)} -signals {tcl_list(qualified)}"
    )
    return json.dumps(qualified)


@mcp.tool()
async def clear_waveform(
    window: str | None = None, session: str | None = None,
) -> str:
    """Remove all signals from a Waveform window."""
    return await (await _sv(session)).send(f"waveform clearall{_using(window)}")


@mcp.tool()
async def viewport(
    min_time: str | None = None,
    max_time: str | None = None,
    window: str | None = None,
    session: str | None = None,
) -> str:
    """Get, set, or zoom-to-full a Waveform window's visible time range.

    Modes, selected by argument pattern:
      - both `min_time` and `max_time` omitted → returns the current viewport
        as `"<min> <max>"`.
      - `min_time="full"` (sentinel, case-insensitive) → zoom to the full
        simulation range.
      - both set → set the visible range.

    Args:
        min_time: e.g. `"0"` or `"100ns"`, or `"full"`.
        max_time: e.g. `"1000ns"`. Ignored when `min_time="full"`.
    """
    wu = _using(window)
    if min_time is None and max_time is None:
        return await (await _sv(session)).send(f"waveform xview limits{wu}")
    if isinstance(min_time, str) and min_time.lower() == "full":
        # Tcl: `waveform xview zoom -outfull` fits all data in the window.
        return await (await _sv(session)).send(f"waveform xview zoom{wu} -outfull")
    if min_time is None or max_time is None:
        raise SimVisionError(
            "viewport: pass both min_time and max_time to set a range, "
            "or neither to query, or min_time='full' to zoom to full."
        )
    return await (await _sv(session)).send(
        f"waveform xview limits{wu} {tcl_brace(min_time)} {tcl_brace(max_time)}"
    )


@mcp.tool()
async def signal_values(
    time: str,
    signals: list[str] | None = None,
    window: str | None = None,
    session: str | None = None,
) -> str:
    """Read signal values at a specific simulation time.

    Returns a JSON object mapping signal name → value string at `time`:

        {"tiny:::tb.clk": "1", "tiny:::tb.counter": "3", "tiny:::tb.valid": "1"}

    Values are in whatever radix SimVision is displaying (set per-signal
    with `tcl_eval "waveform format -radix hex|decimal|..."`).

    Args:
        time: Simulation time, e.g. `"50ns"`, `"1.5us"`, `"1200"`.
        signals: Signal paths. Auto-normalizes `db::path` → `db:::path`. If
            omitted, reads all signals currently in the waveform window;
            keys are then the bare display names (e.g. `"clk"`, not
            `"tiny:::tb.clk"`) because SimVision reports them that way.
        window: Waveform window name. Defaults to the current one.
    """
    wu = _using(window)
    client = await _sv(session)
    if signals:
        normalized = [_normalize_signal_path(s) for s in signals]
        # Wrap the list in [list ...] so the substitution is always a single
        # list argument — `foreach` interprets a bare word list as multi-var
        # pairs, which would mis-parse three signals as (var=a, list=b, body=c).
        sig_literal = f"[list {tcl_list(normalized)}]"
        tcl = (
            f"set __out {{}};"
            f" foreach __s {sig_literal} {{"
            f"   lappend __out $__s [waveform values{wu} -at {tcl_brace(time)} $__s]"
            f" }};"
            f" set __out"
        )
        raw = await client.send(tcl)
        parts = parse_tcl_list(raw)
        pairs = dict(zip(parts[0::2], parts[1::2]))
    else:
        # `waveform values -at <t>` with no signal IDs returns values for every
        # displayed signal in window order. `waveform signals` returns the
        # display names in the same order. Zip them.
        names_raw = await client.send(f"waveform signals{wu}")
        values_raw = await client.send(
            f"waveform values{wu} -at {tcl_brace(time)}"
        )
        names = parse_tcl_list(names_raw)
        values = parse_tcl_list(values_raw)
        pairs = dict(zip(names, values))
    return json.dumps(pairs, indent=2)


# -----------------------------------------------------------------------------
# Cursors and markers
# -----------------------------------------------------------------------------

@mcp.tool()
async def set_cursor(
    time: str, cursor: str | None = None, session: str | None = None,
) -> str:
    """Place a cursor at a specific simulation time. Creates one if needed.

    Args:
        time: Time value with units, e.g. `"400ns"`, `"1.5us"`, `"1200"` (seconds).
        cursor: Cursor name. If given and no such cursor exists, creates one
            with that name. If omitted, reuses the first existing cursor or
            creates SimVision's default (`"TimeA"`) if none exist.

    Returns the cursor name (so you can refer to it later without guessing).
    """
    t = tcl_brace(time)
    if cursor:
        name = tcl_brace(cursor)
        # Create by name if not present, else move it.
        cmd = (
            f"if {{[lsearch -exact [cursor find] {name}] < 0}}"
            f" {{set __r [cursor new -name {name} -time {t}]}}"
            f" else {{cursor set -using {name} -time {t}; set __r {name}}};"
            f" set __r"
        )
    else:
        cmd = (
            f"set __cur [lindex [cursor find] 0];"
            f" if {{$__cur eq \"\"}} {{set __r [cursor new -time {t}]}}"
            f" else {{cursor set -using $__cur -time {t}; set __r $__cur}};"
            f" set __r"
        )
    return await (await _sv(session)).send(cmd)


@mcp.tool()
async def delete_cursor(name: str, session: str | None = None) -> str:
    """Delete a cursor by name."""
    return await (await _sv(session)).send(f"cursor delete {tcl_brace(name)}")


@mcp.tool()
async def new_marker(
    time: str,
    name: str | None = None,
    color: str | None = None,
    session: str | None = None,
) -> str:
    """Add a named time marker. Returns the marker name."""
    cmd = f"marker new -time {tcl_brace(time)}"
    if name:
        cmd += f" -name {tcl_brace(name)}"
    if color:
        cmd += f" -color {tcl_brace(color)}"
    return await (await _sv(session)).send(cmd)


@mcp.tool()
async def delete_marker(name: str, session: str | None = None) -> str:
    """Delete a marker by name."""
    return await (await _sv(session)).send(f"marker delete {tcl_brace(name)}")


# -----------------------------------------------------------------------------
# Simulation control (simulation mode only)
# -----------------------------------------------------------------------------

@mcp.tool()
async def sim_run(
    time: str | None = None, session: str | None = None,
) -> str:
    """Run the simulation (simulation mode only).

    Args:
        time: Optional "run until" time, e.g. `"1000ns"`. If omitted, runs until
              a stop condition or end of simulation.
    """
    cmd = "simcontrol run"
    if time:
        cmd += f" -time {tcl_brace(time)}"
    return await (await _sv(session)).send(cmd)


@mcp.tool()
async def sim_stop(session: str | None = None) -> str:
    """Interrupt a running simulation."""
    return await (await _sv(session)).send("simcontrol run -stop")


@mcp.tool()
async def sim_step(into: bool = False, session: str | None = None) -> str:
    """Single-step the simulation. `into=True` steps into subprogram calls; default steps over."""
    return await (await _sv(session)).send(
        "simcontrol run -step" if into else "simcontrol run -next"
    )


@mcp.tool()
async def sim_reset(session: str | None = None) -> str:
    """Reset the simulation back to time 0, preserving breakpoints and probes."""
    return await (await _sv(session)).send("simcontrol run -reset")


@mcp.tool()
async def sim_showvalue(names: list[str], session: str | None = None) -> str:
    """Print the current simulator values of the named signals/objects to the Console tab."""
    return await (await _sv(session)).send(
        f"simcontrol showvalue {' '.join(tcl_brace(n) for n in names)}"
    )


# -----------------------------------------------------------------------------
# Screenshots
# -----------------------------------------------------------------------------

def _rotate_image_90_cw(path: str) -> None:
    """Rotate an image file 90° clockwise in place.

    Only touches PNG/JPG — leaves PS/PDF alone since they have their own
    orientation metadata. Silently no-ops if Pillow isn't installed.
    """
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext not in ("png", "jpg", "jpeg"):
        return
    try:
        from PIL import Image as _PIL
    except ImportError:
        return
    with _PIL.open(path) as img:
        # PIL's `rotate` is CCW-positive; -90° is one step clockwise.
        img.rotate(-90, expand=True).save(path)


def _rasterize_postscript(ps_path: str, output_path: str) -> str:
    """Convert a PostScript file to PNG/PDF/JPG based on output_path's extension.

    Prefers Ghostscript (`gs`), falls back to `ps2pdf` for PDFs or ImageMagick
    `convert`. Returns the output path on success, or a string starting with
    "Error:" on failure. If the caller requests a .ps extension, the PS file
    is simply copied to the destination.

    This is a pure filesystem function — no SimVision client needed — so tests
    can exercise it directly with a hand-crafted PS file.
    """
    import subprocess
    import shutil as _sh

    abs_out = os.path.abspath(output_path)
    ext = abs_out.rsplit(".", 1)[-1].lower() if "." in abs_out else ""

    if not os.path.isfile(ps_path):
        return f"Error: no PostScript at {ps_path}"

    if ext == "ps":
        _sh.copy(ps_path, abs_out)
        return abs_out

    gs = shutil.which("gs")
    convert = shutil.which("convert") or shutil.which("magick")

    if ext == "pdf" and shutil.which("ps2pdf"):
        r = subprocess.run(
            ["ps2pdf", ps_path, abs_out],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and os.path.isfile(abs_out):
            return abs_out

    if gs and ext in ("png", "jpg", "jpeg", "pdf"):
        device = {"png": "png16m", "jpg": "jpeg", "jpeg": "jpeg", "pdf": "pdfwrite"}[ext]
        r = subprocess.run(
            [
                gs, "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dQUIET",
                f"-sDEVICE={device}", "-r150",
                f"-sOutputFile={abs_out}", ps_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and os.path.isfile(abs_out):
            return abs_out
        return f"Error: gs failed: {(r.stderr or r.stdout).strip()}"

    if convert:
        r = subprocess.run(
            [convert, "-density", "150", ps_path, abs_out],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and os.path.isfile(abs_out):
            return abs_out
        return f"Error: convert failed: {r.stderr.strip()}"

    return "Error: no rasterizer found (install ghostscript or imagemagick)"


def _inline_image_result(path: str, summary: str) -> list:
    """Return a tool result that both describes and inlines a PNG/JPG image.

    Text content gives the saved path (useful for later re-use); image content
    delivers the pixels directly to the agent's context so it doesn't need a
    separate Read call. Non-image formats (.ps, .pdf) are returned as text only.
    """
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in ("png", "jpg", "jpeg"):
        with open(path, "rb") as f:
            data = f.read()
        fmt = "png" if ext == "png" else "jpeg"
        return [summary, Image(data=data, format=fmt)]
    return [summary]


@mcp.tool()
async def screenshot_waveform(
    output_path: str, window: str | None = None, session: str | None = None,
):
    """Capture a PNG/PDF screenshot of a Waveform window's contents.

    Pipeline: SimVision's `waveform print -file tmp.ps` (the only documented
    capture path), then Ghostscript (`gs`) or ImageMagick (`convert`) to
    rasterize. Falls back to saving the raw `.ps` if neither is installed.

    For PNG/JPG outputs, the image is also returned inline so the agent can
    see the waveform directly without a separate Read call.

    Args:
        output_path: Output file; extension drives the format. Supported:
            `.png`, `.jpg`, `.pdf`, `.ps`. Absolute path recommended.
        window: Name of the Waveform window. Defaults to the current one.
    """
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".ps", delete=False) as tf:
        ps_path = tf.name

    try:
        await (await _sv(session)).send(
            f"waveform print{_using(window)} -file {tcl_brace(ps_path)}"
            f" -orientation landscape"
        )
        if not os.path.isfile(ps_path):
            return f"Error: SimVision did not create {ps_path}"
        result = _rasterize_postscript(ps_path, output_path)
        if result.startswith("Error:"):
            return result
        # SimVision's waveform PS sets `%%Orientation: Landscape` but writes
        # a portrait bounding box with pre-rotated content (time axis flowing
        # top→bottom, later times at the bottom). Ghostscript renders that
        # portrait faithfully, so we rotate 90° CW to restore the natural
        # left→right time reading direction.
        _rotate_image_90_cw(result)
        return _inline_image_result(result, f"Saved waveform screenshot to {result}")
    finally:
        if os.path.exists(ps_path):
            try:
                os.unlink(ps_path)
            except OSError:
                pass


def _walk_x_tree(root, pid_filter: int | None = None):
    """Yield every X window under `root`, optionally filtered by pid (`_NET_WM_PID`)."""
    from Xlib import X
    stack = [root]
    while stack:
        win = stack.pop()
        yield win
        try:
            children = win.query_tree().children
        except Exception:
            continue
        stack.extend(children)


def _window_is_viewable(win) -> bool:
    """True for mapped, viewable windows. Filters out unmapped/icon windows."""
    from Xlib import X
    try:
        attrs = win.get_attributes()
        return attrs.map_state == X.IsViewable
    except Exception:
        return False


def _window_title(win) -> str:
    """Best-effort X window title (_NET_WM_NAME → WM_NAME). Empty string on failure."""
    try:
        net = win.get_full_property(
            win.display.intern_atom("_NET_WM_NAME"),
            win.display.intern_atom("UTF8_STRING"),
        )
        if net and net.value:
            return net.value.decode("utf-8", "replace")
    except Exception:
        pass
    try:
        wm = win.get_wm_name()
        if isinstance(wm, bytes):
            wm = wm.decode("utf-8", "replace")
        return wm or ""
    except Exception:
        return ""


def _window_pid(win) -> int | None:
    try:
        pid_prop = win.get_full_property(
            win.display.intern_atom("_NET_WM_PID"), 0
        )
        if pid_prop and pid_prop.value:
            return int(pid_prop.value[0])
    except Exception:
        pass
    return None


def _open_x_display(display_name: str):
    from Xlib import display
    return display.Display(display_name)


@mcp.tool()
async def list_sim_windows(session: str | None = None) -> str:
    """List all X11 window titles belonging to this SimVision session.

    Useful for picking a target for `screenshot_gui`. Returns one title per line.
    """
    try:
        import Xlib  # noqa: F401
    except ImportError:
        return "Error: python-xlib not installed (pip install python-xlib)"
    client = await _sv(session)
    if client.display is None:
        return "Error: session has no display"

    d = _open_x_display(client.display)
    try:
        root = d.screen().root
        titles: list[str] = []
        for win in _walk_x_tree(root):
            if not _window_is_viewable(win):
                continue
            pid = _window_pid(win)
            if client.pid is not None and pid is not None and pid != client.pid:
                continue
            title = _window_title(win)
            if title:
                titles.append(title)
        return "\n".join(sorted(set(titles)))
    finally:
        d.close()


def _grab_window_png(display_name: str, window_id: int | None, output_path: str) -> str:
    """Grab a screenshot of either the root or a specific X window, via mss.

    For a specific window we translate its X coordinates to root-relative and
    grab that rectangle from the root; this sidesteps the problem where
    `XGetImage` on an occluded or non-redirected window returns black.

    Returns the output path on success or "Error: …" on failure.
    """
    try:
        import mss
        import mss.tools
    except ImportError:
        return "Error: mss not installed (pip install mss)"

    abs_out = os.path.abspath(output_path)

    # Determine the bbox to grab.
    if window_id is None:
        bbox = None  # full monitor
    else:
        try:
            from Xlib import X  # noqa: F401
        except ImportError:
            return "Error: python-xlib not installed"
        d = _open_x_display(display_name)
        try:
            win = d.create_resource_object("window", window_id)
            geom = win.get_geometry()
            # Translate (0,0) of win to root coordinates. Xlib's translate_coords
            # returns (child_window, x, y) where x,y are in the target (root) frame.
            root = d.screen().root
            xlated = win.translate_coords(root, 0, 0)
            rx, ry = xlated.x, xlated.y
            bbox = {"left": rx, "top": ry, "width": geom.width, "height": geom.height}
        finally:
            d.close()

    orig = os.environ.get("DISPLAY")
    os.environ["DISPLAY"] = display_name
    try:
        with mss.MSS() as sct:
            sct_img = sct.grab(bbox) if bbox else sct.grab(sct.monitors[1])
            mss.tools.to_png(sct_img.rgb, sct_img.size, output=abs_out)
    finally:
        if orig is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = orig
    return abs_out


@mcp.tool()
async def screenshot_gui(
    output_path: str,
    window_title: str | None = None,
    session: str | None = None,
):
    """Capture a PNG of the whole SimVision GUI, or one X window by title.

    Uses pure-Python X libraries (`mss` for root grabs, `python-xlib` + Pillow
    for per-window grabs) — no external binaries required. The image is also
    returned inline so the agent sees it directly without a separate Read.

    Args:
        output_path: Where to save the PNG (.png).
        window_title: Substring (case-insensitive) of an X window title. If
            given, only that window is captured. Errors if 0 or >1 match. If
            omitted, the entire X root is captured — ideal under a headless
            session, where the root contains just SimVision.
        session: Target session.
    """
    client = await _sv(session)
    if client.display is None:
        return "Error: session has no display"
    abs_out = os.path.abspath(output_path)

    target_window_id: int | None = None
    if window_title:
        try:
            import Xlib  # noqa: F401
        except ImportError:
            return "Error: python-xlib not installed (pip install python-xlib)"
        d = _open_x_display(client.display)
        try:
            root = d.screen().root
            needle = window_title.lower()
            viewable: list[tuple[int, str]] = []
            hidden: list[str] = []
            for win in _walk_x_tree(root):
                pid = _window_pid(win)
                if client.pid is not None and pid is not None and pid != client.pid:
                    continue
                title = _window_title(win)
                if not (title and needle in title.lower()):
                    continue
                if _window_is_viewable(win):
                    viewable.append((win.id, title))
                else:
                    hidden.append(title)
            if not viewable:
                # Include the list of available viewable titles so the caller
                # can retry in the same turn.
                all_viewable: list[str] = []
                for win in _walk_x_tree(root):
                    if not _window_is_viewable(win):
                        continue
                    pid = _window_pid(win)
                    if client.pid is not None and pid is not None and pid != client.pid:
                        continue
                    t = _window_title(win)
                    if t:
                        all_viewable.append(t)
                available = sorted(set(all_viewable))
                if hidden:
                    return (
                        f"Error: {len(hidden)} window(s) match {window_title!r} "
                        f"but none are currently viewable (all minimized/unmapped): "
                        f"{sorted(set(hidden))}. Viewable titles: {available}"
                    )
                return (
                    f"Error: no window matching {window_title!r}. "
                    f"Viewable titles on this display: {available}"
                )
            if len({title for _, title in viewable}) > 1:
                return (
                    f"Error: multiple viewable windows match {window_title!r}: "
                    f"{[t for _, t in viewable]}. Be more specific."
                )
            target_window_id = viewable[0][0]
        finally:
            d.close()

    result = _grab_window_png(client.display, target_window_id, abs_out)
    if result.startswith("Error:"):
        return result
    return _inline_image_result(result, f"Saved GUI screenshot to {result}")


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def _shutdown_all_sessions() -> None:
    """Kill every SimVision + Xvfb subprocess we spawned, synchronously.

    Called from atexit and SIGTERM/SIGINT handlers so the MCP host exiting
    doesn't leave orphans. We use the subprocess handles directly rather
    than `client.stop()` (which is async) — at teardown we just want dead
    children, not graceful Tcl `exit` negotiation.
    """
    import subprocess as _sp
    for name, c in list(_sessions.items()):
        try:
            if c._process is not None and c._process.poll() is None:
                c._process.terminate()
                try:
                    c._process.wait(timeout=2)
                except _sp.TimeoutExpired:
                    c._process.kill()
            if c._xvfb_process is not None and c._xvfb_process.poll() is None:
                c._xvfb_process.terminate()
                try:
                    c._xvfb_process.wait(timeout=2)
                except _sp.TimeoutExpired:
                    c._xvfb_process.kill()
        except Exception as e:
            logger.warning("failed to cleanup session %r: %s", name, e)
    _sessions.clear()


def _install_cleanup_handlers() -> None:
    import atexit
    import signal as _signal

    atexit.register(_shutdown_all_sessions)

    def _on_signal(signum, frame):
        _shutdown_all_sessions()
        # Re-raise the default to actually exit.
        _signal.signal(signum, _signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (_signal.SIGTERM, _signal.SIGINT, _signal.SIGHUP):
        try:
            _signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            # SIGHUP doesn't exist on Windows etc.
            pass


def main():
    logging.basicConfig(level=logging.INFO)
    _install_cleanup_handlers()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
