#!/usr/bin/env python3
"""Scope and relax the MAAP extensions' ``.lm-Widget { overflow: scroll !important }``.

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
overflows -- MAAP's long forms scroll again -- while chrome that does not overflow
stays quiet. It is what the declaration should have said in the first place.

WHY IT IS ALSO SCOPED
---------------------
``auto`` alone was not enough, and the reason is specific: Lumino's *layout* widgets
(``DockPanel``, ``TabBar``, ``SplitPanel``, ``BoxPanel``, ``StackedPanel``, the menus)
position their children **absolutely**, at computed pixel coordinates, and are sized to
exactly the space available. Sub-pixel rounding routinely leaves a child a fraction of a
pixel past the edge -- which ``overflow: hidden`` (Lab's own value) clips silently and
``auto`` turns into a real scrollbar. On the main dock panel that produced a feedback
loop: a horizontal scrollbar appeared, consumed ~15px of a ~30px-tall ``.lm-TabBar``,
which then overflowed vertically too, and the tab bar's top half was clipped out of view.

So the selector is narrowed to exclude those layout classes (``LUMINO_LAYOUT`` below).
Excluded widgets simply fall back to JupyterLab's own ``overflow`` -- the rule is
removed from the cascade for them, not overridden with a guessed value. Everything else,
including the MAAP panels' own content widgets, still gets ``auto`` and still scrolls.
Confirmed live on a hub pod 2026-08-11 by rewriting ``selectorText`` in the browser: the
clipped tab bar came back to full height and Submit Jobs still scrolled.

KNOWN RESIDUAL RISK
-------------------
Content widgets are still matched globally, so a Lab widget whose content *genuinely*
overflows can still show a scrollbar where it previously clipped. If arrows reappear
somewhere, either add that widget's class to ``LUMINO_LAYOUT`` or scope the rule
positively to the MAAP panels -- the DPS panel's React root is ``.submit-jobs-container``;
the algorithms extension defines no root class, so scoping that one needs its DOM
inspected. See ``.clinerules.md`` rule 39.

WHAT IT DOES
------------
Narrows the ``.lm-Widget`` selector and rewrites the ``overflow`` keyword to ``auto``,
leaving everything else in the bundle untouched. The CSS lives inside a JS string in a
webpack chunk, so the escaped ``\\n`` form is handled alongside real newlines. Idempotent:
the pattern requires a bare ``.lm-Widget {``, which a patched rule no longer has. The
keyword alternation accepts ``auto`` as well as ``scroll`` so a bundle left over from the
earlier keyword-only version of this script still gets scoped on a re-run.

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

# Lumino layout widgets: they position children absolutely and are sized to exactly the
# space available, so sub-pixel rounding leaves a fraction-of-a-pixel overflow that
# `auto` turns into a real scrollbar (see WHY IT IS ALSO SCOPED). Excluding them drops
# the rule from the cascade for those elements, so Lab's own `overflow` applies again.
LUMINO_LAYOUT = (
    ".lm-DockPanel",
    ".lm-TabBar",
    ".lm-TabPanel",
    ".lm-SplitPanel",
    ".lm-BoxPanel",
    ".lm-StackedPanel",
    ".lm-MenuBar",
    ".lm-Menu",
    ".lm-ScrollBar",
)
SCOPE = "".join(f":not({cls})" for cls in LUMINO_LAYOUT)

# `.lm-Widget {` then whitespace -- which inside a webpack JS string is the literal
# two-character sequence \n -- then `overflow:` and the offending keyword. Captures the
# selector and the tail separately so the replacement can narrow one and rewrite the
# other. `auto` is accepted alongside `scroll` so a bundle patched by the earlier
# keyword-only version of this script still gets scoped.
RULE_RE = re.compile(
    r"(\.lm-Widget)(\s*\{(?:\\n|\s)*overflow\s*:\s*)(?:scroll|auto)(\s*!important)"
)

# A bare (unscoped) `.lm-Widget` overflow rule surviving the pass means we missed a
# variant and the UI bug is still live.
LEFTOVER_RE = re.compile(
    r"\.lm-Widget\s*\{(?:\\n|\s)*overflow\s*:\s*(?:scroll|auto)\s*!important"
)


def patch_rule(match: re.Match) -> str:
    """`.lm-Widget { overflow: scroll !important` -> scoped selector + `auto`."""
    return f"{match.group(1)}{SCOPE}{match.group(2)}auto{match.group(3)}"

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

        new_text, n = RULE_RE.subn(patch_rule, text)
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
            print(f"  - {rel}  ({n} occurrence(s))  scoped selector + overflow: auto")
    else:
        print(f"fix_lm_widget_overflow: nothing to do under {root}")
        print("  (already patched, or upstream removed the cookiecutter leftover)")

    if leftovers:
        print(
            "fix_lm_widget_overflow: WARNING - an unscoped `.lm-Widget { overflow: ... "
            "!important }` variant survived patching in:",
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
