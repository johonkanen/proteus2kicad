#!/usr/bin/env python3
"""Measure the Proteus copper as built (input for the design rules, playbook Phase 3): smallest gap
between copper of different nets per item-kind pair (Proteus pad flashes, tracks, vias, pours =
assign_nets stage B geometry), smallest copper-to-outline distance and smallest drill-to-drill
distance. Read-only, stdout only; run after make_pcb (needs the board file for the via nets).

  --per-layer   also the smallest gap per copper layer and kind pair, and the track widths per
                layer (the control card's version of the board-local script)

Lifted from the boards' identical tools/measure_gaps.py (the default output is that script's) and
the control card's per-layer variant (B11). SPLIT_PIN_MERGES, SMD_FREE_PADS and BGAPad flashes are
handled by the shared readers (no board wrapper needed).

Usage: tools/p2k/bin/kipy -m p2k.measure_gaps [--per-layer] [LIMIT_MM]   (pairs closer than LIMIT, default 0.2)
"""
import math
import sys
from collections import defaultdict

from . import assign_nets as an
from . import geom2d
from . import pcb
from .gerber_to_tracks import load_copper, load_drills


def gap(a, b, lim):
    lo, hi = 0.0, lim
    for _ in range(14):
        m = (lo + hi) / 2
        if geom2d.touches(a, b, m):
            hi = m
        else:
            lo = m
    return hi


def main(argv=None):
    import pcbnew
    argv = sys.argv[1:] if argv is None else list(argv)
    per_layer_out = "--per-layer" in argv
    rest = [a for a in argv if a != "--per-layer"]
    if any(a.startswith("-") for a in rest) or len(rest) > 1:
        sys.exit(__doc__)
    lim = float(rest[0]) if rest else 0.2
    board = pcbnew.LoadBoard(pcb.board_file())
    layers = load_copper()
    items = an.build_items(board, layers, "B", an.ipc_via_nets(board))
    res = an.analyse(items, an.connect(items, layers))
    gnet = {}
    for g, members in res["groups"].items():
        for i in members:
            gnet[i] = res["net_of"].get(g) or f"group{g}"
    best = defaultdict(lambda: (9.0, ""))
    per_layer = defaultdict(lambda: (9.0, ""))
    npairs = 0
    for cl in layers:
        grid = geom2d.Grid(2.0)
        on = [i for i, it in enumerate(items) if cl.layer in it.shapes]
        for i in on:
            b = items[i].shapes[cl.layer].bbox
            grid.add(i, (b[0] - lim, b[1] - lim, b[2] + lim, b[3] + lim))
        for i in on:
            a = items[i].shapes[cl.layer]
            for j in grid.query(a.bbox):
                if j <= i or gnet[i] == gnet[j]:
                    continue
                b = items[j].shapes[cl.layer]
                if not geom2d.touches(a, b, lim):
                    continue
                npairs += 1
                d = gap(a, b, lim)
                k = " - ".join(sorted((items[i].kind, items[j].kind)))
                ex = f"{items[i].label} [{gnet[i]}] / {items[j].label} [{gnet[j]}]"
                if d < best[k][0]:
                    best[k] = (d, ex)
                if d < per_layer[(cl.layer, k)][0]:
                    per_layer[(cl.layer, k)] = (d, ex)
    for k, (d, ex) in sorted(best.items(), key=lambda kv: kv[1][0]):
        print(f"measure_gaps: {k:12s} {d:.4f} mm  e.g. {ex}")
    print(f"measure_gaps: {npairs} different-net pairs closer than {lim} mm")
    if per_layer_out:
        for cl in layers:
            for (lay, k), (d, ex) in sorted(per_layer.items(), key=lambda kv: kv[1][0]):
                if lay == cl.layer:
                    print(f"measure_gaps: {lay:6s} {k:12s} {d:.4f} mm  e.g. {ex}")
            ws = sorted({round(t[4], 4) for t in cl.tracks})
            print(f"measure_gaps: {cl.layer:6s} track widths {ws[:8]}{' ...' if len(ws) > 8 else ''} "
                  f"(thinnest {ws[0] if ws else '-'} mm, {len(cl.tracks)} tracks)")
    x, y, w, h = pcb.cfg().OUTLINE_RECT
    edge = min((min(s.bbox[0] - x, s.bbox[1] - y, x + w - s.bbox[2], y + h - s.bbox[3]), it.label)
               for it in items for s in it.shapes.values())
    print(f"measure_gaps: copper to outline {edge[0]:.4f} mm ({edge[1]})")
    dr = load_drills()
    hh = min((math.hypot(a[0] - b[0], a[1] - b[1]) - (a[2] + b[2]) / 2, a, b)
             for k, a in enumerate(dr) for b in dr[k + 1:] if (a[0], a[1]) != (b[0], b[1]))
    print(f"measure_gaps: drill to drill {hh[0]:.4f} mm ({hh[1][3]} {hh[1][:2]} / {hh[2][3]} {hh[2][:2]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
