# p2k candidates: inventory and consolidation plan

Status (2026-11-20): batch 0 (regression harness, baseline; c72d909), batch 1 (robustness fixes; 6cf43b1),
batch 2 (Proteus project reader and part map; 0b7a294) and batch 3 (symbols, netlist comparison, ERC;
2686b4b) done;
see "Status per entry" (section 5) and the regression record (section 6). Later sessions implement
the rest batch by batch (section 3) and tick the entries off there. The prompt for the next
session is at the end (section 7).

Sources: the "## p2k candidates" section of `STATUS.md` in the five board repos converted with
p2k `c5682dd`, and their board-local workarounds in `tools/`. `tube_psu_kicad` and `ac_psu_kicad`
have no candidate list; tube_psu contributes code (`tools/measure_gaps.py`, `tools/silk_bodies.py`,
`tools/make_pcb.sh`) and both are regression boards.

| Tag | Repo | Pin of `tools/p2k` |
|---|---|---|
| PA | `tube_power_amp_kicad` (pilot) | c5682dd |
| PR | `tube_preamp_kicad` | c5682dd |
| PF | `tube_pi_fxloop_kicad` | c5682dd |
| HV | `tube_hv_supplies_kicad` | c5682dd |
| TC | `ti60f225_tube_control_card` | c5682dd |
| PSU | `tube_psu_kicad` (no list; code and regression only) | c5682dd |
| AC | `ac_psu_kicad` (regression only) | **32f8fd3** |

"PA5" = candidate 5 in PA's `STATUS.md`. Board paths are relative to the board repo root.

## 0. Rules for every change

1. **No key, no change.** Every new behaviour hangs on a new `p2k_config.py` key, a new spec key, a
   new keyword argument or a new module. Without it, every generated file of every board stays
   byte for byte the same (sheets, symbols, footprints, maps, `kicad/*.kicad_pcb`, `reference/*`,
   `reports/*`). Only pure crash fixes and the acceptance of inputs that used to raise
   (e.g. `BGAPad`) are on by default.
2. **Stricter checks are opt-in.** A new check that can fail a finished board goes into one
   config key, `STRICT_CHECKS` (a set of check ids, default `set()`; `"all"` = every id). The id is
   given with each entry below. The key is read by `p2k.board` (`board.strict(id)`), so all modules
   use one mechanism. Boards opt in during their migration. Flipping the default is a separate,
   last batch (Z) and needs the user (section 4).
3. **Success output goes to stdout, not into committed reports.** A new acceptance or verification
   that passes prints to stdout only. It never adds lines to a committed report
   (`reports/roundtrip/roundtrip.txt`, `reports/assign_nets.txt`, `reports/footprint_*.csv`,
   `reference/*.md`). That way a board's migration from its wrapper to the shared key stays
   byte-identical. Failures may write anywhere.
4. **Wrapper semantics are the spec.** When board code exists, the shared version keeps its key
   names and its exact checks (e.g. "the set of pins equals the table, both ways"), so a migration
   is "delete the wrapper, keep the config".
5. **Regression per batch:** see section 3, "Regression set".

## Decisions (user, 2026-11-20, through the coordinating session)

- **D-R (batch R, report text fixes):** yes. When the cosmetic `ipc356` report fixes (I3, I4, I5)
  land, the committed `reference/*.md` files of the affected boards are re-baselined, in a later
  batch; each board's re-baseline is its own reviewed commit.
- **D-Z (batch Z, strict defaults):** yes. At the end, the strict checks become ON by default
  (`STRICT_CHECKS` default = every id); a board that needs an exception records it in its
  `p2k_config.py`.
- **D-AC (regression set):** `tube_psu_kicad` + the five tube boards are the regression set, plus
  `ac_psu_kicad` for the modules it uses (section 3). `ac_psu_kicad` is **not** switched to the
  shared Phase 2-4 tools.
- **D-PIN (new feature, design entry I6, PCB batch 7a):** an IPC pin-renumber config key, applied
  in one place in `p2k.ipc356` so every check sees it, for the HV supplies J1 and control card J11
  ribbon headers (odd / even rows swapped in Proteus): the pads get standard numbering, the copper
  is unchanged. Key name and semantics are those of PF's board-local `IPC_PIN_IDS` (PF candidate
  24), so PF's migration is a wrapper deletion. Implemented in its batch, not now.

## 1. Deduplicated candidates

Class: **A** = lift existing board-local code into p2k; **B** = new feature or check; **C** = bug fix
in p2k; **D** = docs / playbook only; **X** = not worth doing or superseded (with a reason).

### 1.1 `p2k.pdsprj` (Proteus project reader) and the part map

**P1. Latin-1 strings (µ) dropped.** C. Sources: PA1, PR2 (second half), PF5.
Module: `pdsprj` (`dsn_values.pstr` accepts bytes 32-126 only; `_strings` splits runs at 0xA0-0xFF).
Board code: PA `tools/make_part_map.py` `LATIN1_PARTS`; PR the same plus device / package;
PF `tools/make_part_map.py` `_strings()` and `lyt_parts()` (Latin-1 decode, trailing 0xFF stripped).
Design: keyword `latin1=False` on `dsn_values`, `lyt_parts` and `_strings`. With `True`, bytes
0xA0-0xFF are decoded as Latin-1 and a trailing 0xFF pad byte of a net token is stripped. The
default stays as it is, because PSU / AC part maps might change (some values there could
start being read). Boards opt in with `latin1=True` in the part-map call (M1), and their
`LATIN1_PARTS` entries that only restated the decoded value are dropped.

**P2. One-character refdes (`A`-`D`) break `dsn_values` / `lyt_parts`.** C. Sources: PR2, PF5.
Module: `pdsprj` (`_strings(min_len=2)` drops the refdes token; `dsn_values` matches one-letter
strings all over ROOT.DSN and raises).
Board code: PR `make_part_map.py` `SINGLE_LETTER_PARTS` (calls without A-D, then fills them by
hand); PF `make_part_map.py` local `lyt_parts()` with `_strings(min_len=1)`.
Design: the new code path runs only for requested refdes of length 1, which raise or mismatch
today. It anchors the match to the record structure: in the DSN, the string directly before the
part's `COMPONENT VALUE`; in the LYT, `_strings(min_len=1)` limited to the refdes slot of an
`EID` record. Longer refdes keep the current code. On by default: no board passes a one-letter
refdes today.

**P3. ROOT.LYT without `EID` markers (Proteus 8.12 SP2) gives no layout records.** B. Sources:
HV3, TC4 (and TC PLAN.md).
Module: `pdsprj`.
Board code: HV `make_part_map.py` `dsn_devices()` (device = first string after `COMPONENT VALUE`,
package = `{PACKAGE=...}` in the DSN); TC `make_part_map.py` `lyt_packages()` (anchor on
`PART REFERENCE`, longest refdes prefix of the token before it, accepted when the value token
starts with the DSN value; package = the token before the refdes).
Design: two new functions. `dsn_devices(prj, refs)` is the HV reader. `lyt_parts_ref(prj, refs,
values)` is the TC reader, with trailing junk tolerated. `lyt_parts` itself is unchanged (it
returns nothing for such a file, as now). Both are lifted unchanged, including their acceptance rules.

**P4. Device pin records and brace-less `*PINOUT` lines are not read.** B (device pins) + C
(braces). Sources: PA4, PF6, TC6.
Module: `pdsprj.dsn_pinouts`.
Board code: TC `tools/gen_symbols.py` `ti60_pinout()` (regex `\{?PIN "(.*)" = ([^}]*)\}`); PA / PF
read `$PINSHORT` / `$PINDEFAULT` by hand (no code).
Design:
- `dsn_pinouts(..., brace_optional=False)`: with `True`, the TC regex is used, and multi-element
  pinouts return their element (unit) lists. Opt-in, because TC's `gen_symbols.py` and other
  callers would see more pins.
- New `dsn_device_pins(prj)`: `$PINSHORT name number` / `$PINDEFAULT name` grouped by the
  following `{*DEVICE}` block, with the device name and DESC. Pin names are often one character,
  so it does not use a `strings -n 2` scan. The package `*PINOUT` table overrides the symbol pin
  number when one exists (12AX7 case in PF6). Fix the unreliable `device` attribution of
  `dsn_pinouts` only inside the new function.

**M1. Shared part-map generator `p2k.part_map`.** A. Sources: PA5, PR6, HV6 (TC and PF have the
same script with local readers).
Board code: `tools/make_part_map.py` in PA, PR, PF, HV, TC (all derived from PSU's).
Design: `part_map.run(spec, argv)` writes / `--check`s `lib/part_map.csv`, as `footprints.run` does.
The board script keeps only data:
- `SYMBOL_BY_DEVICE`, `SYMBOL_BY_VALUE` (HV, TC), `SYMBOL_BY_REFDES` (TC);
- `PAGE_OVERRIDES`, `OFF_BOARD`, `UNIT_PAGES` (TC);
- `PART_OVERRIDES = {refdes: {value, device, package}}`, which replaces PA/PR `LATIN1_PARTS` and
  PR `SINGLE_LETTER_PARTS`;
- `FIELDS` (column list: TC has a `package` column, the tube boards have `device`);
- the reader choice: `layout="eid" | "ref" | "dsn"` (P3) and `latin1=` (P1).
Default: the board script stays until it migrates. Migration criterion: `lib/part_map.csv`
identical.

### 1.2 Symbols (`p2k.symgen`, `p2k.check_symbols`)

**S1. Pin-type override for library symbols (valves all passive).** A. Sources: PA9, PR7.
Board code: `pin_types()` in `tools/gen_symbols.py` of PA, PR, PF.
Design: `symgen.from_std(..., pin_types={pin or "*": type})`. Default `None` = unchanged.

**S2. Renumber the units of a std symbol, hide pin names.** A. Source: PR7.
Board code: PR `gen_symbols.py` `swap_units()`, `hide_pin_names()`.
Design: `from_std(..., units={new: old}, hide_pin_names=True)`. Defaults unchanged.

**S3. Multi-unit box symbols.** A. Source: TC7.
Board code: TC `gen_symbols.py` `multi_box()`.
Design: new `symgen.multi_box(name, ref, units)`. `box()` is unchanged.

**S4. `box` pin length fixed at 2.54 mm.** B. Source: HV7.
Design: `box(..., pin_length=2.54)`.

**S5. `box` top / bottom pin margin sized for names inside the body.** B. Source: TC15.
Design: `box(..., tb_margin=None)` (None = current rule).

**S6. `symgen.header(bottom_up=)`.** B, low priority. Source: HV5 (side remark). With K1 (mirror),
it is only a convenience.

**S7. Reject pins of electrical type `no_connect`.** B, strict id `no_connect_pin_type`. Sources:
PA19, PR5, TC10.
Board code: TC `gen_symbols.py` assert, `tools/test_no_connect.py` (negative test); PA / PR pins
made passive.
Design: with the id set, `check_symbols` fails on any `no_connect`-type pin of a symbol placed for
an on-board refdes. A wired pin of that type stays in its own `unconnected-(…)` net in the KiCad 10
netlist and hides the wire from `compare_netlists`. The rule itself goes to the playbook (G3).

### 1.3 Schematic drawing (`p2k.sch_builder`, `p2k.sheetkit`)

**K1. `Sheet.place` cannot mirror; field positions are not mirror / rotation aware.** A.
Sources: PA6, PR10(b), PF7, PF10, HV5, TC9.
Board code: `place_mirrored()` in `tools/sheets/_common.py` of all five boards; PF
`place_rot90_mirrored()`.
Design: `place(..., mirror=None | "x" | "y")` inserts `(mirror …)`, mirrors the returned pin points
for every rotation and mirrors the default field positions. `None` gives a byte-identical
node. Field auto-placement for rotated multi-pin symbols is a separate opt-in,
`fields_auto=True`, so existing `ref_at` / `value_at` calls keep working. Migration check: the
sheets are identical with `place_mirrored` replaced (PA, PR, HV, TC, PF). Callers that also pass
`ref_at` / `value_at` keep them.

**K2. Texts of horizontal two-pin parts lie on capacitor plates.** B. Sources: PR10(a), PF11, HV8.
Board code: PR `_common.hcap()`, PF `_common.ph()`, HV `_common._hcap()` (`ref_at` / `value_at` at
±3.81 mm).
Design: `place(..., text_gap=None)` (mm, or `"body"` = from the symbol's body height). Default
unchanged.

**K3. Value printed on every unit of a multi-unit part.** B. Source: PR10(c).
Design: `place(..., hide_value=False)`.

**K4. `sheetkit.terminal` hard-codes symbol `PIN` / value `"PIN"`, and left/right texts sit on
the circle.** A. Sources: PA3, PA7, HV4, TC9 (and PR8: the same `term()`).
Board code: `term()` in `tools/sheets/_common.py` of PA, PR, PF, HV, TC.
Design: `terminal(..., symbol="PIN", value="PIN", justify="legacy")`; `justify="away"` uses the
swapped justification of `term()`. The default must stay `legacy`, because AC and PSU sheets call
`terminal()` and must not change.

**K5. `sheetkit.rail()` (wire straight to a rail with a junction).** B, low priority. Source: PR11.
Board code: a `gnd(p)` in every PR sheet script.

**K6. IPC-driven sheet helpers and multi-unit helpers.** B, deferred. Sources: TC12, TC16.
Board code: TC `_common.py` `net()`, `net_kind()`, `tie()`, `pin_tie()`, `two()`, `place_pin()`,
`header()`, `unit_of()`, `unit_pins()`, `array_r()`. They need per-unit pages (C4). Worth doing for
the next FPGA / MCU board, not before. `Sheet.place(anchor_pin=)` belongs here too.

**K7. A pin placed on the middle of a wire segment is not connected.** B. Source: TC14.
Design: `Sheet.save()` prints a warning per pin point that lies inside a wire segment (stdout
only; no file change). Splitting the segment automatically is opt-in (`Sheet(split_wires=True)`).

**K8. "Sheets reproducible from their scripts" check.** A, low priority. Sources: TC12 (tail);
code: TC `tools/make_sheets.sh`, PF `tools/check_sheets.sh`.
Design: `p2k sheets [--check]` runs `tools/sheets/sheet[0-9]*.py` into a temp directory and compares.

**K9. PDF text layer as a label check.** B, low priority. Source: TC17 (part).
Design: `pdftext.row_words(page, word)` ("the words left / right of a pin-number word on its row")
so a sheet script can assert its labels. The rest of TC17 is X (X3).

### 1.4 Netlist and ERC checks (`p2k.compare_netlists`, new `p2k.erc`)

**C1. `natkey` / `pinkey` crash on `SEND` + `SEND1` (mixed int / str keys).** C. Sources: PR9, PF9,
TC13, HV10 (not hit there).
Board code: PR / PF `tools/compare.py` (`(kind, int, str)` tuple keys).
Design: lift the type-safe keys. For inputs that do not crash today the order is unchanged, and
the output is stdout only. On by default.

**C2. Joined N/C pins pass (each pin of an `unconnected-(…)` net is a singleton).** B, strict id
`nc_joined`. Sources: PR27, PF19, TC32 (compare part), PA19 / PR5 (background).
Board code: PR / PF `tools/compare.py` and TC `tools/compare_netlists.py` (`load_kicad` wrapper:
fails on an `unconnected-(…)` net with more than one node).
Design: with the id set, a pin counts as unconnected only if its net has exactly one node, and
the error text matches the wrappers. The same id covers R4 (ipc356_diff).

**C3. Per-unit pages for multi-unit parts.** B. Source: TC8.
Design: optional `units` column in `lib/part_map.csv` (`A:2 B:5 …`, already written by TC from
`UNIT_PAGES`). When it exists, `--sheet N` / `--allow-missing` treat the pins of units on undrawn
sheets as pending (warnings), like missing parts. Without the column, behaviour is unchanged.

**C4. Strict net-name mode.** B, strict id `net_names`. Source: HV27 (since the 2026-11-20 label
renames HV's `check_all.sh` fails on any `WARNING: name hint` board-locally: a dropped
`NET_ALIASES` entry otherwise passed every check; that guard is this mode's migration).
Design: fail when a KiCad net that is not auto-named (`Net-(`, `unconnected-(`) matches an IPC net
that is not `#00xxx` under another name, after `NET_ALIASES`. Today this is a "name hint" warning.

**C5. `compare_netlists` ignores `P2K_SCHEMATIC`.** C. Source: PF22 (second half).
Design: use the env var as the default of `--schematic` (the PCB tools honour `P2K_PCB`). It
only has an effect when the variable is set.

**C6. Net counts include N/C pads ("IPC nets touched 47" on a 43-net board).** C, cosmetic,
stdout only. Sources: PA10, HV9.
Design: print "43 nets + 4 N/C pads".

**E1. Shared ERC judge `p2k erc`.** A. Sources: TC11, PA19, PF15, PR (erc.sh), TC10.
Board code: `tools/erc.sh` in every repo (six variants), TC `tools/erc_check.py` + `tools/erc_pending.txt`.
Design: `p2k erc [schematic [report]]` runs kicad-cli and judges the report:
- fails on every error;
- fails on warning types in `ERC_FATAL_WARNINGS` (default `set()` = today's PSU / HV behaviour;
  the tube boards set `{"no_connect_connected", "footprint_link_issues"}`);
- `ERC_PENDING` = `{(type, item text): reason}`: errors expected while sheets are missing; an entry
  that no longer occurs fails;
- warnings are counted by type.

  `reports/erc.rpt` is not committed in any board, so the report path is free. Board `erc.sh`
  becomes a one-liner or is removed from `check_all.sh`.

### 1.5 Footprints (`p2k.footprints`, `p2k.check_footprints`) and pin order

**F1. `footprints.run` does not write `kicad/fp-lib-table`.** A. Sources: PA12, PR12, PF13, HV11, TC22.
Board code: `FP_LIB_TABLE` / `fp_lib_table()` in `tools/gen_footprints.py` of PA, PR, PF, HV, TC
(identical apart from the name); PSU has a hand-written table.
Design: config key `FP_LIB_TABLE = True` (default `False`, because PSU's hand-written table might
differ). When set, `run` writes / `--check`s exactly the board template: version 7, nickname
`PROJECT`, `${KIPRJMOD}/../lib/<PROJECT>.pretty`, the same descr. A string value overrides the descr.

**F2. THT parts mounted on the bottom side are built unmirrored.** A. Sources: PA24, PR25, PF23.
Board code: `build_library` wrapper + spec key `bottom=True` in `tools/gen_footprints.py` of PA, PR, PF.
Design: spec key `bottom=True` (same name), honoured by `from_ipc` (the IPC pads of the `ipc`
refdes are treated as `bottom`) and by `derive` (`_side_mirror` returns `[True]`).
`check_footprints` already places such parts on B.

**F3. `derive()` moves pads any distance and hides a wrong renumbering.** B, strict id
`derive_max_move`. Sources: PR14, PF14, HV12, TC23.
Board code: `MOVE_LIMIT` in PR `tools/check_pin_order.py`; the guards in the other
`check_pin_order.py` copies.
Design:
- Spec key `max_move` (mm). With the strict id, the default limit is config
  `DERIVE_MAX_MOVE = 0.5` × the smallest pad pitch of the base.
- `derive` fails when a pad moves farther, and prints (stdout) the chosen mirror state and the
  largest move per footprint.
- A mirrored-only fit of a spec without `bottom=True` is reported, and fails under the id.
- Without the id and without `max_move` the behaviour is unchanged. Today's largest intended
  move is 1.27 mm (PF DIN0414), so boards that opt in set `max_move` on such specs.

**F4. Pad shape / size / drill per pad, and a pin-1 dot.** A. Sources: HV25, HV23, PR22, PF16.
Board code: HV `gen_footprints.py` `_derive_pads` (spec key `pads={num | "*": dict(shape, size,
rratio, drill)}`); PR `derive_with_pad_shapes` (`pad_shapes`, `pin1_dot`); PF `derive` wrapper
(`ROUND_PAD1`, `PIN1_DOT`).
Design: spec keys `pads` (HV semantics, including the error for unknown pad numbers and the removal
of `roundrect_rratio` / `chamfer*` before setting a shape) and `pin1_dot=(x, y, r)` (filled
`fp_circle` on F.SilkS 0.12 and F.Fab 0.1, inserted directly before pad 1, as PR / PF do it).
Optional `expect_shape` per pad keeps PR's guard "the base pad must be one `rect`". Migration:
PR `pad_shapes={"1": "circle"}` → `pads={"1": dict(shape="circle")}`; PF hard-coded
`ROUND_PAD1` → spec keys. The output must be identical (check the s-expression order).

**F5. Footprint body from a gerber outline (`body_from`).** A. Sources: PA14, PR16; PSU `tools/silk_bodies.py`.
Board code: PA `octal_body()`, PR `noval_body()` (Top Assembly draws around the part), PSU
`silk_bodies.py`.
Design: spec key `body_from=dict(layer="Top Assembly", centre=(x, y), min_len=…, radius=…)`. It
selects the draws longer than `min_len` with both ends within `radius` of the centre and gives
`body_poly` = their start points rounded to 0.001, sorted by angle around the centre, exactly as
the board code does. An optional `expect=n` (point count) keeps the board's guard. The PSU form
(silk outline) is the same idea and moves if its output can be reproduced; otherwise it stays
board-local.

**F6. Rule condition `package` / `device` takes one value.** B. Source: PR15.
Design: also accept a tuple / set (a `str` keeps exact matching).

**F7. `single_pad` makes THT pads only (board-only SMD test pads).** A. Sources: TC21, TC2.
Board code: TC `gen_footprints.py` `smd_pad()` + `build_library` wrapper (spec `smd_pad=(dia, side)`).
Design: board-only spec key `smd_pad=(diameter, "F" | "B")`, the same output as TC's function.

**F8. `PROTEUS_PADS` decision as one config line.** B (depends on F4). Sources: HV17, HV24 (part), TC28.
Board code: HV `PROTEUS_PADS` + four `refdes=` rules + four `_ProteusPads` specs (about 40 lines)
in `gen_footprints.py`; TC the same pattern; the removed `pad_option_test.sh` (HV d1c551a, TC b6387d7).
Design: config key `PROTEUS_PADS = {refdes}`. For each named part, `footprints` derives a
`<footprint>_ProteusPads` spec (`ipc_sizes=True`, plus `pads` overrides from
`PROTEUS_PAD_OVERRIDES = {footprint: pads}`) and a `refdes=` rule placed before the part's rule.
HV / TC migrate only if their footprint names and the map come out identical. If not, they keep
the hand-written specs and use the key only for the check (CP1).

**F9. Pad sizes taken from the IPC are wrong for aperture-macro pads.** B, low priority. Source: HV21.
Design: `ipc_sizes="gerber"` (size and shape from the copper flash at the pad position;
check_sources could compare the two). HV's TO-220 case is solved by F4 `pads` in the meantime.

**F10. `assign` reports an unmatched part twice.** C, stdout only. Source: TC29.
Design: skip the "no IPC pads" message for refdes already reported as "no footprint rule".

**CF1. `check_footprints` accepts a THT part that fits only mirrored.** B (key presence = opt-in).
Sources: HV13, TC23.
Design: config key `BOTTOM_THT = {refdes: reason}`. When present, a THT part that fits only mirrored
and is not listed fails, and a listed part that fits unmirrored fails. Without the key: today's
note. Deciding the side from the Proteus assembly layer is X (X5).

**CF2. KiCad pad extends beyond the Proteus pad (square pin-1 pads).** B. Source: PR21.
Design: `check_footprints` prints a per-part note when the KiCad pad polygon is not inside the
Proteus pad polygon (stdout). With strict id `pad_extent` it fails.

**CF3. A missing `kicad/fp-lib-table` passes `check_footprints`.** B. Source: PF15.
Design: with F1's key set, `check_footprints` resolves every Footprint field through the project
fp-lib-table. Today only ERC warns (`footprint_link_issues`, made fatal by E1).

**F11. Shared pin-order check `p2k.check_pin_order`.** A (the largest lift). Sources: PA13, PR13,
PF13, HV14, TC20.
Board code: `tools/check_pin_order.py` in PA (256 lines), PR (330), PF (327), HV (199), TC (229).
Design:
- The board keeps a `PIN_ORDER` table in `p2k_config.py` (or in `gen_footprints.py`, which the
  tool imports): `[(refdes group, base, renumber, expect)]`.
- `expect` holds:
  - the residual class;
  - the mirror state;
  - the angle (TC20: a rotated BGA fits with residual 0);
  - `polarity` (PR: pin on the marked base pad 1);
  - `ideal=dict(n=…, pcd=…, gap=…)` for circular sockets (octal / noval);
  - `turn` (PF `turn_direction()`).
- base and renumber can be taken from the RULES / PROJECT spec (TC `rule()`), and a check then
  verifies that the tested renumbering equals the spec (PF13).
- Fits:
  - the unmoved base, both mirror states;
  - free rotation (PF `free_fit()`; `fpgeom._solve` snaps to 45°), added to `fpgeom` as
    `fit(..., free_angle=True)`;
  - bottom-side parts (PR / PF `bottom_side_noval()`).
- `--render` draws the overlay PNGs (matplotlib). PNGs are not part of the byte comparison.
- Sets `sys.dont_write_bytecode` (PR17).

The negative tests of the five boards (STATUS.md) become the tool's test list.

**CP1. Board pads of the Proteus-pad parts equal the gerber flashes.** A. Sources: HV24, TC28.
Board code: HV `tools/check_proteus_pads.py` (pcbnew pad polygon vs the gerber flash bounding box
±0.01 mm, area ±6 %, drill ±0.005 mm).
Design: `p2k check_pads` driven by `PROTEUS_PADS` (F8), with `AREA_TOL` as a key. HV's
`DECIDED` list stays independent of `PROTEUS_PADS` on purpose; the shared tool takes an optional
`PROTEUS_PADS_CHECK` list to keep that independence.

### 1.6 PCB build chain (`pcb`, `build_pcb`, `gerber`, `gerber_to_tracks`, `check_sources`, `ipc356`, `assign_nets`, `check_board`, `drc`)

**B1. Mounting-hole refdes `H<n>` collide with schematic refdes.** A. Source: PR18.
Board code: PR `tools/rename_holes.py` (H1-H13 → H101-H113 after `build_pcb`).
Design:
- Config keys `HOLE_PREFIX = "H"` and `HOLE_START = 1`, used by `build_pcb` (`f"{prefix}{start-1+i}"`)
  and by `ipc356_diff`'s hole pattern.
- `pcb.ipc_to_kicad_nets` keys pads of non-board-only footprints only.
- A collision with a schematic refdes prints an error and, under strict id `hole_refdes`, fails.
  It is not on by default: PR would otherwise fail before its migration.

PR migration: `HOLE_START = 101`, delete `rename_holes.py` and its make_pcb step; the board file
must be identical.

**B2. `unconnected-(…)` net names with "/" break the DRC parity.** C. Source: TC25.
Board code: TC `tools/pcb_wrap.py` `patch_export_netlist()`.
Design: `pcb.export_netlist` re-escapes `/` as `{slash}` inside `unconnected-(…)` names (only
there; hierarchical separators untouched). On by default: any board with such a name fails DRC
today, so no finished board has one unescaped (verify in the regression).

**B3. Aperture function `BGAPad,CuDef` not treated as a pad.** C. Sources: TC3, TC27, TC30.
Board code: TC `tools/check_sources.py`, `pcb_wrap.py` `patch_gerber()`, `phase4_wrap.py`
(`PAD_FUNCTIONS` + `BGAPad`).
Design: `BGAPad` = `SMDPad` in `check_sources`, `gerber_to_tracks` and `gerber_roundtrip.PAD_FUNCTIONS`.
On by default: these inputs raise or mismatch today, and no other board has them.

**B4. SMD pads without a part (test pads).** B. Sources: TC2, TC21, TC27, TC30.
Board code: TC `p2k_config.py` `SMD_FREE_PADS`, `tools/ipc356_check.py`, `tools/check_sources.py`,
`tools/pcb_wrap.py` (test-pad flashes moved to the OtherPad list; SMD check in check_board),
`tools/phase4_wrap.py`.
Design: config key `SMD_FREE_PADS = {(x, y): raw net}`. `ipc356 --check` accepts exactly those as
unplated SMD `free_pad` records. `check_sources`, `gerber_to_tracks`, `check_board`, `ipc356_diff`
and `gerber_roundtrip` handle `free_pad` records with `is_tht` False, with the TC wrapper's exact
checks: one board-only SMD pad at each position, F.Cu only, the IPC size and net, and exactly
`len(SMD_FREE_PADS)` such records. Without the key, behaviour is unchanged.

**B5. Two pads of one pin on different nets (`SPLIT_PIN_MERGES`).** B. Sources: TC1, TC24, TC30 (+ TC19: nothing
needed in footprints).
Board code: TC `tools/ipc356_check.py` (`SPLIT_PIN_NETS`), `tools/compare_netlists.py`,
`tools/pcb_wrap.py` (`d1_records`, `patch_assign_nets`), `tools/drc_check.py` (one unconnected
item per D1 net), `tools/phase4_wrap.py`.
Design: one config key `SPLIT_PIN_MERGES = {(refdes, pin): (kept net, merged net)}`, read by:
- `ipc356 --check`: accepts exactly these pins.
- `compare_netlists.load_ipc`: verifies the split pins equal the key and joins merged → kept.
- `pcb.ipc_records`: renames merged → kept in every record.
- `assign_nets`: accepts exactly the opens of those pins, with two groups and the pads in
  different groups.
- `drc`: accepts exactly one unconnected item per such net.
- `ipc356_diff`: accepts exactly those "KiCad net spans Proteus nets" pairs.

Accepted items are printed (stdout). `SPLIT_PIN_NETS` (TC's second key with the same content in
IPC order) is derived from the merges and dropped at TC's migration. Without the key, behaviour is unchanged.

**B6. `assign_nets --check` appends to the committed `reports/assign_nets.txt`.** B. Source: PA16.
Board code: `make_pcb.sh --check` compares only up to `== after zone fill` (sed) in PA and its
descendants.
Design: config key `ASSIGN_NETS_CHECK_REPORT = "reports/assign_nets_check.txt"`. When set, the
`--check` section goes there. Default: as now.

**B7. DRC acceptance per item, not per type.** B. Sources: PR20, PF17.
Design: config key `DRC_ACCEPTED_ITEMS = {type: {item pattern: reason}}` (pattern = regex on the
violation's item texts). Each entry must match at least one violation (stale entries fail), and
each pattern says how many items it matches (`count=`). `DRC_ACCEPTED` stays per type.

**B8. DRC warning counts not enforced.** B. Sources: PA25, PF23, PF25 (second half).
Board code: PF `tools/check_no_dangling.py` (fails on any `track_dangling` / `via_dangling` item in
`reports/drc.json`; added with the design fixes that removed the dangling stubs).
Design: config key `DRC_EXPECTED_WARNINGS = {type: count}`. When present, counts must match.
`{"track_dangling": 0, "via_dangling": 0}` replaces PF's script (its migration).

**B9. Pending user decisions as a tool state.** B. Sources: PA22, HV16, HV22, PF17, TC28.
Board code: PA `tools/phase4_checks.py` (now with `PENDING_DRILLS = False`, a pass-through); TC
`tools/drc_check.py` (empty `PENDING` tables); removed: PF/HV `tools/drc_pending.py` (HV d1c551a),
HV `tools/roundtrip_pending.py` (d666593).
Design: config key `PENDING = {tool: {item pattern: question}}` for `drc`, `assign_nets --check`,
`ipc356_diff`, `gerber_roundtrip` and `erc`:
- The tool passes (exit 0) only when its unaccepted failures are exactly the listed items.
- It prints `PENDING USER DECISION: <question>` per entry, which the `check_all.sh` template counts
  (PA's pattern).
- It fails when a listed item no longer occurs (the question is answered).

**B10. A track net edited in the board file is seen only by the byte compare.** B. Sources: HV18, PR24.
Design: `check_board` reads the track and via nets from the s-expression (not via pcbnew, which
re-derives them) and compares them with the `assign_nets` groups. Strict id `track_nets` while
it is new.

**B11. Shared `p2k.measure_gaps`.** A. Sources: PA15, PR19, HV15, TC28 (part), PF18.
Board code: `tools/measure_gaps.py`, identical in PSU, PA, PR, PF, HV (md5 5f414ce…); TC's
version adds per-layer tables and track widths and applies the `pcb_wrap` patches.
Design: lift TC's version without the wrapper (B3-B5 make it unnecessary). Per-layer output goes
behind `--per-layer`, so the default output equals the old script's. It is read-only and its output
is stdout, so nothing is byte-compared. Later options (B, low): a KiCad-geometry mode (PF18) and
every pad-to-track gap (HV19, KiCad reports only the closest pad).

**B12. Shared `p2k make_pcb [--check]`.** A. Sources: PA17, TC26.
Board code: `tools/make_pcb.sh` in all seven repos (seven variants).
Design: steps from config `PCB_STEPS` (default `["build_pcb", "gerber_to_tracks", "assign_nets",
"place_refdes"]`, plus `pours_to_zones` for boards with pours). Steps are chained so that the first
failure stops the run (TC26, PF25: `set -e` does not act inside `run() || …`, so in the template's
`--check` mode a failing gerber_to_tracks let the later steps run on and the filtered log hid its
ERROR line; the step still failed, but only as "not up to date"). The shared `make_pcb` must stop
on the first failing step and print that step's full output.
Per-board fix (for the migration step of each board, or earlier as a board commit): PA and PSU
still have the unchained `run()` of the pilot template; chain the steps with `&&` (TC, PF, PR and
HV already do: TC26, PF25, HV29). `--check` builds in
`build/tmp` and compares. Comes last in the PCB chain, after B1 / B4 / B5 remove the board-local
steps (`rename_holes.py`, `pcb_wrap.py`).

**B13. Per-refdes table of pad-caused DRC errors.** A, low priority. Source: TC28.
Board code: TC `tools/drc_pads.py`.
Design: `p2k drc --by-refdes` (a report option, stdout).

**B14. `build_pcb` net count includes off-board nets.** C, cosmetic, stdout. Source: PA18.

### 1.7 Phase 4 (`p2k.gerber_roundtrip`, `p2k.ipc356_diff`)

**R1. `parse_excellon` is metric 4.3 only and ignores `INCH`.** C (with A for the converter).
Sources: PA20, PR23, PF22.
Board code: `tools/excellon_metric.py` in PA, PR, PF (differ only in the file name) →
`reference/fab_2010_drill_metric.drl`.
Design: read `INCH` / `METRIC` and the zero / digit format from the header (2.4 inch with sign,
all digits). A file with an `INCH` header is parsed in inches. Today it is silently wrong and
no board feeds one directly, so this is on by default. Migration (optional): point `DRILL_EXCELLON` at the
original file and delete the converter and the `.drl`, but only if `reports/roundtrip/roundtrip.txt`
stays identical. Positions were rounded to 0.001 mm in the converted file; if the report prints
them, keep the converter.

**R2. Drills expected to be missing from an older Excellon file.** A. Source: PR3.
Board code: PR `tools/roundtrip.py` (`EXPECTED_NOT_IN_EXCELLON`).
Design: config key `EXCELLON_MISSING = {(x, y, drill): reason}`. Each entry must match exactly one
"not in the Excellon file" error (0.01 mm), and an unmatched entry fails. The report lines of
the wrapper ("expected, not an error: …") are kept word for word, because PR's committed
`roundtrip.txt` contains them.

**R3. `DRILL_SMALLER` matched by diameter pair, not by pad.** A. Sources: PR26, PF20, TC31.
Board code: `tools/check_drill_smaller.py` in PF, PR, TC (own IPC parser, exact pad set per pair).
Design: the value of a `DRILL_SMALLER` entry may be `(reason, pads)`, where pads is a set of
`(refdes, pin)` or refdes. Both tools then require exactly that pad set for the pair, and every
pair to be used. The plain `reason` form keeps today's matching. The report text is unchanged
(rule 3).

**R4. `ipc356_diff` accepts a Proteus N/C pad on any `unconnected-(…)` net.** B, strict id `nc_joined`
(as C2). Source: TC32.
Board code: TC `tools/phase4_wrap.py` (one record per `unconnected-(…)` net).

**R5. A missing, extra or shifted pad is classed "pad shape".** B, strict id `roundtrip_pad_match`.
Sources: PA21(b, c), HV20, PF21.
Design: require every pad flash of one set to overlap a pad flash of the other, and the pad
copper area per pad to be within a tolerance of the other set. Pad sizes vs the library after
manual edits (PF21) is the same check.

**R6. Only the first 10 drill errors are printed.** C, stdout. Source: PA21(a).

**R7. Track draw counts are not compared.** B. Source: TC33.
Board code: TC `tools/phase4_wrap.py` (`RT_DRAWS`, coverage line).
Design: compare the KiCad track count per layer with the Proteus Conductor draws that
`gerber_to_tracks` built (minus `REMOVED_TRACKS`). Strict id `roundtrip_counts`.

**R8. `ipc356_diff` calls no-net pads "N/C".** C, cosmetic, stdout. Source: PA23.

**R9. `main()` conventions differ (`ipc356_diff.main` returns, `gerber_roundtrip.main` exits).** C. Source: TC30.
Design: both return the exit code; `__main__` exits. Wrappers that call `gr.main()` under
`sys.exit` keep working.

**R10. Recorded Phase 4 counts.** B, low priority. Source: TC `phase4_wrap.py` `EXPECT` (part of TC30).
Design: config key `PHASE4_EXPECTED` (ipc356_diff summary counts). Optional.

### 1.8 `p2k.ipc356` reports

**I1. `TRUNCATED_NETS` checked by KiCad refdes only; unknown keys ignored.** C (+ strict).
Sources: PA11, PR1, PF2.
Design: accept the Proteus or the KiCad spelling (on by default: a mismatch there is a real
error; the regression run shows whether AC / PSU have such keys). Fail on a key that matches no
restored refdes under strict id `truncated_nets_keys`.

**I2. `TRUNCATED_NETS` cannot hold several nets of one refdes.** B, low priority. Source: PF4.
Design: a value can be `{pin: net}`.

**I3. Empty `NET_ALIASES` / `TRUNCATED_REFDES` print "Net aliases: ." / "…truncation: .".** C,
cosmetic. Sources: PA2, PR4, PF1, HV1, TC5.
**I4. The index groups `HT_0` under `HT_` (letter prefix).** C, cosmetic. Sources: PA2, PF1.
**I5. `pins_by_net.md` lists a two-hole pin twice.** C, cosmetic. Source: HV2.

These three change the committed `reference/pin_nets_by_component.md` / `pins_by_net.md` of
several boards (PA, PR, PF and TC print "." today; I5 may touch AC and PSU), so they break
rule 1. They wait for batch R (user decision D-R: re-baseline, each board its own commit).

**I6. IPC pin renumbering (`IPC_PIN_IDS`).** A (B for the shared key). Sources: user decision D-PIN
(HV J1, TC J11 ribbon headers: Proteus numbers the odd / even rows swapped), PF24 (relay
SERIES_MIX: Proteus G5V-2(HI) pads 1-8 → Omron pin numbers).
Board code: PF `p2k_config.py` `IPC_PIN_IDS` + `tools/ipc356.py` (wraps `ipc356.parse`, renames
after parsing; used instead of `p2k ipc356` in PF's `check_all.sh`).
Design:
- Config key `IPC_PIN_IDS = {KiCad refdes: {Proteus pin: KiCad pad}}` (PF's name and semantics),
  applied in `p2k.ipc356.parse` right after the records are read and refdes / nets are fixed up,
  so `reference/ipc356_pads.*`, the cheat sheets and every check that reads them (check_symbols,
  compare_netlists, footprints, check_footprints, check_board, pcb / build_pcb, ipc356_diff,
  gerber_roundtrip, check_pin_order) see the KiCad pad numbers. It is applied in exactly one place,
  like `NET_ALIASES` / `KICAD_REFDES`.
- Checks (PF wrapper semantics, rule 4): every listed refdes exists; the listed Proteus pins equal
  the IPC pins of that refdes, both ways (every pad renamed, only those); the targets are unique.
  Positions, nets and all other fields stay as Proteus wrote them; the copper is unchanged.
- Phase 4 (`ipc356_diff`) reads the Proteus side through `p2k.ipc356`, so it compares with the
  renamed ids; the KiCad side has the standard numbering. Decide in the batch whether to keep the
  Proteus id in an extra field (`pin_proteus`; PF24 notes that `FIELDS` does not allow one today).
  If the field changes the committed `reference/ipc356_pads.csv` / `.json`, it is only written
  when the key is present (rule 1).
- Without the key: byte-identical. Migration: PF deletes `tools/ipc356.py` (reference/ identical);
  HV J1 and TC J11 get the key with their footprints / symbols renumbered in the same board commit
  (a design change of those boards, reviewed there).

**I7. `REMOVED_TRACKS`: direction-exact match and a count per entry.** C (+ B for the count).
Sources: TC34, HV28 (design fixes 2026-11-20).
`p2k.pcb.removed_track` matches an entry in either direction (0.001 mm). A zero-length or
back-and-forth Proteus draw and its reverse (TC), or one segment drawn twice (HV: two zero-length
OUT1 draws), cannot be listed: two entries cannot be told apart (both draws match the first entry),
one entry matches two draws, and gerber_to_tracks fails ("2 listed, found 3 draws").
Board code: TC `tools/pcb_wrap.py` patch 5 (`patch_removed_track`: an entry in the drawn direction
within 1e-6 mm first, then the shared matcher); HV `REMOVED_DOT_TRACKS` + `tools/remove_dot_tracks.py`
(removes the listed identical draws with an exact count).
Design (one `REMOVED_TRACKS` design for both):
- Matching: an entry in the drawn direction (1e-6 mm) first, then today's direction-free match.
- Optional count: the value may be `(reason, n)`: the entry must match exactly n draws (identical
  draws, either direction after the exact pass). The plain `reason` form means exactly one draw,
  as today.
- Every mismatch fails: an entry that matches no draw or another number of draws than its count, a
  draw matched by two entries. gerber_to_tracks and gerber_roundtrip use the same matcher.
- On by default for the direction-exact pass (a board that would need it fails today; with single
  entries both directions still match as now); the count is opt-in by its value form. Batch 7a.
- Migration: TC drops patch 5; HV moves `REMOVED_DOT_TRACKS` into `REMOVED_TRACKS` with a count
  and deletes `tools/remove_dot_tracks.py` (board file identical). (TC34 also notes that its
  board-local `check_drill_smaller.py` had to learn `REMOVED_VIAS`: covered by R3's pad sets.)

### 1.9 Infrastructure, docs and playbook

**G1. Stale bytecode of `p2k_config.py`.** C. Sources: PF3, PR17.
Board code: `check_all.sh` sets `PYTHONDONTWRITEBYTECODE=1` and removes `__pycache__` (PF);
`sys.dont_write_bytecode` in PR `check_pin_order.py` / `check_drill_smaller.py`.
Design: `board.config()` loads the config by `exec` of the source (or with bytecode writing off),
so an edit within the same second is never ignored. No output change; on by default.

**G2. Regression harness for all seven boards.** B (needed by every batch; not a board
candidate). Source: playbook section 4, this plan.
Design: `tools/regress.sh` in this repo (developer script, not in `p2k/`). For each board it clones
into a temp directory and points `tools/p2k` at the candidate commit. It runs the board's
generators (`make_part_map.py`, `gen_symbols.py`, `tools/sheets/sheet*.py`, `gen_footprints.py`,
`make_pcb.sh`, `p2k ipc356`, plus board extras such as `excellon_metric.py`), then
`tools/check_all.sh`, and requires `git status --porcelain` to be empty. For AC it runs AC's own
`check_all.sh` and `regen_check.sh`. Board repos are not changed.

**G3. Playbook / README rules.** D. Sources: PA8, PA9, PA19, PR5, PF12, PF8, TC10, TC26, HV19, PR24, HV18.
- N/C pads: a passive pin with a no-connect flag, never a `no_connect`-type pin. ERC
  `no_connect_connected` is fatal, but ERC alone is not enough, because a wire that reaches other
  pins only through a label gives no message; the netlist comparison catches it (PA19).
- Valve symbols: the KiCad envelope is drawn in every unit, so decide the heater-unit layout up
  front (PA8). Set valve pins `passive` (PA9 / S1).
- Draw the driving sheets first, otherwise there are interim `pin_not_driven` errors (PF12).
- No KiCad THT symbol exists for J112 / IRF820: use the generic substitutes (PF8).
- `make_pcb` template: chain the steps with `&&` (TC26).
- KiCad DRC reports one violation per track against its closest pad, so a conflict can hide
  behind a neighbour (HV19).
- A track net edited in the board file is invisible to everything but the byte compare until B10 (PR24, HV18).
- Every batch documents its new keys in README.md (config table) and the footprint spec keys in
  the `footprints.py` docstring.

### 1.10 Not worth doing / superseded (X)

- **X1** HV10: the `SEND` / `SEND1` crash does not occur on HV. This is an observation and is
  covered by C1.
- **X2** TC19: two pads with one number on different nets need nothing in `footprints` (`fpgeom.fit`
  pairs them); the net side is B5.
- **X3** TC17 `fpga_unit()`, `ball_texts()`, `rail_bus()` and TC18 (global labels beyond the longest
  local label): FPGA-specific layout code of one board. Only the PDF row lookup is kept (K9).
- **X4** HV26: `place_refdes` putting four 0402 references on Fab only was accepted by the user. A
  third text size is not worth a change until a board needs it.
- **X5** HV13 / TC23 (side source): deciding the side of a THT part from the Proteus assembly layers.
  `BOTTOM_THT` (CF1) states it explicitly, which is simpler and reviewable.
- **X6** HV22 (second half): classifying a Proteus copper island enclosed by KiCad clearance as
  "clearance". It was a one-off; the J2 decision removed it, and R5 / B9 cover the general case.
- **X7** PR17: the bytecode pitfall in a board script is the same issue as G1.
- **X8** PA21(c): a 0.1 mm pad shift seen only by the drills is part of R5.

### 1.11 Counts

| Class | n | Entries |
|---|---|---|
| A | 22 | M1 S1 S2 S3 K1 K4 K8 E1 F1 F2 F4 F5 F7 F11 CP1 B1 B11 B12 B13 R2 R3 I6 |
| B | 34 | P3 S4 S5 S6 S7 K2 K3 K5 K6 K7 K9 C2 C3 C4 F3 F6 F8 F9 CF1 CF2 CF3 B4 B5 B6 B7 B8 B9 B10 R4 R5 R7 R10 I2 G2 (deferred / low: S6 K5 K6 K9 F9 R10 I2) |
| C | 20 | P1 P2 P4 C1 C5 C6 F10 B2 B3 B14 R1 R6 R8 R9 I1 I3 I4 I5 I7 G1 (P4 and R1 have a B / A part) |
| D | 1 | G3 (eight playbook rules) |
| X | 8 | X1-X8 |

## 2. Strict check ids (`STRICT_CHECKS`)

| Id | Entry | Fails when |
|---|---|---|
| `no_connect_pin_type` | S7 | an on-board symbol has a `no_connect`-type pin |
| `nc_joined` | C2, R4 | an `unconnected-(…)` net has more than one pin / pad |
| `net_names` | C4 | a named KiCad net matches an IPC net of another name (after aliases) |
| `derive_max_move` | F3 | a derived pad moves more than the limit, or a base fits only mirrored without `bottom` |
| `pad_extent` | CF2 | a KiCad pad reaches outside the Proteus pad |
| `hole_refdes` | B1 | a hole refdes equals a schematic refdes |
| `track_nets` | B10 | a track / via net in the file differs from the assign_nets group |
| `roundtrip_pad_match` | R5 | a pad flash has no counterpart, or its area differs |
| `roundtrip_counts` | R7 | track counts per layer differ |
| `truncated_nets_keys` | I1 | a `TRUNCATED_NETS` key matches no restored refdes |

Implemented (registered in `board.STRICT_IDS`): `no_connect_pin_type`, `nc_joined` (compare_netlists part;
the ipc356_diff part R4 comes with batch 8), `net_names` (batch 3, 2686b4b).

Keys whose presence is the opt-in (no strict id needed): `BOTTOM_THT`, `ERC_FATAL_WARNINGS`,
`DRC_EXPECTED_WARNINGS`, `DRC_ACCEPTED_ITEMS`, `PENDING`, `EXCELLON_MISSING`, `SPLIT_PIN_MERGES`,
`SMD_FREE_PADS`, `PROTEUS_PADS`, the pad-set form of `DRILL_SMALLER`.

## 3. Implementation order

### Regression set (every batch)

1. Commit the batch here on a branch, with README / docstrings updated.
2. Run `tools/regress.sh <commit>` (G2). It covers:
   - **AC** at the new commit: `tools/check_all.sh` and `tools/regen_check.sh` pass with AC's
     committed files unchanged (playbook section 4).
     Note: AC is pinned at 32f8fd3 and still uses its own copies of the Phase 2-4 tools. AC
     therefore covers only `ipc356`, `check_sources`, `gerber`, `sexpr`, `geom2d`, `fpgeom`,
     `symgen`, `sheetkit`, `sch_builder`, `compare_netlists`, `check_symbols`, `make_skeleton`.
   - **PSU, PA, PR, PF, HV, TC** with `tools/p2k` bumped (scratch clone, not committed): each board's
     `tools/check_all.sh` passes, and a full regenerate leaves the clone byte-identical. **These six
     boards are the only regression for `footprints`, `check_footprints`, the PCB chain and Phase 4.**
3. Fast-forward main, push. Then bump `tools/p2k` in the board repos, each as its own commit with
   `check_all.sh` passing, before any board migrates to new keys.
4. **Migration** (per board, a separate commit after the bump): replace the board-local workaround
   with the shared key, delete the wrapper, regenerate, and require byte-identical output and a
   passing `check_all.sh`. A board that wants a new strict id adds it in the same commit (it must
   pass). The board's STATUS.md candidate gets "solved in p2k <commit>".
5. Finally bump the board submodules in `tube_amplifier_kicad` (the meta repo).

### Batch 0: baseline (no p2k change)

- Files: `tools/regress.sh` (G2).
- Bring AC from 32f8fd3 to c5682dd and confirm that `check_all.sh` and `regen_check.sh` pass
  (32f8fd3..c5682dd added the Phase 2-5 modules, which AC does not import). Without this, AC's
  first bump would carry eight unrelated commits.
- Record the green baseline of all seven boards at c5682dd.

### Batch 1: robustness and crash fixes (default on, no output change)

- Files: `p2k/board.py` (G1, plus `board.strict()`), `p2k/compare_netlists.py` (C1, C5, C6),
  `p2k/footprints.py` (F10 only), `p2k/build_pcb.py` (B14 print), `p2k/ipc356_diff.py` (R8, R9),
  `p2k/gerber_roundtrip.py` (R6, R9).
- Extra regression check: the stdout differences only.
- Migration: in PR and PF `tools/compare.py`, drop `natkey` / `pinkey` (keep the joined-N/C part
  until batch 3). In PF `check_all.sh`, the `PYTHONDONTWRITEBYTECODE` workaround may stay or go.

### Batch 2: Proteus project reader and part map

- Files: `p2k/pdsprj.py` (P1, P2, P3, P4), new `p2k/part_map.py` (M1).
- Migration: `tools/make_part_map.py` of PA, PR, PF, HV, TC becomes data plus `part_map.run`, with
  `lib/part_map.csv` identical. TC `gen_symbols.ti60_pinout` → `dsn_pinouts(brace_optional=True)`,
  with `lib/*.kicad_sym` identical. PSU optional (it has `FITTED_VALUES`; map that to `PART_OVERRIDES`).

### Batch 3: symbols, netlist comparison, ERC

- Files: `p2k/symgen.py` (S1-S5), `p2k/check_symbols.py` (S7), `p2k/compare_netlists.py` (C2, C3, C4),
  new `p2k/erc.py` + `bin/p2k erc` (E1).
- Migration:
  - `gen_symbols.py` helpers of PA, PR, PF, TC → `from_std` / `multi_box` keywords (symbol
    libraries identical).
  - PR, PF delete `tools/compare.py` (with `nc_joined`); TC's `compare_netlists.py` keeps only
    the D1 part until batch 7.
  - Every board: `erc.sh` → `p2k erc` with its `ERC_FATAL_WARNINGS`; TC `erc_check.py` /
    `erc_pending.txt` → `ERC_PENDING`.
  - PA, PR, PF, HV, TC add `no_connect_pin_type` and `nc_joined`.

### Batch 4: schematic drawing

- Files: `p2k/sch_builder.py` (K1, K2, K3, K7), `p2k/sheetkit.py` (K4, K5), `bin/p2k sheets` (K8).
- Extra regression check: AC and PSU sheets byte-identical. Both call `terminal()` and
  `Sheet.place`; this batch carries the highest risk for AC.
- Migration: `tools/sheets/_common.py` of PA, PR, PF, HV, TC replace `place_mirrored`,
  `place_rot90_mirrored`, `term()`, `hcap` / `ph` / `_hcap` with the shared parameters. Every
  `kicad/*.kicad_sch` must be identical. TC `make_sheets.sh` / PF `check_sheets.sh` → `p2k sheets --check`.

### Batch 5: footprints

- Files: `p2k/footprints.py` (F1, F2, F3, F4, F5, F6, F7, F8), `p2k/check_footprints.py` (CF1, CF2, CF3),
  `p2k/fpgeom.py` (free-angle fit, needed by F11 in batch 6).
- Migration (`lib/<PROJECT>.pretty/*`, `lib/footprint_map.csv`, `kicad/fp-lib-table`,
  `reports/footprint_*.csv` identical):
  - PA, PR, PF, HV, TC: `fp_lib_table()` → `FP_LIB_TABLE = True`.
  - PA, PR, PF: the `bottom` wrapper removed.
  - HV `_derive_pads`, PR `derive_with_pad_shapes`, PF's derive wrapper → `pads` / `pin1_dot`.
  - PA `octal_body`, PR `noval_body` → `body_from`.
  - TC `smd_pad` wrapper → spec key.
  - HV / TC `PROTEUS_PADS` → F8 only where identical.
  - Boards that want them add `BOTTOM_THT` (HV, TC: J3, PA/PR/PF sockets) and `derive_max_move`.

### Batch 6: pin order and pad checks

- Files: new `p2k/check_pin_order.py` (F11), new `p2k/check_pads.py` (CP1), `bin/p2k`.
- Migration: `tools/check_pin_order.py` of PA, PR, PF, HV, TC → a `PIN_ORDER` table plus
  `p2k check_pin_order`. Every board's negative tests from its STATUS.md are re-run against the
  shared tool before the board script is deleted. HV `check_proteus_pads.py` → `p2k check_pads`.

### Batch 7: PCB build chain

Batch 7 is split into three commits, each regression-tested.

**7a** (default-on fixes and holes)
- Files: `pcb.py` (B2, B1 keying, I7), `build_pcb.py` (B1), `gerber.py` / `check_sources.py` /
  `gerber_to_tracks.py` (B3), `ipc356.py` (I6 `IPC_PIN_IDS`).
- Migration: PR `rename_holes.py` removed (`HOLE_START = 101`, board file identical); TC
  `pcb_wrap.py` patch 5 and HV `remove_dot_tracks.py` removed (I7); PF `tools/ipc356.py` removed (I6, reference/ identical);
  HV J1 / TC J11 renumbered with `IPC_PIN_IDS` (board design commits).

**7b** (TC features)
- Files: `ipc356.py`, `check_sources.py`, `pcb.py`, `gerber_to_tracks.py`, `assign_nets.py`,
  `check_board.py`, `drc.py`, `compare_netlists.py` (B4, B5).
- Migration: TC `ipc356_check.py`, `check_sources.py`, `pcb_wrap.py`, the D1 part of
  `compare_netlists.py` and of `drc_check.py` removed; `SPLIT_PIN_NETS` dropped. TC's board file
  and reports must be identical.

**7c** (acceptance and pending)
- Files: `drc.py` (B7, B8, B9), `assign_nets.py` (B6, B9), `check_board.py` (B10).
- Migration: PA `phase4_checks.py`'s drc part, if any, and TC `drc_check.py` → `PENDING` /
  `DRC_ACCEPTED_ITEMS`. PA and its descendants: the `make_pcb.sh` sed workaround → B6 only if the
  board wants to re-baseline `reports/assign_nets.txt` (otherwise keep).

### Batch 8: Phase 4

- Files: `p2k/gerber_roundtrip.py` (R1, R2, R3, R5, R7, B3 / B4 / B5 parts, B9), `p2k/ipc356_diff.py`
  (R3, R4, B1 pattern, B4 / B5 parts, B9, R10).
- Migration:
  - PR `roundtrip.py` → `EXCELLON_MISSING`, with the report lines identical.
  - PF, PR, TC `check_drill_smaller.py` → the pad-set `DRILL_SMALLER`.
  - TC `phase4_wrap.py` removed (strict ids `nc_joined`, `roundtrip_counts`, plus `PHASE4_EXPECTED`).
  - PA `phase4_checks.py` removed (no pending item left).
  - PA, PR, PF `excellon_metric.py` only if `roundtrip.txt` stays identical (R1).

### Batch 9: tools that come last

- Files: new `p2k/measure_gaps.py` (B11), `p2k/drc.py --by-refdes` (B13), new `p2k/make_pcb.py` +
  `bin/p2k make_pcb` (B12).
- Migration: `tools/measure_gaps.py` deleted in PSU, PA, PR, PF, HV, TC. `tools/make_pcb.sh` →
  `p2k make_pcb` with `PCB_STEPS` once a board has no board-local step left. The board file must
  be identical in both modes. Until then PA and PSU chain their `run()` steps with `&&` (B12).

### Batch 10: docs

- Files: `CONVERSION_PLAYBOOK.md` (G3 rules, the new keys in the table of section 4, the
  `regress.sh` procedure), README.md (anything not documented by earlier batches).

### Batch R (needs the user): report text fixes

- Decided (D-R): re-baseline. Files: `p2k/ipc356.py` (I3, I4, I5); later batches can add cosmetic
  report changes here.
- These deliberately change committed `reference/*.md` files. Each board commits the
  re-generated files together with the bump. For AC this is an exception to the "committed files
  unchanged" rule, and only if I5 touches AC.

### Batch Z (needs the user): flip strict defaults

Decided (D-Z): when every board has either opted in to all strict ids or recorded an opt-out, the
default of `STRICT_CHECKS` becomes `"all"` (new boards get the checks automatically), and boards
keep explicit opt-outs in their config.

### Deferred (no batch yet)

S6, K6, K9, F9, I2, R10, and the B11 extensions (KiCad-geometry gaps, all pad-to-track gaps).
They are picked up when a board needs them.

## 4. Open questions for the user

All three were answered on 2026-11-20 (section "Decisions": D-R, D-Z, D-AC). Kept for the record:

1. **Report text fixes (batch R):** re-baseline the committed `reference/*.md` of PA / PR / PF / TC
   (and AC / PSU if `pins_by_net.md` changes), or leave the cosmetic issues as they are?
2. **Strict defaults (batch Z):** flip `STRICT_CHECKS` to on-by-default at the end, or keep every
   stricter check opt-in for good?
3. **AC regression scope:** AC (pinned at 32f8fd3) still runs its own copies of the Phase 2-4
   tools, so it does not exercise `footprints`, the PCB chain or Phase 4. Is the six-board set
   (PSU + five) acceptable as the regression for those modules, or should AC be switched to the
   shared tools first (the playbook's "switch its scripts to the moved tool")?

## 5. Status per entry

| Entry | Batch | Status |
|---|---|---|
| G2 regression harness | 0 | **done**: `tools/regress.sh` (README "Developing p2k: regression run") |
| AC pin 32f8fd3 → c5682dd | 0 | **done in scratch only**: AC's `check_all.sh` and `regen_check.sh` pass at c5682dd with AC's committed files unchanged (section 6). The bump itself is committed later, in the coordinated pin step |
| baseline, all seven boards at c5682dd | 0 | **done**, green (section 6) |
| G1 config without bytecode | 1 | **done** (6cf43b1): `board.config()` compiles and executes the source; negative test: same-size edit with the mtime reset after a load that left a `.pyc` → old code read the stale value (VAL = 1), new reads VAL = 2 |
| `board.strict()` / `STRICT_CHECKS` | 1 | **done** (6cf43b1): `strict_checks()`, `strict(id)`, `STRICT_IDS` (empty: no strict check implemented yet; each batch adds its id); `"all"`; an unknown id in the config fails ("unknown check id(s) ['nc_joind']"), an unregistered id in code raises |
| C1 type-safe natkey / pinkey | 1 | **done** (6cf43b1): order identical to the old keys on 19914 random pairs where those did not raise; the old keys raised on 86, the new ones sort them; `compare()` with SEND + SEND1 drawn: old TypeError, new reports the merge / split. Migration (PR, PF): drop `natkey` / `pinkey` from `tools/compare.py`, keep the joined-N/C part until batch 3 |
| C5 `P2K_SCHEMATIC` default | 1 | **done** (6cf43b1): with the variable set to a missing file the export fails on that file (exit 1); a relative value from another directory works ("compare: OK") |
| C6 net counts without N/C pins | 1 | **done** (6cf43b1), stdout: PA "IPC nets touched 43, complete 43 (+ 4 N/C / no-net pins)" (was 47) |
| F10 one message per unmatched part | 1 | **done** (6cf43b1): PA with its first rule dropped: 19 "no footprint rule" lines, the 19 duplicate "has no IPC pads" lines are gone |
| B14 build_pcb net count | 1 | **done** (6cf43b1), stdout: PA "47 nets (+6 off-board)" (was 53) |
| R6 all drill errors printed | 1 | **done** (6cf43b1): PA with `DRILL_SMALLER = {}`: 12 ERROR lines (was 10) |
| R8 "without a net" | 1 | **done** (6cf43b1): PA "142 pads incl. 0 N/C and 4 without a net"; a no-net pad moved to a net in the KiCad export: "pad U6-6: no net in Proteus, KiCad net B-" (was "N/C in Proteus"). The summary line is unchanged on boards without no-net pads (TC's `phase4_wrap.py` parses it) |
| R9 `gerber_roundtrip.main` returns | 1 | **done** (6cf43b1): `--check` with errors exits 1, without `--check` 0; PR `roundtrip.py` (`sys.exit(gr.main())`) and TC `phase4_wrap.py` pass in the regression |

| P1 Latin-1 strings | 2 | **done** (0b7a294): `latin1=False` keyword on `dsn_values`, `lyt_parts`, `dsn_devices`, `_strings`; with `True` bytes 0xA0-0xFF are decoded and `lyt_parts` strips a trailing 0xFF of a net token (PF's reader). Default byte-identical on all seven boards (dsn_values / lyt_parts / dsn_pinouts compared old vs new with each board's refdes set). With `True`: PA C5 `10µ/400V` (value, device, record), PR C4 `MMK J04 1µ`, C14 `1µ mmk`, C30 `22µ`, C28 / C33-C37 `22µF 450V` (value + layout record), PF C1 / C36 / C37: exactly the `LATIN1_PARTS` texts of the boards. Negative: PA / PR part map without `LATIN1` → "C5: no layout record in ROOT.LYT" / 9 errors |
| P2 one-character refdes | 2 | **done** (0b7a294): cause found: every COMPONENT ID label is preceded by its font record `0xFF 0x01 "Default Font\0"`, which `pstr` reads as the string "D", so a refdes `D` matched every part. A one-character refdes is now accepted only in the refdes slot (first string before the font record, the font record skipped); the LYT keeps one-character tokens when a one-character refdes is requested (PF's `_strings(min_len=1)`). Only boards with such refdes see a change (PR: old raised "several schematic values" for D, new A-D = TERMINAL; PF: old lyt_parts raised "two layout records for -6", new = PF's local reader). Negative: without the font skip PR raises again; synthetic DSN with an "A" string before a part not in refs: old misattributes (raises), new reads only the real A record |
| P3 layouts without EID | 2 | **done** (0b7a294): `dsn_devices(prj, refs, latin1=False)` (HV `dsn_devices`, unchanged rules, shared refdes search) and `lyt_parts_ref(prj, refs, values)` (TC `lyt_packages`, returns `{ref: {"package"}}`); HV / TC part maps identical through them |
| P4 pinouts | 2 | **done** (0b7a294): `dsn_pinouts(brace_optional=True)` (TC regex, `PIN` lines without `{`, pin value = element list); TC `ti60_pinout()` on it gives the same 225 balls (8 copies) and `lib/tube_control_card.kicad_sym` byte-identical; default mode still 0 pins for BGA225. New `dsn_device_pins(prj)`: per `{*DEVICE}` block (4-byte text length before it) the device name (length-prefixed string after the previous block's text; after the last 0xFFFE marker for the first; the library ends where no record follows, later `{*DEVICE}` texts are component properties), DESC, default package, `$PIN*` records `(kind, name, number)` with one-character names, `pinouts`, `numbers` (package pinout overrides the symbol number). Checked: PA TIPL760 B1 C2 E3, MPSA92 E1 B2 C3, 1N4001 A K; PF 12AX7 `numbers` A1=1 A2=6 H1=4 (symbol A1=6); every device named on all boards. Note: PA 1650TA has 7 pin records (P1 P2 S2-S5 CT), no S1 |
| M1 `p2k.part_map` | 2 | **done** (0b7a294): `part_map.run(SPEC)`, SPEC keys in the module docstring. Migration proven in scratch for all six boards (`--check` against the committed `lib/part_map.csv`, text-identical): PSU (`PART_OVERRIDES` = its `FITTED_VALUES`, `PAGE_CHECK=False`: C91 has no PDF text), PA (`LATIN1`, `OFF_BOARD` 3-tuples; `LATIN1_PARTS` dropped), PR (`LATIN1`; `SINGLE_LETTER_PARTS` and `LATIN1_PARTS` dropped), PF (`LATIN1`, `CHECK_UNUSED_DEVICES`; local readers and `LATIN1_PARTS` dropped), HV (`LAYOUT="dsn"`, `"LT1014": None` + `SYMBOL_BY_VALUE`, `OFF_BOARD` 2-tuples), TC (`LAYOUT="ref"`, `SYMBOL_BY_VALUE` / `_REFDES` / `_PACKAGE` {RESISTOR ARRAY 2X1MM: R_ARRAY4} / `_PREFIX` {R, C, L}, `UNIT_PAGES`, `FIELDS` with `package`, `units`). Negative tests: stale override ("pdsprj reads value '10µ/400V' itself, drop the entry"), unknown override refdes, OFF_BOARD part with pads, override page not among the PDF hits, missing page override, unknown device, device mapped to None without a value symbol, unused device (PF), TC without the prefix map, bad UNIT_PAGES refdes, unknown FIELDS / spec key, wrong LAYOUT, PSU without its override → "out of date" |
| S1 `from_std(pin_types=)` | 3 | **done** (2686b4b): `{number or "*": type}` after the renumbering, every listed number must exist (assert). PA `pin_types()`, PF `pin_types()` (`{"*": PAS}`) → keyword: libraries byte-identical (scratch clone, regenerate, clean status). PA keeps its `add_pins()` (not a candidate) |
| S2 `from_std(units=, hide_pin_names=)` | 3 | **done** (2686b4b): `units={new: old}` (unknown or colliding units assert), `hide_pin_names=True` (PR's node). PR `swap_units()` / `hide_pin_names()` / `pin_types()` → keywords: library byte-identical |
| S3 `multi_box` | 3 | **done** (2686b4b): TC's function, `desc=""` and box options as keywords; TC library byte-identical with the board copy deleted |
| S4 `box(pin_length=)` | 3 | **done** (2686b4b): default 2.54 gives the identical node; 5.08 moves the pin ends out by 2.54 and sets the length |
| S5 `box(tb_margin=)` | 3 | **done** (2686b4b): extra body height beyond one grid row on a side with top / bottom pins (None = the name-based rule); `tb_margin=0` makes a VCCA-on-top box 7.62 mm shorter |
| S7 strict `no_connect_pin_type` | 3 | **done** (2686b4b): check_symbols fails per on-board part with a `no_connect`-type pin. PA, PR, PF, HV, TC pass with the id; **PSU fails** (DRV5 / DRV6 SI8275 pins 7, 12, 13; L1-L3 RM8_INDUCTOR pins 1, 2): before D-Z PSU fixes those symbols (passive + no-connect flag) or records an opt-out |
| C2 strict `nc_joined` | 3 | **done** (2686b4b): compare_netlists fails on an `unconnected-(…)` net with more than one pin (counted before the off-board / power filter, as the wrappers). Negative: PF netlist with SERIES_MIX pads 4 and 6 in one unconnected net → without the id "compare: OK", with it "KiCad net unconnected-(SERIES_MIX-Pad4) joins 2 pins" (PF's wrapper reports the same pair). PR, PF, PA, HV pass with the id; TC passes through its D1 wrapper. R4 (ipc356_diff part) stays in batch 8 |
| C3 per-unit pages | 3 | **done** (2686b4b): part-map column `units`; pins of a unit that is not drawn are warnings ("pending pin U10-R4 …: unit B on sheet 5 not drawn") with `--allow-missing` or a `--sheet` other than the unit's page, errors otherwise (as missing parts); a split IPC net counts the page of such pins as pending. Test: TC netlist without the 23 unit-B pins: `--sheet 2` / `--allow-missing` → OK with 23 warnings (old p2k: 23 errors), no option or `--sheet 5` → 23 errors. Finished boards unchanged (TC has the column) |
| C4 strict `net_names` | 3 | **done** (2686b4b): a name hint becomes an error "net name: KiCad net … = IPC net …". Negative: PF netlist with HYPPY2 renamed → warning without, error with the id. PA, PR, PF, HV, TC (wrapper) pass with it; HV can drop its `check_all.sh` grep guard |
| E1 `p2k erc` | 3 | **done** (2686b4b): `ERC_FATAL_WARNINGS`, `ERC_PENDING` (TC's `"type REF PIN"` keys), `--no-pending`, report `reports/erc.rpt`. Same verdict as the board `erc.sh` on PSU, HV (no keys), PA (`{"no_connect_connected"}`), PR, PF (`+ "footprint_link_issues"`), TC (`{"no_connect_connected"}`, empty `ERC_PENDING` = its empty `erc_pending.txt`). Negative: PF without `kicad/fp-lib-table` → 89 FATAL WARNING footprint_link_issues, exit 1; synthetic report: error fails, fatal warning fails, matching pending entry passes, stale pending entry fails. AC keeps its `erc.sh` (D-AC) |

Every other entry: open.

## 6. Regression record

`tools/regress.sh` runs; HEAD = the board commit cloned (board repos move on in parallel, so a
later run may use newer HEADs). PASS = regenerate + `check_all.sh` + clean `git status`
(AC: its `check_all.sh` + `regen_check.sh` + clean status).

**Batch 0 baseline, p2k c5682dd, 2026-11-20** (`tools/regress.sh -j 4 c5682dd`): ALL BOARDS PASS.

| Board | HEAD | Committed pin | Result |
|---|---|---|---|
| tube_psu_kicad | 6f97fae | c5682dd | PASS (91 s) |
| tube_power_amp_kicad | d842156 | c5682dd | PASS (59 s) |
| tube_preamp_kicad | b2adf22 | c5682dd | PASS (95 s) |
| tube_pi_fxloop_kicad | da551f3 | c5682dd | PASS (69 s) |
| tube_hv_supplies_kicad | 1ab2658 | c5682dd | PASS (48 s) |
| ti60f225_tube_control_card | d2a4f66 | c5682dd | PASS (56 s) |
| ac_psu_kicad | 559175a | **32f8fd3** (tested at c5682dd) | PASS (396 s) |

Harness negative test: a throwaway p2k commit on c5682dd that changed one heading of
`reference/pin_nets_by_component.md` → `tube_power_amp_kicad` FAIL ("working tree differs:
M reference/pin_nets_by_component.md"; note that `check_all.sh` alone passes after a regenerate,
so the clean-status step is what catches it). Found while writing the harness: an exported
`P2K_BOARD` made AC's `regen_check.sh` (whose `make_skeleton --force` runs in a copy) write into
the scratch clone; the harness now unsets `P2K_BOARD` / `P2K_PCB` / `P2K_SCHEMATIC` /
`P2K_REPORTS`. PF regenerates `reference/` through its wrapper `tools/ipc356.py`
(`IPC_PIN_IDS`), so the harness uses a board's `tools/ipc356.py` when it exists.

**Batch 1, p2k 6cf43b1, 2026-11-20** (`tools/regress.sh -j 4 6cf43b1 <board>@<HEAD> ...`, then the
same board commits at c5682dd for the stdout diff): ALL BOARDS PASS at 6cf43b1.

| Board | HEAD | Result at 6cf43b1 | stdout vs c5682dd (same HEAD) |
|---|---|---|---|
| tube_psu_kicad | 842d301 | PASS | compare counts (C6), ipc356_diff "9 N/C and 6 without a net" (R8) |
| tube_power_amp_kicad | 18de7a9 | PASS | build_pcb "47 nets (+6 off-board)" (B14), compare (C6), ipc356_diff (R8) |
| tube_preamp_kicad | b2adf22 | PASS | compare counts (C6) |
| tube_pi_fxloop_kicad | da551f3 | PASS | compare counts (C6) |
| tube_hv_supplies_kicad | 23284f8 | PASS | compare counts (C6) |
| ti60f225_tube_control_card | d2a4f66 | PASS | compare counts (C6) |
| ac_psu_kicad | 559175a (pin 32f8fd3) | PASS | compare counts (C6; AC uses `p2k.compare_netlists` through its v4 wrapper) |

No other stdout line and no generated file changed.

**Batch 2, p2k 0b7a294, and batch 3, p2k 2686b4b, 2026-11-20** (`tools/regress.sh -j 4 <commit>
<board>@<HEAD> ...` on the board commits recorded at the start of the session; batch 2 compared with
95610dc, batch 3 with 0b7a294 on the same commits): ALL BOARDS PASS at both commits.

| Board | HEAD | 0b7a294 | 2686b4b | stdout vs previous p2k (same HEAD) |
|---|---|---|---|---|
| tube_psu_kicad | 842d301 | PASS | PASS | identical |
| tube_power_amp_kicad | 18de7a9 | PASS | PASS | identical |
| tube_preamp_kicad | b2adf22 | PASS | PASS | identical |
| tube_pi_fxloop_kicad | da551f3 | PASS | PASS | identical |
| tube_hv_supplies_kicad | 6cb1d3f | PASS | PASS | identical |
| ti60f225_tube_control_card | d2a4f66 | PASS | PASS | identical, except one KiCad DRC `unconnected` item text that differs between runs (track length 1.5649 / 1.5935 mm of Net-(RS1-B): KiCad picks another track pair; not p2k) |
| ac_psu_kicad | 559175a (pin 32f8fd3) | PASS | PASS | identical |

The board scripts were not changed, so these runs prove the defaults. The migrations (part maps,
symbol libraries, compare wrappers, `erc.sh`) were proven separately in scratch clones (section 5).
PSU (89608a5) and AC (0a71dd3) got new commits during the session; they are not in this record.

## 7. Next batch: prompt for the next session

> You work in ~/dev/proteus2kicad (main). Task: batch 4 of docs/CANDIDATES.md (schematic drawing:
> K1, K2, K3, K7 in `p2k/sch_builder.py`, K4, K5 in `p2k/sheetkit.py`, K8 `p2k sheets`), then batch 5
> (footprints: F1-F8 in `p2k/footprints.py`, CF1-CF3 in `p2k/check_footprints.py`, the free-angle fit
> in `p2k/fpgeom.py`) the same way if batch 4 is committed and green. Read CANDIDATES.md first
> (section 0 rules, "Decisions" D-R / D-Z / D-AC / D-PIN, section 3 batches 4-5, sections 5-6 for
> what is done), README.md and CONVERSION_PLAYBOOK.md section 4. Every change: no key, no change
> (defaults byte-identical on all boards), strict checks register a `board.STRICT_IDS` id (opt-in),
> negative-tested, documented in README.md (footprint spec keys also in the `footprints.py`
> docstring). Batch 4 carries the highest risk for AC and PSU (both call `terminal()` and
> `Sheet.place`): their sheets must stay byte-identical. Regression: commit, record the board HEADs,
> then `tools/regress.sh -j 4 <commit> <board>@<HEAD> ...` (all seven boards, scratch clones only)
> and the previous p2k commit on the same list; diff the per-board logs (TC's DRC `unconnected` item
> text varies between runs). Prove each board migration in a scratch clone (`git clone` of the board,
> `tools/p2k` symlinked to this repo, regenerate, clean `git status`): `_common.py` helpers
> (`place_mirrored`, `place_rot90_mirrored`, `term()`, `hcap` / `ph` / `_hcap`) → shared parameters,
> every `kicad/*.kicad_sch` identical; TC `make_sheets.sh` / PF `check_sheets.sh` → `p2k sheets
> --check`. Do NOT commit anything in a board repo (pins are bumped later in one coordinated step).
> A cosmetic change that would alter committed `reference/*.md` goes to batch R (D-R). Commit and
> push after each batch in proteus2kicad, update sections 5-6, and replace this prompt with the one
> for the following batch.
