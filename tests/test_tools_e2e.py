"""End-to-end tests that drive SimVision through a realistic MCP tool sequence.

Loads a handwritten VCD, lists its hierarchy, adds signals to a waveform window,
moves a cursor, zooms, and verifies state via queries. Reuses the session-scoped
`sv` fixture so the cold-start cost is paid once for the whole session.
"""

from __future__ import annotations

import json
import os
import shutil
import struct

import pytest
import pytest_asyncio

HERE = os.path.dirname(os.path.abspath(__file__))
VCD = os.path.join(HERE, "data", "tiny.vcd")

pytestmark = pytest.mark.skipif(
    shutil.which("simvision") is None, reason="simvision not on PATH",
)


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def loaded_db(sv):
    """Load the tiny VCD once for the module. Returns the logical database name."""
    # -overwrite re-translates any cached .trn/.dsn sitting next to the .vcd.
    await sv.send("catch {database close tiny}")
    name = await sv.send(f"database open -name tiny -overwrite {VCD}")
    assert name, f"database open returned empty: {name!r}"
    yield name
    await sv.send("catch {database close tiny}")


async def test_database_find_shows_loaded_db(loaded_db, sv):
    assert "tiny" in await sv.send("database find")


async def test_scope_show_lists_tb_hierarchy(loaded_db, sv):
    top = await sv.send("scope show tiny::")
    assert "tiny::tb" in top, f"tb scope not found under root: {top!r}"


async def test_scope_show_tb_lists_signals(loaded_db, sv):
    children = await sv.send("scope show tiny::tb")
    for sig in ("tiny::tb.clk", "tiny::tb.counter", "tiny::tb.valid"):
        assert sig in children, f"missing {sig!r} in {children!r}"


async def test_list_scopes_excludes_buses(loaded_db, sv):
    """`list_scopes` must not mis-classify a multi-bit bus as a scope.

    `scope show tb.counter` returns the bit slices, which naively looks
    like a scope with children. classify_scope's `${item}[N]` heuristic
    detects this.
    """
    from simvision_mcp import server as srv
    scopes = await srv.list_scopes(parent="tiny::")
    assert "tiny::tb" in scopes
    assert "tiny::tb.counter" not in scopes, (
        f"bus `counter` classified as scope; got: {scopes!r}"
    )


def test_normalize_signal_path():
    from simvision_mcp.server import _normalize_signal_path as norm
    assert norm("tiny::tb.clk") == "tiny:::tb.clk"
    assert norm("tiny:::tb.clk") == "tiny:::tb.clk"  # already correct, unchanged
    assert norm("db::scope.signal") == "db:::scope.signal"
    assert norm("bare_name") == "bare_name"


def test_parse_sv_time():
    from simvision_mcp.server import _parse_sv_time as p
    assert p("0(0)ns") == (0, "ns")
    assert p("100ns") == (100, "ns")
    assert p("1.5us") == (1.5, "us")
    assert p("-5ps") == (-5, "ps")
    assert p("bogus") == (None, "")


async def test_add_signals_returns_qualified_paths(loaded_db, sv):
    from simvision_mcp import server as srv
    await sv.send("catch {waveform close AddRet}")
    await sv.send("waveform new -name AddRet")
    raw = await srv.add_signals(
        scope="tiny::tb", signals=["clk", "counter"], window="AddRet",
    )
    qualified = json.loads(raw)
    assert qualified == ["tiny:::tb.clk", "tiny:::tb.counter"]
    await sv.send("waveform close AddRet")


async def test_add_signals_matching(loaded_db, sv):
    from simvision_mcp import server as srv
    await sv.send("catch {waveform close Match}")
    await sv.send("waveform new -name Match")
    raw = await srv.add_signals_matching(
        parent="tiny::tb", pattern="*", window="Match",
    )
    added = json.loads(raw)
    assert "tiny:::tb.clk" in added
    assert "tiny:::tb.counter" in added
    assert "tiny:::tb.valid" in added
    await sv.send("waveform close Match")


async def test_describe_session_has_cursor_times_and_viewports(loaded_db, sv):
    from simvision_mcp import server as srv
    await sv.send("catch {waveform close EnrichWave}")
    await sv.send("catch {foreach c [cursor find] {cursor delete $c}}")
    await sv.send("waveform new -name EnrichWave")
    await sv.send("cursor new -name MyCur -time 42ns")
    await sv.send("waveform xview limits -using EnrichWave 0 80ns")

    info = json.loads(await srv.describe_session())

    wins = info["waveform_windows"]
    assert any(w["name"] == "EnrichWave" for w in wins)
    w = next(w for w in wins if w["name"] == "EnrichWave")
    assert "viewport" in w
    assert len(w["viewport"]) == 2

    cursors = info["cursors"]
    assert any(c["name"] == "MyCur" for c in cursors)
    assert any("42" in c.get("time", "") for c in cursors)

    await sv.send("waveform close EnrichWave")


async def test_tail_log_returns_recent_lines(sv):
    from simvision_mcp import server as srv
    out = await srv.tail_log(kind="mcp", lines=5)
    assert isinstance(out, str)
    assert len(out.splitlines()) <= 5


async def test_load_database_returns_summary(loaded_db, sv, tmp_path):
    """Uses a *copy* of the VCD because load_database defaults to overwrite=True,
    which would delete the translated `.trn`/`.dsn` siblings that
    `loaded_db`'s already-open `tiny` handle depends on.
    """
    from simvision_mcp import server as srv
    vcd2 = str(tmp_path / "fresh.vcd")
    shutil.copy(VCD, vcd2)
    raw = await srv.load_database(path=vcd2, name="tiny2")
    info = json.loads(raw)
    assert info["name"] == "tiny2"
    assert info["path"] == vcd2
    assert info["scope_count"] == 1  # just `tb`
    assert info["signal_count"] == 3  # clk, counter, valid
    assert info["start"] == 0
    assert info["end"] == 100
    assert info["time_unit"] == "ns"
    await sv.send("database close tiny2")


async def test_signal_values_at_time(loaded_db, sv):
    from simvision_mcp import server as srv
    await sv.send("catch {waveform close SigV}")
    await sv.send("waveform new -name SigV")
    await sv.send(
        "waveform add -using SigV -signals {tiny:::tb.clk tiny:::tb.counter tiny:::tb.valid}"
    )
    raw = await srv.signal_values(
        time="45ns",
        signals=["tiny:::tb.clk", "tiny:::tb.counter", "tiny:::tb.valid"],
        window="SigV",
    )
    vals = json.loads(raw)
    assert set(vals) == {"tiny:::tb.clk", "tiny:::tb.counter", "tiny:::tb.valid"}
    for v in vals.values():
        assert v and isinstance(v, str)
    await sv.send("waveform close SigV")


async def test_set_cursor_returns_cursor_name(loaded_db, sv):
    from simvision_mcp import server as srv
    await sv.send("catch {foreach c [cursor find] {cursor delete $c}}")
    name = await srv.set_cursor(time="25ns")
    assert name, f"set_cursor returned empty: {name!r}"
    assert name in await sv.send("cursor find")


async def test_add_signals_with_scope(loaded_db, sv):
    from simvision_mcp import server as srv
    await sv.send("catch {waveform close Scoped}")
    await sv.send("waveform new -name Scoped")
    raw = await srv.add_signals(
        scope="tiny:::tb", signals=["clk", "counter"], window="Scoped",
    )
    added = json.loads(raw)
    assert added == ["tiny:::tb.clk", "tiny:::tb.counter"]
    await sv.send("waveform close Scoped")


async def test_describe_session_snapshot(loaded_db, sv):
    """Smoke test — richer content is covered by test_describe_session_has_cursor_times_and_viewports."""
    from simvision_mcp import server as srv
    await sv.send("catch {waveform close DescWave}")
    await sv.send("waveform new -name DescWave")
    info = json.loads(await srv.describe_session())
    assert "tiny" in info["databases"], info
    assert any(w.get("name") == "DescWave" for w in info["waveform_windows"]), info
    assert info["display"] and info["display"].startswith(":")
    await sv.send("waveform close DescWave")


async def test_get_log_paths():
    from simvision_mcp import server as srv
    raw = await srv.get_log_paths()
    info = json.loads(raw)
    assert "mcp_log" in info and "simvision_stderr" in info
    assert info["mcp_log"].endswith("simvision-mcp.log")


async def test_list_signals_treats_bus_as_single_signal(loaded_db, sv):
    """A bus should appear as a single signal, not enumerated bit slices."""
    from simvision_mcp import server as srv
    signals = await srv.list_signals(parent="tiny::tb")
    for sig in ("tiny::tb.clk", "tiny::tb.counter", "tiny::tb.valid"):
        assert sig in signals, f"missing {sig!r} in {signals!r}"
    assert "counter[3]" not in signals, (
        f"bus enumerated as slices instead of single signal: {signals!r}"
    )


async def test_waveform_window_lifecycle(loaded_db, sv):
    w = await sv.send("waveform new -name E2EWave")
    assert "E2EWave" in w

    await sv.send(
        "waveform add -using E2EWave -signals"
        " {tiny:::tb.clk tiny:::tb.counter tiny:::tb.valid}"
    )
    ids = await sv.send("waveform signals -using E2EWave")
    assert len(ids.split()) >= 3, f"expected ≥3 signals in window, got: {ids!r}"

    await sv.send("waveform clearall -using E2EWave")
    ids_after = await sv.send("waveform signals -using E2EWave")
    assert ids_after.strip() == "", f"expected empty, got: {ids_after!r}"

    await sv.send("waveform close E2EWave")


async def test_cursor_at_specific_time(loaded_db, sv):
    await sv.send("catch {cursor delete E2ECur}")
    await sv.send("cursor new -name E2ECur -time 35ns")
    t = await sv.send("cursor get -using E2ECur -time")
    assert "35" in t, f"cursor time was {t!r}"
    await sv.send("cursor delete E2ECur")


async def test_zoom_range(loaded_db, sv):
    await sv.send("catch {waveform close ZoomWave}")
    await sv.send("waveform new -name ZoomWave")
    await sv.send(
        "waveform add -using ZoomWave -signals {tiny:::tb.clk tiny:::tb.counter}"
    )
    await sv.send("waveform xview limits -using ZoomWave 0 80ns")
    limits = await sv.send("waveform xview limits -using ZoomWave")
    parts = limits.split()
    assert len(parts) == 2, f"xview limits returned unexpected: {limits!r}"
    await sv.send("waveform close ZoomWave")


async def test_postscript_snapshot(loaded_db, sv, tmp_path):
    """`waveform print -file` writes PostScript — the built-in screenshot."""
    await sv.send("catch {waveform close SnapWave}")
    await sv.send("waveform new -name SnapWave")
    await sv.send(
        "waveform add -using SnapWave -signals {tiny:::tb.clk tiny:::tb.counter tiny:::tb.valid}"
    )
    await sv.send("waveform xview limits -using SnapWave 0 100ns")

    ps = str(tmp_path / "snap.ps")
    await sv.send(f"waveform print -using SnapWave -file {ps}")
    assert os.path.isfile(ps), f"no postscript file at {ps}"
    with open(ps, "rb") as f:
        header = f.read(4)
    assert header == b"%!PS", f"not a PostScript file: {header!r}"

    await sv.send("waveform close SnapWave")


@pytest.mark.skipif(
    shutil.which("gs") is None and shutil.which("convert") is None,
    reason="no gs or ImageMagick available for rasterization",
)
async def test_png_screenshot(loaded_db, sv, tmp_path):
    """End-to-end: the screenshot_waveform tool produces a valid PNG."""
    from simvision_mcp import server as srv

    await sv.send("catch {waveform close PngWave}")
    await sv.send("waveform new -name PngWave")
    await sv.send(
        "waveform add -using PngWave -signals {tiny:::tb.clk tiny:::tb.counter tiny:::tb.valid}"
    )
    await sv.send("waveform xview limits -using PngWave 0 100ns")

    png = str(tmp_path / "snap.png")
    result = await srv.screenshot_waveform(png, "PngWave")
    assert isinstance(result, list), f"expected list, got {type(result).__name__}"
    assert any(png in str(part) for part in result), f"path missing in: {result!r}"
    assert any(hasattr(p, "data") and p.data[:4] == b"\x89PNG" for p in result), (
        f"no inline PNG in: {result!r}"
    )
    assert os.path.isfile(png)
    with open(png, "rb") as f:
        header = f.read(8)
    assert header[:4] == b"\x89PNG", f"not a PNG: {header!r}"

    await sv.send("waveform close PngWave")


def _png_dimensions(path: str) -> tuple[int, int]:
    """Read width, height from PNG header — no Pillow needed."""
    with open(path, "rb") as f:
        sig = f.read(8)
        assert sig == b"\x89PNG\r\n\x1a\n", f"not a PNG: {path}"
        f.read(8)  # length + chunk type ('IHDR')
        w, h = struct.unpack(">II", f.read(8))
        return w, h


async def test_screenshot_waveform_is_landscape(loaded_db, sv, tmp_path):
    """The waveform PNG must be wider than tall. SimVision's default PS is
    portrait with time flowing down; screenshot_waveform compensates.
    """
    from simvision_mcp import server as srv
    await sv.send("catch {waveform close LandscapeWave}")
    await sv.send("waveform new -name LandscapeWave")
    await sv.send(
        "waveform add -using LandscapeWave -signals"
        " {tiny:::tb.clk tiny:::tb.counter tiny:::tb.valid}"
    )
    await sv.send("waveform xview limits -using LandscapeWave 0 100ns")

    out = str(tmp_path / "landscape.png")
    await srv.screenshot_waveform(out, "LandscapeWave")
    w, h = _png_dimensions(out)
    assert w > h, (
        f"expected landscape aspect, got {w}x{h} — waveform screenshot is "
        f"likely rotated 90° (SimVision's default PS is portrait)."
    )
    await sv.send("waveform close LandscapeWave")
