"""Tests for the session-management tools and multi-session behaviour."""

from __future__ import annotations

import json
import os
import shutil

import pytest

from simvision_mcp.client import SimVisionError

pytestmark = [
    pytest.mark.skipif(shutil.which("simvision") is None, reason="no simvision"),
    pytest.mark.skipif(shutil.which("Xvfb") is None, reason="no Xvfb"),
]


async def test_session_resolution_single(sv):
    """With only the fixture's session registered, tools pick it up with session=None."""
    from simvision_mcp import server as srv

    r = await srv.tcl_eval("expr 2+2")
    assert r == "4"


async def test_session_resolution_explicit(sv):
    from simvision_mcp import server as srv

    r = await srv.tcl_eval("expr 3+4", session="test")
    assert r == "7"


async def test_session_resolution_unknown_errors(sv):
    from simvision_mcp import server as srv
    with pytest.raises(SimVisionError) as exc_info:
        await srv.tcl_eval("expr 1", session="nope")
    assert "unknown session" in str(exc_info.value)


async def test_list_sessions_json(sv):
    from simvision_mcp import server as srv
    r = await srv.list_sessions()
    data = json.loads(r)
    names = {entry["name"] for entry in data}
    assert "test" in names
    test_entry = next(e for e in data if e["name"] == "test")
    assert test_entry["running"] is True
    assert test_entry["headless"] is True
    assert test_entry["display"] and test_entry["display"].startswith(":")


async def test_second_session_is_independent(sv):
    """Creating a second session should work; each has its own DB and windows."""
    from simvision_mcp import server as srv

    await srv.create_session(name="extra", headless=True)
    try:
        # With two sessions active, session-less calls must error.
        with pytest.raises(SimVisionError) as exc_info:
            await srv.tcl_eval("expr 1")
        assert "multiple" in str(exc_info.value)

        # Both sessions work individually.
        assert await srv.tcl_eval("expr 2*3", session="test") == "6"
        assert await srv.tcl_eval("expr 2*3", session="extra") == "6"

        # Databases in one shouldn't leak to the other.
        info = json.loads(await srv.describe_session(session="extra"))
        assert info["databases"] == []
    finally:
        await srv.close_session("extra")

    # After close, the single-session shortcut works again.
    assert await srv.tcl_eval("expr 1") == "1"


def _has_xlib() -> bool:
    try:
        import Xlib  # noqa: F401
        return True
    except ImportError:
        return False


def _has_mss() -> bool:
    try:
        import mss  # noqa: F401
        return True
    except ImportError:
        return False


def _has_pillow() -> bool:
    try:
        from PIL import Image  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _has_xlib(), reason="python-xlib not installed")
async def test_list_sim_windows(sv):
    from simvision_mcp import server as srv
    titles = await srv.list_sim_windows()
    assert "Console" in titles, f"no Console window in: {titles!r}"


@pytest.mark.skipif(not _has_mss(), reason="mss not installed")
async def test_screenshot_gui_root(sv, tmp_path):
    """Grab the whole Xvfb root — should produce a valid PNG."""
    from simvision_mcp import server as srv
    out = str(tmp_path / "root.png")
    r = await srv.screenshot_gui(out)
    assert isinstance(r, list), f"expected list, got {type(r).__name__}"
    assert any(out in str(p) for p in r)
    assert os.path.isfile(out)
    with open(out, "rb") as f:
        assert f.read(4) == b"\x89PNG"


@pytest.mark.skipif(
    not (_has_xlib() and _has_pillow()),
    reason="need python-xlib and Pillow",
)
async def test_screenshot_gui_by_title(sv, tmp_path):
    """Grab the Console window by substring match."""
    from simvision_mcp import server as srv
    out = str(tmp_path / "console.png")
    r = await srv.screenshot_gui(out, window_title="Console")
    assert isinstance(r, list), f"expected list, got {r!r}"
    assert any(out in str(p) for p in r)
    assert os.path.isfile(out)
    with open(out, "rb") as f:
        assert f.read(4) == b"\x89PNG"
