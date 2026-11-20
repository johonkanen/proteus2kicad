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
STACKED_VIAS (expected stacked duplicate via records, default 0), DRILL_EXCELLON (path);
the recorded copper deviations NONCONDUCTOR_LAYERS, REMOVED_TRACKS, REMOVED_VIAS (p2k.pcb): those
Proteus objects (NonConductor draws moved off copper, removed tracks, removed via pads and via
drills) are dropped from the Proteus side before the comparison and counted in the report.
Optional keys of batch 8 (each without effect when absent): DRILL_SMALLER (reason, pads) form = the
smaller drills of that pair must be exactly those pads; EXCELLON_MISSING = {(x, y, drill): reason}
drill gerber flashes expected to be missing from DRILL_EXCELLON (each entry exactly one, 0.01 mm;
EXCELLON_MISSING_LABELS = dict(file=, items=, source=) words of its report lines); PENDING =
{"gerber_roundtrip": {pattern: question}} failures waiting for a user decision ("other: <layer>
...", "missing: <layer> ...", "drill: <error>", "pad match: ...", "counts: ..."); STRICT_CHECKS
"roundtrip_pad_match" (every pad of one set overlaps a pad of the other, per overlapping group the
pad areas within ROUNDTRIP pad_area_tol and the centroids within tol) and "roundtrip_counts" (per
layer the KiCad Conductor draws = the Proteus draws gerber_to_tracks builds, KiCad flashes = Proteus
flashes after the recorded deviations). BGAPad (BGA balls) counts as a pad function.
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
    pad_area_tol=0.25,  # relative pad copper area difference per pad group (strict roundtrip_pad_match)
)
# Pad flashes of either plotter: Proteus ComponentPad / SMDPad,CuDef / OtherPad,Unknown; KiCad
# ComponentPad / SMDPad,CuDef / WasherPad (copper ring of an unplated hole).
PAD_FUNCTIONS = ("ComponentPad", "SMDPad", "OtherPad", "WasherPad")
# Proteus BGAPad,CuDef (BGA balls) is a pad too (B3). Kept out of PAD_FUNCTIONS so a board wrapper from
# before batch 8 that adds it there itself (and asserts it is missing) keeps working.
PAD_FUNCTIONS_EXTRA = ("BGAPad",)


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
        self.pad_area_tol = rt["pad_area_tol"]
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
        fn = (o.ap.function or "").split(",")[0]
        return fn in PAD_FUNCTIONS or fn in PAD_FUNCTIONS_EXTRA
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
    recs = bc.ipc_records(merge=False)   # the Proteus nets as written (Phase 4; SPLIT_PIN_MERGES: batch 8)
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


def apply_recorded(klayer, g):
    """Drop from a Proteus copper gerber what the board deliberately does not build (recorded
    deviations, p2k.pcb): NonConductor draws moved off this copper layer (NONCONDUCTOR_LAYERS),
    REMOVED_TRACKS draws and REMOVED_VIAS via pads. Returns Counter of what was dropped."""
    target = bc.nonconductor_layer(klayer)
    keep, n = [], Counter()
    for o in g.objects:
        if isinstance(o, gerber.Draw):
            if o.ap.function == "NonConductor" and target != klayer:
                n[f"NonConductor draws (moved to {target}, NONCONDUCTOR_LAYERS)"] += 1
                continue
            if o.ap.function == "Conductor" and bc.removed_track(klayer, o.x1, o.y1, o.x2, o.y2):
                n["Conductor draws (REMOVED_TRACKS)"] += 1
                continue
        elif isinstance(o, gerber.Flash) and o.ap.function == "ViaPad" and bc.removed_via(o.x, o.y):
            n["via pads (REMOVED_VIAS)"] += 1
            continue
        keep.append(o)
    g.objects = keep
    return n


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
    strict_pads = pad_match(layer, Ppads, Kpads, canvas, ipc_pads) if _board.strict("roundtrip_pad_match") else None
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
                by_cat=by_cat, area_cat=area_cat, other=[c for c in clusters if c["cat"] == "other" and c["area"] >= min_area],
                pad_match=strict_pads)
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


def pad_match(layer, Ppads, Kpads, canvas, ipc_pads):
    """Strict roundtrip_pad_match (R5): the pad flashes of both sets as connected raster groups
    (Proteus and KiCad pad components joined when they overlap). Every group must have pad copper of
    both sets, its two pad areas within pad_area_tol (relative to the larger) and its two centroids
    within tol. A missing, extra, resized or shifted pad otherwise ends in the "pad shape" mask (the
    pad XOR), which explains it away. -> (errors, number of groups)"""
    lp, n_p = ndimage.label(Ppads, structure=np.ones((3, 3)))
    lk, n_k = ndimage.label(Kpads, structure=np.ones((3, 3)))
    parent = list(range(n_p + n_k + 1))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    both = (lp > 0) & (lk > 0)
    for a, b in np.unique(np.stack([lp[both], lk[both]], axis=1), axis=0) if both.any() else []:
        ra, rb = find(int(a)), find(n_p + int(b))
        if ra != rb:
            parent[ra] = rb
    area_p = np.bincount(lp.ravel(), minlength=n_p + 1)
    area_k = np.bincount(lk.ravel(), minlength=n_k + 1)
    rows = np.arange(lp.shape[0])[:, None]
    cols = np.arange(lp.shape[1])[None, :]

    def sums(lab, n):
        r = np.bincount(lab.ravel(), weights=np.broadcast_to(rows, lab.shape).ravel(), minlength=n + 1)
        c = np.bincount(lab.ravel(), weights=np.broadcast_to(cols, lab.shape).ravel(), minlength=n + 1)
        return r, c

    rp, cp = sums(lp, n_p)
    rk, ck = sums(lk, n_k)
    groups = {}
    for i in range(1, n_p + 1):
        g = groups.setdefault(find(i), [0, 0.0, 0.0, 0, 0.0, 0.0])
        g[0] += area_p[i]; g[1] += rp[i]; g[2] += cp[i]
    for j in range(1, n_k + 1):
        g = groups.setdefault(find(n_p + j), [0, 0.0, 0.0, 0, 0.0, 0.0])
        g[3] += area_k[j]; g[4] += rk[j]; g[5] += ck[j]
    px = S.res * S.res
    errors = []
    for g in groups.values():
        ap_, ak = g[0], g[3]
        r, c = (g[1] + g[4]) / (ap_ + ak), (g[2] + g[5]) / (ap_ + ak)
        x, y = canvas.mm(c + 0.5, r + 0.5)
        pad, dp = nearest(ipc_pads, x, y)
        near = f"near {pad_label(pad)} {dp:.2f} mm" if pad else "no IPC pad near"
        at = f"{layer} pad at ({x:.3f}, {y:.3f}) {near}"
        if not ak or not ap_:
            errors.append(f"pad match: {at}: only {'Proteus' if ap_ else 'KiCad'} pad copper "
                          f"{(ap_ or ak) * px:.3f} mm^2, no pad of the other set")
            continue
        if abs(ap_ - ak) > S.pad_area_tol * max(ap_, ak):
            errors.append(f"pad match: {at}: pad area Proteus {ap_ * px:.3f} mm^2, KiCad {ak * px:.3f} mm^2 "
                          f"(more than {S.pad_area_tol:.0%} apart)")
        xp, yp = canvas.mm(g[2] / ap_ + 0.5, g[1] / ap_ + 0.5)
        xk, yk = canvas.mm(g[5] / ak + 0.5, g[4] / ak + 0.5)
        if math.hypot(xp - xk, yp - yk) > S.tol:
            errors.append(f"pad match: {at}: pad centroid Proteus ({xp:.3f}, {yp:.3f}), KiCad ({xk:.3f}, {yk:.3f}), "
                          f"{math.hypot(xp - xk, yp - yk):.3f} mm apart")
    return errors, len(groups)


def flash_count(g):
    """Flashes of a gerber, identical flashes (same aperture, position within 0.001 mm) once: Proteus
    writes a stacked via record twice, KiCad plots one via."""
    return len({(o.ap.dcode, round(o.x, 3), round(o.y, 3))
                for o in g.objects if isinstance(o, gerber.Flash)})


def count_check(pname, klayer, user, gp, gk):
    """Strict roundtrip_counts (R7): KiCad Conductor draws = the Proteus draws gerber_to_tracks
    builds as tracks (pour edge strokes and REMOVED_TRACKS left out), KiCad flashes = Proteus flashes
    (after the recorded deviations). The area XOR cannot see a short track that lies inside two pads
    (a dog-bone stub between a BGA ball and its via). -> (errors, (tracks, kicad draws, P flashes, K flashes))"""
    from . import gerber_to_tracks as g2t
    want = len(g2t.load_layer(pname, klayer, user).tracks)
    kd = sum(1 for o in gk.objects if isinstance(o, gerber.Draw) and (o.ap.function or "").startswith("Conductor"))
    pf, kf = flash_count(gp), flash_count(gk)
    errors = []
    if kd != want:
        errors.append(f"counts: {klayer}: {kd} KiCad track draws, gerber_to_tracks builds {want} from the Proteus draws")
    if kf != pf:
        errors.append(f"counts: {klayer}: {kf} KiCad flashes, Proteus {pf}")
    return errors, (want, kd, pf, kf)


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
                rec, dr = nearest(_DRILLED, x, y)
                pad = ((rec["refdes"], rec["pin"]) if rec["kind"] == "pad" and rec["refdes"]
                       else ("", f"{rec['kind']} at ({rec['x_mm']}, {rec['y_mm']})")) if rec and dr <= tol \
                    else ("", f"drill at ({x}, {y})")
                _SMALLER_SEEN.setdefault(bc.drill_smaller_entry(d, dk, dtol), set()).add(pad)
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
    """[(x, y, diameter mm)] of an Excellon file. Header: `METRIC` or `INCH` (optionally `,TZ` /
    `,LZ`), `;FILE_FORMAT=i:d`, tools TnnC<diameter in the file unit>; body Tnn and X..Y.. with
    an explicit or implied sign. Coordinates without a decimal point have d implied decimals (default
    3 metric, 4 inch, as Proteus writes them: metric 4.3 / inch 2.4); with `LZ` (leading zeros kept)
    a shorter number is padded on the right to i + d digits. No unit line = metric (as before)."""
    tools, out, cur, header = {}, [], None, True
    scale, idig, ddig, lz = 1.0, None, None, False
    for raw in open(path):
        line = raw.strip()
        if not line:
            continue
        if line in ("%", "M95"):
            header = False
            continue
        if header:
            m = re.fullmatch(r"(METRIC|INCH)(?:,(TZ|LZ))?(?:,.*)?", line)
            if m:
                scale = 25.4 if m[1] == "INCH" else 1.0
                lz = m[2] == "LZ"
                continue
            m = re.search(r"FILE_FORMAT=(\d+):(\d+)", line)
            if m:
                idig, ddig = int(m[1]), int(m[2])
                continue
        m = re.fullmatch(r"T(\d+)C([\d.]+)", line)
        if m and header:
            tools[int(m[1])] = float(m[2]) * scale
            continue
        m = re.fullmatch(r"T(\d+)", line)
        if m:
            cur = tools.get(int(m[1]))
            continue
        m = re.fullmatch(r"X([+-]?[\d.]+)Y([+-]?[\d.]+)", line)
        if m:
            if cur is None:
                raise ValueError(f"{path}: coordinate before a tool selection: {line}")
            dec = ddig if ddig is not None else (4 if scale != 1.0 else 3)
            ndig = (idig if idig is not None else (2 if scale != 1.0 else 3)) + dec

            def num(t):
                if "." in t:
                    return float(t) * scale
                sign = -1 if t.startswith("-") else 1
                digits = t.lstrip("+-")
                if lz:
                    digits = digits.ljust(ndig, "0")
                v = sign * int(digits) / 10 ** dec
                return v * scale if scale != 1.0 else v

            out.append((num(m[1]), num(m[2]), cur))
    return out


def compare_excellon(gerber_drills, report):
    """The Excellon file of another Proteus export vs the drill gerber flashes, 1:1.
    EXCELLON_MISSING ({(x, y, drill): reason}, optional): drill gerber flashes known to be missing
    from that file (e.g. vias added after the export); each entry must match exactly one "not in the
    Excellon file" mismatch (0.01 mm, drill_tol), which is then reported as expected instead."""
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
    missing = []
    for i in np.nonzero(~used)[0]:
        x, y, d, fn = gerber_drills[i]
        missing.append((x, y, d, fn, len(errors)))
        errors.append(f"excellon: drill gerber {fn} {d} mm at ({x}, {y}) not in the Excellon file")
    report.append(f"== excellon ({os.path.basename(path)}): {len(E)} holes vs {len(gerber_drills)} drill gerber flashes, "
                  f"{int(used.sum())} matched within {S.tol} mm, {len(errors)} mismatches")
    for (d, dg), n in sorted(diam_pairs.items()):
        report.append(f"   {n:4d} x Excellon {d:.4f} mm = gerber {dg:.4f} mm" + ("" if abs(d - dg) <= S.drill_tol else "  DIFFERS"))
    expected = getattr(bc.cfg(), "EXCELLON_MISSING", None)
    if expected is None:
        return errors
    words = dict(file="Excellon file", items="expected drills", source="EXCELLON_MISSING")
    words.update(getattr(bc.cfg(), "EXCELLON_MISSING_LABELS", {}) or {})
    unknown = set(words) - {"file", "items", "source"}
    if unknown:
        raise SystemExit(f"roundtrip: EXCELLON_MISSING_LABELS: unknown key(s) {sorted(unknown)} (file, items, source)")
    drop, found, rest = set(), {}, []
    for key in expected:
        ex, ey, ed = key
        hit = [m for m in missing if m[4] not in drop and abs(m[2] - ed) <= S.drill_tol
               and math.hypot(m[0] - ex, m[1] - ey) < 0.01]
        if hit:
            drop.add(hit[0][4])
            found[key] = hit[0][3]
    rest = [e for i, e in enumerate(errors) if i not in drop]
    for key, why in expected.items():
        x, y, d = key
        if key in found:
            report.append(f"   expected, not an error: drill gerber {found[key]} {d} mm at ({x}, {y}) not in the "
                          f"{words['file']}: {why}")
        else:
            rest.append(f"excellon: EXCELLON_MISSING {d} mm at ({x}, {y}) was expected to be missing from the Excellon "
                        "file, but it is not reported missing")
    report.append(f"   {len(found)} of {len(expected)} {words['items']} missing from the Excellon file "
                  f"({words['source']}), {len(rest)} other mismatches")
    return rest


_DRILLED = []        # IPC records with a drill (DRILL_SMALLER pad sets, R3), set by compare_drills
_SMALLER_SEEN = {}   # DRILL_SMALLER key -> {(refdes, pin)} accepted smaller drills


def compare_drills(kdir, report, extra_vias):
    global _DRILLED, _SMALLER_SEEN
    recs = bc.ipc_records(merge=False)
    _DRILLED = [p for p in recs if p["drill"]]
    _SMALLER_SEEN = {}
    errors = []
    P_raw = flashes(bc.gerber_path(S.drill_plated))
    P_pl = [d for d in P_raw if not (d[3] == "ViaDrill" and bc.removed_via(d[0], d[1]))]
    if len(P_pl) != len(P_raw):
        report.append(f"== plated drills: {len(P_raw) - len(P_pl)} Proteus via drills not compared (REMOVED_VIAS)")
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
    errors += bc.drill_smaller_pad_errors(_SMALLER_SEEN, recs, S.drill_tol)
    if S.excellon:
        errors += compare_excellon(P_raw + P_np, report)
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
        recorded = {}
        strict_counts = _board.strict("roundtrip_counts")
        users = {k: u for _, k, u in bc.copper()}
        count_errors, count_seen = [], {}
        for pname, klayer in S.layers:
            gp = gerber.parse(bc.gerber_path(pname))
            dropped = apply_recorded(klayer, gp)
            if dropped:
                report.append(f"== {klayer}: Proteus objects not compared (recorded deviations): "
                              + ", ".join(f"{v} {k}" for k, v in sorted(dropped.items())))
                recorded[klayer] = dropped
            gk = gerber.parse(kicad_layer_file(kdir, klayer))
            if strict_counts:
                e, count_seen[klayer] = count_check(pname, klayer, users[klayer], gp, gk)
                count_errors += e
            summ, arrays = compare_layer(klayer, gp, gk, bbox, ipc_pads[klayer], ipc_vias, report, footprints, in_area)
            overlay(klayer, arrays)
            summaries.append(summ)
            report.append("")
        drill_errors, stacked, smaller = compare_drills(kdir, report, extra_vias)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    rel_out = os.path.relpath(S.out, bc.root())
    pad_errors = [e for s in summaries for e in (s["pad_match"][0] if s["pad_match"] else [])]
    strict_errors = pad_errors + count_errors
    if strict_errors:   # failures only: a passing strict check adds nothing to the committed report
        report.append("== strict checks (STRICT_CHECKS)")
        report += ["   ERROR " + e for e in strict_errors]
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
    if recorded:
        tot = Counter()
        for c in recorded.values():
            tot.update(c)
        print("roundtrip: Proteus objects not compared (recorded deviations, all layers): "
              + ", ".join(f"{v} {k}" for k, v in sorted(tot.items())))
    print(f"roundtrip: drills {len(drill_errors)} mismatches{extra}; report {rel_out}/roundtrip.txt, overlays {rel_out}/*.png")
    for e in drill_errors:
        print("roundtrip: ERROR " + e)
    if _board.strict("roundtrip_pad_match"):
        n = sum(s["pad_match"][1] for s in summaries)
        print(f"roundtrip: pad match (roundtrip_pad_match): {n} pad groups on {len(summaries)} layers, "
              + ("each with both sets' pads, areas within "
                 f"{S.pad_area_tol:.0%}, centroids within {S.tol} mm" if not pad_errors else f"{len(pad_errors)} errors"))
    if strict_counts:
        print("roundtrip: counts (roundtrip_counts): KiCad track draws / flashes per layer "
              + ", ".join(f"{k} {v[1]} / {v[3]}" for k, v in count_seen.items())
              + (": tracks as gerber_to_tracks builds them, flashes = Proteus" if not count_errors else ": FAILED"))
    for e in strict_errors:
        print("roundtrip: ERROR " + e)
    failures = ([f"other: {s['layer']} only {c['side']} {c['area']:.3f} mm^2 at ({c['x']:.3f}, {c['y']:.3f}) near {c['pad']}"
                 for s in summaries for c in s["other"]]
                + [f"missing: {s['layer']} {who} {m[0]} {m[4]} {m[3]} mm at ({m[1]:.3f}, {m[2]:.3f})"
                   for s in summaries for who, key in (("Proteus", "miss_P"), ("KiCad", "miss_K")) for m in s[key]]
                + ["drill: " + e for e in drill_errors] + strict_errors)
    if _board.pending("gerber_roundtrip"):
        rest, questions, stale = _board.judge_pending("gerber_roundtrip", failures)
        for e in stale:
            print("roundtrip: ERROR " + e)
        for r in rest:
            print("roundtrip: not pending: " + r[:300])
        ok = not rest and not stale
        if ok:
            for q in questions:
                print("roundtrip: PENDING USER DECISION: " + q)
        print("roundtrip: " + ("OK" if ok else f"FAILED ({len(rest)} failures not pending, {len(stale)} stale PENDING entries)"))
        return 0 if ok or not args.check else 1
    ok = other == 0 and missing == 0 and not drill_errors and not strict_errors
    print("roundtrip: " + ("OK" if ok else f"FAILED ({other} unexplained clusters, {missing} draws/flashes without copper "
                                            f"in the other set, {len(drill_errors)} drill mismatches"
                                            + (f", {len(strict_errors)} strict check errors" if strict_errors else "") + ")"))
    return 0 if ok or not args.check else 1


if __name__ == "__main__":
    sys.exit(main())
