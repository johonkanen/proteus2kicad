#!/usr/bin/env python3
"""Create the KiCad project skeleton (one-shot; refuses to overwrite).

kicad/<PROJECT>.kicad_pro, sym-lib-table, root sheet <PROJECT>.kicad_sch with one
hierarchical sheet per Proteus PDF page, and empty sub-sheet files.

  p2k make_skeleton [--force]

Config keys: PROJECT, SHEETS = [(page, name, file), ...], SCH_TITLE, SCH_COMPANY, SCH_REV,
SCH_COMMENT (title block), SCH_NOTE (text on the root sheet).
"""
import json
import os
import subprocess
import sys
import uuid

from . import board
from .sexpr import Sym, save

CFG = board.config()
KDIR = board.path("kicad")
PROJECT = CFG.PROJECT
SHEETS = CFG.SHEETS


def uid():
    return str(uuid.uuid4())


def font(size=1.27):
    return [Sym("effects"), [Sym("font"), [Sym("size"), size, size]]]


def title_block(title):
    return [Sym("title_block"), [Sym("title"), title], [Sym("company"), CFG.SCH_COMPANY],
            [Sym("rev"), CFG.SCH_REV], [Sym("comment"), 1, CFG.SCH_COMMENT]]


def schematic(file_uuid, title, body=(), root=False):
    node = [Sym("kicad_sch"), [Sym("version"), Sym("20260101")], [Sym("generator"), "eeschema"],
            [Sym("generator_version"), "10.0"], [Sym("uuid"), file_uuid], [Sym("paper"), "A3"],
            title_block(title), [Sym("lib_symbols")], *body]
    if root:
        node.append([Sym("sheet_instances"), [Sym("path"), "/", [Sym("page"), "1"]]])
    node.append([Sym("embedded_fonts"), Sym("no")])
    return node


def sheet_block(root_uuid, i, page, name, fname):
    w, h = 76.2, 20.32
    x = 25.4 + (i % 3) * 127.0
    y = 38.1 + (i // 3) * 50.8
    return [Sym("sheet"), [Sym("at"), x, y], [Sym("size"), w, h],
            [Sym("exclude_from_sim"), Sym("no")], [Sym("in_bom"), Sym("yes")], [Sym("on_board"), Sym("yes")],
            [Sym("dnp"), Sym("no")],
            [Sym("stroke"), [Sym("width"), 0.1524], [Sym("type"), Sym("solid")]],
            [Sym("fill"), [Sym("color"), 0, 0, 0, 0]],
            [Sym("uuid"), uid()],
            [Sym("property"), "Sheetname", f"{page:02d} {name}", [Sym("at"), x, y - 0.7, 0],
             [Sym("effects"), [Sym("font"), [Sym("size"), 1.524, 1.524]], [Sym("justify"), Sym("left"), Sym("bottom")]]],
            [Sym("property"), "Sheetfile", fname, [Sym("at"), x, y + h + 0.6, 0],
             [Sym("effects"), [Sym("font"), [Sym("size"), 1.27, 1.27]], [Sym("justify"), Sym("left"), Sym("top")]]],
            [Sym("instances"), [Sym("project"), PROJECT, [Sym("path"), f"/{root_uuid}", [Sym("page"), str(page + 1)]]]]]


def main():
    force = "--force" in sys.argv
    os.makedirs(KDIR, exist_ok=True)
    targets = [f"{PROJECT}.kicad_pro", f"{PROJECT}.kicad_sch", "sym-lib-table"] + [s[2] for s in SHEETS]
    existing = [t for t in targets if os.path.exists(os.path.join(KDIR, t))]
    if existing and not force:
        sys.exit(f"make_skeleton: refusing to overwrite {existing} (use --force)")

    mount = subprocess.run(["bash", "-c", f"source {board.BIN}/kicad_env.sh >/dev/null && echo $KICAD_MOUNT"],
                           capture_output=True, text=True, check=True).stdout.strip()
    pro = json.load(open(os.path.join(mount, "share", "kicad", "template", "kicad.kicad_pro")))
    pro["meta"]["filename"] = f"{PROJECT}.kicad_pro"
    root_uuid = uid()
    pro["sheets"] = [[root_uuid, "Root"]]
    with open(os.path.join(KDIR, f"{PROJECT}.kicad_pro"), "w") as f:
        json.dump(pro, f, indent=2)
        f.write("\n")

    with open(os.path.join(KDIR, "sym-lib-table"), "w") as f:
        f.write(f'(sym_lib_table\n\t(version 7)\n\t(lib (name "{PROJECT}")(type "KiCad")'
                f'(uri "${{KIPRJMOD}}/../lib/{PROJECT}.kicad_sym")(options "")(descr "Project symbols, pin ids = IPC"))\n)\n')

    blocks = [sheet_block(root_uuid, i, *s) for i, s in enumerate(SHEETS)]
    note = [Sym("text"), CFG.SCH_NOTE,
            [Sym("exclude_from_sim"), Sym("no")], [Sym("at"), 25.4, 22.86, 0],
            [Sym("effects"), [Sym("font"), [Sym("size"), 2.0, 2.0]], [Sym("justify"), Sym("left"), Sym("bottom")]],
            [Sym("uuid"), uid()]]
    save(os.path.join(KDIR, f"{PROJECT}.kicad_sch"), schematic(root_uuid, CFG.SCH_TITLE, [note, *blocks], root=True))
    for page, name, fname in SHEETS:
        save(os.path.join(KDIR, fname), schematic(uid(), f"{CFG.SCH_TITLE} - {name}"))
    print(f"make_skeleton: wrote {len(targets)} files in kicad/")


if __name__ == "__main__":
    main()
