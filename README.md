# proteus2kicad

Tools for migrating a Proteus PCB project to KiCad 10 and **proving** the result
electrically identical to the original: the schematic is redrawn from the Proteus PDF, the
PCB is rebuilt from the Proteus Gerber X2 export, and every check compares against the
IPC-D-356A netlist of the CADCAM export.

Extracted from the `ac_psu_kicad` migration (single_phase_inverter_psu_v3) and used by
`tube_psu_kicad` (tube_power_5_1). The method is described in the playbook of
`ac_psu_kicad` (`CONVERSION_PLAYBOOK.md`; to be moved here).

## Use in a board repository

Add as a submodule (pinned commit, so a tool change never silently changes a finished board):

```
git submodule add https://github.com/johonkanen/proteus2kicad.git tools/p2k
```

Put a `p2k_config.py` at the board repo root (see below) and run tools from anywhere inside
the board repo (or set `P2K_BOARD=<board root>`):

```
tools/p2k/bin/p2k ipc356 --check          # stdlib tools: system python3
tools/p2k/bin/kipy -m p2k.<tool> ...      # tools that need pcbnew: KiCad's python
tools/p2k/bin/kicad-cli ...               # KiCad's kicad-cli
```

`kipy` and `kicad-cli` mount the KiCad AppImage on demand (`bin/kicad_env.sh`,
`$KICAD_APPIMAGE`, default `~/kicad/kicad-10.0.7-x86_64.AppImage`). Never run the AppImage
with `--version`: it starts the GUI and hangs. Sheet scripts in the board repo import
`from p2k.sch_builder import Sheet` and run with `tools/p2k/bin/p2k`'s `PYTHONPATH`
(or `PYTHONPATH=tools/p2k`).

The board repo keeps what is drawn or decided per board: sheet scripts, symbol list,
footprint rules and project footprints, `lib/`, `reference/`, `kicad/`, `reports/`, its
`tools/check_all.sh` (a list of the steps it runs) and PLAN/STATUS.

## Tools

| Tool | What | Needs |
|---|---|---|
| `ipc356` | IPC-D-356A → `reference/ipc356_pads.{json,csv}`, `pin_nets_by_component.md`, `pins_by_net.md`; `--check` = counts + fix-ups + files up to date | python3 |
| `check_sources` | Gerber X2 export vs IPC: via / component / free / SMD pad flashes and drills 1:1, one `SameCoordinates` id, outline | python3 |
| `render_sheet` | schematic sheet → PNG (kicad-cli SVG + librsvg/cairo) | python3, kicad-cli |
| `board` | board root and config lookup | library |
| `sexpr`, `geom2d`, `gerber`, `fpgeom`, `sch_builder` | s-expressions, 2-D geometry, Gerber X2 reader, footprint fit, schematic writer | library |

Still only in `ac_psu_kicad/tools`, moved here when the next board needs them (with
ac_psu as the regression test): `gen_symbols`, `check_symbols`, `compare_netlists`,
`make_skeleton`, `gen_footprints`, `check_footprints`, `build_pcb`, `gerber_to_tracks`,
`assign_nets`, `pours_to_zones`, `check_board`, `drc`, `gerber_roundtrip`, `ipc356_diff`,
`board_common`, `erc.sh`, `make_pcb.sh`.

## `p2k_config.py`

Plain Python at the board repo root (no TOML: the system python3 may be < 3.11).

| Key | Used by | Meaning |
|---|---|---|
| `PROJECT` | sch_builder | KiCad project / symbol library name |
| `CADCAM` | check_sources | directory of the Gerber X2 export (`* - CADCAM <name>.GBR`) |
| `IPC` | ipc356 | IPC-D-356A netlist file |
| `TRUNCATED_REFDES` | ipc356 | 6-char refdes → full refdes; key `raw` or `(raw, x_um, y_um)` when two parts share a truncated name (then every pad of that part needs a key) |
| `TRUNCATED_NETS` | ipc356 | optional: expected net per restored refdes |
| `KICAD_REFDES` | ipc356 | optional: Proteus refdes → KiCad refdes (spaces, leading +/−) |
| `NET_ALIASES` | ipc356 | e.g. `{"GND=POWER": "GND"}` |
| `VIA_DRILLS`, `FREE_PAD_DRILLS` | ipc356 | blank refdes + net: via if drill in `VIA_DRILLS`, else free pad (must be in `FREE_PAD_DRILLS`) |
| `HOLE_DRILLS`, `HOLES_PLATED` | ipc356 | blank refdes, no net: mounting holes |
| `EXPECTED_IPC` | ipc356 | counts asserted by `--check` (nets, records, refdes_raw, tht/smd records, smd_top/bottom, nc_pads, vias, free_pads, holes, parts) |
| `COPPER` | check_sources, PCB tools | `[(gerber name, KiCad layer, User layer for pour outlines), ...]` top to bottom |
| `DRILL_PLATED`, `DRILL_NONPLATED`, `OUTLINE` | check_sources | gerber names of the drill files and the outline |
| `OUTLINE_RECT` | check_sources | `(x, y, width, height)` mm of the board outline |

Coordinates are mm in the IPC / Gerber frame (Y up); KiCad (x, y) = (x, −y).
