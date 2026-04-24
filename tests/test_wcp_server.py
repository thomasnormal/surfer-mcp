"""Integration tests for simvision-wcp: drive it like a real WCP client.

Starts an in-process WcpServer that shares the session-scoped SimVision
fixture, connects the `simvision_wcp.client.WcpClient` over TCP, exercises
the MVP command set.
"""

from __future__ import annotations

import os
import shutil

import pytest
import pytest_asyncio

from simvision_wcp.client import WcpClient, WcpError
from simvision_wcp.server import WcpServer


pytestmark = [
    pytest.mark.skipif(
        shutil.which("simvision") is None, reason="simvision not on PATH",
    ),
    pytest.mark.skipif(
        shutil.which("Xvfb") is None, reason="Xvfb not installed",
    ),
]


HERE = os.path.dirname(os.path.abspath(__file__))
VCD = os.path.join(HERE, "data", "tiny.vcd")


@pytest_asyncio.fixture(loop_scope="session")
async def wcp_port(sv):
    """Start a WcpServer sharing the session's SimVision; yield its port."""
    srv = WcpServer(port=0, headless=True)
    srv._sv = sv  # skip the spawn path — reuse the fixture's live SimVision
    port = await srv.start()
    try:
        yield port
    finally:
        srv._sv = None  # keep sv alive (owned by the session fixture)
        await srv.close()


async def test_wcp_handshake_advertises_full_spec(wcp_port):
    """All 17 WCP commands from Surfer's proto.rs should be advertised."""
    async with await WcpClient.connect(wcp_port) as client:
        for expected in (
            "load", "reload",
            "add_variables", "add_scope", "add_items", "add_markers",
            "remove_items", "clear",
            "set_cursor", "set_viewport_range", "set_viewport_to",
            "zoom_to_fit", "focus_item", "set_item_color",
            "get_item_list", "get_item_info",
            "shutdown",
        ):
            assert expected in client.server_commands, (
                f"missing {expected!r} in {client.server_commands}"
            )


async def test_wcp_load_and_add_variables(sv, wcp_port):
    async with await WcpClient.connect(wcp_port, events=["waveforms_loaded"]) as client:
        await sv.send("catch {database close tiny}")
        await sv.send("catch {waveform close Wcp}")
        await sv.send("waveform new -name Wcp")
        await sv.send("waveform using Wcp")

        resp = await client.call("load", source=VCD)
        assert resp["command"] == "load"

        resp = await client.call(
            "add_variables",
            variables=["waves:::tb.clk", "waves:::tb.counter", "waves:::tb.valid"],
        )
        ids = resp["ids"]
        assert isinstance(ids, list) and len(ids) == 3
        assert all(isinstance(i, int) for i in ids)
        assert len(set(ids)) == 3


async def test_wcp_set_cursor_and_viewport(sv, wcp_port):
    async with await WcpClient.connect(wcp_port, events=["waveforms_loaded"]) as client:
        await sv.send("catch {database close waves}")
        await sv.send("catch {waveform close Wcp2}")
        await sv.send("waveform new -name Wcp2")
        await sv.send("waveform using Wcp2")

        await client.call("load", source=VCD)
        await client.call("add_variables", variables=["waves:::tb.clk"])

        await client.call("set_cursor", timestamp=55)

        # Sanity: cursor sits at 55ns. Query by name to avoid the
        # "current-cursor context not set" quirk.
        from simvision_mcp.client import parse_tcl_list
        cursors = parse_tcl_list(await sv.send("cursor find"))
        assert cursors, "expected at least one cursor after set_cursor"
        t = await sv.send(f"cursor get -using {cursors[0]} -time")
        assert "55" in t, f"cursor time {t!r}"

        await client.call("set_viewport_range", start=0, end=100)
        limits = await sv.send("waveform xview limits")
        assert len(limits.split()) == 2

        await client.call("zoom_to_fit", viewport_idx=0)


async def test_wcp_remove_items_and_clear(sv, wcp_port):
    async with await WcpClient.connect(wcp_port) as client:
        await sv.send("catch {database close waves}")
        await sv.send("catch {waveform close WcpRm}")
        await sv.send("waveform new -name WcpRm")
        await sv.send("waveform using WcpRm")

        await client.call("load", source=VCD)
        resp = await client.call(
            "add_variables",
            variables=["waves:::tb.clk", "waves:::tb.counter"],
        )
        ids = resp["ids"]
        assert len(ids) == 2

        await client.call("remove_items", ids=[ids[0]])
        remaining = await sv.send("waveform signals -using WcpRm")
        assert len(remaining.split()) == 1

        await client.call("clear")
        after_clear = await sv.send("waveform signals -using WcpRm")
        assert after_clear.strip() == ""


async def test_wcp_reload_uses_last_source(sv, wcp_port):
    async with await WcpClient.connect(wcp_port, events=["waveforms_loaded"]) as client:
        await sv.send("catch {database close waves}")
        await client.call("load", source=VCD)
        await client.call("reload")


async def test_wcp_unknown_command_errors(wcp_port):
    async with await WcpClient.connect(wcp_port) as client:
        with pytest.raises(WcpError, match="unsupported"):
            await client.call("does_not_exist")


async def test_wcp_set_viewport_to(sv, wcp_port):
    async with await WcpClient.connect(wcp_port) as client:
        await sv.send("catch {database close waves}")
        await sv.send("catch {waveform close VpTo}")
        await sv.send("waveform new -name VpTo")
        await sv.send("waveform using VpTo")
        await client.call("load", source=VCD)
        await client.call("add_variables", variables=["waves:::tb.clk"])
        # Just has to not error — viewport centering is hard to assert
        # numerically without knowing SimVision's zoom factor.
        await client.call("set_viewport_to", timestamp=50)


async def test_wcp_focus_item(sv, wcp_port):
    async with await WcpClient.connect(wcp_port) as client:
        await sv.send("catch {database close waves}")
        await sv.send("catch {waveform close Focus}")
        await sv.send("waveform new -name Focus")
        await sv.send("waveform using Focus")
        await client.call("load", source=VCD)
        resp = await client.call(
            "add_variables",
            variables=["waves:::tb.clk", "waves:::tb.counter"],
        )
        await client.call("focus_item", id=resp["ids"][1])


async def test_wcp_set_item_color(sv, wcp_port):
    async with await WcpClient.connect(wcp_port) as client:
        await sv.send("catch {database close waves}")
        await sv.send("catch {waveform close Color}")
        await sv.send("waveform new -name Color")
        await sv.send("waveform using Color")
        await client.call("load", source=VCD)
        resp = await client.call("add_variables", variables=["waves:::tb.clk"])
        await client.call("set_item_color", id=resp["ids"][0], color="red")


async def test_wcp_add_scope_non_recursive(sv, wcp_port):
    async with await WcpClient.connect(wcp_port) as client:
        await sv.send("catch {database close waves}")
        await sv.send("catch {waveform close Scope}")
        await sv.send("waveform new -name Scope")
        await sv.send("waveform using Scope")
        await client.call("load", source=VCD)
        resp = await client.call("add_scope", scope="waves::tb", recursive=False)
        assert len(resp["ids"]) >= 3  # clk, counter, valid


async def test_wcp_add_scope_recursive(sv, wcp_port):
    async with await WcpClient.connect(wcp_port) as client:
        await sv.send("catch {database close waves}")
        await sv.send("catch {waveform close ScopeRec}")
        await sv.send("waveform new -name ScopeRec")
        await sv.send("waveform using ScopeRec")
        await client.call("load", source=VCD)
        resp = await client.call("add_scope", scope="waves::", recursive=True)
        assert len(resp["ids"]) >= 3


async def test_wcp_add_markers(sv, wcp_port):
    async with await WcpClient.connect(wcp_port) as client:
        await sv.send("catch {database close waves}")
        await sv.send("catch {foreach m [marker find] {marker delete $m}}")
        await client.call("load", source=VCD)
        resp = await client.call("add_markers", markers=[
            {"time": 25, "name": "start", "move_focus": False},
            {"time": 75, "name": "end", "move_focus": False},
        ])
        assert len(resp["ids"]) == 2
        # Both markers should now be findable in SimVision.
        names = await sv.send("marker find")
        assert "start" in names and "end" in names


async def test_wcp_get_item_list_and_info(sv, wcp_port):
    async with await WcpClient.connect(wcp_port) as client:
        await sv.send("catch {database close waves}")
        await sv.send("catch {waveform close Info}")
        await sv.send("waveform new -name Info")
        await sv.send("waveform using Info")
        await client.call("load", source=VCD)
        resp = await client.call(
            "add_variables",
            variables=["waves:::tb.clk", "waves:::tb.counter"],
        )
        ids = resp["ids"]

        listed = await client.call("get_item_list")
        assert set(listed["ids"]) >= set(ids)

        info = await client.call("get_item_info", ids=ids)
        assert len(info["results"]) == 2
        for r in info["results"]:
            assert {"name", "type", "id"} <= set(r.keys())
            assert r["id"] in ids
            assert r["name"]


async def test_wcp_add_items_mixed(sv, wcp_port):
    """`add_items` accepts scopes and signals in one call, with recursion."""
    import json
    async with await WcpClient.connect(wcp_port) as client:
        await sv.send("catch {database close waves}")
        await sv.send("catch {waveform close Mixed}")
        await sv.send("waveform new -name Mixed")
        await sv.send("waveform using Mixed")

        await client.call("load", source=VCD)
        # Mix a scope (waves::tb) and a signal path (waves:::tb.clk).
        resp = await client.call(
            "add_items",
            items=["waves::tb", "waves:::tb.clk"],
            recursive=False,
        )
        # The scope expands to 3 signals; the explicit clk dedups internally
        # since we already walked it — at minimum we get 3 unique IDs.
        assert len(resp["ids"]) >= 3


async def test_wcp_add_loads_event(sv, wcp_port):
    """The add_loads event type fires and is delivered to subscribers."""
    import asyncio as _asyncio
    async with await WcpClient.connect(wcp_port, events=["add_loads"]) as client:
        await _asyncio.sleep(0.1)
        await sv.send("::mcp::push_event add_loads variable top.dut.en")
        ev = await _asyncio.wait_for(client.recv_event(), timeout=3.0)
        assert ev["event"] == "add_loads"
        assert ev["variable"] == "top.dut.en"


async def test_wcp_shutdown_tears_down_simvision():
    """`shutdown` ends the SimVision process. Uses a dedicated session so the
    session-scoped `sv` fixture isn't affected.
    """
    from simvision_mcp.client import SimVisionClient
    from simvision_wcp.server import WcpServer

    # Dedicated SimVision, dedicated WCP server, one-shot.
    local_sv = SimVisionClient(headless=True)
    await local_sv.start()
    local_srv = WcpServer(port=0, headless=True)
    local_srv._sv = local_sv
    port = await local_srv.start()

    try:
        async with await WcpClient.connect(port) as client:
            resp = await client.call("shutdown")
            assert resp["command"] == "shutdown"
        # After shutdown, the SimVision subprocess should be gone.
        # Poll briefly — `exit` takes a moment to propagate.
        import asyncio as _asyncio
        for _ in range(20):
            if local_sv._process is None or local_sv._process.poll() is not None:
                break
            await _asyncio.sleep(0.1)
        assert (
            local_sv._process is None
            or local_sv._process.poll() is not None
        ), "SimVision process still alive after WCP shutdown"
    finally:
        local_srv._sv = None
        await local_srv.close()
        # Ensure any leftover Xvfb is cleaned up. stop() is idempotent.
        await local_sv.stop()


async def test_wcp_gui_event_forwarded_to_subscribed_client(sv, wcp_port):
    """Events pushed via `::mcp::push_event` are delivered to clients that
    subscribed for them, and dropped for clients that didn't.

    Simulates what happens when a human clicks a 'WCP>Goto Declaration' menu
    item: the Tcl command fires `::mcp::push_event goto_declaration ...`,
    which the bootstrap writes as an `evt` frame. The background reader
    demuxes it into SimVisionClient's event queue; the WCP server's
    per-client forwarder then relays subscribed ones to the client.
    """
    import asyncio as _asyncio
    async with await WcpClient.connect(
        wcp_port, events=["goto_declaration"],
    ) as client:
        # Give the server a moment to spawn its event-forwarder for this conn.
        await _asyncio.sleep(0.1)
        # Fire the Tcl side directly.
        await sv.send("::mcp::push_event goto_declaration variable top.dut.clk")
        ev = await _asyncio.wait_for(client.recv_event(), timeout=3.0)
        assert ev["event"] == "goto_declaration"
        assert ev["variable"] == "top.dut.clk"


async def test_wcp_gui_event_not_forwarded_to_unsubscribed(sv, wcp_port):
    """A client that doesn't advertise an event type shouldn't receive it."""
    import asyncio as _asyncio
    async with await WcpClient.connect(wcp_port, events=[]) as client:
        await _asyncio.sleep(0.1)
        await sv.send("::mcp::push_event add_drivers variable top.dut.en")
        # No forward expected — send a command, ensure no event interleaves.
        resp = await client.call("get_item_list")
        assert resp["command"] == "get_item_list"


async def test_wcp_screenshot_returns_png_bytes(sv, wcp_port):
    """Non-spec `screenshot` extension renders the waveform window and returns
    base64 PNG bytes inline.
    """
    import base64

    async with await WcpClient.connect(wcp_port) as client:
        assert "screenshot" in client.server_commands
        await client.call("load", source=VCD)
        await client.call("add_variables", variables=["waves:::tb.clk"])

        resp = await client.call("screenshot", format="png")
        assert resp.get("format") == "png"
        data = base64.b64decode(resp["data"])
        # PNG file signature: \x89PNG\r\n\x1a\n
        assert data[:8] == b"\x89PNG\r\n\x1a\n", (
            f"not a PNG: {data[:8]!r}"
        )
        # Sanity: a real waveform render is several KB, not a stub.
        assert len(data) > 2000, f"suspiciously small PNG: {len(data)} bytes"


async def test_wcp_waveforms_loaded_event(sv, wcp_port):
    """When the client subscribes to `waveforms_loaded`, the server emits
    one after a `load`.
    """
    import asyncio
    from simvision_wcp.client import _frame

    async with await WcpClient.connect(wcp_port, events=["waveforms_loaded"]) as client:
        await sv.send("catch {database close waves}")

        # Send load manually and watch for both the response and the event.
        client.writer.write(_frame({"type": "command", "command": "load", "source": VCD}))
        await client.writer.drain()

        saw_event = False
        saw_response = False
        while not (saw_event and saw_response):
            msg = await asyncio.wait_for(client._recv(), timeout=5.0)
            if msg.get("type") == "event" and msg.get("event") == "waveforms_loaded":
                assert msg.get("source", "").endswith("tiny.vcd"), msg
                saw_event = True
            elif msg.get("type") == "response" and msg.get("command") == "load":
                saw_response = True
            elif msg.get("type") == "error":
                raise WcpError(msg.get("message"))
