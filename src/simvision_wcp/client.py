"""Minimal Python WCP client — connect to any WCP server and exchange frames.

Complements `simvision_wcp.server.WcpServer`. Also the recommended way to
drive `simvision-wcp` from a Python caller (tests, scripts, integrations).

Wire format: newline-null-terminated JSON frames, matching Surfer's WCP.

Example:

    async with WcpClient.connect(port, events=["waveforms_loaded"]) as client:
        await client.call("load", source="/path/to/tiny.vcd")
        resp = await client.call("add_variables", variables=["waves:::tb.clk"])
        ids = resp["ids"]
        await client.call("set_cursor", timestamp=55)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


class WcpError(Exception):
    pass


class WcpClient:
    """WCP client over a TCP connection.

    Connect via `await WcpClient.connect(port, events=...)`. Use as an async
    context manager to get automatic cleanup:

        async with WcpClient.connect(port) as client:
            await client.call("shutdown")
    """

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.server_commands: list[str] = []

    # ---- factory + lifecycle ------------------------------------------------

    @classmethod
    async def connect(
        cls,
        port: int,
        host: str = "127.0.0.1",
        events: list[str] | None = None,
    ) -> "WcpClient":
        """Open a connection, perform the greeting handshake, return the client.

        `events` is the list of WCP event types this client is willing to
        receive (empty means "I don't subscribe to anything").
        """
        reader, writer = await asyncio.open_connection(host, port)
        self = cls(reader, writer)
        writer.write(_frame({
            "type": "greeting", "version": "0", "commands": events or [],
        }))
        await writer.drain()
        msg = await self._recv()
        if msg.get("type") != "greeting":
            raise WcpError(f"expected greeting from server, got: {msg!r}")
        self.server_commands = list(msg.get("commands", []))
        return self

    async def close(self) -> None:
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass

    async def __aenter__(self) -> "WcpClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # ---- protocol -----------------------------------------------------------

    async def _recv(self) -> dict:
        data = await self.reader.readuntil(b"\0")
        return json.loads(data[:-1].decode("utf-8"))

    async def call(self, command: str, **fields: Any) -> dict:
        """Send a command and wait for its response. Skips intervening events.

        Raises `WcpError` if the server returns `{"type": "error", ...}`.
        """
        self.writer.write(_frame({"type": "command", "command": command, **fields}))
        await self.writer.drain()
        while True:
            msg = await self._recv()
            t = msg.get("type")
            if t == "event":
                continue  # caller can use `recv_event` if they want events
            if t == "error":
                raise WcpError(f"{command}: {msg.get('message')}")
            if t == "response":
                return msg
            raise WcpError(f"unexpected frame: {msg!r}")

    async def recv_event(self, timeout: float | None = None) -> dict:
        """Wait for the next event frame. Discards responses/errors (unusual)."""
        async def _next():
            while True:
                msg = await self._recv()
                if msg.get("type") == "event":
                    return msg
        if timeout is None:
            return await _next()
        return await asyncio.wait_for(_next(), timeout=timeout)


def _frame(obj: dict) -> bytes:
    return json.dumps(obj).encode("utf-8") + b"\0"
