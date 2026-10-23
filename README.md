# proteus2kicad

Tools for migrating a Proteus PCB project to KiCad 10 and **proving** the result
electrically identical to the original: the schematic is redrawn from the Proteus PDF, the
PCB is rebuilt from the Proteus Gerber X2 export, and every check compares against the
IPC-D-356A netlist of the CADCAM export.

Extracted from the `ac_psu_kicad` migration (single_phase_inverter_psu_v3) and used by
`tube_psu_kicad` (tube_power_5_1). The method – phases, checks, pitfalls, model and session
plan, regression test for tool changes – is in [`CONVERSION_PLAYBOOK.md`](CONVERSION_PLAYBOOK.md).

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
with `--version`: it starts the GUI and hangs. Board scripts (`tools/gen_symbols.py`, `tools/sheets/sheetNN.py`)
put `tools/p2k` on `sys.path` and import `p2k.symgen` / `p2k.sheetkit`.

`lib/part_map.csv` (read by check_symbols and compare_netlists): columns `refdes` (KiCad
refdes), `sheet` (PDF page), `lib_id`, `value`; more columns allowed; `#` lines are comments.

The board repo keeps what is drawn or decided per board: sheet scripts, symbol list,
footprint rules and project footprints, `lib/`, `reference/`, `kicad/`, `reports/`, its
`tools/check_all.sh` (a list of the steps it runs) and PLAN/STATUS.

## Tools

| Tool | What | Needs |
|---|---|---|
| `ipc356` | IPC-D-356A → `reference/ipc356_pads.{json,csv}`, `pin_nets_by_component.md`, `pins_by_net.md`; `--check` = counts + fix-ups + files up to date | python3 |
| `check_sources` | Gerber X2 export vs IPC: via / component / free / SMD pad flashes and drills 1:1, one `SameCoordinates` id, outline | python3 |
| `check_symbols` | every refdes of `lib/part_map.csv`: symbol pin set = IPC pin set; power symbols are IPC nets | python3 |
| `compare_netlists` | KiCad schematic netlist vs IPC by pad sets (`--sheet N`, `--allow-missing` while drawing) | python3, kicad-cli |
| `make_skeleton` | one-shot: KiCad project, sym-lib-table, root sheet with one sub-sheet per PDF page | python3, AppImage |
| `footprints` | library for the board's `tools/gen_footprints.py`: `run(RULES, PROJECT)` assigns a footprint to every IPC part (symbol + pad pattern rules), writes `lib/footprint_map.csv` and `lib/<PROJECT>.pretty` (project footprints derived from KiCad footprints or built from the IPC pads, plus `board_only` one-pad footprints for mounting holes / free pads); `--check` | python3, AppImage mount |
| `check_footprints` | every symbol's footprint vs the IPC pads: pads matched by number, rigid fit (residual < 0.05 mm), `reports/footprint_fit.csv` (placement for Phase 3) and `footprint_pads.csv`; `--pcbnew` re-measures after placing with pcbnew | python3 (`kipy -m p2k.check_footprints --pcbnew` for pcbnew) |
| `render_sheet` | schematic sheet → PNG (kicad-cli SVG + librsvg/cairo) | python3, kicad-cli |
| `board` | board root and config lookup | library |
| `symgen` | helpers for the board's `tools/gen_symbols.py`: `from_std` (KiCad std symbol, pins renumbered), `box`, `header`, `power`, `run(build)` (write / `--check`) | library |
| `sheetkit` | helpers for the board's sheet scripts: grid, labels, power symbols, two-pin parts, terminals | library |
| `pdsprj` | read the Proteus project: schematic values (ROOT.DSN), layout part records (ROOT.LYT: value, device, package, nets), device pinouts | library |
| `pdftext` | words of the schematic PDF with positions (pdftotext -bbox), page of each refdes | library |
| `sexpr`, `geom2d`, `gerber`, `fpgeom`, `sch_builder` | s-expressions, 2-D geometry, Gerber X2 reader, footprint fit, schematic writer | library |

| `pcb` | Phase 3 library: board / schematic / reports paths (config, `P2K_PCB` / `P2K_SCHEMATIC` / `P2K_REPORTS`, or `configure()` from a board wrapper, which can also filter the IPC records), kicadxml netlist export, IPC → KiCad net map | library |
| `build_pcb` | board from the schematic netlist: footprints with nets and symbol links at the `footprint_fit.csv` positions, layers, rules, outline, board-only mounting holes and free pads | `kipy -m p2k.build_pcb` |
| `gerber_to_tracks` | copper gerbers → tracks (pour edge strokes skipped), vias (flash size + drill; stacked duplicates merged), NonConductor graphics, pour outlines on the User layers; every pad flash / drill must land on a footprint pad | `kipy -m p2k.gerber_to_tracks` |
| `assign_nets` | union-find over the copper: nets for tracks / vias / pours (`reports/pour_nets.json`), shorts / orphans / opens; `--check` repeats on the saved board after zone fill | `kipy -m p2k.assign_nets [--check]` |
| `pours_to_zones` | Proteus pours (region + stroke − clear holes) → filled zones | `kipy -m p2k.pours_to_zones` |
| `check_board` | saved board vs IPC: pads, mounting holes, free pads, vias (position, size, net 1:1), every copper item has a net; `main(added=, extra_vias=)` for later design revisions | `kipy -m p2k.check_board` |
| `drc` | `kicad-cli pcb drc --schematic-parity`: fails on shorts, unconnected, parity and error types not in `DRC_ACCEPTED` | `p2k drc` |
| `ipc356_diff` | Phase 4: `kicad-cli pcb export ipcd356` of the board vs the Proteus IPC-D-356A: every pad / via / hole / free pad matched within 0.05 mm, side, THT/SMD, plating, drill (KiCad larger OK, smaller only per `DRILL_SMALLER`), nets 1:1 by pad sets, N/C equal, stacked via records (`STACKED_VIAS`); `main(added=, extra_vias=)` for later revisions | `p2k ipc356_diff` |
| `gerber_roundtrip` | Phase 4: KiCad copper gerbers + drills vs the Proteus gerbers: raster XOR per layer with tolerance, difference clusters classified (pad shape / clearance / zone fill / deviation / other = finding), centre-line coverage of every draw and flash both ways, plated + non-plated drills (stacked records, `DRILL_SMALLER`), optional Excellon cross-check (`DRILL_EXCELLON`); `reports/roundtrip/` report + overlay PNGs; `--check` fails on other / uncovered / drill mismatch; `main(in_area=, extra_vias=)` | `p2k gerber_roundtrip [--check]` (numpy, scipy, PIL) |

The board repo runs the PCB steps in its `tools/make_pcb.sh` (build_pcb, gerber_to_tracks,
assign_nets, pours_to_zones). `erc.sh`, `check_all.sh` and `make_pcb.sh` stay in each board
repo.

`kipy -m p2k.TOOL` runs a tool under KiCad's python (the AppImage python ignores
`PYTHONPATH`, so `kipy` inserts the package through `runpy`); `kipy SCRIPT.py` runs a board
script, which puts `tools/p2k` on `sys.path` itself.

## `p2k_config.py`

Plain Python at the board repo root (no TOML: the system python3 may be < 3.11).

| Key | Used by | Meaning |
|---|---|---|
| `PROJECT` | sch_builder, symgen, check_symbols, compare_netlists, make_skeleton | KiCad project / symbol library name |
| `SHEETS` | make_skeleton | `[(PDF page, sheet name, file), ...]` |
| `SCH_TITLE`, `SCH_COMPANY`, `SCH_REV`, `SCH_COMMENT`, `SCH_NOTE` | make_skeleton | title block and root-sheet note |
| `CADCAM` | check_sources | directory of the Gerber X2 export (`* - CADCAM <name>.GBR`) |
| `IPC` | ipc356 | IPC-D-356A netlist file |
| `TRUNCATED_REFDES` | ipc356 | 6-char refdes → full refdes; key `raw` or `(raw, x_um, y_um)` when two parts share a truncated name (then every pad of that part needs a key) |
| `TRUNCATED_NETS` | ipc356 | optional: expected net per restored refdes |
| `KICAD_REFDES` | ipc356 | optional: Proteus refdes → KiCad refdes (spaces, leading +/−) |
| `NET_ALIASES` | ipc356 | e.g. `{"GND=POWER": "GND"}` |
| `VIA_DRILLS`, `FREE_PAD_DRILLS` | ipc356 (`VIA_DRILLS` also gerber_to_tracks) | blank refdes + net: via if drill in `VIA_DRILLS`, else free pad (must be in `FREE_PAD_DRILLS`) |
| `HOLE_DRILLS`, `HOLES_PLATED` | ipc356 | blank refdes, no net: mounting holes |
| `EXPECTED_IPC` | ipc356 | counts asserted by `--check` (nets, records, refdes_raw, tht/smd records, smd_top/bottom, nc_pads, vias, free_pads, holes, parts) |
| `COPPER` | check_sources, PCB tools | `[(gerber name, KiCad layer, User layer for pour outlines), ...]` top to bottom |
| `DRILL_PLATED`, `DRILL_NONPLATED`, `OUTLINE` | check_sources, build_pcb, gerber_to_tracks | gerber names of the drill files (`DRILL_NONPLATED = None` when Proteus wrote none) and the outline |
| `OUTLINE_RECT` | check_sources | `(x, y, width, height)` mm of the board outline |
| `FOOTPRINT_PAD_SIZES` | footprints | optional, `"kicad"` (default: KiCad library pad sizes and drills, pad centres on the Proteus positions) or `"proteus"` (every footprint gets the Proteus pad sizes and drills; KiCad library footprints become derived `<name>_Proteus` project footprints) |
| `PCB_FILE`, `SCHEMATIC_FILE`, `REPORTS_DIR` | PCB tools | optional, default `kicad/<PROJECT>.kicad_pcb`, `kicad/<PROJECT>.kicad_sch`, `reports` |
| `PCB_THICKNESS` | build_pcb | board thickness, mm |
| `PCB_RULES` | build_pcb, pours_to_zones | dict, mm: `min_track`, `min_via`, `min_annular`, `min_drill`, `clearance` (also the zone clearance), `hole_clearance`, `hole_to_hole`, `edge_clearance`, Default netclass `track`, `via`, `via_drill`; zone min width = `min_track` |
| `HOLE_FOOTPRINT`, `HOLE_VALUE` | build_pcb | board-only footprint (refdes H1..) for the IPC `hole` records |
| `FREE_PAD_FOOTPRINT`, `FREE_PAD_VALUE`, `FREE_PAD_PREFIX` | build_pcb | board-only one-pad footprint for the IPC `free_pad` records (pad on the IPC net), refdes prefix (default `FP`); only needed when there are free pads |
| `DRC_ACCEPTED` | drc | `{violation type: reason}` error types that remain by design (e.g. courtyard overlaps of a Proteus placement) |
| `OFF_BOARD_PARTS` | footprints | optional `{refdes: note}`: parts of `lib/part_map.csv` without IPC pads (not on the board); every other part without pads is an error |
| `STACKED_VIAS` | ipc356_diff, gerber_roundtrip | optional (default 0): number of Proteus via records stacked on an already recorded via position (one drill, one KiCad via); both checks require exactly this many |
| `DRILL_SMALLER` | ipc356_diff, gerber_roundtrip | optional `{(proteus_mm, kicad_mm): reason}`: KiCad footprint drills smaller than the Proteus drill that are a recorded decision (reported, not failed; matched within 0.005 mm). A larger KiCad drill is always only reported |
| `DRILL_EXCELLON` | gerber_roundtrip | optional: Excellon drill file of another Proteus export of the same board, compared 1:1 with the plated + non-plated drill gerbers |
| `ROUNDTRIP` | gerber_roundtrip | optional dict overriding `res` 0.025 (raster mm/px), `tol` 0.05, `min_area` 0.01 mm², `overlay_res` 0.1, `drill_tol` 0.005; the clearance and zone minimum width come from `PCB_RULES` (`clearance`, `min_track`) |

Coordinates are mm in the IPC / Gerber frame (Y up); KiCad (x, y) = (x, −y).
