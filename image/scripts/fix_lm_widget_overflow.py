#!/usr/bin/env python3
"""Relax the MAAP extensions' ``.lm-Widget { overflow: scroll !important }`` to ``auto``.

WHY THIS EXISTS
---------------
``maap-dps-jupyter-extension`` and ``maap_algorithms_jupyter_extension`` both ship
this stylesheet verbatim -- leftover placeholder CSS from the JupyterLab extension
cookiecutter, byte-identical in both projects::

    /*
        See the JupyterLab Developer Guide for useful CSS Patterns:
        https://jupyterlab.readthedocs.io/en/stable/developer/css.html
    */

    .lm-Widget {
      overflow: scroll !important;
    }

``.lm-Widget`` is Lumino's base class, present on **every widget in the application**.
``overflow: scroll`` -- unlike ``auto`` -- paints a scrollbar even when there is nothing
to scroll, and ``!important`` beats JupyterLab's own per-widget ``overflow``. On Lab's
few-px-tall chrome there is no room for a track or thumb, so all that renders is the
pair of stepper buttons: the "chevron icons everywhere" bug,
2i2c-org/infrastructure#8770. Chrome-only, since Firefox and macOS overlay scrollbars
have no stepper arrows.

WHY `auto` AND NOT DELETION
---------------------------
The first version of this script deleted the declaration outright. That killed the
arrows but **broke the MAAP panels**: Lumino's base CSS is ``overflow: hidden``, and
the Submit Jobs form is taller than its panel, so it became impossible to scroll down
to the Submit button. The extensions were (badly) relying on this rule to make their
own content scrollable -- they just applied it to every widget in the app instead of
scoping it to their own.

Changing ``scroll`` to ``auto`` keeps a scrollbar exactly where content genuinely
overflows -- MAAP's long forms scroll again -- while Lab's chrome, which does not
overflow, stays quiet. It is what the rule should have said in the first place.

KNOWN RESIDUAL RISK
-------------------
This is still a global rule. Any Lab widget whose content *genuinely* overflows will
now show a scrollbar where it previously clipped (JupyterLab uses ``overflow: hidden``
deliberately in places). If arrows reappear anywhere, the real fix is to scope the rule
to the MAAP panels instead of relaxing it globally -- the DPS panel's React root is
``.submit-jobs-container``; the algorithms extension defines no root class, so scoping
it needs its DOM inspected. See ``.clinerules.md`` rule 39.

WHAT IT DOES
------------
Rewrites only the ``scroll`` keyword inside a ``.lm-Widget`` rule's ``overflow``
declaration, leaving everything else in the bundle untouched. The CSS lives inside a JS
string in a webpack chunk, so the escaped ``\\n`` form is handled alongside real
newlines. Idempotent: once patched there is no ``scroll`` left to match.

Not scoped to the two MAAP extensions on purpose -- the same cookiecutter leftover can
appear in any extension, so this catches a future one too.

USAGE
-----
    python fix_lm_widget_overflow.py                 # patch the active env
    python fix_lm_widget_overflow.py --check         # report only, exit 1 if found
    python fix_lm_widget_overflow.py --root DIR      # explicit labextensions dir
    python fix_lm_widget_overflow.py --require       # exit 1 if nothing was found

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

# `.lm-Widget {` then whitespace -- which inside a webpack JS string is the literal
# two-character sequence \n -- then `overflow:` and the offending keyword. Captures
# everything either side so the replacement swaps only `scroll` -> `auto`.
RULE_RE = re.compile(
    r"(\.lm-Widget\s*\{(?:\\n|\s)*overflow\s*:\s*)scroll(\s*!important)"
)

# Anything still matching this inside a .lm-Widget rule after patching means we missed
# a variant and the UI bug is still live.
LEFTOVER_RE = re.compile(
    r"\.lm-Widget\s*\{(?:\\n|\s)*overflow\s*:\s*scroll\s*!important"
)

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
        print(f"fix_lm_widget_overflow: no labextensions dir at {root} - nothing to do")
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

        new_text, n = RULE_RE.subn(r"\1auto\2", text)
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
            print(f"fix_lm_widget_overflow: FAILED to write {path}: {exc}", file=sys.stderr)
            return 2

    verb = "would patch" if args.check else "patched"
    if patched:
        print(f"fix_lm_widget_overflow: {verb} {len(patched)} file(s) under {root}")
        for rel, n in patched:
            print(f"  - {rel}  ({n} occurrence(s))  overflow: scroll -> auto")
    else:
        print(f"fix_lm_widget_overflow: nothing to do under {root}")
        print("  (already patched, or upstream removed the cookiecutter leftover)")

    if leftovers:
        print(
            "fix_lm_widget_overflow: WARNING - a `.lm-Widget { overflow: scroll !important }` "
            "variant survived patching in:",
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
