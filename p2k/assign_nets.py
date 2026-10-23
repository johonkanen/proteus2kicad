#!/usr/bin/env python3
"""Phase 3c: give every imported copper item its net, and check copper connectivity
(run under tools/p2k/bin/kipy).

Geometric union-find (tools/geom2d.py) over the copper items, per layer: pads, tracks, vias
(all layers) and pours. Every connected group takes the net of its pads (KiCad nets from
the schematic) and of its IPC vias (IPC net -> KiCad net via the pads). Reported:
  shorts   groups holding two or more nets (pads or IPC vias),
  orphans  groups with no pad (net then only from IPC vias, or none),
  opens    nets whose pads fall into more than one group.

Default mode (after gerber_to_tracks.py), Proteus geometry:
  stage A  pads (the Proteus pad flashes, matched to the footprint pads) + tracks + vias:
           opens are expected where the connection runs through a pour;
  stage B  + the Proteus pours (G36 regions minus their clear holes, grown by the edge
           stroke): assigns the group net to every track and via (saved to the board) and
           writes each pour's net to reports/pour_nets.json for pours_to_zones.py.
--check (after pours_to_zones.py), KiCad geometry: footprint pads, tracks, vias and the
  filled zones of the saved board; every item must carry its group's net, 0 shorts,
  0 opens. Nothing is changed.
Report: reports/assign_nets.txt (default mode writes the stage A/B sections, --check
replaces its own section at the end).

Board-only pads (mounting holes, free pads) take part as copper; a board-only pad without a
net (mounting hole ring) gives its group no net.

Usage: tools/p2k/bin/kipy -m p2k.assign_nets [--check]
"""
import json
import math
import os
import sys
from collections import defaultdict

import pcbnew

from . import geom2d
from . import pcb as bc
from .gerber_to_tracks import flash_shape, load_copper


def report_path():
    return os.path.join(bc.reports_dir(), "assign_nets.txt")


def pour_nets_path():
    return os.path.join(bc.reports_dir(), "pour_nets.json")

EPS = 1e-4  # mm: copper closer than this counts as touching


def to_mm(v):
    return pcbnew.ToMM(v)


def kpt(p):
    """KiCad VECTOR2I -> (x, y) mm in the gerber frame (Y up)."""
    return (to_mm(p.x), -to_mm(p.y))


class Item:
    __slots__ = ("kind", "obj", "net", "shapes", "label")

    def __init__(self, kind, obj, net, label):
        self.kind, self.obj, self.net, self.label = kind, obj, net, label
        self.shapes = {}  # layer name -> geom2d.Shape


def poly_set_shapes(ps):
    """SHAPE_POLY_SET -> [geom2d poly with holes] (gerber frame)."""
    out = []
    for i in range(ps.OutlineCount()):
        o = ps.Outline(i)
        ring = [kpt(o.CPoint(k)) for k in range(o.PointCount())]
        holes = []
        for j in range(ps.HoleCount(i)):
            h = ps.Hole(i, j)
            holes.append([kpt(h.CPoint(k)) for k in range(h.PointCount())])
        if len(ring) >= 3:
            out.append(geom2d.poly(ring, 0.0, holes))
    return out


def kicad_pads(board):
    """[(pad, footprint ref, centre)]"""
    out = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            out.append((pad, fp.GetReference(), kpt(pad.GetPosition())))
    return out


def pad_label(ref, pad):
    return f"{ref}-{pad.GetNumber()}"


def build_items(board, layers, mode, ipc_via_net):
    """mode 'A' / 'B' (Proteus pads, + pours for B) or 'C' (KiCad pads + zone fills)."""
    lids = {cl.layer: board.GetLayerID(cl.layer) for cl in layers}
    items = []
    pads = kicad_pads(board)
    if mode in ("A", "B"):
        # Proteus pad flashes, attached to the footprint pad they sit on
        pad_items = {}
        grid = defaultdict(list)
        for pad, ref, (x, y) in pads:
            grid[(round(x), round(y))].append((pad, ref, x, y))
        for cl in layers:
            for f in cl.pads + cl.other_pads:
                best = None
                for i in (-1, 0, 1):
                    for j in (-1, 0, 1):
                        for pad, ref, x, y in grid.get((round(f.x) + i, round(f.y) + j), []):
                            d = math.hypot(x - f.x, y - f.y)
                            if d <= bc.TOL and pad.IsOnLayer(lids[cl.layer]) and (best is None or d < best[0]):
                                best = (d, pad, ref)
                if best is None:
                    raise SystemExit(f"assign_nets: flash at ({f.x}, {f.y}) on {cl.layer} has no footprint pad")
                _, pad, ref = best
                key = pad.m_Uuid.AsString()
                if key not in pad_items:
                    pad_items[key] = Item("pad", pad, pad.GetNetname(), pad_label(ref, pad))
                    items.append(pad_items[key])
                pad_items[key].shapes[cl.layer] = flash_shape(f)
    else:
        for pad, ref, _ in pads:
            if not pad.IsOnCopperLayer():
                continue  # paste/mask-only aperture
            it = Item("pad", pad, pad.GetNetname(), pad_label(ref, pad))
            for cl in layers:
                if pad.IsOnLayer(lids[cl.layer]):
                    ps = pad.GetEffectivePolygon(lids[cl.layer], pcbnew.ERROR_INSIDE)
                    shapes = poly_set_shapes(ps)
                    if len(shapes) != 1:
                        raise SystemExit(f"assign_nets: pad {it.label} has {len(shapes)} outlines")
                    it.shapes[cl.layer] = shapes[0]
            items.append(it)
    lname = {v: k for k, v in lids.items()}
    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA":
            x, y = kpt(t.GetPosition())
            it = Item("via", t, ipc_via_net(x, y), f"via({x:.3f},{y:.3f})")
            r = to_mm(t.GetWidth(pcbnew.F_Cu)) / 2
            for cl in layers:
                it.shapes[cl.layer] = geom2d.seg(x, y, x, y, r)
        else:
            (x1, y1), (x2, y2) = kpt(t.GetStart()), kpt(t.GetEnd())
            it = Item("track", t, None, f"track {lname[t.GetLayer()]}({x1:.3f},{y1:.3f})-({x2:.3f},{y2:.3f})")
            it.shapes[lname[t.GetLayer()]] = geom2d.seg(x1, y1, x2, y2, to_mm(t.GetWidth()) / 2)
        items.append(it)
    if mode == "B":
        for cl in layers:
            for p in cl.pours:
                it = Item("pour", p, None, f"pour {p.name()}")
                it.shapes[cl.layer] = p.shape()
                items.append(it)
    if mode == "C":
        for z in board.Zones():
            if z.GetIsRuleArea():
                continue
            for cl in layers:
                lid = lids[cl.layer]
                if not z.IsOnLayer(lid):
                    continue
                for k, s in enumerate(poly_set_shapes(z.GetFilledPolysList(lid))):
                    it = Item("zone", z, None, f"zone {z.GetZoneName() or z.GetNetname()} {cl.layer}#{k}")
                    it.shapes[cl.layer] = s
                    items.append(it)
    return items


def connect(items, layers):
    uf = geom2d.UnionFind(len(items))
    for cl in layers:
        grid = geom2d.Grid(2.0)
        on = [i for i, it in enumerate(items) if cl.layer in it.shapes]
        for i in on:
            grid.add(i, items[i].shapes[cl.layer].bbox)
        for i in on:
            a = items[i].shapes[cl.layer]
            for j in grid.query(a.bbox):
                if j <= i or uf.find(i) == uf.find(j):
                    continue
                if geom2d.touches(a, items[j].shapes[cl.layer], EPS):
                    uf.union(i, j)
    return uf


def analyse(items, uf):
    groups = uf.groups()
    res = {"groups": groups, "net_of": {}, "shorts": [], "orphans": [], "opens": []}
    pad_groups = defaultdict(set)
    for g, members in groups.items():
        nets = defaultdict(list)
        has_pad = has_copper = False
        for i in members:
            it = items[i]
            if it.kind == "pad":
                if it.obj.GetParentFootprint().IsBoardOnly() and not it.net:
                    continue  # mounting hole, no net
                has_pad = True
                nets[it.net].append(it.label)
                if not bc.is_unconnected_net(it.net) or len(members) > 1:
                    pad_groups[it.net].add(g)
            else:
                has_copper = True
                if it.kind == "via" and it.net:
                    nets[it.net].append(it.label)
        if len(nets) > 1:
            res["shorts"].append((g, dict(nets)))
        if has_copper and not has_pad:
            res["orphans"].append((g, sorted(nets)))
        if len(nets) == 1:
            res["net_of"][g] = next(iter(nets))
    for net, gs in sorted(pad_groups.items()):
        if len(gs) > 1 and not bc.is_unconnected_net(net):
            res["opens"].append((net, sorted(gs)))
    return res


def describe(name, items, res, out):
    groups = res["groups"]
    out.append(f"== {name}")
    out.append(f"items {len(items)}, groups {len(groups)}, shorts {len(res['shorts'])}, "
               f"orphans {len(res['orphans'])}, opens {len(res['opens'])}")
    for g, nets in res["shorts"]:
        out.append(f"  SHORT group {g}: " + "; ".join(f"{n}: {' '.join(sorted(v)[:6])}" for n, v in nets.items()))
    for g, nets in res["orphans"]:
        kinds = defaultdict(int)
        for i in groups[g]:
            kinds[items[i].kind] += 1
        first = items[groups[g][0]].label
        out.append(f"  orphan group {g} ({', '.join(f'{v} {k}' for k, v in sorted(kinds.items()))}) "
                   f"net from vias {nets or '-'}: {first}")
    for net, gs in res["opens"]:
        parts = []
        for g in gs:
            pads = sorted(items[i].label for i in groups[g] if items[i].kind == "pad")
            parts.append("[" + " ".join(pads[:5]) + (" ..." if len(pads) > 5 else "") + "]")
        out.append(f"  open {net}: {len(gs)} groups " + " ".join(parts))


def summary(name, res):
    return (f"assign_nets: {name}: {len(res['groups'])} groups, {len(res['shorts'])} shorts, "
            f"{len(res['orphans'])} orphans, {len(res['opens'])} opens")


def ipc_via_nets(board):
    mapping, problems = bc.ipc_to_kicad_nets(board)
    if problems:
        raise SystemExit("assign_nets: " + "; ".join(problems))
    vias = [(p["x_mm"], p["y_mm"], mapping.get(p["net"])) for p in bc.ipc_records() if p["kind"] == "via"]

    def lookup(x, y):
        """KiCad net of the IPC via at (x, y), None if there is none within 0.01 mm."""
        for vx, vy, net in vias:
            if abs(vx - x) < 0.01 and abs(vy - y) < 0.01:
                return net
        return None
    return lookup


def run_assign(board, layers, vianets):
    out = []
    items = build_items(board, layers, "A", vianets)
    res_a = analyse(items, connect(items, layers))
    describe("stage A: pads + tracks + vias (Proteus pad flashes)", items, res_a, out)
    print(summary("stage A (no pours)", res_a))
    items = build_items(board, layers, "B", vianets)
    res = analyse(items, connect(items, layers))
    describe("stage B: + Proteus pours", items, res, out)
    print(summary("stage B (with pours)", res))
    # assign
    assigned, unassigned, pours = 0, [], []
    for g, members in res["groups"].items():
        net = res["net_of"].get(g)
        for i in members:
            it = items[i]
            if it.kind in ("track", "via"):
                if net:
                    it.obj.SetNet(board.FindNet(net))
                    assigned += 1
                else:
                    unassigned.append(it.label)
            elif it.kind == "pour":
                p = it.obj
                pours.append({"layer": p.layer, "index": p.index, "net": net or "", "order": p.order,
                              "width": p.width, "group": g})
    out.append(f"== assignment: {assigned} tracks/vias got a net, {len(unassigned)} without")
    for lab in unassigned[:50]:
        out.append(f"  no net: {lab}")
    with open(pour_nets_path(), "w") as f:
        json.dump(sorted(pours, key=lambda p: (p["layer"], p["index"])), f, indent=1)
    with open(report_path(), "w") as f:
        f.write("\n".join(out) + "\n")
    print(f"assign_nets: {assigned} tracks/vias assigned, {len(unassigned)} without net; "
          f"{sum(1 for p in pours if p['net'])}/{len(pours)} pours have a net -> "
          f"{bc.rel(pour_nets_path())}")
    pcbnew.SaveBoard(bc.board_file(), board)
    return not res["shorts"] and not unassigned


def conflicts(items, members, layers):
    """Directly touching item pairs of different nets within one group."""
    out = []
    for cl in layers:
        on = [items[i] for i in members if cl.layer in items[i].shapes]
        for k, a in enumerate(on):
            for b in on[k + 1:]:
                if a.obj.GetNetname() != b.obj.GetNetname() and \
                        geom2d.touches(a.shapes[cl.layer], b.shapes[cl.layer], EPS):
                    out.append((a, b, cl.layer))
    return out


def run_check(board, layers, vianets):
    out = []
    items = build_items(board, layers, "C", vianets)
    uf = connect(items, layers)
    res = analyse(items, uf)
    # every copper item must carry its group's net
    wrong = []
    shorted = {g for g, _ in res["shorts"]}
    for g, members in res["groups"].items():
        net = res["net_of"].get(g)
        if g in shorted:
            continue  # reported as a short
        for i in members:
            it = items[i]
            if it.kind in ("track", "via", "zone"):
                own = it.obj.GetNetname()
                if net and own != net:
                    wrong.append(f"{it.label}: net {own!r}, connected to {net}")
                elif not net and own:
                    wrong.append(f"{it.label}: net {own!r}, group has no pad")
    describe("after zone fill (KiCad pads, tracks, vias, filled zones)", items, res, out)
    for g, _ in res["shorts"]:
        for a, b, layer in conflicts(items, res["groups"][g], layers):
            out.append(f"  touching on {layer}: {a.label} [{a.obj.GetNetname()}] - {b.label} [{b.obj.GetNetname()}]")
    out.append(f"items carrying another net than their group: {len(wrong)}")
    out.extend("  " + w for w in wrong[:100])
    head = ""
    if os.path.exists(report_path()):
        with open(report_path()) as f:
            head = f.read().split(out[0] + "\n")[0]  # keep the stage A/B sections, replace this one
    with open(report_path(), "w") as f:
        f.write(head + "\n".join(out) + "\n")
    print(summary("after zone fill", res) + f", {len(wrong)} items with a foreign net")
    for line in out[1:40]:
        if line.startswith("  "):
            print("assign_nets:" + line)
    return not res["shorts"] and not res["opens"] and not wrong


def main():
    board = pcbnew.LoadBoard(bc.board_file())
    layers = load_copper()
    vianets = ipc_via_nets(board)
    if "--check" in sys.argv:
        ok = run_check(board, layers, vianets)
    else:
        ok = run_assign(board, layers, vianets)
    print("assign_nets: " + ("OK" if ok else "FAILED") + f" (report {bc.rel(report_path())})")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
