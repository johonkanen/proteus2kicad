#!/usr/bin/env python3
"""Phase 3b: Proteus copper gerbers -> KiCad tracks and vias (run under tools/p2k/bin/kipy).

Reads the copper gerbers (COPPER) and the drill gerbers (DRILL_PLATED, DRILL_NONPLATED) with
p2k.gerber and, on the board made by p2k.build_pcb:
  - removes all tracks, vias and zones and the drawings on the pour reference layers
    (re-runnable);
  - every D01 draw with a Conductor aperture -> PCB_TRACK of that width on the matching layer,
    except the pour edge strokes: Proteus outlines every pour region and every clear hole
    in it with the pour's edge width (draws whose two ends are consecutive vertices of a
    G36 region), those belong to the pour (zones, pours_to_zones.py);
  - ViaPad flashes (present on every copper layer, same size) + ViaDrill flash -> through
    PCB_VIA with the flash diameter and the drill (drill must be one of VIA_DRILLS),
    cross-checked against the IPC via records;
  - component pad flashes are not converted (the footprints provide the pads), but every
    flash must land on a footprint pad on that layer (and OtherPad flashes on a board-only
    footprint: mounting hole or free pad); the plated and unplated drill flashes must match a
    pad hole or via;
  - NonConductor draws -> graphic lines on the copper layer, no net;
  - the outer contour of each positive G36 region -> polygon on the layer's reference User
    layer (User.1..4 = Proteus.F/In1/In2/B.Pours).
Tracks and vias get no net here; tools/assign_nets.py assigns them.

load_copper() is the shared reader (also used by assign_nets.py and pours_to_zones.py).

Usage: tools/p2k/bin/kipy -m p2k.gerber_to_tracks
"""
import math
import os
import sys
from collections import Counter

from . import geom2d, gerber
from . import pcb as bc


def _k(x, y):
    return (round(x, 4), round(y, 4))


class Pour:
    """A positive G36 region: outer contour, the clear regions drawn after it that cut it
    (holes), the edge stroke width, and its index in draw order (per layer)."""

    def __init__(self, layer, index, contour, width, order):
        self.layer, self.index, self.contour, self.width, self.order = layer, index, contour, width, order
        self.holes = []

    def shape(self):
        return geom2d.poly(self.contour, self.width / 2, self.holes)

    def name(self):
        return f"{self.layer}#{self.index}"


class CopperLayer:
    def __init__(self, gname, layer, user):
        self.gname, self.layer, self.user = gname, layer, user
        self.tracks = []        # (x1, y1, x2, y2, width)
        self.graphics = []      # NonConductor draws (x1, y1, x2, y2, width): copper, no net
        self.vias = []          # (x, y, diameter)
        self.pads = []          # gerber.Flash (ComponentPad / SMDPad)
        self.other_pads = []    # gerber.Flash (OtherPad = mounting holes)
        self.pours = []         # Pour
        self.stats = Counter()


def flash_shape(f):
    """Copper shape of a pad flash (gerber frame)."""
    ap = f.ap
    if ap.shape == "C":
        return geom2d.seg(f.x, f.y, f.x, f.y, ap.params[0] / 2)
    if ap.shape == "R":
        w, h = ap.params[0] / 2, ap.params[1] / 2
        return geom2d.poly([(f.x - w, f.y - h), (f.x + w, f.y - h), (f.x + w, f.y + h), (f.x - w, f.y + h)])
    return geom2d.poly([(f.x + x, f.y + y) for x, y in ap.outline])


def load_layer(gname, layer, user):
    g = gerber.parse(bc.gerber_path(gname))
    cl = CopperLayer(gname, layer, user)
    regions = [o for o in g.objects if isinstance(o, gerber.Region)]
    edge_owner = {}
    for i, r in enumerate(regions):
        c = r.contours[0]
        for a, b in zip(c, c[1:]):
            edge_owner.setdefault(frozenset((_k(*a), _k(*b))), []).append(i)
    stroke_w = {}
    for d in g.of(gerber.Draw):
        if d.ap.function == "Profile":
            cl.stats["profile draws (skipped)"] += 1
            continue
        if d.ap.function == "NonConductor":
            cl.graphics.append((d.x1, d.y1, d.x2, d.y2, d.ap.params[0]))
            cl.stats["NonConductor draws (copper graphics)"] += 1
            continue
        if d.ap.function != "Conductor":
            raise ValueError(f"{gname}: unexpected draw function {d.ap.function}")
        owners = edge_owner.get(frozenset((_k(d.x1, d.y1), _k(d.x2, d.y2))))
        if owners:
            for i in owners:
                stroke_w.setdefault(i, Counter())[d.ap.params[0]] += 1
            cl.stats["pour edge strokes"] += 1
            continue
        cl.tracks.append((d.x1, d.y1, d.x2, d.y2, d.ap.params[0]))
    for f in g.of(gerber.Flash):
        fn = f.ap.function
        if fn == "ViaPad":
            cl.vias.append((f.x, f.y, f.ap.params[0]))
        elif fn.startswith("OtherPad"):
            cl.other_pads.append(f)
        elif fn in ("ComponentPad", "SMDPad,CuDef"):
            cl.pads.append(f)
        else:
            raise ValueError(f"{gname}: unexpected flash function {fn}")
    # pours and their holes (a clear region cuts every earlier dark region it overlaps)
    order = {id(o): n for n, o in enumerate(g.objects)}
    for i, r in enumerate(regions):
        if r.dark:
            w = stroke_w[i].most_common(1)[0][0] if i in stroke_w else 0.0
            if i not in stroke_w:
                cl.stats["pours without edge stroke"] += 1
            cl.pours.append(Pour(layer, len(cl.pours), r.contours[0], w, order[id(r)]))
        else:
            hole = geom2d.poly(r.contours[0])
            n = 0
            for p in cl.pours:
                ps = geom2d.poly(p.contour)
                if geom2d.bbox_hit(ps.bbox, hole.bbox) and geom2d.core_dist_le(hole, ps, 0.0):
                    p.holes.append(r.contours[0])
                    n += 1
            cl.stats["clear regions"] += 1
            if n == 0:
                cl.stats["clear regions cutting no pour"] += 1
    cl.stats["tracks"] = len(cl.tracks)
    cl.stats["pours"] = len(cl.pours)
    return cl


def load_copper():
    return [load_layer(*c) for c in bc.copper()]


def load_drills():
    """Plated and (if DRILL_NONPLATED) unplated drill flashes: (x, y, diameter, function)."""
    c = bc.cfg()
    out = []
    for name in (c.DRILL_PLATED, getattr(c, "DRILL_NONPLATED", None)):
        if name:
            g = gerber.parse(bc.gerber_path(name))
            out += [(f.x, f.y, f.ap.params[0], f.ap.function) for f in g.of(gerber.Flash)]
    return out


def via_sites(layers, drills, errors):
    """Vias present on all copper layers and in the drill file: [(x, y, diameter, drill)]."""
    per_layer = [Counter(_k(x, y) for x, y, _ in cl.vias) for cl in layers]
    common = set(per_layer[0])
    for c in per_layer[1:]:
        common &= set(c)
    for cl, c in zip(layers, per_layer):
        extra = set(c) - common
        if extra:
            errors.append(f"{cl.layer}: {len(extra)} via pads not on every layer, e.g. {sorted(extra)[:3]}")
    vdr = {_k(x, y): d for x, y, d, fn in drills if fn == "ViaDrill"}
    sites = sorted(common & set(vdr))
    if set(vdr) - common:
        errors.append(f"{len(set(vdr) - common)} via drills without via pads")
    if common - set(vdr):
        errors.append(f"{len(common - set(vdr))} via pads without via drill")
    dia = {}
    for cl in layers:
        for x, y, d in cl.vias:
            dia.setdefault(_k(x, y), set()).add(d)
    out = []
    for s in sites:
        if not any(abs(vdr[s] - d) < 1e-6 for d in bc.cfg().VIA_DRILLS):
            errors.append(f"via at {s}: drill {vdr[s]} not in VIA_DRILLS")
        if len(dia[s]) != 1:
            errors.append(f"via at {s}: pad diameters {sorted(dia[s])} differ between layers")
        out.append((s[0], s[1], max(dia[s]), vdr[s]))
    return out


def main():
    import pcbnew

    def pt(x, y):
        return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(-y))

    pcbnew.KIID.SeedGenerator(3002)  # reproducible UUIDs (distinct seed per script)
    board = pcbnew.LoadBoard(bc.board_file())
    # Delete(), not Remove(): garbage-collecting the proxy of a removed item breaks SWIG's
    # type table in pcbnew 10 (later calls return untyped SwigPyObjects)
    for t in list(board.GetTracks()):
        board.Delete(t)
    for z in list(board.Zones()):
        board.Delete(z)
    own_layers = {getattr(pcbnew, u.replace(".", "_")) for _, _, u in bc.copper()}
    own_layers |= {board.GetLayerID(c) for _, c, _ in bc.copper()}
    for d in list(board.GetDrawings()):
        if d.GetLayer() in own_layers:
            board.Delete(d)

    errors = []
    layers = load_copper()
    drills = load_drills()
    ipc_vias = [p for p in bc.ipc_records() if p["kind"] == "via"]

    # tracks
    for cl in layers:
        lid = board.GetLayerID(cl.layer)
        for x1, y1, x2, y2, w in cl.tracks:
            t = pcbnew.PCB_TRACK(board)
            t.SetStart(pt(x1, y1))
            t.SetEnd(pt(x2, y2))
            t.SetWidth(pcbnew.FromMM(w))
            t.SetLayer(lid)
            board.Add(t)
        for x1, y1, x2, y2, w in cl.graphics:
            s = pcbnew.PCB_SHAPE(board)
            s.SetShape(pcbnew.SHAPE_T_SEGMENT)
            s.SetStart(pt(x1, y1))
            s.SetEnd(pt(x2, y2))
            s.SetWidth(pcbnew.FromMM(w))
            s.SetLayer(lid)
            board.Add(s)
    # vias
    sites = via_sites(layers, drills, errors)
    ipc_k = {_k(p["x_mm"], p["y_mm"]) for p in ipc_vias}
    miss = [s[:2] for s in sites if not any(math.hypot(s[0] - q[0], s[1] - q[1]) < 0.01 for q in ipc_k)]
    if miss or len(sites) != len(ipc_k):
        errors.append(f"vias: {len(sites)} in the gerbers, {len(ipc_k)} IPC via positions, {len(miss)} unmatched {miss[:3]}")
    dup_note = ""
    if len(ipc_k) != len(ipc_vias):
        # Proteus may stack identical vias on one spot (one drill hole): one KiCad via each
        dup_note = f", {len(ipc_vias) - len(ipc_k)} stacked duplicate via records merged"
    for x, y, dia, drill in sites:
        v = pcbnew.PCB_VIA(board)
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetPosition(pt(x, y))
        v.SetWidth(pcbnew.FromMM(dia))
        v.SetDrill(pcbnew.FromMM(drill))
        v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        board.Add(v)
    # component pad flashes must land on footprint pads; drills on pad holes / vias
    pads_by_layer = {}
    holes = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            c = pad.GetPosition()
            x, y = pcbnew.ToMM(c.x), -pcbnew.ToMM(c.y)
            for cl in layers:
                if pad.IsOnLayer(board.GetLayerID(cl.layer)):
                    pads_by_layer.setdefault(cl.layer, []).append((x, y, fp.IsBoardOnly()))
            if pad.GetDrillSizeX() > 0:
                holes.append((x, y))
    unmatched = Counter()
    for cl in layers:
        grid = {}
        for x, y, bo in pads_by_layer.get(cl.layer, []):
            grid.setdefault((round(x), round(y)), []).append((x, y, bo))

        def near(f, board_only):
            for i in (-1, 0, 1):
                for j in (-1, 0, 1):
                    for x, y, bo in grid.get((round(f.x) + i, round(f.y) + j), []):
                        if bo == board_only and math.hypot(x - f.x, y - f.y) <= bc.TOL:
                            return True
            return False

        for f in cl.pads:
            if not near(f, False):
                unmatched[cl.layer] += 1
                if unmatched[cl.layer] <= 3:
                    errors.append(f"{cl.layer}: pad flash D{f.ap.dcode} at ({f.x}, {f.y}) has no footprint pad")
        for f in cl.other_pads:
            if not near(f, True):
                errors.append(f"{cl.layer}: OtherPad flash at ({f.x}, {f.y}) has no board-only pad")
    for cl in layers:
        if unmatched[cl.layer] > 3:
            errors.append(f"{cl.layer}: {unmatched[cl.layer]} pad flashes without footprint pad in total")
    for x, y, d, fn in drills:
        if fn == "ViaDrill":
            continue
        if not any(math.hypot(x - hx, y - hy) <= bc.TOL for hx, hy in holes):
            errors.append(f"drill {fn} {d} mm at ({x}, {y}) has no pad hole")
    # pour outlines on the reference layers
    for cl in layers:
        uid = getattr(pcbnew, cl.user.replace(".", "_"))
        for p in cl.pours:
            s = pcbnew.PCB_SHAPE(board)
            s.SetShape(pcbnew.SHAPE_T_POLY)
            s.SetLayer(uid)
            s.SetFilled(False)
            s.SetWidth(pcbnew.FromMM(0.05))
            chain = pcbnew.SHAPE_LINE_CHAIN()
            for x, y in p.contour[:-1]:
                chain.Append(pt(x, y))
            chain.SetClosed(True)
            ps = pcbnew.SHAPE_POLY_SET()
            ps.AddOutline(chain)
            s.SetPolyShape(ps)
            board.Add(s)

    for cl in layers:
        st = ", ".join(f"{v} {k}" for k, v in sorted(cl.stats.items()))
        print(f"gerber_to_tracks: {cl.layer:6s} {st}, {len(cl.pads)} pad flashes")
    print(f"gerber_to_tracks: {sum(len(cl.tracks) for cl in layers)} tracks, {len(sites)} vias "
          f"(IPC {len(ipc_vias)}{dup_note}), {sum(len(cl.pours) for cl in layers)} pour outlines on User.1-4")
    for e in errors:
        print("gerber_to_tracks: ERROR " + e)
    if errors:
        sys.exit(1)
    pcbnew.SaveBoard(bc.board_file(), board)


if __name__ == "__main__":
    main()
