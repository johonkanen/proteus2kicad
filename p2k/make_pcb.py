#!/usr/bin/env python3
"""Phase 3 build chain: rebuild the board from the schematic and the Proteus gerbers (B12; the
boards' tools/make_pcb.sh as one shared tool; stdlib only, the steps run under kipy).

  p2k make_pcb           run the steps on the board in place
  p2k make_pcb --check   run them into build/tmp/make_pcb_check (with copies of
                         reports/footprint_fit.csv and the .kicad_pro: pcbnew loads the project next
                         to the board, which affects the UUID sequence) and fail unless the board,
                         reports/pour_nets.json and reports/assign_nets.txt equal the committed ones
                         byte for byte (assign_nets.txt only up to "== after zone fill", the section
                         `assign_nets --check` appends, unless ASSIGN_NETS_CHECK_REPORT moves it)

The steps stop at the first failing one, and its full output is printed (`make_pcb: FAILED at step
...`): the template's `run() || ...` in --check mode turned `set -e` off inside run(), so a failing
step let the later steps run on and the filtered log hid its ERROR line (TC26, PF25).

Config: PCB_STEPS (optional) = list of steps, each a p2k module name (`"build_pcb"`, run as `kipy -m
p2k.build_pcb`) or a board script with arguments (`"tools/rename_holes.py"`, `"tools/pcb_wrap.py
build_pcb"`, run as `kipy <script> <args>`). Default: build_pcb, gerber_to_tracks, assign_nets,
pours_to_zones (only when a Proteus copper gerber has a pour, i.e. a dark region), place_refdes.
The reference board for --check is $P2K_PCB_REF, else the board file (PCB_FILE / $P2K_PCB).

Usage: tools/p2k/bin/p2k make_pcb [--check]
"""
import filecmp
import os
import shlex
import shutil
import subprocess
import sys

from . import board as _board
from . import pcb as bc

STEPS = ("build_pcb", "gerber_to_tracks", "assign_nets", "place_refdes")
SUMMARY = {   # --check: the lines of each p2k step that are printed (as the board template's grep)
    "build_pcb": "build_pcb", "gerber_to_tracks": "gerber_to_tracks: ", "assign_nets": "assign_nets: stage",
    "pours_to_zones": "pours_to_zones", "place_refdes": "place_refdes",
}


def has_pours():
    from . import gerber
    for name, _layer, _user in bc.copper():
        g = gerber.parse(bc.gerber_path(name))
        if any(isinstance(o, gerber.Region) and o.dark for o in g.objects):
            return True
    return False


def steps():
    val = getattr(bc.cfg(), "PCB_STEPS", None)
    if val is not None:
        if isinstance(val, str) or not val:
            raise SystemExit(f"make_pcb: PCB_STEPS must be a non-empty list of steps, got {val!r}")
        return list(val)
    st = list(STEPS)
    if has_pours():
        st.insert(3, "pours_to_zones")
    return st


def command(step):
    kipy = os.path.join(_board.BIN, "kipy")
    parts = shlex.split(step)
    if parts[0].endswith(".py") or "/" in parts[0]:
        return [kipy, _board.path(parts[0])] + parts[1:]
    if len(parts) != 1 or not os.path.exists(os.path.join(os.path.dirname(__file__), parts[0] + ".py")):
        raise SystemExit(f"make_pcb: PCB_STEPS entry {step!r} is neither a p2k module nor a board script")
    return [kipy, "-m", f"p2k.{parts[0]}"]


def run_steps(st, env, capture):
    """Run the steps in order; stop at the first failure. -> (ok, [(step, output)])"""
    outs = []
    for step in st:
        cmd = command(step)
        if capture:
            r = subprocess.run(cmd, cwd=bc.root(), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            outs.append((step, r.stdout))
        else:
            r = subprocess.run(cmd, cwd=bc.root(), env=env)
        if r.returncode != 0:
            if capture:
                sys.stdout.write(r.stdout)
            print(f"make_pcb: FAILED at step {step} (exit {r.returncode}); later steps not run: "
                  + (", ".join(st[st.index(step) + 1:]) or "none"))
            return False, outs
    return True, outs


def stages(path):
    """reports/assign_nets.txt without the section `assign_nets --check` appends."""
    if not os.path.exists(path):
        return None
    out = []
    for line in open(path):
        if line.startswith("== after zone fill"):
            break
        out.append(line)
    return "".join(out)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv not in ([], ["--check"]):
        sys.exit(__doc__)
    st = steps()
    for s in st:
        command(s)   # validate every entry before anything runs
    env = dict(os.environ)
    if not argv:
        ok, _ = run_steps(st, env, capture=False)
        return 0 if ok else 1
    ref = os.environ.get("P2K_PCB_REF") or bc.board_file()
    ref = ref if os.path.isabs(ref) else os.path.join(bc.root(), ref)
    reports = bc.reports_dir()
    tmp = os.path.join(bc.root(), "build", "tmp", "make_pcb_check")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(os.path.join(tmp, "reports"))
    try:
        fit = os.path.join(reports, "footprint_fit.csv")
        if os.path.exists(fit):
            shutil.copy(fit, os.path.join(tmp, "reports"))
        pro = os.path.splitext(ref)[0] + ".kicad_pro"
        if os.path.exists(pro):
            shutil.copy(pro, tmp)
        env["P2K_PCB"] = os.path.join(tmp, os.path.basename(ref))
        env["P2K_REPORTS"] = os.path.join(tmp, "reports")
        ok, outs = run_steps(st, env, capture=True)
        if not ok:
            print("make_pcb: FAILED")
            return 1
        same = True
        if not filecmp.cmp(env["P2K_PCB"], ref, shallow=False):
            print(f"make_pcb: {bc.rel(ref)} is not up to date (run p2k make_pcb)")
            same = False
        an_new, an_old = os.path.join(tmp, "reports", "assign_nets.txt"), os.path.join(reports, "assign_nets.txt")
        if getattr(bc.cfg(), "ASSIGN_NETS_CHECK_REPORT", None):
            differs = os.path.exists(an_new) != os.path.exists(an_old) or (
                os.path.exists(an_new) and not filecmp.cmp(an_new, an_old, shallow=False))
        else:
            differs = stages(an_new) != stages(an_old)
        if differs:
            print(f"make_pcb: {bc.rel(an_old)} is not up to date")
            same = False
        pn_new, pn_old = os.path.join(tmp, "reports", "pour_nets.json"), os.path.join(reports, "pour_nets.json")
        if os.path.exists(pn_new) != os.path.exists(pn_old) or (
                os.path.exists(pn_new) and not filecmp.cmp(pn_new, pn_old, shallow=False)):
            print(f"make_pcb: {bc.rel(pn_old)} is not up to date")
            same = False
        for step, out in outs:
            name = shlex.split(step)[0]
            prefix = SUMMARY.get(name) or os.path.splitext(os.path.basename(name))[0]
            for line in out.splitlines():
                if not line.startswith(prefix):
                    continue
                if name == "gerber_to_tracks" and not line[len(prefix):][:1].isdigit():
                    continue
                if name == "build_pcb" and " -> " in line:
                    line = line[:line.index(" -> ")]
                print(line)
        if not same:
            return 1
        print("make_pcb: board rebuilt from scratch, byte-identical")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
