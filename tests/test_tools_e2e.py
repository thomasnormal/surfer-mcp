"""End-to-end tests that drive SimVision through a realistic MCP tool sequence.

Loads a handwritten VCD, lists its hierarchy, adds signals to a waveform window,
moves a cursor, zooms, and verifies state via queries. Reuses the session-scoped
`sv` fixture so the cold-start cost is paid once for the whole session.
"""

from __future__ import annotations

import os
import shutil

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
VCD = os.path.join(HERE, "data", "tiny.vcd")

pytestmark = [
    pytest.mark.skipif(
        shutil.which("simvision") is None,
        reason="simvision not on PATH",
    ),
    pytest.mark.skipif(
        not os.environ.get("DISPLAY"),
        reason="no X display available",
    ),
]


@pytest.fixture(scope="module")
def loaded_db(sv, aio):
    """Load the tiny VCD once for the module. Returns the logical database name."""
    # Using -name ensures a predictable handle ("tiny") for signal paths.
    # -overwrite re-translates any cached .trn/.dsn sitting next to the .vcd.
    aio(sv.send(f"catch {{database close tiny}}"))
    name = aio(sv.send(f"database open -name tiny -overwrite {VCD}"))
    assert name, f"database open returned empty: {name!r}"
    yield name
    aio(sv.send(f"catch {{database close tiny}}"))


def test_database_find_shows_loaded_db(loaded_db, sv, aio):
    assert "tiny" in aio(sv.send("database find"))


def test_scope_show_lists_tb_hierarchy(loaded_db, sv, aio):
    # After loading, the root contains the "tb" scope.
    # scope show returns full-path names of direct children (scopes + signals).
    top = aio(sv.send("scope show tiny::"))
    assert "tiny::tb" in top, f"tb scope not found under root: {top!r}"


def test_scope_show_tb_lists_signals(loaded_db, sv, aio):
    children = aio(sv.send("scope show tiny::tb"))
    # All three VCD signals should appear as full paths under tb.
    for sig in ("tiny::tb.clk", "tiny::tb.counter", "tiny::tb.valid"):
        assert sig in children, f"missing {sig!r} in {children!r}"


def test_list_scopes_excludes_buses(loaded_db, sv, aio):
    """`list_scopes` must not mis-classify a multi-bit bus as a scope.

    The tiny VCD has a `tb.counter[3:0]` bus. `scope show tb.counter` returns
    its bit slices, which naively looks like a scope with children. The
    classify_scope heuristic checks for "${item}[N]" child naming.
    """
    from simvision_mcp import server as srv
    scopes = aio(srv.list_scopes(parent="tiny::"))
    assert "tiny::tb" in scopes
    assert "tiny::tb.counter" not in scopes, (
        f"bus `counter` classified as scope; got: {scopes!r}"
    )


def test_normalize_signal_path():
    from simvision_mcp.server import _normalize_signal_path as norm
    assert norm("tiny::tb.clk") == "tiny:::tb.clk"
    assert norm("tiny:::tb.clk") == "tiny:::tb.clk"  # already correct, unchanged
    assert norm("db::scope.signal") == "db:::scope.signal"
    # Scope paths (no signal component) — leave alone since they stay double-colon.
    # Only the first db::part gets normalized.
    assert norm("bare_name") == "bare_name"


def test_parse_sv_time():
    from simvision_mcp.server import _parse_sv_time as p
    assert p("0(0)ns") == (0, "ns")
    assert p("100ns") == (100, "ns")
    assert p("1.5us") == (1.5, "us")
    assert p("-5ps") == (-5, "ps")
    assert p("bogus") == (None, "")


def test_add_signals_returns_qualified_paths(loaded_db, sv, aio):
    import json
    from simvision_mcp import server as srv
    aio(sv.send("catch {waveform close AddRet}"))
    aio(sv.send("waveform new -name AddRet"))
    # Provide scope-form (double colon) paths — tool should normalize.
    raw = aio(srv.add_signals(
        scope="tiny::tb", signals=["clk", "counter"], window="AddRet",
    ))
    qualified = json.loads(raw)
    assert qualified == ["tiny:::tb.clk", "tiny:::tb.counter"]
    aio(sv.send("waveform close AddRet"))


def test_add_signals_matching(loaded_db, sv, aio):
    import json
    from simvision_mcp import server as srv
    aio(sv.send("catch {waveform close Match}"))
    aio(sv.send("waveform new -name Match"))
    raw = aio(srv.add_signals_matching(
        parent="tiny::tb", pattern="*", window="Match",
    ))
    added = json.loads(raw)
    # Should include clk, counter, valid (bus treated as single signal).
    assert "tiny:::tb.clk" in added
    assert "tiny:::tb.counter" in added
    assert "tiny:::tb.valid" in added
    aio(sv.send("waveform close Match"))


def test_describe_session_has_cursor_times_and_viewports(loaded_db, sv, aio):
    import json
    from simvision_mcp import server as srv
    aio(sv.send("catch {waveform close EnrichWave}"))
    aio(sv.send("catch {foreach c [cursor find] {cursor delete $c}}"))
    aio(sv.send("waveform new -name EnrichWave"))
    aio(sv.send("cursor new -name MyCur -time 42ns"))
    aio(sv.send("waveform xview limits -using EnrichWave 0 80ns"))

    info = json.loads(aio(srv.describe_session()))

    # Waveform windows is now list of dicts with viewports.
    wins = info["waveform_windows"]
    assert any(w["name"] == "EnrichWave" for w in wins)
    w = next(w for w in wins if w["name"] == "EnrichWave")
    assert "viewport" in w
    assert len(w["viewport"]) == 2

    # Cursors is now list of dicts with times.
    cursors = info["cursors"]
    assert any(c["name"] == "MyCur" for c in cursors)
    assert any("42" in c.get("time", "") for c in cursors)

    aio(sv.send("waveform close EnrichWave"))


def test_tail_log_returns_recent_lines():
    from simvision_mcp import server as srv
    import asyncio
    out = asyncio.new_event_loop().run_until_complete(srv.tail_log(kind="mcp", lines=5))
    # Log exists and has some lines (we just made calls above it).
    assert isinstance(out, str)
    assert len(out.splitlines()) <= 5


def test_load_database_returns_summary(loaded_db, sv, aio, tmp_path):
    """The loaded_db fixture uses raw Tcl, so exercise the typed tool too.

    Uses a *copy* of the VCD because load_database defaults to overwrite=True,
    which would delete the translated `.trn`/`.dsn` siblings that
    `loaded_db`'s already-open `tiny` handle depends on.
    """
    import json
    import shutil as _sh
    from simvision_mcp import server as srv
    vcd2 = str(tmp_path / "fresh.vcd")
    _sh.copy(VCD, vcd2)
    raw = aio(srv.load_database(path=vcd2, name="tiny2"))
    info = json.loads(raw)
    assert info["name"] == "tiny2"
    assert info["path"] == vcd2
    assert info["scope_count"] == 1  # just `tb`
    assert info["signal_count"] == 3  # clk, counter, valid
    # Times should be normalized to scalar + time_unit (not "0(0)ns").
    assert info["start"] == 0
    assert info["end"] == 100
    assert info["time_unit"] == "ns"
    aio(sv.send("database close tiny2"))


def test_signal_values_at_time(loaded_db, sv, aio):
    import json
    from simvision_mcp import server as srv
    aio(sv.send("catch {waveform close SigV}"))
    aio(sv.send("waveform new -name SigV"))
    aio(sv.send(
        "waveform add -using SigV -signals {tiny:::tb.clk tiny:::tb.counter tiny:::tb.valid}"
    ))
    raw = aio(srv.signal_values(
        time="45ns",
        signals=["tiny:::tb.clk", "tiny:::tb.counter", "tiny:::tb.valid"],
        window="SigV",
    ))
    vals = json.loads(raw)
    assert set(vals) == {"tiny:::tb.clk", "tiny:::tb.counter", "tiny:::tb.valid"}
    # At 45ns: clk=0 (was high 30-40ns), counter=2, valid=1
    # Values come back in the current radix — don't over-assert specific formats,
    # just that we got non-empty strings back.
    for v in vals.values():
        assert v and isinstance(v, str)
    aio(sv.send("waveform close SigV"))


def test_set_cursor_returns_cursor_name(loaded_db, sv, aio):
    from simvision_mcp import server as srv
    aio(sv.send("catch {foreach c [cursor find] {cursor delete $c}}"))
    name = aio(srv.set_cursor(time="25ns"))
    assert name, f"set_cursor returned empty: {name!r}"
    assert name in aio(sv.send("cursor find"))


def test_add_signals_with_scope(loaded_db, sv, aio):
    import json
    from simvision_mcp import server as srv
    aio(sv.send("catch {waveform close Scoped}"))
    aio(sv.send("waveform new -name Scoped"))
    # Equivalent to adding tiny:::tb.clk, tiny:::tb.counter.
    raw = aio(srv.add_signals(
        scope="tiny:::tb", signals=["clk", "counter"], window="Scoped",
    ))
    added = json.loads(raw)
    assert added == ["tiny:::tb.clk", "tiny:::tb.counter"]
    aio(sv.send("waveform close Scoped"))


def test_describe_session_snapshot(loaded_db, sv, aio):
    """Smoke test — richer content is covered by test_describe_session_has_cursor_times_and_viewports."""
    import json
    from simvision_mcp import server as srv
    aio(sv.send("catch {waveform close DescWave}"))
    aio(sv.send("waveform new -name DescWave"))
    info = json.loads(aio(srv.describe_session()))
    assert "tiny" in info["databases"], info
    assert any(w.get("name") == "DescWave" for w in info["waveform_windows"]), info
    assert info["display"] and info["display"].startswith(":")
    aio(sv.send("waveform close DescWave"))


def test_get_log_paths():
    import json
    from simvision_mcp import server as srv
    import asyncio
    raw = asyncio.new_event_loop().run_until_complete(srv.get_log_paths())
    info = json.loads(raw)
    assert "mcp_log" in info and "simvision_stderr" in info
    assert info["mcp_log"].endswith("simvision-mcp.log")


def test_list_signals_treats_bus_as_single_signal(loaded_db, sv, aio):
    """`list_signals` should return the bus as a single signal, not enumerate slices.

    A designer thinks of `counter[3:0]` as one signal displayed in a waveform
    window as one row — not four independent bits. classify_scope's bus
    heuristic enforces this.
    """
    from simvision_mcp import server as srv
    signals = aio(srv.list_signals(parent="tiny::tb"))
    for sig in ("tiny::tb.clk", "tiny::tb.counter", "tiny::tb.valid"):
        assert sig in signals, f"missing {sig!r} in {signals!r}"
    assert "counter[3]" not in signals, (
        f"bus enumerated as slices instead of single signal: {signals!r}"
    )


def test_waveform_window_lifecycle(loaded_db, sv, aio):
    # Create a waveform window, add signals, verify they're present.
    w = aio(sv.send("waveform new -name E2EWave"))
    assert "E2EWave" in w

    aio(sv.send(
        f"waveform add -using E2EWave -signals {{tiny:::tb.clk tiny:::tb.counter tiny:::tb.valid}}"
    ))
    # waveform signals returns the IDs/names of what's displayed
    ids = aio(sv.send("waveform signals -using E2EWave"))
    # At least three entries — exact format is whitespace-separated
    assert len(ids.split()) >= 3, f"expected ≥3 signals in window, got: {ids!r}"

    # Clear and verify
    aio(sv.send("waveform clearall -using E2EWave"))
    ids_after = aio(sv.send("waveform signals -using E2EWave"))
    assert ids_after.strip() == "", f"expected empty, got: {ids_after!r}"

    aio(sv.send("waveform close E2EWave"))


def test_cursor_at_specific_time(loaded_db, sv, aio):
    # Clean up any prior cursor with this name, then create and verify.
    aio(sv.send("catch {cursor delete E2ECur}"))
    aio(sv.send("cursor new -name E2ECur -time 35ns"))
    t = aio(sv.send("cursor get -using E2ECur -time"))
    # SimVision returns the raw time with units; assert it starts with 35
    assert "35" in t, f"cursor time was {t!r}"
    aio(sv.send("cursor delete E2ECur"))


def test_zoom_range(loaded_db, sv, aio):
    # Create a waveform window just for the viewport check.
    aio(sv.send("catch {waveform close ZoomWave}"))
    aio(sv.send("waveform new -name ZoomWave"))
    aio(sv.send(
        "waveform add -using ZoomWave -signals {tiny:::tb.clk tiny:::tb.counter}"
    ))
    aio(sv.send("waveform xview limits -using ZoomWave 0 80ns"))
    limits = aio(sv.send("waveform xview limits -using ZoomWave"))
    # Expect two whitespace-separated time values (SimVision may reformat units).
    parts = limits.split()
    assert len(parts) == 2, f"xview limits returned unexpected: {limits!r}"
    aio(sv.send("waveform close ZoomWave"))


def test_postscript_snapshot(loaded_db, sv, aio, tmp_path):
    # `waveform print -file` writes PostScript. It's the built-in screenshot.
    aio(sv.send("catch {waveform close SnapWave}"))
    aio(sv.send("waveform new -name SnapWave"))
    aio(sv.send(
        "waveform add -using SnapWave -signals {tiny:::tb.clk tiny:::tb.counter tiny:::tb.valid}"
    ))
    aio(sv.send("waveform xview limits -using SnapWave 0 100ns"))

    ps = str(tmp_path / "snap.ps")
    aio(sv.send(f"waveform print -using SnapWave -file {ps}"))
    assert os.path.isfile(ps), f"no postscript file at {ps}"
    # PostScript files always start with "%!PS"
    with open(ps, "rb") as f:
        header = f.read(4)
    assert header == b"%!PS", f"not a PostScript file: {header!r}"

    aio(sv.send("waveform close SnapWave"))


@pytest.mark.skipif(
    shutil.which("gs") is None and shutil.which("convert") is None,
    reason="no gs or ImageMagick available for rasterization",
)
def test_png_screenshot(loaded_db, sv, aio, tmp_path):
    """End-to-end: the screenshot_waveform tool produces a valid PNG.

    The `sv` fixture has already wired server._client to the shared client,
    so this test calls the @mcp.tool function directly and it talks to our
    running SimVision without launching a second process.
    """
    from simvision_mcp import server as srv

    aio(sv.send("catch {waveform close PngWave}"))
    aio(sv.send("waveform new -name PngWave"))
    aio(sv.send(
        "waveform add -using PngWave -signals {tiny:::tb.clk tiny:::tb.counter tiny:::tb.valid}"
    ))
    aio(sv.send("waveform xview limits -using PngWave 0 100ns"))

    png = str(tmp_path / "snap.png")
    result = aio(srv.screenshot_waveform(png, "PngWave"))
    # Tool returns a list: [summary_text, Image(...)]. PNG path lives in the summary.
    assert isinstance(result, list), f"expected list, got {type(result).__name__}"
    assert any(png in str(part) for part in result), f"path missing in: {result!r}"
    assert any(hasattr(p, "data") and p.data[:4] == b"\x89PNG" for p in result), (
        f"no inline PNG in: {result!r}"
    )
    assert os.path.isfile(png)
    with open(png, "rb") as f:
        header = f.read(8)
    assert header[:4] == b"\x89PNG", f"not a PNG: {header!r}"

    aio(sv.send("waveform close PngWave"))


def _png_dimensions(path: str) -> tuple[int, int]:
    """Read width, height from PNG header — no Pillow needed."""
    import struct
    with open(path, "rb") as f:
        sig = f.read(8)
        assert sig == b"\x89PNG\r\n\x1a\n", f"not a PNG: {path}"
        length, typ = struct.unpack(">I4s", f.read(8))
        assert typ == b"IHDR", f"first chunk isn't IHDR: {typ}"
        w, h = struct.unpack(">II", f.read(8))
        return w, h


def test_screenshot_waveform_is_landscape(loaded_db, sv, aio, tmp_path):
    """The waveform PNG must be wider than tall — a waveform's natural
    shape has time flowing left-to-right across the long edge. Historical
    regression: SimVision's default PostScript output is portrait with
    time flowing down; the tool has to force landscape orientation.
    """
    from simvision_mcp import server as srv
    aio(sv.send("catch {waveform close LandscapeWave}"))
    aio(sv.send("waveform new -name LandscapeWave"))
    aio(sv.send(
        "waveform add -using LandscapeWave -signals"
        " {tiny:::tb.clk tiny:::tb.counter tiny:::tb.valid}"
    ))
    aio(sv.send("waveform xview limits -using LandscapeWave 0 100ns"))

    out = str(tmp_path / "landscape.png")
    aio(srv.screenshot_waveform(out, "LandscapeWave"))
    w, h = _png_dimensions(out)
    assert w > h, (
        f"expected landscape aspect, got {w}x{h} — waveform screenshot is "
        f"likely rotated 90° (SimVision's default PS is portrait)."
    )
    aio(sv.send("waveform close LandscapeWave"))
