"""The board repository the tools work on, and its configuration.

The board root is $P2K_BOARD, or else the nearest directory at or above the current
directory that contains `p2k_config.py`. That file is plain Python (dict / tuple literals)
holding everything that is specific to one board: source file names, refdes and net
fix-ups, expected counts, layer names, outline. See README.md for the keys.

    from p2k import board
    cfg = board.config()            # the p2k_config module
    board.path("reference")         # absolute path inside the board repo
    board.strict("nc_joined")       # is this opt-in stricter check on (STRICT_CHECKS)?

The config is executed from its source on every first load (no bytecode is read or
written), so an edit is never hidden by a stale __pycache__ entry with the same mtime and size.
"""
import os
import types

CONFIG_NAME = "p2k_config.py"
_ROOT = None
_CFG = None


def root():
    global _ROOT
    if _ROOT is None:
        env = os.environ.get("P2K_BOARD")
        if env:
            d = os.path.abspath(env)
            if not os.path.isfile(os.path.join(d, CONFIG_NAME)):
                raise SystemExit(f"p2k: P2K_BOARD={env} has no {CONFIG_NAME}")
        else:
            d = os.getcwd()
            while not os.path.isfile(os.path.join(d, CONFIG_NAME)):
                parent = os.path.dirname(d)
                if parent == d:
                    raise SystemExit(f"p2k: no {CONFIG_NAME} at or above {os.getcwd()} "
                                     "(run from the board repo or set P2K_BOARD)")
                d = parent
        _ROOT = d
    return _ROOT


def config():
    global _CFG
    if _CFG is None:
        path = os.path.join(root(), CONFIG_NAME)
        with open(path, "rb") as f:
            code = compile(f.read(), path, "exec", dont_inherit=True)
        mod = types.ModuleType("p2k_config")
        mod.__file__ = path
        exec(code, mod.__dict__)
        _CFG = mod
    return _CFG


# Ids of the implemented opt-in stricter checks (CANDIDATES.md section 2). Each batch that adds
# a check adds its id here and to README.md; a board names the ids it wants in STRICT_CHECKS.
STRICT_IDS = frozenset({
    "no_connect_pin_type",   # check_symbols (S7)
    "nc_joined",             # compare_netlists (C2); ipc356_diff later (R4)
    "net_names",             # compare_netlists (C4)
    "derive_max_move",       # footprints (F3)
    "pad_extent",            # check_footprints (CF2)
    "hole_refdes",           # build_pcb (B1)
})


def strict_checks():
    """The set of enabled strict check ids: config STRICT_CHECKS (a set / list / tuple of ids,
    or "all" = every id in STRICT_IDS; default none). An unknown id is an error, so a typo or an
    id of a check that this p2k commit does not have yet never passes silently."""
    val = getattr(config(), "STRICT_CHECKS", set())
    if isinstance(val, str):
        val = {val}
    val = set(val)
    if "all" in val:
        val = (val - {"all"}) | STRICT_IDS
    unknown = val - STRICT_IDS
    if unknown:
        raise SystemExit(f"p2k: STRICT_CHECKS: unknown check id(s) {sorted(unknown)} "
                         f"(known: {sorted(STRICT_IDS) or 'none'})")
    return val


def strict(check_id):
    """True if the stricter check `check_id` is enabled for this board (STRICT_CHECKS)."""
    if check_id not in STRICT_IDS:
        raise ValueError(f"p2k.board.strict: {check_id!r} is not in STRICT_IDS")
    return check_id in strict_checks()


def path(*parts):
    return os.path.join(root(), *parts)


BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
