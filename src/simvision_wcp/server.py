"""simvision-wcp: WCP server that translates WCP commands into SimVision Tcl.

Wire format mirrors Surfer's WCP: null-terminated JSON frames over TCP.

Handshake:
    client → {"type": "greeting", "version": "0", "commands": [<events>]}
    server → {"type": "greeting", "version": "0", "commands": [<cmd names>]}

Command:
    client → {"type": "command", "command": "<name>", <fields>}
    server → {"type": "response", "command": "<name>", <fields>}
          | {"type": "error", "command": "<name>", "message": "..."}

Event (async, server → client):
    {"type": "event", "event": "<name>", <fields>}

Not every WCP command is implemented in this MVP — see SUPPORTED below.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

from simvision_mcp.client import (
    SimVisionClient,
    SimVisionError,
    tcl_brace,
    tcl_list,
    parse_tcl_list,
)

logger = logging.getLogger("simvision_wcp")


# ---------------------------------------------------------------------------
# Wire framing
# ---------------------------------------------------------------------------

def _encode_frame(obj: dict) -> bytes:
    return json.dumps(obj).encode("utf-8") + b"\0"


async def _read_frame(reader: asyncio.StreamReader) -> dict | None:
    """Read one null-terminated JSON frame. Returns None on EOF."""
    data = await reader.readuntil(b"\0")
    if not data:
        return None
    return json.loads(data[:-1].decode("utf-8"))


# ---------------------------------------------------------------------------
# Timestamp conversion
# ---------------------------------------------------------------------------
# WCP timestamps are arbitrary-precision integers in the database's native
# time unit. SimVision Tcl wants a string like "55ns" or "1200ps". We pair
# each integer with the unit we learned from `database get -limits` /
# `load_database`, falling back to whatever the user/session sets.

def _ts_to_tcl(value: int | str, unit: str) -> str:
    """BigInt (maybe str) + unit → SimVision time string."""
    if isinstance(value, str):
        # If the caller already passed a unit-bearing string, trust it.
        if any(c.isalpha() for c in value):
            return value
        value = int(value)
    return f"{value}{unit}"


# ---------------------------------------------------------------------------
# Command translator
# ---------------------------------------------------------------------------

class WcpSession:
    """Per-connection state: the WCP client's advertised event interests,
    and the WCP→SimVision handle map.
    """

    def __init__(self, sv: SimVisionClient, writer: asyncio.StreamWriter) -> None:
        self.sv = sv
        self.writer = writer
        self.client_events: set[str] = set()
        # DisplayedItemRef (int) ↔ SimVision handle ("@@N")
        self._next_ref = 0
        self._ref_to_handle: dict[int, str] = {}
        self._handle_to_ref: dict[str, int] = {}
        # Last-source cache for `reload` (WCP reload takes no args).
        self.last_source: str | None = None
        # Time unit of the most-recently loaded DB, used to convert BigInts.
        self.time_unit: str = "ns"

    # -- ref bookkeeping ------------------------------------------------

    def assign_refs(self, handles: list[str]) -> list[int]:
        refs: list[int] = []
        for h in handles:
            if h in self._handle_to_ref:
                refs.append(self._handle_to_ref[h])
                continue
            ref = self._next_ref
            self._next_ref += 1
            self._ref_to_handle[ref] = h
            self._handle_to_ref[h] = ref
            refs.append(ref)
        return refs

    def handles_from_refs(self, refs: list[int]) -> list[str]:
        out = []
        for r in refs:
            h = self._ref_to_handle.get(int(r))
            if h is None:
                raise SimVisionError(f"unknown DisplayedItemRef: {r}")
            out.append(h)
        return out

    # -- event emission -------------------------------------------------

    async def emit(self, event: str, **fields: Any) -> None:
        if event not in self.client_events:
            return  # client didn't subscribe
        frame = {"type": "event", "event": event, **fields}
        self.writer.write(_encode_frame(frame))
        await self.writer.drain()


# Supported commands in this MVP. Each entry maps a WCP command name to a
# dispatch function `async def handler(sess, msg) -> dict | None` which
# returns the response payload (everything after {type, command}).
#
# Commands NOT in this table respond with an error.

async def _handle_load(sess: WcpSession, msg: dict) -> dict:
    source = msg["source"]
    abs_path = os.path.abspath(source)
    # Force the SimVision database name to "waves" so WCP-style signal paths
    # like `waves:::tb.clk` resolve. Without this, SimVision auto-names the
    # database after the file (e.g. "tiny" for tiny.vcd) and `waves:::tb.clk`
    # references resolve to nothing — they show as "placeholder for future
    # object creation" in the rendered waveform.
    # Pre-close any existing "waves" database so reload (and back-to-back
    # loads) don't trip "database name 'waves' is already in use".
    await sess.sv.send("catch {database close waves}")
    raw = await sess.sv.send(
        f"database open -overwrite -name waves {tcl_brace(abs_path)}"
    )
    db_name = raw.strip() or "waves"
    sess.last_source = abs_path
    # Attempt to learn the time unit for future BigInt conversions.
    try:
        limits = await sess.sv.send(
            f"database get -using {tcl_brace(db_name)} -limits"
        )
        # e.g. "0(0)ns 100ns" — parse the unit off the end of the second value
        parts = parse_tcl_list(limits)
        if parts:
            import re
            m = re.search(r"([a-zA-Z]+)$", parts[-1])
            if m:
                sess.time_unit = m.group(1)
    except Exception:
        pass
    await sess.emit("waveforms_loaded", source=abs_path)
    return {}  # ack


async def _handle_reload(sess: WcpSession, msg: dict) -> dict:
    if sess.last_source is None:
        raise SimVisionError("reload before any load")
    return await _handle_load(sess, {"source": sess.last_source})


async def _ensure_waveform_window(sess: WcpSession) -> None:
    # `waveform add` errors with "no waveform window name entered" when no
    # window is current. Auto-create a "wcp" window the first time we need one.
    await sess.sv.send(
        "if {[catch {waveform using} __r]} {waveform new -name wcp}; set _ ok"
    )


async def _handle_add_variables(sess: WcpSession, msg: dict) -> dict:
    variables: list[str] = msg["variables"]
    if not variables:
        return {"ids": []}
    await _ensure_waveform_window(sess)
    # WCP signal paths: the client is responsible for correct form (db:::path).
    # We don't second-guess here — Surfer's clients pass what they read from
    # get_item_info or from the hierarchy they walked.
    raw = await sess.sv.send(
        f"waveform add -signals {tcl_list(variables)}"
    )
    handles = parse_tcl_list(raw)
    refs = sess.assign_refs(handles)
    return {"ids": refs}


async def _handle_remove_items(sess: WcpSession, msg: dict) -> dict:
    refs: list[int] = msg.get("ids", [])
    if not refs:
        return {}
    handles = sess.handles_from_refs(refs)
    # Tcl: `waveform clear signalID…` removes specific signals;
    # `waveform clearall` removes everything.
    await sess.sv.send(f"waveform clear {' '.join(handles)}")
    for h in handles:
        r = sess._handle_to_ref.pop(h, None)
        if r is not None:
            sess._ref_to_handle.pop(r, None)
    return {}


async def _handle_clear(sess: WcpSession, msg: dict) -> dict:
    await sess.sv.send("waveform clearall")
    sess._ref_to_handle.clear()
    sess._handle_to_ref.clear()
    return {}


async def _handle_set_cursor(sess: WcpSession, msg: dict) -> dict:
    ts = _ts_to_tcl(msg["timestamp"], sess.time_unit)
    # create-or-move the first cursor — mirrors simvision_mcp.server.set_cursor.
    await sess.sv.send(
        f"set __cur [lindex [cursor find] 0];"
        f" if {{$__cur eq \"\"}} {{cursor new -time {tcl_brace(ts)}}}"
        f" else {{cursor set -using $__cur -time {tcl_brace(ts)}}}"
    )
    return {}


async def _handle_set_viewport_range(sess: WcpSession, msg: dict) -> dict:
    start = _ts_to_tcl(msg["start"], sess.time_unit)
    end = _ts_to_tcl(msg["end"], sess.time_unit)
    await sess.sv.send(
        f"waveform xview limits {tcl_brace(start)} {tcl_brace(end)}"
    )
    return {}


async def _handle_zoom_to_fit(sess: WcpSession, msg: dict) -> dict:
    # viewport_idx ignored — SimVision has one viewport per waveform window.
    await sess.sv.send("waveform xview zoom -outfull")
    return {}


async def _handle_set_viewport_to(sess: WcpSession, msg: dict) -> dict:
    """Center the viewport on a timestamp."""
    ts = _ts_to_tcl(msg["timestamp"], sess.time_unit)
    await sess.sv.send(f"waveform xview see {tcl_brace(ts)}")
    return {}


async def _handle_focus_item(sess: WcpSession, msg: dict) -> dict:
    """Scroll/reveal a displayed item. WCP expects the item to already be
    in a waveform window; we select it, which SimVision auto-scrolls to."""
    handle = sess.handles_from_refs([msg["id"]])[0]
    await sess.sv.send(f"waveform select set {handle}")
    return {}


async def _handle_set_item_color(sess: WcpSession, msg: dict) -> dict:
    """Colorize a signal using SimVision's `highlight add` command."""
    handle = sess.handles_from_refs([msg["id"]])[0]
    color = msg["color"]
    await sess.sv.send(f"highlight add -color {tcl_brace(color)} {handle}")
    return {}


async def _handle_add_scope(sess: WcpSession, msg: dict) -> dict:
    """Add every signal in a scope to the current waveform window."""
    scope = msg["scope"]
    recursive = msg.get("recursive", False)
    return await _add_from_hierarchy(sess, [scope], scopes_only=False,
                                      include_scope_signals=True,
                                      recursive=recursive)


async def _handle_add_items(sess: WcpSession, msg: dict) -> dict:
    """Add mixed items (scopes or signals) to the current waveform window."""
    items = msg.get("items", [])
    recursive = msg.get("recursive", False)
    return await _add_from_hierarchy(sess, items, scopes_only=False,
                                      include_scope_signals=True,
                                      recursive=recursive)


async def _add_from_hierarchy(
    sess: WcpSession,
    items: list[str],
    *,
    scopes_only: bool,
    include_scope_signals: bool,
    recursive: bool,
) -> dict:
    """Common path for add_scope / add_items. Resolves each input into a flat
    list of signal paths via the bootstrap's ::mcp::walk_hierarchy helper.
    """
    all_sigs: list[str] = []
    for item in items:
        # If the item itself refers to a leaf signal, just add it directly.
        # If it's a scope, walk it (recursive or one level).
        raw_children = await sess.sv.send(f"scope show {tcl_brace(item)}")
        children = parse_tcl_list(raw_children)
        if not children:
            # Leaf: treat as a signal.
            all_sigs.append(item)
            continue
        # It's a scope. Walk for signals.
        if recursive:
            raw = await sess.sv.send(
                f"::mcp::walk_hierarchy {tcl_brace(item)} signal {{*}}"
            )
            all_sigs.extend(parse_tcl_list(raw))
        else:
            # Non-recursive: treat immediate signal children only.
            for c in children:
                sub_raw = await sess.sv.send(f"scope show {tcl_brace(c)}")
                sub_children = parse_tcl_list(sub_raw)
                # If c has no children (or only `c[N]` bus slices), it's a signal.
                import re
                if not sub_children or all(
                    re.match(re.escape(c) + r"\[\d+\]$", s) for s in sub_children
                ):
                    all_sigs.append(c)

    if not all_sigs:
        return {"ids": []}
    # SimVision's signal refs use 3 colons; scope paths use 2. Normalize.
    import re
    qualified = []
    for s in all_sigs:
        if ":::" in s:
            qualified.append(s)
        else:
            qualified.append(re.sub(r"(\w+)::(?!:)", r"\1:::", s, count=1))
    raw = await sess.sv.send(
        f"waveform add -signals {tcl_list(qualified)}"
    )
    handles = parse_tcl_list(raw)
    return {"ids": sess.assign_refs(handles)}


async def _handle_add_markers(sess: WcpSession, msg: dict) -> dict:
    """Batch-create markers. Returns refs for each created marker."""
    markers = msg.get("markers", [])
    names: list[str] = []
    for m in markers:
        ts = _ts_to_tcl(m["time"], sess.time_unit)
        cmd = f"marker new -time {tcl_brace(ts)}"
        if m.get("name"):
            cmd += f" -name {tcl_brace(m['name'])}"
        name = await sess.sv.send(cmd)
        names.append(name.strip())
    # Allocate WCP refs for markers (share the int-ref pool with signals —
    # they're still DisplayedItemRef).
    refs = sess.assign_refs(names)
    return {"ids": refs}


async def _handle_get_item_list(sess: WcpSession, msg: dict) -> dict:
    """Return the list of currently-displayed item refs."""
    # Return everything we've minted a ref for, in insertion order.
    return {"ids": sorted(sess._ref_to_handle.keys())}


async def _handle_get_item_info(sess: WcpSession, msg: dict) -> dict:
    """Return {name, type, id} for each requested ref."""
    refs: list[int] = msg.get("ids", [])
    handles = sess.handles_from_refs(refs)
    results = []
    for ref, handle in zip(refs, handles):
        # `waveform format -using $handle` returns a Tcl-flat list of option
        # values including `-name`. Pull specific fields.
        info_raw = await sess.sv.send(f"waveform format {handle}")
        parts = parse_tcl_list(info_raw)
        opts: dict[str, str] = {}
        i = 0
        while i < len(parts) - 1:
            key, val = parts[i], parts[i + 1]
            if key.startswith("-"):
                opts[key.lstrip("-")] = val
                i += 2
            else:
                i += 1
        name = opts.get("name", handle)
        # SimVision doesn't expose a single "type" field — infer from radix
        # + width. "signal" is WCP's catch-all; "bus" and "scope" differ.
        t = "signal"
        if opts.get("width") and opts["width"] != "1":
            t = "bus"
        results.append({"name": name, "type": t, "id": ref})
    return {"results": results}


async def _handle_screenshot(sess: WcpSession, msg: dict) -> dict:
    """Non-spec extension: render the current waveform window to PNG/JPG/PDF
    and return the bytes inline as base64.

    Pipeline: SimVision `waveform print -file <ps>` → Ghostscript → optional
    rotation. Reuses `simvision_mcp.server`'s helpers so we have one
    rasterization path, not two.
    """
    import base64
    import os
    import tempfile
    from simvision_mcp.server import rasterize_postscript, rotate_image_90_cw

    fmt = (msg.get("format") or "png").lower()
    if fmt not in ("png", "jpg", "jpeg", "pdf", "ps"):
        raise SimVisionError(f"unsupported screenshot format: {fmt!r}")

    await _ensure_waveform_window(sess)
    with tempfile.NamedTemporaryFile(suffix=".ps", delete=False) as tf:
        ps_path = tf.name
    out_path = ps_path.replace(".ps", f".{fmt}")
    try:
        await sess.sv.send(
            f"waveform print -file {tcl_brace(ps_path)} -orientation landscape"
        )
        if fmt == "ps":
            with open(ps_path, "rb") as f:
                data = f.read()
        else:
            result = rasterize_postscript(ps_path, out_path)
            if isinstance(result, str) and result.startswith("Error:"):
                raise SimVisionError(result)
            if fmt in ("png", "jpg", "jpeg"):
                rotate_image_90_cw(out_path)
            with open(out_path, "rb") as f:
                data = f.read()
        return {"format": fmt, "data": base64.b64encode(data).decode("ascii")}
    finally:
        for p in (ps_path, out_path):
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


async def _handle_shutdown(sess: WcpSession, msg: dict) -> dict:
    # Tear down SimVision; the connection will close after we return.
    try:
        await sess.sv.stop()
    except Exception as e:
        logger.warning("error during shutdown: %s", e)
    return {}


# ---------------------------------------------------------------------------
# GUI-event wiring
# ---------------------------------------------------------------------------
# The three human-click events in WCP are goto_declaration, add_drivers, and
# add_loads. We inject custom menu items into SimVision's Waveform window
# that, when clicked, call `::mcp::push_event` back to Python. The
# SimVisionClient's background reader demuxes those into its event queue, and
# _forward_events() relays them to each connected WCP client.

_GUI_EVENT_NAMES = {"goto_declaration", "add_drivers", "add_loads"}

_EVENT_MENU_TCL = r"""
# Idempotent — safe to call on every WCP client connect.
namespace eval ::wcp {}
if {[info commands ::wcp::_installed] eq ""} {
    proc ::wcp::_installed {} {}
    # %o is the object (signal) the menu fired on.
    window extensions menu create -type waveform \
        "WCP>Goto Declaration" command \
        -command {::mcp::push_event goto_declaration variable %o}
    window extensions menu create -type waveform \
        "WCP>Add Drivers" command \
        -command {::mcp::push_event add_drivers variable %o}
    window extensions menu create -type waveform \
        "WCP>Add Loads" command \
        -command {::mcp::push_event add_loads variable %o}
}
"""


async def _register_gui_event_hooks(sv: SimVisionClient, subscribed: set[str]) -> None:
    """Install custom menu items that emit WCP events when clicked.

    Only installs once per SimVision session (idempotent), and only when the
    client has asked for a human-click event that needs GUI menu integration.
    Menu items stay installed even after the WCP client disconnects; they're
    harmless if no client is listening because `::mcp::push_event` no-ops when
    the socket is gone.
    """
    if not (_GUI_EVENT_NAMES & subscribed):
        return
    try:
        await sv.send(_EVENT_MENU_TCL)
    except SimVisionError as e:
        logger.warning("failed to install WCP event menus: %s", e)


async def _forward_events(sv: SimVisionClient, sess: WcpSession) -> None:
    """Drain SimVision's async event queue and forward subscribed events."""
    while True:
        try:
            ev = await sv.next_event()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("event forward loop exiting: %s", e)
            return
        name = ev.pop("event", None)
        if not name:
            continue
        if name not in sess.client_events:
            continue  # client didn't subscribe
        try:
            sess.writer.write(_encode_frame({"type": "event", "event": name, **ev}))
            await sess.writer.drain()
        except Exception:
            return  # client gone


SUPPORTED: dict[str, Any] = {
    "load": _handle_load,
    "reload": _handle_reload,
    "add_variables": _handle_add_variables,
    "add_scope": _handle_add_scope,
    "add_items": _handle_add_items,
    "add_markers": _handle_add_markers,
    "remove_items": _handle_remove_items,
    "clear": _handle_clear,
    "set_cursor": _handle_set_cursor,
    "set_viewport_range": _handle_set_viewport_range,
    "set_viewport_to": _handle_set_viewport_to,
    "zoom_to_fit": _handle_zoom_to_fit,
    "focus_item": _handle_focus_item,
    "set_item_color": _handle_set_item_color,
    "get_item_list": _handle_get_item_list,
    "get_item_info": _handle_get_item_info,
    "shutdown": _handle_shutdown,
    # Non-spec extension: render the waveform server-side and ship bytes back.
    "screenshot": _handle_screenshot,
}


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class WcpServer:
    def __init__(
        self,
        port: int,
        *,
        headless: bool = True,
        existing_port: int | None = None,
    ) -> None:
        self.port = port
        self.headless = headless
        self.existing_port = existing_port
        self._sv: SimVisionClient | None = None

    async def _get_sv(self) -> SimVisionClient:
        if self._sv is None:
            if self.existing_port is not None:
                os.environ["SIMVISION_MCP_PORT"] = str(self.existing_port)
            self._sv = SimVisionClient(headless=self.headless)
            await self._sv.start()
        return self._sv

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        logger.info("WCP client connected: %s", peer)
        sv = await self._get_sv()
        sess = WcpSession(sv, writer)

        try:
            # Expect client's greeting first.
            greeting = await _read_frame(reader)
            if greeting is None or greeting.get("type") != "greeting":
                await self._send_error(writer, None, "expected greeting first")
                return
            sess.client_events = set(greeting.get("commands", []))
            logger.info("client accepts events: %s", sess.client_events)

            # Respond with our own greeting.
            writer.write(_encode_frame({
                "type": "greeting",
                "version": "0",
                "commands": sorted(SUPPORTED.keys()),
            }))
            await writer.drain()

            # Install GUI menu hooks so human clicks fire WCP events.
            await _register_gui_event_hooks(sv, sess.client_events)

            # Spawn a task that forwards Tcl-pushed events as WCP event frames
            # to this client for the lifetime of the connection.
            event_task = asyncio.create_task(_forward_events(sv, sess))

            # Command loop.
            while True:
                msg = await _read_frame(reader)
                if msg is None:
                    break
                if msg.get("type") != "command":
                    await self._send_error(writer, msg.get("command"),
                                            f"unexpected type {msg.get('type')!r}")
                    continue
                cmd = msg.get("command")
                handler = SUPPORTED.get(cmd)
                if handler is None:
                    await self._send_error(writer, cmd, f"unsupported command {cmd!r}")
                    continue
                try:
                    payload = await handler(sess, msg)
                except SimVisionError as e:
                    await self._send_error(writer, cmd, str(e))
                    continue
                except Exception as e:
                    logger.exception("handler crash: %s", e)
                    await self._send_error(writer, cmd, f"internal error: {e}")
                    continue
                writer.write(_encode_frame({
                    "type": "response",
                    "command": cmd,
                    **(payload or {}),
                }))
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        except Exception as e:
            logger.exception("client loop crashed: %s", e)
        finally:
            try:
                event_task.cancel()
                try:
                    await event_task
                except (asyncio.CancelledError, Exception):
                    pass
            except NameError:
                pass  # greeting failed before event_task was spawned
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            logger.info("WCP client disconnected: %s", peer)

    async def _send_error(
        self, writer: asyncio.StreamWriter, cmd: str | None, message: str
    ) -> None:
        writer.write(_encode_frame({
            "type": "error",
            "command": cmd,
            "message": message,
        }))
        try:
            await writer.drain()
        except Exception:
            pass

    async def start(self) -> int:
        """Bind the listener and return the chosen port. Non-blocking."""
        self._server = await asyncio.start_server(
            self._handle_client, "127.0.0.1", self.port
        )
        self.port = self._server.sockets[0].getsockname()[1]
        logger.info("simvision-wcp listening on 127.0.0.1:%d", self.port)
        return self.port

    async def serve(self) -> None:
        """Bind and block forever — the CLI entry point."""
        await self.start()
        print(f"simvision-wcp listening on 127.0.0.1:{self.port}", flush=True)
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if getattr(self, "_server", None) is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def stop(self) -> None:
        if self._sv is not None:
            await self._sv.stop()
            self._sv = None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="simvision-wcp",
        description="WCP (Waveform Communication Protocol) server for SimVision",
    )
    p.add_argument("--port", type=int, default=8080,
                   help="TCP port to listen on (default 8080)")
    p.add_argument("--headless", action="store_true", default=True,
                   help="Spawn SimVision in headless Xvfb mode (default)")
    p.add_argument("--no-headless", dest="headless", action="store_false",
                   help="Use the ambient $DISPLAY instead of spawning Xvfb")
    p.add_argument("--attach", type=int, metavar="PORT",
                   help="Attach to an already-running SimVision on the given"
                        " Tcl control port (skips launch)")
    p.add_argument("-v", "--verbose", action="count", default=0)
    args = p.parse_args(argv)

    level = logging.WARNING - 10 * args.verbose
    logging.basicConfig(
        level=max(level, logging.DEBUG),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    server = WcpServer(
        port=args.port,
        headless=args.headless,
        existing_port=args.attach,
    )
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        asyncio.run(server.stop())


if __name__ == "__main__":
    main()
