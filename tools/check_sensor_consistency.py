"""
Lint: pyproject.toml is in sync with the sensor directories on disk,
and each sensor has a matching pair of workflow notebooks that aren't
contaminated by copy-paste leftovers from another sensor.

A "sensor directory" is any top-level dir containing both `cli.py` and at
least one `process_*.py`. For each such dir, we assert:

  1. `<sensor>*` appears in [tool.setuptools.packages.find].include
  2. `process_<sensor>` is registered in [project.scripts]
  3. The script entry resolves to `<sensor>.cli:process_<sensor>_cli`
  4. notebooks/<sensor>_workflow.ipynb exists
  5. notebooks/testing-notebooks/<sensor>_workflow.ipynb exists
  6. cell 0 of each notebook mentions THIS sensor name (case-insensitive)
  7. cell 0 of each notebook does NOT mention any OTHER sensor's name
     (catches the Capella "Sentinel-2"/"Umbra" copy-paste leftover bug)

Catches the silent-failure modes from the capella rollout:
  - Bug #1: console script registered, package missing from include
            (capella shipped as a shim that crashed on import inside the hub).
  - Bug #2: package present but no console script entry
            (process_capella was missing from [project.scripts] at merge time).
  - Bug #3: notebook frontmatter copy-pasted from another sensor and not
            fully relabeled (capella notebook claimed to be about Sentinel-2).

Run locally:  python tools/check_sensor_consistency.py
CI:           wired into .github/workflows/lint.yml
Exit codes:   0 = pass, 1 = at least one inconsistency.
"""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
NOTEBOOKS_DIR = REPO_ROOT / "notebooks"
TESTING_NOTEBOOKS_DIR = REPO_ROOT / "notebooks" / "testing-notebooks"

# Some sensors use a hyphenated canonical name in titles even though their
# package dir is alphanumeric. Map sensor-dir name -> list of substrings
# (lowercase) that count as "mentioning this sensor". New sensors default
# to [sensor_name].
SENSOR_ALIASES: dict[str, list[str]] = {
    "sentinel2": ["sentinel-2", "sentinel2"],
}


def aliases_for(sensor_name: str) -> list[str]:
    """Lowercase substrings that count as "this notebook is about <sensor>"."""
    return SENSOR_ALIASES.get(sensor_name, [sensor_name])


def read_cell0_source(nb_path: Path) -> str:
    """Return the source of cell 0 of an .ipynb, or "" if unreadable.

    Uses the stdlib json parser — no nbformat dep required, keeps this
    script importable in the bare CI conda env."""
    try:
        data = json.loads(nb_path.read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    cells = data.get("cells", [])
    if not cells:
        return ""
    src = cells[0].get("source", "")
    if isinstance(src, list):
        src = "".join(src)
    return src


def check_notebook_conformance(
    sensor_name: str,
    all_sensor_names: list[str],
) -> list[str]:
    """Return a list of failure strings for this sensor's two notebooks."""
    failures: list[str] = []

    main_nb = NOTEBOOKS_DIR / f"{sensor_name}_workflow.ipynb"
    test_nb = TESTING_NOTEBOOKS_DIR / f"{sensor_name}_workflow.ipynb"

    self_aliases = [a.lower() for a in aliases_for(sensor_name)]
    other_aliases: list[tuple[str, str]] = [
        (other, a.lower())
        for other in all_sensor_names
        if other != sensor_name
        for a in aliases_for(other)
    ]

    for nb_path, role in [(main_nb, "notebook"), (test_nb, "testing notebook")]:
        if not nb_path.exists():
            failures.append(
                f"{sensor_name}/ has no matching {role} at "
                f"{nb_path.relative_to(REPO_ROOT)}. "
                f"Every sensor must ship a {role} pair — copy from "
                f"capella's or use tools/new_sensor.py to scaffold."
            )
            continue

        cell0 = read_cell0_source(nb_path).lower()
        if not cell0:
            failures.append(
                f"{nb_path.relative_to(REPO_ROOT)} cell 0 is empty / unreadable."
            )
            continue

        if not any(alias in cell0 for alias in self_aliases):
            failures.append(
                f"{nb_path.relative_to(REPO_ROOT)} cell 0 does not mention "
                f"the sensor name '{sensor_name}'. Add a title / heading."
            )

        leaked = sorted({
            f"'{alias}' (from {other}/)"
            for other, alias in other_aliases
            if alias in cell0
        })
        if leaked:
            failures.append(
                f"{nb_path.relative_to(REPO_ROOT)} cell 0 mentions another "
                f"sensor: {', '.join(leaked)}. This is the classic "
                f"copy-paste leftover bug — relabel the frontmatter to "
                f"refer to '{sensor_name}' throughout."
            )

    return failures


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

    # ------------------------------------------------------------------
    # Notebook conformance — every sensor ships a notebook pair with
    # cell-0 frontmatter that mentions this sensor and only this sensor.
    # ------------------------------------------------------------------
    sensor_names = [s.name for s in sensors]
    for sensor in sensors:
        failures.extend(check_notebook_conformance(sensor.name, sensor_names))

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
