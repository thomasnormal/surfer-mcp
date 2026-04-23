"""Client that launches SimVision and sends Tcl commands over a TCP control socket.

SimVision runs a full Tcl/Tk interpreter. We hand it a small bootstrap script via
`-input`, which opens a TCP server inside SimVision's event loop. The Python side
then connects and exchanges hex-framed messages:

  request:   "<hex-encoded utf-8 tcl command>\n"
  response:  "<status> <hex-encoded utf-8 result>\n"   status is "ok" or "err"

Hex framing keeps the wire protocol ASCII-safe and avoids depending on Tcl 8.6's
`binary encode base64`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket as _socket
import subprocess
import tempfile

LOG_DIR = os.path.expanduser("~/.simvision-mcp")
os.makedirs(LOG_DIR, exist_ok=True)

_fh = logging.FileHandler(os.path.join(LOG_DIR, "simvision-mcp.log"))
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

logger = logging.getLogger("simvision_mcp")
logger.setLevel(logging.DEBUG)
logger.addHandler(_fh)


_BOOTSTRAP_TCL = r"""
# simvision-mcp bootstrap: listen on a TCP port and evaluate hex-framed Tcl commands.
namespace eval ::mcp {}

# Recursively walk the design hierarchy starting at $parent, collecting items
# that match $type ("scope" or "signal") and $pattern (Tcl glob, "" = all).
# $parent "" means "all open databases". Results are deduplicated and sorted.
proc ::mcp::walk_hierarchy {parent type pattern} {
    set result {}
    if {$parent eq ""} {
        foreach db [database find] {
            set dbroot "${db}::"
            foreach item [::mcp::walk_hierarchy $dbroot $type $pattern] {
                lappend result $item
            }
        }
        return [lsort -unique $result]
    }
    if {[catch {scope show $parent} items]} { return {} }
    foreach item $items {
        set is_scope [::mcp::classify_scope $item]
        set want_it 0
        if {$type eq "scope" && $is_scope} { set want_it 1 }
        if {$type eq "signal" && !$is_scope} { set want_it 1 }
        if {$want_it && ($pattern eq "" || [string match $pattern $item])} {
            lappend result $item
        }
        if {$is_scope} {
            foreach deep [::mcp::walk_hierarchy $item $type $pattern] {
                lappend result $deep
            }
        }
    }
    return $result
}

# Classify a design item as a true scope (module/instance) vs. a signal.
# Multi-bit buses appear to `scope show` as parents of their bit slices
# (e.g. `foo.counter` has children `foo.counter[3]`..`foo.counter[0]`);
# those are signals, not scopes. We detect this by checking whether every
# child is named `${item}[digits]` — if so, treat item as a signal.
proc ::mcp::classify_scope {item} {
    if {[catch {scope show $item} children]} { return 0 }
    if {[llength $children] == 0} { return 0 }
    foreach c $children {
        if {[string first $item $c] != 0} { return 1 }
        set suffix [string range $c [string length $item] end]
        if {![regexp {^\[[0-9]+\]$} $suffix]} { return 1 }
    }
    return 0
}

proc ::mcp::accept {sock addr port} {
    fconfigure $sock -buffering line -translation lf -encoding binary -blocking 0
    fileevent $sock readable [list ::mcp::handle $sock]
}

proc ::mcp::handle {sock} {
    if {[catch {gets $sock line} n]} {
        catch {close $sock}
        return
    }
    if {$n < 0} {
        if {[eof $sock]} { catch {close $sock} }
        return
    }
    if {$line eq ""} return
    if {[catch {binary format H* $line} bytes]} {
        puts $sock "err [::mcp::tohex {bad hex framing}]"
        flush $sock
        return
    }
    set cmd [encoding convertfrom utf-8 $bytes]
    if {[catch {uplevel #0 $cmd} result]} {
        set status "err"
    } else {
        set status "ok"
    }
    set rbytes [encoding convertto utf-8 $result]
    set rhex ""
    if {[string length $rbytes] > 0} {
        binary scan $rbytes H* rhex
    }
    if {$rhex eq ""} { set rhex "-" }
    puts $sock "$status $rhex"
    flush $sock
}

proc ::mcp::tohex {s} {
    set b [encoding convertto utf-8 $s]
    binary scan $b H* h
    return $h
}

# Log incoming connections to stderr for debugging.
proc ::mcp::log {msg} {
    if {[info exists ::env(SIMVISION_MCP_DEBUG)]} {
        puts stderr "mcp: $msg"
    }
}

socket -server ::mcp::accept __MCP_PORT__
::mcp::log "listening on port __MCP_PORT__"
"""


class SimVisionError(Exception):
    pass


def _pick_port() -> int:
    s = _socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _start_xvfb_process(geometry: str) -> tuple[subprocess.Popen, str]:
    """Launch Xvfb on an unused display; return (process, ":N") once it's ready.

    Uses Xvfb's -displayfd option: Xvfb writes the chosen display number to the
    given fd once the server is listening. We pass a pipe and read it.
    """
    xvfb = shutil.which("Xvfb")
    if xvfb is None:
        raise SimVisionError(
            "headless=True but Xvfb is not on PATH. "
            "Install it (e.g. `dnf install xorg-x11-server-Xvfb`) "
            "or switch to headless=False."
        )

    r_fd, w_fd = os.pipe()
    os.set_inheritable(w_fd, True)
    try:
        proc = subprocess.Popen(
            [
                xvfb, "-displayfd", str(w_fd),
                "-screen", "0", geometry,
                "-nolisten", "tcp",  # local UNIX socket only, no TCP
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(w_fd,),
        )
    finally:
        os.close(w_fd)

    # Xvfb writes "<display-number>\n" when ready.
    import time
    deadline = time.time() + 10.0
    buf = b""
    try:
        while time.time() < deadline:
            chunk = os.read(r_fd, 16)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break
        if b"\n" not in buf:
            proc.kill()
            raise SimVisionError(
                f"Xvfb did not report ready within 10s (read: {buf!r})"
            )
    finally:
        os.close(r_fd)

    display_num = buf.split(b"\n", 1)[0].decode().strip()
    return proc, f":{display_num}"


class SimVisionClient:
    """Persistent connection to a SimVision subprocess over the Tcl control socket.

    Defaults to headless (Xvfb): start() spawns its own X virtual framebuffer
    and points SimVision at it. Headless boot is ~8s vs. ~120s on an
    ssh-forwarded display.

    Pass headless=False to use the ambient $DISPLAY instead.

    If the env var `SIMVISION_MCP_PORT` is set, `start()` will attach to an
    already-running SimVision on `127.0.0.1:<port>` instead of launching one.
    """

    def __init__(
        self,
        headless: bool = True,
        geometry: str = "1920x1200x24",
    ) -> None:
        self._headless = headless
        self._geometry = geometry
        self._process: subprocess.Popen | None = None
        self._xvfb_process: subprocess.Popen | None = None
        self._display: str | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._boot_path: str | None = None
        self._stderr_log = None
        # When true, we attached to an existing listener and must not shut the
        # SimVision process down in stop().
        self._borrowed = False

    @property
    def headless(self) -> bool:
        return self._headless

    @property
    def display(self) -> str | None:
        return self._display

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def running(self) -> bool:
        return (
            self._process is not None
            and self._process.poll() is None
            and self._writer is not None
            and not self._writer.is_closing()
        )

    async def start(self) -> None:
        async with self._start_lock:
            if self.running:
                return
            await self._do_start()

    def _start_xvfb(self) -> str:
        """Spawn Xvfb, store the process, and return its display string (":N")."""
        proc, display = _start_xvfb_process(self._geometry)
        self._xvfb_process = proc
        return display

    async def _do_start(self) -> None:
        # Fast path: attach to a pre-launched SimVision already running our bootstrap.
        port_env = os.environ.get("SIMVISION_MCP_PORT")
        if port_env:
            port = int(port_env)
            try:
                self._reader, self._writer = await asyncio.open_connection(
                    "127.0.0.1", port
                )
                self._borrowed = True
                logger.info("attached to existing SimVision on port %d", port)
                return
            except (ConnectionRefusedError, OSError) as e:
                raise SimVisionError(
                    f"SIMVISION_MCP_PORT={port} set but no listener there. "
                    f"Either unset it to launch SimVision, or source the "
                    f"bootstrap first. ({e})"
                )

        simvision = shutil.which("simvision")
        if simvision is None:
            raise SimVisionError(
                "simvision not found on PATH. Source your Cadence environment first."
            )

        port = _pick_port()
        tcl = _BOOTSTRAP_TCL.replace("__MCP_PORT__", str(port))
        fd, path = tempfile.mkstemp(suffix=".tcl", prefix="simvision-mcp-")
        with os.fdopen(fd, "w") as fh:
            fh.write(tcl)
        self._boot_path = path
        logger.info("bootstrap script at %s (port %d)", path, port)

        stderr_log = open(os.path.join(LOG_DIR, "simvision-stderr.log"), "w")
        self._stderr_log = stderr_log

        env = os.environ.copy()
        if not env.get("XAUTHORITY"):
            env.pop("XAUTHORITY", None)

        if self._headless:
            self._display = self._start_xvfb()
            env["DISPLAY"] = self._display
            # Under Xvfb we own the display; no auth cookie needed.
            env.pop("XAUTHORITY", None)
            logger.info("headless mode: using Xvfb display %s", self._display)
        else:
            if not env.get("DISPLAY"):
                raise SimVisionError(
                    "headless=False but $DISPLAY is not set. "
                    "Set DISPLAY or switch to headless=True."
                )
            self._display = env["DISPLAY"]

        self._process = subprocess.Popen(
            [simvision, "-nosplash", "-input", path],
            stdout=subprocess.DEVNULL,
            stderr=stderr_log,
            env=env,
        )
        logger.info("launched simvision (pid %d)", self._process.pid)

        # SimVision's GUI takes ~60s to initialize on first launch on some systems
        # (Console window must be realized before -input fires), so be patient.
        deadline = asyncio.get_event_loop().time() + 180.0
        last_err: Exception | None = None
        while asyncio.get_event_loop().time() < deadline:
            if self._process.poll() is not None:
                raise SimVisionError(
                    f"simvision exited with code {self._process.returncode} "
                    f"before control socket came up. See {stderr_log.name}."
                )
            try:
                self._reader, self._writer = await asyncio.open_connection(
                    "127.0.0.1", port
                )
                logger.info("connected to simvision control socket")
                return
            except (ConnectionRefusedError, OSError) as e:
                last_err = e
                await asyncio.sleep(0.3)

        if self._process and self._process.poll() is None:
            self._process.kill()
        raise SimVisionError(
            f"simvision did not open control socket within 180s: {last_err}"
        )

    async def send(self, tcl: str) -> str:
        """Send a Tcl command and return its result as a string. Raises on Tcl error."""
        if not self.running:
            await self.start()

        async with self._lock:
            assert self._reader is not None and self._writer is not None
            hex_cmd = tcl.encode("utf-8").hex()
            logger.debug("-> %s", tcl if len(tcl) < 500 else tcl[:500] + "...[truncated]")
            self._writer.write((hex_cmd + "\n").encode("ascii"))
            await self._writer.drain()
            try:
                line = await asyncio.wait_for(self._reader.readline(), timeout=60.0)
            except asyncio.TimeoutError as e:
                raise SimVisionError(f"timeout waiting for response to: {tcl}") from e
            if not line:
                raise SimVisionError("simvision closed the control connection")

            parts = line.decode("ascii").strip().split(" ", 1)
            status = parts[0]
            rhex = parts[1] if len(parts) > 1 else ""
            if rhex == "-" or rhex == "":
                result = ""
            else:
                try:
                    result = bytes.fromhex(rhex).decode("utf-8")
                except ValueError:
                    raise SimVisionError(f"malformed response: {line!r}")

            logger.debug("<- %s %s", status, result if len(result) < 500 else result[:500] + "...")
            if status == "err":
                raise SimVisionError(result or "unknown Tcl error")
            return result

    async def stop(self) -> None:
        # When attached to an existing listener, just drop the socket —
        # we don't own the SimVision process and must not kill it.
        if self._borrowed:
            if self._writer is not None:
                try:
                    self._writer.close()
                    await self._writer.wait_closed()
                except Exception:
                    pass
                self._writer = None
                self._reader = None
            self._borrowed = False
            return

        if self._process is None:
            return
        try:
            # `exit` from SimVision's Tcl interpreter shuts the app down cleanly.
            await asyncio.wait_for(self.send("after 50; exit"), timeout=2.0)
        except Exception:
            pass

        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None

        if self._stderr_log is not None:
            try:
                self._stderr_log.close()
            except Exception:
                pass
            self._stderr_log = None

        if self._boot_path and os.path.exists(self._boot_path):
            try:
                os.unlink(self._boot_path)
            except OSError:
                pass
            self._boot_path = None

        if self._xvfb_process is not None:
            if self._xvfb_process.poll() is None:
                self._xvfb_process.terminate()
                try:
                    self._xvfb_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._xvfb_process.kill()
            self._xvfb_process = None
            self._display = None


# Single Tcl interpreter used for list parsing and quoting. tkinter.Tcl()
# gives us an interp without creating a Tk window, and exposing its `list`,
# `splitlist`, and `eval` gives us bulletproof Tcl string handling from Python.
_tcl_interp = None


def _tcl():
    global _tcl_interp
    if _tcl_interp is None:
        import tkinter
        _tcl_interp = tkinter.Tcl()
    return _tcl_interp


def parse_tcl_list(s: str) -> list[str]:
    """Split a Tcl list string into a Python list.

    Uses the real Tcl parser (via `tkinter.Tcl()`) so brace-quoting, nested
    braces, and backslash escapes are all handled correctly.
    """
    return list(_tcl().splitlist(s))


def tcl_list(items: list[str]) -> str:
    """Build a Tcl list string from a Python list of strings. Always safe.

    Uses `lappend` on a temp var and reads back the string form via `eval`.
    `tcl.call("list", ...)` would also work in Tcl, but tkinter auto-converts
    the return value to a Python tuple — we need the Tcl source text here.
    """
    tcl = _tcl()
    tcl.eval("set ::mcp_quote_tmp {}")
    for item in items:
        tcl.call("lappend", "::mcp_quote_tmp", item)
    return tcl.eval("set ::mcp_quote_tmp")


def tcl_brace(s: str) -> str:
    """Quote a string as a single Tcl word. Always safe.

    Tcl's own `list` command with a single element picks the minimal safe
    quoting (bare word, braces, or backslash escapes) for any input.
    """
    return tcl_list([s])
