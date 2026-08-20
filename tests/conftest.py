"""Make this project's `src/` win, even when sibling projects are on the path.

Nine independent projects each own a flat `src/` directory, and several of them
legitimately contain a module called `generate.py`, `models.py`, `features.py` or
`synth.py`. Running one project's tests is fine. Running them all in one pytest
process is not: the first `src` inserted into `sys.path` wins, and every later
project silently imports its neighbour's module -- which fails in confusing ways
(`AttributeError` on a function that exists in the file you are looking at).

This conftest puts THIS project's `src` at the front of `sys.path` and evicts any
already-imported module that came from a different project's `src`, so each suite
gets its own modules regardless of collection order. The alternative is renaming
every module to a project-unique prefix, which would make each project less
readable on its own -- and each project is meant to be read on its own.
"""
import pathlib
import sys

SRC = (pathlib.Path(__file__).resolve().parents[1] / "src")
SRC_STR = str(SRC)


def _evict_foreign_modules() -> None:
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        f = f.replace("\\", "/")
        if "/manufacturing-hm/" in f and "/src/" in f and not f.startswith(
                SRC_STR.replace("\\", "/")):
            del sys.modules[name]


_evict_foreign_modules()
if SRC_STR in sys.path:
    sys.path.remove(SRC_STR)
sys.path.insert(0, SRC_STR)


def pytest_collectstart(collector):
    _evict_foreign_modules()
    if sys.path[0] != SRC_STR:
        if SRC_STR in sys.path:
            sys.path.remove(SRC_STR)
        sys.path.insert(0, SRC_STR)
