"""PostScript → PNG/JPG/PDF rasterization helpers.

Pulled out of `simvision_mcp.server` so `simvision_wcp.server` can reuse the
same pipeline without reaching across packages into a private name.

Pure filesystem functions — no SimVision client involved — so they're easy to
unit-test with hand-crafted PS input.
"""

from __future__ import annotations

import os
import shutil
import subprocess


def rotate_image_90_cw(path: str) -> None:
    """Rotate an image file 90° clockwise in place.

    Only touches PNG/JPG — leaves PS/PDF alone since they have their own
    orientation metadata. Silently no-ops if Pillow isn't installed.
    """
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext not in ("png", "jpg", "jpeg"):
        return
    try:
        from PIL import Image as _PIL
    except ImportError:
        return
    with _PIL.open(path) as img:
        # PIL's `rotate` is CCW-positive; -90° is one step clockwise.
        img.rotate(-90, expand=True).save(path)


def rasterize_postscript(ps_path: str, output_path: str) -> str:
    """Convert a PostScript file to PNG/PDF/JPG based on output_path's extension.

    Prefers Ghostscript (`gs`), falls back to `ps2pdf` for PDFs or ImageMagick
    `convert`. Returns the output path on success, or a string starting with
    "Error:" on failure. If the caller requests a .ps extension, the PS file
    is simply copied to the destination.
    """
    abs_out = os.path.abspath(output_path)
    ext = abs_out.rsplit(".", 1)[-1].lower() if "." in abs_out else ""

    if not os.path.isfile(ps_path):
        return f"Error: no PostScript at {ps_path}"

    if ext == "ps":
        shutil.copy(ps_path, abs_out)
        return abs_out

    gs = shutil.which("gs")
    convert = shutil.which("convert") or shutil.which("magick")

    if ext == "pdf" and shutil.which("ps2pdf"):
        r = subprocess.run(
            ["ps2pdf", ps_path, abs_out],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and os.path.isfile(abs_out):
            return abs_out

    if gs and ext in ("png", "jpg", "jpeg", "pdf"):
        device = {"png": "png16m", "jpg": "jpeg", "jpeg": "jpeg", "pdf": "pdfwrite"}[ext]
        r = subprocess.run(
            [
                gs, "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dQUIET",
                f"-sDEVICE={device}", "-r150",
                f"-sOutputFile={abs_out}", ps_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and os.path.isfile(abs_out):
            return abs_out
        return f"Error: gs failed: {(r.stderr or r.stdout).strip()}"

    if convert:
        r = subprocess.run(
            [convert, "-density", "150", ps_path, abs_out],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and os.path.isfile(abs_out):
            return abs_out
        return f"Error: convert failed: {r.stderr.strip()}"

    return "Error: no rasterizer found (install ghostscript or imagemagick)"
