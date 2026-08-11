#!/usr/bin/env python3
"""Strip the ``.lm-Widget { overflow: scroll !important; }`` rule from installed
JupyterLab extensions.

WHY THIS EXISTS
---------------
``maap-dps-jupyter-extension`` and ``maap_algorithms_jupyter_extension`` both ship
this stylesheet verbatim::

    /*
        See the JupyterLab Developer Guide for useful CSS Patterns:
        https://jupyterlab.readthedocs.io/en/stable/developer/css.html
    */

    .lm-Widget {
      overflow: scroll !important;
    }

That is leftover **placeholder CSS from the JupyterLab extension cookiecutter** --
it is unrelated to anything either extension does, and it was copy-pasted into both
projects and never deleted. ``.lm-Widget`` is Lumino's base class, present on *every
widget in the application*, so the rule forces an always-visible scrollbar onto the
menu bar, every toolbar, every individual toolbar button, the tab bar, the breadcrumb
and the dir listing. ``overflow: scroll`` (unlike ``auto``) paints a scrollbar even
with nothing to scroll, and ``!important`` beats JupyterLab's own per-widget
``overflow``. On few-px-tall chrome there is no room for a track or thumb, so all that
renders is the pair of stepper buttons -- the "chevron icons everywhere" bug,
2i2c-org/infrastructure#8770. Chrome-only, since Firefox and macOS overlay scrollbars
have no stepper arrows.

These extensions come from the MAAP base image, so they cannot be removed via
``image/environment.yml``. Disabling them works but costs the Register Algorithm /
Submit Jobs / My Builds / View My Jobs Launcher tiles. Deleting one line of dead
placeholder CSS keeps the GUI and fixes the UI.

WHAT IT DOES
------------
Removes only the ``overflow: scroll !important`` declaration, leaving the (now empty)
``.lm-Widget`` rule in place, so the patch is minimal and the surrounding bundle is
untouched. The CSS lives inside a JS string in a webpack chunk, so the escaped ``\\n``
form is handled alongside real newlines. Idempotent: re-running finds nothing to do.

Not scoped to the two MAAP extensions on purpose -- the same cookiecutter leftover can
appear in any extension, so this catches a future one too.

USAGE
-----
    python strip_lm_widget_overflow.py                 # patch the active env
    python strip_lm_widget_overflow.py --check         # report only, exit 1 if found
    python strip_lm_widget_overflow.py --root DIR      # explicit labextensions dir
    python strip_lm_widget_overflow.py --require       # exit 1 if nothing was found

``--require`` is for CI: it turns "upstream fixed it, this patch is now dead" into a
visible failure rather than a silent no-op. The image build deliberately does NOT use
it, so a base-image bump that fixes this upstream won't break the build.

See ``.clinerules.md`` rule 39 and ``docs/HUB_EXTENSIONS.md``.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# `.lm-Widget {` then whitespace -- which in a webpack JS string is the literal
# two-character sequence \n -- then the offending declaration. Captures the
# selector + brace so the replacement can keep them.
RULE_RE = re.compile(
    r"(\.lm-Widget\s*\{(?:\\n|\s)*)overflow\s*:\s*scroll\s*!important\s*;?"
)

# Anything still matching this after patching means we missed a variant.
LEFTOVER_RE = re.compile(r"overflow\s*:\s*scroll\s*!important")

SCAN_EXTS = (".js", ".css")


def default_root() -> str:
    return os.path.join(sys.prefix, "share", "jupyter", "labextensions")


def iter_files(root: str):
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(SCAN_EXTS) and not fn.endswith(".map"):
                yield os.path.join(dirpath, fn)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--root", default=None, help="labextensions dir (default: sys.prefix)")
    ap.add_argument("--check", action="store_true", help="report only, do not write")
    ap.add_argument("--require", action="store_true", help="exit 1 if no occurrence found")
    args = ap.parse_args()

    root = args.root or default_root()
    if not os.path.isdir(root):
        print(f"strip_lm_widget_overflow: no labextensions dir at {root} - nothing to do")
        return 1 if args.require else 0

    patched: list[tuple[str, int]] = []
    leftovers: list[str] = []

    for path in iter_files(root):
        try:
            with open(path, "r", encoding="utf8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue

        if ".lm-Widget" not in text or "!important" not in text:
            continue

        new_text, n = RULE_RE.subn(r"\1", text)
        if not n:
            continue

        rel = os.path.relpath(path, root)
        patched.append((rel, n))

        if LEFTOVER_RE.search(new_text):
            leftovers.append(rel)

        if args.check:
            continue

        try:
            with open(path, "w", encoding="utf8") as fh:
                fh.write(new_text)
        except OSError as exc:
            print(f"strip_lm_widget_overflow: FAILED to write {path}: {exc}", file=sys.stderr)
            return 2

    verb = "would patch" if args.check else "patched"
    if patched:
        print(f"strip_lm_widget_overflow: {verb} {len(patched)} file(s) under {root}")
        for rel, n in patched:
            print(f"  - {rel}  ({n} occurrence(s))")
    else:
        print(f"strip_lm_widget_overflow: nothing to do under {root}")
        print("  (already patched, or upstream removed the cookiecutter leftover)")

    if leftovers:
        # A variant the capture-group regex didn't cover. Loud, because the UI bug
        # would still be live.
        print(
            "strip_lm_widget_overflow: WARNING - `overflow: scroll !important` still "
            "present after patching in:",
            file=sys.stderr,
        )
        for rel in leftovers:
            print(f"  - {rel}", file=sys.stderr)
        return 2

    if args.check and patched:
        return 1
    if args.require and not patched:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
