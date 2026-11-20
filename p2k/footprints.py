"""Footprint assignment and the project footprint library (stdlib only).

The board's `tools/gen_footprints.py` holds what is decided per board - the assignment RULES
and the PROJECT footprint specs - and calls `run(RULES, PROJECT)`, which writes

  lib/footprint_map.csv       refdes -> footprint for every part of lib/part_map.csv
  lib/<PROJECT>.pretty/       the project footprints (derived from KiCad library footprints,
                              or built from the IPC-D-356A pad geometry where KiCad has nothing)

Usage in the board repo: python3 tools/gen_footprints.py [--check]

Assignment: every IPC refdes is classified by its symbol (lib/part_map.csv `lib_id` without
the library prefix) and its IPC pad pattern (pad count, largest pad-to-pad distance = "span",
pad size, drill). RULES = [(symbol, conditions, footprint, note), ...]; the first matching rule
wins and every refdes must match one. Conditions: n = pad count, tht = True/False,
span (mm, +-0.02), pad = (w, h) of the IPC pads (all pads this size, order-free), drill,
package / device = that column of lib/part_map.csv (when two Proteus packages have the same
pad pattern, e.g. film capacitors of different widths; a string matches exactly, a tuple / set
matches any of its values), refdes = a tuple/set of refdes (a rule
for named parts only, e.g. the Proteus pad sizes where the KiCad pads collide with Proteus
copper; put it before the general rule; every refdes named in such a condition must be
assigned by one of the rules naming it).

Pad numbers always equal the IPC pin ids. Policy: use the KiCad library footprint unchanged
when its pads land on the IPC pads (check_footprints: residual < 0.05 mm per pad); otherwise
derive a project footprint from it with the pad centres moved onto the Proteus positions, or
build one from the IPC pads when KiCad has no such package.

PROJECT = {name: spec}; spec keys
  base       KiCad library footprint to start from (graphics, 3D model, pad shapes)
  renumber   base pad number -> IPC pin id (several base pads may map to one id)
  drop       refdes: remove base pads whose (renumbered) number that part does not have
  ipc        refdes whose IPC pads give the pad centres (moved into the base frame); pads of
             the base without a number (paste segments) follow the nearest numbered pad
  ipc_sizes  also take pad size (and drill) from the IPC pads of that refdes
  body       (w, h) body outline for footprints built from the IPC pads only (centred on the pads)
  body_poly  [(x, y), ...] body outline polygon in board coordinates (IPC frame, mm) of the `ipc`
             refdes, e.g. measured from the Proteus silk screen (IPC-built footprints only): Fab =
             the polygon, silk = the polygon with gaps of SILK_PAD_GAP around the pads, courtyard =
             convex hull of body and pads + 0.25 mm
  rotate     degrees: build the IPC-only footprint in a frame rotated by this angle (a part
             placed at 45 deg on the board gets an axis-aligned footprint)
  descr      description text
  board_only True: a one-pad footprint for board-only items (mounting holes, free pads; used
             by p2k.build_pcb, not by a symbol): pad = (diameter, drill), plated (bool),
             number (pad number, "" for an unplated hole); not reported as unused;
             or smd_pad = (diameter, "F" | "B"): a round SMD pad 1 on that side's copper and mask
  bottom     True: the part is mounted on the bottom side: the IPC pads of the `ipc` refdes are
             taken as bottom-side pads, so a footprint built from them (or fitted by derive) is
             mirrored, and in its own view the pads run as seen from the component side
  max_move   derived footprints: largest allowed pad move (mm) from the base position onto the
             IPC position; derive fails beyond it and prints the mirror state and the largest move.
             With STRICT_CHECKS "derive_max_move" every derived spec with `ipc` gets the limit
             DERIVE_MAX_MOVE (config, default 0.5) x the smallest pad pitch of the base unless it
             sets max_move, and a base that fits only mirrored fails unless the spec sets bottom
  pads       derived footprints: {pad number or "*": dict(shape=, size=(w, h), rratio=, drill=,
             expect_shape=)} applied after the derivation (sizes in the footprint frame, the pad
             rotation is kept; roundrect_rratio / chamfer entries are removed before a shape is set,
             roundrect needs rratio; a circle needs a square size; expect_shape: the pad number must
             be exactly one pad of that shape in the derived footprint before the override); a
             number the footprint does not have is an error
  pin1_dot   derived footprints: (x, y, r) in the footprint frame (Y down): a filled circle on
             F.SilkS (0.12) and F.Fab (0.1) inserted directly before pad 1
  body_from  IPC-built footprints: dict(layer="Top Assembly", centre=(x, y), min_len=, radius=,
             expect=n): body_poly = the start points (rounded to 0.001) of the draws of the CADCAM
             gerber `layer` longer than min_len with both ends within radius of centre (board
             coordinates), sorted by angle around the centre; expect = required point count

p2k_config.py keys
  PROJECT              library nickname (lib/<PROJECT>.pretty, "<PROJECT>:<name>")
  FOOTPRINT_PAD_SIZES  "kicad" (default: KiCad pad sizes; Proteus sizes only where a spec says
                       ipc_sizes) or "proteus" (every footprint gets the Proteus pad sizes and
                       drills: KiCad library footprints are replaced by derived "<name>_Proteus"
                       project footprints, derived specs get ipc_sizes)
  OFF_BOARD_PARTS      optional {refdes: note} for parts of the part map that have no IPC pads
                       (not fitted); they get no footprint
  FP_LIB_TABLE         optional: True = run also writes / --checks kicad/fp-lib-table (one entry:
                       nickname PROJECT, ${KIPRJMOD}/../lib/<PROJECT>.pretty); a string = that
                       entry's descr instead of the default text
  DERIVE_MAX_MOVE      optional factor for the strict derive_max_move limit (default 0.5)
  PROTEUS_PADS         optional {refdes}: these parts get the Proteus pad sizes: the footprint their
                       rule assigns is replaced by "<name>_ProteusPads" (KiCad library footprint ->
                       derived with ipc_sizes; derived project footprint -> a copy with ipc_sizes;
                       one per (footprint, note), `ipc` = its first part), note + " (Proteus pad sizes)"
  PROTEUS_PAD_OVERRIDES optional {"<name>_ProteusPads": pads}: the `pads` key of such a spec

`added` (optional argument of run / assign): {refdes: {"footprint": ..., "note": ...}} for
parts the board added after the Proteus design (no IPC pads); they are assigned as listed.
"""
import copy
import csv
import io
import math
import os
import sys

from . import board, fpgeom, sexpr
from .sexpr import Sym

FILE_VERSION = 20260206


def project():
    return board.config().PROJECT


def pretty_dir():
    return board.path("lib", f"{project()}.pretty")


def map_csv_path():
    return board.path("lib", "footprint_map.csv")


def kicad_footprint_dir():
    state = f"{os.environ.get('TMPDIR', '/tmp')}/p2k_kicad_mount.{os.getuid()}"
    mount = os.environ.get("KICAD_MOUNT") or (open(state).read().strip() if os.path.exists(state) else "")
    d = os.path.join(mount, "share", "kicad", "footprints")
    if not mount or not os.path.isdir(d):
        sys.exit("footprints: KiCad AppImage not mounted - run `source tools/p2k/bin/kicad_env.sh` "
                 "(or any tools/p2k/bin wrapper) first")
    return d


def footprint_path(fpid):
    """File of a footprint id: the project library or the KiCad AppImage libraries."""
    nick, _, name = fpid.partition(":")
    if nick == project():
        return os.path.join(pretty_dir(), name + ".kicad_mod")
    return os.path.join(kicad_footprint_dir(), nick + ".pretty", name + ".kicad_mod")


def part_map():
    with open(board.path("lib", "part_map.csv")) as f:
        return {r["refdes"]: r for r in csv.DictReader(l for l in f if not l.startswith("# "))}


def features(pads):
    span = max((math.hypot(a["x_mm"] - b["x_mm"], a["y_mm"] - b["y_mm"])
                for a in pads for b in pads), default=0.0)
    return {"n": len(pads), "tht": pads[0]["is_tht"], "span": span,
            "pads": {tuple(sorted((p["pad_w"], p["pad_h"]))) for p in pads},
            "drills": {p["drill"] for p in pads}}


def match(cond, f, part):
    for key, want in cond.items():
        if key == "refdes":
            if isinstance(want, str):
                raise SystemExit(f"footprints: rule condition refdes must be a tuple/set of refdes, not {want!r}")
            if part["refdes"] not in want:
                return False
            continue
        if key in ("package", "device"):
            if key not in part:
                raise SystemExit(f"footprints: rule condition {key!r} needs that column in lib/part_map.csv")
            if (part[key] != want) if isinstance(want, str) else (part[key] not in want):
                return False
            continue
        if key == "n" and f["n"] != want:
            return False
        if key == "tht" and f["tht"] != want:
            return False
        if key == "span" and abs(f["span"] - want) > 0.02:
            return False
        if key == "pad" and f["pads"] != {tuple(sorted(want))}:
            return False
        if key == "drill" and f["drills"] != {want}:
            return False
        if key not in ("n", "tht", "span", "pad", "drill"):
            raise SystemExit(f"footprints: unknown rule condition {key!r}")
    return True


def assign(rules, added=None):
    """refdes -> (footprint, note) for every IPC part (and the `added` parts)."""
    pm = part_map()
    ipc = fpgeom.ipc_pads_by_refdes()
    off_board = getattr(board.config(), "OFF_BOARD_PARTS", {})
    out, errors = {}, []
    used_rule = {}
    no_rule = set()
    for ref in sorted(ipc):
        if ref not in pm:
            errors.append(f"{ref}: in the IPC file but not in lib/part_map.csv")
            continue
        sym = pm[ref]["lib_id"].split(":", 1)[1]
        f = features(ipc[ref])
        hits = [rule for rule in rules if rule[0] == sym and match(rule[1], f, pm[ref])]
        if not hits:
            errors.append(f"{ref}: no footprint rule for {sym} (n={f['n']} tht={f['tht']} span={f['span']:.2f} "
                          f"pads={sorted(f['pads'])} drills={sorted(f['drills'])})")
            no_rule.add(ref)
            continue
        out[ref] = (hits[0][2], hits[0][3])
        used_rule.setdefault(id(hits[0]), set()).add(ref)
    # every refdes named in a `refdes` condition must be assigned by one of those rules
    named = set().union(*(set(r[1].get("refdes", ())) for r in rules))
    taken = set().union(*(used_rule.get(id(r), set()) for r in rules if "refdes" in r[1]))
    if named - taken:
        errors.append("refdes listed in a rule condition but assigned by no such rule: " + " ".join(sorted(named - taken)))
    for ref, p in (added or {}).items():
        if ref not in pm:
            errors.append(f"{ref}: added part missing from lib/part_map.csv")
        out[ref] = (p["footprint"], p.get("note", "added part"))
    for ref in sorted(pm):
        if ref not in out and ref not in off_board and ref not in no_rule:   # no_rule: reported above
            errors.append(f"{ref}: in lib/part_map.csv but has no IPC pads (list it in OFF_BOARD_PARTS "
                          "or pass it as an added part)")
    return out, errors


# ---------------------------------------------------------------- footprint building

def _side_mirror(pads):
    """Mirror flags to try when fitting a base footprint onto a refdes: bottom SMD parts are
    mirrored; THT parts (IPC side 'both') are taken as top side unless only the mirrored fit works."""
    side = pads[0]["side"]
    if side == "bottom":
        return [True]
    if side == "top":
        return [False]
    return [False, True]


def _to_fp(fitres, qx, qy):
    """Board (IPC, Y up) point -> footprint frame (Y up) for a fit result."""
    a = math.radians(fitres["angle"])
    dx, dy = qx - fitres["tx"], qy - fitres["ty"]
    x = math.cos(a) * dx + math.sin(a) * dy
    y = -math.sin(a) * dx + math.cos(a) * dy
    return (-x if fitres["mirror"] else x), y


def _fp_rot(fitres, qrot):
    r = qrot - fitres["angle"]
    return (-r if fitres["mirror"] else r) % 180.0


def _r4(v):
    v = round(v, 4)
    return 0 if v == 0 else v


def derive(name, spec, ipc):
    base = sexpr.load(footprint_path(spec["base"]))
    base[1] = name
    ren = spec.get("renumber", {})
    for pad in sexpr.find_all(base, "pad"):
        pad[1] = ren.get(str(pad[1]), str(pad[1]))
    keep_ref = spec.get("drop")
    if keep_ref:
        pins = {p["pin"] for p in ipc[keep_ref]}
        base[:] = [c for c in base if not (isinstance(c, list) and c and c[0] == "pad" and str(c[1]) not in pins)]
    if spec.get("ipc"):
        ref = spec["ipc"]
        pads = ipc[ref]
        fp_pads, unnumbered = [], []
        nodes = {}
        for pad in sexpr.find_all(base, "pad"):
            if str(pad[2]) == "np_thru_hole":
                continue
            at, size = sexpr.find(pad, "at"), sexpr.find(pad, "size")
            d = {"num": str(pad[1]), "kind": str(pad[2]), "x": float(at[1]), "y": -float(at[2]),
                 "rot": float(at[3]) if len(at) > 3 else 0.0, "w": float(size[1]), "h": float(size[2]), "drill": 0.0}
            nodes[id(d)] = pad
            (unnumbered if d["num"] == "" else fp_pads).append(d)
        fits = [fpgeom.fit(fp_pads, pads, m) for m in _side_mirror(pads)]
        fr = min(fits, key=lambda r: r["max_res"])
        if fr["missing"] or fr["extra"] or fr["count_diff"]:
            raise SystemExit(f"{name}: pad numbers of {spec['base']} do not match {ref}: "
                             f"missing {fr['missing']}, extra {fr['extra']}, count differs {fr['count_diff']}")
        strict_move = board.strict("derive_max_move")
        limit = spec.get("max_move")
        if limit is None and strict_move:
            centres = sorted({(round(f["x"], 6), round(f["y"], 6)) for f in fp_pads})
            pitch = min((math.hypot(a[0] - b[0], a[1] - b[1]) for i, a in enumerate(centres)
                         for b in centres[i + 1:]), default=0.0)
            limit = getattr(board.config(), "DERIVE_MAX_MOVE", 0.5) * pitch if pitch else None
        moved = {}
        for num, f, q, _ in fr["pads"]:
            pad = nodes[id(f)]
            x, y = _to_fp(fr, q["x_mm"], q["y_mm"])
            moved[id(f)] = (x - f["x"], y - f["y"])
            at = sexpr.find(pad, "at")
            rot = float(at[3]) if len(at) > 3 else 0.0
            if spec.get("ipc_sizes"):
                rot = _fp_rot(fr, q["rot"])
                size = sexpr.find(pad, "size")
                size[1:] = [q["pad_w"], q["pad_h"]]
                if q["is_tht"]:
                    drill = sexpr.find(pad, "drill")
                    drill[1:] = [q["drill"]]
            at[1:] = [_r4(x), _r4(-y)] + ([_r4(rot)] if abs(rot) > 1e-9 else [])
        if limit is not None or strict_move:
            worst = max(((math.hypot(*moved[id(f)]), num) for num, f, _, _ in fr["pads"]), default=(0.0, ""))
            mirrored_only = fr["mirror"] and not spec.get("bottom") and pads[0]["side"] != "bottom"
            print(f"footprints: {name}: fit on {ref} {'mirrored' if fr['mirror'] else 'unmirrored'}, "
                  f"largest pad move {worst[0]:.3f} mm (pad {worst[1]})"
                  + (f", limit {limit:.3f} mm" if limit is not None else ""))
            if limit is not None and worst[0] > limit + 1e-9:
                raise SystemExit(f"{name}: pad {worst[1]} moves {worst[0]:.3f} mm onto {ref}, more than "
                                 f"{limit:.3f} mm (max_move; wrong renumbering or base?)")
            if mirrored_only:
                msg = (f"{name}: {spec['base']} fits {ref} only mirrored (a bottom-side part? set "
                       "bottom=True, or fix the renumbering)")
                if strict_move:
                    raise SystemExit(msg)
                print("footprints: note: " + msg)
        # pads without a number (paste segments of a big pad) follow the nearest numbered pad
        for u in unnumbered:
            near = min(fp_pads, key=lambda f: math.hypot(f["x"] - u["x"], f["y"] - u["y"]))
            dx, dy = moved[id(near)]
            at = sexpr.find(nodes[id(u)], "at")
            at[1:3] = [_r4(u["x"] + dx), _r4(-(u["y"] + dy))]
    if spec.get("ipc_sizes"):
        # Proteus land pattern -> the board's clearance rules, not a library override
        # (e.g. KiCad's TSSOP-8 carries a footprint clearance of 0.2 mm)
        base[:] = [c for c in base if not (isinstance(c, list) and c and c[0] == "clearance")]
    for prop in sexpr.find_all(base, "property"):
        if prop[1] == "Value":
            prop[2] = name
    descr = sexpr.find(base, "descr")
    text = f"{spec['descr']} ({project()}, derived from {spec['base']})"
    if descr is None:
        base.insert(4, [Sym("descr"), text])
    else:
        descr[1] = text
    gen = sexpr.find(base, "generator")
    if gen is not None:
        gen[1] = f"{project()}_gen_footprints"
    return base


def _eff(size=1.0, thick=0.15):
    return [Sym("effects"), [Sym("font"), [Sym("size"), size, size], [Sym("thickness"), thick]]]


def _rect(x0, y0, x1, y1, layer, width):
    return [Sym("fp_rect"), [Sym("start"), _r4(x0), _r4(y0)], [Sym("end"), _r4(x1), _r4(y1)],
            [Sym("stroke"), [Sym("width"), width], [Sym("type"), Sym("solid")]], [Sym("fill"), Sym("no")],
            [Sym("layer"), "F." + layer]]


SILK_PAD_GAP = 0.2   # mm, silk kept off the pads (circumscribed pad circle + this)


def _poly(pts, layer, width):
    """Closed polygon outline; pts in the footprint frame, Y up."""
    return [Sym("fp_poly"), [Sym("pts"), *[[Sym("xy"), _r4(x), _r4(-y)] for x, y in pts]],
            [Sym("stroke"), [Sym("width"), width], [Sym("type"), Sym("solid")]], [Sym("fill"), Sym("no")],
            [Sym("layer"), "F." + layer]]


def _line(a, b, layer, width):
    return [Sym("fp_line"), [Sym("start"), _r4(a[0]), _r4(-a[1])], [Sym("end"), _r4(b[0]), _r4(-b[1])],
            [Sym("stroke"), [Sym("width"), width], [Sym("type"), Sym("solid")]], [Sym("layer"), "F." + layer]]


def _hull(points):
    """Convex hull (counter-clockwise, Y up) of a point list (monotone chain)."""
    pts = sorted(set((round(x, 6), round(y, 6)) for x, y in points))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, hi = [], []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 1e-9:
            lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(hi) >= 2 and cross(hi[-2], hi[-1], p) <= 1e-9:
            hi.pop()
        hi.append(p)
    return lo[:-1] + hi[:-1]


def _grown_hull(points, d):
    """Convex hull of the points grown by at least d (each point replaced by an octagon around
    a circle of radius d; corners come out bevelled)."""
    r = d / math.cos(math.pi / 8)
    grown = [(x + r * math.cos(k * math.pi / 4 + math.pi / 8), y + r * math.sin(k * math.pi / 4 + math.pi / 8))
             for x, y in points for k in range(8)]
    return _hull(grown)


def _clip(a, b, keepout, min_len=0.1):
    """Parts of segment a-b outside every circle (cx, cy, r) of keepout."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    cut = []
    for cx, cy, r in keepout:
        fx, fy = a[0] - cx, a[1] - cy
        bq = 2 * (fx * dx + fy * dy)
        cq = fx * fx + fy * fy - r * r
        disc = bq * bq - 4 * L2 * cq
        if L2 == 0 or disc <= 0:
            continue
        t0, t1 = (-bq - math.sqrt(disc)) / (2 * L2), (-bq + math.sqrt(disc)) / (2 * L2)
        if t1 > 0 and t0 < 1:
            cut.append((max(t0, 0.0), min(t1, 1.0)))
    keep, t = [], 0.0
    for c0, c1 in sorted(cut):
        if c0 > t:
            keep.append((t, c0))
        t = max(t, c1)
    if t < 1:
        keep.append((t, 1.0))
    L = math.sqrt(L2)
    return [((a[0] + dx * u, a[1] + dy * u), (a[0] + dx * v, a[1] + dy * v)) for u, v in keep if (v - u) * L >= min_len]


def _circle(r, layer, width):
    return [Sym("fp_circle"), [Sym("center"), 0, 0], [Sym("end"), _r4(r), 0],
            [Sym("stroke"), [Sym("width"), width], [Sym("type"), Sym("solid")]], [Sym("fill"), Sym("no")],
            [Sym("layer"), "F." + layer]]


def from_ipc(name, spec, ipc):
    pads = ipc[spec["ipc"]]
    mirror = pads[0]["side"] == "bottom"
    m = -1.0 if mirror else 1.0
    rot0 = float(spec.get("rotate", 0.0))
    ca, sa = math.cos(math.radians(-rot0)), math.sin(math.radians(-rot0))
    xs = [m * (ca * p["x_mm"] - sa * p["y_mm"]) for p in pads]
    ys = [sa * p["x_mm"] + ca * p["y_mm"] for p in pads]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    tht = pads[0]["is_tht"]
    # pad extents (axis-aligned bounding box of each pad, Y up, centred)
    ext = []
    for p, x, y in zip(pads, xs, ys):
        a = math.radians(p["rot"] - rot0)
        hw = abs(math.cos(a)) * p["pad_w"] / 2 + abs(math.sin(a)) * p["pad_h"] / 2
        hh = abs(math.sin(a)) * p["pad_w"] / 2 + abs(math.cos(a)) * p["pad_h"] / 2
        ext.append((x - cx - hw, y - cy - hh, x - cx + hw, y - cy + hh))
    bx0, by0 = min(e[0] for e in ext), min(e[1] for e in ext)
    bx1, by1 = max(e[2] for e in ext), max(e[3] for e in ext)
    # body = spec["body"] or the pad extents; courtyard 0.25 mm around body and pads (KLC),
    # a circle for single round pins (PIN terminals) so neighbours are not blocked diagonally
    body = None
    if spec.get("body_poly"):
        body = [(m * (ca * x - sa * y) - cx, sa * x + ca * y - cy) for x, y in spec["body_poly"]]
        fx0, fy0 = min(x for x, _ in body), min(y for _, y in body)
        fx1, fy1 = max(x for x, _ in body), max(y for _, y in body)
    elif spec.get("body"):
        w, h = spec["body"]
        fx0, fy0, fx1, fy1 = -w / 2, -h / 2, w / 2, h / 2
    else:
        fx0, fy0, fx1, fy1 = bx0, by0, bx1, by1
    cx0, cy0 = min(fx0, bx0) - 0.25, min(fy0, by0) - 0.25
    cx1, cy1 = max(fx1, bx1) + 0.25, max(fy1, by1) + 0.25
    sx0, sy0, sx1, sy1 = fx0 - 0.12, fy0 - 0.12, fx1 + 0.12, fy1 + 0.12
    round_pin = len(pads) == 1 and abs(pads[0]["pad_w"] - pads[0]["pad_h"]) < 1e-6 and not (spec.get("body") or body)
    if body:
        keepout = [(x - cx, y - cy, math.hypot(p["pad_w"], p["pad_h"]) / 2 if not tht or p["pad_w"] != p["pad_h"]
                    else p["pad_w"] / 2) for p, x, y in zip(pads, xs, ys)]
        keepout = [(x, y, r + SILK_PAD_GAP) for x, y, r in keepout]
        silk = [_line(a, b, "SilkS", 0.12) for i in range(len(body))
                for a, b in _clip(body[i], body[(i + 1) % len(body)], keepout)]
        corners = list(body)
        for p, x, y in zip(pads, xs, ys):   # pad outline points (round pads: 16-gon around the circle)
            if tht and abs(p["pad_w"] - p["pad_h"]) < 1e-6:
                r = p["pad_w"] / 2 / math.cos(math.pi / 16)
                corners += [(x - cx + r * math.cos(k * math.pi / 8), y - cy + r * math.sin(k * math.pi / 8)) for k in range(16)]
            else:
                a = math.radians(p["rot"] - rot0)
                for u, v in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
                    du, dv = u * p["pad_w"] / 2, v * p["pad_h"] / 2
                    corners.append((x - cx + du * math.cos(a) - dv * math.sin(a), y - cy + du * math.sin(a) + dv * math.cos(a)))
        graphics = [_poly(body, "Fab", 0.1), *silk, _poly(_grown_hull(corners, 0.25), "CrtYd", 0.05)]
    elif round_pin:
        graphics = [_circle(fx1, "Fab", 0.1), _circle(sx1, "SilkS", 0.12), _circle(cx1, "CrtYd", 0.05)]
    else:
        graphics = [_rect(fx0, -fy1, fx1, -fy0, "Fab", 0.1), _rect(sx0, -sy1, sx1, -sy0, "SilkS", 0.12),
                    _rect(cx0, -cy1, cx1, -cy0, "CrtYd", 0.05)]
    # file coordinates are Y down
    node = [Sym("footprint"), name, [Sym("version"), FILE_VERSION], [Sym("generator"), f"{project()}_gen_footprints"],
            [Sym("layer"), "F.Cu"], [Sym("descr"), f"{spec['descr']} ({project()}, pads from IPC-D-356A {spec['ipc']})"],
            [Sym("property"), "Reference", "REF**", [Sym("at"), 0, _r4(-sy1 - 1.2), 0], [Sym("layer"), "F.SilkS"], _eff()],
            [Sym("property"), "Value", name, [Sym("at"), 0, _r4(-sy0 + 1.2), 0], [Sym("layer"), "F.Fab"], _eff()],
            [Sym("attr"), Sym("through_hole" if tht else "smd")],
            *graphics,
            [Sym("fp_text"), Sym("user"), "${REFERENCE}", [Sym("at"), 0, 0, 0], [Sym("layer"), "F.Fab"], _eff()]]
    for p, x, y in zip(pads, xs, ys):
        px, py = _r4(x - cx), _r4(-(y - cy))
        rot = (-(p["rot"] - rot0) if mirror else (p["rot"] - rot0)) % 180.0
        at = [Sym("at"), px, py] + ([_r4(rot)] if rot else [])
        if tht:
            shape = "circle" if abs(p["pad_w"] - p["pad_h"]) < 1e-6 else "oval"
            node.append([Sym("pad"), p["pin"], Sym("thru_hole"), Sym(shape), at,
                         [Sym("size"), p["pad_w"], p["pad_h"]], [Sym("drill"), p["drill"]],
                         [Sym("layers"), "*.Cu", "*.Mask"]])
        else:
            node.append([Sym("pad"), p["pin"], Sym("smd"), Sym("rect"), at,
                         [Sym("size"), p["pad_w"], p["pad_h"]], [Sym("layers"), "F.Cu", "F.Paste", "F.Mask"]])
    node.append([Sym("embedded_fonts"), Sym("no")])
    return node


def single_pad(name, spec):
    """Board-only one-pad footprint (mounting hole / free pad), round pad on every copper layer."""
    dia, drill = spec["pad"]
    plated = spec.get("plated", True)
    r = dia / 2
    node = [Sym("footprint"), name, [Sym("version"), FILE_VERSION], [Sym("generator"), f"{project()}_gen_footprints"],
            [Sym("layer"), "F.Cu"], [Sym("descr"), f"{spec['descr']} ({project()}, board-only)"],
            [Sym("property"), "Reference", "REF**", [Sym("at"), 0, _r4(-r - 1.2), 0], [Sym("layer"), "F.SilkS"], _eff()],
            [Sym("property"), "Value", name, [Sym("at"), 0, _r4(r + 1.2), 0], [Sym("layer"), "F.Fab"], _eff()],
            [Sym("attr"), *([Sym("through_hole")] if plated else []), Sym("board_only"),
             Sym("exclude_from_pos_files"), Sym("exclude_from_bom")],
            _circle(r, "Fab", 0.1), _circle(r + 0.25, "CrtYd", 0.05),
            [Sym("pad"), spec.get("number", "1" if plated else ""), Sym("thru_hole" if plated else "np_thru_hole"),
             Sym("circle"), [Sym("at"), 0, 0], [Sym("size"), dia, dia], [Sym("drill"), drill],
             [Sym("layers"), "*.Cu", "*.Mask"]],
            [Sym("embedded_fonts"), Sym("no")]]
    return node


def smd_single_pad(name, spec):
    """Board-only one-pad SMD footprint (spec smd_pad = (diameter, "F" | "B"))."""
    dia, side = spec["smd_pad"]
    if side not in ("F", "B"):
        raise SystemExit(f"{name}: smd_pad side must be 'F' or 'B', not {side!r}")
    r = dia / 2

    def lay(layer):
        return f"{side}.{layer}"
    return [Sym("footprint"), name, [Sym("version"), FILE_VERSION], [Sym("generator"), f"{project()}_gen_footprints"],
            [Sym("layer"), lay("Cu")], [Sym("descr"), f"{spec['descr']} ({project()}, board-only)"],
            [Sym("property"), "Reference", "REF**", [Sym("at"), 0, _r4(-r - 1.0), 0],
             [Sym("layer"), lay("SilkS")], _eff(0.8, 0.12)],
            [Sym("property"), "Value", name, [Sym("at"), 0, _r4(r + 1.0), 0], [Sym("layer"), lay("Fab")], _eff(0.8, 0.12)],
            [Sym("attr"), Sym("smd"), Sym("board_only"), Sym("exclude_from_pos_files"), Sym("exclude_from_bom")],
            _circle(r + 0.25, "CrtYd", 0.05),
            [Sym("pad"), "1", Sym("smd"), Sym("circle"), [Sym("at"), 0, 0], [Sym("size"), dia, dia],
             [Sym("layers"), lay("Cu"), lay("Mask")]],
            [Sym("embedded_fonts"), Sym("no")]]


def pad_options(name, spec, node):
    """Spec keys `pads` and `pin1_dot` on a derived footprint node (in place)."""
    over = spec.get("pads")
    if over:
        for num, o in over.items():
            if "expect_shape" in o:
                hits = [p for p in sexpr.find_all(node, "pad") if num == "*" or str(p[1]) == num]
                if not hits or any(str(p[3]) != o["expect_shape"] for p in hits) or (num != "*" and len(hits) != 1):
                    raise SystemExit(f"{name}: expected one {o['expect_shape']} pad {num} in the base, got "
                                     f"{[str(p[3]) for p in hits]}")
        seen = set()
        for pad in sexpr.find_all(node, "pad"):
            o = over.get(str(pad[1]), over.get("*"))
            if o is None:
                continue
            seen.add(str(pad[1]) if str(pad[1]) in over else "*")
            if "shape" in o:
                pad[3] = Sym(o["shape"])
            if "size" in o:
                sexpr.find(pad, "size")[1:] = list(o["size"])
            if "drill" in o:
                sexpr.find(pad, "drill")[1:] = [o["drill"]]
            pad[:] = [c for c in pad if not (isinstance(c, list) and c and c[0] in ("roundrect_rratio", "chamfer_ratio", "chamfer"))]
            if o.get("shape") == "roundrect":
                if "rratio" not in o:
                    raise SystemExit(f"{name}: pads {pad[1]}: roundrect needs rratio")
                pad.append([Sym("roundrect_rratio"), o["rratio"]])
            size = sexpr.find(pad, "size")
            if str(pad[3]) == "circle" and "shape" in o and float(size[1]) != float(size[2]):
                raise SystemExit(f"{name} pad {pad[1]}: circle needs a square size, not {size[1:]}")
        missing = set(over) - seen
        if missing:
            raise SystemExit(f"{name}: `pads` names pads the footprint does not have: {sorted(missing)}")
    if spec.get("pin1_dot"):
        x, y, r = spec["pin1_dot"]
        pad1 = [p for p in sexpr.find_all(node, "pad") if str(p[1]) == "1"]
        if not pad1:
            raise SystemExit(f"{name}: pin1_dot but no pad 1")
        i = node.index(pad1[0])
        dots = [_dot(x, y, r, "F.SilkS", 0.12), _dot(x, y, r, "F.Fab", 0.1)]
        if node[i - 2:i] != dots:      # not twice (a board wrapper may already have added it)
            node[i:i] = dots
    return node


def _dot(x, y, r, layer, width):
    return [Sym("fp_circle"), [Sym("center"), x, y], [Sym("end"), round(x + r, 4), y],
            [Sym("stroke"), [Sym("width"), width], [Sym("type"), Sym("solid")]], [Sym("fill"), Sym("yes")],
            [Sym("layer"), layer]]


def body_from(name, bf):
    """Spec key body_from -> body_poly (board coordinates)."""
    import glob
    from . import gerber
    layer = bf.get("layer", "Top Assembly")
    paths = glob.glob(os.path.join(board.path(board.config().CADCAM), f"*{layer}.GBR"))
    if len(paths) != 1:
        raise SystemExit(f"{name}: body_from: gerber '*{layer}.GBR' not found once in CADCAM ({len(paths)} hits)")
    cx, cy = bf["centre"]
    pts = []
    for d in gerber.parse(paths[0]).of(gerber.Draw):
        if math.hypot(d.x2 - d.x1, d.y2 - d.y1) > bf["min_len"] and \
                all(math.hypot(x - cx, y - cy) < bf["radius"] for x, y in ((d.x1, d.y1), (d.x2, d.y2))):
            pts.append((round(d.x1, 3), round(d.y1, 3)))
    if "expect" in bf and len(pts) != bf["expect"]:
        raise SystemExit(f"{name}: body_from: {len(pts)} draws found, expected {bf['expect']}: {pts}")
    if len(pts) < 3:
        raise SystemExit(f"{name}: body_from: only {len(pts)} draws found: {pts}")
    return sorted(pts, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))


def build_library(project_fps, ipc=None):
    ipc = ipc or fpgeom.ipc_pads_by_refdes()
    if any(spec.get("bottom") for spec in project_fps.values()):
        ipc = copy.deepcopy(ipc)
        for spec in project_fps.values():
            if spec.get("bottom"):
                for p in ipc[spec["ipc"]]:
                    p["side"] = "bottom"
    out = {}
    for name, spec in project_fps.items():
        if spec.get("board_only"):
            node = smd_single_pad(name, spec) if spec.get("smd_pad") else single_pad(name, spec)
        elif spec.get("base"):
            node = pad_options(name, spec, derive(name, spec, ipc))
        else:
            if spec.get("body_from"):
                if spec.get("body_poly"):
                    raise SystemExit(f"{name}: body_from and body_poly are exclusive")
                spec = dict(spec, body_poly=body_from(name, spec["body_from"]))
            node = from_ipc(name, spec, ipc)
        out[name + ".kicad_mod"] = sexpr.dump(node) + "\n"
    return out


def _refdes_key(r):
    stem = r.rstrip("0123456789")
    return (stem, int(r[len(stem):] or 0), r)


def map_csv(assignment):
    pm = part_map()
    off_board = getattr(board.config(), "OFF_BOARD_PARTS", {})
    buf = io.StringIO()
    buf.write("# refdes -> footprint, generated by tools/gen_footprints.py (do not edit)\n")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["refdes", "lib_id", "footprint", "note"])
    for ref in sorted(pm, key=_refdes_key):
        fp, note = assignment.get(ref, ("", off_board.get(ref, "")))
        w.writerow([ref, pm[ref]["lib_id"], fp, note])
    return buf.getvalue()


# ---------------------------------------------------------------- pad-size mode

def proteus_sizes(assignment, project_fps):
    """FOOTPRINT_PAD_SIZES = "proteus": every footprint gets the Proteus pad sizes. KiCad
    library footprints become derived project footprints "<name>_Proteus" (one per rule, i.e.
    per pad pattern), derived specs get ipc_sizes. Returns (assignment, project_fps)."""
    proj = project()
    out = dict(assignment)
    fps = copy.deepcopy(project_fps)
    groups = {}
    for ref in sorted(assignment, key=_refdes_key):
        fp, note = assignment[ref]
        if fp.startswith(proj + ":") or not fp:
            continue
        groups.setdefault((fp, note), []).append(ref)
    names = {}
    for (fp, note), refs in sorted(groups.items()):
        stem = fp.split(":", 1)[1] + "_Proteus"
        name, k = stem, 1
        while name in fps:
            k += 1
            name = f"{stem}{k}"
        fps[name] = dict(base=fp, ipc=refs[0], ipc_sizes=True, descr=f"{note}; Proteus pad sizes and positions")
        names[(fp, note)] = name
        for ref in refs:
            out[ref] = (f"{proj}:{name}", note + " (Proteus pad sizes)")
    for name, spec in fps.items():
        if spec.get("base") and not spec.get("ipc_sizes"):
            spec["ipc_sizes"] = True
            if not spec.get("ipc"):
                users = [r for r in sorted(out, key=_refdes_key) if out[r][0] == f"{proj}:{name}"]
                if users:
                    spec["ipc"] = users[0]
            spec["descr"] = spec["descr"] + "; Proteus pad sizes"
    return out, fps


def proteus_pads(assignment, project_fps, refs, overrides=None):
    """PROTEUS_PADS: the parts `refs` get "<footprint>_ProteusPads" (Proteus pad sizes).
    Returns (assignment, project_fps)."""
    proj = project()
    overrides = dict(overrides or {})
    out, fps = dict(assignment), copy.deepcopy(project_fps)
    unknown = sorted(set(refs) - set(assignment))
    if unknown:
        raise SystemExit(f"footprints: PROTEUS_PADS names parts without a footprint: {' '.join(unknown)}")
    groups = {}
    for ref in sorted(refs, key=_refdes_key):
        groups.setdefault(assignment[ref], []).append(ref)
    for (fp, note), grp in sorted(groups.items()):
        nick, _, stem = fp.partition(":")
        name = stem + "_ProteusPads"
        if nick == proj:
            src = project_fps.get(stem)
            if src is None or not src.get("base"):
                raise SystemExit(f"footprints: PROTEUS_PADS {' '.join(grp)}: {fp} is not a derived project "
                                 "footprint (an IPC-built footprint already has the Proteus pads)")
            spec = dict(copy.deepcopy(src), ipc=grp[0], ipc_sizes=True, descr=src["descr"] + "; Proteus pad sizes")
        else:
            spec = dict(base=fp, ipc=grp[0], ipc_sizes=True, descr=f"{note}; Proteus pad sizes")
        if name in overrides:
            spec["pads"] = overrides.pop(name)
        if name in fps and fps[name] != spec:
            raise SystemExit(f"footprints: PROTEUS_PADS: {name} is already a different project footprint")
        fps[name] = spec
        for ref in grp:
            out[ref] = (f"{proj}:{name}", note + " (Proteus pad sizes)")
    if overrides:
        raise SystemExit(f"footprints: PROTEUS_PAD_OVERRIDES for footprints PROTEUS_PADS does not make: {sorted(overrides)}")
    return out, fps


FP_LIB_TABLE_DESCR = "Project footprints, pad numbers = IPC pin ids (tools/gen_footprints.py)"


def fp_lib_table_text(value):
    descr = value if isinstance(value, str) else FP_LIB_TABLE_DESCR
    proj = project()
    return ("(fp_lib_table\n\t(version 7)\n"
            f'\t(lib (name "{proj}")(type "KiCad")(uri "${{KIPRJMOD}}/../lib/{proj}.pretty")(options "")(descr "{descr}"))\n'
            ")\n")


# ---------------------------------------------------------------- generator main

def run(rules, project_fps, argv=None, added=None):
    """Main of the board's tools/gen_footprints.py: write (or with --check verify) the map and
    the library. Exits non-zero on assignment errors or (--check) stale files."""
    argv = sys.argv[1:] if argv is None else argv
    check = "--check" in argv
    proj = project()
    assignment, errors = assign(rules, added)
    mode = getattr(board.config(), "FOOTPRINT_PAD_SIZES", "kicad")
    if mode == "proteus":
        assignment, project_fps = proteus_sizes(assignment, project_fps)
    elif mode != "kicad":
        raise SystemExit(f"footprints: FOOTPRINT_PAD_SIZES must be 'kicad' or 'proteus', not {mode!r}")
    pp = getattr(board.config(), "PROTEUS_PADS", None)
    if pp:
        if mode == "proteus":
            raise SystemExit("footprints: PROTEUS_PADS with FOOTPRINT_PAD_SIZES = 'proteus' (every part has them)")
        assignment, project_fps = proteus_pads(assignment, project_fps, pp,
                                               getattr(board.config(), "PROTEUS_PAD_OVERRIDES", None))
    used = {fp.split(":", 1)[1] for fp, _ in assignment.values() if fp.startswith(proj + ":")}
    unused = sorted(set(project_fps) - used - {n for n, sp in project_fps.items() if sp.get("board_only")})
    undefined = sorted(used - set(project_fps))
    for n in unused:
        errors.append(f"project footprint {n} is not used")
    for n in undefined:
        errors.append(f"project footprint {n} is not defined")
    for n in undefined:
        project_fps = {k: v for k, v in project_fps.items() if k != n}
    for fp in sorted({fp for fp, _ in assignment.values() if fp and not fp.startswith(proj + ":")}):
        if not os.path.exists(footprint_path(fp)):
            errors.append(f"KiCad library footprint {fp} not found")
    files = build_library(project_fps)
    files_map = map_csv(assignment)
    pretty = pretty_dir()
    map_path = map_csv_path()
    stale = []
    want = {os.path.join(pretty, k): v for k, v in files.items()}
    want[map_path] = files_map
    fplt = getattr(board.config(), "FP_LIB_TABLE", False)
    if fplt:
        want[board.path("kicad", "fp-lib-table")] = fp_lib_table_text(fplt)
    have = set()
    if os.path.isdir(pretty):
        have = {os.path.join(pretty, f) for f in os.listdir(pretty) if f.endswith(".kicad_mod")}
    for path, text in want.items():
        cur = open(path).read() if os.path.exists(path) else None
        if cur != text:
            stale.append(path)
            if not check:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write(text)
    extra = sorted(have - set(want))
    for path in extra:
        stale.append(path)
        if not check:
            os.remove(path)
    rel = [os.path.relpath(p, board.root()) for p in stale]
    for e in errors:
        print("gen_footprints: " + e)
    nfp = len({fp for fp, _ in assignment.values()})
    if check:
        if rel:
            print("gen_footprints: out of date: " + ", ".join(rel))
        print(f"gen_footprints: {len(assignment)} parts, {nfp} footprints ({len(files)} project)"
              + ("" if rel else ", up to date"))
        sys.exit(1 if rel or errors else 0)
    print(f"gen_footprints: {len(assignment)} parts, {nfp} footprints ({len(files)} project); "
          f"wrote {len(rel)} file(s)")
    sys.exit(1 if errors else 0)
