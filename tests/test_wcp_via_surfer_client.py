"""End-to-end test: drive SimVision through surfer-mcp's WCP client.

Closes the loop by pointing Paolo's upstream WCP client (the one normally
used to talk to Surfer) at our simvision-wcp server. Validates that
simvision-wcp is spec-conformant enough for a real, independently-developed
WCP client to work against it.

Flow:
  surfer_mcp.WcpClient ──TCP WCP──▶ simvision_wcp.WcpServer ──Tcl──▶ SimVision
"""

from __future__ import annotations

import os
import shutil

import pytest
import pytest_asyncio

from simvision_wcp.server import WcpServer
from surfer_mcp.wcp import WcpClient as SurferWcpClient


HERE = os.path.dirname(os.path.abspath(__file__))
VCD = os.path.join(HERE, "data", "tiny.vcd")


pytestmark = [
    pytest.mark.skipif(
        shutil.which("simvision") is None, reason="simvision not on PATH",
    ),
    pytest.mark.skipif(
        shutil.which("Xvfb") is None, reason="Xvfb not installed",
    ),
]


@pytest_asyncio.fixture(loop_scope="session")
async def wcp_server_port(sv):
    """simvision-wcp server sharing the session's SimVision, bound to an
    ephemeral port."""
    srv = WcpServer(port=0, headless=True)
    srv._sv = sv
    port = await srv.start()
    try:
        yield port
    finally:
        srv._sv = None
        await srv.close()


@pytest_asyncio.fixture(loop_scope="session")
async def surfer_client(wcp_server_port, monkeypatch):
    """surfer-mcp's WcpClient, dialled at our simvision-wcp.

    Uses the new WCP_SERVER_URL env var to switch surfer-mcp from its
    default "spawn Surfer and wait for dial-home" flow into "dial an
    existing WCP server" mode.
    """
    monkeypatch.setenv("WCP_SERVER_URL", str(wcp_server_port))
    client = SurferWcpClient()
    await client.start()
    try:
        yield client
    finally:
        await client.stop()


async def test_load_via_surfer_client(sv, surfer_client):
    """load_waveform through the surfer-mcp client reaches SimVision."""
    await sv.send("catch {database close waves}")
    # surfer-mcp's wire command name is `load` with a `source` field.
    await surfer_client.send_command("load", source=VCD)
    # simvision-wcp pins the database name to "waves" so WCP-style paths
    # (`waves:::tb.clk`) resolve.
    assert "waves" in await sv.send("database find")


async def test_add_variables_via_surfer_client(sv, surfer_client):
    """add_variables round-trips IDs through the spec-conformant path."""
    await sv.send("catch {database close waves}")
    await sv.send("catch {waveform close Via}")
    await sv.send("waveform new -name Via")
    await sv.send("waveform using Via")

    await surfer_client.send_command("load", source=VCD)
    resp = await surfer_client.send_command(
        "add_variables",
        variables=["waves:::tb.clk", "waves:::tb.counter"],
    )
    ids = resp["ids"]
    assert isinstance(ids, list) and len(ids) == 2


async def test_set_cursor_via_surfer_client(sv, surfer_client):
    """set_cursor flows all the way through."""
    await sv.send("catch {database close waves}")
    await sv.send("catch {foreach c [cursor find] {cursor delete $c}}")

    await surfer_client.send_command("load", source=VCD)
    await surfer_client.send_command("set_cursor", timestamp=55)

    from simvision_mcp.client import parse_tcl_list
    cursors = parse_tcl_list(await sv.send("cursor find"))
    assert cursors, "no cursor created"
    t = await sv.send(f"cursor get -using {cursors[0]} -time")
    assert "55" in t


async def test_clear_via_surfer_client(sv, surfer_client):
    """A command with no arguments works through the surfer-mcp path."""
    await sv.send("catch {database close waves}")
    await sv.send("catch {waveform close ViaClear}")
    await sv.send("waveform new -name ViaClear")
    await sv.send("waveform using ViaClear")

    await surfer_client.send_command("load", source=VCD)
    await surfer_client.send_command(
        "add_variables", variables=["waves:::tb.clk"],
    )
    await surfer_client.send_command("clear")
    remaining = await sv.send("waveform signals -using ViaClear")
    assert remaining.strip() == ""


async def test_reload_via_surfer_client(sv, surfer_client):
    """reload (no args) uses simvision-wcp's cached last-source."""
    await sv.send("catch {database close waves}")
    await surfer_client.send_command("load", source=VCD)
    await surfer_client.send_command("reload")
    # Just has to not error — simvision-wcp's cache did the work.
