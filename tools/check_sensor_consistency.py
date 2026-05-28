"""
Lint: pyproject.toml is in sync with the sensor directories on disk.

A "sensor directory" is any top-level dir containing both `cli.py` and at
least one `process_*.py`. For each such dir, we assert:

  1. `<sensor>*` appears in [tool.setuptools.packages.find].include
  2. `process_<sensor>` is registered in [project.scripts]
  3. The script entry resolves to `<sensor>.cli:process_<sensor>_cli`

Catches the two silent-failure modes that broke the capella rollout:
  - Bug #1: console script registered, package missing from include
            (capella shipped as a shim that crashed on import inside the hub).
  - Bug #2: package present but no console script entry
            (process_capella was missing from [project.scripts] at merge time).

Run locally:  python tools/check_sensor_consistency.py
CI:           wired into .github/workflows/lint.yml
Exit codes:   0 = pass, 1 = at least one inconsistency.
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"


def find_sensor_dirs(root: Path) -> list[Path]:
    """Top-level dirs that look like a sensor pipeline (cli.py + process_*.py)."""
    sensors = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name.startswith("_"):
            continue
        if not (child / "cli.py").exists():
            continue
        if not any(child.glob("process_*.py")):
            continue
        sensors.append(child)
    return sensors


def main() -> int:
    pyproject = tomllib.loads(PYPROJECT.read_text())

    include = (
        pyproject.get("tool", {})
        .get("setuptools", {})
        .get("packages", {})
        .get("find", {})
        .get("include", [])
    )
    scripts = pyproject.get("project", {}).get("scripts", {})

    sensors = find_sensor_dirs(REPO_ROOT)
    if not sensors:
        print("ERROR: no sensor directories found — check repo layout.", file=sys.stderr)
        return 1

    failures: list[str] = []

    # Verb prefixes we treat as "this file should be wired to a console script".
    # Matches the existing landsat / sentinel2 / sensor pipelines + the download
    # variant in sentinel2.
    VERB_PREFIXES = ("process_", "download_")

    for sensor in sensors:
        name = sensor.name
        expected_glob = f"{name}*"

        # 1. Package discovery includes the sensor
        if expected_glob not in include:
            failures.append(
                f"{name}/ exists (has cli.py + process_*.py) but `{expected_glob}` "
                f"is not in [tool.setuptools.packages.find].include. "
                f"pip install would skip the package — the console script will "
                f"crash with ModuleNotFoundError. "
                f"Add `\"{expected_glob}\"` to pyproject.toml."
            )

        # 2. Every verb-prefixed entrypoint file under <sensor>/ should have a
        #    matching console script. Eg landsat/process_landsat89.py expects
        #    [project.scripts] process_landsat89 = "landsat.cli:process_landsat89_cli".
        for entry_file in sorted(sensor.glob("*.py")):
            stem = entry_file.stem  # "process_landsat89"
            if not any(stem.startswith(v) for v in VERB_PREFIXES):
                continue
            expected_target = f"{name}.cli:{stem}_cli"

            if stem not in scripts:
                failures.append(
                    f"{name}/{entry_file.name} exists but `{stem}` is not in "
                    f"[project.scripts]. Add: "
                    f"`{stem} = \"{expected_target}\"`"
                )
                continue

            target = scripts[stem]
            if target != expected_target:
                failures.append(
                    f"[project.scripts].{stem} is `{target}`, expected "
                    f"`{expected_target}` (matches the <pkg>.cli:<verb>_cli shape)."
                )

    if failures:
        print("Sensor consistency check FAILED:\n", file=sys.stderr)
        for i, msg in enumerate(failures, 1):
            print(f"  {i}. {msg}\n", file=sys.stderr)
        print(
            f"Found {len(sensors)} sensor dir(s): {', '.join(s.name for s in sensors)}",
            file=sys.stderr,
        )
        return 1

    print(f"OK: {len(sensors)} sensor(s) consistent with pyproject.toml:")
    for s in sensors:
        print(f"  - {s.name}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
