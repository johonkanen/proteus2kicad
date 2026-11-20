#!/usr/bin/env bash
# Regression run for a proteus2kicad commit over the board repos (CANDIDATES.md G2, playbook
# section 4). Developer script: it never changes a board repo.
#
#   tools/regress.sh [-j N] [-k] [-o DIR] [COMMIT] [BOARD ...]
#
#   COMMIT  proteus2kicad commit to test (default: HEAD of this repo; unpushed commits work)
#   BOARD   board repo names under $REGRESS_BOARDS_DIR (default ~/dev); default: all seven
#   -j N    boards run in parallel (default 1)
#   -k      keep the scratch clones (printed at the end)
#   -o DIR  write the per-board logs to DIR (default: a new temporary directory, printed, kept)
#
# Per board, in a scratch directory:
#   1. clone the board repo from its local path at its current HEAD (uncommitted edits in the
#      board repo are not seen), init only the tools/p2k submodule, from this repo;
#   2. check out COMMIT in tools/p2k and commit the bump in the scratch clone only (so the
#      board's own scripts that re-clone it, e.g. ac_psu's regen_check.sh, see the bump);
#   3. regenerate every generated file with the board's own scripts (see regen() below);
#   4. run tools/check_all.sh (must exit 0);
#   5. require a clean git status (every generated file byte-identical with the board's commit).
# ac_psu_kicad: steps 3-5 are its own tools/check_all.sh and tools/regen_check.sh.
# PNG renders (reports/render, the pin-order --render overlays) are visual aids and are not
# regenerated; the round-trip overlay PNGs are rewritten by check_all.sh and so are compared.
set -u
P2K_REPO="$(cd "$(dirname "$0")/.." && pwd)"
BOARDS_DIR="${REGRESS_BOARDS_DIR:-$HOME/dev}"
ALL_BOARDS="tube_psu_kicad tube_power_amp_kicad tube_preamp_kicad tube_pi_fxloop_kicad tube_hv_supplies_kicad ti60f225_tube_control_card ac_psu_kicad"
jobs=1; keep=0; logdir=""
while getopts "j:ko:" opt; do
    case $opt in
        j) jobs=$OPTARG ;;
        k) keep=1 ;;
        o) logdir=$OPTARG ;;
        *) sed -n '2,12p' "$0"; exit 2 ;;
    esac
done
shift $((OPTIND - 1))
commit_arg="HEAD"
if [ $# -gt 0 ] && git -C "$P2K_REPO" rev-parse -q --verify "$1^{commit}" >/dev/null; then
    commit_arg=$1; shift
fi
commit="$(git -C "$P2K_REPO" rev-parse "$commit_arg^{commit}")" || { echo "regress: unknown commit $commit_arg"; exit 2; }
boards="${*:-$ALL_BOARDS}"
scratch="$(mktemp -d "${TMPDIR:-/tmp}/p2k_regress.XXXXXX")"
[ $keep -eq 1 ] || trap 'rm -rf "$scratch"' EXIT
[ -n "$logdir" ] || logdir="$(mktemp -d "${TMPDIR:-/tmp}/p2k_regress_logs.XXXXXX")"
mkdir -p "$logdir"; logdir="$(cd "$logdir" && pwd)"
[ -z "$(git -C "$P2K_REPO" status --porcelain -- p2k bin)" ] || echo "regress: note: uncommitted changes in p2k/ or bin/ are NOT tested (commit them first)"

# the steps that regenerate a tube board's generated files (order = data flow)
regen() {
    local K=tools/p2k/bin/kipy P=tools/p2k/bin/p2k
    if [ -f tools/ipc356.py ]; then run python3 tools/ipc356.py; else run "$P" ipc356; fi   # board wrapper if any
    [ -f tools/make_part_map.py ] && run python3 tools/make_part_map.py
    run python3 tools/gen_symbols.py
    for s in tools/sheets/sheet[0-9]*.py; do run python3 "$s"; done
    run python3 tools/gen_footprints.py
    run "$K" -m p2k.check_footprints --pcbnew
    [ -f tools/excellon_metric.py ] && run python3 tools/excellon_metric.py
    run tools/make_pcb.sh
    return 0
}

run() { echo "-- $*"; "$@" || { echo "!! regen step FAILED: $*"; rfail=1; }; }

one_board() {   # $1 = board name; prints one summary line; full log in $logdir/$1.log
    local b=$1 src="$BOARDS_DIR/$1" dst="$scratch/$1" log="$logdir/$1.log" head pin rfail=0 ok=1 t0=$SECONDS
    {
        head="$(git -C "$src" rev-parse HEAD)" || { echo "regress: $src is not a git repo"; return 1; }
        git clone -q "$src" "$dst" && cd "$dst" && git checkout -q "$head" \
            && git submodule -q init tools/p2k && git config submodule.tools/p2k.url "$P2K_REPO" \
            && git -c protocol.file.allow=always submodule -q update tools/p2k \
            || { echo "!! clone failed"; return 1; }
        pin="$(git rev-parse HEAD:tools/p2k)"
        echo "board $b HEAD $head, committed pin $pin, testing $commit"
        git -C tools/p2k checkout -q "$commit" || { echo "!! checkout $commit failed"; return 1; }
        if [ "$pin" != "$commit" ]; then
            git add tools/p2k && git -c user.name=regress -c user.email=regress@localhost \
                commit -q -m "regress: tools/p2k $pin -> $commit (scratch only)" || return 1
        fi
        unset P2K_BOARD P2K_PCB P2K_SCHEMATIC P2K_REPORTS   # the board's scripts find the clone by cwd
        if [ "$b" = ac_psu_kicad ]; then
            echo "== tools/check_all.sh"; tools/check_all.sh || ok=0
            echo "== tools/regen_check.sh"; tools/regen_check.sh || ok=0
        else
            echo "== regenerate"; regen; [ $rfail -eq 0 ] || ok=0
            echo "== tools/check_all.sh"; tools/check_all.sh || ok=0
        fi
        echo "== git status"
        local st sub
        st="$(git status --porcelain --untracked-files=all)"
        sub="$(git -C tools/p2k status --porcelain)"
        if [ -n "$st$sub" ]; then echo "!! working tree differs from the board commit:"; echo "$st"; echo "$sub"; ok=0
        else echo "clean"; fi
        echo "RESULT $b $([ $ok -eq 1 ] && echo PASS || echo FAIL) head=${head:0:7} pin=${pin:0:7} p2k=${commit:0:7} $((SECONDS - t0))s"
    } >"$log" 2>&1
    tail -n1 "$log" | grep '^RESULT' || echo "RESULT $b FAIL (see $log)"
}

echo "regress: proteus2kicad $commit, boards: $boards, logs: $logdir"
for b in $boards; do
    while [ "$(jobs -rp | wc -l)" -ge "$jobs" ]; do wait -n; done
    one_board "$b" > "$logdir/$b.result" &
done
wait
fail=0
for b in $boards; do
    cat "$logdir/$b.result"
    grep -q " PASS " "$logdir/$b.result" || fail=1
done
[ $keep -eq 1 ] && echo "regress: scratch clones kept in $scratch"
[ $fail -eq 0 ] && echo "regress: ALL BOARDS PASS" || echo "regress: SOME BOARDS FAILED (logs in $logdir)"
exit $fail
