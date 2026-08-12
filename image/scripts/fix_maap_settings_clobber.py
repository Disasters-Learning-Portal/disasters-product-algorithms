#!/usr/bin/env python3
"""Stop ``maap-jupyter-server-extension`` from overwriting saved MAAP settings with "".

WHY THIS EXISTS
---------------
On **every JupyterLab page load**, the MAAP shared-settings plugin fetches its five
settings from the server and writes all of them back into the user's settings file --
unconditionally, with no guard for empty values (``src/index.ts``, v3.0.0, the version
``maap-workspaces/base_images/2i2c/pangeo`` pins)::

    GET {baseUrl}maap-jupyter-server-extension/get-maap-params
      .then(maapParams => Promise.all([
        serverExtSettings.set('maapApiUrl', maapParams.maapApiUrl),
        serverExtSettings.set('maapToken',  maapParams.maapToken),
        ...
      ]))

``ISettings.set()`` PATCHes ``/lab/api/settings/<plugin-id>``, which is what writes
``~/.jupyter/lab/user-settings/maap-jupyter-server-extension/plugin.jupyterlab-settings``.

The server half (``handlers.py::GetMaapParamsHandler``) reads env vars and returns ``""``
for anything unset::

    token    = os.environ.get('MAAP_PGT', "")
    api_host = os.environ.get('MAAP_API_HOST', "")

MAAP injects those per-pod from the ADE devfile
(``maap-workspaces/devfiles/pangeo/devfile/devfile.yaml``: ``MAAP_API_HOST:
api.dit.maap-project.org``). **The Disasters 2i2c hub is not a MAAP-managed hub**, so
nothing sets them -- the endpoint returns ``""`` for both, and the plugin faithfully
writes ``""`` over whatever the operator typed into Settings -> MAAP Settings. Every
page load. That is the whole of Disasters-Learning-Portal/disasters-product-algorithms#88:
paste PGT, use it, log back in, paste it again.

It reproduces on a plain browser hard-refresh -- no pod restart needed -- and the DevTools
console logs ``Successfully updated MAAP extension settings.`` as it happens. That is the
verification loop for this bug; a rebuild + promote + restart is far too slow to guess
against.

The ``MAAP_API_HOST`` half is fixed separately and more cheaply, by ``ENV MAAP_API_HOST``
in ``image/Dockerfile`` -- then the write lands the *correct* URL instead of ``""`` (and
bare ``MAAP()`` in maap-py picks it up too). The token cannot be baked into an image; it
is per-user, so it has to survive in the settings file, which is what this script buys.

WHAT IT DOES
------------
Rewrites each unconditional ``R.set("<key>", V)`` in the installed bundle into
``(V ? R.set(<marker>"<key>", V) : Promise.resolve())`` -- write only a non-empty value,
leave the saved one alone otherwise. Auto-population still works wherever the env var IS
set; only the destructive empty write is suppressed. ``Promise.resolve()`` keeps it a
valid ``Promise.all([...])`` element.

Both ``R`` and ``V`` appear twice in the output, so the value pattern deliberately
excludes parentheses and commas: it matches property reads and bare identifiers (what the
minifier emits here) and refuses anything that could be a call with side effects. A form
it cannot match is a loud failure, not a silent skip.

SCOPED TO THE CLOBBERING PLUGIN ON PURPOSE
------------------------------------------
Only files containing ``get-maap-params`` are touched. ``dps-jupyter-extension`` and
``algorithms-jupyter-extension`` call ``settings.set('maapApiUrl', ...)`` too, but theirs
is a **user-initiated** write from the token modal -- guarding that one would stop an
operator from deliberately clearing a field.

IDEMPOTENT / LOUD
-----------------
A patched call carries ``<marker>`` right after ``set(``, which the pattern (requiring a
quote there) can no longer match, so re-runs are no-ops.

Unlike ``fix_lm_widget_overflow.py``, this one is **strict by default**: if the plugin
bundle is missing, or is present but nothing matched and no marker is there, it exits
non-zero and fails the image build. A silent miss here is invisible in the UI and sends
operators straight back to re-pasting a token every login, so it should stop the build
and get a human to look. A base-image bump that restructures (or fixes) this upstream is
expected to trip it -- re-read the bundle, then either update the pattern or delete this
script and its Dockerfile layer.

USAGE
-----
    python fix_maap_settings_clobber.py                # patch the active env (strict)
    python fix_maap_settings_clobber.py --check        # report only, exit 1 if unpatched
    python fix_maap_settings_clobber.py --root DIR     # explicit labextensions dir
    python fix_maap_settings_clobber.py --lenient      # downgrade strict failures to 0

Upstream: https://github.com/MAAP-Project/jupyter-server-extension (the guard belongs in
``src/index.ts``; this script retires the day it lands). See ``.clinerules.md`` rule 41.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# The five keys the plugin writes back on every page load (schema/plugin.json). All are
# guarded, not just maapToken: any of them can be unset in a non-MAAP-managed hub, and
# guarding one but not the rest would be an arbitrary line.
SETTING_KEYS = (
    "maapApiUrl",
    "maapToken",
    "defaultAppImage",
    "currentAppImage",
    "workspaceBucket",
)

# Identifies the bundle that performs the clobber. The endpoint path survives
# minification (it is a literal inside a template string), and no other extension
# references it -- see SCOPED TO THE CLOBBERING PLUGIN ON PURPOSE.
FINGERPRINT = "get-maap-params"

# Injected immediately after `set(` so the pattern below cannot match a patched call.
MARKER = "/*disasters-settings-guard*/"

# `<receiver>.set("<key>", <value>)`. The value is restricted to a parenthesis- and
# comma-free expression (`t.maapToken`, or a bare identifier if the minifier
# destructured) because the replacement evaluates it twice.
SET_RE = re.compile(
    r"([A-Za-z_$][\w$]*)\.set\(\s*(['\"])(" + "|".join(SETTING_KEYS) + r")\2\s*,\s*([^,()]+?)\s*\)"
)


def guard_call(match: re.Match) -> str:
    """`R.set("maapToken", V)` -> `(V?R.set(<marker>"maapToken",V):Promise.resolve())`."""
    receiver, quote, key, value = match.groups()
    return f"({value}?{receiver}.set({MARKER}{quote}{key}{quote},{value}):Promise.resolve())"


def default_root() -> str:
    return os.path.join(sys.prefix, "share", "jupyter", "labextensions")


def iter_files(root: str):
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(".js") and not fn.endswith(".map"):
                yield os.path.join(dirpath, fn)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--root", default=None, help="labextensions dir (default: sys.prefix)")
    ap.add_argument("--check", action="store_true", help="report only, do not write")
    ap.add_argument(
        "--lenient",
        action="store_true",
        help="exit 0 when the plugin is missing or unmatched (default: fail loudly)",
    )
    args = ap.parse_args()

    def fail(msg: str) -> int:
        stream = sys.stdout if args.lenient else sys.stderr
        label = "note" if args.lenient else "FAILED"
        print(f"fix_maap_settings_clobber: {label} - {msg}", file=stream)
        return 0 if args.lenient else 1

    root = args.root or default_root()
    if not os.path.isdir(root):
        return fail(f"no labextensions dir at {root}")

    patched: list[tuple[str, int]] = []
    already: list[str] = []
    leftovers: list[str] = []
    seen_plugin = False

    for path in iter_files(root):
        try:
            with open(path, "r", encoding="utf8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue

        if FINGERPRINT not in text:
            continue

        seen_plugin = True
        rel = os.path.relpath(path, root)

        new_text, n = SET_RE.subn(guard_call, text)
        if not n:
            if MARKER in text:
                already.append(rel)
            continue

        patched.append((rel, n))

        # The marker makes a guarded call unmatchable, so anything still matching is a
        # variant the replacement did not cover.
        if SET_RE.search(new_text):
            leftovers.append(rel)

        if args.check:
            continue

        try:
            with open(path, "w", encoding="utf8") as fh:
                fh.write(new_text)
        except OSError as exc:
            print(
                f"fix_maap_settings_clobber: FAILED to write {path}: {exc}", file=sys.stderr
            )
            return 2

    if leftovers:
        print(
            "fix_maap_settings_clobber: FAILED - an unguarded settings write survived "
            "patching in:",
            file=sys.stderr,
        )
        for rel in leftovers:
            print(f"  - {rel}", file=sys.stderr)
        return 2

    if not seen_plugin:
        return fail(
            f"no bundle referencing '{FINGERPRINT}' under {root} - "
            "maap-jupyter-server-extension is gone or renamed; re-check the base image"
        )

    if patched:
        verb = "would guard" if args.check else "guarded"
        print(f"fix_maap_settings_clobber: {verb} {len(patched)} file(s) under {root}")
        for rel, n in patched:
            print(f"  - {rel}  ({n} settings write(s) guarded)")
        return 1 if args.check else 0

    if already:
        print("fix_maap_settings_clobber: already guarded")
        for rel in already:
            print(f"  - {rel}")
        return 0

    return fail(
        "found the maap-jupyter-server-extension bundle but no settings write matched "
        f"(keys: {', '.join(SETTING_KEYS)}) - upstream restructured it; re-read the "
        "bundle and update SET_RE, or drop this script if the guard landed upstream"
    )


if __name__ == "__main__":
    sys.exit(main())
