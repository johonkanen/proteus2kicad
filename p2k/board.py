"""The board repository the tools work on, and its configuration.

The board root is $P2K_BOARD, or else the nearest directory at or above the current
directory that contains `p2k_config.py`. That file is plain Python (dict / tuple literals)
holding everything that is specific to one board: source file names, refdes and net
fix-ups, expected counts, layer names, outline. See README.md for the keys.

    from p2k import board
    cfg = board.config()            # the p2k_config module
    board.path("reference")         # absolute path inside the board repo
"""
import importlib.util
import os

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
        spec = importlib.util.spec_from_file_location("p2k_config", os.path.join(root(), CONFIG_NAME))
        _CFG = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_CFG)
    return _CFG


def path(*parts):
    return os.path.join(root(), *parts)


BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
