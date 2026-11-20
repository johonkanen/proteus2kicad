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
rot = 0/90/180/270 (counter-clockwise, as in eeschema), mirror = None / 'x' / 'y' (applied after
the rotation, as KiCad does). Pin positions returned by .pin() are the connection points.
save() warns (stdout) about a pin drawn on the middle of a wire without a junction (KiCad does not
connect it); Sheet(..., split_wires=True) splits such wires instead.
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


def _xform(px, py, rot, mirror=None):
    """Library point (Y up) -> offset (Y up) of a symbol at `rot` with `mirror` ('x' = about the
    X axis, 'y' = about the Y axis), as KiCad draws (at x y rot) (mirror ...): rotated first,
    then mirrored on the sheet."""
    dx, dy = _rot(px, py, rot)
    if mirror == "x":
        dy = -dy
    elif mirror == "y":
        dx = -dx
    elif mirror is not None:
        raise ValueError(f"mirror must be None, 'x' or 'y', not {mirror!r}")
    return dx, dy


def _r(v):
    return round(v, 4)


def _body_points(unit_sym, rot, mirror, pins=True, axis=None):
    """Transformed (rot, mirror) points of a unit's graphics (rectangle, polyline, circle, arc)
    and, with pins, of its pins (both ends): (dx, dy) offsets, Y up, or one coordinate (axis 0 / 1)."""
    def xy(n):
        return float(n[1]), float(n[2])

    pts = []
    for r in find_all(unit_sym, "rectangle"):
        (x1, y1), (x2, y2) = xy(find(r, "start")), xy(find(r, "end"))
        pts += [(x1, y1), (x2, y2), (x1, y2), (x2, y1)]
    for pl in find_all(unit_sym, "polyline"):
        pts += [xy(q) for q in find_all(find(pl, "pts"), "xy")]
    for c in find_all(unit_sym, "circle"):
        (cx, cy), rad = xy(find(c, "center")), float(find(c, "radius")[1])
        pts += [(cx - rad, cy - rad), (cx + rad, cy + rad), (cx - rad, cy + rad), (cx + rad, cy - rad)]
    for a in find_all(unit_sym, "arc"):
        pts += [xy(find(a, k)) for k in ("start", "mid", "end")]
    if pins:
        for p in find_all(unit_sym, "pin"):
            at = find(p, "at")
            px, py, ang = float(at[1]), float(at[2]), float(at[3]) if len(at) > 3 else 0.0
            ln = float(find(p, "length")[1]) if find(p, "length") else 0.0
            d = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}[int(ang) % 360]
            pts += [(px, py), (px + d[0] * ln, py + d[1] * ln)]
    out = [_xform(a, b, rot, mirror) for a, b in pts]
    return out if axis is None else [q[axis] for q in out]


class Placed:
    def __init__(self, ref, pins):
        self.ref, self.pins = ref, pins

    def pin(self, num):
        return self.pins[str(num)]


class Sheet:
    def __init__(self, path, project=None, split_wires=False):
        """split_wires: save() splits a wire segment at every pin point drawn on its middle (KiCad
        connects a pin only at a wire end); without it save() prints a warning per such pin."""
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
        self.split_wires = split_wires
        self._pin_points, self._wires, self._junctions = [], [], []
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
        self._pin_points, self._wires, self._junctions = [], [], []
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

    def place(self, name, ref, x, y, rot=0, unit=1, value=None, fields=None, value_at=None, text=None, ref_at=None,
              mirror=None, fields_auto=False, text_gap=None, hide_value=False):
        """value_at = (x, y, angle, justify) overrides the Value field placement (absolute
        sheet coordinates; angle is relative to the symbol, as KiCad stores it).
        text: for two-pin parts, where Reference+Value go: 'above'/'below' (horizontal part)
        or 'left'/'right' (vertical part); default is beside / above-and-below.
        mirror: None, 'x' (about the X axis: top and bottom swapped) or 'y' (about the Y axis:
        left and right swapped), for every rotation; KiCad rotates first, then mirrors on the
        sheet. The returned pin points and the default Reference / Value positions of multi-pin
        symbols are mirrored (hidden fields keep the unmirrored offset; two-pin texts are placed
        around the body centre and do not change).
        fields_auto: multi-pin symbols: Reference and Value centred and horizontal above the
        rotated / mirrored body (instead of the library offsets, which are meant for rotation 0).
        text_gap: horizontal two-pin parts: mm from the part's centre line to the texts
        (Reference above, Value below; with text='above' / 'below' both on that side, 1.905 mm
        apart), or 'body' = half the body height plus 1.27 mm, rounded up to the 1.27 mm grid.
        hide_value: the Value field is hidden (e.g. on all but one unit of a multi-unit part).
        ref_at / value_at always win."""
        sym = lib()[name]
        _xform(0, 0, 0, mirror)              # validates the axis before anything is added
        self._cache(name)
        ob = find(sym, "on_board")
        node = [Sym("symbol"), [Sym("lib_id"), f"{self.project}:{name}"], [Sym("at"), x, y, rot], [Sym("unit"), unit],
                [Sym("exclude_from_sim"), Sym("no")], [Sym("in_bom"), Sym("yes")],
                [Sym("on_board"), Sym("no" if ob and ob[1] == "no" else "yes")], [Sym("dnp"), Sym("no")],
                [Sym("uuid"), U(f"sym:{ref}:{unit}")]]
        if mirror is not None:
            node.insert(3, [Sym("mirror"), Sym(mirror)])
        is_power = find(sym, "power") is not None
        pins = {}
        pin_nodes = []
        units = []
        for sub in find_all(sym, "symbol"):
            u = int(sub[1].rsplit("_", 2)[1])
            if u not in (0, unit):
                continue
            units.append(sub)
            for p in find_all(sub, "pin"):
                num = find(p, "number")[1]
                at = find(p, "at")
                dx, dy = _xform(float(at[1]), float(at[2]), rot, mirror)
                pins[num] = (_r(x + dx), _r(y - dy))
                pin_nodes.append([Sym("pin"), num, [Sym("uuid"), U(f"pin:{ref}:{unit}:{num}")]])
        self._pin_points.extend((ref, num, pt) for num, pt in pins.items())
        two_pin = len(pins) == 2 and not is_power
        if two_pin:
            (ax, ay), (bx, by) = pins.values()
            vertical = abs(ax - bx) < 1e-6
        if text_gap is not None and not (two_pin and not vertical):
            raise ValueError(f"{ref}: text_gap is for horizontal two-pin parts")
        if text_gap == "body":
            ys = [dy for sub in units for dy in _body_points(sub, rot, mirror, pins=False, axis=1)]
            half = max(abs(v) for v in ys)
            text_gap = _r(-(-(half + 1.27) // 1.27) * 1.27)
        auto = None
        if fields_auto and not two_pin and not is_power:
            xs = [x + d for sub in units for d in _body_points(sub, rot, mirror, axis=0)]
            ys = [y - d for sub in units for d in _body_points(sub, rot, mirror, axis=1)]
            cx, top = _r((min(xs) + max(xs)) / 2), min(ys)
            auto = {"Reference": (cx, _r(top - 3.81)), "Value": (cx, _r(top - 1.905))}
        props = {p[1]: p for p in find_all(sym, "property")}
        for key in ("Reference", "Value", "Footprint", "Datasheet", "Description"):
            val = {"Reference": ref, "Value": value or props["Value"][2]}.get(key, props.get(key, [0, 0, ""])[2])
            if key == "Footprint":
                val = footprint(ref) or val
            at = props[key][3] if key in props else [Sym("at"), 0, 0, 0]
            hide = key not in ("Reference", "Value") or (is_power and key == "Reference") or (hide_value and key == "Value")
            dx, dy = _xform(float(at[1]), float(at[2]), rot, None if hide else mirror)
            override = value_at if key == "Value" else ref_at if key == "Reference" else None
            if override:
                node.append([Sym("property"), key, val, [Sym("at"), override[0], override[1], override[2]],
                             _eff(override[3], hide=hide)])
                continue
            fa = {0: 0, 90: 270, 180: 0, 270: 90}[rot % 360]
            if auto and key in auto:
                node.append([Sym("property"), key, val, [Sym("at"), *auto[key], fa], _eff(hide=hide)])
                continue
            if two_pin and key in ("Reference", "Value"):
                # KiCad mirrors/swaps field justification for rotated symbols, so use centred
                # text: beside a vertical part, above/below a horizontal one.
                sign = -1 if key == "Reference" else 1
                if vertical:
                    side = -1 if text == "left" else 1
                    fx, fy = x + side * (2.54 + 0.55 * len(val)), y + sign * 1.27
                elif text_gap is not None:
                    if text == "above":
                        fx, fy = x, y - text_gap - (1.905 if key == "Reference" else 0)
                    elif text == "below":
                        fx, fy = x, y + text_gap + (0 if key == "Reference" else 1.905)
                    else:
                        fx, fy = x, y + sign * text_gap
                elif text in ("above", "below"):
                    base = -3.81 if text == "above" else 2.03
                    fx, fy = x, y + base + (0 if key == "Reference" else 1.78)
                else:
                    fx, fy = x, y + sign * 2.54
                node.append([Sym("property"), key, val, [Sym("at"), _r(fx), _r(fy), fa], _eff(hide=hide)])
                continue
            # multi-pin symbols: keep the library field placement (meant for rot 0; a symbol
            # rotated 180 gets its left/right justification mirrored by KiCad, so swap it; a
            # mirrored symbol keeps the library justification: with the position mirrored, KiCad's
            # own flip then draws the text away from the body for rot 0 and 180)
            just = None
            if key in props:
                eff = find(props[key], "effects")
                j = find(eff, "justify") if eff else None
                if j:
                    just = " ".join(str(a) for a in j[1:])
                    if rot % 360 == 180 and mirror is None:
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
        self._junctions.append((x, y))

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
                w = [Sym("wire"), [Sym("pts"), [Sym("xy"), *q], [Sym("xy"), *p]],
                     [Sym("stroke"), [Sym("width"), 0], [Sym("type"), Sym("default")]], [Sym("uuid"), U()]]
                self._add(w)
                self._wires.append(w)

    def text(self, s, x, y, size=1.27):
        self._add([Sym("text"), s, [Sym("exclude_from_sim"), Sym("no")], [Sym("at"), x, y, 0],
                   [Sym("effects"), [Sym("font"), [Sym("size"), size, size]], [Sym("justify"), Sym("left"), Sym("bottom")]],
                   [Sym("uuid"), U()]])

    def mid_wire_pins(self):
        """[(ref, pin, point, wire node)]: pins drawn by this script whose connection point lies
        strictly inside a wire segment drawn by it, without a junction there (KiCad connects a
        pin to a wire only at the wire's end or at a junction)."""
        out = []
        for ref, num, (px, py) in self._pin_points:
            if any(abs(px - jx) < 1e-6 and abs(py - jy) < 1e-6 for jx, jy in self._junctions):
                continue
            for w in self._wires:
                (_, x1, y1), (_, x2, y2) = find_all(find(w, "pts"), "xy")
                if any(abs(px - ex) < 1e-6 and abs(py - ey) < 1e-6 for ex, ey in ((x1, y1), (x2, y2))):
                    continue
                if (abs(x1 - x2) < 1e-6 and abs(px - x1) < 1e-6 and min(y1, y2) < py < max(y1, y2)) or \
                        (abs(y1 - y2) < 1e-6 and abs(py - y1) < 1e-6 and min(x1, x2) < px < max(x1, x2)):
                    out.append((ref, num, (px, py), w))
        return out

    def save(self):
        name = os.path.basename(self.path)
        found = self.mid_wire_pins()
        while self.split_wires and found:
            ref, num, pt, w = found[0]
            pts = find(w, "pts")
            (_, x1, y1), (_, x2, y2) = find_all(pts, "xy")
            pts[2] = [Sym("xy"), *pt]
            w2 = copy.deepcopy(w)
            find(w2, "pts")[1:] = [[Sym("xy"), *pt], [Sym("xy"), x2, y2]]
            find(w2, "uuid")[1] = U(f"split:{find(w, 'uuid')[1]}:{pt[0]}:{pt[1]}")
            self.tree.insert(next(i for i, c in enumerate(self.tree) if c is w) + 1, w2)
            self._wires.append(w2)
            print(f"sch_builder: {name}: wire ({_r(float(x1))}, {_r(float(y1))})-({_r(float(x2))}, {_r(float(y2))}) split at pin {ref}-{num}")
            found = self.mid_wire_pins()
        for ref, num, pt, w in found:
            (_, x1, y1), (_, x2, y2) = find_all(find(w, "pts"), "xy")
            x1, y1, x2, y2 = (_r(float(v)) for v in (x1, y1, x2, y2))
            print(f"WARNING sch_builder: {name}: pin {ref}-{num} at {pt} lies inside the wire "
                  f"({x1}, {y1})-({x2}, {y2}); KiCad does not connect it (end the wire there, "
                  f"or Sheet(split_wires=True))")
        save(self.path, self.tree)
