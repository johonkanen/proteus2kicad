"""Read what is readable from a Proteus project file (.pdsprj = zip of ROOT.DSN, ROOT.LYT, ...).

The design files are binary; strings in them are stored as 0xFF <length> <bytes>, and the
layout keeps per-part records as runs of printable text. Extracted:

  dsn_values(prj)   {refdes: value}       schematic value of every part (COMPONENT ID label
                                          followed by its COMPONENT VALUE label) - the value
                                          printed in the schematic PDF
  dsn_devices(prj)  {refdes: (device, package)}
                                          schematic component record: library device (first
                                          string after the COMPONENT VALUE label) and its
                                          {PACKAGE=...} property (None if it has none)
  lyt_parts(prj)    {refdes: {value, device, package, nets}}
                                          layout record: refdes, value, device, package, pin
                                          nets; anchored on the PACKAGE that follows the
                                          EID marker. The layout value can be stale - use
                                          dsn_values for values.
  lyt_parts_ref(prj, refs, values)  {refdes: {package}}
                                          layout records of a ROOT.LYT without EID markers
                                          (Proteus 8.12 SP2): `<package> <refdes><junk> ...
                                          PART REFERENCE <value><junk> ...`
  dsn_pinouts(prj)  [{package, elements, pins: {name: number}, device}]
                                          package pinouts (*PINOUT blocks) of the schematic's
                                          device library
  dsn_device_pins(prj)  [{device, desc, package, pins, pinouts, numbers}]
                                          the device symbols' own pin records ($PINSHORT /
                                          $PINDEFAULT / ...) per {*DEVICE} block, with the
                                          package pinout applied

`refs` is the set of known (full Proteus) refdes, e.g. `refdes_proteus` of ipc356_pads.json;
it disambiguates records. Stdlib only.

Options (all off by default; the defaults read exactly what this module always read):
  latin1=True      dsn_values / dsn_devices / lyt_parts / _strings: bytes 0xA0-0xFF are part of
                   a string and decoded as Latin-1 (e.g. "10µ/400V"); lyt_parts strips a
                   trailing 0xFF pad byte of a net token
  brace_optional   dsn_pinouts: `PIN "name" = ...}` lines without the leading "{" are read too,
                   and every pin value is the list of its elements (one per unit)
One-character refdes (A-D terminals) in `refs` are anchored to the record structure: in the
DSN only the string directly before the COMPONENT ID label's font record is the refdes; in the
LYT one-character tokens are kept (otherwise "D" of "Default Font" or any one-letter string
matched).
"""
import re
import struct
import zipfile

_FONT = re.compile(rb"[\x20-\x7e]+\x00")   # the label's font name, directly before COMPONENT ID


def _read(prj, name):
    with zipfile.ZipFile(prj) as z:
        return z.read(name)


def _strings(data, min_len=2, latin1=False):
    """Like `strings -n 2`: runs of printable ASCII (tab included), tab-split, empties dropped.
    latin1=True: bytes 0xA0-0xFF belong to the runs too (decoded as Latin-1)."""
    out = []
    if latin1:
        for run in re.findall(rb"[\x20-\x7e\t\xa0-\xff]{%d,}" % min_len, data):
            out += [t.decode("latin-1") for t in run.split(b"\t") if t]
        return out
    for run in re.findall(rb"[\x20-\x7e\t]{%d,}" % min_len, data):
        out += [t.decode("ascii") for t in run.split(b"\t") if t]
    return out


def _pstr_reader(b, latin1):
    def ok(c):
        return 32 <= c < 127 or (latin1 and c >= 0xA0)

    def pstr(p):
        n = b[p + 1]
        s = b[p + 2:p + 2 + n]
        return s.decode("latin-1") if n and all(ok(c) for c in s) else None
    return pstr


def _components(b, refs, pstr):
    """(refdes, COMPONENT ID match) of every component label of a known refdes. The refdes is
    the nearest 0xFF string before the label that names a known part; a one-character refdes
    only counts in the refdes slot (the first string before the label's font record)."""
    for m in re.finditer(rb"COMPONENT ID\x00", b):
        i = m.start()
        ref = None
        slot = True
        for p in range(i - 1, max(0, i - 160), -1):
            if b[p] != 0xFF:
                continue
            s = pstr(p)
            if not s or p + 2 + len(s) > i:
                continue
            r = s.split(":")[0]
            if len(r) == 1:
                if slot and _FONT.fullmatch(b, p + 2, i):
                    continue          # the label's font record ("D" of "Default Font")
                if slot and r in refs:
                    ref = r
                    break
            elif r in refs:
                ref = r
                break
            slot = False
        if ref:
            yield ref, m


def dsn_values(prj, refs, latin1=False):
    b = _read(prj, "ROOT.DSN")
    pstr = _pstr_reader(b, latin1)
    out = {}
    for ref, m in _components(b, refs, pstr):
        val = None
        for p in range(m.end(), m.end() + 40):
            if b[p] == 0xFF:
                val = pstr(p)
                if val:
                    break
        out.setdefault(ref, set()).add(val)
    multi = {r: v for r, v in out.items() if len(v) > 1}
    if multi:
        raise ValueError(f"pdsprj: parts with several schematic values: {multi}")
    return {r: v.pop() for r, v in out.items()}


def dsn_devices(prj, refs, latin1=False):
    """{refdes: (device, package)} from the ROOT.DSN component records: the device is the first
    string after the COMPONENT VALUE label, the package the {PACKAGE=...} property that follows
    (None for parts without one, e.g. off-board parts). Same refdes search as dsn_values.
    For layouts without EID records (lyt_parts finds nothing)."""
    b = _read(prj, "ROOT.DSN")
    pstr = _pstr_reader(b, latin1)
    out = {}
    for ref, m in _components(b, refs, pstr):
        mv = b.find(b"COMPONENT VALUE\x00", m.end(), m.end() + 200)
        dev = None
        for p in range(mv + 16, mv + 60):
            if b[p] == 0xFF:
                dev = pstr(p)
                if dev:
                    break
        pk = re.search(rb"\{PACKAGE=([^}]*)\}", b[mv:mv + 400])
        out.setdefault(ref, set()).add((dev, pk.group(1).decode("latin-1") if pk else None))
    multi = {r: v for r, v in out.items() if len(v) > 1}
    if multi:
        raise ValueError(f"pdsprj: parts with several component records: {multi}")
    return {r: v.pop() for r, v in out.items()}


def lyt_parts(prj, refs, latin1=False):
    min_len = 1 if any(len(r) == 1 for r in refs) else 2
    toks = _strings(_read(prj, "ROOT.LYT"), min_len=min_len, latin1=latin1)
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
        nets = [n.rstrip("\xff") for n in rec[4:]] if latin1 else rec[4:]
        out[rec[0]] = {"value": rec[1], "device": rec[2], "package": pkg or rec[3], "nets": nets}
    return out


def lyt_parts_ref(prj, refs, values):
    """{refdes: {"package": package}} from a ROOT.LYT without EID markers (Proteus 8.12 SP2).

    A record is `<package> <refdes><junk> ... PART REFERENCE <value><junk> ... PART VALUE
    <nets>`; the refdes token often carries binary junk, so it is matched as the longest known
    refdes the token starts with, and accepted only when the value token after PART REFERENCE
    starts with the schematic value (`values` = dsn_values). The package is the token before
    the refdes."""
    toks = _strings(_read(prj, "ROOT.LYT"))
    out = {}
    for k, t in enumerate(toks):
        if t != "PART REFERENCE" or k + 1 >= len(toks):
            continue
        for j in range(k - 1, max(0, k - 7), -1):
            cand = [r for r in refs if toks[j].strip().startswith(r)]
            if not cand:
                continue
            ref = max(cand, key=len)
            if toks[k + 1].strip().startswith(values[ref]):
                if ref in out:
                    raise ValueError(f"pdsprj: two layout records for {ref}")
                out[ref] = {"package": toks[j - 1].strip()}
                break
    return out


def dsn_pinouts(prj, devices=(), brace_optional=False):
    lines = _strings(_read(prj, "ROOT.DSN"))
    out = []
    for i, line in enumerate(lines):
        if not line.startswith("*PINOUT "):
            continue
        entry = {"package": line[8:], "elements": None, "pins": {}, "device": None}
        j = i + 1
        while j < len(lines) and (lines[j].startswith("{") or (brace_optional and lines[j].startswith("PIN "))):
            if brace_optional:
                m = re.match(r'\{?PIN "(.*)" = ([^}]*)\}', lines[j])
                if m:
                    entry["pins"][m.group(1)] = m.group(2).split(",")
            else:
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


_PIN_REC = re.compile(rb"\$(PIN[A-Z]*)\x00([\x20-\x7e\xa0-\xff]*)\x00([\x20-\x7e\xa0-\xff]*)\x00")


def _pascal(b, p):
    """Latin-1 string <length> <bytes> at p, or None if it is not printable."""
    if p >= len(b) or not b[p]:
        return None
    s = b[p + 1:p + 1 + b[p]]
    return s.decode("latin-1") if all(32 <= c < 127 or c >= 0xA0 for c in s) else None


def dsn_device_pins(prj):
    """Pins of the library devices of ROOT.DSN, one entry per {*DEVICE} block, in file order:

      device   device name (the length-prefixed string that starts the device record: after
               the last 0xFFFE marker for the first device, right after the previous block's
               text for the others; the device library ends where no record follows a block);
               None when it cannot be read
      desc     {DESC=...} of the block, or None
      package  the device's default package ({PACKAGE=...} after {*COMPONENT}), or None
      pins     [(kind, name, number)] of the symbol's pin records in record order: kind =
               PINSHORT / PINDEFAULT / PINBUS / ..., number = symbol pin number ("" if none).
               One-character names (A, K, B, C, E, G, S, D) are kept
      pinouts  {package: {pin name: number}} of the block's *PINOUT tables (raw element string)
      numbers  {pin name: number}: the default package's pinout number when it lists the pin
               (it overrides the symbol number, e.g. 12AX7 $PINDEFAULT A1 6 vs package A1 = 1),
               else the symbol number (None if empty)
    """
    b = _read(prj, "ROOT.DSN")
    blocks = []
    for m in re.finditer(rb"\{\*DEVICE\}", b):
        d = m.start()
        (n,) = struct.unpack("<I", b[d - 4:d])
        blocks.append((d, d + n))
    out = []
    prev = None
    for d, e in blocks:
        if prev is None:     # first device: after the last 0xFFFE marker with a name
            start = next((k + 2 for k in sorted((m.start() for m in re.finditer(rb"\xfe\xff", b[:d])), reverse=True)
                          if _pascal(b, k + 2)), None)
        elif _pascal(b, prev):
            start = prev
        else:
            break            # the library ends; later "{*DEVICE}" texts are component properties
        name = _pascal(b, start) if start is not None else None
        seg = b[start if start is not None else 0:d]
        pins = [(k.decode(), nm.decode("latin-1"), num.decode("latin-1")) for k, nm, num in _PIN_REC.findall(seg)]
        text = b[d:e].decode("latin-1")
        desc = re.search(r"^\{?DESC=(.*?)\}?$", text, re.M)
        comp = text.split("{*COMPONENT}", 1)
        pkg = re.search(r"^\{PACKAGE=([^}\n]*)\}", comp[1], re.M) if len(comp) > 1 else None
        pinouts = {}
        cur = None
        for line in text.splitlines():
            if line.startswith("*PINOUT "):
                cur = pinouts.setdefault(line[8:].strip(), {})
                continue
            mp = re.match(r'\{?PIN "(.*)" = ([^}]*)\}?$', line)
            if mp and cur is not None:
                cur[mp.group(1)] = mp.group(2)
            elif not line.startswith("{ELEMENTS"):
                cur = None
        package = pkg.group(1) if pkg else None
        table = pinouts.get(package, {})
        numbers = {nm: table.get(nm, num or None) for _, nm, num in pins}
        out.append({"device": name, "desc": desc.group(1) if desc else None, "package": package,
                    "pins": pins, "pinouts": pinouts, "numbers": numbers})
        prev = e
    return out
