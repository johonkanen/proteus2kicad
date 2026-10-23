#!/usr/bin/env python3
"""Phase 3d: Proteus pours -> KiCad zones, then fill (run under tools/p2k/bin/kipy).

Input: the Proteus pours from the copper gerbers (gerber_to_tracks.load_copper) and their
nets from reports/pour_nets.json (assign_nets.py). For each pour:
  outline = the Proteus copper: the G36 region grown by half its edge stroke, minus its clear
            holes shrunk by half the stroke. Holes the stroke closes (tiny octagons Proteus
            leaves on same-net pads) vanish; all others are kept: they are Proteus' clearance
            cut-outs around other-net copper, and its thermal reliefs (a hole around a
            same-net pad bridged by spoke tracks, which gerber_to_tracks imported as tracks)
            or deliberate isolation of a same-net via/pad from the pour on that layer. So the
            zones reproduce the Proteus copper; KiCad still applies its own clearance
            (PCB_RULES clearance);
  zone    = one ZONE per resulting outline: layer and net of the pour, solid pad connection
            (Proteus pours connect pads solid, no thermal spokes), min width = PCB_RULES
            min_track, islands removed, priority = Proteus draw order (later regions are drawn
            on top).
Existing zones are removed first (re-runnable). The zones are filled with ZONE_FILLER and
the board is saved.

Usage: tools/p2k/bin/kipy -m p2k.pours_to_zones
"""
import json
import os
import sys
from collections import Counter

import pcbnew

from . import assign_nets as an
from . import pcb as bc
from .gerber_to_tracks import load_copper

MAX_ERROR = 0.002   # mm, arc approximation of the stroke growth


def chain(ring):
    c = pcbnew.SHAPE_LINE_CHAIN()
    for x, y in (ring[:-1] if ring[0] == ring[-1] else ring):
        c.Append(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(-y)))
    c.SetClosed(True)
    return c


def grown(ring, amount):
    ps = pcbnew.SHAPE_POLY_SET()
    ps.AddOutline(chain(ring))
    ps.Simplify()
    if amount:
        ps.Inflate(pcbnew.FromMM(amount), pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS, pcbnew.FromMM(MAX_ERROR))
    return ps


def main():
    pcbnew.KIID.SeedGenerator(3004)  # reproducible UUIDs (distinct seed per script)
    rules = bc.cfg().PCB_RULES
    board = pcbnew.LoadBoard(bc.board_file())
    for z in list(board.Zones()):
        board.Delete(z)  # not Remove(): collecting a removed item's proxy breaks SWIG's type table
    layers = load_copper()
    with open(an.pour_nets_path()) as f:
        nets = {(p["layer"], p["index"]): p["net"] for p in json.load(f)}
    stats = Counter()
    errors = []
    zones = []
    pours = [p for cl in layers for p in cl.pours]
    rank = {id(p): n for n, p in enumerate(sorted(pours, key=lambda p: (p.layer, p.order)))}
    for cl in layers:
        lid = board.GetLayerID(cl.layer)
        for p in cl.pours:
            net = nets.get((p.layer, p.index))
            if not net:
                errors.append(f"pour {p.name()} has no net (run assign_nets.py)")
                continue
            outline = grown(p.contour, p.width / 2)
            cut = pcbnew.SHAPE_POLY_SET()
            for h in p.holes:
                g = grown(h, -p.width / 2)
                if g.OutlineCount() == 0:
                    stats["holes closed by the stroke"] += 1
                    continue
                stats["holes kept"] += 1
                for i in range(g.OutlineCount()):
                    cut.AddOutline(g.Outline(i))
            if cut.OutlineCount():
                outline.BooleanSubtract(cut)
            for i in range(outline.OutlineCount()):
                z = pcbnew.ZONE(board)
                z.SetLayer(lid)
                z.SetNet(board.FindNet(net))
                zo = z.Outline()
                zo.AddOutline(outline.Outline(i))
                for j in range(outline.HoleCount(i)):
                    zo.AddHole(outline.Hole(i, j))
                z.SetZoneName(f"Proteus {cl.layer} pour {p.index}")
                z.SetAssignedPriority(rank[id(p)])
                z.SetMinThickness(pcbnew.FromMM(rules["min_track"]))
                z.SetLocalClearance(pcbnew.FromMM(rules["clearance"]))
                z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
                z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
                z.SetFillMode(pcbnew.ZONE_FILL_MODE_POLYGONS)
                board.Add(z)
                z.thisown = False  # the board owns it now
                zones.append(z)
                stats["zones"] += 1
            if outline.OutlineCount() > 1:
                stats["pours split into several zones by holes"] += 1
    if errors:
        for e in errors:
            print("pours_to_zones: ERROR " + e)
        sys.exit(1)
    filler = pcbnew.ZONE_FILLER(board)
    filler.Fill(board.Zones())
    empty = [z.GetZoneName() for z in zones if not z.HasFilledPolysForLayer(z.GetLayer())
             or z.GetFilledPolysList(z.GetLayer()).OutlineCount() == 0]
    pcbnew.SaveBoard(bc.board_file(), board)
    print(f"pours_to_zones: {len(pours)} pours -> {stats['zones']} zones; " +
          ", ".join(f"{v} {k}" for k, v in sorted(stats.items()) if k != "zones"))
    if empty:
        print(f"pours_to_zones: {len(empty)} zones filled empty: {', '.join(empty[:10])}")


if __name__ == "__main__":
    main()
