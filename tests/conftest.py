"""Pytest configuration — session-scoped SimVision fixture.

Cold-starting SimVision takes ~120s, so we launch it once per test session and
reuse the same control socket across every test that needs it.

Each test function can take the `sv` fixture to get the connected client.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from simvision_mcp.client import SimVisionClient  # noqa: E402


@pytest.fixture(scope="session")
def event_loop():
    """A single event loop for the entire test session, so the session-scoped
    fixture's coroutines and each test's coroutines share it."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


SESSION_NAME = "test"


@pytest.fixture(scope="session")
def sv(event_loop):
    """Shared SimVisionClient registered as session "test".

    Headless by default — Xvfb boot is ~8s. Registers the client in
    `server._sessions` so tests can invoke @mcp.tool functions with
    session="test" (or leave session=None if it's the only active session).
    """
    from simvision_mcp import server as srv  # noqa: E402

    client = SimVisionClient(headless=True)
    event_loop.run_until_complete(client.start())
    srv._sessions[SESSION_NAME] = client
    try:
        yield client
    finally:
        event_loop.run_until_complete(client.stop())
        srv._sessions.pop(SESSION_NAME, None)


@pytest.fixture(scope="session")
def aio(event_loop):
    """Run an async test body on the shared session loop.

    Session-scoped so module- and session-scoped fixtures can request it.

    Usage:
        def test_something(sv, aio):
            r = aio(sv.send("expr 1+2"))
            assert r == "3"
    """
    def _run(coro):
        return event_loop.run_until_complete(coro)
    return _run
