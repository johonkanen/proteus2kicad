#!/usr/bin/env python3
"""Geometric check of the schematic's footprints against the IPC-D-356A pads (stdlib only).

For every IPC refdes: take the Footprint field from the schematic (kicad/*.kicad_sch), load
the footprint (<PROJECT>: -> lib/<PROJECT>.pretty, others from the KiCad AppImage libraries),
match pads by number and solve the rigid transform (rotation, mirror for bottom-side parts,
translation) onto the IPC pad centres (p2k.fpgeom). Fails when a part has no footprint, the
footprint differs from lib/footprint_map.csv, pad numbers differ, or any pad is more than
0.05 mm off. Pad size / drill differences are only reported.

THT pads are on both sides in the IPC file, so a THT part is assumed on top; it is reported
as bottom only when the pad pattern fits mirrored but not unmirrored (chiral pattern).

Writes reports/footprint_fit.csv (placement per part: side, angle, origin in IPC
coordinates and as KiCad position/orientation, worst residual - input for Phase 3
placement; the KiCad recipe is in p2k.fpgeom) and
reports/footprint_pads.csv (residual and size difference per pad).

With --pcbnew (run under tools/p2k/bin/kipy) every footprint is also loaded with pcbnew, placed
on an empty BOARD with that KiCad recipe, and its pad positions are compared with the IPC pads
again - this pins down the placement convention Phase 3 uses.

Usage: tools/p2k/bin/p2k check_footprints [-v]
       tools/p2k/bin/kipy -m p2k.check_footprints --pcbnew [-v]

A board script may call main(added=...) with {refdes: {"footprint": fpid, "pins": [...]}} for
parts added after the Proteus design (no IPC pads): their footprint and pad numbers are
checked, nothing is fitted.
"""
import csv
import glob
import os
import sys

from . import board, fpgeom, sexpr
from .footprints import footprint_path, map_csv_path

TOL = 0.05


def schematic_footprints():
    """refdes -> (set of footprints over all units, on_board)."""
    out = {}
    for path in sorted(glob.glob(board.path("kicad", "*.kicad_sch"))):
        for s in sexpr.find_all(sexpr.load(path), "symbol"):
            props = {p[1]: p[2] for p in sexpr.find_all(s, "property")}
            ref = props.get("Reference", "")
            if not ref or ref.startswith("#"):
                continue
            ob = sexpr.find(s, "on_board")
            fps, on_board = out.get(ref, (set(), True))
            fps.add(props.get("Footprint", ""))
            out[ref] = (fps, on_board and not (ob and ob[1] == "no"))
    return out


def best_fit(fp_pads, pads):
    side = pads[0]["side"]
    if side in ("top", "bottom"):
        return fpgeom.fit(fp_pads, pads, side == "bottom")
    top = fpgeom.fit(fp_pads, pads, False)
    if top["max_res"] <= TOL:
        return top
    bot = fpgeom.fit(fp_pads, pads, True)
    return bot if bot["max_res"] < top["max_res"] else top


def pcbnew_check(fit_rows, ipc):
    """Place every footprint with pcbnew using the KiCad columns of footprint_fit.csv and
    return (worst pad offset, [errors])."""
    import math
    import pcbnew
    brd = pcbnew.BOARD()
    worst, errs = 0.0, []
    for row in fit_rows:
        ref, fpid, side = row[0], row[1], row[2]
        kx, ky, kang = float(row[6]), float(row[7]), float(row[8])
        path = footprint_path(fpid)
        fp = pcbnew.FootprintLoad(os.path.dirname(path), os.path.basename(path)[:-len(".kicad_mod")])
        if fp is None:
            errs.append(f"{ref}: pcbnew cannot load {fpid}")
            continue
        brd.Add(fp)
        pos = pcbnew.VECTOR2I(pcbnew.FromMM(kx), pcbnew.FromMM(ky))
        fp.SetPosition(pos)
        if side == "bottom":
            fp.Flip(pos, pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        fp.SetOrientationDegrees(kang)
        byp = {}
        for q in ipc[ref]:
            byp.setdefault(q["pin"], []).append(q)
        mx = 0.0
        for pad in fp.Pads():
            if pad.GetNumber() == "":
                continue  # paste-only segments of a big pad (e.g. LFPAK tab)
            c = pad.GetPosition()
            px, py = pcbnew.ToMM(c.x), -pcbnew.ToMM(c.y)
            qs = byp.get(pad.GetNumber(), [])
            mx = max(mx, min((math.hypot(px - q["x_mm"], py - q["y_mm"]) for q in qs), default=float("inf")))
        if mx > TOL:
            errs.append(f"{ref}: pcbnew placement off by {mx:.3f} mm")
        worst = max(worst, mx)
    return worst, errs


def main(argv=None, added=None):
    argv = sys.argv[1:] if argv is None else argv
    verbose = "-v" in argv
    added = added or {}
    ipc = fpgeom.ipc_pads_by_refdes()
    sch = schematic_footprints()
    with open(map_csv_path()) as f:
        fmap = {r["refdes"]: r["footprint"] for r in csv.DictReader(l for l in f if not l.startswith("# "))}
    errors, notes = [], []
    cache = {}
    fit_rows, pad_rows = [], []
    per_fp = {}
    added_checked = []
    for ref in sorted(sch):
        fps, on_board = sch[ref]
        if not on_board:
            if fps - {""}:
                errors.append(f"{ref}: excluded from board but has footprint {sorted(fps)}")
            continue
        if ref not in ipc:
            a = added.get(ref)
            if not a:
                errors.append(f"{ref}: on the board in the schematic but not in the IPC file")
                continue
            # added part: footprint as listed, pads = the listed pins (no Proteus geometry to fit)
            fpid = next(iter(fps)) if len(fps) == 1 else None
            if fpid != a["footprint"] or fmap.get(ref) != fpid:
                errors.append(f"{ref}: footprint {sorted(fps)}, map {fmap.get(ref)}, added-parts list {a['footprint']}")
                continue
            path = footprint_path(fpid)
            nums = {p["num"] for p in fpgeom.load_footprint(path)} if os.path.exists(path) else set()
            if nums != set(a["pins"]):
                errors.append(f"{ref}: footprint pads {sorted(nums)} != listed pins {sorted(a['pins'])}")
            added_checked.append(ref)
    for ref in sorted(ipc):
        if ref not in sch:
            errors.append(f"{ref}: not in the schematic")
            continue
        fps, _ = sch[ref]
        if len(fps) != 1 or "" in fps:
            errors.append(f"{ref}: footprint field {sorted(fps)} (need exactly one, non-empty)")
            continue
        fpid = next(iter(fps))
        if fmap.get(ref) != fpid:
            errors.append(f"{ref}: schematic footprint {fpid} != lib/footprint_map.csv {fmap.get(ref)} "
                          "(re-run the sheet scripts)")
        if fpid not in cache:
            path = footprint_path(fpid)
            if not os.path.exists(path):
                errors.append(f"{ref}: footprint {fpid} not found ({path})")
                continue
            cache[fpid] = fpgeom.load_footprint(path)
        pads = ipc[ref]
        r = best_fit(cache[fpid], pads)
        bad = []
        if r["missing"]:
            bad.append(f"IPC pins without pad {r['missing']}")
        if r["extra"]:
            bad.append(f"footprint pads not in IPC {r['extra']}")
        if r["count_diff"]:
            bad.append(f"pad count differs for {r['count_diff']}")
        if r["max_res"] > TOL:
            worst = sorted(r["pads"], key=lambda t: -t[3])[:3]
            bad.append(f"residual {r['max_res']:.3f} mm (" +
                       ", ".join(f"pad {n} {res:.3f}" for n, _, _, res in worst) + ")")
        if bad:
            errors.append(f"{ref} ({fpid}): " + "; ".join(bad))
        side = "bottom" if r["mirror"] else "top"
        dsz, ddr, axis = 0.0, 0.0, 0.0
        for num, f, q, res in r["pads"]:
            dl, ds, ax = fpgeom.size_diff(f, q, r["angle"], r["mirror"])
            dd = (f["drill"] - q["drill"]) if q["is_tht"] else 0.0
            dsz = max(dsz, abs(dl), abs(ds))
            ddr = max(ddr, abs(dd))
            axis = max(axis, ax or 0.0)
            pad_rows.append([ref, num, f"{res:.4f}", f"{q['x_mm']:.3f}", f"{q['y_mm']:.3f}",
                             f"{q['pad_w']:g}x{q['pad_h']:g}", f"{f['w']:g}x{f['h']:g}",
                             f"{dl:+.3f}", f"{ds:+.3f}", "" if ax is None else f"{ax:g}",
                             f"{q['drill']:g}" if q["is_tht"] else "", f"{f['drill']:g}" if q["is_tht"] else ""])
        kang = (180.0 + r["angle"]) % 360.0 if r["mirror"] else r["angle"]
        fit_rows.append([ref, fpid, side, f"{r['angle']:g}", f"{r['tx']:.4f}", f"{r['ty']:.4f}",
                         f"{r['tx']:.4f}", f"{-r['ty']:.4f}", f"{kang:g}",
                         f"{r['max_res']:.4f}", f"{dsz:.3f}", f"{ddr:.3f}", f"{axis:g}"])
        agg = per_fp.setdefault(fpid, {"refs": [], "res": 0.0, "dsz": 0.0, "ddr": 0.0, "axis": 0.0})
        agg["refs"].append(ref)
        agg["res"] = max(agg["res"], r["max_res"])
        agg["dsz"] = max(agg["dsz"], dsz)
        agg["ddr"] = max(agg["ddr"], ddr)
        agg["axis"] = max(agg["axis"], axis)
        if pads[0]["is_tht"] and r["mirror"]:
            notes.append(f"{ref}: THT pad pattern fits only mirrored -> bottom side")
    reports = board.path("reports")
    os.makedirs(reports, exist_ok=True)
    with open(os.path.join(reports, "footprint_fit.csv"), "w", newline="") as f:
        f.write("# IPC frame (mm, Y up): board = (x, y) + R(angle) . M . pad, M = mirror x for bottom. "
                "KiCad (Y down): SetPosition(kicad_x, kicad_y); bottom: Flip(pos, LEFT_RIGHT); "
                "then SetOrientationDegrees(kicad_orientation)\n")
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["refdes", "footprint", "side", "angle", "x", "y", "kicad_x", "kicad_y", "kicad_orientation",
                    "max_residual", "max_pad_size_diff",
                    "max_drill_diff", "max_pad_axis_diff"])
        w.writerows(fit_rows)
    with open(os.path.join(reports, "footprint_pads.csv"), "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["refdes", "pad", "residual", "ipc_x", "ipc_y", "ipc_size", "fp_size", "d_long", "d_short",
                    "axis_diff", "ipc_drill", "fp_drill"])
        w.writerows(pad_rows)
    if verbose or errors:
        print(f"{'footprint':58s} {'parts':>5s} {'max res':>8s} {'d size':>7s} {'d drill':>7s}")
        for fpid in sorted(per_fp):
            a = per_fp[fpid]
            print(f"{fpid:58s} {len(a['refs']):5d} {a['res']:8.3f} {a['dsz']:7.3f} {a['ddr']:7.3f}"
                  + (f"  axis {a['axis']:g}" if a["axis"] > 1 else ""))
    if "--pcbnew" in argv:
        pworst, perrs = pcbnew_check(fit_rows, ipc)
        errors.extend(perrs)
        print(f"check_footprints: pcbnew placement of {len(fit_rows)} parts, worst pad offset {pworst:.3f} mm")
    for n in notes:
        print("check_footprints: note: " + n)
    for e in errors:
        print("check_footprints: ERROR " + e)
    worst = max((float(r[9]) for r in fit_rows), default=0.0)
    print(f"check_footprints: {len(fit_rows)}/{len(ipc)} parts fitted, {len(pad_rows)} pads, "
          f"{len(per_fp)} footprints, worst residual {worst:.3f} mm (limit {TOL})"
          + (f"; added parts without IPC pads (footprint/pads checked): {' '.join(added_checked)}"
             if added_checked else ""))
    print("check_footprints: " + ("FAILED" if errors else "OK"))
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
