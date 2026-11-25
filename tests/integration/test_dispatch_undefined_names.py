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

The STAC ("ODR") CLIs are the other shape
-----------------------------------------
``process_sentinel2_odr.py`` and ``process_landsat_odr.py`` keep their whole
dispatch inside ``main()``, so the module-scope check above sees nothing of it.
They get the mirror-image check: every name a function reads as a GLOBAL must
be bound at module scope. That is the bug PR #190 shipped -- three names were
dropped from ``process_sentinel2_odr``'s import block while ``main()`` still
called them, a ``NameError`` before a single scene was read, for the notebook
and the DPS job alike. ``--help`` exits inside ``parse_args()``, long before
any of those calls, so ``cli-smoke`` was green.
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

# CLIs whose dispatch lives inside functions rather than at module scope.
FUNCTION_DISPATCH_SCRIPTS = [
    _SRC / "landsat" / "process_landsat_odr.py",
    _SRC / "sentinel2" / "process_sentinel2_odr.py",
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


def undefined_globals_in_functions(source, path="<source>"):
    """Return names that a function reads as globals but the module never binds.

    The counterpart of ``undefined_names`` for code that lives inside ``def``:
    a name a function references without assigning it is looked up in the
    module at CALL time, so a missing import only fails when that line runs.
    """
    table = symtable.symtable(source, str(path), "exec")
    bound = {
        sym.get_name() for sym in table.get_symbols()
        if sym.is_assigned() or sym.is_imported()
    }
    allowed = set(dir(builtins)) | _DUNDERS | _star_import_names(source) | bound
    flagged = set()

    def walk(scope):
        for child in scope.get_children():
            for sym in child.get_symbols():
                if sym.is_referenced() and sym.is_global() and sym.get_name() not in allowed:
                    flagged.add(sym.get_name())
            walk(child)

    walk(table)
    return sorted(flagged)


@pytest.mark.parametrize("script", FUNCTION_DISPATCH_SCRIPTS, ids=lambda p: p.name)
def test_function_dispatch_has_no_undefined_globals(script):
    """Every global a STAC CLI's functions use must be imported or defined."""
    missing = undefined_globals_in_functions(script.read_text(), script)
    assert missing == [], (
        f"{script.name}: names used inside a function but never bound at module "
        f"scope: {missing}. Import them -- this is a NameError at run time."
    )


def test_function_guard_flags_a_missing_import():
    """Self-proving negative, on the exact shape PR #190 shipped."""
    clean = "from m import search\n\ndef main():\n    return search(1)\n"
    broken = "def main():\n    return search(1)\n"
    assert undefined_globals_in_functions(clean) == []
    assert undefined_globals_in_functions(broken) == ["search"]
    # A local, a parameter and a builtin are not globals and must not be flagged.
    assert undefined_globals_in_functions(
        "def main(a):\n    b = len(a)\n    return [c for c in range(b)]\n"
    ) == []


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
