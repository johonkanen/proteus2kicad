"""Footprint pad geometry vs the IPC-D-356A pads (stdlib only).

Coordinates: IPC pads are in mm with Y up (reference/ipc356_pads.json). KiCad
footprint files have Y down; `load_footprint` returns pads converted to Y up, so
all fitting happens in one right-handed frame.

`fit(fp_pads, ipc_pads, mirror)` solves the rigid transform
    board = T + R(angle) . M . fp          (Y up, angle CCW, M = x -> -x if mirror)
by least squares over pads matched by pad number, snaps the angle to a multiple
of 45 deg when it is within 0.5 deg, and returns the per-pad residuals.

In KiCad's board frame (Y down) the same placement is position (Tx, -Ty) and
  top part:    SetOrientationDegrees(angle)
  bottom part: Flip(position, FLIP_DIRECTION_LEFT_RIGHT), then
               SetOrientationDegrees((180 + angle) % 360)
(verified with pcbnew 10.0.7 against all 267 parts of ac_psu, see check_footprints.py).
"""
import itertools
import json
import math
import os
import sys

from . import board, sexpr


def ipc_pads_by_refdes():
    with open(board.path("reference", "ipc356_pads.json")) as f:
        data = json.load(f)
    out = {}
    for p in data["pads"]:
        if p["kind"] == "pad":
            out.setdefault(p["refdes"], []).append(p)
    return out


def load_footprint(path):
    """Copper pads of a .kicad_mod: dicts num, kind, shape, x, y (Y up), w, h, rot, drill."""
    node = sexpr.load(path)
    pads = []
    for pad in sexpr.find_all(node, "pad"):
        num, kind, shape = str(pad[1]), str(pad[2]), str(pad[3])
        if kind == "np_thru_hole":
            continue
        at = sexpr.find(pad, "at")
        size = sexpr.find(pad, "size")
        drill = sexpr.find(pad, "drill")
        d = 0.0
        if drill is not None:
            nums = [float(a) for a in drill[1:] if not isinstance(a, list) and _isnum(a)]
            d = nums[0] if nums else 0.0
        pads.append({
            "num": num, "kind": kind, "shape": shape,
            "x": float(at[1]), "y": -float(at[2]),
            "rot": float(at[3]) if len(at) > 3 and _isnum(at[3]) else 0.0,
            "w": float(size[1]), "h": float(size[2]), "drill": d,
        })
    return pads


def _isnum(a):
    try:
        float(a)
        return True
    except (TypeError, ValueError):
        return False


def _snap(angle):
    a = angle % 360.0
    k = round(a / 45.0)
    if abs(a - 45.0 * k) < 0.5:
        a = (45.0 * k) % 360.0
    return a


def _solve(pairs):
    """pairs: [((px, py), (qx, qy))] -> (angle_deg, tx, ty, residuals)."""
    n = len(pairs)
    pcx = sum(p[0] for p, _ in pairs) / n
    pcy = sum(p[1] for p, _ in pairs) / n
    qcx = sum(q[0] for _, q in pairs) / n
    qcy = sum(q[1] for _, q in pairs) / n
    s = c = 0.0
    for (px, py), (qx, qy) in pairs:
        px, py, qx, qy = px - pcx, py - pcy, qx - qcx, qy - qcy
        c += px * qx + py * qy
        s += px * qy - py * qx
    ang = _snap(math.degrees(math.atan2(s, c))) if n > 1 else 0.0
    ca, sa = math.cos(math.radians(ang)), math.sin(math.radians(ang))
    tx = qcx - (ca * pcx - sa * pcy)
    ty = qcy - (sa * pcx + ca * pcy)
    res = []
    for (px, py), (qx, qy) in pairs:
        bx, by = tx + ca * px - sa * py, ty + sa * px + ca * py
        res.append(math.hypot(bx - qx, by - qy))
    return ang, tx, ty, res


def fit(fp_pads, ipc_pads, mirror):
    """Match pads by number and fit. Pads sharing a number are paired by trying
    every permutation (small groups only). Returns a dict, or None-free error info:
      ok_numbers, missing (IPC pins without fp pad), extra (fp pads without IPC pin),
      angle, tx, ty, mirror, pads: [(num, fp_pad, ipc_pad, residual)], max_res."""
    m = -1.0 if mirror else 1.0
    fpg, ipg = {}, {}
    for p in fp_pads:
        fpg.setdefault(p["num"], []).append(p)
    for p in ipc_pads:
        ipg.setdefault(p["pin"], []).append(p)
    missing = sorted(set(ipg) - set(fpg))
    extra = sorted(n for n in set(fpg) - set(ipg) if n != "")
    count_diff = sorted(n for n in set(fpg) & set(ipg) if len(fpg[n]) != len(ipg[n]))
    result = {"missing": missing, "extra": extra, "count_diff": count_diff, "mirror": mirror,
              "pads": [], "max_res": float("inf"), "angle": 0.0, "tx": 0.0, "ty": 0.0}
    common = sorted(n for n in set(fpg) & set(ipg) if n not in count_diff)
    if not common:
        return result
    singles = [n for n in common if len(fpg[n]) == 1]
    multis = [n for n in common if len(fpg[n]) > 1]
    options = [list(itertools.permutations(ipg[n])) for n in multis]
    best = None
    for combo in itertools.product(*options) if options else [()]:
        pairs, meta = [], []
        for n in singles:
            f, q = fpg[n][0], ipg[n][0]
            pairs.append(((m * f["x"], f["y"]), (q["x_mm"], q["y_mm"])))
            meta.append((n, f, q))
        for n, perm in zip(multis, combo):
            for f, q in zip(fpg[n], perm):
                pairs.append(((m * f["x"], f["y"]), (q["x_mm"], q["y_mm"])))
                meta.append((n, f, q))
        ang, tx, ty, res = _solve(pairs)
        if best is None or max(res) < best[0]:
            best = (max(res), ang, tx, ty, res, meta)
    mx, ang, tx, ty, res, meta = best
    result.update(max_res=mx, angle=ang, tx=tx, ty=ty,
                  pads=[(n, f, q, r) for (n, f, q), r in zip(meta, res)])
    return result


def pad_board_shape(w, h, rot):
    """(long, short, long-axis angle mod 180) of a rectangle w x h rotated by rot."""
    if abs(w - h) < 1e-6:
        return w, h, None
    if w >= h:
        return w, h, rot % 180.0
    return h, w, (rot + 90.0) % 180.0


def size_diff(f, q, angle, mirror):
    """Compare the fp pad (after placement) with the IPC pad. Returns
    (dlong, dshort, axis_mismatch_deg or None)."""
    frot = f["rot"]
    if mirror:
        frot = 180.0 - frot
    fl, fs, fa = pad_board_shape(f["w"], f["h"], frot + angle)
    ql, qs, qa = pad_board_shape(q["pad_w"], q["pad_h"], q["rot"])
    ax = None
    if fa is not None and qa is not None:
        d = abs(fa - qa) % 180.0
        ax = min(d, 180.0 - d)
    elif (fa is None) != (qa is None) and abs(fl - fs) > 0.05 and abs(ql - qs) > 0.05:
        ax = 0.0
    return fl - ql, fs - qs, ax
