"""The board repository the tools work on, and its configuration.

The board root is $P2K_BOARD, or else the nearest directory at or above the current
directory that contains `p2k_config.py`. That file is plain Python (dict / tuple literals)
holding everything that is specific to one board: source file names, refdes and net
fix-ups, expected counts, layer names, outline. See README.md for the keys.

    from p2k import board
    cfg = board.config()            # the p2k_config module
    board.path("reference")         # absolute path inside the board repo
    board.strict("nc_joined")       # is this stricter check on (STRICT_CHECKS, STRICT_CHECKS_OFF)?

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


# Ids of the implemented stricter checks (CANDIDATES.md section 2). Each batch that adds a check adds
# its id here and to README.md.
STRICT_IDS = frozenset({
    "no_connect_pin_type",   # check_symbols (S7)
    "nc_joined",             # compare_netlists (C2), ipc356_diff (R4)
    "net_names",             # compare_netlists (C4)
    "derive_max_move",       # footprints (F3)
    "pad_extent",            # check_footprints (CF2)
    "hole_refdes",           # build_pcb (B1)
    "track_nets",            # check_board (B10)
    "roundtrip_pad_match",   # gerber_roundtrip (R5)
    "roundtrip_counts",      # gerber_roundtrip (R7)
})

# The ids that are on when a board does not set STRICT_CHECKS (decision D-Z, batch Z): every id the
# finished tube boards pass after their migration. pad_extent and roundtrip_pad_match fail on every
# board until its pad sizes are decided (KiCad library pads), so they stay opt-in: a board names
# them in STRICT_CHECKS (or "all"). A new id joins the default only in a batch that says so.
STRICT_DEFAULT = STRICT_IDS - {"pad_extent", "roundtrip_pad_match"}


def strict_checks():
    """The set of enabled strict check ids.

    STRICT_CHECKS absent: STRICT_DEFAULT. STRICT_CHECKS present: exactly those ids (a set / list /
    tuple, or "all" = every id in STRICT_IDS; set() = none, the pre-D-Z behaviour).
    STRICT_CHECKS_OFF = {id: reason}: the recorded exceptions of this board, removed from the set
    above. Every id must be known, every reason a non-empty text, and every listed id must be on
    without the entry (otherwise the entry is stale: remove it). An unknown id is an error in both
    keys, so a typo or an id of a check that this p2k commit does not have never passes silently."""
    cfg = config()
    val = getattr(cfg, "STRICT_CHECKS", None)
    if val is None:
        val = set(STRICT_DEFAULT)
    else:
        if isinstance(val, str):
            val = {val}
        val = set(val)
        if "all" in val:
            val = (val - {"all"}) | STRICT_IDS
    unknown = val - STRICT_IDS
    if unknown:
        raise SystemExit(f"p2k: STRICT_CHECKS: unknown check id(s) {sorted(unknown)} "
                         f"(known: {sorted(STRICT_IDS) or 'none'})")
    off = getattr(cfg, "STRICT_CHECKS_OFF", None)
    if off is None:
        return val
    if not isinstance(off, dict):
        raise SystemExit("p2k: STRICT_CHECKS_OFF must be a dict {check id: reason}")
    unknown = set(off) - STRICT_IDS
    if unknown:
        raise SystemExit(f"p2k: STRICT_CHECKS_OFF: unknown check id(s) {sorted(unknown)} "
                         f"(known: {sorted(STRICT_IDS)})")
    bad = sorted(k for k, r in off.items() if not isinstance(r, str) or not r.strip())
    if bad:
        raise SystemExit(f"p2k: STRICT_CHECKS_OFF: {bad}: give the reason as a text")
    stale = sorted(set(off) - val)
    if stale:
        raise SystemExit(f"p2k: STRICT_CHECKS_OFF: {stale} not on for this board anyway "
                         f"(not in STRICT_CHECKS / the default): remove the entry")
    return val - set(off)


def strict(check_id):
    """True if the stricter check `check_id` is enabled for this board (STRICT_CHECKS,
    STRICT_CHECKS_OFF)."""
    if check_id not in STRICT_IDS:
        raise ValueError(f"p2k.board.strict: {check_id!r} is not in STRICT_IDS")
    return check_id in strict_checks()


# Tools that read PENDING (B9); an entry for any other tool fails instead of being ignored.
PENDING_TOOLS = frozenset({"drc", "assign_nets", "erc", "ipc356_diff", "gerber_roundtrip"})


def pending(tool):
    """PENDING[tool] ({item pattern (regex): question}) of the config, {} without the key. PENDING =
    {tool: {...}}: open user decisions, see judge_pending(). An unknown tool key is an error."""
    val = getattr(config(), "PENDING", {}) or {}
    unknown = set(val) - PENDING_TOOLS
    if unknown:
        raise SystemExit(f"p2k: PENDING: unknown or unsupported tool(s) {sorted(unknown)} "
                         f"(supported: {sorted(PENDING_TOOLS)})")
    return val.get(tool, {}) or {}


def judge_pending(tool, failures):
    """Split a tool's failure texts by PENDING[tool]: -> (unmatched failures, questions, stale).
    A failure matched by a pattern (re.search) is pending; every pattern must match at least one
    failure, else it is stale (the decision was made: remove the entry). The tool passes only
    when nothing is unmatched and nothing is stale; it prints `PENDING USER DECISION: <question>`
    per entry (check_all.sh counts those)."""
    import re
    entries = pending(tool)
    rest, hit = [], {k: 0 for k in entries}
    for f in failures:
        keys = [k for k in entries if re.search(k, f)]
        if keys:
            for k in keys:
                hit[k] += 1
        else:
            rest.append(f)
    questions = [f"{q} ({hit[k]} item{'s' if hit[k] != 1 else ''})" for k, q in entries.items() if hit[k]]
    stale = [f"PENDING[{tool!r}] entry {k!r} matches no failure (decision made? remove it): {q}"
             for k, q in entries.items() if not hit[k]]
    return rest, questions, stale


def path(*parts):
    return os.path.join(root(), *parts)


BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
