#!/usr/bin/env python3
"""Reference designators on the silk screen placed clear of pads, silk and the board edge
(run under tools/p2k/bin/kipy; a make_pcb step after the board is complete).

KiCad footprints come with their reference above the part; on a dense Proteus placement many
references then lie on the neighbours' pads and silk (DRC silk_over_copper / silk_overlap). For
every footprint with its reference on a silk layer (smallest courtyard first, so the dense small
parts get the free spots near them):
  1. keep the reference where it is if it is clear;
  2. else try, for each text size of `sizes` (largest first): horizontal and vertical text centred
     above / below / left / right of the courtyard box, then shifted along that side, then at the
     four corners of the box, then in the middle of the part (large parts with an empty body); the first clear spot wins;
  3. else the reference goes to the Fab layer (hidden when the footprint has a ${REFERENCE} text
     there, visible in the footprint centre otherwise) - the part stays identified in the
     assembly drawing.
"Clear" = the text shape (KiCad's own stroke-font outline, SHAPE.Collide) keeps `pad_gap` from
every pad on that side, `silk_gap` from every other silk item (footprint graphics and texts, board
drawings, references already placed) and `edge_gap` from Edge.Cuts. Deterministic (re-runs give
the same board).

Config key (optional): REFDES_PLACEMENT = dict(sizes=(1.0, 0.8), thickness_ratio=0.15,
pad_gap=0.1, silk_gap=0.1, edge_gap=0.5, side_gap=0.15) in mm; sizes below the board's minimum
silk text height are not used.

Usage: tools/p2k/bin/kipy -m p2k.place_refdes [-v]
"""
import math
import sys
from collections import Counter

import pcbnew

from . import pcb

DEFAULTS = dict(sizes=(1.0, 0.8), thickness_ratio=0.15, pad_gap=0.1, silk_gap=0.1, edge_gap=0.5, side_gap=0.15)
CELL = pcbnew.FromMM(5.0)


def iu(mm):
    return pcbnew.FromMM(mm)


class Index:
    """Shapes in a coarse grid of bounding boxes: (shape, bbox, owner)."""

    def __init__(self):
        self.cells = {}

    def _keys(self, bb, grow=0):
        x0, y0 = (bb.GetLeft() - grow) // CELL, (bb.GetTop() - grow) // CELL
        x1, y1 = (bb.GetRight() + grow) // CELL, (bb.GetBottom() + grow) // CELL
        return [(i, j) for i in range(x0, x1 + 1) for j in range(y0, y1 + 1)]

    def add(self, shape, owner):
        bb = shape.BBox()
        item = (shape, bb, owner)
        for k in self._keys(bb):
            self.cells.setdefault(k, []).append(item)

    def hits(self, shape, gap):
        bb = shape.BBox()
        seen = set()
        for k in self._keys(bb, gap):
            for item in self.cells.get(k, ()):
                if id(item) in seen:
                    continue
                seen.add(id(item))
                ob = item[1]
                if ob.GetLeft() > bb.GetRight() + gap or ob.GetRight() < bb.GetLeft() - gap or \
                        ob.GetTop() > bb.GetBottom() + gap or ob.GetBottom() < bb.GetTop() - gap:
                    continue
                if shape.Collide(item[0], gap):
                    return True
        return False


def fab_reference_text(fp, fab):
    for g in fp.GraphicalItems():
        if g.GetClass() == "PCB_TEXT" and g.GetLayer() == fab and g.GetText() == "${REFERENCE}":
            return g
    return None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    pcbnew.KIID.SeedGenerator(3005)   # no new items, but keep the convention
    s = dict(DEFAULTS)
    s.update(getattr(pcb.cfg(), "REFDES_PLACEMENT", {}) or {})
    board = pcbnew.LoadBoard(pcb.board_file())
    ds = board.GetDesignSettings()
    sizes = [h for h in s["sizes"] if iu(h) >= ds.m_MinSilkTextHeight] or [pcbnew.ToMM(ds.m_MinSilkTextHeight)]
    sides = {"F": (pcbnew.F_SilkS, pcbnew.F_Cu, pcbnew.F_Fab), "B": (pcbnew.B_SilkS, pcbnew.B_Cu, pcbnew.B_Fab)}
    pads = {k: Index() for k in sides}
    silk = {k: Index() for k in sides}
    edge = Index()
    fps = list(board.GetFootprints())
    for fp in fps:
        fp.BuildCourtyardCaches()
        for pad in fp.Pads():
            for k, (_, cu, _) in sides.items():
                if pad.IsOnLayer(cu):
                    pads[k].add(pad.GetEffectiveShape(cu), fp)
        for g in fp.GraphicalItems():
            for k, (sl, _, _) in sides.items():
                if g.GetLayer() != sl:
                    continue
                if g.GetClass() == "PCB_TEXT":
                    if g.IsVisible():
                        silk[k].add(g.GetEffectiveTextShape(), fp)
                else:
                    silk[k].add(g.GetEffectiveShape(), fp)
        for f in fp.GetFields():
            if f.IsReference() or not f.IsVisible():
                continue
            for k, (sl, _, _) in sides.items():
                if f.GetLayer() == sl:
                    silk[k].add(f.GetEffectiveTextShape(), fp)
    for d in board.GetDrawings():
        for k, (sl, _, _) in sides.items():
            if d.GetLayer() == sl:
                silk[k].add(d.GetEffectiveShape() if d.GetClass() != "PCB_TEXT" else d.GetEffectiveTextShape(), None)
        if d.GetLayer() == pcbnew.Edge_Cuts:
            edge.add(d.GetEffectiveShape(), None)

    def courtyard_box(fp, k):
        cy = fp.GetCourtyard(pcbnew.F_CrtYd if k == "F" else pcbnew.B_CrtYd)
        if cy.OutlineCount():
            return cy.BBox()
        return fp.GetBoundingBox(False)

    def clear(f, fp, k):
        shape = f.GetEffectiveTextShape()
        return not (pads[k].hits(shape, iu(s["pad_gap"])) or silk[k].hits(shape, iu(s["silk_gap"]))
                    or edge.hits(shape, iu(s["edge_gap"])))

    stats = Counter()
    order = sorted(fps, key=lambda fp: (courtyard_box(fp, "B" if fp.IsFlipped() else "F").GetArea(), fp.GetReference()))
    for fp in order:
        f = fp.Reference()
        k = "B" if f.GetLayer() == pcbnew.B_SilkS else "F" if f.GetLayer() == pcbnew.F_SilkS else None
        if k is None or not f.IsVisible():
            stats["not on silk"] += 1
            continue
        if clear(f, fp, k):
            silk[k].add(f.GetEffectiveTextShape(), fp)
            stats["kept"] += 1
            continue
        bb = courtyard_box(fp, k)
        cx, cy = bb.Centre().x, bb.Centre().y
        hw, hh = bb.GetWidth() // 2, bb.GetHeight() // 2
        gap = iu(s["side_gap"])
        placed = False
        for size in sizes:
            f.SetTextSize(pcbnew.VECTOR2I(iu(size), iu(size)))
            f.SetTextThickness(iu(size * s["thickness_ratio"]))
            spots = []
            for ang in (0.0, 90.0):
                f.SetTextAngleDegrees(ang)
                f.SetPosition(pcbnew.VECTOR2I(cx, cy))
                tb = f.GetEffectiveTextShape().BBox()
                tw, th = tb.GetWidth() // 2, tb.GetHeight() // 2
                above = (cx, cy - hh - gap - th)
                below = (cx, cy + hh + gap + th)
                left = (cx - hw - gap - tw, cy)
                right = (cx + hw + gap + tw, cy)
                base = [above, below] if ang == 0.0 else [left, right]
                other = [left, right] if ang == 0.0 else [above, below]
                spots += [(ang, p) for p in base]
                shifted = []
                for (px, py) in base:
                    for frac in (-1, 1):
                        shifted.append((px + frac * max(hw - tw, 0), py))
                for (px, py) in other:
                    for frac in (-1, 1):
                        shifted.append((px, py + frac * max(hh - th, 0)))
                corners = [(sx * (hw + gap + tw), sy * (hh + gap + th)) for sy in (-1, 1) for sx in (-1, 1)]
                spots += [(ang, p) for p in other] + [(ang, p) for p in shifted] + \
                         [(ang, (cx + dx, cy + dy)) for dx, dy in corners]
            spots += [(0.0, (cx, cy)), (90.0, (cx, cy))]
            seen = set()
            for ang, (px, py) in spots:
                if (ang, px, py) in seen:
                    continue
                seen.add((ang, px, py))
                f.SetTextAngleDegrees(ang)
                f.SetPosition(pcbnew.VECTOR2I(int(px), int(py)))
                if clear(f, fp, k):
                    placed = True
                    break
            if placed:
                break
        if placed:
            silk[k].add(f.GetEffectiveTextShape(), fp)
            stats["moved" if size == sizes[0] else f"moved, {size} mm text"] += 1
            continue
        # no room on the silk: Fab only
        fab = sides[k][2]
        f.SetTextSize(pcbnew.VECTOR2I(iu(sizes[0]), iu(sizes[0])))
        f.SetTextThickness(iu(sizes[0] * s["thickness_ratio"]))
        f.SetTextAngleDegrees(0.0)
        f.SetPosition(pcbnew.VECTOR2I(cx, cy))
        f.SetLayer(fab)
        if fab_reference_text(fp, fab) is not None:
            f.SetVisible(False)
        stats["Fab only"] += 1
        if "-v" in argv:
            print(f"place_refdes: {fp.GetReference()}: no room on the silk, reference on the Fab layer")
    pcbnew.SaveBoard(pcb.board_file(), board)
    print("place_refdes: " + ", ".join(f"{v} {k}" for k, v in sorted(stats.items())) +
          f" (sizes {', '.join(str(x) for x in sizes)} mm)")


if __name__ == "__main__":
    main()
