#!/usr/bin/env python3
"""Audit installed JupyterLab extensions for globally-scoped CSS.

A federated JupyterLab extension's stylesheet is injected into ``document.head``
unscoped. If it carries a CSS framework's global reset (Bootstrap Reboot,
Tailwind preflight, ``normalize.css``) its bare ``body{}`` / ``*{}`` rules
compete with JupyterLab's own at *equal specificity* -- and last-injected wins.
Overriding Lab's 13px ``--jp-ui-font-size1`` overflows every fixed-height piece
of Lab chrome; those containers are ``overflow: auto``, so Chrome paints
scrollbars with no room for a track or thumb and only the stepper arrows render.
That is the "chevron icons everywhere" bug (2i2c-org/infrastructure#8770).

This scans EVERY installed labextension -- including ones inherited from the
base image, not just the ones pinned in ``image/environment.yml`` -- and reports
which of them ship global rules.

Usage (from a terminal on the hub pod)::

    python tools/audit_labextension_css.py
    python tools/audit_labextension_css.py --json > /tmp/css_audit.json
    python tools/audit_labextension_css.py 2>&1 | tee /tmp/css_audit.log

Exit status is 1 if any extension ships a global reset, so it works as a check.

See ``.clinerules.md`` rule 39 and ``docs/HUB_DEPLOYMENT.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

# Each signature is (label, compiled regex, why it matters). Order matters only
# for readability of the report.
SIGNATURES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "universal-box-sizing",
        re.compile(r"\*\s*,\s*(?:\*)?::(?:after|before)\s*,?\s*(?:\*)?::(?:after|before)?\s*\{[^}]*box-sizing"),
        "resets box-sizing on every element in the app, not just the extension",
    ),
    (
        "bare-body-rule",
        re.compile(r"(?<![-\w.#>\]])body\s*\{[^}]*(?:font-size|font-family|line-height|margin)\s*:"),
        "sets typography/margin on <body> -- competes with Lab's --jp-ui-font-size1",
    ),
    (
        "bare-html-rule",
        re.compile(r"(?<![-\w.#>\]])html\s*\{[^}]*(?:font-size|font-family|line-height)\s*:"),
        "sets the root font size -- rescales every rem-based Lab dimension",
    ),
    (
        "scrollbar-standard-props",
        re.compile(r"(?<![-\w])scrollbar-(?:color|width)\s*:"),
        "Chrome 121+ gives these precedence over ::-webkit-scrollbar, changing scrollbar rendering",
    ),
    (
        "scrollbar-button",
        re.compile(r"::-webkit-scrollbar-button"),
        "explicitly styles the scrollbar stepper arrows",
    ),
    (
        "color-scheme",
        re.compile(r"(?<![-\w])color-scheme\s*:"),
        "switches native control + scrollbar theming for the subtree",
    ),
]

# Framework fingerprints -- these tell you WHICH library leaked, which is what
# you need for the upstream bug report.
FRAMEWORKS: list[tuple[str, str]] = [
    ("bootstrap", "--bs-body-font-family"),
    ("tailwind-preflight", "--tw-ring-offset-shadow"),
    ("normalize.css", "normalize.css"),
    ("mui", "MuiCssBaseline"),
    ("syncfusion-ej2", "e-control e-"),
]

SKIP_SUFFIXES = (".map", ".ts", ".md", ".json", ".woff", ".woff2", ".ttf", ".eot")


def default_roots() -> list[str]:
    """Every place JupyterLab looks for federated extensions."""
    candidates = [
        os.path.join(sys.prefix, "share", "jupyter", "labextensions"),
        os.path.join(os.path.expanduser("~"), ".local", "share", "jupyter", "labextensions"),
        "/usr/local/share/jupyter/labextensions",
        "/usr/share/jupyter/labextensions",
    ]
    seen, out = set(), []
    for c in candidates:
        real = os.path.realpath(c)
        if real not in seen and os.path.isdir(real):
            seen.add(real)
            out.append(c)
    return out


def extension_name(root: str, path: str) -> str:
    """Extension dir relative to the labextensions root (handles @scope/name)."""
    rel = os.path.relpath(path, root)
    parts = rel.split(os.sep)
    if parts and parts[0].startswith("@") and len(parts) > 1:
        return "/".join(parts[:2])
    return parts[0] if parts else rel


def scan_file(path: str) -> tuple[list[tuple[str, int, str]], set[str]]:
    """Return (hits, frameworks) for one file. hits = [(label, offset, excerpt)]."""
    try:
        with open(path, "r", encoding="utf8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return [], set()

    hits: list[tuple[str, int, str]] = []
    for label, pattern, _why in SIGNATURES:
        m = pattern.search(text)
        if m:
            excerpt = " ".join(text[m.start() : m.start() + 110].split())
            hits.append((label, m.start(), excerpt))

    frameworks = {name for name, marker in FRAMEWORKS if marker in text}
    return hits, frameworks


def audit(roots: list[str]) -> dict:
    findings: dict[str, dict] = {}
    scanned = 0

    for root in roots:
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if fn.endswith(SKIP_SUFFIXES) or not (fn.endswith(".js") or fn.endswith(".css")):
                    continue
                full = os.path.join(dirpath, fn)
                scanned += 1
                hits, frameworks = scan_file(full)
                if not hits and not frameworks:
                    continue
                ext = extension_name(root, full)
                entry = findings.setdefault(
                    ext, {"root": root, "frameworks": set(), "hits": {}}
                )
                entry["frameworks"] |= frameworks
                for label, offset, excerpt in hits:
                    # Keep the first occurrence per (extension, signature).
                    entry["hits"].setdefault(
                        label,
                        {"file": os.path.relpath(full, root), "offset": offset, "excerpt": excerpt},
                    )

    return {"roots": roots, "files_scanned": scanned, "extensions": findings}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", action="append", dest="roots", metavar="DIR",
                    help="labextensions dir to scan (repeatable; default: auto-detect)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    args = ap.parse_args()

    roots = args.roots or default_roots()
    if not roots:
        print("No labextensions directory found. Pass --root explicitly.", file=sys.stderr)
        return 2

    result = audit(roots)
    offenders = {
        name: data for name, data in result["extensions"].items() if data["hits"]
    }

    if args.json:
        serializable = {
            "roots": result["roots"],
            "files_scanned": result["files_scanned"],
            "extensions": {
                name: {
                    "root": d["root"],
                    "frameworks": sorted(d["frameworks"]),
                    "hits": d["hits"],
                }
                for name, d in result["extensions"].items()
            },
        }
        print(json.dumps(serializable, indent=2))
        return 1 if offenders else 0

    print("JupyterLab extension CSS audit")
    for r in result["roots"]:
        print(f"  root: {r}")
    print(f"  files scanned: {result['files_scanned']}")
    print(f"  extensions with findings: {len(result['extensions'])}")
    print()

    if not offenders:
        print("PASS - no extension ships a global CSS reset.")
        for name, d in sorted(result["extensions"].items()):
            if d["frameworks"]:
                print(f"  note: {name} bundles {', '.join(sorted(d['frameworks']))} (scoped - no global rules found)")
        return 0

    why = {label: reason for label, _p, reason in SIGNATURES}
    print(f"FAIL - {len(offenders)} extension(s) ship globally-scoped CSS:\n")
    for name, d in sorted(offenders.items()):
        fw = ", ".join(sorted(d["frameworks"])) or "unidentified"
        print(f"  {name}   [bundles: {fw}]")
        for label, info in sorted(d["hits"].items()):
            print(f"    - {label} @ {info['offset']} in {info['file']}")
            print(f"        {info['excerpt']}")
            print(f"        why: {why.get(label, '')}")
        print()

    print("Any extension listed above can restyle the whole Lab UI. See")
    print(".clinerules.md rule 39 and docs/HUB_DEPLOYMENT.md for the failure chain.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
