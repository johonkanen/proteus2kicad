"""Helpers for the per-sheet transcription scripts of a board (tools/sheets/sheetNN.py).

    from p2k.sheetkit import *       # Sheet, g, sheet, left/right/up/down, labels, power_at,
                                     # bus_group, flagged_power, vert2, horiz2, terminal, rail
    sh = sheet("11_startup.kicad_sch")
    c, top, bot = vert2(sh, "C", "C14", g(40), g(40), "4.7u")
    power_at(sh, "20V", top, "up"); power_at(sh, "GND", bot, "down")
    sh.save()

Each script rebuilds its sub-sheet from scratch (Sheet.clear()) so it can be re-run after
a fix. Coordinates: schematic mm, Y down, 1.27 mm grid. Rotations CCW like eeschema.
"""
from . import board
from .sch_builder import Sheet  # noqa: F401

G = 1.27  # grid


def g(n):
    """n grid units in mm."""
    return round(n * G, 4)


def sheet(fname, **kw):
    """The sub-sheet kicad/<fname> of the board repo, cleared for a fresh rebuild (keywords go to
    Sheet, e.g. split_wires=True)."""
    return Sheet(board.path("kicad", fname), **kw).clear()


def left(p, n=2):
    return (round(p[0] - g(n), 4), p[1])


def right(p, n=2):
    return (round(p[0] + g(n), 4), p[1])


def up(p, n=2):
    return (p[0], round(p[1] - g(n), 4))


def down(p, n=2):
    return (p[0], round(p[1] + g(n), 4))


def glabel_left(sh, name, p, n=2):
    """Wire n grid units to the left of pin point p and put a global label there."""
    q = left(p, n)
    sh.wire(p, q)
    sh.glabel(name, *q, 180)
    return q


def glabel_right(sh, name, p, n=2):
    q = right(p, n)
    sh.wire(p, q)
    sh.glabel(name, *q, 0)
    return q


def label_left(sh, name, p, n=2):
    q = left(p, n)
    sh.wire(p, q)
    sh.label(name, *q, 180)
    return q


def label_right(sh, name, p, n=2):
    q = right(p, n)
    sh.wire(p, q)
    sh.label(name, *q, 0)
    return q


def power_at(sh, net, p, direction):
    """Power symbol whose connection point is p, body pointing in `direction`
    ('up', 'down', 'left', 'right'). Works for the ground-style (pin dir 270) and
    supply-style (pin dir 90) project power symbols."""
    from p2k.sch_builder import lib
    from p2k.sexpr import find, walk
    sym = lib()[net]
    pin_dir = int(float(find(next(walk(sym, "pin")), "at")[3]))
    body = {90: "up", 270: "down"}[pin_dir]
    want = {"up": 0, "left": 90, "down": 180, "right": 270}[direction]
    have = {"up": 0, "left": 90, "down": 180, "right": 270}[body]
    rot = (want - have) % 360
    value_at = None
    if direction == "left":
        value_at = (round(p[0] - 3.3, 4), p[1], rot, "left")
    elif direction == "right":
        value_at = (round(p[0] + 3.3, 4), p[1], rot, "right")
    return sh.power(net, p[0], p[1], rot, value_at=value_at)


def bus_group(sh, pins, label_pin, side, label=None, power=None, n=2, m=2):
    """Join vertically stacked pin points `pins` (list of (x,y)) with a bus line n grid
    units outward (side 'left'/'right'), then branch m more units outward at the y of
    `label_pin` and attach a global label or a power symbol there."""
    sgn = -1 if side == "left" else 1
    xv = round(pins[0][0] + sgn * g(n), 4)
    ys = sorted(p[1] for p in pins)
    for p in pins:
        sh.wire(p, (xv, p[1]))
    if len(pins) > 1:
        sh.wire((xv, ys[0]), (xv, ys[-1]))
    ly = label_pin[1]
    q = (round(xv + sgn * g(m), 4), ly)
    sh.wire((xv, ly), q)
    if len(pins) > 1 and ys[0] < ly < ys[-1] or (len(pins) > 1 and ly in (ys[0], ys[-1])):
        # T or 3-way meeting point
        sh.junction(xv, ly)
    if label:
        sh.glabel(label, *q, 180 if side == "left" else 0)
    if power:
        power_at(sh, power, q, side)
    return q


def flagged_power(sh, net, x, y):
    """A power symbol with a PWR_FLAG attached by a short vertical wire (supply symbols
    point up with the flag below, ground symbols point down with the flag above).
    Returns the power-symbol connection point (x, y)."""
    from p2k.sch_builder import lib
    from p2k.sexpr import find, walk
    pin_dir = int(float(find(next(walk(lib()[net], "pin")), "at")[3]))
    if pin_dir == 90:      # supply style: body up
        power_at(sh, net, (x, y), "up")
        q = down((x, y), 2)
        sh.wire((x, y), q)
        sh.pwr_flag(q[0], q[1], 180)
    else:                  # ground style: body down
        power_at(sh, net, (x, y), "down")
        q = up((x, y), 2)
        sh.wire((x, y), q)
        sh.pwr_flag(q[0], q[1], 0)
    return (x, y)


def _two_pin_rot(sym, pin, want):
    """Rotation that puts `pin` of two-pin symbol `sym` at `want` ('top' or 'left')."""
    from p2k.sch_builder import lib, _rot
    from p2k.sexpr import find, walk
    for p in walk(lib()[sym], "pin"):
        if find(p, "number")[1] == str(pin):
            at = find(p, "at")
            px, py = float(at[1]), float(at[2])
            break
    else:
        raise KeyError(f"{sym} has no pin {pin}")
    for rot in (0, 90, 180, 270):
        dx, dy = _rot(px, py, rot)      # lib coords, y up
        if want == "top" and abs(dx) < 1e-6 and dy > 0:
            return rot
        if want == "left" and abs(dy) < 1e-6 and dx < 0:
            return rot
    raise ValueError(f"{sym} pin {pin} cannot be placed at {want}")


def vert2(sh, sym, ref, x, y, value=None, top=1, **kw):
    """Two-pin symbol (R, C, D, L ...) placed vertically with pin `top` at the top.
    Returns (placed, top_point, bottom_point)."""
    p = sh.place(sym, ref, x, y, _two_pin_rot(sym, top, "top"), value=value, **kw)
    pins = sorted(p.pins.values(), key=lambda q: q[1])
    return p, pins[0], pins[-1]


def horiz2(sh, sym, ref, x, y, value=None, left=1, **kw):
    """Two-pin symbol placed horizontally with pin `left` on the left.
    Returns (placed, left_point, right_point)."""
    p = sh.place(sym, ref, x, y, _two_pin_rot(sym, left, "left"), value=value, **kw)
    pins = sorted(p.pins.values(), key=lambda q: q[0])
    return p, pins[0], pins[-1]


def terminal(sh, ref, x, y, direction="up", symbol="PIN", value="PIN", justify="legacy"):
    """Proteus-style PIN terminal (symbol `symbol` of the project library, default PIN, value
    `value`, default "PIN") with its connection point at (x, y) and the circle pointing in
    `direction`. Texts are kept horizontal beside the circle. justify (left / right terminals):
    "legacy" = the original justification (KiCad 10 draws the texts over the circle and wire),
    "away" = the opposite justification, which draws them beside the circle, away from the wire
    (the boards' former term())."""
    if justify not in ("legacy", "away"):
        raise ValueError(f"justify must be 'legacy' or 'away', not {justify!r}")
    rot = {"up": 0, "left": 90, "down": 180, "right": 270}[direction]
    j_right, j_left = ("right", "left") if justify == "legacy" else ("left", "right")
    kw = {}
    if direction == "right":     # KiCad mirrors justification for rot 270: "right" draws left-justified
        kw = dict(ref_at=(x + 5.08, y - 1.27, 90, j_right), value_at=(x + 5.08, y + 1.27, 90, j_right))
    elif direction == "left":
        kw = dict(ref_at=(x - 5.08, y - 1.27, 270, j_left), value_at=(x - 5.08, y + 1.27, 270, j_left))
    return sh.place(symbol, ref, x, y, rot, value=value, **kw)


def rail(sh, p, y=None, x=None, junction=True):
    """Wire from point p straight to a rail: down / up to the horizontal rail at `y`, or left /
    right to the vertical rail at `x` (give one), with a junction on the rail (junction=False:
    none, e.g. at the rail's end). Returns the rail point."""
    if (y is None) == (x is None):
        raise ValueError("rail: give exactly one of y (horizontal rail) or x (vertical rail)")
    q = (p[0], y) if y is not None else (x, p[1])
    sh.wire(p, q)
    if junction:
        sh.junction(*q)
    return q
