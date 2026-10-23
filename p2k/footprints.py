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
pad pattern, e.g. film capacitors of different widths).

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
  body       (w, h) body outline for footprints built from the IPC pads only
  rotate     degrees: build the IPC-only footprint in a frame rotated by this angle (a part
             placed at 45 deg on the board gets an axis-aligned footprint)
  descr      description text
  board_only True: a one-pad footprint for board-only items (mounting holes, free pads; used
             by p2k.build_pcb, not by a symbol): pad = (diameter, drill), plated (bool),
             number (pad number, "" for an unplated hole); not reported as unused

p2k_config.py keys
  PROJECT              library nickname (lib/<PROJECT>.pretty, "<PROJECT>:<name>")
  FOOTPRINT_PAD_SIZES  "kicad" (default: KiCad pad sizes; Proteus sizes only where a spec says
                       ipc_sizes) or "proteus" (every footprint gets the Proteus pad sizes and
                       drills: KiCad library footprints are replaced by derived "<name>_Proteus"
                       project footprints, derived specs get ipc_sizes)
  OFF_BOARD_PARTS      optional {refdes: note} for parts of the part map that have no IPC pads
                       (not fitted); they get no footprint

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
        if key in ("package", "device"):
            if key not in part:
                raise SystemExit(f"footprints: rule condition {key!r} needs that column in lib/part_map.csv")
            if part[key] != want:
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
    for ref in sorted(ipc):
        if ref not in pm:
            errors.append(f"{ref}: in the IPC file but not in lib/part_map.csv")
            continue
        sym = pm[ref]["lib_id"].split(":", 1)[1]
        f = features(ipc[ref])
        hits = [(fp, note) for s, cond, fp, note in rules if s == sym and match(cond, f, pm[ref])]
        if not hits:
            errors.append(f"{ref}: no footprint rule for {sym} (n={f['n']} tht={f['tht']} span={f['span']:.2f} "
                          f"pads={sorted(f['pads'])} drills={sorted(f['drills'])})")
            continue
        out[ref] = hits[0]
    for ref, p in (added or {}).items():
        if ref not in pm:
            errors.append(f"{ref}: added part missing from lib/part_map.csv")
        out[ref] = (p["footprint"], p.get("note", "added part"))
    for ref in sorted(pm):
        if ref not in out and ref not in off_board:
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
    if spec.get("body"):
        w, h = spec["body"]
        fx0, fy0, fx1, fy1 = -w / 2, -h / 2, w / 2, h / 2
    else:
        fx0, fy0, fx1, fy1 = bx0, by0, bx1, by1
    cx0, cy0 = min(fx0, bx0) - 0.25, min(fy0, by0) - 0.25
    cx1, cy1 = max(fx1, bx1) + 0.25, max(fy1, by1) + 0.25
    sx0, sy0, sx1, sy1 = fx0 - 0.12, fy0 - 0.12, fx1 + 0.12, fy1 + 0.12
    round_pin = len(pads) == 1 and abs(pads[0]["pad_w"] - pads[0]["pad_h"]) < 1e-6 and not spec.get("body")
    # file coordinates are Y down
    node = [Sym("footprint"), name, [Sym("version"), FILE_VERSION], [Sym("generator"), f"{project()}_gen_footprints"],
            [Sym("layer"), "F.Cu"], [Sym("descr"), f"{spec['descr']} ({project()}, pads from IPC-D-356A {spec['ipc']})"],
            [Sym("property"), "Reference", "REF**", [Sym("at"), 0, _r4(-sy1 - 1.2), 0], [Sym("layer"), "F.SilkS"], _eff()],
            [Sym("property"), "Value", name, [Sym("at"), 0, _r4(-sy0 + 1.2), 0], [Sym("layer"), "F.Fab"], _eff()],
            [Sym("attr"), Sym("through_hole" if tht else "smd")],
            *([_circle(fx1, "Fab", 0.1), _circle(sx1, "SilkS", 0.12), _circle(cx1, "CrtYd", 0.05)] if round_pin else
              [_rect(fx0, -fy1, fx1, -fy0, "Fab", 0.1), _rect(sx0, -sy1, sx1, -sy0, "SilkS", 0.12),
               _rect(cx0, -cy1, cx1, -cy0, "CrtYd", 0.05)]),
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


def build_library(project_fps, ipc=None):
    ipc = ipc or fpgeom.ipc_pads_by_refdes()
    out = {}
    for name, spec in project_fps.items():
        if spec.get("board_only"):
            node = single_pad(name, spec)
        else:
            node = derive(name, spec, ipc) if spec.get("base") else from_ipc(name, spec, ipc)
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
