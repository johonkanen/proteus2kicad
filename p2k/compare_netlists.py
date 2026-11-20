#!/usr/bin/env python3
"""Compare the KiCad schematic netlist with the Proteus IPC-D-356A netlist.

Nets are compared by pad-set equivalence (names are only hints; Proteus auto-names
many nets #00xxx). Only refdes present in the KiCad schematic take part in
the pin comparison, so the check is meaningful while sheets are still missing.

  p2k compare_netlists                    # export kicad/<PROJECT>.kicad_sch and compare
  p2k compare_netlists --sheet 3          # missing-part report limited to PDF page 3
  p2k compare_netlists --netlist x.xml    # use an existing kicadxml netlist
  p2k compare_netlists --allow-missing    # missing parts do not fail the check

The schematic is --schematic, else $P2K_SCHEMATIC, else kicad/<PROJECT>.kicad_sch.

Errors (non-zero exit): pins on the wrong net (merged or split nets; a split is only a
warning while the IPC net still has pins on parts that are not drawn yet), pins
connected in KiCad that are N/C in Proteus (and vice versa), missing/extra pins,
extra parts, and missing parts unless --allow-missing.
Parts whose symbol is excluded from the board (e.g. an off-board transformer) are ignored.

Multi-unit parts drawn on several sheets: with a `units` column in lib/part_map.csv
("A:2 B:5 ...", unit letter : PDF page), the pins of a unit that is not drawn are pending
(warnings) when its page is outside --sheet or with --allow-missing, as missing parts are.
Strict checks (STRICT_CHECKS): `nc_joined` - an unconnected-(...) net with more than one pin
fails (no-connect pins wired together pass otherwise: each is counted as a lone N/C pin);
`net_names` - a "name hint" (a named KiCad net equal to an IPC net of another name) fails.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

from . import board
from .check_symbols import library_symbols, read_part_map
from .sexpr import find, find_all, load

NC, NONET = "N/C", "(no net)"


def natkey(s):
    """Natural sort key. Every token is a (kind, number, text) tuple, so a number never meets a
    text (SEND next to SEND1 in pinkey); the order is the plain [text, int, text, ...] order
    wherever that one did not raise."""
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", str(s))]


def pinkey(rp):
    return natkey(rp[0]) + [(1, 0, "-")] + natkey(rp[1])


def fmt(pins, limit=12):
    pins = sorted(pins, key=pinkey)
    s = " ".join(f"{r}-{p}" for r, p in pins[:limit])
    return s + (f" ... (+{len(pins) - limit})" if len(pins) > limit else "")


def load_ipc():
    ipc = json.load(open(board.path("reference", "ipc356_pads.json")))
    pin_net = {}
    for p in ipc["pads"]:
        if p["kind"] != "pad":
            continue
        net = NC if p["net_raw"] == "N/C" else (p["net"] or NONET)
        pin_net[(p["refdes"], p["pin"])] = net
    return pin_net


def export_netlist(sch, out):
    cli = os.path.join(board.BIN, "kicad-cli")
    r = subprocess.run([cli, "sch", "export", "netlist", "--format", "kicadxml", "-o", out, sch],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"compare_netlists: netlist export failed:\n{r.stdout}{r.stderr}")


def load_kicad(path):
    """-> comps {ref: {'sheet', 'lib', 'excluded'}}, pin_net {(ref, pin): net}"""
    root = ET.parse(path).getroot()
    comps = {}
    for c in root.iter("comp"):
        ref = c.get("ref")
        props = {p.get("name"): p.get("value") for p in c.findall("property")}
        sp = c.find("sheetpath")
        ls = c.find("libsource")
        excluded = "exclude_from_board" in props or c.find("exclude_from_board") is not None
        comps[ref] = {"sheet": sp.get("names") if sp is not None else "?",
                      "lib": f"{ls.get('lib')}:{ls.get('part')}" if ls is not None else "?",
                      "excluded": excluded}
    pin_net = {}
    for n in root.iter("net"):
        name = n.get("name")
        for node in n.findall("node"):
            pin_net[(node.get("ref"), node.get("pin"))] = name
    return comps, pin_net


def unconnected(name):
    return name.startswith("unconnected-(")


def short_name(name):
    return name.rsplit("/", 1)[-1] if name else name


def unit_pages(part_map):
    """{(refdes, pin): (unit letter, page)} from the optional part-map column `units`
    ("A:2 B:5 ..."), the units of the symbol's sub-symbols (NAME_<unit>_<style>, unit 1 = A;
    pins of unit 0 belong to every unit and get no page). {} without the column."""
    rows = [r for r in part_map if (r.get("units") or "").strip()]
    if not rows:
        return {}
    lib = load(board.path("lib", f"{board.config().PROJECT}.kicad_sym"))
    syms = {s[1]: s for s in find_all(lib, "symbol")}
    out = {}
    for row in rows:
        pages = {}
        for tok in row["units"].split():
            u, _, pg = tok.partition(":")
            pages[u] = int(pg)
        name = row["lib_id"].partition(":")[2]
        sym = syms.get(name)
        if sym is None:
            continue
        for sub in find_all(sym, "symbol"):
            unit = int(sub[1][len(name) + 1:].split("_")[0])
            if unit == 0:
                continue
            letter = chr(ord("A") + unit - 1)
            for pin in find_all(sub, "pin"):
                if letter in pages:
                    out[(row["refdes"], find(pin, "number")[1])] = (letter, pages[letter])
    return out


def compare(comps, k_pin_net, ipc_pin_net, part_map, sheet=None, allow_missing=False, nc_joined=False,
            net_names=False, units=None):
    """nc_joined: an unconnected-(...) net with more than one pin is an error (strict check
    nc_joined); net_names: a name hint is an error (strict check net_names); units: unit_pages()
    (pins of units on undrawn sheets are pending, like missing parts)."""
    errors, warnings, info = [], [], []
    units = units or {}
    ipc_refs = {r for r, _ in ipc_pin_net}
    excluded = {r for r, c in comps.items() if c["excluded"]}
    k_refs = {r for r in comps if not r.startswith("#") and r not in excluded}
    if nc_joined:
        joined = defaultdict(list)
        for rp, n in k_pin_net.items():
            if unconnected(n):
                joined[n].append(rp)
        for n in sorted(joined, key=natkey):
            if len(joined[n]) > 1:
                errors.append(f"KiCad net {n} joins {len(joined[n])} pins: {fmt(joined[n])} "
                              "(no-connect pins wired together; strict check nc_joined)")
    k_pin_net = {rp: n for rp, n in k_pin_net.items() if rp[0] in k_refs}
    sheet_of = {row["refdes"]: int(row["sheet"]) for row in part_map}

    # parts
    present = ipc_refs & k_refs
    scope = {r for r in ipc_refs if sheet is None or sheet_of.get(r) == sheet}
    missing = sorted(scope - k_refs, key=natkey)
    extra = sorted(k_refs - ipc_refs, key=natkey)
    if missing:
        (warnings if allow_missing else errors).append(
            f"missing parts ({len(missing)}): {' '.join(missing)}")
    for r in extra:
        errors.append(f"extra part {r} ({comps[r]['lib']}, sheet {comps[r]['sheet']}) not in IPC")

    # pins of present parts
    ipc_pins = {rp for rp in ipc_pin_net if rp[0] in present}
    k_pins = {rp for rp in k_pin_net if rp[0] in present}
    for rp in sorted(ipc_pins - k_pins, key=pinkey):
        if ipc_pin_net[rp] in (NC, NONET):
            info.append(f"pin {rp[0]}-{rp[1]} ({ipc_pin_net[rp]} in IPC) not in KiCad netlist (unplaced unit?)")
        elif rp in units and (allow_missing or (sheet is not None and units[rp][1] != sheet)):
            warnings.append(f"pending pin {rp[0]}-{rp[1]} (IPC net {ipc_pin_net[rp]}): unit {units[rp][0]} "
                            f"on sheet {units[rp][1]} not drawn")
        else:
            errors.append(f"missing pin {rp[0]}-{rp[1]} (IPC net {ipc_pin_net[rp]}; unit not placed?)")
    for rp in sorted(k_pins - ipc_pins, key=pinkey):
        errors.append(f"extra pin {rp[0]}-{rp[1]} (KiCad net {k_pin_net[rp]}) not in IPC")
    common = ipc_pins & k_pins

    # IPC nets as groups: N/C and no-net pins are singletons
    def ipc_group(rp):
        n = ipc_pin_net[rp]
        return (n, rp) if n in (NC, NONET) else (n, None)

    def k_group(rp):
        n = k_pin_net[rp]
        return (n, rp) if unconnected(n) else (n, None)

    ipc_size = Counter(ipc_group(rp) for rp in ipc_pin_net)
    by_k = defaultdict(set)
    by_ipc = defaultdict(set)
    for rp in common:
        by_k[k_group(rp)].add(rp)
        by_ipc[ipc_group(rp)].add(rp)

    wrong = set()
    # merged: one KiCad net holds pins of several IPC nets
    for kg, pins in sorted(by_k.items(), key=lambda kv: natkey(kv[0][0])):
        groups = defaultdict(set)
        for rp in pins:
            groups[ipc_group(rp)].add(rp)
        if len(groups) < 2:
            continue
        ranked = sorted(groups.items(), key=lambda g: (-len(g[1]), natkey(g[0][0])))
        parts = []
        for (iname, _), gp in ranked:
            parts.append(f"[{iname}: {fmt(gp)}]")
            if iname in (NC, NONET):
                errors.append(f"N/C mismatch: {fmt(gp)} is {iname} in IPC but on KiCad net {kg[0]}")
        errors.append(f"KiCad net {kg[0]} merges IPC nets " + " ".join(parts))
        for _, gp in ranked[1:]:
            wrong |= gp
    # split: one IPC net spread over several KiCad nets
    for ig, pins in sorted(by_ipc.items(), key=lambda kv: natkey(kv[0][0])):
        groups = defaultdict(set)
        for rp in pins:
            groups[k_group(rp)].add(rp)
        if len(groups) < 2:
            continue
        ranked = sorted(groups.items(), key=lambda g: (-len(g[1]), natkey(g[0][0])))
        parts = [f"[{kn}: {fmt(gp)}]" for (kn, _), gp in ranked]
        # A split may still be joined by a sheet that is not drawn yet (e.g. G2_SOURCE_LO
        # meets PGND on sheet 1): a warning while the IPC net has pins on undrawn parts.
        pending = sorted({sheet_of.get(rp[0], 0) for rp in ipc_pin_net
                          if ipc_group(rp) == ig and rp[0] not in present}
                         | {units[rp][1] for rp in ipc_pin_net
                            if ipc_group(rp) == ig and rp in units and rp[0] in present and rp not in k_pins})
        if pending:
            warnings.append(f"IPC net {ig[0]} split (pending sheets {' '.join(map(str, pending))}) "
                            "over KiCad nets " + " ".join(parts))
        else:
            errors.append(f"IPC net {ig[0]} split over KiCad nets " + " ".join(parts))
            for _, gp in ranked[1:]:
                wrong |= gp
    # connected in Proteus, explicitly unconnected in KiCad
    for rp in sorted(common, key=pinkey):
        ig = ipc_group(rp)
        if unconnected(k_pin_net[rp]) and ig[1] is None:
            if ipc_size[ig] > 1:
                if len(by_ipc[ig]) == 1:
                    errors.append(f"N/C mismatch: {rp[0]}-{rp[1]} unconnected in KiCad, IPC net {ig[0]} "
                                  f"({ipc_size[ig]} pins: {fmt([p for p in ipc_pin_net if ipc_pin_net[p] == ig[0]])})")
            else:
                info.append(f"{rp[0]}-{rp[1]}: single-pin IPC net {ig[0]} is unconnected in KiCad")

    # name hints for nets that match one-to-one
    hints = 0
    for ig, pins in by_ipc.items():
        kgs = {k_group(rp) for rp in pins}
        if len(kgs) != 1 or ig[1] is not None:
            continue
        kn = next(iter(kgs))[0]
        if len(by_k[next(iter(kgs))]) != len(pins):
            continue
        if not ig[0].startswith("#") and not kn.startswith(("Net-(", "unconnected-(")) \
                and short_name(kn).upper() != ig[0].upper():
            hints += 1
            if net_names:
                errors.append(f"net name: KiCad net {kn} = IPC net {ig[0]} ({fmt(pins, 4)}; strict check net_names)")
            else:
                warnings.append(f"name hint: KiCad net {kn} = IPC net {ig[0]} ({fmt(pins, 4)})")

    complete = sum(1 for ig, pins in by_ipc.items() if len(pins) == ipc_size[ig])
    nc_pins = sum(1 for ig in by_ipc if ig[1] is not None)   # N/C and no-net pins: singleton groups
    stats = {"ipc_parts": len(ipc_refs), "present": len(present), "missing_in_scope": len(missing),
             "extra": len(extra), "pins_compared": len(common), "wrong_pins": len(wrong),
             "ipc_nets_touched": len(by_ipc), "ipc_nets_complete": complete, "nc_pins": nc_pins}
    return errors, warnings, info, stats


def sheet_summary(k_refs, part_map):
    off_board = {n for n, s in library_symbols().items() if not s["on_board"]}
    per = defaultdict(lambda: [0, 0])
    for row in part_map:
        if row["lib_id"].partition(":")[2] in off_board:
            continue
        s = int(row["sheet"])
        per[s][1] += 1
        per[s][0] += row["refdes"] in k_refs
    return " ".join(f"{s}:{d}/{t}" for s, (d, t) in sorted(per.items()))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    env = os.environ.get("P2K_SCHEMATIC")   # as the PCB tools (p2k.pcb)
    ap.add_argument("--schematic", default=os.path.abspath(env) if env else
                    board.path("kicad", f"{board.config().PROJECT}.kicad_sch"))
    ap.add_argument("--netlist", help="existing kicadxml netlist (skip export)")
    ap.add_argument("--sheet", type=int, help="limit the missing-part report to this PDF page")
    ap.add_argument("--allow-missing", action="store_true", help="missing parts are warnings, not errors")
    ap.add_argument("-v", "--verbose", action="store_true", help="also print info lines and all missing parts")
    args = ap.parse_args()

    if args.netlist:
        comps, k_pin_net = load_kicad(args.netlist)
    else:
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "netlist.xml")
            export_netlist(args.schematic, out)
            comps, k_pin_net = load_kicad(out)
    part_map = read_part_map()
    errors, warnings, info, st = compare(comps, k_pin_net, load_ipc(), part_map, args.sheet, args.allow_missing,
                                         nc_joined=board.strict("nc_joined"), net_names=board.strict("net_names"),
                                         units=unit_pages(part_map))

    k_refs = {r for r, c in comps.items() if not r.startswith("#") and not c["excluded"]}
    print(f"compare: parts {st['present']}/{st['ipc_parts']} present, {st['missing_in_scope']} missing"
          f"{f' on sheet {args.sheet}' if args.sheet else ''}, {st['extra']} extra; "
          f"pins compared {st['pins_compared']}, wrong-net pins {st['wrong_pins']}; "
          f"IPC nets touched {st['ipc_nets_touched'] - st['nc_pins']}, "
          f"complete {st['ipc_nets_complete'] - st['nc_pins']}"
          + (f" (+ {st['nc_pins']} N/C / no-net pins)" if st["nc_pins"] else ""))
    print(f"compare: per sheet (drawn/total) {sheet_summary(k_refs, part_map)}")
    for w in warnings:
        if w.startswith("missing parts") and not args.verbose and not args.sheet:
            print("WARNING:", w[:w.index(":") + 1], "(see --verbose or --sheet N)")
        else:
            print("WARNING:", w)
    if args.verbose:
        for i in info:
            print("info:", i)
    for e in errors:
        print("ERROR:", e)
    print("compare: OK" if not errors else f"compare: {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
