#!/usr/bin/env python3
"""Check lib/<PROJECT>.kicad_sym against the IPC netlist.

For every refdes in lib/part_map.csv the set of pin numbers of its symbol
(all units) must equal the set of IPC pin ids of that refdes. Also checks that
every IPC refdes is mapped, every mapped symbol exists, off-board symbols are
excluded from the board, and power symbol names are IPC net names.

  p2k check_symbols

lib/part_map.csv columns: refdes (KiCad refdes = `refdes` of reference/ipc356_pads.json),
sheet (Proteus PDF page), lib_id (<PROJECT>:<symbol>), value; more columns are allowed.
Lines starting with '#' are comments.
"""
import csv
import json
import sys
from collections import defaultdict

from . import board
from .sexpr import find, find_all, load, walk


def project():
    return board.config().PROJECT


def read_part_map(path=None):
    with open(path or board.path("lib", "part_map.csv"), newline="") as f:
        return list(csv.DictReader(line for line in f if not line.startswith("#")))


def library_symbols(path=None):
    """name -> {'pins': set, 'power': bool, 'on_board': bool}"""
    out = {}
    for sym in find_all(load(path or board.path("lib", f"{project()}.kicad_sym")), "symbol"):
        pins = {find(p, "number")[1] for p in walk(sym, "pin")}
        ob = find(sym, "on_board")
        out[sym[1]] = {"pins": pins, "power": find(sym, "power") is not None,
                       "on_board": not (ob and ob[1] == "no")}
    return out


def ipc_pins():
    ipc = json.load(open(board.path("reference", "ipc356_pads.json")))
    pins = defaultdict(set)
    nets = set()
    for p in ipc["pads"]:
        if p["kind"] == "pad":
            pins[p["refdes"]].add(p["pin"])
            nets.add(p["net"])
    return pins, nets


def main():
    lib = library_symbols()
    parts = read_part_map()
    ipc, nets = ipc_pins()
    errors = []
    mapped = set()
    by_symbol = defaultdict(list)
    for row in parts:
        ref, lib_id = row["refdes"], row["lib_id"]
        mapped.add(ref)
        nick, _, name = lib_id.partition(":")
        if nick != project() or name not in lib:
            errors.append(f"{ref}: symbol {lib_id} not in lib/{project()}.kicad_sym")
            continue
        sym = lib[name]
        by_symbol[name].append(ref)
        if not sym["on_board"]:
            if ref in ipc:
                errors.append(f"{ref}: {name} is excluded from board but the IPC has pads for it")
            continue
        if ref not in ipc:
            errors.append(f"{ref}: not in IPC (mark the symbol 'exclude from board' if it is off-board)")
            continue
        if sym["pins"] != ipc[ref]:
            errors.append(f"{ref} ({name}): symbol-only pins {sorted(sym['pins'] - ipc[ref])}, "
                          f"IPC-only pins {sorted(ipc[ref] - sym['pins'])}")
    for ref in sorted(set(ipc) - mapped):
        errors.append(f"{ref}: in IPC but missing from lib/part_map.csv")
    power = sorted(n for n, s in lib.items() if s["power"] and n != "PWR_FLAG")
    for n in power:
        if n not in nets:
            errors.append(f"power symbol {n} is not an IPC net name")
    unused = sorted(n for n, s in lib.items() if not s["power"] and n not in by_symbol)
    for name in sorted(by_symbol):
        print(f"  {name:20s} {len(by_symbol[name]):3d}  pins {len(lib[name]['pins']):2d}  "
              f"{' '.join(by_symbol[name][:8])}{' ...' if len(by_symbol[name]) > 8 else ''}")
    print(f"symbols: {len(by_symbol)} part types, {len(mapped)} refdes, {len(power)} power symbols"
          + (f", unused: {' '.join(unused)}" if unused else ""))
    for e in errors:
        print("ERROR:", e)
    print("symbols: OK" if not errors else f"symbols: {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
