# Proteus → KiCad conversion playbook

A generic plan for migrating a Proteus PCB project to KiCad with the proteus2kicad tools of
this repository, distilled from the `ac_psu_kicad` migration (single_phase_inverter_psu_v3,
Proteus 8.12 → KiCad 10.0.7, 267 parts, 4 layers, done 2026-10-17 … 10-19) and used for
`tube_psu_kicad` (tube_power_5_1, from 2026-10-21). For a new board: add this repository as
the submodule `tools/p2k`, fill in the inventory of step 0, and write a project `PLAN.md` from
this playbook (refer to it, do not copy it).

The result of following it: a KiCad schematic redrawn from the Proteus PDF and a KiCad PCB
rebuilt from the Proteus gerbers, both **proven** electrically identical to the original by
scripted checks, with every generated file reproducible from scripts.

## 1. Principles

1. **The IPC-D-356A netlist is the source of truth.** The Proteus project file
   (`.pdsprj` = zip of binary `ROOT.DSN` / `ROOT.LYT`) cannot be parsed. The CADCAM
   export's IPC-D-356A file is the only machine-readable link between parts, pins, nets and
   pad coordinates. Every check compares against it.
2. **Refdes and pin ids stay identical to the IPC file.** Symbol pin numbers and footprint
   pad numbers must equal the IPC pin ids (`A`/`C` on diodes, Proteus pin order on
   transistors and regulators, …). Then every script can match by `(refdes, pin)`.
3. **Compare nets by pad-set equivalence, not by name.** Proteus auto-names many nets
   (`#00xxx`), truncates names, and uses aliases. A net is the set of `(refdes, pin)` it
   touches.
4. **Generate, don't hand-edit.** Symbols, footprints, schematic sheets and the board are
   written by re-runnable scripts with deterministic UUIDs, so a fix is a script change plus a
   re-run, and two runs give byte-identical files.
5. **One command verifies everything** (`tools/check_all.sh`), with a pass/fail per step.
   Every phase adds its checks to it. Run it at the start and end of every session.
6. **The repo is the memory, not the chat.** `PLAN.md` (the plan), `STATUS.md` (what is
   done, last check output, findings, decisions), `next_prompt.md` (ready-to-paste prompt for
   the next session). One session per phase.
7. **Findings about the original design are reported, not silently fixed.** The migration
   reproduces Proteus; changes (pinout doubts, polarity, footprint bodies) are user decisions.

## 2. Inputs to collect from Proteus

| Export | Why |
|---|---|
| Schematic PDF (all sheets, text layer kept) | Picture to redraw from; text layer gives values. Render each page to PNG (`hi-NN.png`, ~200 dpi) for reading. |
| CADCAM output, Gerber X2, metric, all layers incl. Top/Bottom Assembly and Mechanical (outline) | Copper, pours, vias, silk reference. Check `TF.SameCoordinates`. |
| Drill (Proteus may write it as a gerber, not Excellon) | Vias and pad holes. |
| **IPC-D-356A netlist** (tick it in the CADCAM dialog) | Nets, refdes, pin ids, pad positions/sizes/sides. Without it this plan does not work. |
| The `.pdsprj` | Only for `strings` (part numbers, package names). |
| BOM if available | Values / part numbers the PDF does not show legibly. |

Things to check in the inventory (write them into PLAN.md section 1):

- Layer count, outline size, units/format (`FSLAX45Y45`), arcs (`G02/G03`) present or not.
- Pours: `G36` regions and `%LPC%` clear regions per layer (they must become zones).
- Blind/buried vias (drill file spans).
- IPC: number of nets, refdes, THT/SMD records, N/C pads, vias, mounting holes.
- **Truncated refdes** (IPC limits refdes to 6 characters: `FILTER_IN`/`FILTER_OUT` both
  become `FILTER`) → disambiguate by coordinate.
- Proteus power-net names (`GND=POWER`, `VCC/VDD=POWER`) → alias table.
- Pin-id conventions per part family; odd refdes; parts with two pads of the same number.

## 3. Phases

Effort below is agent wall-clock for a ~270-part, 4-layer, 11-sheet board; scale roughly
with part count and sheet count.

### Phase 0 – skeleton and tooling (½ day)

1. `git init`; layout `source/` (read-only exports), `reference/` (tables derived from IPC),
   `lib/` (project symbols/footprints, part/footprint maps), `kicad/` (project),
   `tools/` (board scripts), `reports/`;
   `git submodule add https://github.com/johonkanen/proteus2kicad.git tools/p2k`.
2. KiCad environment: the wrappers `tools/p2k/bin/kipy` (KiCad's Python with `pcbnew` and
   `p2k`) and `tools/p2k/bin/kicad-cli` mount the AppImage on demand (`--appimage-mount`,
   interpreter `<mount>/bin/python3.11`, `PYTHONPATH=<mount>/shared/lib/python3.11/dist-packages`;
   `$KICAD_APPIMAGE`). With a distro install, adapt `bin/kicad_env.sh`.
3. `p2k_config.py` at the repo root (keys in README.md): source files, truncated-refdes table,
   KiCad refdes map, net aliases, via/hole drills, expected IPC counts, copper layers, drill
   files, outline. Then `tools/p2k/bin/p2k ipc356` → `reference/ipc356_pads.{json,csv}` (net,
   refdes, pin, x, y, side, size, rotation, THT/SMD, drill) plus `pin_nets_by_component.md`
   (redraw cheat sheet) and `pins_by_net.md`; `--check` self-test (counts equal the
   inventory). `p2k check_sources`: every Gerber X2 pad/via/drill flash matches an IPC record.
4. `tools/check_all.sh` with the first steps (KiCad env, `p2k ipc356 --check`,
   `p2k check_sources`).

Done when: the parser counts match the inventory and the cheat sheet exists.

### Phase 1 – schematic (1–2 days)

Preparation (one session):

1. `lib/part_map.csv`: refdes → sheet, symbol, value. `p2k.pdsprj` reads the schematic
   values from ROOT.DSN (they equal the PDF; the layout values in ROOT.LYT can be stale),
   device and package from ROOT.LYT, and the Proteus pin names from the DSN pinouts;
   `p2k.pdftext` finds the PDF page of each refdes (resolve the ambiguous ones on the renders:
   refdes-like net labels, hand notes, hidden refdes).
2. `tools/gen_symbols.py` → `lib/<proj>.kicad_sym`: copy KiCad standard symbols (flattened,
   pins renumbered to the IPC ids) or draw box symbols with the Proteus pin names; power
   symbols named after the IPC power nets. `--check` = file up to date.
3. `tools/check_symbols.py`: each refdes' symbol pin set == its IPC pin set.
4. `tools/compare_netlists.py`: export the kicadxml netlist, compare by pad sets; report
   wrong-net pins, merged/split nets, N/C mismatches, missing/extra parts/pins; `--sheet N`.
   Only parts already drawn take part, so it works sheet by sheet.
5. `tools/make_skeleton.py`: project, sym/fp-lib-tables, root sheet with one hierarchical
   sheet per PDF page. `tools/sch_builder.py`: small API to place symbols, wires, labels,
   power symbols, no-connects with deterministic UUIDs. `tools/render_sheet.py`: sheet → PNG
   for a visual check.
6. Write **transcription rules** into STATUS.md: power nets → power symbols, other named
   nets → labels in IPC spelling (global labels for nets on several sheets), auto-named nets
   → plain wires, the list of N/C pins, single-pin nets, parts excluded from the board,
   grid (1.27 mm).

Transcription (one session per few sheets): one script `tools/sheets/sheetNN.py` per sheet,
easiest sheets first (connectors, regulators) to build the label vocabulary. After every
sheet: `compare_netlists.py --sheet N` and ERC; target 0 missing, 0 compare errors, 0 ERC
errors on that sheet before moving on.

Done when: compare shows all parts present, 0 wrong-net pins, every IPC net complete; ERC 0
errors (warnings explained in STATUS.md).

### Phase 2 – footprints (½–1 day)

1. `tools/gen_footprints.py` → `lib/footprint_map.csv` (refdes → footprint) and
   `lib/<proj>.pretty`. Assign by symbol + IPC pad pattern (pad count, span, pad size, drill).
   Project footprints are derived from a KiCad footprint (renumber, drop pads, move pad
   centres onto the Proteus positions, optionally `ipc_sizes` = Proteus pad sizes/drills) or
   built from the IPC pads where KiCad has nothing.
2. `tools/fpgeom.py` + `tools/check_footprints.py`: per refdes, match pads by number and solve
   the rigid transform (rotation snapped to 45°, mirror for bottom side) onto the IPC pad
   centres; fail on residual > 0.05 mm. `--pcbnew` places each footprint on a pcbnew board
   with the placement recipe and re-measures. Writes `reports/footprint_fit.csv` (side,
   angle, KiCad position/orientation per part) – the placement input for Phase 3.
3. Default: keep KiCad pad sizes; switch to Proteus sizes only where DRC later forces it
   (user decision). Sheet scripts fill the Footprint field from the map.

Done when: every part fits ≤ 0.05 mm, the map and library are up to date. Report pinout
oddities found by the fit (e.g. a SOT-23 that only fits with renumbered pins).

### Phase 3 – PCB (1–2 days)

`tools/make_pcb.sh` runs, in order, re-creating the board from scratch every time:

| Step | Script | What |
|---|---|---|
| 3a | `build_pcb.py` | Board with the Proteus origin (KiCad y = −IPC y), layers, design rules = Proteus values, outline from Mechanical, footprints from the netlist with nets and symbol links (DRC parity), placed from `footprint_fit.csv`, mounting holes at the IPC hole records. |
| 3b | `gerber_to_tracks.py` | Conductor `D01` draws → tracks; ViaPad flash + drill → vias; pad flashes skipped (footprints provide them) but checked to land on a pad; positive `G36` outlines → User layers (pour reference). **Pour edge strokes are not tracks.** |
| 3c | `assign_nets.py` | Geometric union-find over pads, tracks, vias, pours; each group takes its pads' net; report shorts, orphans, opens. `--check` repeats on the saved board after zone fill. |
| 3d | `pours_to_zones.py` | One zone per Proteus pour: outline = region grown by half the stroke minus clear holes shrunk by half the stroke (keeps clearance cut-outs and thermal-relief holes), net from 3c, solid pad connection, priority = draw order; fill. |
| check | `check_board.py`, `drc.py` | Pads/vias/holes vs IPC (position, net 1:1); DRC with schematic parity: fail on shorts, unconnected, parity, and error types not in an explicit `ACCEPTED` list. |

Expect the first DRC to show shorts/clearance errors where KiCad pads are larger than the
Proteus pads and reach Proteus tracks → ask the user: Proteus pad sizes for those
footprints (and Proteus drills where KiCad's are smaller).

Done when: copper 0 shorts / 0 orphans / 0 opens after zone fill; DRC 0 shorts,
0 unconnected, 0 parity; remaining DRC items listed and justified; board reproducible.

### Phase 4 – independent verification (½ day, preferably a different model/session)

1. `gerber_roundtrip.py`: export KiCad gerbers + drill, rasterise both sets on one grid
   (0.025 mm) in file order with polarity, XOR per copper layer with 0.05 mm tolerance,
   classify difference clusters (pad shape / clearance / zone fill / **other** = finding),
   centre-line coverage check of every draw and flash both ways (finds thin features the
   tolerance hides), drill comparison; overlay PNGs. `--check` fails on an `other` cluster,
   an uncovered draw/flash or a smaller/missing drill.
2. `ipc356_diff.py`: `kicad-cli pcb export ipcd356` vs the Proteus file: every pad/via/hole
   within 0.05 mm, same side, THT/SMD, nets 1:1 by pad sets, N/C equal.
3. Review: read the generating scripts, spot-check the easy-to-get-wrong places (thermal
   reliefs, floating non-conductor graphics, mounting holes, bottom-side parts, multi-pad
   pins, Kelvin parts, single-pin nets), confirm or refute every earlier finding.
4. **Negative-test every new check** (move a track, change a net, shift a pad 0.1 mm) and
   confirm it fails.

Done when: both checks are in `check_all.sh` and pass; STATUS.md has the final state.

### Phase 5 – clean-up (optional, user decisions first)

Typical open points: footprint body graphics that differ from the Proteus silk, symbol
polarity questions, silkscreen/refdes positions (KiCad defaults → optionally from the
Proteus assembly gerbers), KiCad vs Proteus pad sizes/drills where DRC does not force a
choice. Ask which to do; implement in the generators; re-run `make_pcb.sh` and
`check_all.sh`; update the accepted DRC counts.

After the migration, decide when the board stops being generated (first manual layout
change in KiCad) and record it in STATUS.md.

## 4. The shared tools (proteus2kicad)

Tools live in this repository (package `p2k/`, wrappers in `bin/`), pinned per board as the
submodule `tools/p2k`. Everything specific to a board is in its `p2k_config.py` or in board
scripts; the shared code has no board names in it.

| In proteus2kicad (shared) | In the board repo |
|---|---|
| `sexpr`, `geom2d`, `gerber`, `fpgeom`, `sch_builder`, `render_sheet`, `ipc356`, `check_sources`, `bin/kipy`, `bin/kicad-cli`, `bin/p2k` | `p2k_config.py`, `tools/check_all.sh`, `tools/erc.sh` |
| `check_symbols`, `compare_netlists`, `make_skeleton`, `symgen` (symbol generator helpers), `sheetkit` (sheet-script helpers), `pdsprj` (values, layout records, pinouts from the .pdsprj), `pdftext` (PDF words, refdes pages) | `tools/gen_symbols.py` (the symbol list), `lib/part_map.csv` and its generator, `tools/sheets/sheetNN.py` (written fresh) and board-specific sheet helpers |
| to move (still in `ac_psu_kicad/tools`): `check_footprints`, the generator part of `gen_footprints` | footprint `RULES` and project footprints |
| to move: `gerber_to_tracks`, `assign_nets`, `pours_to_zones`, `board_common`, `build_pcb`, `check_board`, `drc`, `gerber_roundtrip`, `ipc356_diff`, `make_pcb.sh` | design rules, mounting-hole footprint, `ACCEPTED` DRC types, project exceptions (no-net pins such as ac_psu R87-4/5) → config keys |

Moving a tool: put it under `p2k/`, replace hard-coded board values by `p2k_config.py` keys
(document them in README.md), grep for refdes and net names of the old board
(`grep -nE "R87|J12|U8|ISOGND"`) to find hidden exceptions. Then the **regression test**:
bump the submodule in `ac_psu_kicad`, switch its scripts to the moved tool, and run its
`tools/check_all.sh` and `tools/regen_check.sh` (clean clone, regenerate every file, fail on
any byte difference). Both must pass with ac_psu's committed files unchanged. Commit and
push here first, then commit the submodule bumps in the board repos.

## 5. Pitfalls learned

Proteus exports:
- The CADCAM dialog may write plain RS-274X; tick Gerber X2 (aperture functions are what
  separates tracks, via pads, component pads, non-conductor lines and the outline). With X2
  the drill comes as `Drill TOP-BOT Plated` / `NonPlated` gerbers, without it as Excellon.
- Mounting holes differ per board: plated with a 4 mm ring in the plated drill file (ac_psu),
  or unplated 3 mm with a 3.5 mm copper ring on every layer in the NonPlated file (tube_psu).
- Every pour region **and** every clear hole is stroked with the pour's edge width; those
  draws are not tracks (their ends are consecutive region vertices). Pour copper = region
  grown by half the stroke minus holes shrunk by half the stroke.
- Thermal reliefs = a clear hole around the pad + spoke **tracks**. Pads in a pour without a
  hole are connected solid.
- Copper gerbers contain the board outline as a `Profile` aperture draw – skip it.
- Non-conductor graphics may sit on copper layers (`NonConductor` aperture) – import as
  graphics without a net.
- IPC refdes truncated to 6 characters; power nets carry `=POWER`; some pins have no net at
  all (not even N/C) – handle as KiCad `unconnected-(…)`.
- Proteus pinouts may differ from the manufacturer's / KiCad's (e.g. REF3025, SOT-23 parts);
  follow the IPC ids and report the difference.

KiCad 10 (pcbnew Python, kicad-cli):
- The AppImage hangs with `--version`; use `--appimage-mount`. `--appimage-extract` may not
  work.
- No headless "Update PCB from schematic": build footprints from the kicadxml netlist in
  Python, including the symbol path link (sheet path + symbol UUID) for DRC parity.
- Footprint must be `board.Add()`-ed before `Flip()` (segfault otherwise). Bottom placement:
  `Flip(pos, FLIP_DIRECTION_LEFT_RIGHT)` then `SetOrientationDegrees((180 + angle) % 360)`;
  top: `SetOrientationDegrees(angle)`; position = (x, −y) of the IPC origin.
- Use `board.Delete(item)`, not `Remove()` (SWIG type-table corruption); set
  `thisown = False` on items created in Python after `Add()`.
- `KIID.SeedGenerator(n)` for reproducible UUIDs; uuid5 per sheet in the schematic writer.
- `LoadBoard` re-derives track nets from pads; `SaveBoard` writes design settings into
  `.kicad_pro`.
- Schematic: field justification is mirrored/swapped for rotated symbols (centre the text);
  a global label on a net with a power symbol is an ERC `multiple_net_names` error (draw
  Proteus alias labels of power nets as power symbols, alias in a text note); project
  footprint libraries need a project `fp-lib-table` for ERC.
- `kicad-cli pcb export ipcd356`: units `CUST 0` = 0.0001 inch, `Y0000` for round pads, vias
  refdes `VIA`, mounting holes as N/C, bottom side = access code of the layer count, net
  names > 14 characters as `NNAME` aliases.
- No SVG converter? librsvg + cairo via ctypes renders `kicad-cli … export svg` to PNG.

Process:
- Courtyard overlaps are normal on a dense Proteus placement (Proteus has no IPC-7351
  courtyards); accept them explicitly in `drc.py`, with reasons.
- Silk warnings are normal with KiCad default refdes positions.
- Datasheets can contradict themselves (side view vs recommended layout): confirm body
  orientation with a physical part before mirroring footprint graphics, and after mirroring
  check the orientation of the mirrored graphics (body side, pin-1 mark, courtyard) again.

## 6. Model and session plan

| Work | Model | Note |
|---|---|---|
| Phase 0, Phase 1 preparation, Phase 2, Phase 3 | Opus | Scripted work with hard oracles (counts, compare, fit residuals, DRC). |
| Phase 1 sheet transcription from PDF images | Fable | Dense images and cross-sheet context; one sheet per turn, compare after each. |
| Stubborn diffs, shorts or opens | Escalate to Fable after two failed attempts | |
| Phase 4 independent verification | Fable, fresh session | A different reader than the one who built it. |

Every session: read PLAN.md and STATUS.md → run `tools/check_all.sh` → work → update
STATUS.md (done, last check output, findings, decisions) → replace `next_prompt.md` →
commit → end with the prompt in a fenced block.

### Handoff prompt contents

1. Model and the phase/step to start on.
2. Files to read first.
3. The verify command and its output at handoff.
4. Open problems and decisions that belong to the user ("do not change without asking").
5. Definition of done, including which checks must pass.

### First prompt for a new project

```
New project: migrate the Proteus project <name> to KiCad <version> in <repo path>.
Sources: <Windows/WSL path to .pdsprj, schematic PDF, CADCAM zip with gerbers, drill and
IPC-D-356A netlist>. KiCad: <AppImage or install path>.
Read the proteus2kicad playbook (https://github.com/johonkanen/proteus2kicad,
CONVERSION_PLAYBOOK.md; after adding the submodule: tools/p2k/CONVERSION_PLAYBOOK.md) and
its README.md. Do Phase 0 now: inventory the sources (fill in the checklist of playbook
section 2), write PLAN.md for this board from the playbook (phases, risks, model plan,
effort), add proteus2kicad as submodule tools/p2k, write p2k_config.py, run p2k ipc356 and
p2k check_sources, and create tools/check_all.sh. Tool changes go into proteus2kicad with
ac_psu_kicad as the regression test (playbook section 4). End with STATUS.md, next_prompt.md
(Phase 1 preparation prompt) and a commit.
```
