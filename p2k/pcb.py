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

Config keys used here: PROJECT, CADCAM, COPPER, DRILL_PLATED, DRILL_NONPLATED, OUTLINE; the
recorded copper deviations NONCONDUCTOR_LAYERS, REMOVED_TRACKS, REMOVED_VIAS (optional; used by
gerber_to_tracks and accepted by check_board, ipc356_diff, gerber_roundtrip); SMD_FREE_PADS (SMD test
pads without a part) and SPLIT_PIN_MERGES (pins on two IPC nets joined by the schematic; applied by
ipc_records()), both optional.
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


def ipc_records(merge=True):
    """IPC records (reference/ipc356_pads.json), through the configured filter if any, then with
    the SPLIT_PIN_MERGES of the config applied (merge=False: the Proteus nets as written, for the
    independent Phase 4 comparison). See merge_split_pins()."""
    with open(_board.path("reference", "ipc356_pads.json")) as f:
        recs = json.load(f)["pads"]
    fn = _over.get("records")
    if fn:
        recs = fn(recs)
    merges = split_pin_merges()
    if merge and merges:
        merged = {m for _, m in merges.values()}
        if fn and not split_pins(recs) and not any(p["net"] in merged for p in recs):
            return recs   # the board's own record filter has joined them already (wrapper before migration)
        errors = split_pin_errors(recs)
        if errors:
            raise SystemExit("p2k (SPLIT_PIN_MERGES): " + "; ".join(errors))
        recs = merge_split_pins(recs)
    return recs


# ---------------------------------------------------------------- SMD test pads, split pins (B4, B5)

def smd_free_pads():
    """SMD_FREE_PADS ({(x, y): raw IPC net}, IPC frame, exact IPC coordinates): SMD pads without
    a part (test pads); {} without the key."""
    return getattr(cfg(), "SMD_FREE_PADS", {}) or {}


def is_smd_free_pad(p):
    """True for an IPC record that is one of the SMD_FREE_PADS: blank refdes, SMD (327), top side,
    unplated, no drill, at a listed position with the listed raw net."""
    sf = smd_free_pads()
    key = (p["x_mm"], p["y_mm"])
    return (bool(sf) and not p["refdes"] and not p["is_tht"] and p["side"] == "top" and not p["plated"]
            and not p["drill"] and key in sf and p["net_raw"] == sf[key])


def split_pin_merges():
    """SPLIT_PIN_MERGES ({(refdes, pin): (kept net, merged net)}, nets after NET_ALIASES): pins
    whose pads Proteus puts on two nets and the schematic joins (e.g. a two-pad terminal drawn as
    one pin); {} without the key."""
    return getattr(cfg(), "SPLIT_PIN_MERGES", {}) or {}


def split_pins(recs):
    """{(refdes, pin): {nets}} of the pins whose pad records carry more than one net."""
    nets = {}
    for p in recs:
        if p["kind"] == "pad":
            nets.setdefault((p["refdes"], p["pin"]), set()).add(p["net"])
    return {k: v for k, v in nets.items() if len(v) > 1}


def split_pin_errors(recs):
    """The split pins of the records must equal SPLIT_PIN_MERGES, both ways, with exactly the
    listed two nets. -> [error]"""
    merges, split = split_pin_merges(), split_pins(recs)
    errors = []
    for k in sorted(set(split) | set(merges)):
        if k not in merges:
            errors.append(f"IPC pin {k[0]}-{k[1]} has pads on {sorted(split[k])}, not in SPLIT_PIN_MERGES")
        elif split.get(k) != set(merges[k]):
            errors.append(f"SPLIT_PIN_MERGES {k[0]}-{k[1]} {merges[k]} != IPC pad nets {sorted(split.get(k, []))}")
    return errors


def merge_split_pins(recs):
    """Records with every merged net of SPLIT_PIN_MERGES renamed to its kept net (pads, vias, free
    pads; other pins of the merged net included)."""
    rename = {merged: kept for kept, merged in split_pin_merges().values()}
    return [dict(p, net=rename[p["net"]]) if p["net"] in rename else p for p in recs]


def split_pin_nets(nets):
    """{KiCad net name: "REF-PIN"} of the SPLIT_PIN_MERGES pins in an export_netlist() net map;
    every pin must be found on a net."""
    merges = split_pin_merges()
    out = {}
    for name, nodes in nets.items():
        for rp in nodes:
            if tuple(rp) in merges:
                out[name] = f"{rp[0]}-{rp[1]}"
    if len(out) != len(merges):
        raise SystemExit(f"p2k: SPLIT_PIN_MERGES pins {sorted(merges)} found on {len(out)} KiCad nets")
    return out


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
        name = n.get("name")
        if name.startswith("unconnected-(") and name.endswith(")") and "/" in name:
            # the board file writes "/" in these names as {slash}: keep the DRC parity equal
            name = "unconnected-(" + name[len("unconnected-("):-1].replace("/", "{slash}") + ")"
        nets[name] = [(nd.get("ref"), nd.get("pin")) for nd in n.findall("node")]
    return comps, nets


def drill_smaller_accepted(d_proteus, d_kicad, tol=0.005):
    """Reason from the DRILL_SMALLER config key ({(proteus_mm, kicad_mm): reason}) when a KiCad
    drill smaller than the Proteus drill is an accepted (recorded, reversible) decision; None
    otherwise. Both diameters are matched within tol (file roundings: 1.0668 / 1.066, KiCad's
    IPC-D-356 0.0001 inch quantum 1.5011 / 1.5)."""
    for (a, b), why in (getattr(cfg(), "DRILL_SMALLER", {}) or {}).items():
        if abs(a - d_proteus) <= tol and abs(b - d_kicad) <= tol:
            return why
    return None


# ---------------------------------------------------------------- recorded copper deviations

def nonconductor_layer(klayer):
    """Target layer of the NonConductor draws of a copper layer: NONCONDUCTOR_LAYERS
    ({copper layer: layer}, e.g. {"F.Cu": "F.SilkS"}) or the copper layer itself (default: copper
    graphics without net, exactly as Proteus drew them)."""
    return (getattr(cfg(), "NONCONDUCTOR_LAYERS", {}) or {}).get(klayer, klayer)


def removed_track_count(value):
    """(reason, count) of a REMOVED_TRACKS value: "reason" (exactly one draw) or (reason, n)."""
    if isinstance(value, tuple):
        why, n = value
        if not isinstance(n, int) or n < 1:
            raise SystemExit(f"REMOVED_TRACKS: count must be an integer >= 1, got {value!r}")
        return why, n
    return value, 1


def removed_track(klayer, x1, y1, x2, y2, tol=0.001):
    """(key, reason) when the Conductor draw is listed in REMOVED_TRACKS ({(layer, x1, y1, x2, y2):
    reason or (reason, count)}, IPC frame) - Proteus copper deliberately not built (e.g. dangling
    stubs); None otherwise. An entry in the drawn direction (within 1e-6 mm) is taken first, then
    any entry in either direction (within tol), so a zero-length or back-and-forth draw and its
    reverse can be listed as two entries. gerber_to_tracks requires each entry to match exactly
    `count` draws (1 for the plain form)."""
    entries = [(key, removed_track_count(v)[0]) for key, v in (getattr(cfg(), "REMOVED_TRACKS", {}) or {}).items()
               if key[0] == klayer]
    exact = 1e-6
    for key, why in entries:
        _, a1, b1, a2, b2 = key
        if abs(a1 - x1) <= exact and abs(b1 - y1) <= exact and abs(a2 - x2) <= exact and abs(b2 - y2) <= exact:
            return key, why
    for key, why in entries:
        _, a1, b1, a2, b2 = key
        for p, q in (((a1, b1), (a2, b2)), ((a2, b2), (a1, b1))):
            if abs(p[0] - x1) <= tol and abs(p[1] - y1) <= tol and abs(q[0] - x2) <= tol and abs(q[1] - y2) <= tol:
                return key, why
    return None


def removed_via(x, y, tol=0.01):
    """(key, reason) when a via at (x, y) (IPC frame) is listed in REMOVED_VIAS ({(x, y): reason}) -
    a Proteus via deliberately not built (its ViaPad flashes and drill are dropped, its IPC records
    are expected without a board via); None otherwise."""
    for key, why in (getattr(cfg(), "REMOVED_VIAS", {}) or {}).items():
        if abs(key[0] - x) <= tol and abs(key[1] - y) <= tol:
            return key, why
    return None


def hole_prefix():
    """Refdes prefix of the board-only mounting-hole footprints (HOLE_PREFIX, default "H")."""
    return getattr(cfg(), "HOLE_PREFIX", "H")


def hole_start():
    """Number of the first mounting hole (HOLE_START, default 1): holes are prefix + start, start + 1, ..."""
    return getattr(cfg(), "HOLE_START", 1)


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
        if fp.IsBoardOnly():
            continue   # mounting holes / free pads: never an IPC part pad (B1: a hole H4 next to a terminal H4)
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
