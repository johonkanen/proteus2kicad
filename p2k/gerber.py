"""Minimal Gerber X2 reader for the Proteus CADCAM files and KiCad's plots (stdlib only).

Covers what Proteus 8.12 and kicad-cli 10 write: %FSLAX45Y45*% / %FSLAX46Y46*%, %MOMM*%,
G01 linear interpolation only (G02/G03 are rejected), D01 draws, D02 moves, D03 flashes,
G36/G37 regions with several contours, %LPD*% / %LPC*% polarity, standard C/R/O apertures,
and aperture macros: Proteus' PPAD (one outline primitive, code 4) and KiCad's RoundRect
(primitives 4, 1 and 20 with $n modifier expressions). The aperture function comes from the
preceding %TA.AperFunction,...*% attribute; KiCad's object attributes (%TO.P refdes,pin,
%TO.N net, %TO.C component) are attached to the objects they apply to.

Coordinates are returned in mm in the Gerber frame (= IPC frame, Y up).

parse(path) -> Gerber with
  apertures {dcode: Aperture(shape, params, function, outline, primitives)}
  objects   [Draw | Flash | Region] in file order, each with .dark (polarity) and .attrs
"""
import math
import re
from dataclasses import dataclass, field


@dataclass
class Aperture:
    dcode: int
    shape: str            # C, R, O, or a macro name
    params: tuple         # C: (d,), R / O: (w, h), macro: the modifiers
    function: str         # e.g. "Conductor", "ViaPad", "ComponentPad", "SMDPad,CuDef"
    outline: list = None  # macro with a single outline primitive: its points (mm, relative)
    primitives: list = None  # macro: [("poly", [(x, y), ...]) | ("circle", d, cx, cy)], all dark

    @property
    def diameter(self):
        return self.params[0] if self.shape == "C" else None


@dataclass
class Draw:
    ap: Aperture
    x1: float
    y1: float
    x2: float
    y2: float
    dark: bool
    attrs: dict = field(default_factory=dict)


@dataclass
class Flash:
    ap: Aperture
    x: float
    y: float
    dark: bool
    attrs: dict = field(default_factory=dict)


@dataclass
class Region:
    contours: list        # [[(x, y), ...], ...] closed (first point == last point)
    dark: bool
    attrs: dict = field(default_factory=dict)


@dataclass
class Gerber:
    path: str
    attributes: dict = field(default_factory=dict)
    apertures: dict = field(default_factory=dict)
    objects: list = field(default_factory=list)

    def of(self, kind):
        return [o for o in self.objects if isinstance(o, kind)]


_COORD = re.compile(r"([XYIJ])([+-]?\d+)")
_EXPR_OK = re.compile(r"^[\d.\s$+\-xX/()]*$")


def _macro_eval(expr, mods):
    """Value of a macro primitive modifier: a number or an expression in $n (x = multiply)."""
    if not _EXPR_OK.match(expr):
        raise ValueError(f"unsupported macro expression {expr!r}")
    e = re.sub(r"\$(\d+)", lambda m: f"({mods[int(m.group(1)) - 1]!r})", expr)
    e = e.replace("x", "*").replace("X", "*")
    return float(eval(e, {"__builtins__": {}}, {}))  # only digits, $n, + - * / ( ) survive the regex


def _rot(pts, deg):
    if not deg:
        return pts
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [(x * c - y * s, x * s + y * c) for x, y in pts]


def _macro_primitives(name, prims, mods):
    """Resolve a macro's primitives for one aperture definition (mm, relative to the flash)."""
    out = []
    for p in prims:
        v = [_macro_eval(e, mods) for e in p]
        code = int(v[0])
        if code == 4:         # outline: exposure, n, x0, y0, ..., xn, yn, rotation
            if v[1] != 1:
                raise ValueError(f"macro {name}: clear outline primitive")
            n = int(v[2])
            pts = [(v[3 + 2 * i], v[4 + 2 * i]) for i in range(n + 1)]
            out.append(("poly", _rot(pts, v[3 + 2 * (n + 1)])))
        elif code == 1:       # circle: exposure, diameter, cx, cy [, rotation]
            if v[1] != 1:
                raise ValueError(f"macro {name}: clear circle primitive")
            cx, cy = _rot([(v[3], v[4])], v[5] if len(v) > 5 else 0)[0]
            out.append(("circle", v[2], cx, cy))
        elif code == 20:      # vector line: exposure, width, x1, y1, x2, y2, rotation
            if v[1] != 1:
                raise ValueError(f"macro {name}: clear line primitive")
            w, x1, y1, x2, y2 = v[2] / 2, v[3], v[4], v[5], v[6]
            dx, dy = x2 - x1, y2 - y1
            L = math.hypot(dx, dy)
            if L == 0:
                continue
            nx, ny = -dy / L * w, dx / L * w
            pts = [(x1 + nx, y1 + ny), (x2 + nx, y2 + ny), (x2 - nx, y2 - ny), (x1 - nx, y1 - ny)]
            out.append(("poly", _rot(pts, v[7])))
        elif code == 21:      # center line: exposure, w, h, cx, cy, rotation
            if v[1] != 1:
                raise ValueError(f"macro {name}: clear center line primitive")
            w, h, cx, cy = v[2] / 2, v[3] / 2, v[4], v[5]
            pts = [(cx - w, cy - h), (cx + w, cy - h), (cx + w, cy + h), (cx - w, cy + h)]
            out.append(("poly", _rot(pts, v[6])))
        else:
            raise ValueError(f"macro {name}: unsupported primitive {code}")
    return out


def parse(path):
    with open(path) as f:
        text = f.read()
    g = Gerber(path)
    # split into extended (%...%) blocks and plain word commands
    tokens = re.findall(r"%[^%]*%|[^%*\s][^*]*\*", text)
    scale = None
    func = ""
    macros = {}
    cur_ap = None
    x = y = 0.0
    dark = True
    in_region = False
    contour = None
    region = None
    attrs = {}
    for tok in tokens:
        if tok.startswith("%"):
            body = tok[1:-1]
            if body.startswith("FS"):
                m = re.match(r"FSLAX(\d)(\d)Y(\d)(\d)\*", body)
                if not m:
                    raise ValueError(f"{path}: unsupported format {body}")
                scale = 10 ** -int(m.group(2))
            elif body.startswith("MO"):
                if body != "MOMM*":
                    raise ValueError(f"{path}: unsupported units {body}")
            elif body.startswith("TF."):
                k, _, v = body[3:-1].partition(",")
                g.attributes[k] = v
            elif body.startswith("TA.AperFunction"):
                func = body[len("TA.AperFunction,"):-1]
            elif body.startswith("TO."):
                k, _, v = body[3:-1].partition(",")
                attrs = dict(attrs)
                attrs[k] = v
            elif body.startswith("TD"):
                name = body[2:-1]
                if not name or name.startswith("TA") or name == "AperFunction":
                    func = ""
                if not name or name.startswith("TO"):
                    attrs = {}
                elif name.startswith("."):
                    attrs = {k: v for k, v in attrs.items() if k != name[1:]}
            elif body.startswith("LP"):
                dark = body[2] == "D"
            elif body.startswith("AM"):
                name, _, rest = body[2:].partition("*")
                prims = []
                for stmt in rest.split("*"):
                    stmt = stmt.strip()
                    if not stmt or stmt.startswith("0"):   # empty or comment
                        continue
                    prims.append([e.strip() for e in stmt.split(",")])
                macros[name] = prims
            elif body.startswith("AD"):
                m = re.match(r"ADD(\d+)([A-Za-z_][A-Za-z0-9_]*)(?:,([^*]*))?\*", body)
                d, shape = int(m.group(1)), m.group(2)
                params = tuple(float(p) for p in m.group(3).split("X")) if m.group(3) else ()
                ap = Aperture(d, shape, params, func)
                if shape not in ("C", "R", "O"):
                    ap.primitives = _macro_primitives(shape, macros[shape], params)
                    polys = [p for p in ap.primitives if p[0] == "poly"]
                    if len(ap.primitives) == 1 and polys:
                        ap.outline = polys[0][1]
                g.apertures[d] = ap
            continue
        w = tok[:-1]
        if w.startswith("G04") or not w:
            continue
        if w == "M02":
            break
        if w in ("G01", "G1"):
            continue
        if w in ("G02", "G03", "G75", "G74"):
            raise ValueError(f"{path}: arcs not supported ({w})")
        if w == "G36":
            in_region, region, contour = True, Region([], dark, attrs), None
            continue
        if w == "G37":
            if contour and len(contour) > 1:
                region.contours.append(contour)
            g.objects.append(region)
            in_region, region, contour = False, None, None
            continue
        m = re.fullmatch(r"D(\d+)", w)
        if m and int(m.group(1)) >= 10:
            cur_ap = g.apertures[int(m.group(1))]
            continue
        m = re.fullmatch(r"((?:[XYIJ][+-]?\d+)*)D0?([123])", w)
        if not m:
            raise ValueError(f"{path}: unsupported command {w!r}")
        nx, ny = x, y
        for c, v in _COORD.findall(m.group(1)):
            if c == "X":
                nx = round(int(v) * scale, 6)
            elif c == "Y":
                ny = round(int(v) * scale, 6)
        op = m.group(2)
        if in_region:
            if op == "2":
                if contour and len(contour) > 1:
                    region.contours.append(contour)
                contour = [(nx, ny)]
            elif op == "1":
                contour.append((nx, ny))
            else:
                raise ValueError(f"{path}: flash inside region")
        elif op == "1":
            g.objects.append(Draw(cur_ap, x, y, nx, ny, dark, attrs))
        elif op == "3":
            g.objects.append(Flash(cur_ap, nx, ny, dark, attrs))
        x, y = nx, ny
    return g
