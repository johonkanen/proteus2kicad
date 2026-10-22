"""Helpers for a board's symbol generator (tools/gen_symbols.py in the board repo).

The board script defines `build()` -> list of symbol nodes and calls `run(build)`:

    from p2k.symgen import *          # from_std, box, header, power, set_flag, PI, PO, ...
    def build():
        return [from_std("Device", "R", "R", "R", "R"), box("LM317", "U", [...], [...]), ...]
    if __name__ == "__main__":
        sys.exit(run(build))

Pin numbers must equal the Proteus/IPC pin ids. `from_std` copies a KiCad 10 standard symbol
(flattened, pins renumbered with `pins={old: new}`), `box` draws an IC box with the Proteus
pin names, `header` lays out a dual-row connector, `power` makes a power symbol named after
an IPC net. Writes lib/<PROJECT>.kicad_sym; `--check` fails if the file is out of date.
Needs the KiCad AppImage mount for the standard libraries.
"""
import copy
import os
import subprocess
import sys

from . import board
from .sexpr import Sym, dump, find, find_all, load, parse, save, walk  # noqa: F401


def std_lib_dir():
    mount = subprocess.run(["bash", "-c", f"source {board.BIN}/kicad_env.sh >/dev/null && echo $KICAD_MOUNT"],
                           capture_output=True, text=True, check=True).stdout.strip()
    return os.path.join(mount, "share", "kicad", "symbols")


STD = None


def std_symbol(lib, name):
    """Return the flattened std symbol node `lib:name` (extends resolved)."""
    path = os.path.join(STD, f"{lib}.kicad_symdir", f"{name}.kicad_sym")
    sym = copy.deepcopy(find(load(path), "symbol"))
    ext = find(sym, "extends")
    if ext:
        parent = std_symbol(lib, ext[1])
        props = {p[1]: p for p in find_all(sym, "property")}
        for i, c in enumerate(parent):
            if isinstance(c, list) and c[0] == "property" and c[1] in props:
                parent[i] = props[c[1]]
        sym = parent
    return sym


def rename(sym, new):
    old = sym[1]
    sym[1] = new
    for sub in find_all(sym, "symbol"):
        sub[1] = new + sub[1][len(old):]
    return sym


def set_prop(sym, key, value, hide=None):
    for p in find_all(sym, "property"):
        if p[1] == key:
            p[2] = value
            if hide is not None:
                p[:] = [c for c in p if not (isinstance(c, list) and c[0] == "hide")]
                if hide:
                    p.insert(4, [Sym("hide"), Sym("yes")])
            return
    node = [Sym("property"), key, value, [Sym("at"), 0, 0, 0]]
    if hide is not False:
        node.append([Sym("hide"), Sym("yes")])
    node.append([Sym("effects"), [Sym("font"), [Sym("size"), 1.27, 1.27]]])
    sym.insert(len([c for c in sym if not isinstance(c, list) or c[0] != "symbol"]), node)


def set_flag(sym, key, value):
    node = find(sym, key)
    if node:
        node[1] = Sym("yes" if value else "no")


def remap_pins(sym, mapping, names=None):
    seen = set()
    for pin in walk(sym, "pin"):
        num = find(pin, "number")
        if num[1] in mapping:
            new = mapping[num[1]]
            num[1] = new
            seen.add(new)
            if names and new in names:
                find(pin, "name")[1] = names[new]
    missing = set(mapping.values()) - seen
    assert not missing, (sym[1], missing)
    return sym


def from_std(lib, name, new, ref, value=None, desc=None, pins=None, names=None):
    sym = rename(std_symbol(lib, name), new)
    if pins:
        remap_pins(sym, pins, names)
    set_prop(sym, "Reference", ref)
    set_prop(sym, "Value", value or new)
    set_prop(sym, "Footprint", "")
    set_prop(sym, "Datasheet", "")
    if desc:
        set_prop(sym, "Description", desc)
    # drop sim models: pin numbers no longer match them
    sym[:] = [c for c in sym if not (isinstance(c, list) and c[0] == "property" and str(c[1]).startswith("Sim."))]
    return sym


# ---------------------------------------------------------------- box symbols

def _font():
    return [Sym("effects"), [Sym("font"), [Sym("size"), 1.27, 1.27]]]


def _prop(key, value, x, y, hide=False, justify=None):
    p = [Sym("property"), key, value, [Sym("at"), x, y, 0]]
    if hide:
        p.append([Sym("hide"), Sym("yes")])
    eff = _font()
    if justify:
        eff.append([Sym("justify"), Sym(justify)])
    p.append(eff)
    return p


def _pin(num, name, etype, x, y, rot, length=2.54, hidden_name=False):
    return [Sym("pin"), Sym(etype), Sym("line"), [Sym("at"), x, y, rot], [Sym("length"), length],
            [Sym("name"), name, _font()], [Sym("number"), str(num), _font()]]


def box(name, ref, left, right, top=(), bottom=(), value=None, desc="", show_pin_names=True,
        min_width=7.62, on_board=True):
    """left/right/top/bottom: lists of (number, name, electrical_type); None = gap."""
    g = 2.54
    def maxlen(plist):
        return max([len(p[1].replace("~{", "").replace("}", "")) for p in plist if p] + [0])
    ml, mr = (maxlen(left), maxlen(right)) if show_pin_names else (0, 0)
    width = max(min_width, (ml + mr) * 1.2 + 4.5, (max(len(top), len(bottom)) + 1) * 2 * g)
    hw = -(-width / 2 // g) * g
    rows = max(len(left), len(right), 1)
    ytop = -(-(rows - 1) // 2) * g          # pins on the 2.54 grid, first row at ytop
    pad = lambda plist: -(-(maxlen(plist) * 1.2 + 1.5) // g) * g if plist else 0
    top_y, bot_y = ytop + g + pad(top), ytop - rows * g - pad(bottom)
    pins = []
    for side, plist in (("L", left), ("R", right)):
        for i, p in enumerate(plist):
            if not p:
                continue
            x = -hw - g if side == "L" else hw + g
            pins.append(_pin(p[0], p[1], p[2], x, ytop - i * g, 0 if side == "L" else 180))
    for side, plist in (("T", top), ("B", bottom)):
        n = len(plist)
        for i, p in enumerate(plist):
            if not p:
                continue
            x = (i - (n - 1) / 2) * g * 2
            x = round(x / g) * g
            y = top_y + g if side == "T" else bot_y - g
            pins.append(_pin(p[0], p[1], p[2], x, y, 270 if side == "T" else 90))
    sym = [Sym("symbol"), name]
    if not show_pin_names:
        sym.append([Sym("pin_names"), [Sym("offset"), 1.016], [Sym("hide"), Sym("yes")]])
    else:
        sym.append([Sym("pin_names"), [Sym("offset"), 1.016]])
    sym += [[Sym("exclude_from_sim"), Sym("no")], [Sym("in_bom"), Sym("yes")],
            [Sym("on_board"), Sym("yes" if on_board else "no")],
            _prop("Reference", ref, -hw, top_y + 1.27 + (g if top else 0), justify="left"),
            _prop("Value", value or name, -hw, bot_y - 1.27 - (g if bottom else 0), justify="left"),
            _prop("Footprint", "", 0, 0, hide=True),
            _prop("Datasheet", "", 0, 0, hide=True),
            _prop("Description", desc, 0, 0, hide=True),
            [Sym("symbol"), f"{name}_0_1",
             [Sym("rectangle"), [Sym("start"), -hw, top_y], [Sym("end"), hw, bot_y],
              [Sym("stroke"), [Sym("width"), 0.254], [Sym("type"), Sym("default")]],
              [Sym("fill"), [Sym("type"), Sym("background")]]]],
            [Sym("symbol"), f"{name}_1_1", *pins],
            [Sym("embedded_fonts"), Sym("no")]]
    return sym


def header(pins):
    """Dual-row connector box: odd pins left, even pins right, absent pins leave gaps."""
    nums = sorted(int(p) for p in pins)
    lo, hi = nums[0] - (nums[0] % 2 == 0), nums[-1] + (nums[-1] % 2)
    left, right = [], []
    for odd in range(lo, hi, 2):
        left.append((odd, str(odd), "passive") if odd in nums else None)
        right.append((odd + 1, str(odd + 1), "passive") if odd + 1 in nums else None)
    return left, right


# ---------------------------------------------------------------- power symbols

def power(net, ground):
    sym = rename(std_symbol("power", "GND" if ground else "+3V3"), net)
    set_prop(sym, "Value", net)
    set_prop(sym, "Description", f'Power symbol for net "{net}" (IPC net name)')
    return sym


PI, PO, IN, OUT, PAS = "power_in", "power_out", "input", "output", "passive"


def run(build, argv=None):
    """Write lib/<PROJECT>.kicad_sym from build(), or check it with --check."""
    global STD
    argv = sys.argv[1:] if argv is None else argv
    project = board.config().PROJECT
    lib_path = board.path("lib", f"{project}.kicad_sym")
    rel = os.path.relpath(lib_path, board.root())
    STD = std_lib_dir()
    lib = [Sym("kicad_symbol_lib"), [Sym("version"), Sym("20251024")], [Sym("generator"), f"{project}_gen_symbols"],
           [Sym("generator_version"), "10.0"], *build()]
    if "--check" in argv:
        current = load(lib_path) if os.path.exists(lib_path) else None
        if current != parse(dump(lib)):
            print(f"gen_symbols: {rel} is out of date, run tools/gen_symbols.py")
            return 1
        print(f"gen_symbols: {rel} up to date ({len(lib) - 4} symbols)")
        return 0
    save(lib_path, lib)
    print(f"wrote {rel}: {len(lib) - 4} symbols")
    return 0
