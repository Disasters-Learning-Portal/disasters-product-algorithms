"""Static "undefined name" guard for the sensor dispatch scripts.

Why this exists
---------------
``lint.yml``'s ``cli-smoke`` job only runs ``<cli> --help``, which argparse
short-circuits *before* any processing. And ``process_landsat89.py`` /
``process_sentinel2.py`` run their whole dispatch under
``if __name__ == "__main__":`` (sentinel2 at bare module level) with no
importable ``main()`` -- so a ``NameError`` in that dispatch (e.g. ``cloudMask``
referenced by the index products after its assignment is deleted) is invisible
to CI and can't be reached by an import-based smoke test.

This test statically proves that every name *referenced* at module scope in
those two scripts is actually *bound* there -- assigned, imported, a parameter,
a builtin, or provided by a ``from X import *``. It catches the ``cloudMask``
class without executing the pipeline and without touching production code.

Scope
-----
The check is over **module scope**, where the dispatch names live (an
``if __name__`` block does not create a new scope). Names used only inside
nested functions/classes live in child scopes and are intentionally not
deep-checked -- that is what keeps false positives at zero. This is a static
reachability guard, not a substitute for executing the pipeline.
"""

import builtins
import importlib
import pathlib
import re
import symtable

import pytest

# Repo root -> src/<sensor>/<script>. This file is tests/integration/<here>.
_SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
DISPATCH_SCRIPTS = [
    _SRC / "landsat" / "process_landsat89.py",
    _SRC / "sentinel2" / "process_sentinel2.py",
]

# Module globals that are always available but never "assigned" in source.
_DUNDERS = {
    "__name__", "__file__", "__doc__", "__builtins__", "__spec__",
    "__loader__", "__package__", "__annotations__", "__cached__", "__dict__",
}


def _star_import_names(source):
    """Names brought in by every ``from X import *`` in *source*.

    Resolved by importing X and reading ``__all__`` (or its public ``dir()``),
    which transitively captures re-exported star-imports. The star-imported
    modules import fine under tests/conftest.py's osgeo stub.
    """
    names = set()
    for mod_name in re.findall(r"^from\s+([\w.]+)\s+import\s+\*", source, re.MULTILINE):
        mod = importlib.import_module(mod_name)
        exported = getattr(mod, "__all__", None) or [n for n in dir(mod) if not n.startswith("_")]
        names.update(exported)
    return names


def undefined_names(source, path="<source>"):
    """Return names referenced-but-never-bound at the module scope of *source*."""
    table = symtable.symtable(source, str(path), "exec")
    allowed = set(dir(builtins)) | _DUNDERS | _star_import_names(source)
    flagged = set()
    for sym in table.get_symbols():
        bound = sym.is_assigned() or sym.is_imported() or sym.is_parameter()
        if sym.is_referenced() and not bound and sym.get_name() not in allowed:
            flagged.add(sym.get_name())
    return sorted(flagged)


@pytest.mark.parametrize("script", DISPATCH_SCRIPTS, ids=lambda p: p.name)
def test_dispatch_has_no_undefined_names(script):
    """Every name used in the dispatch must be bound -- guards the cloudMask class."""
    source = script.read_text()
    missing = undefined_names(source, script)
    assert missing == [], f"{script.name}: names referenced but never bound at module scope: {missing}"


def test_guard_flags_an_undefined_name():
    """Self-proving negative: the guard must not silently become a no-op."""
    assert undefined_names("x = 1\nprint(x)\n") == []          # clean source -> nothing
    assert undefined_names("x = 1\nprint(foo, x)\n") == ["foo"]  # undefined name -> flagged
