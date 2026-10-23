#!/usr/bin/env python3
"""Check the saved board against the IPC-D-356A file (run under kipy).

Pads:  every IPC component pad has a footprint pad with that refdes and pin id within
       0.05 mm, and every footprint pad has an IPC pad (same check in reverse);
       nets: IPC nets and KiCad nets map one-to-one through the pads; N/C pins and pins
       without an IPC net sit on no net or KiCad's "unconnected-(...)" net.
Holes: every IPC 'hole' record has exactly one board-only pad at its position with the IPC
       drill and pad size, plated as the IPC record says, no net.
Free pads: every IPC 'free_pad' record has exactly one board-only pad there with the IPC
       drill and pad size, plated, on the IPC net (mapped to KiCad through the pads).
Vias:  every IPC via has a board via at its position (0.01 mm) with the IPC drill and pad
       size on the IPC net, and every board via is an IPC via.
Copper: every track and via has a net, every zone has a net (after the full make_pcb run).

A board wrapper may call main(added=..., extra_vias=...): `added` = {refdes: {"pins": {pin:
net}}} for parts added after the Proteus design (no IPC pads; their pads must be on the listed
nets), `extra_vias` = [(x, y, net)] vias added after the Proteus design (IPC frame).

Usage: tools/p2k/bin/kipy -m p2k.check_board [--pads-only]
"""
import math
import sys

import pcbnew

from . import pcb as bc


def mm(v):
    return pcbnew.ToMM(v)


def check_pads(board, recs, errors, added):
    ipc = {}
    for p in recs:
        if p["kind"] == "pad":
            ipc.setdefault((p["refdes"], p["pin"]), []).append(p)
    mapping, problems = bc.ipc_to_kicad_nets(board)
    errors.extend(problems)
    worst, npads, used = 0.0, 0, set()
    addpads, pending = [], []
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        if fp.IsBoardOnly():
            continue
        for pad in fp.Pads():
            if not pad.IsOnCopperLayer():
                continue  # paste/mask-only aperture (unnumbered), not a pad
            npads += 1
            key = (ref, pad.GetNumber())
            c = pad.GetPosition()
            x, y = mm(c.x), -mm(c.y)
            cands = ipc.get(key, [])
            a = added.get(ref)
            if a and not cands:
                # added part: no Proteus position; its pads must be on the listed nets
                want = a["pins"].get(pad.GetNumber())
                if want is None:
                    errors.append(f"{ref}-{pad.GetNumber()}: added part pad not in the added-parts list")
                else:
                    addpads.append(f"{ref}-{pad.GetNumber()}@({x:.3f},{y:.3f})")
                    pending.append((ref, pad.GetNumber(), want, pad.GetNetname()))
                continue
            if not cands:
                errors.append(f"{ref}-{pad.GetNumber()}: footprint pad without IPC pad")
                continue
            q = min(cands, key=lambda q: math.hypot(x - q["x_mm"], y - q["y_mm"]))
            d = math.hypot(x - q["x_mm"], y - q["y_mm"])
            used.add(id(q))
            worst = max(worst, d)
            if d > bc.TOL:
                errors.append(f"{ref}-{pad.GetNumber()}: {d:.3f} mm from the IPC pad")
            kn = pad.GetNetname()
            if q["net_raw"] == "N/C" or not q["net"]:
                if not bc.is_unconnected_net(kn):
                    errors.append(f"{ref}-{pad.GetNumber()}: N/C in IPC but on net {kn}")
            elif mapping.get(q["net"]) != kn:
                errors.append(f"{ref}-{pad.GetNumber()}: net {kn}, IPC net {q['net']} -> {mapping.get(q['net'])}")
    for ref, pin, want, kn in pending:
        if mapping.get(want, want) != kn:
            errors.append(f"{ref}-{pin}: net {kn}, added-parts list {want} -> {mapping.get(want)}")
    if addpads:
        print(f"check_board: added-part pads without Proteus pad (nets checked): {' '.join(addpads)}")
    for key, qs in ipc.items():
        for q in qs:
            if id(q) not in used:
                errors.append(f"{key[0]}-{key[1]}: IPC pad without footprint pad")
    return worst, npads, mapping


def check_board_only(board, recs, mapping, kind, errors):
    """IPC 'hole' / 'free_pad' records vs the board-only footprint pads."""
    want = [p for p in recs if p["kind"] == kind]
    found = 0
    for h in want:
        hit = []
        for fp in board.GetFootprints():
            if not fp.IsBoardOnly():
                continue
            for pad in fp.Pads():
                c = pad.GetPosition()
                if math.hypot(mm(c.x) - h["x_mm"], -mm(c.y) - h["y_mm"]) < 0.01:
                    hit.append((fp, pad))
        if len(hit) != 1:
            errors.append(f"{kind} at ({h['x_mm']}, {h['y_mm']}): {len(hit)} board pads")
            continue
        found += 1
        fp, pad = hit[0]
        attrib = pcbnew.PAD_ATTRIB_PTH if h["plated"] else pcbnew.PAD_ATTRIB_NPTH
        net = mapping.get(h["net"]) if h["net"] else ""
        if abs(mm(pad.GetDrillSizeX()) - h["drill"]) > 1e-3 or abs(mm(pad.GetSizeX()) - h["pad_w"]) > 1e-3 \
                or pad.GetAttribute() != attrib or pad.GetNetname() != (net or ""):
            errors.append(f"{fp.GetReference()}: {kind} {mm(pad.GetDrillSizeX())}/{mm(pad.GetSizeX())} mm, "
                          f"plated {pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH}, net {pad.GetNetname()!r} "
                          f"(want {h['drill']}/{h['pad_w']} mm, plated {h['plated']}, net {net!r})")
    return len(want), found


def check_copper(board, recs, mapping, errors, extra_vias):
    vias = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA"]
    tracks = [t for t in board.GetTracks() if t.GetClass() != "PCB_VIA"]
    ipc_vias = [p for p in recs if p["kind"] == "via"]
    grid = {}
    for v in vias:
        c = v.GetPosition()
        grid.setdefault((round(mm(c.x), 1), round(-mm(c.y), 1)), []).append(v)
    matched = set()
    for q in ipc_vias:
        hit = None
        for dx in (-0.1, 0, 0.1):
            for dy in (-0.1, 0, 0.1):
                for v in grid.get((round(q["x_mm"] + dx, 1), round(q["y_mm"] + dy, 1)), []):
                    c = v.GetPosition()
                    if math.hypot(mm(c.x) - q["x_mm"], -mm(c.y) - q["y_mm"]) < 0.01:
                        hit = v
        if hit is None:
            errors.append(f"IPC via ({q['x_mm']}, {q['y_mm']}) {q['net']}: no board via")
            continue
        matched.add(hit.m_Uuid.AsString())
        want = mapping.get(q["net"])
        if want is None:
            errors.append(f"IPC via ({q['x_mm']}, {q['y_mm']}): IPC net {q['net']} has no KiCad net")
        elif hit.GetNetname() != want:
            errors.append(f"via ({q['x_mm']}, {q['y_mm']}): net {hit.GetNetname()!r}, IPC {q['net']} -> {want}")
        if abs(mm(hit.GetDrillValue()) - q["drill"]) > 1e-3 or abs(mm(hit.GetWidth(pcbnew.F_Cu)) - q["pad_w"]) > 1e-3:
            errors.append(f"via ({q['x_mm']}, {q['y_mm']}): {mm(hit.GetWidth(pcbnew.F_Cu))}/{mm(hit.GetDrillValue())} mm, "
                          f"IPC {q['pad_w']}/{q['drill']} mm")
    nextra = 0
    for v in vias:
        if v.m_Uuid.AsString() not in matched:
            c = v.GetPosition()
            x, y = mm(c.x), -mm(c.y)
            hit = [d for d in extra_vias if math.hypot(d[0] - x, d[1] - y) < 0.01]
            if not hit:
                errors.append(f"board via ({x:.3f}, {y:.3f}) is neither an IPC via nor a listed added via")
            elif mapping.get(hit[0][2], hit[0][2]) != v.GetNetname():
                errors.append(f"added via ({x:.3f}, {y:.3f}): net {v.GetNetname()}, list {hit[0][2]}")
            else:
                nextra += 1
    if nextra != len(extra_vias):
        errors.append(f"{len(extra_vias)} added vias listed, {nextra} found on the board")
    if extra_vias:
        print(f"check_board: {nextra} added vias at their positions and nets")
    nonet = [t for t in tracks if t.GetNetCode() <= 0]
    if nonet:
        errors.append(f"{len(nonet)} tracks without net")
    zones = list(board.Zones())
    zn = [z for z in zones if z.GetNetCode() <= 0 and not z.GetIsRuleArea()]
    if zn:
        errors.append(f"{len(zn)} zones without net")
    return len(tracks), len(vias), len(zones)


def main(argv=None, added=None, extra_vias=None):
    argv = sys.argv[1:] if argv is None else argv
    board = pcbnew.LoadBoard(bc.board_file())
    recs = bc.ipc_records()
    errors = []
    worst, npads, mapping = check_pads(board, recs, errors, added or {})
    nholes, _ = check_board_only(board, recs, mapping, "hole", errors)
    nfree, _ = check_board_only(board, recs, mapping, "free_pad", errors)
    nbo = sum(1 for fp in board.GetFootprints() if fp.IsBoardOnly())
    if nbo != nholes + nfree:
        errors.append(f"{nbo} board-only footprints, {nholes} IPC holes + {nfree} IPC free pads")
    extra = f", {nfree} free pads" if nfree else ""
    print(f"check_board: {len(board.GetFootprints())} footprints, {npads} pads, worst pad offset {worst:.3f} mm "
          f"(limit {bc.TOL}), {len(mapping)} IPC nets mapped 1:1, {nholes} mounting holes{extra}")
    if "--pads-only" not in argv:
        nt, nv, nz = check_copper(board, recs, mapping, errors, extra_vias or [])
        print(f"check_board: {nt} tracks, {nv} vias, {nz} zones")
    for e in errors[:60]:
        print("check_board: ERROR " + e)
    if len(errors) > 60:
        print(f"check_board: ... {len(errors) - 60} more errors")
    print("check_board: " + ("FAILED" if errors else "OK"))
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
