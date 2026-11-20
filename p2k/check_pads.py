#!/usr/bin/env python3
"""Board pads of the Proteus-pad parts = the Proteus pad flashes and drills (needs pcbnew).

Guard for the pad decisions "these parts keep the Proteus pads" (PROTEUS_PADS): on every copper
layer a pad of such a part is on, the copper of the board pad (pcbnew polygon) must have the
bounding box (+-0.01 mm) and the area (+- CHECK_PADS_AREA_TOL) of the one Proteus gerber flash
within 0.05 mm of its centre, and a plated pad must have the Proteus drill (+-0.005 mm). The
Proteus side comes from the CADCAM gerbers, not from the IPC (which may record a pad with another
size), so a pad turned by 90 degrees, a square pad on a round flash, a KiCad pad size or a KiCad
drill fail here; the gerber round trip only classifies a pad shape difference and ipc356_diff
accepts larger KiCad drills.

Usage: tools/p2k/bin/kipy -m p2k.check_pads      (P2K_BOARD / P2K_PCB honoured)

p2k_config.py keys
  PROTEUS_PADS_CHECK   optional {refdes}: the parts checked, written out independently of the
                       footprint decisions (a part taken out of the footprint rules fails here);
                       default: PROTEUS_PADS (required when this key is absent)
  CHECK_PADS_AREA_TOL  optional relative area tolerance (default 0.06: KiCad's rounded corners on
                       an SMD pad with the Proteus size keep up to ~5 % less area than the
                       Proteus rectangle; a square pad on a round flash is +27 %)
"""
import math
import sys

import pcbnew

from . import board, gerber, pcb

POS_TOL, BOX_TOL, DRILL_TOL = 0.05, 0.01, 0.005
PAD_FUNCTIONS = ("ComponentPad", "SMDPad", "BGAPad")


def ap_shape(f):
    """(bbox w, bbox h, area) of a flash, gerber frame."""
    ap = f.ap
    if ap.shape == "C":
        d = ap.params[0]
        return d, d, math.pi * d * d / 4
    if ap.shape == "R":
        w, h = ap.params[:2]
        return w, h, w * h
    if ap.shape == "O":
        w, h = ap.params[:2]
        d = min(w, h)
        return w, h, w * h - d * d + math.pi * d * d / 4
    pts = ap.outline
    if not pts:
        raise SystemExit(f"check_pads: aperture {ap.shape} without a single outline")
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    area = abs(sum(pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1] for i in range(len(pts) - 1))) / 2
    return max(xs) - min(xs), max(ys) - min(ys), area


def parts(cfg):
    refs = getattr(cfg, "PROTEUS_PADS_CHECK", None)
    if refs is None:
        refs = getattr(cfg, "PROTEUS_PADS", None)
    if refs is None:
        raise SystemExit("check_pads: neither PROTEUS_PADS_CHECK nor PROTEUS_PADS in p2k_config.py")
    return list(refs)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        print(f"check_pads: unknown argument(s) {argv} (usage: kipy -m p2k.check_pads)")
        return 2
    cfg = board.config()
    refs = parts(cfg)
    area_tol = getattr(cfg, "CHECK_PADS_AREA_TOL", 0.06)
    layers = {klayer: [f for f in gerber.parse(pcb.gerber_path(gname)).of(gerber.Flash)
                       if f.dark and f.ap.function.startswith(PAD_FUNCTIONS)]
              for gname, klayer, _ in cfg.COPPER}
    drills = gerber.parse(pcb.gerber_path(cfg.DRILL_PLATED)).of(gerber.Flash)
    brd = pcbnew.LoadBoard(pcb.board_file())
    fps = {fp.GetReference(): fp for fp in brd.GetFootprints()}
    errors, n_pads, n_layers = [], 0, 0
    for ref in refs:
        fp = fps.get(ref)
        if fp is None:
            errors.append(f"{ref}: not on the board")
            continue
        for pad in fp.Pads():
            n_pads += 1
            x, y = pcbnew.ToMM(pad.GetPosition().x), -pcbnew.ToMM(pad.GetPosition().y)
            name = f"{ref}-{pad.GetNumber()}"
            for klayer, flashes in layers.items():
                lid = brd.GetLayerID(klayer)
                if not pad.IsOnLayer(lid):
                    continue
                near = [f for f in flashes if math.hypot(f.x - x, f.y - y) <= POS_TOL]
                if len(near) != 1:
                    errors.append(f"{name} {klayer}: {len(near)} Proteus pad flashes within {POS_TOL} mm")
                    continue
                pw, ph, pa = ap_shape(near[0])
                poly = pad.GetEffectivePolygon(lid, pcbnew.ERROR_INSIDE)
                bb = poly.BBox()
                kw, kh = pcbnew.ToMM(bb.GetWidth()), pcbnew.ToMM(bb.GetHeight())
                ka = poly.Area() / 1e12   # nm^2 -> mm^2
                n_layers += 1
                if abs(kw - pw) > BOX_TOL or abs(kh - ph) > BOX_TOL or abs(ka - pa) > area_tol * pa:
                    errors.append(f"{name} {klayer}: board pad {kw:.3f} x {kh:.3f} mm, {ka:.3f} mm^2; "
                                  f"Proteus flash ({near[0].ap.shape}) {pw:.3f} x {ph:.3f} mm, {pa:.3f} mm^2")
            if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH:
                near = [f for f in drills if math.hypot(f.x - x, f.y - y) <= POS_TOL]
                kd = pcbnew.ToMM(pad.GetDrillSize().x)
                if len(near) != 1:
                    errors.append(f"{name}: {len(near)} Proteus drills within {POS_TOL} mm")
                elif abs(near[0].ap.params[0] - kd) > DRILL_TOL:
                    errors.append(f"{name}: drill board {kd:.3f} mm, Proteus {near[0].ap.params[0]:.4f} mm")
    for e in errors:
        print("check_pads: " + e)
    print(f"check_pads: {len(refs)} parts, {n_pads} pads, {n_layers} pad layers compared "
          f"with the Proteus flashes and drills: " + ("OK" if not errors else f"FAILED ({len(errors)})"))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
