#!/usr/bin/env python3
"""Render one sub-sheet to PNG for a visual check.

  p2k render_sheet kicad/02_fpga_connector.kicad_sch [out.png] [--width 3000]

Uses kicad-cli (SVG export of that single sheet file) and librsvg + cairo via ctypes
(no SVG converter is installed). Default output: reports/render/<sheet>.png
"""
import ctypes
import glob
import os
import subprocess
import sys
import tempfile

from . import board


def svg_to_png(svg, png, width):
    rsvg = ctypes.CDLL("librsvg-2.so.2")
    cairo = ctypes.CDLL("libcairo.so.2")
    gobj = ctypes.CDLL("libgobject-2.0.so.0")
    rsvg.rsvg_handle_new_from_file.restype = ctypes.c_void_p
    rsvg.rsvg_handle_new_from_file.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
    h = rsvg.rsvg_handle_new_from_file(svg.encode(), None)
    if not h:
        raise SystemExit(f"rsvg could not load {svg}")

    class Dim(ctypes.Structure):
        _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int), ("em", ctypes.c_double), ("ex", ctypes.c_double)]
    dim = Dim()
    rsvg.rsvg_handle_get_dimensions.argtypes = [ctypes.c_void_p, ctypes.POINTER(Dim)]
    rsvg.rsvg_handle_get_dimensions(h, ctypes.byref(dim))
    scale = width / dim.width
    height = int(dim.height * scale)
    cairo.cairo_image_surface_create.restype = ctypes.c_void_p
    cairo.cairo_create.restype = ctypes.c_void_p
    cairo.cairo_create.argtypes = [ctypes.c_void_p]
    cairo.cairo_scale.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
    cairo.cairo_set_source_rgb.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double, ctypes.c_double]
    cairo.cairo_paint.argtypes = [ctypes.c_void_p]
    cairo.cairo_surface_write_to_png.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    rsvg.rsvg_handle_render_cairo.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    surf = cairo.cairo_image_surface_create(0, width, height)  # 0 = ARGB32
    cr = cairo.cairo_create(surf)
    cairo.cairo_set_source_rgb(cr, 1, 1, 1)
    cairo.cairo_paint(cr)
    cairo.cairo_scale(cr, scale, scale)
    rsvg.rsvg_handle_render_cairo(h, cr)
    cairo.cairo_surface_write_to_png(surf, png.encode())
    return width, height


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    width = 3000
    if "--width" in sys.argv:
        width = int(sys.argv[sys.argv.index("--width") + 1])
        args = [a for a in args if a != str(width)]
    sch = args[0]
    out = args[1] if len(args) > 1 else board.path("reports", "render",
                                                      os.path.basename(sch).replace(".kicad_sch", ".png"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        subprocess.run([os.path.join(board.BIN, "kicad-cli"), "sch", "export", "svg",
                        "--no-background-color", "-o", td, sch], check=True, stdout=subprocess.DEVNULL)
        svgs = glob.glob(os.path.join(td, "*.svg"))
        if len(svgs) != 1:
            raise SystemExit(f"expected one svg, got {svgs}")
        w, h = svg_to_png(svgs[0], out, width)
    print(f"render: {out} ({w}x{h})")


if __name__ == "__main__":
    main()
