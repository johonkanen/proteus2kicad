#!/usr/bin/env python3
"""DRC of the board via kicad-cli, with schematic parity (stdlib only).

Writes <reports>/drc.json (full kicad-cli report) and prints a count per violation type.
Fails on: any short (shorting_items), any unconnected item, any schematic parity issue, and
any other error-severity violation whose type is not in the config key DRC_ACCEPTED
({type: reason}; each accepted type is justified there and in the board's STATUS.md).
Warnings are only counted.

Usage: tools/p2k/bin/p2k drc [-v]
"""
import json
import os
import subprocess
import sys
from collections import Counter

from . import board
from . import pcb


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    accepted = getattr(pcb.cfg(), "DRC_ACCEPTED", {})
    report = os.path.join(pcb.reports_dir(), "drc.json")
    cli = os.path.join(board.BIN, "kicad-cli")
    os.makedirs(os.path.dirname(report), exist_ok=True)
    r = subprocess.run([cli, "pcb", "drc", "--schematic-parity", "--severity-all", "--format", "json",
                        "-o", report, pcb.board_file()], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(report):
        sys.exit(f"drc: kicad-cli failed:\n{r.stdout}{r.stderr}")
    with open(report) as f:
        d = json.load(f)
    viol = d["violations"]
    unconnected = d.get("unconnected_items", [])
    parity = d.get("schematic_parity", [])
    by_type = Counter((v["type"], v["severity"]) for v in viol)
    errors = []
    shorts = by_type.get(("shorting_items", "error"), 0)
    if shorts:
        errors.append(f"{shorts} shorts")
    if unconnected:
        errors.append(f"{len(unconnected)} unconnected items")
    if parity:
        errors.append(f"{len(parity)} schematic parity issues")
    for (t, sev), n in sorted(by_type.items()):
        if sev == "error" and t != "shorting_items" and t not in accepted:
            errors.append(f"{n} {t}")
    print(f"drc: {len(viol)} violations, {shorts} shorts, {len(unconnected)} unconnected, "
          f"{len(parity)} parity issues ({pcb.rel(report)})")
    for (t, sev), n in sorted(by_type.items(), key=lambda kv: (kv[0][1], -kv[1])):
        note = f"  accepted: {accepted[t]}" if sev == "error" and t in accepted else ""
        print(f"drc:   {n:4d} {sev:8s} {t}{note}")
    if "-v" in argv or errors:
        for v in viol:
            if v["severity"] == "error" and v["type"] not in accepted:
                items = " / ".join(i["description"] for i in v["items"])
                print(f"drc: {v['type']}: {v['description']}: {items}")
        for u in unconnected[:20]:
            print("drc: unconnected: " + " / ".join(i["description"] for i in u["items"]))
        for p in parity[:20]:
            print(f"drc: parity: {p['description']}: " + " / ".join(i["description"] for i in p["items"]))
    for e in errors:
        print("drc: ERROR " + e)
    print("drc: " + ("FAILED" if errors else "OK"))
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
