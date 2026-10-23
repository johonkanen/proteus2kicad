#!/usr/bin/env python3
"""Phase 4: IPC-D-356 netlist exported from the KiCad board vs the Proteus IPC-D-356A file
(stdlib only, system python3; kicad-cli for the export).

Exports the board with `kicad-cli pcb export ipcd356`, parses it (KiCad writes units CUST 0
= 0.0001 inch, net names of more than 14 characters as NNAME aliases, refdes truncated to 6
characters like Proteus, vias with refdes VIA, plated mounting holes as N/C pads, unplated
holes as 367 records) and matches every Proteus record (p2k ipc356, through the configured
record filter) to a KiCad record:
  pads       same truncated refdes and pin id, nearest position within TOL (two pads with one
             number, or two parts sharing a truncated refdes, are told apart by position);
  vias       nearest KiCad via within TOL; Proteus via records stacked on one spot share one
             KiCad via (counted, must equal STACKED_VIAS);
  holes      nearest KiCad hole record (unplated 367 record, or an N/C pad of a board-only
             footprint H<n>) within TOL;
  free pads  nearest KiCad pad of a board-only footprint FREE_PAD_PREFIX<n> within TOL.
Checked per matched pair: position (TOL), side (access code: KiCad writes the bottom as the
copper layer count, Proteus as 16), THT vs SMD record type, plating, drill (KiCad may be
larger; smaller only for the pairs listed in DRILL_SMALLER), and the net: Proteus nets map
one-to-one onto KiCad nets through the component pads (pad-set equivalence, names differ for
the #00xxx and sheet-local nets); a Proteus N/C pad (or one without a net) must be on a KiCad
unconnected-(...) net or N/C; holes have no net; free pads and vias are on the mapped net.
Unmatched records on either side are errors. Pad sizes are only summarised.

Config keys: PROJECT, COPPER (layer count), DRILL_SMALLER, STACKED_VIAS, FREE_PAD_PREFIX.
Board wrapper hooks: main(argv, added={refdes: {"pins": {pin: net}}}, extra_vias=[(x, y,
net), ...]) for parts and vias of a later design revision that have no Proteus record; the
IPC records go through p2k.pcb.configure(records=...).

Usage: tools/p2k/bin/p2k ipc356_diff [--kicad FILE] [-v]
"""
import argparse
import math
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter

from . import board as _board
from . import pcb as bc

TOL = bc.TOL        # mm, position
DRILL_TOL = 0.005   # mm (KiCad writes 0.0001 inch = 0.00254 mm)
SAME_SPOT = 0.005   # mm, stacked duplicate records: identical position up to KiCad's 0.00254 mm quantum

# KiCad leaves the 'M' (mid-point) column blank and pads with spaces, Proteus writes M + D/blanks
DATA_RE = re.compile(
    r"[M\s]*(?:D(?P<drill>\d{4})(?P<plated>[PU]))?\s*"
    r"A(?P<access>\d\d)"
    r"X(?P<x>[+-]\d{6})Y(?P<y>[+-]\d{6})"
    r"X(?P<w>\d{4})(?:Y(?P<h>\d{4}))?(?:R(?P<rot>\d{3}))?"
    r"\s*S(?P<mask>\d)\s*$")


def export(path):
    cli = os.path.join(_board.BIN, "kicad-cli")
    r = subprocess.run([cli, "pcb", "export", "ipcd356", "-o", path, bc.board_file()],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(path):
        sys.exit(f"ipc356_diff: kicad-cli export failed:\n{r.stdout}{r.stderr}")


def parse_kicad(path, copper_layers, free_pad_prefix):
    """KiCad IPC-D-356 records -> list of dicts (mm, Y up, like ipc356.parse)."""
    scale = None
    names = {}
    recs = []
    free_re = re.compile(re.escape(free_pad_prefix) + r"\d+") if free_pad_prefix else None
    with open(path) as f:
        lines = f.read().splitlines()
    for lineno, line in enumerate(lines, 1):
        if line.startswith("P  UNITS"):
            u = line.split()[2:]
            scale = 0.00254 if u[0] == "CUST" and u[1] == "0" else 0.001 if u[0] == "SI" else None  # 0.0001 inch
            if scale is None:
                raise ValueError(f"{path}: units {line!r}")
        elif line.startswith("P  NNAME"):
            nid, name = line[3:].split(None, 1)
            names[nid] = name.strip()
        op = line[:3]
        if op not in ("317", "327", "367"):
            continue
        net_field = line[3:17].strip()
        refdes = line[20:26].strip()
        pin = line[27:31].strip()
        m = DATA_RE.match(line[31:])
        if not m:
            raise ValueError(f"{path}: line {lineno}: cannot parse {line!r}")
        net = names.get("NNAME" + net_field, net_field)
        acc = m["access"]
        side = "both" if acc == "00" else "top" if acc == "01" else "bottom" if int(acc) == copper_layers else acc
        w = int(m["w"]) * scale
        h = int(m["h"]) * scale if m["h"] else 0.0
        if refdes == "VIA":
            kind = "via"
        elif op == "367" or (net == "N/C" and re.fullmatch(r"H\d+", refdes)):
            kind = "hole"      # unplated tooling hole, or a plated board-only mounting hole H<n>
        elif free_re and free_re.fullmatch(refdes):
            kind = "free_pad"
        else:
            kind = "pad"
        recs.append(dict(
            net=net, refdes=refdes, pin=pin, x_mm=int(m["x"]) * scale, y_mm=int(m["y"]) * scale,
            side=side, pad_w=w, pad_h=h or w, is_tht=op != "327",   # KiCad writes Y0000 for round pads
            drill=int(m["drill"]) * scale if m["drill"] else 0.0,
            plated=m["plated"] != "U" if m["drill"] else False,
            kind=kind, line=lineno))
    return recs


def kicad_unconnected(net):
    return net == "N/C" or net.startswith("unconnected-(")


def main(argv=None, added=None, extra_vias=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--kicad", help="KiCad IPC-D-356 file (default: export it)")
    ap.add_argument("-v", action="store_true")
    args = ap.parse_args(argv)
    cfg = bc.cfg()
    added = added or {}
    extra_vias = list(extra_vias or [])
    stacked_expected = getattr(cfg, "STACKED_VIAS", 0)
    copper_layers = len(bc.copper())
    free_prefix = getattr(cfg, "FREE_PAD_PREFIX", "FP") if getattr(cfg, "FREE_PAD_FOOTPRINT", None) else None

    prot = bc.ipc_records()
    if args.kicad:
        kic = parse_kicad(args.kicad, copper_layers, free_prefix)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, f"{cfg.PROJECT}.d356")
            export(p)
            kic = parse_kicad(p, copper_layers, free_prefix)

    errors = []
    # candidates per key
    by_key = {}
    for r in kic:
        key = (r["kind"], r["refdes"] if r["kind"] == "pad" else "", r["pin"] if r["kind"] == "pad" else "")
        by_key.setdefault(key, []).append(r)
    used = set()
    pairs = []
    stacked = []
    worst = 0.0

    def dist(p, r):
        return math.hypot(r["x_mm"] - p["x_mm"], r["y_mm"] - p["y_mm"])

    for p in prot:
        key = (p["kind"], p["refdes"][:6] if p["kind"] == "pad" else "", p["pin"] if p["kind"] == "pad" else "")
        allc = by_key.get(key, [])
        cands = [r for r in allc if id(r) not in used]
        what = f"{p['refdes']}-{p['pin']}" if p["kind"] == "pad" else f"at ({p['x_mm']}, {p['y_mm']})"
        if not cands:
            if p["kind"] == "via" and allc:
                r = min(allc, key=lambda r: dist(p, r))
                if dist(p, r) < SAME_SPOT:
                    stacked.append((p, r))
                    continue
            errors.append(f"Proteus {p['kind']} {what}: no KiCad record")
            continue
        r = min(cands, key=lambda r: dist(p, r))
        d = dist(p, r)
        if d > TOL:
            if p["kind"] == "via":
                r2 = min(allc, key=lambda r: dist(p, r))
                if dist(p, r2) < SAME_SPOT:
                    stacked.append((p, r2))
                    continue
            errors.append(f"Proteus {p['kind']} {what}: nearest KiCad record {d:.3f} mm away")
            continue
        used.add(id(r))
        worst = max(worst, d)
        pairs.append((p, r))
    if len(stacked) != stacked_expected:
        errors.append(f"{len(stacked)} stacked duplicate Proteus via records share a KiCad via, STACKED_VIAS = {stacked_expected}")
    addrecs = []
    for r in kic:
        if id(r) not in used:
            if r["kind"] == "pad" and r["refdes"] in added and r["pin"] in added[r["refdes"]]["pins"]:
                addrecs.append((r, added[r["refdes"]]["pins"][r["pin"]]))
                continue
            hit = [v for v in extra_vias if r["kind"] == "via" and math.hypot(v[0] - r["x_mm"], v[1] - r["y_mm"]) < TOL]
            if hit:
                addrecs.append((r, hit[0][2]))
                continue
            errors.append(f"KiCad {r['kind']} {r['refdes']}-{r['pin']} at ({r['x_mm']:.3f}, {r['y_mm']:.3f}) net {r['net']}: "
                          f"no Proteus record (line {r['line']})")

    # net mapping by pad-set equivalence (component pads with a real net on both sides)
    fwd, back = {}, {}
    for p, r in pairs:
        if p["kind"] == "pad" and p["net"] and p["net_raw"] != "N/C" and not kicad_unconnected(r["net"]):
            fwd.setdefault(p["net"], set()).add(r["net"])
            back.setdefault(r["net"], set()).add(p["net"])
    for n, ks in sorted(fwd.items()):
        if len(ks) > 1:
            errors.append(f"Proteus net {n} spans KiCad nets {sorted(ks)}")
    for k, ns in sorted(back.items()):
        if len(ns) > 1:
            errors.append(f"KiCad net {k} spans Proteus nets {sorted(ns)}")
    mapping = {n: next(iter(ks)) for n, ks in fwd.items() if len(ks) == 1}
    for r, want in addrecs:   # added pads / vias: on the listed net
        if mapping.get(want, want) != r["net"]:
            errors.append(f"added {r['kind']} {r['refdes']}-{r['pin']} at ({r['x_mm']:.3f}, {r['y_mm']:.3f}): "
                          f"KiCad net {r['net']}, listed {want} -> {mapping.get(want)}")
    naddp = sum(1 for r, _ in addrecs if r["kind"] == "pad")
    if naddp != sum(len(p["pins"]) for p in added.values()) or len(addrecs) - naddp != len(extra_vias):
        errors.append(f"added records: {naddp} pads, {len(addrecs) - naddp} vias found; "
                      f"listed {sum(len(p['pins']) for p in added.values())} pads, {len(extra_vias)} vias")

    stats = Counter()
    size_diff = Counter()
    drill_larger = Counter()
    drill_smaller_seen = Counter()
    for p, r in pairs + stacked:
        what = f"{p['kind']} {p['refdes']}-{p['pin']}" if p["kind"] == "pad" else f"{p['kind']} at ({p['x_mm']}, {p['y_mm']})"
        stats[p["kind"]] += 1
        if p["is_tht"] != r["is_tht"]:
            errors.append(f"{what}: Proteus {'THT' if p['is_tht'] else 'SMD'}, KiCad {'THT' if r['is_tht'] else 'SMD'}")
        if p["side"] != r["side"]:
            errors.append(f"{what}: side Proteus {p['side']}, KiCad {r['side']}")
        if p["is_tht"] and r["is_tht"] and p["plated"] != r["plated"]:
            errors.append(f"{what}: plated Proteus {p['plated']}, KiCad {r['plated']}")
        if r["drill"] < p["drill"] - DRILL_TOL:
            if bc.drill_smaller_accepted(p["drill"], r["drill"], DRILL_TOL):
                drill_smaller_seen[(p["drill"], round(r["drill"], 3))] += 1
            else:
                errors.append(f"{what}: drill Proteus {p['drill']}, KiCad {r['drill']:.4f} is smaller")
        elif r["drill"] > p["drill"] + DRILL_TOL:
            drill_larger[(p["drill"], round(r["drill"], 3))] += 1
        # nets
        p_nc = p["net_raw"] == "N/C" or not p["net"]
        if p["kind"] == "hole":
            if not kicad_unconnected(r["net"]):
                errors.append(f"{what}: mounting hole on KiCad net {r['net']}")
        elif p_nc:
            if not kicad_unconnected(r["net"]):
                errors.append(f"{what}: N/C in Proteus, KiCad net {r['net']}")
            stats["N/C pads"] += 1
        elif kicad_unconnected(r["net"]):
            errors.append(f"{what}: Proteus net {p['net']}, KiCad unconnected ({r['net']})")
        elif mapping.get(p["net"]) != r["net"]:
            errors.append(f"{what}: Proteus net {p['net']} -> {mapping.get(p['net'])}, KiCad net {r['net']}")
        if p["kind"] == "pad":
            dw = abs(p["pad_w"] - r["pad_w"]) > 0.01 or abs(p["pad_h"] - r["pad_h"]) > 0.01
            dr = abs(p["pad_w"] - r["pad_h"]) > 0.01 or abs(p["pad_h"] - r["pad_w"]) > 0.01   # rotated 90
            if dw and dr:
                size_diff[re.sub(r"\d+$", "", p["refdes"]) or p["refdes"]] += 1

    extra = f", {stats['free_pad']} free pads" if stats["free_pad"] else ""
    st = f" ({len(stacked)} stacked duplicate via records on matched vias)" if stacked else ""
    print(f"ipc356_diff: Proteus {len(prot)} records, KiCad {len(kic)}; matched {len(pairs) + len(stacked)} "
          f"({stats['pad']} pads incl. {stats['N/C pads']} N/C, {stats['via']} vias{st}, {stats['hole']} holes{extra}), "
          f"worst position offset {worst:.3f} mm (limit {TOL})")
    print(f"ipc356_diff: {len(mapping)} Proteus nets mapped 1:1 onto KiCad nets, "
          f"{len(errors)} mismatches")
    if added or extra_vias:
        print(f"ipc356_diff: {len(addrecs)} KiCad records of added parts / vias without a Proteus record, nets checked")
    if drill_larger:
        print("ipc356_diff: KiCad drill larger (footprint kept): " +
              ", ".join(f"{n}x {a}->{b}" for (a, b), n in sorted(drill_larger.items())))
    if drill_smaller_seen:
        print("ipc356_diff: KiCad drill smaller, accepted (DRILL_SMALLER): " +
              ", ".join(f"{n}x {a}->{b}" for (a, b), n in sorted(drill_smaller_seen.items())))
    if size_diff:
        print("ipc356_diff: pads with another size in KiCad (info): " +
              ", ".join(f"{k} {n}" for k, n in sorted(size_diff.items())))
    for e in errors[:50]:
        print("ipc356_diff: ERROR " + e)
    if len(errors) > 50:
        print(f"ipc356_diff: ... {len(errors) - 50} more")
    print("ipc356_diff: " + ("OK" if not errors else "FAILED"))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
