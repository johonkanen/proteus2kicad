"""Generate / check lib/part_map.csv from the Proteus project (stdlib only).

The board's `tools/make_part_map.py` keeps only data and calls

    sys.exit(part_map.run(SPEC))          # tools/make_part_map.py [--check]

which writes (or with --check compares) lib/part_map.csv: one row per part, refdes -> PDF page,
symbol, value, Proteus device and package. Sources:

  refdes   reference/ipc356_pads.json (`refdes` = KiCad name, `refdes_proteus` = Proteus name),
           plus OFF_BOARD (drawn in the PDF, no IPC pads)
  value    schematic value from ROOT.DSN (pdsprj.dsn_values, read with the FULL refdes set)
  device, package   LAYOUT reader: "eid" = ROOT.LYT records (pdsprj.lyt_parts), "ref" =
           ROOT.LYT without EID markers (pdsprj.lyt_parts_ref: package only), "dsn" = the
           ROOT.DSN component record (pdsprj.dsn_devices)
  sheet    PDF page whose text layer has the refdes (pdftext); PAGE_OVERRIDES when ambiguous
  lib_id   "<PROJECT>:<symbol>"; the symbol from OFF_BOARD, SYMBOL_BY_REFDES, SYMBOL_BY_DEVICE
           (a device mapped to None = by value), SYMBOL_BY_VALUE, SYMBOL_BY_PACKAGE,
           SYMBOL_BY_PREFIX (leading letters of the refdes), in this order. With
           SYMBOL_BY_DEVICE, a device that is not in it is an error.

SPEC keys (dict; paths relative to the board root):
  PRJ               the .pdsprj file (required)
  PDF               the schematic PDF (required)
  LAYOUT            "eid" (default) | "ref" | "dsn"
  LATIN1            read Latin-1 strings (e.g. µ) in ROOT.DSN / ROOT.LYT (default False)
  SYMBOL_BY_DEVICE, SYMBOL_BY_VALUE, SYMBOL_BY_REFDES, SYMBOL_BY_PACKAGE, SYMBOL_BY_PREFIX
                    {key: symbol name in the project library}
  OFF_BOARD         {Proteus refdes: (KiCad refdes, symbol[, device])}: parts drawn in the PDF
                    without IPC pads (off_board = "yes", package ""); the device is read from
                    the layout reader when not given; with LAYOUT "eid" / "ref" such a part must
                    have no layout record
  PART_OVERRIDES    {Proteus refdes: {"value" / "device" / "package": text}}: what the Proteus
                    files do not say (or say wrong, e.g. a fitted part); an entry that equals
                    what pdsprj reads fails (drop it)
  PAGE_OVERRIDES    {Proteus refdes: page} for refdes with several / no PDF text hits
  PAGE_CHECK        True (default): an override page must be among the PDF hits of the refdes
  UNIT_PAGES        {KiCad refdes: "A:2 B:5 ..."} -> column `units`
  FIELDS            output columns, from: refdes sheet lib_id value device package
                    proteus_refdes off_board units (default: the first seven)
  COMMENT           text of the first line (after "# ")
  CHECK_UNUSED_DEVICES  True: a SYMBOL_BY_DEVICE entry that no part uses fails (default False)

Rows are sorted by sheet, then natural refdes order. The file is UTF-8, "\\n" line ends.
"""
import csv
import io
import json
import os
import re
import sys

from . import board, pdftext, pdsprj

DEFAULT_FIELDS = ["refdes", "sheet", "lib_id", "value", "device", "package", "proteus_refdes"]
ALL_FIELDS = DEFAULT_FIELDS + ["off_board", "units"]
KNOWN = {"PRJ", "PDF", "LAYOUT", "LATIN1", "SYMBOL_BY_DEVICE", "SYMBOL_BY_VALUE", "SYMBOL_BY_REFDES",
         "SYMBOL_BY_PACKAGE", "SYMBOL_BY_PREFIX", "OFF_BOARD", "PART_OVERRIDES", "PAGE_OVERRIDES",
         "PAGE_CHECK", "UNIT_PAGES", "FIELDS", "COMMENT", "CHECK_UNUSED_DEVICES"}


def natkey(s):
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.findall(r"\d+|\D+", s)]


def _path(p):
    return p if os.path.isabs(p) else board.path(p)


def build(spec):
    """(csv text, rows). Exits with the list of errors if any part cannot be mapped."""
    unknown = set(spec) - KNOWN
    if unknown:
        raise SystemExit(f"part_map: unknown spec key(s) {sorted(unknown)}")
    prj, pdf = _path(spec["PRJ"]), _path(spec["PDF"])
    layout = spec.get("LAYOUT", "eid")
    if layout not in ("eid", "ref", "dsn"):
        raise SystemExit(f"part_map: LAYOUT {layout!r} (eid, ref or dsn)")
    latin1 = spec.get("LATIN1", False)
    by_dev = spec.get("SYMBOL_BY_DEVICE") or {}
    by_val = spec.get("SYMBOL_BY_VALUE") or {}
    by_ref = spec.get("SYMBOL_BY_REFDES") or {}
    by_pkg = spec.get("SYMBOL_BY_PACKAGE") or {}
    by_pre = spec.get("SYMBOL_BY_PREFIX") or {}
    off_board = spec.get("OFF_BOARD") or {}
    overrides = spec.get("PART_OVERRIDES") or {}
    page_over = spec.get("PAGE_OVERRIDES") or {}
    unit_pages = spec.get("UNIT_PAGES") or {}
    fields = spec.get("FIELDS", DEFAULT_FIELDS)
    bad = [f for f in fields if f not in ALL_FIELDS]
    if bad:
        raise SystemExit(f"part_map: unknown FIELDS {bad} (known: {ALL_FIELDS})")
    lib = board.config().PROJECT

    ipc = json.load(open(board.path("reference", "ipc356_pads.json")))
    kicad = {p["refdes_proteus"]: p["refdes"] for p in ipc["pads"] if p["kind"] == "pad"}
    errors = []
    for ref, entry in off_board.items():
        if ref in kicad:
            errors.append(f"OFF_BOARD part {ref} has IPC pads")
        kicad[ref] = entry[0]
    refs = set(kicad)
    values = pdsprj.dsn_values(prj, refs, latin1=latin1)
    if layout == "eid":
        parts = pdsprj.lyt_parts(prj, refs, latin1=latin1)
    elif layout == "ref":
        parts = pdsprj.lyt_parts_ref(prj, refs, values)
    else:
        parts = {r: {"device": d, "package": p}
                 for r, (d, p) in pdsprj.dsn_devices(prj, refs, latin1=latin1).items()}
    pages = pdftext.refdes_pages(pdftext.pdf_words(pdf), refs)

    for ref in sorted(set(overrides) - refs):
        errors.append(f"PART_OVERRIDES: {ref} is not a refdes")
    for ref in sorted(set(page_over) - refs):
        errors.append(f"PAGE_OVERRIDES: {ref} is not a refdes")
    rows = []
    for ref in sorted(refs):
        page = page_over.get(ref)
        if page is None:
            if len(pages[ref]) != 1:
                errors.append(f"{ref}: PDF pages {pages[ref]} (add to PAGE_OVERRIDES)")
                continue
            page = pages[ref][0]
        elif spec.get("PAGE_CHECK", True) and page not in pages[ref]:
            errors.append(f"{ref}: PAGE_OVERRIDES page {page} not among the PDF hits {pages[ref]}")
        rec = parts.get(ref)
        if ref in off_board:
            if rec is not None and layout != "dsn":
                errors.append(f"{ref}: OFF_BOARD part has a layout record")
            entry = off_board[ref]
            dev = entry[2] if len(entry) > 2 else (rec or {}).get("device")
            read = {"value": values.get(ref), "device": dev, "package": ""}
        else:
            if rec is None and layout != "dsn" and ref not in overrides:
                errors.append(f"{ref}: no layout record in ROOT.LYT")
                continue
            rec = rec or {}
            read = {"value": values.get(ref), "device": rec.get("device"), "package": rec.get("package")}
        fix = overrides.get(ref, {})
        for k, v in fix.items():
            if k not in ("value", "device", "package"):
                errors.append(f"PART_OVERRIDES[{ref}]: unknown field {k!r}")
            elif read.get(k) == v:
                errors.append(f"PART_OVERRIDES[{ref}]: pdsprj reads {k} {v!r} itself, drop the entry")
        cur = {**read, **{k: v for k, v in fix.items() if k in read}}
        value, dev, pkg = cur["value"], cur["device"], cur["package"]
        if not value:
            errors.append(f"{ref}: no schematic value")
            continue
        if layout != "ref" and not dev:
            errors.append(f"{ref}: no device")
            continue
        if ref not in off_board and not pkg:
            errors.append(f"{ref}: no package")
            continue
        sym = None
        if ref in off_board:
            sym = off_board[ref][1]
        elif kicad[ref] in by_ref or ref in by_ref:
            sym = by_ref.get(ref, by_ref.get(kicad[ref]))
        else:
            if by_dev:
                if dev not in by_dev:
                    errors.append(f"{ref}: no symbol for Proteus device {dev!r}")
                    continue
                sym = by_dev[dev]
                if sym is None:
                    sym = by_val.get(value)
                    if sym is None:
                        errors.append(f"{ref}: no symbol for Proteus device {dev!r} (value {value!r})")
                        continue
            if sym is None:
                pre = re.match(r"[A-Za-z_]*", ref).group(0)
                sym = by_val.get(value) or by_pkg.get(pkg) or by_pre.get(pre)
            if sym is None:
                errors.append(f"{ref}: no symbol for value {value!r} (device {dev!r}, package {pkg!r})")
                continue
        rows.append({"refdes": kicad[ref], "sheet": page, "lib_id": f"{lib}:{sym}", "value": value,
                     "device": dev or "", "package": pkg or "", "proteus_refdes": ref if ref != kicad[ref] else "",
                     "off_board": "yes" if ref in off_board else "", "units": unit_pages.get(kicad[ref], "")})
    for ref in sorted(set(unit_pages) - {r["refdes"] for r in rows}):
        errors.append(f"UNIT_PAGES: {ref} is not a refdes")
    if spec.get("CHECK_UNUSED_DEVICES"):
        for dev in sorted(set(by_dev) - {r["device"] for r in rows}):
            errors.append(f"SYMBOL_BY_DEVICE: device {dev!r} is not used")
    if errors:
        raise SystemExit("make_part_map:\n  " + "\n  ".join(errors))
    rows.sort(key=lambda r: (r["sheet"], natkey(r["refdes"])))
    buf = io.StringIO()
    if spec.get("COMMENT"):
        buf.write(f"# {spec['COMMENT']}\n")
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue(), rows


def run(spec, argv=None):
    """Write lib/part_map.csv, or with --check fail (1) if it is not up to date."""
    argv = sys.argv[1:] if argv is None else argv
    text, rows = build(spec)
    out = board.path("lib", "part_map.csv")
    rel = os.path.relpath(out, board.root())
    per = {}
    for r in rows:
        per[r["sheet"]] = per.get(r["sheet"], 0) + 1
    summary = f"{len(rows)} parts ({sum(not r['off_board'] for r in rows)} on the board); per sheet " + \
        " ".join(f"{s}:{n}" for s, n in sorted(per.items()))
    if "--check" in argv:
        if not os.path.exists(out) or open(out, encoding="utf-8").read() != text:
            print(f"make_part_map: {rel} is out of date, run tools/make_part_map.py")
            return 1
        print(f"make_part_map: {rel} up to date, {summary}")
        return 0
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    print(f"wrote {rel}: {summary}")
    return 0
