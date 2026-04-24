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
    raw = await sess.sv.send(
        f"database open -overwrite {tcl_brace(abs_path)}"
    )
    # `database open` returns the logical name. Remember it + the unit.
    db_name = raw.strip()
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


async def _handle_add_variables(sess: WcpSession, msg: dict) -> dict:
    variables: list[str] = msg["variables"]
    if not variables:
        return {"ids": []}
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


async def _handle_shutdown(sess: WcpSession, msg: dict) -> dict:
    # Tear down SimVision; the connection will close after we return.
    try:
        await sess.sv.stop()
    except Exception as e:
        logger.warning("error during shutdown: %s", e)
    return {}


SUPPORTED: dict[str, Any] = {
    "load": _handle_load,
    "reload": _handle_reload,
    "add_variables": _handle_add_variables,
    "remove_items": _handle_remove_items,
    "clear": _handle_clear,
    "set_cursor": _handle_set_cursor,
    "set_viewport_range": _handle_set_viewport_range,
    "zoom_to_fit": _handle_zoom_to_fit,
    "shutdown": _handle_shutdown,
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
