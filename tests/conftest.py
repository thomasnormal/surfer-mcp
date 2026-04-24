"""Pytest configuration — session-scoped SimVision fixture.

Cold-starting SimVision takes ~8s under headless Xvfb (and ~120s on an
ssh-forwarded display), so we launch it once per test session and reuse
the control socket across every test that needs it.

Tests that touch SimVision are `async def test_…` and take the `sv`
fixture. Pure-Python tests stay plain `def` — pytest-asyncio's `auto`
mode leaves them alone.
"""

from __future__ import annotations

import os
import sys

import pytest
import pytest_asyncio

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from simvision_mcp.client import SimVisionClient  # noqa: E402


SESSION_NAME = "test"


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def sv():
    """Shared SimVisionClient registered as session "test".

    Headless by default — Xvfb boot is ~8s. Registers the client in
    `server._sessions` so tests can invoke @mcp.tool functions with
    session="test" (or leave session=None if it's the only active session).
    """
    from simvision_mcp import server as srv  # noqa: E402

    client = SimVisionClient(headless=True)
    await client.start()
    srv._sessions[SESSION_NAME] = client
    try:
        yield client
    finally:
        await client.stop()
        srv._sessions.pop(SESSION_NAME, None)
