#!/usr/bin/env python3
"""Pin-order check (playbook Phase 2, step 2): UNMOVED KiCad base footprints, with the Proteus
renumbering, fitted onto the IPC pads (stdlib only; --render needs matplotlib).

`p2k.footprints.derive` moves every pad onto the IPC position of its number, so a wrong
renumbering in a PROJECT spec still passes check_footprints. This tool fits the base as it is
(both mirror states, rigid transform, p2k.fpgeom) and compares the result with what the board
recorded: a small residual on the expected side confirms that the Proteus pin ids sit on the pads
the renumbering says; a residual of a pad pitch means they sit on other pads (chirality).
Two-pad and inline patterns fit both ways round: for those `polarity` and the rendered body
markers (--render) decide.

Usage: tools/p2k/bin/p2k check_pin_order [--render]
       (the KiCad libraries must be mounted: any tools/p2k/bin wrapper, e.g. kipy, does that)
Exit 1 when a result differs from the table.

p2k_config.py keys
  PIN_ORDER  [entry, ...] (required): one dict per check. Base pads (exactly one of):
      footprint="Lib:Name"   that footprint file as it is (a library footprint, or a project
                             footprint "<PROJECT>:<name>" of lib/<PROJECT>.pretty), with
                             renumber={base pad: pin id} (default {})
      spec="name"            the base and renumber of PROJECT[name] (the footprint script)
      rule="SYMBOL"          the footprint of the single RULES rule of that symbol: a project
                             footprint as its spec's base + renumber, a library footprint as it is
      ideal=dict(n=, pcd=, step=, start=0, pad=1.6, drill=0.8)
                             an ideal circular socket: pads "1".."n" on the pitch circle pcd (mm),
                             pad k at start + (k - 1) * step degrees (counter-clockwise, Y up)
    symbol_renumber=dict(symbol=, pattern=, lib=None)
                             renumber = {last group of `pattern` matched on the pin name: pin
                             number} for every pin of that symbol in lib/<PROJECT>.kicad_sym (or
                             `lib`, relative to the board root); every pin name must match
    Target (one of):
      refs="U1 U2"           the IPC pads of each refdes (the worst refdes counts per mirror state)
      on_ideal=dict(...)     the pads of an ideal socket (keys as `ideal`), e.g. to prove that a
                             project socket footprint has the standard numbering in its own view
    Expectation:
      mirror=False | True    the expected side (mirrored = bottom); on a tie (|unmirrored -
                             mirrored| < 0.001 mm, symmetric patterns) the expected side is taken
             | None          "does not fit on top": the unmirrored residual must exceed `nofit`
                             (default 0.1 mm)
      res=mm                 residual upper bound on the expected side
      other=mm               optional: the other side's residual must exceed this ("fits only ...")
      angle=deg              optional: the fitted angle on the expected side (+-0.01 deg)
      free_angle=True        least-squares angle without the 45 deg snap (e.g. 36 deg noval steps)
      polarity="+"           the pin id the renumbering puts on base pad "1" (the marked pad)
    Checks without base pads (with refs=):
      turn="clockwise" | "counter-clockwise"
                             the sense of the numeric pins 1 -> n around their centre, seen from
                             the top (IPC frame, Y up)
      distances={"1-2": mm, ...}, tol=0.01
                             the distance between two pins of each refdes
    Other keys: name= (default: refs), note= (printed), render=False (skip in --render).
  PIN_ORDER_BOTTOM     optional {refdes}: reports/footprint_fit.csv has exactly these parts on the
                       bottom side and the board file exactly these footprints on B.Cu
  PIN_ORDER_MAX_MOVE   optional {spec name: mm}: every PROJECT spec with a base must be listed; the
                       pads of its footprint in lib/<PROJECT>.pretty may sit at most that far from
                       the renumbered base pads (footprint frame), and the pad numbers must equal
  FOOTPRINT_SCRIPT     the board script that defines RULES and PROJECT (default
                       tools/gen_footprints.py); imported (without writing bytecode) only when an
                       entry uses spec= / rule= or PIN_ORDER_MAX_MOVE is set

--render writes reports/pin_order/<refdes>.png per refdes of the entries with a refs target and an
expected side: the Proteus silk screen and assembly layer of that side (black), the IPC pads with
their pin ids (orange) and the base footprint's F.SilkS / F.Fab graphics and pads at the fit (red).
"""
import csv
import glob
import importlib.util
import math
import os
import re
import sys

from . import board, fpgeom, pcb, sexpr
from .footprints import footprint_path

sys.dont_write_bytecode = True   # the footprint script is imported: no stale tools/__pycache__
TIE = 1e-3
_SCRIPT = None


def script():
    """The board's footprint script module (RULES, PROJECT)."""
    global _SCRIPT
    if _SCRIPT is None:
        rel = getattr(board.config(), "FOOTPRINT_SCRIPT", os.path.join("tools", "gen_footprints.py"))
        path = board.path(rel)
        if not os.path.isfile(path):
            raise SystemExit(f"check_pin_order: FOOTPRINT_SCRIPT {rel} not found")
        sys.path.insert(0, os.path.dirname(path))
        spec = importlib.util.spec_from_file_location(os.path.splitext(os.path.basename(path))[0], path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _SCRIPT = mod
    return _SCRIPT


def base_pads(fpid, ren):
    """Pads of a footprint file (unnumbered pads dropped), numbers renumbered."""
    out = []
    for p in fpgeom.load_footprint(footprint_path(fpid)):
        if p["num"] == "":
            continue
        q = dict(p)
        q["num"] = ren.get(p["num"], p["num"])
        out.append(q)
    return out


def ideal_pads(d):
    r = d["pcd"] / 2
    pad, drill = d.get("pad", 1.6), d.get("drill", 0.8)
    return [{"num": str(k + 1), "x": r * math.cos(math.radians(d.get("start", 0.0) + d["step"] * k)),
             "y": r * math.sin(math.radians(d.get("start", 0.0) + d["step"] * k)), "w": pad, "h": pad,
             "rot": 0.0, "drill": drill, "kind": "thru_hole", "shape": "circle"} for k in range(d["n"])]


def as_ipc(pads):
    return [dict(p, pin=p["num"], x_mm=p["x"], y_mm=p["y"], pad_w=p["w"], pad_h=p["h"]) for p in pads]


def spec_base(name):
    proj = script().PROJECT
    if name not in proj or not proj[name].get("base"):
        raise SystemExit(f"check_pin_order: PROJECT has no spec {name!r} with a base")
    return proj[name]["base"], dict(proj[name].get("renumber", {}))


def rule_base(symbol):
    fps = [r[2] for r in script().RULES if r[0] == symbol]
    if len(fps) != 1:
        return None, None, f"rule {symbol!r}: {len(fps)} RULES entries for this symbol, expected one"
    nick, name = fps[0].split(":", 1)
    if nick == board.config().PROJECT:
        return (*spec_base(name), None)
    return fps[0], {}, None


def symbol_renumber(d):
    lib = board.path(d["lib"]) if d.get("lib") else board.path("lib", f"{board.config().PROJECT}.kicad_sym")
    sym = next((s for s in sexpr.find_all(sexpr.load(lib), "symbol") if s[1] == d["symbol"]), None)
    if sym is None:
        raise SystemExit(f"check_pin_order: symbol {d['symbol']!r} not in {os.path.relpath(lib, board.root())}")
    out, bad = {}, []
    for pin in sexpr.walk(sym, "pin"):
        name, num = sexpr.find(pin, "name")[1], sexpr.find(pin, "number")[1]
        m = re.fullmatch(d["pattern"], name)
        if not m or not m.lastindex:
            bad.append(f"symbol {d['symbol']} pin {num}: name {name!r} does not match {d['pattern']!r}")
            continue
        out[m.group(m.lastindex)] = num
    return out, bad


def entry_name(e):
    return e.get("name") or e.get("refs") or "(entry)"


def resolve(e):
    """(label, pads, renumber, errors) of an entry's base."""
    keys = [k for k in ("footprint", "spec", "rule", "ideal") if k in e]
    if len(keys) != 1:
        raise SystemExit(f"check_pin_order: {entry_name(e)}: give exactly one of footprint / spec / rule / ideal "
                         f"(got {keys})")
    errs = []
    if "ideal" in e:
        d = e["ideal"]
        return f"ideal n={d['n']} PCD {d['pcd']:g} mm", ideal_pads(d), {}, errs
    if "footprint" in e:
        fpid, ren = e["footprint"], dict(e.get("renumber", {}))
    elif "spec" in e:
        fpid, ren = spec_base(e["spec"])
    else:
        fpid, ren, err = rule_base(e["rule"])
        if err:
            return e["rule"], None, None, [err]
    if "symbol_renumber" in e:
        ren, errs = symbol_renumber(e["symbol_renumber"])
    return fpid, base_pads(fpid, ren), ren, errs


def turn_direction(pads):
    pts = {}
    for p in pads:
        if p["pin"].isdigit():
            pts[int(p["pin"])] = (p["x_mm"], p["y_mm"])
    ks = sorted(pts)
    cx = sum(x for x, _ in pts.values()) / len(pts)
    cy = sum(y for _, y in pts.values()) / len(pts)
    tot = 0.0
    for a, b in zip(ks, ks[1:]):
        a0 = math.atan2(pts[a][1] - cy, pts[a][0] - cx)
        a1 = math.atan2(pts[b][1] - cy, pts[b][0] - cx)
        tot += (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
    return ("counter-clockwise" if tot > 0 else "clockwise"), ks[0], ks[-1]


ENTRY_KEYS = {"name", "note", "render", "refs", "footprint", "renumber", "spec", "rule", "ideal", "symbol_renumber",
              "on_ideal", "mirror", "res", "other", "angle", "free_angle", "nofit", "polarity", "turn", "distances",
              "tol"}


def check_entry(e, ipc, bad, renders):
    name = entry_name(e)
    unknown = sorted(set(e) - ENTRY_KEYS)
    if unknown:
        raise SystemExit(f"check_pin_order: {name}: unknown key(s) {unknown}")
    if any(k in e for k in ("footprint", "spec", "rule", "ideal")) and e.get("mirror") is not None and "res" not in e:
        raise SystemExit(f"check_pin_order: {name}: res= is required with mirror={e['mirror']}")
    refs = e.get("refs", "").split()
    for r in refs:
        if r not in ipc:
            bad.append(f"{name}: {r} has no IPC pads")
    if any(r not in ipc for r in refs):
        return
    if "turn" in e:
        for r in refs:
            d, k0, k1 = turn_direction(ipc[r])
            print(f"{name:24s} {r}: pins {k0} -> {k1} run {d} seen from the top (expected {e['turn']})")
            if d != e["turn"]:
                bad.append(f"{name} {r}: pins {k0} -> {k1} run {d}, expected {e['turn']}")
    if "distances" in e:
        tol = e.get("tol", 0.01)
        for r in refs:
            q = {p["pin"]: (p["x_mm"], p["y_mm"]) for p in ipc[r]}
            got = []
            for pair, want in e["distances"].items():
                a, b = pair.split("-")
                if a not in q or b not in q:
                    bad.append(f"{name} {r}: no pin {a if a not in q else b}")
                    continue
                dist = math.dist(q[a], q[b])
                got.append(f"{pair} {dist:.3f}")
                if abs(dist - want) > tol:
                    bad.append(f"{name} {r}: pins {pair} {dist:.3f} mm apart, expected {want:g} +- {tol:g}")
            print(f"{name:24s} {r}: distances {', '.join(got)} mm")
    if not any(k in e for k in ("footprint", "spec", "rule", "ideal")):
        return
    label, fp, ren, errs = resolve(e)
    bad.extend(f"{name}: {x}" for x in errs)
    if fp is None:
        return
    if "polarity" in e:
        got = ren.get("1", "1")
        print(f"{name:24s} polarity: base pad 1 (marked) = pin {got!r} (expected {e['polarity']!r})")
        if got != e["polarity"]:
            bad.append(f"{name}: the marked base pad 1 is pin {got!r}, expected {e['polarity']!r}")
    if "on_ideal" in e:
        targets = [("(ideal)", as_ipc(ideal_pads(e["on_ideal"])))]
    else:
        targets = [(r, ipc[r]) for r in refs]
    if not targets:
        raise SystemExit(f"check_pin_order: {name}: no target (refs= or on_ideal=)")
    free = bool(e.get("free_angle"))
    worst = {False: None, True: None}
    for r, pads in targets:
        for mir in (False, True):
            f = fpgeom.fit(fp, pads, mir, free_angle=free)
            if f["missing"] or f["extra"] or f["count_diff"]:
                bad.append(f"{name} {r}: pad numbers differ from {label}: missing {f['missing']} "
                           f"extra {f['extra']} count {f['count_diff']}")
            if worst[mir] is None or f["max_res"] > worst[mir]["max_res"]:
                worst[mir] = f
        if "on_ideal" not in e and e.get("render", True) and e.get("mirror") is not None and r not in renders:
            renders[r] = (label if ":" in label else None, fp, e["mirror"], free)
    u, m = worst[False], worst[True]
    want = e.get("mirror")
    if want is None:
        lim = e.get("nofit", 0.1)
        ok = u["max_res"] > lim
        result = "no fit on top" if ok else "FITS on top (unexpected)"
        expect = f"unmirrored > {lim:g}"
    else:
        tie = abs(m["max_res"] - u["max_res"]) < TIE
        best_mir = want if tie else m["max_res"] < u["max_res"]
        best, oth = (m, u) if best_mir else (u, m)
        lim = e["res"]
        ok = best_mir == want and best["max_res"] <= lim
        result = f"{'mirrored' if best_mir else 'unmirrored'} {best['max_res']:.3f}"
        expect = f"{'mirrored' if want else 'unmirrored'} <= {lim:g}"
        if "other" in e:
            other = (u if want else m)["max_res"]
            ok = ok and other > e["other"]
            expect += f", {'unmirrored' if want else 'mirrored'} > {e['other']:g}"
            if other <= e["other"] and best_mir == want:
                result += f" ({'unmirrored' if want else 'mirrored'} {other:.3f})"
        if "angle" in e:
            ang = best["angle"]
            ok = ok and abs((ang - e["angle"] + 180) % 360 - 180) < 0.01
            result += f" @{ang:g} deg"
            expect += f" @{e['angle']:g} deg"
    cell = lambda w: f"{w['max_res']:7.3f} @{round(w['angle'], 2) % 360:6.4g}"  # noqa: E731
    short = label.split(":")[-1]
    print(f"{name:24s} {short:44s} {cell(u):>16s} {cell(m):>16s}  {result}"
          + (f"  ({e['note']})" if e.get("note") else ""))
    if not ok:
        bad.append(f"{name}: {result}, expected {expect}")


def check_bottom(refs, bad):
    refs = sorted(refs)
    with open(board.path("reports", "footprint_fit.csv")) as f:
        rows = list(csv.DictReader(l for l in f if not l.startswith("# ")))
    wrong = [r["refdes"] for r in rows if (r["side"] == "bottom") != (r["refdes"] in refs)]
    if wrong:
        bad.append(f"reports/footprint_fit.csv: wrong side for {wrong} (bottom = exactly {refs})")
    brd = pcb.board_file()
    rel = os.path.relpath(brd, board.root())
    layers = {}
    for node in sexpr.find_all(sexpr.load(brd), "footprint"):
        ref = next((p[2] for p in sexpr.find_all(node, "property") if p[1] == "Reference"), "")
        layers[ref] = sexpr.find(node, "layer")[1]
    wrong = sorted(r for r, l in layers.items() if l != ("B.Cu" if r in refs else "F.Cu"))
    if wrong or not set(refs) <= set(layers):
        bad.append(f"{rel}: wrong layer for {wrong or refs} (B.Cu = exactly {refs})")
    else:
        print(f"{'bottom':24s} footprint_fit.csv and {rel}: {' '.join(refs)} bottom (B.Cu), "
              f"the other {len(layers) - len(refs)} footprints top")


def check_max_move(limits, bad):
    proj = board.config().PROJECT
    for name, spec in sorted(script().PROJECT.items()):
        if not spec.get("base"):
            continue
        basep = {p["num"]: p for p in base_pads(spec["base"], spec.get("renumber", {}))}
        mine = {p["num"]: p for p in fpgeom.load_footprint(footprint_path(f"{proj}:{name}")) if p["num"] != ""}
        if set(basep) != set(mine):
            bad.append(f"{name}: pad numbers {sorted(mine)} != renumbered base {sorted(basep)}")
            continue
        move = max(math.hypot(mine[n]["x"] - basep[n]["x"], mine[n]["y"] - basep[n]["y"]) for n in mine)
        lim = limits.get(name)
        print(f"{'derived':24s} {name:52s} pads moved {move:6.3f} mm (limit {lim})")
        if lim is None or move > lim:
            bad.append(f"{name}: pads moved {move:.3f} mm from the renumbered base {spec['base']} (limit {lim})")
    for name in sorted(set(limits) - {n for n, s in script().PROJECT.items() if s.get("base")}):
        bad.append(f"PIN_ORDER_MAX_MOVE: {name!r} is not a PROJECT spec with a base")


def graphics(fpid):
    """F.SilkS / F.Fab polylines of a footprint (footprint frame, Y up)."""
    lines = []

    def xy(n):
        return float(n[1]), -float(n[2])
    for g in sexpr.load(footprint_path(fpid)):
        if not isinstance(g, list) or not g or str(g[0]) not in ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly"):
            continue
        layer = sexpr.find(g, "layer")
        if layer is None or layer[1] not in ("F.SilkS", "F.Fab"):
            continue
        kind = str(g[0])
        if kind == "fp_line":
            lines.append([xy(sexpr.find(g, "start")), xy(sexpr.find(g, "end"))])
        elif kind == "fp_rect":
            (x0, y0), (x1, y1) = xy(sexpr.find(g, "start")), xy(sexpr.find(g, "end"))
            lines.append([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])
        elif kind == "fp_circle":
            (cx, cy), (ex, ey) = xy(sexpr.find(g, "center")), xy(sexpr.find(g, "end"))
            r = math.hypot(ex - cx, ey - cy)
            lines.append([(cx + r * math.cos(t / 20 * math.pi), cy + r * math.sin(t / 20 * math.pi)) for t in range(41)])
        elif kind == "fp_arc":
            lines.append(_arc(xy(sexpr.find(g, "start")), xy(sexpr.find(g, "mid")), xy(sexpr.find(g, "end"))))
        else:
            pts = [xy(p) for p in sexpr.find(g, "pts")[1:]]
            lines.append(pts + pts[:1])
    return lines


def _arc(a, m, b):
    (ax, ay), (bx, by), (cx, cy) = a, m, b
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return [a, m, b]
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    t0, tm, t1 = (math.atan2(p[1] - uy, p[0] - ux) for p in (a, m, b))

    def ccw(s, e):
        return (e - s) % (2 * math.pi)
    span = ccw(t0, t1) if ccw(t0, tm) <= ccw(t0, t1) else -ccw(t1, t0)
    return [(ux + r * math.cos(t0 + span * k / 24), uy + r * math.sin(t0 + span * k / 24)) for k in range(25)]


def render(ref, fpid, fp, fr, ipc_pads, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle
    from . import gerber

    def to_board(x, y):
        a = math.radians(fr["angle"])
        x = -x if fr["mirror"] else x
        return fr["tx"] + math.cos(a) * x - math.sin(a) * y, fr["ty"] + math.sin(a) * x + math.cos(a) * y
    side = "Bottom" if fr["mirror"] else "Top"
    xs = [p["x_mm"] for p in ipc_pads]
    ys = [p["y_mm"] for p in ipc_pads]
    m = max(4.0, 0.4 * max(max(xs) - min(xs), max(ys) - min(ys)))
    box = (min(xs) - m, max(xs) + m, min(ys) - m, max(ys) + m)
    cadcam = board.path(board.config().CADCAM)
    layers = [l for l in (f"{side} Silk Screen", f"{side} Assembly") if glob.glob(os.path.join(cadcam, f"*{l}.GBR"))]
    fig, axs = plt.subplots(1, max(1, len(layers)), figsize=(5.5 * max(1, len(layers)), 5.5), squeeze=False)
    for ax, layer in zip(axs[0], layers or [None]):
        if layer:
            for d in gerber.parse(glob.glob(os.path.join(cadcam, f"*{layer}.GBR"))[0]).of(gerber.Draw):
                if box[0] <= d.x1 <= box[1] and box[2] <= d.y1 <= box[3]:
                    ax.plot([d.x1, d.x2], [d.y1, d.y2], color="k", lw=1.0)
        for p in ipc_pads:
            ax.add_patch(Circle((p["x_mm"], p["y_mm"]), min(p["pad_w"], p["pad_h"]) / 2, color="orange", alpha=0.5))
            ax.text(p["x_mm"], p["y_mm"], p["pin"], ha="center", va="center", fontsize=7, color="b")
        if fpid:
            for line in graphics(fpid):
                pts = [to_board(x, y) for x, y in line]
                ax.plot([p[0] for p in pts], [p[1] for p in pts], color="r", lw=0.8)
        for num, f, _, _ in fr["pads"]:
            bx, by = to_board(f["x"], f["y"])
            ax.add_patch(Circle((bx, by), 0.2, color="r"))
            if num in ("1", "K", "+") or f is fr["pads"][0][1]:
                ax.text(bx + 0.3, by + 0.3, f"KiCad {num}", fontsize=6, color="r")
        ax.set_title(f"{ref} {layer or ''}: black = Proteus, orange = IPC pads (ids)\nred = unmoved "
                     f"{(fpid or 'ideal').split(':')[-1]} ({'mirrored, ' if fr['mirror'] else ''}"
                     f"{fr['angle']:g} deg, res {fr['max_res']:.3f})", fontsize=7)
        ax.set_aspect("equal")
        ax.set_xlim(box[0], box[1])
        ax.set_ylim(box[2], box[3])
    fig.savefig(out, dpi=110, bbox_inches="tight")
    plt.close(fig)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    unknown = [a for a in argv if a != "--render"]
    if unknown:
        print(f"check_pin_order: unknown argument(s) {unknown} (usage: p2k check_pin_order [--render])")
        return 2
    cfg = board.config()
    table = getattr(cfg, "PIN_ORDER", None)
    if table is None:
        raise SystemExit("check_pin_order: no PIN_ORDER table in p2k_config.py")
    ipc = fpgeom.ipc_pads_by_refdes()
    bad, renders = [], {}
    print(f"{'check':24s} {'base (unmoved)':44s} {'unmirrored':>16s} {'mirrored':>16s}  result")
    for e in table:
        check_entry(e, ipc, bad, renders)
    if getattr(cfg, "PIN_ORDER_MAX_MOVE", None) is not None:
        check_max_move(cfg.PIN_ORDER_MAX_MOVE, bad)
    if getattr(cfg, "PIN_ORDER_BOTTOM", None) is not None:
        check_bottom(cfg.PIN_ORDER_BOTTOM, bad)
    if "--render" in argv:
        os.makedirs(board.path("reports", "pin_order"), exist_ok=True)
        for ref, (fpid, fp, mir, free) in renders.items():
            render(ref, fpid, fp, fpgeom.fit(fp, ipc[ref], mir, free_angle=free), ipc[ref],
                   board.path("reports", "pin_order", f"{ref}.png"))
        print(f"check_pin_order: {len(renders)} renders in reports/pin_order/")
    for b in bad:
        print("check_pin_order: ERROR " + b)
    print("check_pin_order: " + (f"FAILED ({len(bad)})" if bad else f"OK ({len(table)} checks as recorded)"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
