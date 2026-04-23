"""Unit tests for PostScript rasterization — runs without SimVision."""

from __future__ import annotations

import os
import shutil
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from simvision_mcp.server import _rasterize_postscript  # noqa: E402


MINIMAL_PS = b"""%!PS-Adobe-3.0
%%BoundingBox: 0 0 200 100
/Helvetica findfont 24 scalefont setfont
50 40 moveto (hello) show
showpage
"""


@pytest.fixture
def ps_file(tmp_path):
    p = tmp_path / "in.ps"
    p.write_bytes(MINIMAL_PS)
    return str(p)


def test_ps_passthrough(ps_file, tmp_path):
    out = str(tmp_path / "out.ps")
    result = _rasterize_postscript(ps_file, out)
    assert result == out
    with open(out, "rb") as f:
        assert f.read(4) == b"%!PS"


@pytest.mark.skipif(shutil.which("gs") is None, reason="ghostscript not installed")
def test_png_via_gs(ps_file, tmp_path):
    out = str(tmp_path / "out.png")
    result = _rasterize_postscript(ps_file, out)
    assert result == out, result
    with open(out, "rb") as f:
        assert f.read(4) == b"\x89PNG"


@pytest.mark.skipif(shutil.which("gs") is None, reason="ghostscript not installed")
def test_pdf_via_gs_or_ps2pdf(ps_file, tmp_path):
    out = str(tmp_path / "out.pdf")
    result = _rasterize_postscript(ps_file, out)
    assert result == out, result
    with open(out, "rb") as f:
        assert f.read(4) == b"%PDF"


def test_missing_input(tmp_path):
    result = _rasterize_postscript(str(tmp_path / "nope.ps"), str(tmp_path / "out.png"))
    assert result.startswith("Error:")
