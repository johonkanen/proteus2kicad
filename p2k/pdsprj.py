"""Read what is readable from a Proteus project file (.pdsprj = zip of ROOT.DSN, ROOT.LYT, ...).

The design files are binary; strings in them are stored as 0xFF <length> <bytes>, and the
layout keeps per-part records as runs of printable text. Three things are extracted:

  dsn_values(prj)   {refdes: value}       schematic value of every part (COMPONENT ID label
                                          followed by its COMPONENT VALUE label) - the value
                                          printed in the schematic PDF
  lyt_parts(prj)    {refdes: {value, device, package, nets}}
                                          layout record: refdes, value, device, package, pin
                                          nets; anchored on the PACKAGE that follows the
                                          EID marker. The layout value can be stale - use
                                          dsn_values for values.
  dsn_pinouts(prj)  [{package, elements, pins: {name: number}, device}]
                                          device pinouts of the schematic's device library

`refs` is the set of known (full Proteus) refdes, e.g. `refdes_proteus` of ipc356_pads.json;
it disambiguates records. Stdlib only.
"""
import re
import zipfile


def _read(prj, name):
    with zipfile.ZipFile(prj) as z:
        return z.read(name)


def _strings(data, min_len=2):
    """Like `strings -n 2`: runs of printable ASCII (tab included), tab-split, empties dropped."""
    out = []
    for run in re.findall(rb"[\x20-\x7e\t]{%d,}" % min_len, data):
        out += [t.decode("ascii") for t in run.split(b"\t") if t]
    return out


def dsn_values(prj, refs):
    b = _read(prj, "ROOT.DSN")

    def pstr(p):
        n = b[p + 1]
        s = b[p + 2:p + 2 + n]
        return s.decode("latin-1") if n and all(32 <= c < 127 for c in s) else None

    out = {}
    for m in re.finditer(rb"COMPONENT ID\x00", b):
        i = m.start()
        ref = None
        for p in range(i - 1, max(0, i - 160), -1):
            if b[p] == 0xFF:
                s = pstr(p)
                if s and s.split(":")[0] in refs and p + 2 + len(s) <= i:
                    ref = s.split(":")[0]
                    break
        val = None
        for p in range(m.end(), m.end() + 40):
            if b[p] == 0xFF:
                val = pstr(p)
                if val:
                    break
        if ref:
            out.setdefault(ref, set()).add(val)
    multi = {r: v for r, v in out.items() if len(v) > 1}
    if multi:
        raise ValueError(f"pdsprj: parts with several schematic values: {multi}")
    return {r: v.pop() for r, v in out.items()}


def lyt_parts(prj, refs):
    toks = _strings(_read(prj, "ROOT.LYT"))
    out = {}
    for k, t in enumerate(toks):
        if t not in ("EID", "EID_A"):
            continue
        tail = toks[k + 1:k + 20]
        pkg = next((b for a, b in zip(tail, tail[1:]) if a == "PACKAGE"), None)
        j = next((j for j in range(k - 1, max(-1, k - 600), -1)
                  if toks[j] in refs and j + 3 < k and (pkg is None or toks[j + 3].startswith(pkg))), None)
        if j is None:
            continue
        rec = [t.strip() for t in toks[j:k]]
        if rec[0] in out:
            raise ValueError(f"pdsprj: two layout records for {rec[0]}")
        out[rec[0]] = {"value": rec[1], "device": rec[2], "package": pkg or rec[3], "nets": rec[4:]}
    return out


def dsn_pinouts(prj, devices=()):
    lines = _strings(_read(prj, "ROOT.DSN"))
    out = []
    for i, line in enumerate(lines):
        if not line.startswith("*PINOUT "):
            continue
        entry = {"package": line[8:], "elements": None, "pins": {}, "device": None}
        j = i + 1
        while j < len(lines) and lines[j].startswith("{"):
            m = re.match(r'\{PIN "(.*)" = ([^}]*)\}', lines[j])
            if m:
                entry["pins"][m.group(1)] = m.group(2)
            m = re.match(r"\{ELEMENTS=(\d+)\}", lines[j])
            if m:
                entry["elements"] = int(m.group(1))
            j += 1
        entry["device"] = next((lines[k] for k in range(i - 1, max(0, i - 60), -1) if lines[k] in devices), None)
        out.append(entry)
    return out
