#!/usr/bin/env python3
"""Cross-check the Proteus Gerber X2 export against the IPC-D-356A netlist.

  * every file parses with tools/gerber.py and carries the same %TF.SameCoordinates id;
  * per copper layer: ViaPad flashes = IPC vias, ComponentPad flashes = IPC component THT
    pads, OtherPad flashes = IPC free pads + holes, SMDPad flashes = IPC SMD pads of that
    side, each flash within TOL of an IPC record of the right kind;
  * drill gerbers: ViaDrill / ComponentDrill / plated OtherDrill (free pads, plated holes) /
    NonPlated (unplated holes) flashes match the IPC drills (position within TOL, diameter
    equal);
  * the outline gerber spans the OUTLINE_RECT of p2k_config.py.

Config keys: CADCAM, COPPER, DRILL_PLATED, DRILL_NONPLATED (None: no unplated drill file),
OUTLINE, OUTLINE_RECT; HOLES_PLATED (ipc356) says which drill file has the mounting holes.
Stdlib only (reads reference/ipc356_pads.json, so run `p2k ipc356` first).
"""
import glob
import json
import os
import re
import sys
from collections import Counter

from . import board, gerber

CFG = board.config()
CADCAM = board.path(CFG.CADCAM)
TOL = 0.01  # mm
SIDE = {"F.Cu": "top", "B.Cu": "bottom"}


def path(name):
    p = glob.glob(os.path.join(CADCAM, f"* - CADCAM {name}.GBR"))
    assert len(p) == 1, (name, p)
    return p[0]


def match(flashes, records, what, errors, drill=False):
    """Every flash has a record within TOL and vice versa (1:1 by nearest)."""
    left = list(records)
    for f in flashes:
        best = min(left, key=lambda r: (r["x_mm"] - f.x) ** 2 + (r["y_mm"] - f.y) ** 2, default=None)
        if best is None or abs(best["x_mm"] - f.x) > TOL or abs(best["y_mm"] - f.y) > TOL:
            errors.append(f"{what}: flash at ({f.x:.3f}, {f.y:.3f}) has no IPC record")
            continue
        if drill and abs(f.ap.diameter - best["drill"]) > 0.001:
            errors.append(f"{what}: drill {f.ap.diameter} at ({f.x:.3f}, {f.y:.3f}), IPC {best['drill']}")
        left.remove(best)
    for r in left:
        errors.append(f"{what}: IPC record {r['refdes']}-{r['pin']} ({r['x_mm']}, {r['y_mm']}) has no flash")


def main():
    with open(board.path("reference", "ipc356_pads.json")) as f:
        pads = json.load(f)["pads"]
    by_kind = lambda *k: [p for p in pads if p["kind"] in k]
    tht_pads = [p for p in pads if p["kind"] == "pad" and p["is_tht"]]
    errors = []
    summary = []

    files = sorted(glob.glob(os.path.join(CADCAM, "*.GBR")))
    ids = Counter()
    for fpath in files:
        text = open(fpath).read()
        m = re.search(r"%TF\.SameCoordinates,([^*]*)\*%", text)
        ids[m.group(1) if m else None] += 1
        gerber.parse(fpath)
    if len(ids) != 1 or None in ids:
        errors.append(f"SameCoordinates ids differ: {dict(ids)}")
    summary.append(f"{len(files)} gerbers, one SameCoordinates id")

    for name, layer, _user in CFG.COPPER:
        side = SIDE.get(layer)
        g = gerber.parse(path(name))
        fl = [o for o in g.objects if isinstance(o, gerber.Flash)]
        fn = lambda prefix: [f for f in fl if f.ap.function.startswith(prefix)]
        match(fn("ViaPad"), by_kind("via"), f"{name} ViaPad", errors)
        match(fn("ComponentPad"), tht_pads, f"{name} ComponentPad", errors)
        match(fn("OtherPad"), by_kind("free_pad", "hole"), f"{name} OtherPad", errors)
        smd = [p for p in pads if not p["is_tht"] and p["side"] == side]
        match(fn("SMDPad"), smd, f"{name} SMDPad", errors)
        other = [f for f in fl if not f.ap.function.startswith(("ViaPad", "ComponentPad", "OtherPad", "SMDPad"))]
        if other:
            errors.append(f"{name}: {len(other)} flashes of unexpected function")
        summary.append(f"{name}: " + ", ".join(f"{k} {v}" for k, v in sorted(
            Counter(type(o).__name__ + ":" + (o.ap.function if hasattr(o, "ap") else "-") for o in g.objects).items())))

    g = gerber.parse(path(CFG.DRILL_PLATED))
    fl = [o for o in g.objects if isinstance(o, gerber.Flash)]
    match([f for f in fl if f.ap.function == "ViaDrill"], by_kind("via"), "ViaDrill", errors, True)
    match([f for f in fl if f.ap.function == "ComponentDrill"], tht_pads, "ComponentDrill", errors, True)
    plated_other = by_kind("free_pad", "hole") if CFG.HOLES_PLATED else by_kind("free_pad")
    match([f for f in fl if f.ap.function.startswith("OtherDrill")], plated_other, "plated OtherDrill", errors, True)
    unplated = []
    if CFG.DRILL_NONPLATED:
        g = gerber.parse(path(CFG.DRILL_NONPLATED))
        unplated = [o for o in g.objects if isinstance(o, gerber.Flash)]
    match(unplated, [] if CFG.HOLES_PLATED else by_kind("hole"), "NonPlated drill", errors, True)
    summary.append(f"drills: {len(fl)} plated, {len(unplated)} non-plated")

    g = gerber.parse(path(CFG.OUTLINE))
    pts = [(d.x1, d.y1) for d in g.objects] + [(d.x2, d.y2) for d in g.objects]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    got = (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
    if any(abs(a - b) > 0.05 for a, b in zip(got, CFG.OUTLINE_RECT)):
        errors.append(f"outline {got}, expected {CFG.OUTLINE_RECT}")
    summary.append("outline %.1f x %.1f mm at (%.2f, %.2f)" % (got[2], got[3], got[0], got[1]))

    for s in summary:
        print("  " + s)
    for e in errors[:50]:
        print("  FAIL " + e)
    print("check_sources: " + ("FAIL (%d)" % len(errors) if errors else "OK"))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
