#!/usr/bin/env bash
# KiCad 10 AppImage environment.
#
#   source bin/kicad_env.sh          # mount if needed, put kipy/kicad-cli/p2k on PATH
#   bin/kicad_env.sh CMD ARGS...     # run one command inside the environment
#
# The AppImage is mounted with --appimage-mount (it stays mounted while the
# background process lives). Never run the AppImage with --version: it starts
# the GUI and hangs.

KICAD_APPIMAGE="${KICAD_APPIMAGE:-$HOME/kicad/kicad-10.0.7-x86_64.AppImage}"
_kicad_state="${TMPDIR:-/tmp}/p2k_kicad_mount.$(id -u)"

_kicad_mount_ok() { [ -n "$1" ] && [ -x "$1/bin/kicad-cli" ] && [ -x "$1/bin/python3.11" ]; }

_kicad_ensure_mount() {
    local m=""
    [ -f "$_kicad_state" ] && m="$(cat "$_kicad_state")"
    if ! _kicad_mount_ok "$m"; then
        [ -x "$KICAD_APPIMAGE" ] || { echo "kicad_env: AppImage not found: $KICAD_APPIMAGE" >&2; return 1; }
        local out
        out="$(mktemp)"
        setsid nohup "$KICAD_APPIMAGE" --appimage-mount >"$out" 2>/dev/null </dev/null &
        local i
        for i in $(seq 1 100); do
            m="$(head -n1 "$out" 2>/dev/null)"
            _kicad_mount_ok "$m" && break
            sleep 0.1
        done
        rm -f "$out"
        _kicad_mount_ok "$m" || { echo "kicad_env: mounting $KICAD_APPIMAGE failed" >&2; return 1; }
        echo "$m" >"$_kicad_state"
    fi
    export KICAD_MOUNT="$m"
    export KICAD_PYTHONPATH="$m/shared/lib/python3.11/dist-packages"
}

_kicad_ensure_mount || { return 1 2>/dev/null || exit 1; }
_kicad_tools_bin="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case ":$PATH:" in *":$_kicad_tools_bin:"*) ;; *) export PATH="$_kicad_tools_bin:$PATH" ;; esac

# Executed rather than sourced: run the given command in the environment.
if [ "${BASH_SOURCE[0]}" = "$0" ] && [ $# -gt 0 ]; then
    exec "$@"
fi
