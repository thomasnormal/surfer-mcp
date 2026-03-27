"""WCP client: TCP connection, framing, handshake, and subprocess management for Surfer."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess

LOG_DIR = os.path.expanduser("~/.surfer-mcp")
os.makedirs(LOG_DIR, exist_ok=True)

# File handler for persistent debug logs
_fh = logging.FileHandler(os.path.join(LOG_DIR, "surfer-mcp.log"))
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

logger = logging.getLogger("surfer_mcp")
logger.setLevel(logging.DEBUG)
logger.addHandler(_fh)

# Events we can receive from Surfer (advertised in our greeting)
WCP_CLIENT_COMMANDS = [
    "waveforms_loaded",
    "goto_declaration",
    "add_drivers",
    "add_loads",
]


class WcpError(Exception):
    pass


class WcpClient:
    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._response_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._event_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._lock = asyncio.Lock()  # serialise commands
        self._start_lock = asyncio.Lock()  # prevent concurrent launches
        self._started = False
        self._connection_lost = False

    @property
    def running(self) -> bool:
        return self._started and not self._connection_lost

    async def start(self) -> None:
        async with self._start_lock:
            if self._started and not self._connection_lost:
                return
            await self._do_start()

    async def _do_start(self) -> None:

        # Reset state for reconnect
        self._connection_lost = False
        self._started = False
        self._drain_queue(self._response_queue)
        self._drain_queue(self._event_queue)

        surfer = shutil.which("surfer")
        if surfer is None:
            raise WcpError("surfer executable not found on PATH")

        # Listen on ephemeral port
        server = await asyncio.start_server(self._on_connect, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        logger.info("WCP listener on port %d", port)

        self._tcp_server = server

        # Launch surfer, capture stderr to log file
        surfer_log = open(os.path.join(LOG_DIR, "surfer-stderr.log"), "w")
        self._surfer_log = surfer_log
        self._process = subprocess.Popen(
            [surfer, "--wcp-initiate", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=surfer_log,
        )
        logger.info("Launched surfer (pid %d)", self._process.pid)

        # Wait for Surfer to connect
        self._connect_event = asyncio.Event()
        try:
            await asyncio.wait_for(self._connect_event.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            self._process.kill()
            server.close()
            raise WcpError("Surfer did not connect within 15 seconds")

        # Perform handshake
        await self._handshake()
        self._started = True

    async def _on_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        logger.info("Surfer connected via WCP")
        self._reader = reader
        self._writer = writer
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._connect_event.set()
        self._tcp_server.close()

    async def _handshake(self) -> None:
        greeting = {
            "type": "greeting",
            "version": "0",
            "commands": WCP_CLIENT_COMMANDS,
        }
        self._send_frame(greeting)

        resp = await asyncio.wait_for(self._response_queue.get(), timeout=10.0)
        if resp.get("type") != "greeting":
            raise WcpError(f"Expected greeting, got: {resp}")
        logger.info("WCP handshake complete, server commands: %s", resp.get("commands"))

    def _send_frame(self, obj: dict) -> None:
        data = json.dumps(obj).encode() + b"\0"
        assert self._writer is not None
        self._writer.write(data)

    async def _reader_loop(self) -> None:
        assert self._reader is not None
        buf = b""
        try:
            while True:
                chunk = await self._reader.read(65536)
                if not chunk:
                    logger.warning("WCP connection closed by Surfer (EOF)")
                    self._connection_lost = True
                    break
                buf += chunk
                while b"\0" in buf:
                    frame, buf = buf.split(b"\0", 1)
                    msg = json.loads(frame)
                    logger.debug("WCP recv: %s", msg)
                    if msg.get("type") == "event":
                        await self._event_queue.put(msg)
                    else:
                        await self._response_queue.put(msg)
        except asyncio.CancelledError:
            pass
        except ConnectionResetError:
            logger.error("WCP connection reset by Surfer")
            self._connection_lost = True
        except Exception as e:
            logger.error("WCP reader error: %s", e)
            self._connection_lost = True

    def _drain_queue(self, queue: asyncio.Queue) -> None:
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _recv_response(self, timeout: float = 30.0) -> dict:
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            if self._connection_lost:
                raise WcpError("Connection to Surfer lost")
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            msg = await asyncio.wait_for(self._response_queue.get(), timeout=remaining)
            if msg.get("type") == "event":
                logger.debug("Skipping event in response queue: %s", msg)
                continue
            return msg

    async def send_command(
        self, command: str, *, wait_event: str | None = None, **kwargs
    ) -> dict:
        """Send a WCP command and return the response."""
        if not self.running:
            await self.start()

        async with self._lock:
            if self._connection_lost:
                raise WcpError("Connection to Surfer lost")

            self._drain_queue(self._response_queue)

            msg: dict = {"type": "command", "command": command}
            for k, v in kwargs.items():
                if v is not None:
                    msg[k] = v
            logger.info("WCP send: %s", msg)
            self._send_frame(msg)
            await self._writer.drain()

            try:
                resp = await self._recv_response(timeout=30.0)
            except asyncio.TimeoutError:
                raise WcpError(f"Timeout waiting for response to '{command}'")

            logger.info("WCP resp for '%s': %s", command, resp)

            if resp.get("type") == "error":
                raise WcpError(resp.get("message", str(resp)))

            if wait_event:
                try:
                    event = await asyncio.wait_for(self._event_queue.get(), timeout=30.0)
                    logger.info("WCP event after %s: %s", command, event)
                except asyncio.TimeoutError:
                    logger.warning("Timeout waiting for event '%s' after '%s'", wait_event, command)

            return resp

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False

        try:
            async with self._lock:
                self._send_frame({"type": "command", "command": "shutdown"})
                await self._writer.drain()
        except Exception:
            pass

        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        if self._writer:
            self._writer.close()

        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()

        if hasattr(self, "_surfer_log"):
            self._surfer_log.close()
