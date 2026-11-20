# proteus2kicad

Tools for migrating a Proteus PCB project to KiCad 10 and **proving** the result
electrically identical to the original: the schematic is redrawn from the Proteus PDF, the
PCB is rebuilt from the Proteus Gerber X2 export, and every check compares against the
IPC-D-356A netlist of the CADCAM export.

Extracted from the `ac_psu_kicad` migration (single_phase_inverter_psu_v3) and used by
`tube_psu_kicad` (tube_power_5_1). The method – phases, checks, pitfalls, model and session
plan, regression test for tool changes – is in [`CONVERSION_PLAYBOOK.md`](CONVERSION_PLAYBOOK.md).

## Changes (upgrade notes)

Board repos pin `tools/p2k` to a commit, so these take effect when a board bumps past them.
Behaviour changes that make a finished board fail or re-baseline committed files:

- **Report texts of `p2k ipc356` (batch R).** The cheat sheets in `reference/` change on the
  next `p2k ipc356` run: `pin_nets_by_component.md` prints `Net aliases: none.` /
  `Refdes restored from 6-char truncation: none.` instead of a bare `.` when the key is empty,
  and its `Index:` line names a refdes whose letter prefix no other refdes has by its full name
  (`HT_0 (1)`, `P6 (1)`, `OSC1 (1)`; was `HT_ (1)`, `P (1)`, `OSC (1)`); `pins_by_net.md` lists
  a pin with two pads (two-hole terminal) once, and its pin count drops accordingly. Upgrade:
  run `p2k ipc356` and commit the regenerated `reference/*.md` with the bump (text only; the
  `.csv` / `.json` tables and every check are unchanged).

- **Strict checks on by default (batch Z, decision D-Z).** Without a `STRICT_CHECKS` key a board
  now runs the stricter checks of `board.STRICT_DEFAULT`: `no_connect_pin_type`, `nc_joined`,
  `net_names`, `derive_max_move`, `hole_refdes`, `track_nets`, `roundtrip_counts` (before: none).
  A finished board can start failing on the bump (e.g. a derived footprint whose pads move more
  than half the pad pitch: `derive_max_move`; mounting holes named like schematic refdes:
  `hole_refdes`). Upgrade, per failing id: fix the board (a spec `max_move` for an intended
  pad move, `HOLE_START`, …) or record the exception in `p2k_config.py`:

  ```python
  STRICT_CHECKS_OFF = {"hole_refdes": "schematic has H4 / H5; HOLE_START migration pending"}
  ```

  The entry must name a check that is on (stale entries fail). The old behaviour in one line:
  `STRICT_CHECKS = set()`. Older p2k commits ignore `STRICT_CHECKS_OFF`, so the key can be
  added before the bump. Passing strict checks print to stdout only; no generated file changes.

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
| `ipc356` | IPC-D-356A → `reference/ipc356_pads.{json,csv}`, `pin_nets_by_component.md`, `pins_by_net.md`; `--check` = counts + fix-ups + files up to date; `IPC_PIN_IDS` renames pin ids per refdes before anything is checked or written (so every tool sees the KiCad pad numbers); `--check` accepts exactly the `SPLIT_PIN_MERGES` pins and the `SMD_FREE_PADS` records (printed as `accepted:`); cheat-sheet texts: an empty `NET_ALIASES` / `TRUNCATED_REFDES` prints `none`, the index groups refdes by letter prefix only when two or more share it (else the refdes itself: `HT_0`, `P6`), `pins_by_net.md` lists each pin once (a pin with two pads is one member) | python3 |
| `check_sources` | Gerber X2 export vs IPC: via / component / free / SMD pad flashes (`BGAPad,CuDef` flashes count as SMD pads) and drills 1:1, one `SameCoordinates` id, outline; the `SMD_FREE_PADS` records are matched with the SMD pad flashes of their side only (no OtherPad / OtherDrill flash); `BGA_REFDES`: the BGAPad flashes of every copper layer are exactly the IPC pads of those parts on that side | python3 |
| `check_symbols` | every refdes of `lib/part_map.csv`: symbol pin set = IPC pin set; power symbols are IPC nets; strict id `no_connect_pin_type`: no on-board symbol has a pin of electrical type `no_connect` | python3 |
| `compare_netlists` | KiCad schematic netlist vs IPC by pad sets (`--sheet N`, `--allow-missing` while drawing; schematic `--schematic`, else `$P2K_SCHEMATIC`, else `kicad/<PROJECT>.kicad_sch`); type-safe natural sort (refdes `SEND` next to `SEND1`); the net counts leave out N/C / no-net pins (printed separately); optional part-map column `units` (`A:2 B:5 ...`): pins of an undrawn unit are pending (warnings) when its page is outside `--sheet` or with `--allow-missing`; strict ids `nc_joined` (an `unconnected-(…)` net with more than one pin fails) and `net_names` (a "name hint" fails); `SPLIT_PIN_MERGES`: the IPC split pins must equal the key, then each merged net is joined into its kept net (printed) | python3, kicad-cli |
| `make_skeleton` | one-shot: KiCad project, sym-lib-table, root sheet with one sub-sheet per PDF page | python3, AppImage |
| `footprints` | library for the board's `tools/gen_footprints.py`: `run(RULES, PROJECT)` assigns a footprint to every IPC part (symbol + pad pattern rules), writes `lib/footprint_map.csv` and `lib/<PROJECT>.pretty` (project footprints derived from KiCad footprints or built from the IPC pads – optionally with a `body_poly` outline, e.g. from the Proteus silk, or `body_from` a CADCAM gerber layer –, plus `board_only` one-pad footprints for mounting holes / free pads, THT or `smd_pad=(dia, "F" \| "B")`); rule conditions incl. `refdes` (rules for named parts only), `package` / `device` as a string or a tuple / set; spec keys `bottom=True` (bottom-side part: built / fitted mirrored), `max_move` (mm, derive fails beyond it), `pads={num \| "*": dict(shape, size, rratio, drill, expect_shape)}`, `pin1_dot=(x, y, r)` (full list in the module docstring); with `FP_LIB_TABLE` also `kicad/fp-lib-table`; `PROTEUS_PADS` gives named parts `<footprint>_ProteusPads`; strict id `derive_max_move` (default limit `DERIVE_MAX_MOVE` × smallest base pad pitch; a base that fits only mirrored without `bottom` fails); a part no rule matches is reported once; `--check` | python3, AppImage mount |
| `check_footprints` | every symbol's footprint vs the IPC pads: pads matched by number, rigid fit (residual < 0.05 mm), `reports/footprint_fit.csv` (placement for Phase 3) and `footprint_pads.csv`; `--pcbnew` re-measures after placing with pcbnew; `BOTTOM_THT` (a THT part that fits only mirrored must be listed, a listed one must fit only mirrored); with `FP_LIB_TABLE` every project footprint is resolved through `kicad/fp-lib-table`; strict id `pad_extent` (a KiCad pad reaching outside the Proteus pad fails; `-v` lists them as notes) | python3 (`kipy -m p2k.check_footprints --pcbnew` for pcbnew) |
| `check_pin_order` | Phase 2 pin order: the UNMOVED base footprints (with the Proteus renumbering) fitted onto the IPC pads in both mirror states, compared with the board's `PIN_ORDER` table: expected side and residual bound, optional "fits only …" bound for the other side, fitted angle, `free_angle` fits, `mirror=None` ("does not fit on top"), `polarity` (pin on the marked base pad 1), base from a footprint, a PROJECT spec (`spec=`), the single RULES rule of a symbol (`rule=`) or an ideal circular socket (`ideal=`), target the IPC pads or an ideal socket (`on_ideal=`: a socket footprint's own numbering), renumbering read from symbol pin names (`symbol_renumber=`), pin sense (`turn=`), pin distances; `PIN_ORDER_BOTTOM` (footprint_fit.csv sides and board layers), `PIN_ORDER_MAX_MOVE` (derived pads vs the renumbered base); `--render` overlay PNGs in `reports/pin_order/` (matplotlib). Keys in the module docstring | `p2k check_pin_order` (KiCad libraries mounted: run once through a `bin` wrapper, or `kipy -m p2k.check_pin_order`) |
| `check_pads` | the board pads of the Proteus-pad parts (`PROTEUS_PADS_CHECK`, else `PROTEUS_PADS`) = the Proteus gerber flashes (pcbnew polygon bounding box ±0.01 mm, area ± `CHECK_PADS_AREA_TOL`) on every copper layer, and the Proteus drills (±0.005 mm) | `kipy -m p2k.check_pads` |
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

| `pcb` | Phase 3 library: board / schematic / reports paths (config, `P2K_PCB` / `P2K_SCHEMATIC` / `P2K_REPORTS`, or `configure()` from a board wrapper, which can also filter the IPC records), kicadxml netlist export (a `/` inside an `unconnected-(…)` net name is written `{slash}` as in the board file, so the DRC parity matches), IPC → KiCad net map (board-only footprints are not part pads), `REMOVED_TRACKS` matcher; `ipc_records()` applies `SPLIT_PIN_MERGES` (merged → kept net in every record) for every Phase 3 tool (`ipc_records(merge=False)`: the Proteus nets, used by ipc356_diff / gerber_roundtrip); `SMD_FREE_PADS` helpers | library |
| `build_pcb` | board from the schematic netlist: footprints with nets and symbol links at the `footprint_fit.csv` positions, layers, rules, outline, board-only mounting holes (`HOLE_PREFIX` + `HOLE_START`.., default H1..; a hole refdes equal to a schematic refdes prints a WARNING, strict id `hole_refdes` fails) and free pads; the printed net count leaves out nets that are only on off-board parts (shown as "+n off-board") | `kipy -m p2k.build_pcb` |
| `gerber_to_tracks` | copper gerbers → tracks (pour edge strokes skipped), vias (flash size + drill; stacked duplicates merged), NonConductor graphics, pour outlines on the User layers; every pad flash (ComponentPad, SMDPad, BGAPad) / drill must land on a footprint pad; `REMOVED_TRACKS` entries must match exactly their draw count; the top SMD pad flashes of `SMD_FREE_PADS` must land on board-only pads (one round flash of the IPC size per position, none on other layers) | `kipy -m p2k.gerber_to_tracks` |
| `assign_nets` | union-find over the copper: nets for tracks / vias / pours (`reports/pour_nets.json`), shorts / orphans / opens; `--check` repeats on the saved board after zone fill; `SPLIT_PIN_MERGES`: the open of such a pin's net is accepted exactly when it has two groups and the pin's two pads are in different groups (report line `accepted open (SPLIT_PIN_MERGES): …`); `ASSIGN_NETS_CHECK_REPORT` sends the `--check` section to its own file; `PENDING["assign_nets"]` | `kipy -m p2k.assign_nets [--check]` |
| `pours_to_zones` | Proteus pours (region + stroke − clear holes) → filled zones | `kipy -m p2k.pours_to_zones` |
| `check_board` | saved board vs IPC: pads, mounting holes, free pads, vias (position, size, net 1:1), every copper item has a net; `SMD_FREE_PADS`: exactly one board-only SMD pad at each, copper on F.Cu only, IPC size and net, and the IPC has exactly that many; strict id `track_nets`: the track / via nets as written in the board file equal their copper group's net (pcbnew re-derives them on load, so an edited net is otherwise seen only by the byte compare); `main(added=, extra_vias=)` for later design revisions | `kipy -m p2k.check_board` |
| `place_refdes` | references on the silk clear of pads / silk / board edge (kept if clear, else moved around the courtyard box, else a smaller size, else Fab layer only); a `make_pcb` step after the board is complete | `kipy -m p2k.place_refdes` |
| `erc` | `kicad-cli sch erc` of the schematic (`p2k erc [--no-pending] [schematic [report]]`, default report `reports/erc.rpt`): fails on every error not in `ERC_PENDING`, on warning types in `ERC_FATAL_WARNINGS`, and on a stale `ERC_PENDING` entry; other warnings are counted by type; `PENDING["erc"]` | `p2k erc` |
| `drc` | `kicad-cli pcb drc --schematic-parity`: fails on shorts, unconnected, parity and error types not in `DRC_ACCEPTED`; `SPLIT_PIN_MERGES`: exactly one unconnected item per KiCad net of such a pin is accepted (printed); `DRC_ACCEPTED_ITEMS` (single violations by item text), `DRC_EXPECTED_WARNINGS` (exact warning counts), `PENDING["drc"]`; `--by-refdes [drc.json]`: no DRC run, a per-refdes table of the pad-caused error violations of an existing report (types in `DRC_ACCEPTED` left out; violations naming no pad listed as "other") | `p2k drc` |
| `make_pcb` | the Phase 3 build chain: `PCB_STEPS` in order (default build_pcb, gerber_to_tracks, assign_nets, pours_to_zones when a Proteus copper gerber has a pour, place_refdes), stopping at the first failing step with its full output (`make_pcb: FAILED at step …`); `--check` builds into `build/tmp/make_pcb_check` (copies of `reports/footprint_fit.csv` and the `.kicad_pro`) and fails unless the board, `reports/pour_nets.json` and `reports/assign_nets.txt` (up to `== after zone fill`, whole file with `ASSIGN_NETS_CHECK_REPORT`) equal the committed ones; reference board `$P2K_PCB_REF`, else the board file. Same files and output as the boards' `tools/make_pcb.sh` template | `p2k make_pcb [--check]` |
| `measure_gaps` | read-only, stdout: smallest gap between copper of different nets per item-kind pair (assign_nets stage B geometry), copper to outline, drill to drill (the boards' identical `tools/measure_gaps.py`); `--per-layer` adds per-layer gaps and track widths (the control card's variant); `[LIMIT_MM]` (default 0.2) | `kipy -m p2k.measure_gaps [--per-layer] [LIMIT]` |
| `ipc356_diff` | Phase 4: `kicad-cli pcb export ipcd356` of the board vs the Proteus IPC-D-356A: every pad / via / hole / free pad matched within 0.05 mm, side, THT/SMD, plating, drill (KiCad larger OK, smaller only per `DRILL_SMALLER`), nets 1:1 by pad sets, N/C equal (pads without a net are counted and named as such, not as N/C), stacked via records (`STACKED_VIAS`); `main(added=, extra_vias=)` for later revisions, returns the exit code; `SPLIT_PIN_MERGES`: exactly one "KiCad net spans Proteus nets [kept, merged]" per listed pin accepted (both pad records of the pin on that net; printed `accepted (SPLIT_PIN_MERGES)`); `SMD_FREE_PADS`: exactly one KiCad SMD free-pad record (327, top, no drill) per listed position; the `(reason, pads)` form of `DRILL_SMALLER`; `PHASE4_EXPECTED`; `PENDING["ipc356_diff"]`; strict id `nc_joined`: every `unconnected-(…)` net of the export has exactly one record | `p2k ipc356_diff` |
| `gerber_roundtrip` | Phase 4: KiCad copper gerbers + drills vs the Proteus gerbers: raster XOR per layer with tolerance, difference clusters classified (pad shape / clearance / zone fill / deviation / other = finding), centre-line coverage of every draw and flash both ways, plated + non-plated drills (stacked records, `DRILL_SMALLER`), optional Excellon cross-check (`DRILL_EXCELLON`); `reports/roundtrip/` report + overlay PNGs; `--check` fails on other / uncovered / drill mismatch (every drill error printed); `main(in_area=, extra_vias=)` returns the exit code (`python -m` exits with it); `BGAPad` flashes count as pads; `DRILL_EXCELLON` read in its own unit (`METRIC` / `INCH`, `,TZ` / `,LZ`, `;FILE_FORMAT=i:d`); `EXCELLON_MISSING`; the `(reason, pads)` form of `DRILL_SMALLER`; `PENDING["gerber_roundtrip"]`; strict ids `roundtrip_pad_match` (pad groups of both sets: each with both sets' pads, areas within `pad_area_tol`, centroids within `tol`; catches a missing, extra, resized or shifted pad that the "pad shape" class explains away) and `roundtrip_counts` (per layer KiCad Conductor draws = the tracks `gerber_to_tracks` builds from the Proteus draws, KiCad flashes = Proteus flashes; catches a missing dog-bone stub inside two pads). Strict failures are added to the report (a section at its end), passes only to stdout | `p2k gerber_roundtrip [--check]` (numpy, scipy, PIL) |

The board repo runs the PCB steps with `p2k make_pcb` (`PCB_STEPS`), or still with its own
`tools/make_pcb.sh` (build_pcb, gerber_to_tracks, assign_nets, pours_to_zones, place_refdes) until it
migrates; `tools/make_pcb.sh` can become a one-line `exec tools/p2k/bin/p2k make_pcb "$@"` (the
regression harness calls `tools/make_pcb.sh`). `check_all.sh` stays in each board repo.
Which board-local script of the first boards each shared tool / key replaces is tabulated in
the playbook, section 4 ("Shared tools that replace the board-local templates").

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
| `IPC_PIN_IDS` | ipc356 (so every tool) | optional `{KiCad refdes: {Proteus pin: KiCad pad}}`: pin ids renamed after parsing (positions, nets, other fields unchanged); every listed refdes must exist, its IPC pins must equal the listed Proteus pins both ways, the targets must be unique. E.g. a 2-row header whose odd / even rows Proteus numbers swapped, or a relay with the manufacturer's pin numbers. A board wrapper that replaces `ipc356.parse` with its own renaming keeps working (the shared renaming is skipped then) |
| `VIA_DRILLS`, `FREE_PAD_DRILLS` | ipc356 (`VIA_DRILLS` also gerber_to_tracks) | blank refdes + net: via if drill in `VIA_DRILLS`, else free pad (must be in `FREE_PAD_DRILLS`) |
| `HOLE_DRILLS`, `HOLES_PLATED` | ipc356 | blank refdes, no net: mounting holes |
| `EXPECTED_IPC` | ipc356 | counts asserted by `--check` (nets, records, refdes_raw, tht/smd records, smd_top/bottom, nc_pads, vias, free_pads, holes, parts) |
| `COPPER` | check_sources, PCB tools | `[(gerber name, KiCad layer, User layer for pour outlines), ...]` top to bottom |
| `DRILL_PLATED`, `DRILL_NONPLATED`, `OUTLINE` | check_sources, build_pcb, gerber_to_tracks | gerber names of the drill files (`DRILL_NONPLATED = None` when Proteus wrote none) and the outline |
| `OUTLINE_RECT` | check_sources | `(x, y, width, height)` mm of the board outline |
| `FOOTPRINT_PAD_SIZES` | footprints | optional, `"kicad"` (default: KiCad library pad sizes and drills, pad centres on the Proteus positions) or `"proteus"` (every footprint gets the Proteus pad sizes and drills; KiCad library footprints become derived `<name>_Proteus` project footprints) |
| `PCB_FILE`, `SCHEMATIC_FILE`, `REPORTS_DIR` | PCB tools | optional, default `kicad/<PROJECT>.kicad_pcb`, `kicad/<PROJECT>.kicad_sch`, `reports` |
| `PCB_STEPS` | make_pcb | optional list of steps: a p2k module name (`"build_pcb"`, run as `kipy -m p2k.build_pcb`) or a board script with arguments (`"tools/rename_holes.py"`, `"tools/pcb_wrap.py build_pcb"`, run as `kipy <script> <args>`); default build_pcb, gerber_to_tracks, assign_nets, pours_to_zones (only when a Proteus copper gerber has a dark region), place_refdes |
| `PCB_THICKNESS` | build_pcb | board thickness, mm |
| `PCB_RULES` | build_pcb, pours_to_zones | dict, mm: `min_track`, `min_via`, `min_annular`, `min_drill`, `clearance` (also the zone clearance), `hole_clearance`, `hole_to_hole`, `edge_clearance`, Default netclass `track`, `via`, `via_drill`; zone min width = `min_track` |
| `HOLE_FOOTPRINT`, `HOLE_VALUE` | build_pcb | board-only footprint (refdes H1..) for the IPC `hole` records |
| `HOLE_PREFIX`, `HOLE_START` | build_pcb, ipc356_diff | optional, default `"H"` and 1: the mounting holes are named prefix + start, start + 1, … (e.g. `HOLE_START = 101` when the schematic has refdes H4 / H5); ipc356_diff recognises the KiCad hole records by prefix + digits |
| `FREE_PAD_FOOTPRINT`, `FREE_PAD_VALUE`, `FREE_PAD_PREFIX` | build_pcb | board-only one-pad footprint for the IPC `free_pad` records (pad on the IPC net), refdes prefix (default `FP`); only needed when there are free pads |
| `SMD_FREE_PADS` | ipc356, check_sources, gerber_to_tracks, check_board | optional `{(x, y): raw IPC net}` (IPC frame, exact IPC coordinates): SMD pads without a part (test pads), each an unplated top SMD `free_pad` record without drill. ipc356 `--check` accepts exactly those (every entry must be found), check_sources matches them with SMD flashes only, gerber_to_tracks wants their top SMD flash on a board-only pad, check_board wants a board-only SMD pad on F.Cu only (`FREE_PAD_FOOTPRINT` = an SMD pad, e.g. `footprints` spec `smd_pad`), ipc356_diff exactly one KiCad SMD free-pad record (327, top, no drill) per position. Without the key nothing changes |
| `SPLIT_PIN_MERGES` | ipc356, compare_netlists, pcb (every Phase 3 tool), assign_nets, drc | optional `{(refdes, pin): (kept net, merged net)}` (nets after `NET_ALIASES`): pins whose pads Proteus puts on two nets and the schematic draws as one pin (e.g. a two-pad terminal). The IPC pins with pads on two nets must equal the key, both ways, each with exactly its two nets (ipc356 `--check`, compare_netlists, pcb); the merged net is renamed to the kept net in every record (with its other pins); assign_nets accepts exactly the opens of those pins (two groups, the two pads in different groups), drc exactly one unconnected item per such net. Phase 4 sees the Proteus nets: ipc356_diff accepts exactly one "KiCad net K spans Proteus nets [kept, merged]" per entry (each must occur, both pad records of the pin on K; printed `accepted (SPLIT_PIN_MERGES)`). Board wrappers that do the same before their migration keep working (the shared code steps aside: replaced `compare_netlists.load_ipc` / `assign_nets.analyse`, or a `pcb.configure(records=)` filter that joined the nets already). Without the key nothing changes |
| `ERC_FATAL_WARNINGS` | erc | optional set of ERC warning types that fail (default none), e.g. `{"no_connect_connected", "footprint_link_issues"}` |
| `ERC_PENDING` | erc | optional `{"<type> <refdes> <pin>": reason}`: ERC errors expected while sheets are missing (matched against the violation's first item `Symbol <refdes> Pin <pin>`); an entry ERC no longer reports fails |
| `DRC_ACCEPTED` | drc | `{violation type: reason}` error types that remain by design (e.g. courtyard overlaps of a Proteus placement) |
| `DRC_ACCEPTED_ITEMS` | drc | optional `{violation type: {item pattern: reason or (reason, count)}}`: single error violations accepted by a regex (`re.search`) on their item texts (the item descriptions joined with ` / `, e.g. `Footprint C15 / Footprint PWR1`); every pattern must match at least one violation, with a count exactly that many, so an entry cannot go stale; the other violations of the type still fail. `DRC_ACCEPTED` stays per type |
| `DRC_EXPECTED_WARNINGS` | drc | optional `{warning type: count}`: these warning counts must match exactly (e.g. `{"track_dangling": 0, "via_dangling": 0}` after removing dangling copper); other warnings are only counted |
| `PENDING` | drc, assign_nets `--check`, erc | optional `{tool: {item pattern: question}}`: open user decisions. A tool passes only when every remaining failure matches an entry (`re.search` on its text: drc `<type>: <description>: <items>` / `unconnected: <items>` / `parity: …`; assign_nets `short: <net>: <pads> … \| touching on …` / `open <net>: n groups [pads] …` / `foreign net: <item>`; erc `<type>: <items>`) and every entry still matches at least one failure (else it fails: the decision was made, remove the entry); it prints `PENDING USER DECISION: <question> (n items)` per entry, which the `check_all.sh` template counts; also ipc356_diff (its `ERROR` texts) and gerber_roundtrip (`other: <layer> only <side> <area> mm^2 at (x, y) near <pad>`, `missing: <layer> <Proteus \| KiCad> <draw \| flash> <function> <width> mm at (x, y)`, `drill: <error>`, `pad match: …`, `counts: …`). A key for any other tool is an error |
| `ASSIGN_NETS_CHECK_REPORT` | assign_nets | optional path (relative to the board root, e.g. `"reports/assign_nets_check.txt"`): `assign_nets --check` writes its section there instead of appending it to `reports/assign_nets.txt`, so the committed report holds only the stage A/B sections that `make_pcb` writes (no `sed` in `make_pcb.sh --check`). Switching it on re-baselines `reports/assign_nets.txt` (the after-zone-fill section leaves it) |
| `OFF_BOARD_PARTS` | footprints | optional `{refdes: note}`: parts of `lib/part_map.csv` without IPC pads (not on the board); every other part without pads is an error |
| `FP_LIB_TABLE` | footprints, check_footprints | optional: `True` = `footprints.run` writes / `--check`s `kicad/fp-lib-table` (nickname `PROJECT`, `${KIPRJMOD}/../lib/<PROJECT>.pretty`, descr "Project footprints, pad numbers = IPC pin ids (tools/gen_footprints.py)"; a string replaces the descr); check_footprints then resolves the project footprints through it |
| `DERIVE_MAX_MOVE` | footprints | optional factor (default 0.5) × the smallest pad pitch of the base = the pad-move limit of derived footprints under strict id `derive_max_move` (a spec's `max_move` wins) |
| `PROTEUS_PADS`, `PROTEUS_PAD_OVERRIDES` | footprints | optional `{refdes}`: these parts get `<footprint>_ProteusPads` (Proteus pad sizes; KiCad library footprint → derived with `ipc_sizes`, derived project footprint → copy with `ipc_sizes`); `{"<name>_ProteusPads": pads}` adds a `pads` key to such a spec. Not with `FOOTPRINT_PAD_SIZES = "proteus"` |
| `BOTTOM_THT` | check_footprints | optional `{refdes: reason}`: the THT parts on the bottom side (key presence = the check) |
| `PIN_ORDER` | check_pin_order | `[dict(...), ...]`: one pin-order check per entry (base `footprint=` + `renumber=` / `spec=` / `rule=` / `ideal=`; target `refs=` / `on_ideal=`; expectation `mirror=` True / False / None, `res=`, `other=`, `angle=`, `free_angle=`, `nofit=`, `polarity=`; `turn=`, `distances=` + `tol=`; `name=`, `note=`, `render=`); unknown keys are an error. Full list in the `p2k.check_pin_order` docstring |
| `PIN_ORDER_BOTTOM` | check_pin_order | optional `{refdes}`: exactly these parts are on the bottom in `reports/footprint_fit.csv` and on B.Cu in the board file |
| `PIN_ORDER_MAX_MOVE` | check_pin_order | optional `{spec name: mm}`: every PROJECT spec with a base must be listed; its library footprint's pads may sit at most that far from the renumbered base pads |
| `FOOTPRINT_SCRIPT` | check_pin_order | optional, default `tools/gen_footprints.py`: the board script defining `RULES` and `PROJECT` (imported without writing bytecode when `spec=` / `rule=` / `PIN_ORDER_MAX_MOVE` need it) |
| `PROTEUS_PADS_CHECK`, `CHECK_PADS_AREA_TOL` | check_pads | optional `{refdes}` checked instead of `PROTEUS_PADS` (written out independently of the footprint decisions); relative area tolerance (default 0.06) |
| `BGA_REFDES` | check_sources | optional refdes or set of refdes (e.g. `"U10"`): the BGAPad flashes of every copper layer must be exactly the IPC pads of those parts on that layer's side (1:1 within 0.01 mm; the SMD match alone accepts a BGAPad flash on any SMD record) |
| `PHASE4_EXPECTED` | ipc356_diff | optional `{count: n}` of `proteus`, `kicad`, `matched`, `pads`, `nc`, `nonet`, `vias`, `stacked`, `holes`, `free`, `nets` (the summary counts): each listed count must be this (unknown key = error) |
| `EXCELLON_MISSING`, `EXCELLON_MISSING_LABELS` | gerber_roundtrip | optional `{(x, y, drill): reason}`: drill gerber flashes known to be missing from `DRILL_EXCELLON` (e.g. vias added after that export); each entry must match exactly one "not in the Excellon file" mismatch (0.01 mm, `drill_tol`), reported as `expected, not an error: …` plus a count line; an entry that matches nothing fails. `EXCELLON_MISSING_LABELS = dict(file=, items=, source=)` sets the words of those report lines (defaults "Excellon file", "expected drills", "EXCELLON_MISSING"; lets a board wrapper's committed lines stay word for word) |
| `STACKED_VIAS` | ipc356_diff, gerber_roundtrip | optional (default 0): number of Proteus via records stacked on an already recorded via position (one drill, one KiCad via); both checks require exactly this many |
| `DRILL_SMALLER` | ipc356_diff, gerber_roundtrip | optional `{(proteus_mm, kicad_mm): reason or (reason, pads)}`: KiCad footprint drills smaller than the Proteus drill that are a recorded decision (reported, not failed; matched within 0.005 mm). With `(reason, pads)` (pads = a set of `(refdes, pin)` or refdes, a refdes = every drilled IPC pad of it with the entry's Proteus drill) both tools require the smaller drills of that pair to be exactly those pads, both ways (every listed pad used, no other pad / via of the pair). A larger KiCad drill is always only reported |
| `DRILL_EXCELLON` | gerber_roundtrip | optional: Excellon drill file of another Proteus export of the same board, compared 1:1 with the plated + non-plated drill gerbers; metric or inch (header `METRIC` / `INCH`, optional `,TZ` / `,LZ` and `;FILE_FORMAT=i:d`; default implied decimals 3 metric / 4 inch; no unit line = metric as before) |
| `NONCONDUCTOR_LAYERS` | gerber_to_tracks, gerber_roundtrip | optional `{copper layer: layer}`: Proteus NonConductor draws of that copper layer go to the named layer (e.g. silk) instead of copper graphics; the round trip drops them from the Proteus copper |
| `REMOVED_TRACKS` | gerber_to_tracks, gerber_roundtrip | optional `{(layer, x1, y1, x2, y2): reason or (reason, count)}` (IPC frame): Proteus Conductor draws deliberately not built (e.g. dangling stubs). A draw takes the entry in its drawn direction (within 1e-6 mm) first, else an entry in either direction (0.001 mm), so a back-and-forth draw and its reverse can be two entries; the plain form must match exactly one draw, `(reason, n)` exactly n draws (identical draws, e.g. one segment drawn twice) |
| `REMOVED_VIAS` | gerber_to_tracks, check_board, ipc356_diff, gerber_roundtrip | optional `{(x, y): reason}` (IPC frame): Proteus vias deliberately not built; the checks expect no board via there (and fail if one is there) and drop its via pads / drill from the Proteus side |
| `REFDES_PLACEMENT` | place_refdes | optional dict, mm: `sizes` (1.0, 0.8), `thickness_ratio` 0.15, `pad_gap` 0.1, `silk_gap` 0.1, `edge_gap` 0.5, `side_gap` 0.15 |
| `ROUNDTRIP` | gerber_roundtrip | optional dict overriding `res` 0.025 (raster mm/px), `tol` 0.05, `min_area` 0.01 mm², `overlay_res` 0.1, `drill_tol` 0.005, `pad_area_tol` 0.25 (strict `roundtrip_pad_match`); the clearance and zone minimum width come from `PCB_RULES` (`clearance`, `min_track`) |
| `STRICT_CHECKS` | every tool (`board.strict(id)`) | optional set of stricter check ids. **Absent = the default set** (since batch Z, decision D-Z): `no_connect_pin_type` (check_symbols), `nc_joined` (compare_netlists, ipc356_diff), `net_names` (compare_netlists), `derive_max_move` (footprints), `hole_refdes` (build_pcb), `track_nets` (check_board), `roundtrip_counts` (gerber_roundtrip) = `board.STRICT_DEFAULT`, every id the finished tube boards pass. Not in the default (opt-in by naming them, or `"all"` = every id): `pad_extent` (check_footprints) and `roundtrip_pad_match` (gerber_roundtrip), which fail on every board until its pad sizes are decided. Present = exactly these ids (`set()` = none, the behaviour before batch Z). An unknown id is an error, so a typo never passes silently. Prefer leaving it unset and recording exceptions in `STRICT_CHECKS_OFF`, so a board gets the checks that later batches add to the default; `docs/CANDIDATES.md` section 2 lists the ids |
| `STRICT_CHECKS_OFF` | every tool (`board.strict(id)`) | optional `{check id: reason}`: the strict checks this board does not pass, removed from `STRICT_CHECKS` / the default (see "Changes" above). Every id must be known, every reason a non-empty text, and every listed id must be on without the entry (an entry for an id that is off anyway fails: remove it), so the list stays minimal. E.g. `{"hole_refdes": "schematic has H4 / H5; HOLE_START migration pending"}` |

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
