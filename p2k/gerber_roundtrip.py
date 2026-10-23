#!/usr/bin/env python3
"""Phase 4: gerber round trip - KiCad's copper gerbers vs the Proteus CADCAM gerbers
(system python3: numpy, scipy, PIL; kicad-cli for the export).

Exports the copper layers and the drills of the board with kicad-cli (gerbers, drill as
gerber), parses both sets with p2k.gerber and rasterises every copper layer of both on the
same grid (`res` mm per pixel, objects drawn in file order with their polarity, so Proteus'
clear regions cut the pours exactly as a plotter would). Per layer:
  raw XOR area, and the differing area after a `tol` mm tolerance (a pixel counts only when
  it is farther than `tol` from any copper of the other set - absorbs the rounding of pad
  corners, zone edges and the raster itself);
  the difference clusters (8-connected), each with side (only Proteus / only KiCad), area,
  position (IPC frame), the nearest IPC pad with its net, the nearest via, and a category:
    pad shape   inside the XOR of the two sets' pad flashes: a KiCad pad kept with the KiCad
                library size or shape (round-rect corners, larger / smaller pads, ...);
    clearance   Proteus copper within clearance + tol of a KiCad pad that is larger than the
                Proteus pad: KiCad's zone clearance around the bigger pad;
    zone fill   Proteus copper thinner than the zone minimum width (slivers KiCad does not
                fill);
    deviation   inside an area the board wrapper lists as intentionally changed (hook);
    other       everything else - a finding, listed in full.
  An overlay PNG per layer (reports/roundtrip/<layer>.png, `overlay_res` mm per pixel):
  common copper grey, only-Proteus red, only-KiCad cyan, differences absorbed by the
  tolerance dim.
Coverage: the centre line of every draw and the centre of every flash of each set must lie on
copper of the other set (1 px slack) - finds features thinner than 2 * tol, which the XOR with
tolerance cannot see. Samples inside the clearance / pad shape masks are explained.
Drills: every Proteus drill flash (plated file, and the non-plated file when the board has
one) must have a KiCad drill of the same kind within tol and vice versa. Proteus records
stacked on one spot (identical position and diameter) share one KiCad drill and are counted
(STACKED_VIAS). A KiCad drill may be larger than the Proteus drill (footprints kept with the
KiCad drill), smaller only when the pair is listed in DRILL_SMALLER (an open user decision
recorded in the config). DRILL_EXCELLON (optional): an Excellon drill file of another Proteus
export of the same board, compared 1:1 with the plated + non-plated gerber flashes.
Report: reports/roundtrip/roundtrip.txt (per layer: areas, categories, every cluster of at
least `min_area` mm^2, coverage misses, pad shape clusters per footprint; drill tables).
Exit status 1 (--check) when an 'other' cluster of at least `min_area` mm^2 exists, a draw
or flash is not covered, or a drill does not match.

Config keys: PROJECT, CADCAM, COPPER, OUTLINE, DRILL_PLATED, DRILL_NONPLATED, VIA_DRILLS,
PCB_RULES (clearance, min_track = zone minimum width); optional ROUNDTRIP = dict(res, tol,
min_area, overlay_res, drill_tol), DRILL_SMALLER = {(proteus_mm, kicad_mm): reason},
STACKED_VIAS (expected stacked duplicate via records, default 0), DRILL_EXCELLON (path).
Board wrapper hooks: main(argv, in_area=fn(layer, x, y) -> bool, extra_vias=[(x, y, net,
note), ...]) for intentional deviations of a later design revision.

Usage: tools/p2k/bin/p2k gerber_roundtrip [--check] [--kicad-dir DIR] [--keep]
  --kicad-dir  reuse KiCad gerbers exported earlier (default: export to a temp dir)
  --keep       keep the exported gerbers in reports/roundtrip/kicad_gerbers/
"""
import argparse
import csv
import glob
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from . import board as _board
from . import gerber
from . import pcb as bc

DEFAULTS = dict(
    res=0.025,          # mm per pixel of the comparison raster
    tol=0.05,           # mm, geometric tolerance (same as the pad position tolerance)
    min_area=0.01,      # mm^2, clusters below this are only counted
    overlay_res=0.1,    # mm per pixel of the overlay PNGs
    drill_tol=0.005,    # mm, drill diameter tolerance (KiCad plots drills with 1 um, Proteus 0.1 um)
)
# Pad flashes of either plotter: Proteus ComponentPad / SMDPad,CuDef / OtherPad,Unknown; KiCad
# ComponentPad / SMDPad,CuDef / WasherPad (copper ring of an unplated hole).
PAD_FUNCTIONS = ("ComponentPad", "SMDPad", "OtherPad", "WasherPad")


class Settings:
    def __init__(self):
        cfg = bc.cfg()
        rt = dict(DEFAULTS)
        rt.update(getattr(cfg, "ROUNDTRIP", {}) or {})
        self.res = rt["res"]
        self.tol = rt["tol"]
        self.min_area = rt["min_area"]
        self.overlay_res = rt["overlay_res"]
        self.drill_tol = rt["drill_tol"]
        self.clearance = cfg.PCB_RULES["clearance"]
        self.zone_min_width = cfg.PCB_RULES["min_track"]
        self.layers = [(g, k) for g, k, _ in bc.copper()]
        self.drill_plated = cfg.DRILL_PLATED
        self.drill_nonplated = getattr(cfg, "DRILL_NONPLATED", None)
        self.via_drills = tuple(cfg.VIA_DRILLS)
        self.stacked_vias = getattr(cfg, "STACKED_VIAS", 0)
        self.excellon = getattr(cfg, "DRILL_EXCELLON", None)
        self.project = os.path.splitext(os.path.basename(bc.board_file()))[0]
        self.out = os.path.join(bc.reports_dir(), "roundtrip")


S = None  # Settings, set in main()


def is_pad(o):
    if isinstance(o, gerber.Flash):
        return (o.ap.function or "").split(",")[0] in PAD_FUNCTIONS
    return isinstance(o, gerber.Region) and "P" in o.attrs   # KiCad custom pad shape


def export_kicad(outdir):
    cli = os.path.join(_board.BIN, "kicad-cli")
    layers = ",".join(k for _, k in S.layers)
    board = bc.board_file()
    for args in (["pcb", "export", "gerbers", "-o", outdir + "/", "--layers", layers, board],
                 ["pcb", "export", "drill", "-o", outdir + "/", "--format", "gerber", board]):
        r = subprocess.run([cli] + args, capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"kicad-cli {' '.join(args[:3])} failed:\n{r.stdout}{r.stderr}")


def kicad_layer_file(kdir, klayer):
    paths = glob.glob(os.path.join(kdir, f"{S.project}-{klayer.replace('.', '_')}.*"))
    if len(paths) != 1:
        sys.exit(f"roundtrip: KiCad gerber of {klayer} not found in {kdir}: {paths}")
    return paths[0]


def board_bbox():
    """Bounding box of the Proteus board outline, mm."""
    g = gerber.parse(bc.gerber_path(bc.cfg().OUTLINE))
    xs, ys = [], []
    for d in g.of(gerber.Draw):
        xs += [d.x1, d.x2]
        ys += [d.y1, d.y2]
    return min(xs), min(ys), max(xs), max(ys)


class Canvas:
    """Rasteriser: mm (Y up) -> pixels (row 0 = top edge), PIL 'L' image, 255 = copper."""

    def __init__(self, bbox, margin=1.0):
        x0, y0, x1, y1 = bbox
        self.res = S.res
        self.x0, self.y1 = x0 - margin, y1 + margin
        self.w = int(math.ceil((x1 - x0 + 2 * margin) / self.res))
        self.h = int(math.ceil((y1 - y0 + 2 * margin) / self.res))
        self.im = Image.new("L", (self.w, self.h), 0)
        self.d = ImageDraw.Draw(self.im)

    def px(self, x, y):
        return ((x - self.x0) / self.res, (self.y1 - y) / self.res)

    def mm(self, col, row):
        return (self.x0 + col * self.res, self.y1 - row * self.res)

    def circle(self, cx, cy, dia, v):
        r = dia / 2 / self.res
        px, py = self.px(cx, cy)
        self.d.ellipse([px - r, py - r, px + r, py + r], fill=v)

    def poly(self, pts, v):
        if len(pts) >= 3:
            self.d.polygon([self.px(x, y) for x, y in pts], fill=v)

    def rect(self, cx, cy, w, h, v):
        w, h = w / 2, h / 2
        self.poly([(cx - w, cy - h), (cx + w, cy - h), (cx + w, cy + h), (cx - w, cy + h)], v)

    def line(self, x1, y1, x2, y2, w, v):
        dx, dy = x2 - x1, y2 - y1
        L = math.hypot(dx, dy)
        if L > 0:
            nx, ny = -dy / L * w / 2, dx / L * w / 2
            self.poly([(x1 + nx, y1 + ny), (x2 + nx, y2 + ny), (x2 - nx, y2 - ny), (x1 - nx, y1 - ny)], v)
            self.circle(x2, y2, w, v)
        self.circle(x1, y1, w, v)

    def flash(self, f, v):
        ap = f.ap
        if ap.shape == "C":
            self.circle(f.x, f.y, ap.params[0], v)
        elif ap.shape == "R":
            self.rect(f.x, f.y, ap.params[0], ap.params[1], v)
        elif ap.shape == "O":
            w, h = ap.params
            if abs(w - h) < 1e-9:
                self.circle(f.x, f.y, w, v)
            elif w > h:
                self.line(f.x - (w - h) / 2, f.y, f.x + (w - h) / 2, f.y, h, v)
            else:
                self.line(f.x, f.y - (h - w) / 2, f.x, f.y + (h - w) / 2, w, v)
        else:
            for p in ap.primitives:
                if p[0] == "poly":
                    self.poly([(f.x + x, f.y + y) for x, y in p[1]], v)
                else:
                    self.circle(f.x + p[2], f.y + p[3], p[1], v)

    def obj(self, o, v=None):
        if v is None:
            v = 255 if o.dark else 0
        if isinstance(o, gerber.Flash):
            self.flash(o, v)
        elif isinstance(o, gerber.Draw):
            if o.ap.shape != "C":
                raise ValueError(f"draw with a {o.ap.shape} aperture")
            self.line(o.x1, o.y1, o.x2, o.y2, o.ap.params[0], v)
        else:
            for c in o.contours:
                self.poly(c, v)

    def array(self):
        return np.asarray(self.im) > 0


def disk(r_px):
    r = int(math.ceil(r_px))
    yy, xx = np.ogrid[-r:r + 1, -r:r + 1]
    return (xx * xx + yy * yy) <= r_px * r_px + 1e-9


def grow(mask, r_mm):
    return ndimage.binary_dilation(mask, structure=disk(r_mm / S.res))


def render(g, bbox):
    """(copper, pads, canvas) of one gerber: everything in file order, and the pad flashes
    alone."""
    full, pads = Canvas(bbox), Canvas(bbox)
    for o in g.objects:
        if isinstance(o, gerber.Draw) and o.ap.function == "Profile":
            continue   # Proteus draws the board outline into every copper gerber: not copper
        full.obj(o)
        if is_pad(o):
            pads.obj(o)
    return full.array(), pads.array(), full


def coverage(g, other, canvas, explained):
    """Draws and flashes of gerber g whose centre line / centre is not on copper of the other
    set (other = boolean raster, 1 px of slack). Catches features thinner than 2 * tol, which
    the XOR with tolerance cannot see. Samples inside `explained` (the clearance / pad shape
    masks) are counted separately. Returns [(kind, x, y, width/dia, function, attrs, off,
    explained, samples)]."""
    cover = ndimage.binary_dilation(other, structure=np.ones((3, 3)))
    h, w = cover.shape
    missing = []

    def on_copper(x, y):
        c, r = canvas.px(x, y)
        c, r = int(c), int(r)
        if not (0 <= r < h and 0 <= c < w):
            return False
        return True if cover[r, c] else ("explained" if explained[r, c] else False)

    for o in g.objects:
        if not o.dark:
            continue
        if isinstance(o, gerber.Draw):
            if o.ap.function == "Profile":
                continue
            n = max(2, int(math.hypot(o.x2 - o.x1, o.y2 - o.y1) / (S.res / 2)) + 1)
            res = Counter(on_copper(o.x1 + (o.x2 - o.x1) * i / (n - 1), o.y1 + (o.y2 - o.y1) * i / (n - 1)) for i in range(n))
            if res[False] or res["explained"]:
                missing.append(("draw", (o.x1 + o.x2) / 2, (o.y1 + o.y2) / 2, o.ap.params[0], o.ap.function, o.attrs,
                                res[False], res["explained"], n))
        elif isinstance(o, gerber.Flash):
            r = on_copper(o.x, o.y)
            if r is not True:
                missing.append(("flash", o.x, o.y, o.ap.params[0] if o.ap.params else 0, o.ap.function, o.attrs,
                                int(r is False), int(r == "explained"), 1))
    return missing


def load_footprints():
    out = {}
    path = _board.path("lib", "footprint_map.csv")
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for row in csv.DictReader(r for r in f if not r.startswith("#")):
            out[row["refdes"]] = row["footprint"]
    return out


def load_ipc():
    """(pads per KiCad copper layer, vias) from the IPC records (through the configured filter).
    Free pads and holes count as pads (they carry copper on every layer)."""
    recs = bc.ipc_records()
    pads = {k: [] for _, k in S.layers}
    top, bottom = S.layers[0][1], S.layers[-1][1]
    for p in recs:
        if p["kind"] not in ("pad", "free_pad", "hole"):
            continue
        for _, k in S.layers:
            if p["side"] == "both" or (k == top and p["side"] == "top") or (k == bottom and p["side"] == "bottom"):
                pads[k].append(p)
    vias = [p for p in recs if p["kind"] == "via"]
    return pads, vias


def nearest(recs, x, y):
    if not recs:
        return None, math.inf
    xy = np.array([(p["x_mm"], p["y_mm"]) for p in recs])
    d = np.hypot(xy[:, 0] - x, xy[:, 1] - y)
    i = int(d.argmin())
    return recs[i], float(d[i])


def pad_label(p):
    if p is None:
        return "-"
    ref = p["refdes"] or p["kind"]
    pin = f"-{p['pin']}" if p["pin"] else ""
    return f"{ref}{pin} ({p['net'] or p['net_raw'] or 'no net'})"


def compare_layer(layer, gp, gk, bbox, ipc_pads, ipc_vias, report, footprints, in_area):
    """Rasterise, XOR, cluster and classify one layer. Returns (summary dict, overlay arrays)."""
    tol, min_area = S.tol, S.min_area
    P, Ppads, canvas = render(gp, bbox)
    K, Kpads, _ = render(gk, bbox)
    px_area = S.res * S.res
    xor = P ^ K
    onlyP = P & ~grow(K, tol)
    onlyK = K & ~grow(P, tol)
    diff = onlyP | onlyK
    # classification masks
    pad_xor = grow(Ppads ^ Kpads, tol)
    k_bigger = Kpads & ~Ppads
    clearance = grow(k_bigger, S.clearance + tol) & ~Kpads
    miss_P = coverage(gp, K, canvas, clearance | pad_xor)   # Proteus items without KiCad copper under them
    miss_K = coverage(gk, P, canvas, pad_xor)               # KiCad items without Proteus copper under them
    devmiss = [m for m in miss_P + miss_K if m[6] and in_area(layer, m[1], m[2])]
    unexpl_P = [m for m in miss_P if m[6] and m not in devmiss]
    unexpl_K = [m for m in miss_K if m[6] and m not in devmiss]
    # Proteus copper too thin for the zone fill: copper that an opening of the zone min width removes
    thin = P & ~ndimage.binary_opening(P, structure=disk(S.zone_min_width / 2 / S.res))
    labels, n = ndimage.label(diff, structure=np.ones((3, 3)))
    objs = ndimage.find_objects(labels)
    clusters = []
    for i, sl in enumerate(objs, 1):
        m = labels[sl] == i
        area = int(m.sum()) * px_area
        side = "Proteus" if onlyP[sl][m].sum() >= onlyK[sl][m].sum() else "KiCad"
        rows, cols = np.nonzero(m)
        cy = sl[0].start + rows.mean()
        cx = sl[1].start + cols.mean()
        x, y = canvas.mm(cx + 0.5, cy + 0.5)
        hgt, wid = (sl[0].stop - sl[0].start) * S.res, (sl[1].stop - sl[1].start) * S.res
        if pad_xor[sl][m].all():
            cat = "pad shape"
        elif side == "Proteus" and clearance[sl][m].all():
            cat = "clearance"
        elif side == "Proteus" and (thin[sl][m] | pad_xor[sl][m] | clearance[sl][m]).all():
            cat = "zone fill"
        else:
            x0, y0 = canvas.mm(sl[1].start, sl[0].stop)
            x1, y1 = canvas.mm(sl[1].stop, sl[0].start)
            inside = all(in_area(layer, a, b) for a in (x0, x1) for b in (y0, y1))
            cat = "deviation" if inside else "other"
        pad, dp = nearest(ipc_pads, x, y)
        via, dv = nearest(ipc_vias, x, y)
        clusters.append(dict(cat=cat, side=side, area=area, x=x, y=y, w=wid, h=hgt,
                             pad=f"{pad_label(pad)} {dp:.2f} mm" if pad else "-",
                             via=f"via ({via['net']}) {dv:.2f} mm" if via else "-"))
    clusters.sort(key=lambda c: -c["area"])
    by_cat = Counter()
    area_cat = Counter()
    for c in clusters:
        if c["area"] >= min_area:
            by_cat[c["cat"]] += 1
            area_cat[c["cat"]] += c["area"]
    summ = dict(layer=layer, miss_P=unexpl_P, miss_K=unexpl_K, devmiss=devmiss,
                copper_P=int(P.sum()) * px_area, copper_K=int(K.sum()) * px_area,
                xor=int(xor.sum()) * px_area, onlyP=int(onlyP.sum()) * px_area, onlyK=int(onlyK.sum()) * px_area,
                clusters=len(clusters), small=sum(1 for c in clusters if c["area"] < min_area),
                by_cat=by_cat, area_cat=area_cat, other=[c for c in clusters if c["cat"] == "other" and c["area"] >= min_area])
    report.append(f"== {layer}: Proteus copper {summ['copper_P']:.1f} mm^2, KiCad copper {summ['copper_K']:.1f} mm^2")
    report.append(f"   raw XOR {summ['xor']:.2f} mm^2; after {tol} mm tolerance: only Proteus {summ['onlyP']:.2f} mm^2, "
                  f"only KiCad {summ['onlyK']:.2f} mm^2, {len(clusters)} clusters ({summ['small']} below {min_area} mm^2)")
    for cat in CATEGORIES:
        if by_cat[cat]:
            report.append(f"   {cat:10s} {by_cat[cat]:4d} clusters, {area_cat[cat]:.2f} mm^2")
    nd_p, nf_p = sum(isinstance(o, gerber.Draw) for o in gp.objects), sum(isinstance(o, gerber.Flash) for o in gp.objects)
    nd_k, nf_k = sum(isinstance(o, gerber.Draw) for o in gk.objects), sum(isinstance(o, gerber.Flash) for o in gk.objects)
    report.append(f"   coverage: Proteus {nd_p} draws / {nf_p} flashes, {len(unexpl_P)} not on KiCad copper "
                  f"({len(miss_P) - len(unexpl_P)} explained by clearance / pad shape); "
                  f"KiCad {nd_k} draws / {nf_k} flashes, {len(unexpl_K)} not on Proteus copper "
                  f"({len(miss_K) - len(unexpl_K)} explained by pad shape)")
    for who, miss in (("Proteus", miss_P), ("KiCad", miss_K)):
        for kind, x, y, wd, fn, at, bad, expl, n in miss:
            pad, dp = nearest(ipc_pads, x, y)
            tag = "MISSING  " if bad else "explained"
            report.append(f"   {tag} {who} {kind} {fn} {wd} mm at ({x:.3f}, {y:.3f}) {at or ''} "
                          f"{bad} samples off copper, {expl} in clearance/pad shape, of {n}; "
                          f"near {pad_label(pad)} {dp:.2f} mm")
    # pad shape clusters per footprint (nearest IPC pad -> refdes -> footprint)
    per_fp = {}
    for c in clusters:
        if c["cat"] == "pad shape" and c["area"] >= min_area:
            ref = c["pad"].split("-")[0].split(" ")[0]
            e = per_fp.setdefault(footprints.get(ref, ref), [0, 0.0, 0.0, set()])
            e[0] += 1
            e[1 if c["side"] == "KiCad" else 2] += c["area"]
            e[3].add(ref)
    for fp, (n, ak, ap_, refs) in sorted(per_fp.items(), key=lambda kv: -(kv[1][1] + kv[1][2])):
        report.append(f"   pad shape  {n:4d} clusters, only KiCad {ak:6.2f} mm^2, only Proteus {ap_:6.2f} mm^2  {fp}  "
                      f"({len(refs)} parts: {' '.join(sorted(refs)[:6])}{' ...' if len(refs) > 6 else ''})")
    order = {c: i for i, c in enumerate(("other", "deviation", "clearance", "zone fill", "pad shape"))}
    for c in sorted(clusters, key=lambda c: (order[c["cat"]], -c["area"])):
        if c["area"] < min_area:
            continue
        report.append(f"   {c['cat']:10s} only {c['side']:7s} {c['area']:6.3f} mm^2 at ({c['x']:8.3f}, {c['y']:8.3f}) "
                      f"{c['w']:.2f}x{c['h']:.2f} mm  near {c['pad']}, {c['via']}")
    return summ, (P, K, xor, onlyP, onlyK)


CATEGORIES = ("pad shape", "clearance", "zone fill", "deviation", "other")


def overlay(layer, arrays):
    P, K, xor, onlyP, onlyK = arrays
    f = int(round(S.overlay_res / S.res))
    h, w = P.shape
    H, W = -(-h // f), -(-w // f)

    def pool(m):
        pad = np.zeros((H * f, W * f), bool)
        pad[:h, :w] = m
        return pad.reshape(H, f, W, f).any(axis=(1, 3))

    common = pool(P & K)
    img = np.zeros((H, W, 3), np.uint8)
    img[common] = (90, 90, 90)
    img[pool(xor)] = (120, 110, 40)
    img[pool(onlyP)] = (255, 60, 60)
    img[pool(onlyK)] = (0, 200, 255)
    Image.fromarray(img).save(os.path.join(S.out, layer.replace(".", "_") + ".png"))


def flashes(path):
    """[(x, y, diameter, function)] of a drill gerber (an absent file = no drills)."""
    if not path or not os.path.exists(path):
        return []
    return [(f.x, f.y, f.ap.params[0], f.ap.function) for f in gerber.parse(path).of(gerber.Flash)]


def match_drills(P, K, what, report, extra_vias=()):
    """Proteus drill flashes P vs KiCad drill flashes K (lists of (x, y, d, fn)). Returns
    (errors, stacked, pairs Counter)."""
    tol, dtol = S.tol, S.drill_tol
    errors = []
    kxy = np.array([(x, y) for x, y, _, _ in K]) if K else np.zeros((0, 2))
    used = np.zeros(len(K), bool)
    pairs = Counter()
    smaller = Counter()
    stacked = 0
    for x, y, d, fn in P:
        if not len(K):
            errors.append(f"{what}: Proteus {fn} {d} mm at ({x}, {y}) has no KiCad drill at all")
            continue
        dist = np.hypot(kxy[:, 0] - x, kxy[:, 1] - y)
        free = dist.copy()
        free[used] = np.inf
        i = int(free.argmin())
        if free[i] > tol:
            # a second Proteus record on an already matched drill of the same diameter = stacked
            j = int(dist.argmin())
            same = abs(K[j][2] - d) <= dtol or bc.drill_smaller_accepted(d, K[j][2], dtol)
            if used[j] and dist[j] < 0.005 and same:
                stacked += 1
                continue
            errors.append(f"{what}: Proteus {fn} {d} mm at ({x}, {y}) has no KiCad drill within {tol} mm")
            continue
        used[i] = True
        dk = K[i][2]
        pairs[(d, dk)] += 1
        if dk < d - dtol:
            if bc.drill_smaller_accepted(d, dk, dtol):
                smaller[(d, dk)] += 1
            else:
                errors.append(f"{what}: drill at ({x}, {y}): Proteus {d} mm, KiCad {dk} mm is smaller")
    nextra = 0
    for i in np.nonzero(~used)[0]:
        x, y, d, fn = K[i]
        if any(math.hypot(vx - x, vy - y) < tol for vx, vy, *_ in extra_vias) \
                and any(abs(d - vd) <= dtol for vd in S.via_drills):
            nextra += 1
            continue
        errors.append(f"{what}: KiCad {fn} {d} mm at ({x}, {y}) has no Proteus drill")
    if extra_vias:
        if nextra != len(extra_vias):
            errors.append(f"{what}: {len(extra_vias)} added vias listed, {nextra} added via drills found")
        report.append(f"== {what} drills: {nextra} KiCad via drills of the listed added vias")
    report.append(f"== {what} drills: Proteus {len(P)}, KiCad {len(K)}, matched {int(used.sum())} within {tol} mm"
                  + (f", {stacked} stacked duplicate Proteus records on matched drills" if stacked else ""))
    for (d, dk), n in sorted(pairs.items()):
        if abs(d - dk) <= dtol:
            note = ""
        elif dk > d:
            note = "  KiCad footprint drill kept (larger)"
        else:
            why = bc.drill_smaller_accepted(d, dk, dtol)
            note = "  SMALLER" + (f", accepted: {why}" if why else "")
        report.append(f"   {n:4d} x Proteus {d:.4f} mm -> KiCad {dk:.4f} mm{note}")
    return errors, stacked, smaller


def parse_excellon(path):
    """[(x, y, diameter)] of a metric Excellon file (M48 header with TnnCd.ddd, body Tnn and
    X..Y.. in 4.3 with explicit sign, as Proteus writes it)."""
    tools, out, cur, header = {}, [], None, True
    for raw in open(path):
        line = raw.strip()
        if not line:
            continue
        if line in ("%", "M95"):
            header = False
            continue
        m = re.fullmatch(r"T(\d+)C([\d.]+)", line)
        if m and header:
            tools[int(m[1])] = float(m[2])
            continue
        m = re.fullmatch(r"T(\d+)", line)
        if m:
            cur = tools.get(int(m[1]))
            continue
        m = re.fullmatch(r"X([+-]?\d+)Y([+-]?\d+)", line)
        if m:
            if cur is None:
                raise ValueError(f"{path}: coordinate before a tool selection: {line}")
            out.append((int(m[1]) / 1000.0, int(m[2]) / 1000.0, cur))
    return out


def compare_excellon(gerber_drills, report):
    """The Excellon file of another Proteus export vs the drill gerber flashes, 1:1."""
    path = _board.path(S.excellon)
    E = parse_excellon(path)
    errors = []
    used = np.zeros(len(gerber_drills), bool)
    gxy = np.array([(x, y) for x, y, _, _ in gerber_drills])
    diam_pairs = Counter()
    for x, y, d in E:
        dist = np.hypot(gxy[:, 0] - x, gxy[:, 1] - y)
        dist[used] = np.inf
        i = int(dist.argmin())
        if dist[i] > S.tol:
            errors.append(f"excellon: {d} mm at ({x}, {y}) has no drill gerber flash within {S.tol} mm")
            continue
        used[i] = True
        dg = gerber_drills[i][2]
        diam_pairs[(d, dg)] += 1
        if abs(dg - d) > S.drill_tol:
            errors.append(f"excellon: {d} mm at ({x}, {y}): drill gerber {dg} mm")
    for i in np.nonzero(~used)[0]:
        x, y, d, fn = gerber_drills[i]
        errors.append(f"excellon: drill gerber {fn} {d} mm at ({x}, {y}) not in the Excellon file")
    report.append(f"== excellon ({os.path.basename(path)}): {len(E)} holes vs {len(gerber_drills)} drill gerber flashes, "
                  f"{int(used.sum())} matched within {S.tol} mm, {len(errors)} mismatches")
    for (d, dg), n in sorted(diam_pairs.items()):
        report.append(f"   {n:4d} x Excellon {d:.4f} mm = gerber {dg:.4f} mm" + ("" if abs(d - dg) <= S.drill_tol else "  DIFFERS"))
    return errors


def compare_drills(kdir, report, extra_vias):
    errors = []
    P_pl = flashes(bc.gerber_path(S.drill_plated))
    K_pl = flashes(os.path.join(kdir, f"{S.project}-PTH-drl.gbr"))
    e, stacked, smaller = match_drills(P_pl, K_pl, "plated", report, extra_vias)
    errors += e
    P_np = flashes(bc.gerber_path(S.drill_nonplated)) if S.drill_nonplated else []
    K_np = flashes(os.path.join(kdir, f"{S.project}-NPTH-drl.gbr"))
    if P_np or K_np:
        e, st2, sm2 = match_drills(P_np, K_np, "non-plated", report)
        errors += e
        stacked += st2
        smaller.update(sm2)
    if stacked != S.stacked_vias:
        errors.append(f"{stacked} stacked duplicate Proteus drill records, STACKED_VIAS = {S.stacked_vias}")
    if S.excellon:
        errors += compare_excellon(P_pl + P_np, report)
    for e in errors:
        report.append("   ERROR " + e)
    return errors, stacked, smaller


def main(argv=None, in_area=None, extra_vias=None):
    global S
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--kicad-dir")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)
    S = Settings()
    in_area = in_area or (lambda layer, x, y: False)
    extra_vias = list(extra_vias or [])
    os.makedirs(S.out, exist_ok=True)
    tmp = None
    if args.kicad_dir:
        kdir = args.kicad_dir
    elif args.keep:
        kdir = os.path.join(S.out, "kicad_gerbers")
        shutil.rmtree(kdir, ignore_errors=True)
        os.makedirs(kdir)
        export_kicad(kdir)
    else:
        tmp = tempfile.mkdtemp(prefix=f"{S.project}_gerbers_")
        kdir = tmp
        export_kicad(kdir)
    try:
        bbox = board_bbox()
        ipc_pads, ipc_vias = load_ipc()
        footprints = load_footprints()
        report = [f"Gerber round trip: KiCad plot of {bc.rel(bc.board_file())} vs the Proteus CADCAM gerbers",
                  f"raster {S.res} mm/px, tolerance {S.tol} mm, clusters >= {S.min_area} mm^2 counted, "
                  f"positions in the IPC frame (mm, Y up)",
                  f"board outline {bbox[0]} .. {bbox[2]} x {bbox[1]} .. {bbox[3]} mm", ""]
        summaries = []
        for pname, klayer in S.layers:
            gp = gerber.parse(bc.gerber_path(pname))
            gk = gerber.parse(kicad_layer_file(kdir, klayer))
            summ, arrays = compare_layer(klayer, gp, gk, bbox, ipc_pads[klayer], ipc_vias, report, footprints, in_area)
            overlay(klayer, arrays)
            summaries.append(summ)
            report.append("")
        drill_errors, stacked, smaller = compare_drills(kdir, report, extra_vias)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    rel_out = os.path.relpath(S.out, bc.root())
    with open(os.path.join(S.out, "roundtrip.txt"), "w") as f:
        f.write("\n".join(report) + "\n")
    other = sum(len(s["other"]) for s in summaries)
    missing = sum(len(s["miss_P"]) + len(s["miss_K"]) for s in summaries)
    for s in summaries:
        cats = ", ".join(f"{s['by_cat'][c]} {c}" for c in CATEGORIES if s["by_cat"][c])
        print(f"roundtrip: {s['layer']:6s} XOR {s['xor']:7.2f} mm^2, beyond {S.tol} mm: only Proteus {s['onlyP']:6.2f}, "
              f"only KiCad {s['onlyK']:6.2f} mm^2; clusters >= {S.min_area} mm^2: {cats or 'none'}; "
              f"draws/flashes off the other copper: {len(s['miss_P'])} Proteus, {len(s['miss_K'])} KiCad"
              + (f" (+{len(s['devmiss'])} inside deviation areas)" if s["devmiss"] else ""))
    for s in summaries:
        for c in s["other"]:
            print(f"roundtrip: {s['layer']} OTHER only {c['side']} {c['area']:.3f} mm^2 at ({c['x']:.3f}, {c['y']:.3f}) near {c['pad']}, {c['via']}")
    extra = ""
    if stacked:
        extra += f", {stacked} stacked duplicate Proteus records"
    if smaller:
        extra += "; KiCad drill smaller, accepted (DRILL_SMALLER): " + ", ".join(
            f"{n}x {a}->{b}" for (a, b), n in sorted(smaller.items()))
    print(f"roundtrip: drills {len(drill_errors)} mismatches{extra}; report {rel_out}/roundtrip.txt, overlays {rel_out}/*.png")
    for e in drill_errors[:10]:
        print("roundtrip: ERROR " + e)
    ok = other == 0 and missing == 0 and not drill_errors
    print("roundtrip: " + ("OK" if ok else f"FAILED ({other} unexplained clusters, {missing} draws/flashes without copper "
                                            f"in the other set, {len(drill_errors)} drill mismatches)"))
    sys.exit(0 if ok or not args.check else 1)


if __name__ == "__main__":
    main()
