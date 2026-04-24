from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import tempfile

import pytest

from simvision_wcp.server import (
    _EVENT_MENU_TCL,
    _register_gui_event_hooks,
)


class FakeSimVision:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, tcl: str) -> str:
        self.sent.append(tcl)
        return ""


def _local_tcp_sockets_available() -> bool:
    try:
        sock = socket.socket()
    except OSError:
        return False
    try:
        sock.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False
    finally:
        sock.close()


@pytest.mark.asyncio
async def test_register_gui_event_hooks_skips_non_gui_events() -> None:
    sv = FakeSimVision()

    await _register_gui_event_hooks(sv, {"waveforms_loaded"})

    assert sv.sent == []


@pytest.mark.asyncio
async def test_register_gui_event_hooks_installs_for_gui_events() -> None:
    sv = FakeSimVision()

    await _register_gui_event_hooks(sv, {"add_loads"})

    assert sv.sent == [_EVENT_MENU_TCL]


def test_event_menu_tcl_creates_wcp_namespace_before_proc() -> None:
    namespace_pos = _EVENT_MENU_TCL.index("namespace eval ::wcp {}")
    proc_pos = _EVENT_MENU_TCL.index("proc ::wcp::_install_menus")

    assert namespace_pos < proc_pos


def test_event_menu_tcl_uses_per_window_menus_not_global_waveform_type() -> None:
    assert "window extensions menu create -window $wname" in _EVENT_MENU_TCL
    assert "window extensions menu create -type waveform" not in _EVENT_MENU_TCL


def test_event_menu_tcl_deletes_stale_menu_entries_before_create() -> None:
    delete_pos = _EVENT_MENU_TCL.index("window extensions menu delete -window $wname")
    create_pos = _EVENT_MENU_TCL.index("window extensions menu create -window $wname")

    assert delete_pos < create_pos


def test_event_menu_tcl_uses_after_1_not_after_idle() -> None:
    """Regression guard for the SimVision 2403 hang.

    `after idle` fires during `waveform new`'s internal idletask pump,
    running the menu install re-entrantly on a still-initializing window.
    After ~4 such callbacks the 5th `waveform new` wedges. `after 1` uses
    a real 1ms timer so the callback only runs after `waveform new` has
    returned. See docs/SIMVISION-MENU-HANG.md.
    """
    assert "after 1 [list ::wcp::_install_menus $ev(window)]" in _EVENT_MENU_TCL
    # Strip comment lines before asserting — "after idle" appears in the
    # explanatory comment in the Tcl heredoc, but must not appear as a
    # live command.
    non_comment = "\n".join(
        line for line in _EVENT_MENU_TCL.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "after idle" not in non_comment


@pytest.mark.asyncio
@pytest.mark.skipif(
    shutil.which("simvision") is None, reason="simvision not on PATH",
)
@pytest.mark.skipif(
    shutil.which("Xvfb") is None, reason="Xvfb not installed",
)
@pytest.mark.skipif(
    not _local_tcp_sockets_available(), reason="local TCP sockets unavailable",
)
async def test_gui_event_menu_install_does_not_break_waveform_new() -> None:
    """Install per-window GUI event menus, then create waveform windows.

    This uses a dedicated SimVision process instead of the session-scoped
    fixture because a failing menu install can leave SimVision wedged.
    """
    from simvision_mcp.client import SimVisionClient
    from simvision_wcp.client import WcpClient
    from simvision_wcp.server import WcpServer

    async def send_with_timeout(tcl: str) -> str:
        return await asyncio.wait_for(sv.send(tcl), timeout=20.0)

    sv = SimVisionClient(headless=True)
    srv = WcpServer(port=0, headless=True)
    await sv.start()
    srv._sv = sv
    port = await srv.start()
    try:
        async with await WcpClient.connect(port, events=["add_loads"]) as client:
            # A command response proves the server got past menu registration.
            await asyncio.wait_for(client.call("get_item_list"), timeout=20.0)

            for idx in range(8):
                name = f"MenuSmoke{idx}"
                await send_with_timeout(f"catch {{waveform close {name}}}")
                await send_with_timeout(f"waveform new -name {name}")
                await send_with_timeout(f"waveform using {name}")
    finally:
        srv._sv = None
        await srv.close()
        await sv.stop()


@pytest.mark.skipif(
    shutil.which("simvision") is None, reason="simvision not on PATH",
)
@pytest.mark.skipif(
    shutil.which("Xvfb") is None, reason="Xvfb not installed",
)
def test_event_menu_tcl_in_real_simvision_waveform_new_smoke() -> None:
    """Run the per-window menu Tcl in real SimVision without the TCP bridge."""
    from simvision_mcp.client import SimVisionError, _start_xvfb_process

    simvision = shutil.which("simvision")
    assert simvision is not None

    # Use a sentinel file rather than stderr — SimVision buffers `puts stderr`
    # unreliably when run with `-input`, but file I/O is flushed correctly.
    sentinel_fd, sentinel_path = tempfile.mkstemp(suffix=".txt", prefix="wcp-smoke-")
    os.close(sentinel_fd)
    os.unlink(sentinel_path)

    script = _EVENT_MENU_TCL + r"""
set ::_smoke_log [open {%s} w]
fconfigure $::_smoke_log -buffering line
if {[catch {
    for {set i 0} {$i < 8} {incr i} {
        set name MenuSmoke$i
        catch {waveform close $name}
        puts $::_smoke_log "new $name"
        waveform new -name $name
        waveform using $name
        update
        puts $::_smoke_log "done $name"
    }
} err]} {
    puts $::_smoke_log "error: $err"
    close $::_smoke_log
    exit 1
}
puts $::_smoke_log "ok"
close $::_smoke_log
exit 0
""" % sentinel_path

    fd, script_path = tempfile.mkstemp(suffix=".tcl", prefix="wcp-menu-smoke-")
    xvfb_proc = None
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(script)

        try:
            xvfb_proc, display = _start_xvfb_process("1920x1200x24")
        except SimVisionError as e:
            pytest.skip(f"Xvfb unavailable: {e}")
        env = os.environ.copy()
        env["DISPLAY"] = display
        env.pop("XAUTHORITY", None)

        result = subprocess.run(
            [simvision, "-nosplash", "-input", script_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
    finally:
        if xvfb_proc is not None:
            xvfb_proc.terminate()
            try:
                xvfb_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                xvfb_proc.kill()
        os.unlink(script_path)

    sentinel_contents = ""
    if os.path.exists(sentinel_path):
        with open(sentinel_path) as f:
            sentinel_contents = f.read()
        os.unlink(sentinel_path)

    assert result.returncode == 0, (
        f"simvision exit={result.returncode} stderr={result.stderr!r} "
        f"sentinel={sentinel_contents!r}"
    )
    assert sentinel_contents.rstrip().endswith("ok"), (
        f"smoke sentinel did not reach 'ok': {sentinel_contents!r}"
    )
