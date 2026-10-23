#!/usr/bin/env python3
"""Phase 3a: create the board from the schematic, footprints placed where Proteus put them
(run under tools/p2k/bin/kipy). Re-runnable: the board is rebuilt from scratch.

There is no headless "Update PCB from schematic", so this does it in Python:
  - export the kicadxml netlist (kicad-cli) and create one footprint per symbol ("exclude from
    board" symbols are skipped) with reference, value, Datasheet/Description, the symbol link
    (path = sheet path + symbol UUID, sheet name/file -> DRC schematic parity) and every pad's
    net;
  - place each footprint from reports/footprint_fit.csv with the p2k.fpgeom recipe
    (SetPosition; bottom: Flip(LEFT_RIGHT); SetOrientationDegrees);
  - copper layers (COPPER), board outline on Edge.Cuts from the OUTLINE gerber (Proteus origin
    kept, KiCad y = -IPC y), drill/grid origin at the Proteus origin;
  - the IPC 'hole' records (mounting holes, no net) as board-only footprints HOLE_FOOTPRINT
    named H1..; the IPC 'free_pad' records (plated pads with a net but no part) as board-only
    footprints FREE_PAD_FOOTPRINT named FREE_PAD_PREFIX1.., pad 1 on the IPC net;
  - the User layers of COPPER named Proteus.<layer>.Pours (gerber_to_tracks fills them);
  - board thickness PCB_THICKNESS and the design rules PCB_RULES.
Tracks, vias and zones are added by the later steps (gerber_to_tracks, assign_nets,
pours_to_zones).

Config keys: COPPER, OUTLINE, PCB_THICKNESS, PCB_RULES = dict(min_track, min_via,
min_annular, min_drill, clearance, hole_clearance, hole_to_hole, edge_clearance, track, via,
via_drill) in mm, HOLE_FOOTPRINT, HOLE_VALUE, and when the IPC file has free pads
FREE_PAD_FOOTPRINT, FREE_PAD_VALUE, FREE_PAD_PREFIX (default "FP").

Usage: tools/p2k/bin/kipy -m p2k.build_pcb
"""
import csv
import os
import sys

import pcbnew

from . import gerber, pcb
from .footprints import footprint_path


def mm(v):
    return pcbnew.FromMM(v)


def pt(x, y):
    """IPC/gerber point (Y up) -> KiCad VECTOR2I (Y down)."""
    return pcbnew.VECTOR2I(mm(x), mm(-y))


def fit_csv():
    return os.path.join(pcb.reports_dir(), "footprint_fit.csv")


def load_fit():
    with open(fit_csv()) as f:
        rows = csv.DictReader(l for l in f if not l.startswith("# "))
        return {r["refdes"]: r for r in rows}


def load_fp(fpid):
    path = footprint_path(fpid)
    fp = pcbnew.FootprintLoad(os.path.dirname(path), os.path.basename(path)[:-len(".kicad_mod")])
    if fp is None:
        sys.exit(f"build_pcb: cannot load footprint {fpid} ({path})")
    lib, name = fpid.split(":", 1)
    fp.SetFPID(pcbnew.LIB_ID(pcbnew.UTF8(lib), pcbnew.UTF8(name)))
    return fp


def place(board, fp, x, y, side, orientation):
    board.Add(fp)  # must be on a board before Flip() (pcbnew 10 segfaults otherwise)
    pos = pcbnew.VECTOR2I(mm(x), mm(y))
    fp.SetPosition(pos)
    if side == "bottom":
        fp.Flip(pos, pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
    fp.SetOrientationDegrees(orientation)


def setup_board(board):
    c = pcb.cfg()
    copper = pcb.copper()
    board.SetCopperLayerCount(len(copper))
    layers = board.GetEnabledLayers()
    for _, _, user in copper:
        layers.AddLayer(getattr(pcbnew, user.replace(".", "_")))
    board.SetEnabledLayers(layers)
    for gname, cu, user in copper:
        board.SetLayerName(getattr(pcbnew, user.replace(".", "_")), f"Proteus.{cu.split('.')[0]}.Pours")
    ds = board.GetDesignSettings()
    ds.SetBoardThickness(mm(c.PCB_THICKNESS))
    r = c.PCB_RULES
    ds.m_TrackMinWidth = mm(r["min_track"])
    ds.m_ViasMinSize = mm(r["min_via"])
    ds.m_ViasMinAnnularWidth = mm(r["min_annular"])
    ds.m_MinThroughDrill = mm(r["min_drill"])
    ds.m_MinClearance = mm(r["clearance"])
    ds.m_HoleClearance = mm(r["hole_clearance"])
    ds.m_HoleToHoleMin = mm(r["hole_to_hole"])
    ds.m_CopperEdgeClearance = mm(r["edge_clearance"])
    ds.SetAuxOrigin(pcbnew.VECTOR2I(0, 0))
    ds.SetGridOrigin(pcbnew.VECTOR2I(0, 0))
    nc = ds.m_NetSettings.GetDefaultNetclass()
    nc.SetClearance(mm(r["clearance"]))
    nc.SetTrackWidth(mm(r["track"]))
    nc.SetViaDiameter(mm(r["via"]))
    nc.SetViaDrill(mm(r["via_drill"]))


def add_outline(board):
    g = gerber.parse(pcb.gerber_path(pcb.cfg().OUTLINE))
    n = 0
    for d in g.of(gerber.Draw):
        if d.ap.function != "Profile":
            continue
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetStart(pt(d.x1, d.y1))
        s.SetEnd(pt(d.x2, d.y2))
        s.SetWidth(mm(0.1))
        board.Add(s)
        n += 1
    return n


def board_only(board, fpid, ref, value, x, y):
    fp = load_fp(fpid)
    fp.SetReference(ref)
    fp.SetValue(value)
    fp.SetBoardOnly(True)
    fp.SetExcludedFromBOM(True)
    fp.SetExcludedFromPosFiles(True)
    place(board, fp, x, -y, "top", 0.0)
    return fp


def main():
    pcbnew.KIID.SeedGenerator(3001)  # reproducible UUIDs (distinct seed per script)
    c = pcb.cfg()
    out = pcb.board_file()
    comps, nets = pcb.export_netlist()
    fit = load_fit()
    pin_net = {}
    for name, nodes in nets.items():
        for ref, pin in nodes:
            pin_net[(ref, pin)] = name

    board = pcbnew.NewBoard(out)
    setup_board(board)
    netinfo = {}
    for name in sorted(nets):
        ni = pcbnew.NETINFO_ITEM(board, name)
        board.Add(ni)
        netinfo[name] = ni

    errors = []
    placed = skipped = 0
    for ref in sorted(comps):
        cm = comps[ref]
        if cm["excluded"]:
            skipped += 1
            continue
        if ref not in fit:
            errors.append(f"{ref}: not in {fit_csv()}")
            continue
        row = fit[ref]
        if row["footprint"] != cm["footprint"]:
            errors.append(f"{ref}: schematic footprint {cm['footprint']} != fit {row['footprint']}")
            continue
        fp = load_fp(cm["footprint"])
        fp.SetReference(ref)
        fp.SetValue(cm["value"])
        for fname, ftype in (("Datasheet", pcbnew.FIELD_T_DATASHEET), ("Description", pcbnew.FIELD_T_DESCRIPTION)):
            fp.GetField(ftype).SetText(cm["fields"].get(fname, ""))
        fp.SetPath(pcbnew.KIID_PATH(cm["sheet_tstamps"] + cm["tstamps"][0]))
        fp.SetSheetname(cm["sheetname"])
        fp.SetSheetfile(cm["sheetfile"])
        if cm["dnp"]:
            fp.SetDNP(True)
        place(board, fp, float(row["kicad_x"]), float(row["kicad_y"]), row["side"], float(row["kicad_orientation"]))
        for pad in fp.Pads():
            net = pin_net.get((ref, pad.GetNumber()))
            if net is not None:
                pad.SetNet(netinfo[net])
        placed += 1

    recs = pcb.ipc_records()
    holes = [p for p in recs if p["kind"] == "hole"]
    for i, h in enumerate(sorted(holes, key=lambda p: (-p["y_mm"], p["x_mm"])), 1):
        board_only(board, c.HOLE_FOOTPRINT, f"H{i}", c.HOLE_VALUE, h["x_mm"], h["y_mm"])

    free = [p for p in recs if p["kind"] == "free_pad"]
    if free:
        mapping, problems = pcb.ipc_to_kicad_nets(board)
        errors.extend(problems)
        prefix = getattr(c, "FREE_PAD_PREFIX", "FP")
        for i, q in enumerate(sorted(free, key=lambda p: (-p["y_mm"], p["x_mm"])), 1):
            fp = board_only(board, c.FREE_PAD_FOOTPRINT, f"{prefix}{i}", c.FREE_PAD_VALUE, q["x_mm"], q["y_mm"])
            net = mapping.get(q["net"])
            if net is None:
                errors.append(f"free pad at ({q['x_mm']}, {q['y_mm']}): IPC net {q['net']} has no KiCad net")
                continue
            for pad in fp.Pads():
                pad.SetNet(netinfo[net])

    edges = add_outline(board)
    if errors:
        for e in errors:
            print("build_pcb: ERROR " + e)
        sys.exit(1)
    pcbnew.SaveBoard(out, board)
    extra = f", {len(free)} free pads" if free else ""
    print(f"build_pcb: {placed} footprints placed ({skipped} excluded from board), {len(holes)} mounting holes{extra}, "
          f"{len(nets)} nets, {edges} outline segments -> {pcb.rel(out)}")


if __name__ == "__main__":
    main()
