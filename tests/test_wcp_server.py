"""Integration tests for simvision-wcp: drive it like a real WCP client.

Starts an in-process WcpServer that shares the test-session SimVision, connects
a minimal WCP client over TCP, exercises the MVP command set.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from simvision_wcp.server import WcpServer  # noqa: E402


pytestmark = [
    pytest.mark.skipif(
        shutil.which("simvision") is None, reason="simvision not on PATH",
    ),
    pytest.mark.skipif(
        shutil.which("Xvfb") is None, reason="Xvfb not installed",
    ),
]


VCD = os.path.join(HERE, "data", "tiny.vcd")


class WcpClient:
    """Minimal WCP client for tests — JSON frames terminated by NUL."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.server_commands: list[str] = []

    @classmethod
    async def connect(cls, port: int, events: list[str] | None = None):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self = cls(reader, writer)
        writer.write(self._frame({
            "type": "greeting", "version": "0", "commands": events or [],
        }))
        await writer.drain()
        msg = await self._recv()
        assert msg["type"] == "greeting", msg
        self.server_commands = msg["commands"]
        return self

    @staticmethod
    def _frame(obj: dict) -> bytes:
        return json.dumps(obj).encode("utf-8") + b"\0"

    async def _recv(self) -> dict:
        data = await self.reader.readuntil(b"\0")
        return json.loads(data[:-1].decode("utf-8"))

    async def call(self, command: str, **fields) -> dict:
        self.writer.write(self._frame({"type": "command", "command": command, **fields}))
        await self.writer.drain()
        # Skip any interleaved events — the server may emit `waveforms_loaded`
        # asynchronously. Returns the first response/error frame.
        while True:
            msg = await self._recv()
            if msg.get("type") == "event":
                continue
            if msg.get("type") == "error":
                raise RuntimeError(f"{command}: {msg.get('message')}")
            assert msg.get("type") == "response", msg
            return msg

    async def recv_event(self, timeout: float = 2.0) -> dict:
        """Wait specifically for an event frame."""
        return await asyncio.wait_for(self._recv(), timeout=timeout)

    async def close(self):
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass


@pytest.fixture
def wcp_port(sv, event_loop):
    """Start a WcpServer that shares the session's SimVisionClient, bound to
    an ephemeral port. Yields the port; cleans up on teardown.
    """
    srv = WcpServer(port=0, headless=True)
    # Reuse the fixture's already-booted SimVision — skip the spawn path.
    srv._sv = sv
    port = event_loop.run_until_complete(srv.start())
    try:
        yield port
    finally:
        # Keep sv alive (owned by the session fixture); only close our server.
        srv._sv = None
        event_loop.run_until_complete(srv.close())


def test_wcp_handshake_advertises_mvp_commands(wcp_port, aio):
    client = aio(WcpClient.connect(wcp_port))
    try:
        # Server should advertise every MVP command we claim to support.
        for expected in (
            "load", "reload", "add_variables", "remove_items", "clear",
            "set_cursor", "set_viewport_range", "zoom_to_fit", "shutdown",
        ):
            assert expected in client.server_commands, (
                f"missing {expected!r} in {client.server_commands}"
            )
    finally:
        aio(client.close())


def test_wcp_load_and_add_variables(sv, aio, wcp_port):
    client = aio(WcpClient.connect(wcp_port, events=["waveforms_loaded"]))
    try:
        # Clean up any prior `tiny` registration — the session fixture shares state.
        aio(sv.send("catch {database close tiny}"))
        aio(sv.send("catch {waveform close Wcp}"))
        aio(sv.send("waveform new -name Wcp"))
        aio(sv.send(f"waveform using Wcp"))

        # load
        resp = aio(client.call("load", source=VCD))
        assert resp["type"] == "response" and resp["command"] == "load"

        # add_variables (need to use fully qualified names — WCP doesn't
        # auto-normalize separators the way the MCP does).
        resp = aio(client.call(
            "add_variables",
            variables=["waves:::tb.clk", "waves:::tb.counter", "waves:::tb.valid"],
        ))
        ids = resp["ids"]
        assert isinstance(ids, list) and len(ids) == 3
        assert all(isinstance(i, int) for i in ids)
        assert len(set(ids)) == 3  # unique
    finally:
        aio(client.close())


def test_wcp_set_cursor_and_viewport(sv, aio, wcp_port):
    client = aio(WcpClient.connect(wcp_port, events=["waveforms_loaded"]))
    try:
        aio(sv.send("catch {database close waves}"))
        aio(sv.send("catch {waveform close Wcp2}"))
        aio(sv.send("waveform new -name Wcp2"))
        aio(sv.send("waveform using Wcp2"))

        aio(client.call("load", source=VCD))
        aio(client.call("add_variables", variables=["waves:::tb.clk"]))

        # set_cursor — BigInt in the DB's native unit (ns for tiny.vcd).
        aio(client.call("set_cursor", timestamp=55))

        # Sanity: cursor should now be at 55ns. Query by name to avoid the
        # "current cursor context not set" quirk when no cursor has been
        # explicitly made current.
        from simvision_mcp.client import parse_tcl_list
        cursors = parse_tcl_list(aio(sv.send("cursor find")))
        assert cursors, "expected at least one cursor after set_cursor"
        t = aio(sv.send(f"cursor get -using {cursors[0]} -time"))
        assert "55" in t, f"cursor time {t!r}"

        # set_viewport_range.
        aio(client.call("set_viewport_range", start=0, end=100))
        limits = aio(sv.send("waveform xview limits"))
        parts = limits.split()
        assert len(parts) == 2

        # zoom_to_fit — should succeed, state-only check.
        aio(client.call("zoom_to_fit", viewport_idx=0))
    finally:
        aio(client.close())


def test_wcp_remove_items_and_clear(sv, aio, wcp_port):
    client = aio(WcpClient.connect(wcp_port))
    try:
        aio(sv.send("catch {database close waves}"))
        aio(sv.send("catch {waveform close WcpRm}"))
        aio(sv.send("waveform new -name WcpRm"))
        aio(sv.send("waveform using WcpRm"))

        aio(client.call("load", source=VCD))
        resp = aio(client.call(
            "add_variables",
            variables=["waves:::tb.clk", "waves:::tb.counter"],
        ))
        ids = resp["ids"]
        assert len(ids) == 2

        # Remove the first one.
        aio(client.call("remove_items", ids=[ids[0]]))
        remaining = aio(sv.send("waveform signals -using WcpRm"))
        assert len(remaining.split()) == 1

        # Clear.
        aio(client.call("clear"))
        after_clear = aio(sv.send("waveform signals -using WcpRm"))
        assert after_clear.strip() == ""
    finally:
        aio(client.close())


def test_wcp_reload_uses_last_source(sv, aio, wcp_port):
    client = aio(WcpClient.connect(wcp_port, events=["waveforms_loaded"]))
    try:
        aio(sv.send("catch {database close waves}"))
        aio(client.call("load", source=VCD))
        # Second load via reload — no source arg.
        aio(client.call("reload"))
    finally:
        aio(client.close())


def test_wcp_unknown_command_errors(wcp_port, aio):
    client = aio(WcpClient.connect(wcp_port))
    try:
        with pytest.raises(RuntimeError, match="unsupported"):
            aio(client.call("does_not_exist"))
    finally:
        aio(client.close())


def test_wcp_waveforms_loaded_event(sv, aio, wcp_port):
    """When the client subscribes to `waveforms_loaded`, the server emits
    one after a `load`."""
    client = aio(WcpClient.connect(wcp_port, events=["waveforms_loaded"]))
    try:
        aio(sv.send("catch {database close waves}"))

        # Send load, then look for the event (may arrive before or after response).
        client.writer.write(client._frame({"type": "command", "command": "load", "source": VCD}))
        aio(client.writer.drain())

        saw_event = False
        saw_response = False
        while not (saw_event and saw_response):
            msg = aio(asyncio.wait_for(client._recv(), timeout=5.0))
            if msg.get("type") == "event" and msg.get("event") == "waveforms_loaded":
                assert msg.get("source", "").endswith("tiny.vcd"), msg
                saw_event = True
            elif msg.get("type") == "response" and msg.get("command") == "load":
                saw_response = True
            elif msg.get("type") == "error":
                raise RuntimeError(msg.get("message"))
    finally:
        aio(client.close())
