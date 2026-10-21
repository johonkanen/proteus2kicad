#!/usr/bin/env python3
"""Parse the Proteus IPC-D-356A netlist into a pad table.

Writes (default, into reference/):
  ipc356_pads.json / ipc356_pads.csv   one row per pad/via/hole record
  pin_nets_by_component.md             per refdes: pin -> net (redraw cheat sheet)
  pins_by_net.md                       per net: list of refdes-pin

Applied fix-ups (tables in the board's p2k_config.py):
  * refdes truncated to 6 chars by Proteus are restored (TRUNCATED_REFDES: raw -> full, or
    (raw, x_um, y_um) -> full where two parts share a truncated name); TRUNCATED_NETS gives
    the expected net per restored refdes for the self-check;
  * Proteus refdes that KiCad cannot use (spaces, leading +/-) are renamed (KICAD_REFDES);
    the `refdes` column holds the KiCad name, `refdes_proteus` the full Proteus name,
    `refdes_raw` the 6-char IPC field;
  * Proteus power-net names are aliased (NET_ALIASES).

Records with a blank refdes: `via` (net, drill in VIA_DRILLS), `free_pad` (net, other drill,
e.g. wire pads) and `hole` (no net; HOLE_DRILLS / HOLES_PLATED checked).

Coordinates are kept exactly as in the IPC file: mm, Y axis pointing up
(KiCad's Y points down; flip when placing).

  p2k ipc356            regenerate the reference files
  p2k ipc356 --check    self-check counts (EXPECTED) and that reference/ is up to date

Stdlib only; runs with any python3 (no pcbnew needed).
"""

import argparse
import csv
import io
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from . import board

CFG = board.config()
ROOT = Path(board.root())
DEFAULT_IPC = ROOT / CFG.IPC
REF_DIR = ROOT / "reference"

TRUNCATED_REFDES = CFG.TRUNCATED_REFDES
TRUNCATED_NETS = getattr(CFG, "TRUNCATED_NETS", {})
KICAD_REFDES = getattr(CFG, "KICAD_REFDES", {})
NET_ALIASES = CFG.NET_ALIASES
EXPECTED = CFG.EXPECTED_IPC
VIA_DRILLS = tuple(CFG.VIA_DRILLS)
FREE_PAD_DRILLS = tuple(getattr(CFG, "FREE_PAD_DRILLS", ()))
HOLE_DRILLS = tuple(CFG.HOLE_DRILLS)
HOLES_PLATED = CFG.HOLES_PLATED
_TRUNC_RAW = {k if isinstance(k, str) else k[0] for k in TRUNCATED_REFDES}

FIELDS = ["kind", "net", "net_raw", "net_id", "refdes", "refdes_proteus", "refdes_raw", "pin",
          "x_mm", "y_mm", "side", "pad_w", "pad_h", "rot", "is_tht", "drill",
          "plated", "mask"]

# Columns after the fixed 3/14/6/1/4 header part, e.g.
#   MD0558PA00X+019720Y-064800X0812          S0
#   M      A01X+089400Y-018000X1600Y2260R090 S0
DATA_RE = re.compile(
    r"M(?:D(?P<drill>\d{4})(?P<plated>[PU])|\s{6})"
    r"A(?P<access>\d\d)"
    r"X(?P<x>[+-]\d{6})Y(?P<y>[+-]\d{6})"
    r"X(?P<w>\d{4})(?:Y(?P<h>\d{4}))?(?:R(?P<rot>\d{3}))?"
    r"\s*S(?P<mask>\d)\s*$")

SIDES = {"00": "both", "01": "top", "16": "bottom"}


def natural_key(s):
    return [(0, int(t), "") if t.isdigit() else (1, 0, t)
            for t in re.findall(r"\d+|\D+", s)]


def parse(path):
    net_names = {}
    pads = []
    trunc_used = Counter()
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        if line.startswith("P  NNAME"):
            nid, name = line[3:].split(None, 1)
            net_names[nid] = name.strip()
            continue
        op = line[:3]
        if op not in ("317", "327"):
            continue
        net_field = line[3:17].strip()
        refdes_raw = line[20:26].strip()
        pin = line[27:31].strip()
        m = DATA_RE.match(line[31:])
        if not m:
            raise ValueError(f"line {lineno}: cannot parse {line!r}")
        x_um, y_um = int(m["x"]), int(m["y"])

        if net_field == "N/C":
            net_id, net_raw = "", "N/C"
        elif net_field:
            net_id, net_raw = net_field, net_names[net_field]
        else:
            net_id, net_raw = "", ""
        net = NET_ALIASES.get(net_raw, net_raw)

        refdes = refdes_raw
        for key in ((refdes_raw, x_um, y_um), refdes_raw):
            if key in TRUNCATED_REFDES:
                refdes = TRUNCATED_REFDES[key]
                trunc_used[key] += 1
                break
        else:
            if refdes_raw in _TRUNC_RAW:
                raise ValueError(f"line {lineno}: truncated refdes {refdes_raw} at "
                                 f"({x_um}, {y_um}) not in TRUNCATED_REFDES")
        if refdes:
            kind = "pad"
        elif net_raw:
            kind = "via" if m["drill"] and int(m["drill"]) / 1000 in VIA_DRILLS else "free_pad"
        else:
            kind = "hole"
        w = int(m["w"]) / 1000
        pads.append({
            "kind": kind,
            "net": net,
            "net_raw": net_raw,
            "net_id": net_id,
            "refdes": KICAD_REFDES.get(refdes, refdes),
            "refdes_proteus": refdes,
            "refdes_raw": refdes_raw,
            "pin": pin,
            "x_mm": x_um / 1000,
            "y_mm": y_um / 1000,
            "side": SIDES.get(m["access"], m["access"]),
            "pad_w": w,
            "pad_h": int(m["h"]) / 1000 if m["h"] else w,
            "rot": int(m["rot"]) if m["rot"] else 0,
            "is_tht": op == "317",
            "drill": int(m["drill"]) / 1000 if m["drill"] else 0.0,
            "plated": m["plated"] == "P" if m["drill"] else False,
            "mask": int(m["mask"]),
        })
    unused = set(TRUNCATED_REFDES) - set(trunc_used)
    if unused:
        raise ValueError(f"TRUNCATED_REFDES entries not found in IPC: {sorted(unused)}")
    return net_names, pads


def stats(net_names, pads):
    parts = {p["refdes"] for p in pads if p["kind"] == "pad"}
    return {
        "nets": len(net_names),
        "records": len(pads),
        "refdes_raw": len({p["refdes_raw"] for p in pads}),
        "tht_records": sum(p["is_tht"] for p in pads),
        "smd_records": sum(not p["is_tht"] for p in pads),
        "smd_top": sum(not p["is_tht"] and p["side"] == "top" for p in pads),
        "smd_bottom": sum(not p["is_tht"] and p["side"] == "bottom" for p in pads),
        "nc_pads": sum(p["net_raw"] == "N/C" for p in pads),
        "vias": sum(p["kind"] == "via" for p in pads),
        "free_pads": sum(p["kind"] == "free_pad" for p in pads),
        "holes": sum(p["kind"] == "hole" for p in pads),
        "parts": len(parts),
    }


def render_csv(pads):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
    w.writeheader()
    w.writerows(pads)
    return buf.getvalue()


def render_json(net_names, pads):
    doc = {
        "source": DEFAULT_IPC.name,
        "units": "mm, IPC coordinates (Y up)",
        "net_aliases": NET_ALIASES,
        "truncated_refdes": {k if isinstance(k, str) else f"{k[0]}@{k[1]},{k[2]}": v
                             for k, v in TRUNCATED_REFDES.items()},
        "kicad_refdes": KICAD_REFDES,
        "nets": {nid: NET_ALIASES.get(n, n) for nid, n in net_names.items()},
        "pads": pads,
    }
    return json.dumps(doc, indent=1) + "\n"


def net_label(p):
    if p["net_raw"] == "N/C":
        return "N/C"
    if not p["net"]:
        return "(no net)"
    return p["net"]


def render_by_component(pads):
    by_ref = defaultdict(list)
    for p in pads:
        if p["kind"] == "pad":
            by_ref[p["refdes"]].append(p)
    out = ["# Pin → net by component",
           "",
           "Generated by `tools/ipc356.py` from the Proteus IPC-D-356A netlist. Do not edit.",
           "",
           "Net aliases: " + ", ".join(f"`{k}` → `{v}`" for k, v in NET_ALIASES.items()) + ".",
           "Refdes restored from 6-char truncation: "
           + ", ".join(f"`{k if isinstance(k, str) else k[0]}` → `{v}`"
                       for k, v in TRUNCATED_REFDES.items()) + ".",
           "KiCad refdes for Proteus names with spaces/signs: "
           + ", ".join(f"`{k}` → `{v}`" for k, v in KICAD_REFDES.items()) + ".",
           "`N/C` = explicitly unconnected in Proteus; `(no net)` = pad without any net record.",
           "Coordinates in mm, IPC orientation (Y up). Side: top/bottom for SMD, both for THT.",
           ""]
    groups = defaultdict(list)
    for ref in by_ref:
        groups[re.match(r"[A-Z_]*?(?=\d|$)", ref).group(0) or ref].append(ref)
    out.append("Index: " + ", ".join(
        f"{g} ({len(groups[g])})" for g in sorted(groups, key=natural_key)))
    out.append("")
    for ref in sorted(by_ref, key=natural_key):
        rows = sorted(by_ref[ref], key=lambda p: natural_key(p["pin"]))
        kinds = {"THT" if p["is_tht"] else "SMD" for p in rows}
        sides = {p["side"] for p in rows}
        out.append(f"## {ref}")
        out.append("")
        if rows[0]["refdes_proteus"] != ref:
            out.append(f"Proteus refdes `{rows[0]['refdes_proteus']}`")
            out.append("")
        out.append(f"{len(rows)} pads, {'/'.join(sorted(kinds))}, side {'/'.join(sorted(sides))}")
        out.append("")
        out.append("| pin | net | x | y |")
        out.append("|---|---|---|---|")
        for p in rows:
            out.append(f"| {p['pin']} | {net_label(p)} | {p['x_mm']:.3f} | {p['y_mm']:.3f} |")
        out.append("")
    return "\n".join(out)


def render_by_net(pads):
    by_net = defaultdict(list)
    vias = Counter()
    for p in pads:
        if p["kind"] == "pad" and p["net"] and p["net"] != "N/C":
            by_net[p["net"]].append(f"{p['refdes']}-{p['pin']}")
        elif p["kind"] in ("via", "free_pad"):
            vias[p["net"]] += 1
    out = ["# Pins by net",
           "",
           "Generated by `tools/ipc356.py`. Do not edit. Nets with a single pin are",
           "usually test points or connections made through the ignored-by-IPC copper.",
           "",
           "`vias` counts vias and free pads (blank refdes with a net).",
           "",
           "| net | pins | vias | members |",
           "|---|---|---|---|"]
    for net in sorted(set(by_net) | set(vias), key=natural_key):
        members = sorted(by_net.get(net, []), key=natural_key)
        out.append(f"| {net} | {len(members)} | {vias.get(net, 0)} | {' '.join(members)} |")
    out.append("")
    return "\n".join(out)


def outputs(net_names, pads):
    return {
        "ipc356_pads.json": render_json(net_names, pads),
        "ipc356_pads.csv": render_csv(pads),
        "pin_nets_by_component.md": render_by_component(pads),
        "pins_by_net.md": render_by_net(pads),
    }


def self_check(net_names, pads, out_dir):
    errors = []
    st = stats(net_names, pads)
    for k, v in EXPECTED.items():
        if st[k] != v:
            errors.append(f"{k}: got {st[k]}, expected {v}")

    names = set(net_names.values())
    for raw, alias in NET_ALIASES.items():
        if raw not in names:
            errors.append(f"alias source net {raw!r} not in IPC")
        if alias in names:
            errors.append(f"alias target {alias!r} collides with an existing net")
    if len({NET_ALIASES.get(n, n) for n in names}) != len(names):
        errors.append("net names not unique after aliasing")
    used = {p["net_id"] for p in pads if p["net_id"]}
    unused = set(net_names) - used
    if unused:
        errors.append(f"declared nets without pads: {sorted(unused)}")

    # The terminals have two holes both numbered 1: fine if the net agrees.
    pin_nets = defaultdict(set)
    for p in pads:
        if p["kind"] == "pad":
            pin_nets[(p["refdes"], p["pin"])].add(p["net_raw"])
    conflicts = sorted(k for k, n in pin_nets.items() if len(n) > 1)
    if conflicts:
        errors.append(f"same refdes-pin on different nets: {conflicts}")
    for p in pads:
        exp = TRUNCATED_NETS.get(p["refdes"])
        if exp and p["net"] != exp:
            errors.append(f"{p['refdes']}: net {p['net']}, expected {exp}")

    refs = {p["refdes"] for p in pads if p["kind"] == "pad"}
    bad = sorted(r for r in refs if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", r))
    if bad:
        errors.append(f"refdes not usable in KiCad (add to KICAD_REFDES): {bad}")
    if set(KICAD_REFDES.values()) & {p["refdes_proteus"] for p in pads}:
        errors.append("KICAD_REFDES target collides with a Proteus refdes")
    for p in pads:
        if p["kind"] == "via" and not (p["plated"] and p["drill"] in VIA_DRILLS):
            errors.append(f"via at ({p['x_mm']}, {p['y_mm']}): drill {p['drill']}")
        if p["kind"] == "free_pad" and not (p["plated"] and p["drill"] in FREE_PAD_DRILLS):
            errors.append(f"free pad at ({p['x_mm']}, {p['y_mm']}): drill {p['drill']}")
        if p["kind"] == "hole" and (p["plated"] != HOLES_PLATED or p["drill"] not in HOLE_DRILLS):
            errors.append(f"hole at ({p['x_mm']}, {p['y_mm']}): drill {p['drill']}")

    for name, text in outputs(net_names, pads).items():
        f = out_dir / name
        if not f.exists() or f.read_text() != text:
            errors.append(f"{f.relative_to(ROOT)} is stale; run p2k ipc356")
    return st, errors


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ipc", type=Path, default=DEFAULT_IPC)
    ap.add_argument("--out", type=Path, default=REF_DIR)
    ap.add_argument("--check", action="store_true",
                    help="verify counts and that the files in --out are up to date")
    args = ap.parse_args()

    net_names, pads = parse(args.ipc)
    if args.check:
        st, errors = self_check(net_names, pads, args.out)
        print("ipc356: " + ", ".join(f"{k}={v}" for k, v in st.items()))
        for e in errors:
            print("  FAIL " + e)
        print("ipc356: " + ("FAIL" if errors else "OK"))
        return 1 if errors else 0

    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in outputs(net_names, pads).items():
        (args.out / name).write_text(text)
    st = stats(net_names, pads)
    print("ipc356: " + ", ".join(f"{k}={v}" for k, v in st.items()))
    print(f"wrote {len(outputs(net_names, pads))} files to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
