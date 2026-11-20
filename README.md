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
| `check_symbols` | every refdes of `lib/part_map.csv`: symbol pin set = IPC pin set; power symbols are IPC nets; strict id `no_connect_pin_type`: no on-board symbol has a pin of electrical type `no_connect` | python3 |
| `compare_netlists` | KiCad schematic netlist vs IPC by pad sets (`--sheet N`, `--allow-missing` while drawing; schematic `--schematic`, else `$P2K_SCHEMATIC`, else `kicad/<PROJECT>.kicad_sch`); type-safe natural sort (refdes `SEND` next to `SEND1`); the net counts leave out N/C / no-net pins (printed separately); optional part-map column `units` (`A:2 B:5 ...`): pins of an undrawn unit are pending (warnings) when its page is outside `--sheet` or with `--allow-missing`; strict ids `nc_joined` (an `unconnected-(…)` net with more than one pin fails) and `net_names` (a "name hint" fails) | python3, kicad-cli |
| `make_skeleton` | one-shot: KiCad project, sym-lib-table, root sheet with one sub-sheet per PDF page | python3, AppImage |
| `footprints` | library for the board's `tools/gen_footprints.py`: `run(RULES, PROJECT)` assigns a footprint to every IPC part (symbol + pad pattern rules), writes `lib/footprint_map.csv` and `lib/<PROJECT>.pretty` (project footprints derived from KiCad footprints or built from the IPC pads – optionally with a `body_poly` outline, e.g. from the Proteus silk, or `body_from` a CADCAM gerber layer –, plus `board_only` one-pad footprints for mounting holes / free pads, THT or `smd_pad=(dia, "F" \| "B")`); rule conditions incl. `refdes` (rules for named parts only), `package` / `device` as a string or a tuple / set; spec keys `bottom=True` (bottom-side part: built / fitted mirrored), `max_move` (mm, derive fails beyond it), `pads={num \| "*": dict(shape, size, rratio, drill, expect_shape)}`, `pin1_dot=(x, y, r)` (full list in the module docstring); with `FP_LIB_TABLE` also `kicad/fp-lib-table`; `PROTEUS_PADS` gives named parts `<footprint>_ProteusPads`; strict id `derive_max_move` (default limit `DERIVE_MAX_MOVE` × smallest base pad pitch; a base that fits only mirrored without `bottom` fails); a part no rule matches is reported once; `--check` | python3, AppImage mount |
| `check_footprints` | every symbol's footprint vs the IPC pads: pads matched by number, rigid fit (residual < 0.05 mm), `reports/footprint_fit.csv` (placement for Phase 3) and `footprint_pads.csv`; `--pcbnew` re-measures after placing with pcbnew; `BOTTOM_THT` (a THT part that fits only mirrored must be listed, a listed one must fit only mirrored); with `FP_LIB_TABLE` every project footprint is resolved through `kicad/fp-lib-table`; strict id `pad_extent` (a KiCad pad reaching outside the Proteus pad fails; `-v` lists them as notes) | python3 (`kipy -m p2k.check_footprints --pcbnew` for pcbnew) |
| `render_sheet` | schematic sheet → PNG (kicad-cli SVG + librsvg/cairo) | python3, kicad-cli |
| `board` | board root and config lookup | library |
| `symgen` | helpers for the board's `tools/gen_symbols.py`: `from_std` (KiCad std symbol, pins renumbered; options `pin_types={pin or "*": type}`, `units={new: old}`, `hide_pin_names=True`), `box` (options `pin_length=`, `tb_margin=`), `multi_box` (one box body per unit), `header`, `power`, `run(build)` (write / `--check`) | library |
| `sheetkit` | helpers for the board's sheet scripts: grid, labels, power symbols, two-pin parts, terminals (`terminal(..., symbol="PIN", value="PIN", justify="legacy")`; `justify="away"` draws the texts of left / right terminals beside the circle instead of over it), `rail(sh, p, y= | x=, junction=True)` (wire straight to a rail, junction on the rail), `sheet(fname, **Sheet keywords)` | library |
| `sch_builder` | schematic writer used by the sheet scripts: `Sheet.place(..., mirror=None \| "x" \| "y")` (pin points for every rotation, KiCad order: rotate, then mirror; the default Reference / Value positions of multi-pin symbols mirrored, hidden fields keep the unmirrored offset), `fields_auto=True` (multi-pin symbols: Reference / Value centred and horizontal above the rotated / mirrored body), `text_gap=mm \| "body"` (horizontal two-pin parts: texts that distance from the centre line, Reference above / Value below, or both on the `text="above"` / `"below"` side 1.905 mm apart; `"body"` = half the body height + 1.27 mm rounded up to the grid), `hide_value=True`; `ref_at` / `value_at` always win. `Sheet.save()` prints a WARNING per pin drawn on the middle of a wire without a junction (KiCad does not connect it); `Sheet(..., split_wires=True)` splits the wire there instead | library |
| `sheets` | `p2k sheets [--check]`: runs `tools/sheets/sheet[0-9]*.py` (name order) and compares every `kicad/*.kicad_sch` byte for byte; `--check` fails on a changed or new sheet and restores the originals | python3 |
| `pdsprj` | read the Proteus project: schematic values (`dsn_values`, ROOT.DSN), layout part records (`lyt_parts`, ROOT.LYT: value, device, package, nets), device + package from the DSN component record (`dsn_devices`) and package from a ROOT.LYT without EID markers (`lyt_parts_ref`, Proteus 8.12 SP2), package pinouts (`dsn_pinouts`), the device symbols' own `$PIN*` records with the package pinout applied (`dsn_device_pins`); options `latin1=True` (Latin-1 strings such as µ) and `dsn_pinouts(brace_optional=True)` (`PIN` lines without `{`, element lists); one-character refdes are anchored to the record (defaults read what they always read) | library |
| `part_map` | library for the board's `tools/make_part_map.py`: `run(SPEC)` writes / `--check`s `lib/part_map.csv` (refdes, PDF page, lib_id, value, device, package, ...) from the IPC refdes, the Proteus project and the PDF text; SPEC keys in the module docstring (`PRJ`, `PDF`, `LAYOUT` eid / ref / dsn, `LATIN1`, `SYMBOL_BY_DEVICE` / `_VALUE` / `_REFDES` / `_PACKAGE` / `_PREFIX`, `OFF_BOARD`, `PART_OVERRIDES`, `PAGE_OVERRIDES`, `PAGE_CHECK`, `UNIT_PAGES`, `FIELDS`, `COMMENT`, `CHECK_UNUSED_DEVICES`) | python3 |
| `pdftext` | words of the schematic PDF with positions (pdftotext -bbox), page of each refdes | library |
| `sexpr`, `geom2d`, `gerber`, `fpgeom` | s-expressions, 2-D geometry, Gerber X2 reader, footprint fit (`fpgeom.fit(..., free_angle=True)`: least-squares angle without the 45° snap) | library |

| `pcb` | Phase 3 library: board / schematic / reports paths (config, `P2K_PCB` / `P2K_SCHEMATIC` / `P2K_REPORTS`, or `configure()` from a board wrapper, which can also filter the IPC records), kicadxml netlist export, IPC → KiCad net map | library |
| `build_pcb` | board from the schematic netlist: footprints with nets and symbol links at the `footprint_fit.csv` positions, layers, rules, outline, board-only mounting holes and free pads; the printed net count leaves out nets that are only on off-board parts (shown as "+n off-board") | `kipy -m p2k.build_pcb` |
| `gerber_to_tracks` | copper gerbers → tracks (pour edge strokes skipped), vias (flash size + drill; stacked duplicates merged), NonConductor graphics, pour outlines on the User layers; every pad flash / drill must land on a footprint pad | `kipy -m p2k.gerber_to_tracks` |
| `assign_nets` | union-find over the copper: nets for tracks / vias / pours (`reports/pour_nets.json`), shorts / orphans / opens; `--check` repeats on the saved board after zone fill | `kipy -m p2k.assign_nets [--check]` |
| `pours_to_zones` | Proteus pours (region + stroke − clear holes) → filled zones | `kipy -m p2k.pours_to_zones` |
| `check_board` | saved board vs IPC: pads, mounting holes, free pads, vias (position, size, net 1:1), every copper item has a net; `main(added=, extra_vias=)` for later design revisions | `kipy -m p2k.check_board` |
| `place_refdes` | references on the silk clear of pads / silk / board edge (kept if clear, else moved around the courtyard box, else a smaller size, else Fab layer only); a `make_pcb` step after the board is complete | `kipy -m p2k.place_refdes` |
| `erc` | `kicad-cli sch erc` of the schematic (`p2k erc [--no-pending] [schematic [report]]`, default report `reports/erc.rpt`): fails on every error not in `ERC_PENDING`, on warning types in `ERC_FATAL_WARNINGS`, and on a stale `ERC_PENDING` entry; other warnings are counted by type | `p2k erc` |
| `drc` | `kicad-cli pcb drc --schematic-parity`: fails on shorts, unconnected, parity and error types not in `DRC_ACCEPTED` | `p2k drc` |
| `ipc356_diff` | Phase 4: `kicad-cli pcb export ipcd356` of the board vs the Proteus IPC-D-356A: every pad / via / hole / free pad matched within 0.05 mm, side, THT/SMD, plating, drill (KiCad larger OK, smaller only per `DRILL_SMALLER`), nets 1:1 by pad sets, N/C equal (pads without a net are counted and named as such, not as N/C), stacked via records (`STACKED_VIAS`); `main(added=, extra_vias=)` for later revisions, returns the exit code | `p2k ipc356_diff` |
| `gerber_roundtrip` | Phase 4: KiCad copper gerbers + drills vs the Proteus gerbers: raster XOR per layer with tolerance, difference clusters classified (pad shape / clearance / zone fill / deviation / other = finding), centre-line coverage of every draw and flash both ways, plated + non-plated drills (stacked records, `DRILL_SMALLER`), optional Excellon cross-check (`DRILL_EXCELLON`); `reports/roundtrip/` report + overlay PNGs; `--check` fails on other / uncovered / drill mismatch (every drill error printed); `main(in_area=, extra_vias=)` returns the exit code (`python -m` exits with it) | `p2k gerber_roundtrip [--check]` (numpy, scipy, PIL) |

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
| `ERC_FATAL_WARNINGS` | erc | optional set of ERC warning types that fail (default none), e.g. `{"no_connect_connected", "footprint_link_issues"}` |
| `ERC_PENDING` | erc | optional `{"<type> <refdes> <pin>": reason}`: ERC errors expected while sheets are missing (matched against the violation's first item `Symbol <refdes> Pin <pin>`); an entry ERC no longer reports fails |
| `DRC_ACCEPTED` | drc | `{violation type: reason}` error types that remain by design (e.g. courtyard overlaps of a Proteus placement) |
| `OFF_BOARD_PARTS` | footprints | optional `{refdes: note}`: parts of `lib/part_map.csv` without IPC pads (not on the board); every other part without pads is an error |
| `FP_LIB_TABLE` | footprints, check_footprints | optional: `True` = `footprints.run` writes / `--check`s `kicad/fp-lib-table` (nickname `PROJECT`, `${KIPRJMOD}/../lib/<PROJECT>.pretty`, descr "Project footprints, pad numbers = IPC pin ids (tools/gen_footprints.py)"; a string replaces the descr); check_footprints then resolves the project footprints through it |
| `DERIVE_MAX_MOVE` | footprints | optional factor (default 0.5) × the smallest pad pitch of the base = the pad-move limit of derived footprints under strict id `derive_max_move` (a spec's `max_move` wins) |
| `PROTEUS_PADS`, `PROTEUS_PAD_OVERRIDES` | footprints | optional `{refdes}`: these parts get `<footprint>_ProteusPads` (Proteus pad sizes; KiCad library footprint → derived with `ipc_sizes`, derived project footprint → copy with `ipc_sizes`); `{"<name>_ProteusPads": pads}` adds a `pads` key to such a spec. Not with `FOOTPRINT_PAD_SIZES = "proteus"` |
| `BOTTOM_THT` | check_footprints | optional `{refdes: reason}`: the THT parts on the bottom side (key presence = the check) |
| `STACKED_VIAS` | ipc356_diff, gerber_roundtrip | optional (default 0): number of Proteus via records stacked on an already recorded via position (one drill, one KiCad via); both checks require exactly this many |
| `DRILL_SMALLER` | ipc356_diff, gerber_roundtrip | optional `{(proteus_mm, kicad_mm): reason}`: KiCad footprint drills smaller than the Proteus drill that are a recorded decision (reported, not failed; matched within 0.005 mm). A larger KiCad drill is always only reported |
| `DRILL_EXCELLON` | gerber_roundtrip | optional: Excellon drill file of another Proteus export of the same board, compared 1:1 with the plated + non-plated drill gerbers |
| `NONCONDUCTOR_LAYERS` | gerber_to_tracks, gerber_roundtrip | optional `{copper layer: layer}`: Proteus NonConductor draws of that copper layer go to the named layer (e.g. silk) instead of copper graphics; the round trip drops them from the Proteus copper |
| `REMOVED_TRACKS` | gerber_to_tracks, gerber_roundtrip | optional `{(layer, x1, y1, x2, y2): reason}` (IPC frame): Proteus Conductor draws deliberately not built (e.g. dangling stubs); every entry must match a draw |
| `REMOVED_VIAS` | gerber_to_tracks, check_board, ipc356_diff, gerber_roundtrip | optional `{(x, y): reason}` (IPC frame): Proteus vias deliberately not built; the checks expect no board via there (and fail if one is there) and drop its via pads / drill from the Proteus side |
| `REFDES_PLACEMENT` | place_refdes | optional dict, mm: `sizes` (1.0, 0.8), `thickness_ratio` 0.15, `pad_gap` 0.1, `silk_gap` 0.1, `edge_gap` 0.5, `side_gap` 0.15 |
| `ROUNDTRIP` | gerber_roundtrip | optional dict overriding `res` 0.025 (raster mm/px), `tol` 0.05, `min_area` 0.01 mm², `overlay_res` 0.1, `drill_tol` 0.005; the clearance and zone minimum width come from `PCB_RULES` (`clearance`, `min_track`) |
| `STRICT_CHECKS` | every tool (`board.strict(id)`) | optional set of ids of opt-in stricter checks (default none; `"all"` = every implemented id). An unknown id is an error, so a typo never passes silently. Implemented ids: `no_connect_pin_type` (check_symbols), `nc_joined` (compare_netlists), `net_names` (compare_netlists), `derive_max_move` (footprints), `pad_extent` (check_footprints); `docs/CANDIDATES.md` section 2 lists the planned ones. **Planned behaviour change (decision D-Z, a later batch):** the default becomes `"all"`, so every strict check runs on a board that does not set the key; a board keeps the old behaviour with `STRICT_CHECKS = set()` (or an explicit set of the ids it passes) in its `p2k_config.py` before it bumps to that p2k commit |

Coordinates are mm in the IPC / Gerber frame (Y up); KiCad (x, y) = (x, −y).

`board.config()` executes `p2k_config.py` from its source (no bytecode is read or written), so an
edit is seen even when a stale `__pycache__` entry has the same mtime and size. Environment
overrides: `P2K_BOARD` (board root), `P2K_PCB`, `P2K_SCHEMATIC` (also the default of
`compare_netlists --schematic`), `P2K_REPORTS`.

## Developing p2k: regression run

`tools/regress.sh [-j N] [-k] [-o DIR] [COMMIT] [BOARD[@REV] ...]` tests a proteus2kicad commit
(default HEAD; commit first, unpushed is fine) against the board repos under `~/dev`
(`$REGRESS_BOARDS_DIR`): per board a scratch clone at the board's HEAD with `tools/p2k` at
COMMIT (bump committed in the scratch clone only), every generated file regenerated with the
board's own scripts (`p2k ipc356`, `make_part_map.py`, `gen_symbols.py`, the sheet scripts,
`gen_footprints.py`, `check_footprints --pcbnew`, `excellon_metric.py`, `make_pcb.sh`), then
`tools/check_all.sh`, then a clean `git status`. The default board set is the five tube boards
(power amp, preamp, PI / FX loop, HV supplies, ti60f225 control card; user decision 2026-11-20).
Board repos are never changed. Per-board logs (`-o DIR`) can be diffed between
two commits to review stdout changes. The playbook (section 4) and `docs/CANDIDATES.md`
(section 3) say when to run it.
