#!/usr/bin/env python3
"""DRC of the board via kicad-cli, with schematic parity (stdlib only).

Writes <reports>/drc.json (full kicad-cli report) and prints a count per violation type.
Fails on: any short (shorting_items), any unconnected item, any schematic parity issue, and
any other error-severity violation whose type is not in the config key DRC_ACCEPTED
({type: reason}; each accepted type is justified there and in the board's STATUS.md).
Warnings are only counted (DRC_EXPECTED_WARNINGS = {type: count}: these warning counts must match
exactly). DRC_ACCEPTED_ITEMS = {type: {item pattern: reason or (reason, count)}}: single error
violations accepted by a regex on their item texts; every pattern must match (exactly `count`
violations when given). PENDING = {"drc": {pattern: question}}: remaining failures (texts
"<type>: <description>: <items>", "unconnected: <items>", "parity: ...") that wait for a user
decision; passes only when every remaining failure is listed and every entry still occurs, and
prints "PENDING USER DECISION: <question>". SPLIT_PIN_MERGES (optional): exactly one unconnected item per KiCad
net of such a pin (its two pads have no copper between them) is accepted (printed).

Usage: tools/p2k/bin/p2k drc [-v]
"""
import json
import os
import re
import subprocess
import sys
from collections import Counter

from . import board
from . import pcb


NET = re.compile(r"\[([^]]*)\]")


def split_pin_unconnected(unconnected):
    """SPLIT_PIN_MERGES: exactly one unconnected item per KiCad net of such a pin (the net from the
    schematic netlist), every item of it on that net. -> (other items, accepted texts, errors)"""
    _, nets = pcb.export_netlist()
    want = pcb.split_pin_nets(nets)
    rest, seen = [], []
    for u in unconnected:
        names = {m.group(1) for i in u["items"] for m in [NET.search(i["description"])] if m}
        if len(names) == 1 and next(iter(names)) in want:
            seen.append(next(iter(names)))
        else:
            rest.append(u)
    errors = []
    if sorted(seen) != sorted(want):
        errors.append(f"SPLIT_PIN_MERGES: unconnected items on {sorted(seen)}, expected one on each of {sorted(want)}")
    return rest, [f"{want[n]} {n}" for n in sorted(seen)], errors


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
    n_unconnected = len(unconnected)
    parity = d.get("schematic_parity", [])
    by_type = Counter((v["type"], v["severity"]) for v in viol)
    errors = []
    # DRC_ACCEPTED_ITEMS: single error violations accepted by their item texts (B7)
    failing = [v for v in viol if v["severity"] == "error" and (v["type"] == "shorting_items" or v["type"] not in accepted)]
    failing, item_acc, item_err = accept_items(failing)
    errors += item_err
    shorts_total = by_type.get(("shorting_items", "error"), 0)
    shorts = sum(1 for v in failing if v["type"] == "shorting_items")
    split_acc = []
    if unconnected and pcb.split_pin_merges():
        unconnected, split_acc, split_err = split_pin_unconnected(unconnected)
        errors += split_err
    # PENDING["drc"]: the remaining failures that wait for a user decision (B9)
    texts = ([f"{v['type']}: {v['description']}: " + " / ".join(i["description"] for i in v["items"]) for v in failing]
             + ["unconnected: " + " / ".join(i["description"] for i in u["items"]) for u in unconnected]
             + [f"parity: {p['description']}: " + " / ".join(i["description"] for i in p["items"]) for p in parity])
    questions = []
    if board.pending("drc"):
        rest, questions, stale = board.judge_pending("drc", texts)
        errors += stale
        keep = set(rest)
        n1, n2 = len(failing), len(failing) + len(unconnected)
        failing = [v for v, t in zip(failing, texts[:n1]) if t in keep]
        unconnected = [u for u, t in zip(unconnected, texts[n1:n2]) if t in keep]
        parity = [p for p, t in zip(parity, texts[n2:]) if t in keep]
        shorts = sum(1 for v in failing if v["type"] == "shorting_items")
    if shorts:
        errors.append(f"{shorts} shorts")
    if unconnected:
        errors.append(f"{len(unconnected)} unconnected items")
    if parity:
        errors.append(f"{len(parity)} schematic parity issues")
    for t, n in sorted(Counter(v["type"] for v in failing if v["type"] != "shorting_items").items()):
        errors.append(f"{n} {t}")
    # DRC_EXPECTED_WARNINGS: exact warning counts per type (B8)
    expected = getattr(pcb.cfg(), "DRC_EXPECTED_WARNINGS", None)
    if expected is not None:
        for t, n in sorted(expected.items()):
            got = by_type.get((t, "warning"), 0)
            if got != n:
                errors.append(f"{got} {t} warnings, DRC_EXPECTED_WARNINGS {n}")
    print(f"drc: {len(viol)} violations, {shorts_total} shorts, {n_unconnected} unconnected, "
          f"{len(d.get('schematic_parity', []))} parity issues ({pcb.rel(report)})")
    for (t, sev), n in sorted(by_type.items(), key=lambda kv: (kv[0][1], -kv[1])):
        note = f"  accepted: {accepted[t]}" if sev == "error" and t in accepted else ""
        print(f"drc:   {n:4d} {sev:8s} {t}{note}")
    if "-v" in argv or errors:
        for v in failing:
            items = " / ".join(i["description"] for i in v["items"])
            print(f"drc: {v['type']}: {v['description']}: {items}")
        for u in unconnected[:20]:
            print("drc: unconnected: " + " / ".join(i["description"] for i in u["items"]))
        for p in parity[:20]:
            print(f"drc: parity: {p['description']}: " + " / ".join(i["description"] for i in p["items"]))
    if not errors:
        for a in item_acc:
            print("drc: accepted (DRC_ACCEPTED_ITEMS): " + a)
        if expected:
            print("drc: warning counts as expected (DRC_EXPECTED_WARNINGS): "
                  + ", ".join(f"{n} {t}" for t, n in sorted(expected.items())))
    if split_acc and not errors:
        print("drc: accepted (SPLIT_PIN_MERGES): " + ", ".join(split_acc))
    for e in errors:
        print("drc: ERROR " + e)
    if not errors:
        for q in questions:
            print("drc: PENDING USER DECISION: " + q)
    print("drc: " + ("FAILED" if errors else "OK"))
    sys.exit(1 if errors else 0)


def accept_items(failing):
    """DRC_ACCEPTED_ITEMS ({type: {item pattern: reason or (reason, count)}}): an error violation of
    that type whose item texts (" / "-joined descriptions) match the pattern (re.search) is accepted.
    Every pattern must match at least one violation, with a count exactly that many.
    -> (remaining violations, accepted texts, errors)"""
    table = getattr(pcb.cfg(), "DRC_ACCEPTED_ITEMS", {}) or {}
    if not table:
        return failing, [], []
    hits = {(t, pat): 0 for t, pats in table.items() for pat in pats}
    rest = []
    for v in failing:
        text = " / ".join(i["description"] for i in v["items"])
        pat = next((p for p in table.get(v["type"], {}) if re.search(p, text)), None)
        if pat is None:
            rest.append(v)
        else:
            hits[(v["type"], pat)] += 1
    acc, errors = [], []
    for (t, pat), n in hits.items():
        val = table[t][pat]
        why, want = val if isinstance(val, tuple) else (val, None)
        if n == 0 or (want is not None and n != want):
            errors.append(f"DRC_ACCEPTED_ITEMS[{t!r}][{pat!r}]: matches {n} violations"
                          + (f", expected {want}" if want is not None else ", expected at least 1"))
        else:
            acc.append(f"{n} {t} /{pat}/: {why}")
    return rest, acc, errors

if __name__ == "__main__":
    main()
