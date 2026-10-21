#!/usr/bin/env python3
"""Small helper for writing KiCad 10 sub-sheets programmatically.

    from p2k.sch_builder import Sheet
    sh = Sheet("kicad/03_regulators.kicad_sch")
    u3 = sh.place("TPS73033DBVT", "U3", 50.8, 50.8, value="TPS73033DBVT")
    sh.power("5V", *u3.pin("1"))            # power symbol on pin 1
    sh.label("REF_1V65", *r19.pin("2"))     # local label
    sh.glabel("PWM_G1", *j1.pin("43"))      # global label
    sh.no_connect(*u3.pin("4"))
    sh.wire(u3.pin("5"), c69.pin("2"))      # straight or L-shaped wire
    sh.save()

Symbols come from lib/<PROJECT>.kicad_sym (copied into the sheet's lib_symbols, PROJECT from
p2k_config.py); the Footprint field comes from lib/footprint_map.csv (gen_footprints.py).
UUIDs are deterministic (uuid5 per sheet file: symbols/pins keyed by refdes, unit and pin,
everything else by drawing order), so re-running a sheet script keeps the symbol UUIDs the
PCB links to.
Coordinates are schematic mm (Y down); keep them on the 1.27 mm grid.
rot = 0/90/180/270 (counter-clockwise, as in eeschema). Pin positions returned
by .pin() are the connection points.
"""
import copy
import os
import uuid

from . import board
from .sexpr import Sym, find, find_all, load, save, walk

_LIB = None
_FOOTPRINTS = None
_UUID_NS = None
_UUID_N = 0


def lib():
    global _LIB
    if _LIB is None:
        _LIB = {s[1]: s for s in find_all(load(board.path("lib", f"{_project()}.kicad_sym")), "symbol")}
    return _LIB


def footprint(ref):
    """Footprint of a refdes from lib/footprint_map.csv ('' if none)."""
    global _FOOTPRINTS
    if _FOOTPRINTS is None:
        import csv
        path = board.path("lib", "footprint_map.csv")
        _FOOTPRINTS = {}
        if os.path.exists(path):
            with open(path) as f:
                _FOOTPRINTS = {r["refdes"]: r["footprint"]
                               for r in csv.DictReader(l for l in f if not l.startswith("# "))}
    return _FOOTPRINTS.get(ref, "")


def _project():
    return board.config().PROJECT


def _uuid_seed(fname):
    global _UUID_NS, _UUID_N
    _UUID_NS = uuid.uuid5(uuid.NAMESPACE_URL, _project() + "/" + fname)
    _UUID_N = 0


def U(key=None):
    global _UUID_N
    if _UUID_NS is None:
        return str(uuid.uuid4())
    if key is None:
        _UUID_N += 1
        key = f"#{_UUID_N}"
    return str(uuid.uuid5(_UUID_NS, key))


def _eff(justify=None, hide=False):
    e = [Sym("effects"), [Sym("font"), [Sym("size"), 1.27, 1.27]]]
    if justify:
        e.append([Sym("justify"), *[Sym(j) for j in justify.split()]])
    if hide:
        e.append([Sym("hide"), Sym("yes")])
    return e


def _rot(px, py, rot):
    return {0: (px, py), 90: (-py, px), 180: (-px, -py), 270: (py, -px)}[rot % 360]


def _r(v):
    return round(v, 4)


class Placed:
    def __init__(self, ref, pins):
        self.ref, self.pins = ref, pins

    def pin(self, num):
        return self.pins[str(num)]


class Sheet:
    def __init__(self, path, project=None):
        project = project or _project()
        self.path = path
        self.tree = load(path)
        self.project = project
        root = load(os.path.join(os.path.dirname(path), f"{project}.kicad_sch"))
        fname = os.path.basename(path)
        sheet = [s for s in find_all(root, "sheet")
                 if any(p[1] == "Sheetfile" and p[2] == fname for p in find_all(s, "property"))]
        if not sheet:
            raise ValueError(f"{fname} is not a sheet of {project}.kicad_sch")
        self.inst_path = f"/{find(root, 'uuid')[1]}/{find(sheet[0], 'uuid')[1]}"
        _uuid_seed(fname)
        self.cache = find(self.tree, "lib_symbols")
        self.pwr = 0
        for s in find_all(self.tree, "symbol"):
            ref = next((p[2] for p in find_all(s, "property") if p[1] == "Reference"), "")
            if ref.startswith("#PWR") and ref[4:].isdigit():
                self.pwr = max(self.pwr, int(ref[4:]))

    def clear(self):
        """Remove everything drawn so far (symbols, wires, labels, ...) and empty lib_symbols,
        so a sheet script can be re-run from scratch. Header, uuid, paper and title block stay."""
        drawn = {"symbol", "wire", "bus", "junction", "label", "global_label", "hierarchical_label",
                 "no_connect", "text", "polyline", "rectangle", "bus_entry", "image", "text_box"}
        self.tree[:] = [c for c in self.tree if not (isinstance(c, list) and c[0] in drawn)]
        del self.cache[1:]
        self.pwr = 0
        _uuid_seed(os.path.basename(self.path))
        return self

    def _add(self, node):
        i = next(k for k, c in enumerate(self.tree) if isinstance(c, list) and c[0] in ("sheet_instances", "embedded_fonts"))
        self.tree.insert(i, node)

    def _cache(self, name):
        if not any(c[1] == f"{self.project}:{name}" for c in self.cache[1:]):
            s = copy.deepcopy(lib()[name])
            s[1] = f"{self.project}:{name}"
            self.cache.append(s)

    def place(self, name, ref, x, y, rot=0, unit=1, value=None, fields=None, value_at=None, text=None, ref_at=None):
        """value_at = (x, y, angle, justify) overrides the Value field placement (absolute
        sheet coordinates; angle is relative to the symbol, as KiCad stores it).
        text: for two-pin parts, where Reference+Value go: 'above'/'below' (horizontal part)
        or 'left'/'right' (vertical part); default is beside / above-and-below."""
        sym = lib()[name]
        self._cache(name)
        ob = find(sym, "on_board")
        node = [Sym("symbol"), [Sym("lib_id"), f"{self.project}:{name}"], [Sym("at"), x, y, rot], [Sym("unit"), unit],
                [Sym("exclude_from_sim"), Sym("no")], [Sym("in_bom"), Sym("yes")],
                [Sym("on_board"), Sym("no" if ob and ob[1] == "no" else "yes")], [Sym("dnp"), Sym("no")],
                [Sym("uuid"), U(f"sym:{ref}:{unit}")]]
        is_power = find(sym, "power") is not None
        pins = {}
        pin_nodes = []
        for sub in find_all(sym, "symbol"):
            u = int(sub[1].rsplit("_", 2)[1])
            if u not in (0, unit):
                continue
            for p in find_all(sub, "pin"):
                num = find(p, "number")[1]
                at = find(p, "at")
                dx, dy = _rot(float(at[1]), float(at[2]), rot)
                pins[num] = (_r(x + dx), _r(y - dy))
                pin_nodes.append([Sym("pin"), num, [Sym("uuid"), U(f"pin:{ref}:{unit}:{num}")]])
        two_pin = len(pins) == 2 and not is_power
        if two_pin:
            (ax, ay), (bx, by) = pins.values()
            vertical = abs(ax - bx) < 1e-6
        props = {p[1]: p for p in find_all(sym, "property")}
        for key in ("Reference", "Value", "Footprint", "Datasheet", "Description"):
            val = {"Reference": ref, "Value": value or props["Value"][2]}.get(key, props.get(key, [0, 0, ""])[2])
            if key == "Footprint":
                val = footprint(ref) or val
            at = props[key][3] if key in props else [Sym("at"), 0, 0, 0]
            dx, dy = _rot(float(at[1]), float(at[2]), rot)
            hide = key not in ("Reference", "Value") or (is_power and key == "Reference")
            override = value_at if key == "Value" else ref_at if key == "Reference" else None
            if override:
                node.append([Sym("property"), key, val, [Sym("at"), override[0], override[1], override[2]],
                             _eff(override[3], hide=hide)])
                continue
            if two_pin and key in ("Reference", "Value"):
                # KiCad mirrors/swaps field justification for rotated symbols, so use centred
                # text: beside a vertical part, above/below a horizontal one.
                fa = {0: 0, 90: 270, 180: 0, 270: 90}[rot % 360]
                sign = -1 if key == "Reference" else 1
                if vertical:
                    side = -1 if text == "left" else 1
                    fx, fy = x + side * (2.54 + 0.55 * len(val)), y + sign * 1.27
                elif text in ("above", "below"):
                    base = -3.81 if text == "above" else 2.03
                    fx, fy = x, y + base + (0 if key == "Reference" else 1.78)
                else:
                    fx, fy = x, y + sign * 2.54
                node.append([Sym("property"), key, val, [Sym("at"), _r(fx), _r(fy), fa], _eff(hide=hide)])
                continue
            # multi-pin symbols: keep the library field placement (meant for rot 0; a symbol
            # rotated 180 gets its left/right justification mirrored by KiCad, so swap it)
            just = None
            if key in props:
                eff = find(props[key], "effects")
                j = find(eff, "justify") if eff else None
                if j:
                    just = " ".join(str(a) for a in j[1:])
                    if rot % 360 == 180:
                        just = just.replace("left", "L").replace("right", "left").replace("L", "right")
            node.append([Sym("property"), key, val, [Sym("at"), _r(x + dx), _r(y - dy), 0], _eff(just, hide=hide)])
        for key, val in (fields or {}).items():
            node.append([Sym("property"), key, val, [Sym("at"), x, y, 0], _eff(hide=True)])
        node.extend(pin_nodes)
        node.append([Sym("instances"), [Sym("project"), self.project,
                                        [Sym("path"), self.inst_path, [Sym("reference"), ref], [Sym("unit"), unit]]]])
        self._add(node)
        return Placed(ref, pins)

    def power(self, net, x, y, rot=0, value_at=None):
        self.pwr += 1
        return self.place(net, f"#PWR{self.pwr:03d}", x, y, rot, value_at=value_at)

    def pwr_flag(self, x, y, rot=0):
        self.pwr += 1
        return self.place("PWR_FLAG", f"#FLG{self.pwr:03d}", x, y, rot)

    def label(self, name, x, y, rot=0):
        just = {0: "left bottom", 90: "left bottom", 180: "right bottom", 270: "right bottom"}[rot % 360]
        self._add([Sym("label"), name, [Sym("at"), x, y, rot], _eff(just), [Sym("uuid"), U()]])

    def glabel(self, name, x, y, rot=0, shape="passive"):
        just = {0: "left", 90: "left", 180: "right", 270: "right"}[rot % 360]
        self._add([Sym("global_label"), name, [Sym("shape"), Sym(shape)], [Sym("at"), x, y, rot],
                   [Sym("fields_autoplaced"), Sym("yes")], _eff(just), [Sym("uuid"), U()],
                   [Sym("property"), "Intersheetrefs", "${INTERSHEET_REFS}", [Sym("at"), x, y, 0], _eff(just, hide=True)]])

    def no_connect(self, x, y):
        self._add([Sym("no_connect"), [Sym("at"), x, y], [Sym("uuid"), U()]])

    def junction(self, x, y):
        self._add([Sym("junction"), [Sym("at"), x, y], [Sym("diameter"), 0], [Sym("color"), 0, 0, 0, 0],
                   [Sym("uuid"), U()]])

    def wire(self, a, b, *more, horizontal_first=True):
        """Wire through the given points; an L-bend is inserted between non-aligned points."""
        pts = [a, b, *more]
        path = [pts[0]]
        for p in pts[1:]:
            q = path[-1]
            if q[0] != p[0] and q[1] != p[1]:
                path.append((p[0], q[1]) if horizontal_first else (q[0], p[1]))
            path.append(p)
        for q, p in zip(path, path[1:]):
            if q != p:
                self._add([Sym("wire"), [Sym("pts"), [Sym("xy"), *q], [Sym("xy"), *p]],
                           [Sym("stroke"), [Sym("width"), 0], [Sym("type"), Sym("default")]], [Sym("uuid"), U()]])

    def text(self, s, x, y, size=1.27):
        self._add([Sym("text"), s, [Sym("exclude_from_sim"), Sym("no")], [Sym("at"), x, y, 0],
                   [Sym("effects"), [Sym("font"), [Sym("size"), size, size]], [Sym("justify"), Sym("left"), Sym("bottom")]],
                   [Sym("uuid"), U()]])

    def save(self):
        save(self.path, self.tree)
