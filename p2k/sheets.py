#!/usr/bin/env python3
"""Re-run the board's sheet scripts and compare the schematic sheets (CANDIDATES.md K8).

    p2k sheets            run tools/sheets/sheet[0-9]*.py; print how many sheet files changed
    p2k sheets --check    the same, then fail (exit 1) if a kicad/*.kicad_sch changed; the
                          original files are restored, so a check never changes the repo

The scripts run in name order with the python running this tool, from the board root (stdout of
the scripts is suppressed unless one fails). Compared: every kicad/*.kicad_sch (the root sheet
included), byte for byte. The scripts write the sheets in place, so the old content is kept in
memory and written back after a failed --check.
"""
import glob
import os
import re
import subprocess
import sys

from . import board


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    check = "--check" in argv
    unknown = [a for a in argv if a != "--check"]
    if unknown:
        print(f"usage: p2k sheets [--check]  (unknown argument {unknown[0]})")
        return 2
    root = board.root()
    scripts = sorted(s for s in glob.glob(os.path.join(root, "tools", "sheets", "sheet*.py"))
                     if re.fullmatch(r"sheet[0-9].*\.py", os.path.basename(s)))
    if not scripts:
        print("sheets: no tools/sheets/sheet[0-9]*.py")
        return 1
    files = sorted(glob.glob(os.path.join(root, "kicad", "*.kicad_sch")))
    before = {f: open(f, "rb").read() for f in files}
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for s in scripts:
        r = subprocess.run([sys.executable, s], cwd=root, env=env, capture_output=True, text=True)
        if r.returncode:
            print(r.stdout + r.stderr, end="")
            print(f"sheets: {os.path.relpath(s, root)} failed (exit {r.returncode})")
            if check:
                _restore(before)
            return 1
    now = set(glob.glob(os.path.join(root, "kicad", "*.kicad_sch")))
    changed = [f for f in files if open(f, "rb").read() != before[f]]
    new = sorted(now - set(files))
    if not check:
        print(f"sheets: {len(scripts)} sheet scripts run, {len(changed)} sheet files changed"
              + (f", {len(new)} new" if new else ""))
        return 0
    if changed or new:
        for f in changed:
            print(f"sheets: kicad/{os.path.basename(f)} was not up to date")
        for f in new:
            print(f"sheets: kicad/{os.path.basename(f)} is new")
        _restore(before, new)
        print("sheets: FAILED (originals restored; run `p2k sheets` and commit)")
        return 1
    print(f"sheets: {len(scripts)} sheet scripts, kicad/*.kicad_sch up to date")
    return 0


def _restore(before, new=()):
    for f, data in before.items():
        with open(f, "wb") as fh:
            fh.write(data)
    for f in new:
        os.remove(f)


if __name__ == "__main__":
    sys.exit(main())
