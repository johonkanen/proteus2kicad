"""Shared bits of the Phase 3 board tools (build_pcb, gerber_to_tracks, assign_nets,
pours_to_zones, check_board, drc). pcbnew is imported only inside the functions that need it,
so drc runs with the system python3.

Coordinates: the IPC file and the gerbers use mm with Y up; the KiCad board uses the same
origin with Y down, so KiCad (x, y) = (x, -y) of the IPC/gerber point.

Paths (defaults from p2k_config.py, overridable):
  board      PCB_FILE key or kicad/<PROJECT>.kicad_pcb       env P2K_PCB
  schematic  SCHEMATIC_FILE key or kicad/<PROJECT>.kicad_sch  env P2K_SCHEMATIC
  reports    reports/                                         env P2K_REPORTS
A board wrapper may call configure(board=..., schematic=..., reports=..., records=fn) before
running a tool; `records` filters the IPC records (e.g. intended deviations of a later design
revision) and is used by every tool that reads them.

Config keys used here: PROJECT, CADCAM, COPPER, DRILL_PLATED, DRILL_NONPLATED, OUTLINE.
"""
import glob
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from . import board as _board

TOL = 0.05  # mm, pad position tolerance against the IPC file

_over = {}


def configure(board=None, schematic=None, reports=None, records=None):
    """Override paths / the IPC record filter (board wrappers, tests)."""
    for k, v in (("board", board), ("schematic", schematic), ("reports", reports), ("records", records)):
        if v is not None:
            _over[k] = v


def cfg():
    return _board.config()


def root():
    return _board.root()


def _path(key, env, cfg_key, default):
    if key in _over:
        return _over[key]
    if os.environ.get(env):
        return os.path.abspath(os.environ[env])
    rel = getattr(cfg(), cfg_key, None) or default
    return _board.path(rel)


def board_file():
    return _path("board", "P2K_PCB", "PCB_FILE", os.path.join("kicad", f"{cfg().PROJECT}.kicad_pcb"))


def schematic_file():
    return _path("schematic", "P2K_SCHEMATIC", "SCHEMATIC_FILE", os.path.join("kicad", f"{cfg().PROJECT}.kicad_sch"))


def reports_dir():
    return _path("reports", "P2K_REPORTS", "REPORTS_DIR", "reports")


def rel(path):
    return os.path.relpath(path, root())


def copper():
    """[(gerber name, KiCad copper layer, User layer for the pour outlines), ...]"""
    return list(cfg().COPPER)


def gerber_path(name):
    cadcam = _board.path(cfg().CADCAM)
    paths = glob.glob(os.path.join(cadcam, f"* - CADCAM {name}.GBR"))
    if len(paths) != 1:
        sys.exit(f"gerber {name!r} not found in {cadcam}")
    return paths[0]


def ipc_records():
    """IPC records (reference/ipc356_pads.json), through the configured filter if any."""
    with open(_board.path("reference", "ipc356_pads.json")) as f:
        recs = json.load(f)["pads"]
    fn = _over.get("records")
    return fn(recs) if fn else recs


def export_netlist():
    """kicadxml netlist of the schematic -> (comps, nets).
    comps: {ref: dict(value, footprint, fields, sheet_tstamps, tstamps, sheetname,
                      sheetfile, excluded, dnp)}
    nets:  {name: [(ref, pin), ...]}"""
    cli = os.path.join(_board.BIN, "kicad-cli")
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "netlist.xml")
        r = subprocess.run([cli, "sch", "export", "netlist", "--format", "kicadxml", "-o", out, schematic_file()],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"netlist export failed:\n{r.stdout}{r.stderr}")
        xml = ET.parse(out).getroot()
    comps = {}
    for c in xml.iter("comp"):
        props = {p.get("name"): p.get("value") for p in c.findall("property")}
        fields = {f.get("name"): (f.text or "") for f in c.iter("field")}
        sp = c.find("sheetpath")
        comps[c.get("ref")] = dict(
            value=c.findtext("value", ""), footprint=c.findtext("footprint", ""), fields=fields,
            sheet_tstamps=sp.get("tstamps"), tstamps=c.findtext("tstamps", "").split(),
            sheetname=props.get("Sheetname", ""), sheetfile=props.get("Sheetfile", ""),
            excluded="exclude_from_board" in props, dnp="dnp" in props)
    nets = {}
    for n in xml.iter("net"):
        nets[n.get("name")] = [(nd.get("ref"), nd.get("pin")) for nd in n.findall("node")]
    return comps, nets


def is_unconnected_net(name):
    """KiCad's per-pin nets of unconnected symbol pins (no-connect flags)."""
    return not name or name.startswith("unconnected-(")


def ipc_to_kicad_nets(board):
    """Map IPC net -> KiCad net name through the footprint pads (pad-set equivalence).
    Returns (mapping, problems). An IPC net maps to exactly one KiCad net, or it is a problem."""
    ipc = {}
    for p in ipc_records():
        if p["kind"] == "pad" and p["net"] and p["net_raw"] != "N/C":
            ipc[(p["refdes"], p["pin"])] = p["net"]
    seen = {}
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        for pad in fp.Pads():
            key = (ref, pad.GetNumber())
            if key in ipc:
                seen.setdefault(ipc[key], set()).add(pad.GetNetname())
    mapping, problems = {}, []
    for net, kn in sorted(seen.items()):
        if len(kn) == 1:
            mapping[net] = next(iter(kn))
        else:
            problems.append(f"IPC net {net} spans KiCad nets {sorted(kn)}")
    back = {}
    for net, kn in mapping.items():
        back.setdefault(kn, []).append(net)
    for kn, nets in back.items():
        if len(nets) > 1:
            problems.append(f"KiCad net {kn} spans IPC nets {sorted(nets)}")
    return mapping, problems
