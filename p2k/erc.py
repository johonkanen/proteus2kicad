#!/usr/bin/env python3
"""ERC of the project schematic via kicad-cli, judged by the board's config (stdlib only).

  p2k erc [--no-pending] [schematic [report]]

Runs `kicad-cli sch erc --severity-all` on the schematic (default: SCHEMATIC_FILE /
$P2K_SCHEMATIC / kicad/<PROJECT>.kicad_sch) and writes the report (default
<reports>/erc.rpt). Fails on
- every ERC error, except the ones in ERC_PENDING;
- every warning whose type is in ERC_FATAL_WARNINGS (default none; e.g.
  {"no_connect_connected", "footprint_link_issues"});
- an ERC_PENDING entry that ERC no longer reports (the list cannot go stale).
Other warnings are counted by type.

p2k_config.py keys
  ERC_FATAL_WARNINGS   set of warning types that fail
  ERC_PENDING          {"<type> <refdes> <pin>": reason}: errors expected while sheets are still
                       missing (matched against the first item of a violation, "Symbol <refdes>
                       Pin <pin>"); must be empty when the schematic is complete
  PENDING              {"erc": {pattern: question}}: errors / fatal warnings ("<type>: <items>")
                       that wait for a user decision (see board.judge_pending); printed as
                       "PENDING USER DECISION", an entry that matches nothing fails
--no-pending ignores ERC_PENDING and PENDING["erc"] (for tests on a scratch copy).
"""
import os
import re
import subprocess
import sys
from collections import Counter

from . import board, pcb


def violations(path):
    """[(type, severity, [item lines])] of a kicad-cli ERC report."""
    out = []
    for line in open(path, encoding="utf-8"):
        m = re.match(r"\[(\w+)\]:", line)
        if m:
            out.append([m.group(1), "?", []])
        elif out and line.strip().startswith(";"):
            out[-1][1] = line.strip("; \n").split()[0]
        elif out and line.strip().startswith("@("):
            out[-1][2].append(line.strip())
    return out


def key(v):
    """'type REF PIN' of a violation whose first item is a symbol pin, else None."""
    m = re.search(r"Symbol (\S+) Pin (\S+)", v[2][0]) if v[2] else None
    return f"{v[0]} {m.group(1)} {m.group(2)}" if m else None


def judge(rpt, fatal_types=(), pending=None, decisions=True):
    """Print the verdict of a report; return the exit code."""
    pending = {" ".join(k.split()): why for k, why in (pending or {}).items()}
    vs = violations(rpt)
    errors = [v for v in vs if v[1] == "error"]
    warns = [v for v in vs if v[1] == "warning"]
    real = [v for v in errors if key(v) not in pending]
    seen = {key(v) for v in errors}
    stale = sorted(k for k in pending if k not in seen)
    fatal = [v for v in warns if v[0] in fatal_types]
    questions, pstale = [], []
    if decisions and board.pending("erc"):
        # PENDING["erc"]: errors / fatal warnings that wait for a user decision ("<type>: <items>")
        text = lambda v: f"{v[0]}: " + " | ".join(v[2])
        rest, questions, pstale = board.judge_pending("erc", [text(v) for v in real + fatal])
        keep = set(rest)
        real = [v for v in real if text(v) in keep]
        fatal = [v for v in fatal if text(v) in keep]
    for v in real:
        print(f"erc: ERROR {v[0]}: " + " | ".join(v[2]))
    for v in fatal:
        print(f"erc: FATAL WARNING {v[0]} (ERC_FATAL_WARNINGS): " + " | ".join(v[2]))
    for k in stale:
        print(f"erc: stale ERC_PENDING entry (no longer reported, remove it): {k}")
    by_type = Counter(v[0] for v in warns)
    print(f"erc: {len(real)} errors, {len(warns)} warnings"
          + (" (" + ", ".join(f"{n} {t}" for t, n in sorted(by_type.items())) + ")" if warns else "")
          + (f", {len(errors) - len(real)} pending errors (ERC_PENDING)" if pending else "")
          + f" ({os.path.relpath(rpt)})")
    for p in pstale:
        print("erc: ERROR " + p)
    bad = real or fatal or stale or pstale
    if not bad:
        for q in questions:
            print("erc: PENDING USER DECISION: " + q)
    return 1 if bad else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    use_pending = "--no-pending" not in argv
    args = [a for a in argv if not a.startswith("--")]
    sch = os.path.abspath(args[0]) if args else pcb.schematic_file()
    rpt = os.path.abspath(args[1]) if len(args) > 1 else os.path.join(pcb.reports_dir(), "erc.rpt")
    cfg = board.config()
    fatal_types = set(getattr(cfg, "ERC_FATAL_WARNINGS", set()))
    pending = getattr(cfg, "ERC_PENDING", {}) if use_pending else {}
    os.makedirs(os.path.dirname(rpt), exist_ok=True)
    cli = os.path.join(board.BIN, "kicad-cli")
    r = subprocess.run([cli, "sch", "erc", "--severity-all", "-o", rpt, sch], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(rpt):
        print(f"erc: kicad-cli failed:\n{r.stdout}{r.stderr}")
        return 1
    return judge(rpt, fatal_types, pending, use_pending)


if __name__ == "__main__":
    sys.exit(main())
