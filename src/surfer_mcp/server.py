"""MCP server exposing Surfer waveform viewer controls as tools."""

from __future__ import annotations

import json
import logging
import os

from mcp.server.fastmcp import FastMCP

from surfer_mcp.wcp import WcpClient

logger = logging.getLogger(__name__)

mcp = FastMCP("surfer")

# Module-level singleton — avoids reliance on ctx.request_context
_client = WcpClient()


async def _wcp() -> WcpClient:
    if not _client.running:
        await _client.start()
    return _client


def _fmt(resp: dict) -> str:
    """Format a WCP response for the user."""
    # Response fields vary — return the whole thing minus "type"/"command" boilerplate
    out = {k: v for k, v in resp.items() if k not in ("type", "command")}
    if not out:
        return "OK"
    if len(out) == 1:
        val = next(iter(out.values()))
        if isinstance(val, (dict, list)):
            return json.dumps(val, indent=2)
        return str(val)
    return json.dumps(out, indent=2)


@mcp.tool()
async def load_waveform(path: str) -> str:
    """Load a waveform file (VCD, FST, GHW, etc.) into Surfer. Use an absolute path."""
    abs_path = os.path.abspath(path)
    if not os.path.isfile(abs_path):
        return f"Error: file not found: {abs_path}"
    resp = await (await _wcp()).send_command("load", wait_event="waveforms_loaded", source=abs_path)
    return f"Loaded {abs_path}"


@mcp.tool()
async def reload_waveform() -> str:
    """Reload the currently loaded waveform file."""
    resp = await (await _wcp()).send_command("reload", wait_event="waveforms_loaded")
    return _fmt(resp)


@mcp.tool()
async def add_signals(variables: list[str]) -> str:
    """Add signals/variables to the waveform viewer by their hierarchical names."""
    resp = await (await _wcp()).send_command("add_variables", variables=variables)
    return _fmt(resp)


@mcp.tool()
async def add_scope(scope: str, recursive: bool = False) -> str:
    """Add all signals in a scope to the viewer. Set recursive=True to include sub-scopes."""
    resp = await (await _wcp()).send_command("add_scope", scope=scope, recursive=recursive)
    return _fmt(resp)


@mcp.tool()
async def add_items(items: list[str], recursive: bool = False) -> str:
    """Add items (signal paths) to the viewer."""
    resp = await (await _wcp()).send_command("add_items", items=items, recursive=recursive)
    return _fmt(resp)


@mcp.tool()
async def remove_items(ids: list[int]) -> str:
    """Remove items from the waveform viewer by their IDs."""
    resp = await (await _wcp()).send_command("remove_items", ids=ids)
    return _fmt(resp)


@mcp.tool()
async def get_item_list() -> str:
    """Get the list of item IDs currently displayed in the waveform viewer."""
    resp = await (await _wcp()).send_command("get_item_list")
    return _fmt(resp)


@mcp.tool()
async def get_item_info(ids: list[int]) -> str:
    """Get detailed information about items in the viewer by their IDs."""
    resp = await (await _wcp()).send_command("get_item_info", ids=ids)
    return _fmt(resp)


@mcp.tool()
async def set_cursor(timestamp: int) -> str:
    """Set the cursor position to a specific timestamp (in simulation units)."""
    resp = await (await _wcp()).send_command("set_cursor", timestamp=timestamp)
    return _fmt(resp)


@mcp.tool()
async def set_viewport_range(start: int, end: int) -> str:
    """Set the visible time range in the waveform viewer (timestamps in simulation units)."""
    resp = await (await _wcp()).send_command("set_viewport_range", start=start, end=end)
    return _fmt(resp)


@mcp.tool()
async def set_viewport_to(timestamp: int) -> str:
    """Center the viewport on a specific timestamp (in simulation units)."""
    resp = await (await _wcp()).send_command("set_viewport_to", timestamp=timestamp)
    return _fmt(resp)


@mcp.tool()
async def zoom_to_fit(viewport_idx: int = 0) -> str:
    """Zoom the viewport to fit all waveform data. viewport_idx defaults to 0."""
    resp = await (await _wcp()).send_command("zoom_to_fit", viewport_idx=viewport_idx)
    return _fmt(resp)


@mcp.tool()
async def add_markers(markers: list[dict]) -> str:
    """Add time markers to the waveform viewer.

    Each marker is a dict with:
      - 'time' (numeric, required): timestamp in simulation time units
      - 'name' (string, optional): label for the marker
      - 'move_focus' (bool, optional, default false): whether to move focus to the marker
    """
    # Surfer requires move_focus to be present in every marker object
    sanitized = []
    for m in markers:
        entry = dict(m)
        entry.setdefault("move_focus", False)
        sanitized.append(entry)
    resp = await (await _wcp()).send_command("add_markers", markers=sanitized)
    return _fmt(resp)


@mcp.tool()
async def focus_item(id: int) -> str:
    """Focus/scroll to a specific item in the waveform viewer by its ID."""
    resp = await (await _wcp()).send_command("focus_item", id=id)
    return _fmt(resp)


@mcp.tool()
async def set_item_color(id: int, color: str) -> str:
    """Set the display color of an item. Color can be a name or hex value."""
    resp = await (await _wcp()).send_command("set_item_color", id=id, color=color)
    return _fmt(resp)


@mcp.tool()
async def clear() -> str:
    """Clear all items from the waveform viewer."""
    resp = await (await _wcp()).send_command("clear")
    return _fmt(resp)


@mcp.tool()
async def get_signal_values(path: str, signals: list[str], timestamp: int) -> str:
    """Read signal values at a specific timestamp directly from a waveform file.

    Args:
        path: Path to the waveform file (FST, VCD, GHW).
        signals: List of hierarchical signal names to query.
        timestamp: The simulation time to read values at.

    Returns a JSON object mapping each signal name to its value at the given time.
    Also moves the Surfer cursor to the queried timestamp.
    """
    from pywellen import Waveform

    # Move cursor in Surfer if connected
    try:
        client = await _wcp()
        await client.send_command("set_cursor", timestamp=timestamp)
    except Exception:
        pass  # Don't fail the read if Surfer isn't running

    abs_path = os.path.abspath(path)
    waveform = Waveform(abs_path)
    results = {}
    for sig_name in signals:
        try:
            signal = waveform.get_signal_from_path(sig_name)
            val = signal.value_at_time(timestamp)
            results[sig_name] = str(val)
        except Exception as e:
            results[sig_name] = f"error: {e}"
    return json.dumps(results, indent=2)


def _find_edge(path: str, signal_name: str, after: int, edge: str) -> dict:
    """Find the next rising or falling edge of a signal after a given timestamp."""
    from pywellen import Waveform

    abs_path = os.path.abspath(path)
    waveform = Waveform(abs_path)
    signal = waveform.get_signal_from_path(signal_name)

    prev_val = None
    for time, val in signal.all_changes():
        val_str = str(val)
        if time > after and prev_val is not None:
            is_rising = prev_val == "0" and val_str == "1"
            is_falling = prev_val == "1" and val_str == "0"
            if (edge == "rising" and is_rising) or (edge == "falling" and is_falling):
                return {"timestamp": time, "signal": signal_name, "edge": edge, "value": val_str}
        prev_val = val_str

    return {"error": f"No {edge} edge found for '{signal_name}' after t={after}"}


@mcp.tool()
async def find_rising_edge(path: str, signal: str, after: int) -> str:
    """Find the next rising edge (0->1) of a signal after a given timestamp.

    Args:
        path: Path to the waveform file.
        signal: Hierarchical signal name (must be a 1-bit signal).
        after: Timestamp to search from.

    Returns the timestamp of the next rising edge. Also moves the Surfer cursor there.
    """
    result = _find_edge(path, signal, after, "rising")
    if "timestamp" in result:
        try:
            client = await _wcp()
            await client.send_command("set_cursor", timestamp=result["timestamp"])
        except Exception:
            pass
    return json.dumps(result, indent=2)


@mcp.tool()
async def find_falling_edge(path: str, signal: str, after: int) -> str:
    """Find the next falling edge (1->0) of a signal after a given timestamp.

    Args:
        path: Path to the waveform file.
        signal: Hierarchical signal name (must be a 1-bit signal).
        after: Timestamp to search from.

    Returns the timestamp of the next falling edge. Also moves the Surfer cursor there.
    """
    result = _find_edge(path, signal, after, "falling")
    if "timestamp" in result:
        try:
            client = await _wcp()
            await client.send_command("set_cursor", timestamp=result["timestamp"])
        except Exception:
            pass
    return json.dumps(result, indent=2)


def _open_waveform(path: str):
    """Open a waveform file, returning the Waveform object."""
    from pywellen import Waveform
    return Waveform(os.path.abspath(path))


def _collect_scopes(scope, hier, prefix=""):
    """Recursively collect all scope full names."""
    results = []
    name = scope.full_name(hier)
    results.append(name)
    for child in scope.scopes(hier):
        results.extend(_collect_scopes(child, hier))
    return results


@mcp.tool()
async def list_scopes(path: str) -> str:
    """List all scopes (modules/instances) in the design hierarchy.

    Args:
        path: Path to the waveform file (FST, VCD, GHW).

    Returns a list of scope names.
    """
    waveform = _open_waveform(path)
    hier = waveform.hierarchy
    scopes = []
    for top in hier.top_scopes():
        scopes.extend(_collect_scopes(top, hier))
    return json.dumps(scopes, indent=2)


@mcp.tool()
async def list_signals(path: str, scope: str) -> str:
    """List all signals within a specific scope (direct children only).

    Args:
        path: Path to the waveform file.
        scope: Hierarchical scope name (e.g. 'top.cpu.alu').

    Returns a list of signal names within that scope.
    """
    waveform = _open_waveform(path)
    hier = waveform.hierarchy
    signals = []
    for var in hier.all_vars():
        var_path = var.full_name(hier)
        if var_path.startswith(scope + "."):
            remainder = var_path[len(scope) + 1:]
            if "." not in remainder:
                signals.append(var_path)
    return json.dumps(signals, indent=2)


@mcp.tool()
async def get_time_range(path: str) -> str:
    """Get the simulation time range and timescale of a waveform file.

    Args:
        path: Path to the waveform file.

    Returns the first and last timestamp and the timescale.
    """
    waveform = _open_waveform(path)
    hier = waveform.hierarchy
    # Get time range from a signal's changes since TimeTable isn't directly indexable
    first_var = next(iter(hier.all_vars()), None)
    if first_var is None:
        return json.dumps({"error": "No signals in waveform"})
    sig = waveform.get_signal(first_var)
    changes = list(sig.all_changes())
    if not changes:
        return json.dumps({"error": "No transitions in waveform"})
    return json.dumps({
        "start": int(changes[0][0]),
        "end": int(changes[-1][0]),
        "timescale": str(hier.timescale()),
    }, indent=2)


@mcp.tool()
async def find_value(path: str, signal: str, value: str, after: int) -> str:
    """Find the next timestamp where a signal equals a specific value.

    Args:
        path: Path to the waveform file.
        signal: Hierarchical signal name.
        value: The value to search for (e.g. '1', '0', '101', 'ff').
        after: Timestamp to search from.

    Returns the timestamp where the signal first equals the value. Moves the Surfer cursor there.
    """
    waveform = _open_waveform(path)
    sig = waveform.get_signal_from_path(signal)

    for time, val in sig.all_changes():
        if time > after and str(val) == value:
            try:
                client = await _wcp()
                await client.send_command("set_cursor", timestamp=time)
            except Exception:
                pass
            return json.dumps({
                "timestamp": time,
                "signal": signal,
                "value": value,
            }, indent=2)

    return json.dumps({"error": f"Value '{value}' not found for '{signal}' after t={after}"})


@mcp.tool()
async def get_transitions(path: str, signal: str, start: int, end: int) -> str:
    """Get all value changes of a signal within a time window.

    Args:
        path: Path to the waveform file.
        signal: Hierarchical signal name.
        start: Start of time window.
        end: End of time window.

    Returns a list of {timestamp, value} transitions. Limited to 1000 entries.
    """
    waveform = _open_waveform(path)
    sig = waveform.get_signal_from_path(signal)

    # Include the value at the start time
    transitions = []
    try:
        val_at_start = sig.value_at_time(start)
        transitions.append({"timestamp": start, "value": str(val_at_start)})
    except Exception:
        pass

    for time, val in sig.all_changes():
        if time > end:
            break
        if time > start:
            transitions.append({"timestamp": int(time), "value": str(val)})
            if len(transitions) >= 1000:
                transitions.append({"note": "truncated at 1000 entries"})
                break

    return json.dumps(transitions, indent=2)


@mcp.tool()
async def screenshot(output_path: str) -> str:
    """Capture a screenshot of the Surfer waveform viewer window and save it as a PNG.

    Args:
        output_path: Where to save the screenshot (e.g. 'waveforms.png'). Use absolute path.

    Returns the absolute path to the saved screenshot.
    """
    import subprocess as sp

    if not _client.running:
        return "Error: Surfer is not running"

    abs_output = os.path.abspath(output_path)

    # Dial-mode (e.g. simvision-wcp): no Surfer process to capture from. Ask
    # the WCP server to render server-side and ship the bytes back.
    if _client._process is None:
        import base64
        ext = os.path.splitext(abs_output)[1].lstrip(".").lower() or "png"
        try:
            resp = await _client.send_command("screenshot", format=ext)
        except Exception as e:
            return f"Error: WCP screenshot command failed: {e}"
        data = resp.get("data")
        if not data:
            return f"Error: WCP server returned no image data: {resp}"
        with open(abs_output, "wb") as f:
            f.write(base64.b64decode(data))
        return f"Screenshot saved to {abs_output}"

    pid = _client._process.pid

    # Find the Surfer window ID via Quartz CoreGraphics
    try:
        import Quartz
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID
        )
        # Find the largest on-screen window belonging to our Surfer PID
        best_id = None
        best_area = 0
        for w in windows:
            if int(w.get("kCGWindowOwnerPID", 0)) != pid:
                continue
            if int(w.get("kCGWindowLayer", -1)) != 0:
                continue
            bounds = w.get("kCGWindowBounds", {})
            area = float(bounds.get("Width", 0)) * float(bounds.get("Height", 0))
            if area > best_area:
                best_area = area
                best_id = int(w["kCGWindowNumber"])

        if best_id is None:
            return "Error: Could not find Surfer window"

        result = sp.run(
            ["screencapture", "-l", str(best_id), "-o", abs_output],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return f"Error: screencapture failed: {result.stderr}"

    except ImportError:
        return "Error: pyobjc-framework-Quartz not installed (macOS only)"

    if not os.path.isfile(abs_output):
        return "Error: screenshot file was not created"

    return f"Screenshot saved to {abs_output}"


@mcp.tool()
async def shutdown_surfer() -> str:
    """Shut down the Surfer waveform viewer."""
    await _client.stop()
    return "Surfer shut down"


def main():
    logging.basicConfig(level=logging.INFO)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
