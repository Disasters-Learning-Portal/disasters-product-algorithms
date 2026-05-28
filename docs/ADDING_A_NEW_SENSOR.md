# Adding a new sensor pipeline

This is the operator-facing guide for shipping a new sensor product
end-to-end — from a fresh branch to a working `process_<sensor>` CLI on a
JupyterHub pod.

For lower-level `shared_utils` function additions (not a full sensor
pipeline), see [ADDING_FUNCTIONS_TUTORIAL.md](ADDING_FUNCTIONS_TUTORIAL.md).
For the automation that backs this guide, see [AUTOMATION.md](AUTOMATION.md).

---

## tl;dr

The scaffolder writes all four sensor files, both notebooks, and both
`pyproject.toml` entries in one command:

```bash
# 1. Clone & branch
git checkout -b feat/<sensor>

# 2. Scaffold the sensor (writes ~6 files + 2 pyproject mutations).
python tools/new_sensor.py <sensor>

# 3. Implement the calibration math in <sensor>/<sensor>_v2.py.
#    The other three files (__init__.py, cli.py, process_<sensor>.py) work
#    out of the box; you'll rarely need to edit them.

# 4. Push; CI runs lint.yml automatically; merge to dev when green.
git add -A && git commit -m "feat(<sensor>): add sensor pipeline"
git push -u origin feat/<sensor>
gh pr create --base dev
```

The scaffolder runs `tools/check_sensor_consistency.py` as a post-condition
check and rolls back all file changes if it fails — so the only state that
can leak out is a sensor whose template renders cleanly. If you have the
pre-commit hook installed (`pip install pre-commit && pre-commit install`,
see [CI / validation](#ci--validation)), the same lint fires at `git commit`
time as a second safety net.

If you need to customise the default S3 bucket or the notebook description
up-front:

```bash
python tools/new_sensor.py spire \
    --bucket csda-spire-delivery \
    --description "Workflow for Spire RO / GNSS-R imagery."
```

For when the scaffolder can't be used (no Python env, exotic sensor name,
unusual file layout), the manual flow is still documented at the bottom
under [Manual fallback](#manual-fallback).

---

## What you need to write

The four files under `<sensor>/`:

### `<sensor>/__init__.py`

Re-export the user-facing symbols from your `_v2` module:

```python
from <sensor>.<sensor>_v2 import *

__all__ = [
    'retrieve_<sensor>_resources',
    'sigmaCalib',  # or whatever your sensor's calibration functions are named
    'apply_filter',
]
```

The `__all__` list controls what `from <sensor> import *` exposes. Keep
internal helpers off this list. The lint at
`tools/check_sensor_consistency.py` doesn't validate `__all__` contents
today, but the planned notebook conformance lint will.

### `<sensor>/cli.py`

Verbatim copy from `capella/cli.py` with one string change. The pattern
uses `exec(compile(...))` so that running `python <sensor>/process_<sensor>.py`
directly and invoking the console script `process_<sensor>` produce the
same result. (See [AUTOMATION.md](AUTOMATION.md#audit-flagged-simplifications-not-yet-actioned)
for an open question about whether this trick is necessary.)

```python
"""
CLI entry points for <Sensor> processing.
"""

import os


def process_<sensor>_cli():
    """Entry point for process_<sensor> command."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(script_dir, "process_<sensor>.py")

    with open(script_path) as f:
        code = compile(f.read(), script_path, "exec")
        exec(code, {"__name__": "__main__"})


if __name__ == "__main__":
    process_<sensor>_cli()
```

### `<sensor>/process_<sensor>.py`

The argparse entry point. **Required surface area** (matches every
existing sensor):

- `--product` — `choices=[...]` of the products your sensor generates
- `--date` — required, free-form (each sensor parses its own date format)
- `--output` — default `/tmp/s3_temp`
- `--bucket`, `--prefix` — S3 source location
- `-nodata`, `-compression`, `-compression_level` — COG knobs
- **`-dst_crs` defaulting to `"native"`** — this is the canonical pattern.
  Copy the snippet below verbatim.

```python
parser.add_argument(
    "-dst_crs",
    type=str,
    default="native",
    help=(
        "Target CRS for COG output. 'native' (default) preserves the "
        "source UTM projection; pass 'EPSG:3857' for Web Mercator "
        "(required by veda-data-airflow build_stac)."
    ),
)

args = parser.parse_args()
dst_crs_value = None if args.dst_crs.lower() == "native" else args.dst_crs
```

After producing the un-COGed output file:

```python
cog_path = convert_to_cog(
    outfile,
    nodata=args.nodata,
    dst_crs=dst_crs_value,
    compression=args.compression,
    compression_level=args.compression_level,
)
```

The `"native"` ↔ `None` mapping is consistent across `process_capella`,
`process_umbra`, `process_satellogic`. `process_landsat89` and
`process_sentinel2` keep `EPSG:4326` as their default for back-compat —
new sensors should default to `"native"` per the convention.

### `<sensor>/<sensor>_v2.py`

The actual sensor logic. Pattern:

```python
"""
<sensor>_v2.py

Utilities for retrieving and processing <Sensor> products.
"""

import os
from datetime import datetime
from typing import Union
import numpy as np
from osgeo import gdal

from shared_utils.geotools import *
from shared_utils.s3utils import *


def retrieve_<sensor>_resources(
    date: Union[str, datetime],
    bucket: str = "<canonical-s3-bucket>",
    prefix: str = "disasters",
) -> list[str]:
    """S3 listing + date filter; return list of s3:// URIs for the matching scene."""
    ...


def sigmaCalib(s3_image_paths: list[str], save_location: str = "/tmp/s3_temp") -> str:
    """Apply your sensor's calibration; return path to the local .tif."""
    ...


def apply_filter(infile: str, size: int = 5) -> str:
    """Optional speckle / smoothing filter (SAR sensors). Return path to the filtered .tif."""
    ...
```

Use `shared_utils.cog_utils.convert_to_cog` for final COG conversion (the
CLI driver does this — `_v2.py` should produce a regular GeoTIFF, not a COG).

---

## pyproject.toml wiring

**Two edits, both required.** The CI lint
(`tools/check_sensor_consistency.py`) will fail if either is missing.

### `[project.scripts]` — alphabetical

```toml
process_<sensor> = "<sensor>.cli:process_<sensor>_cli"
```

### `[tool.setuptools.packages.find].include`

Add `"<sensor>*"` to the include list (location does not matter, but
following the existing order helps reviewers):

```toml
include = ["landsat*", "sentinel2*", "satellogic*", "umbra*", "capella*", "<sensor>*", "shared_utils*", "raster_tools*"]
```

If you miss this entry, the lint produces:

```
<sensor>/ exists (has cli.py + process_*.py) but `<sensor>*` is not in
[tool.setuptools.packages.find].include. pip install would skip the
package — the console script will crash with ModuleNotFoundError.
Add `"<sensor>*"` to pyproject.toml.
```

This is exactly the bug that broke the initial Capella rollout. The lint
catches it pre-merge.

---

## Notebook conventions

Both notebooks (CLI-subprocess style under `notebooks/`, import-based style
under `notebooks/testing-notebooks/`) must follow the project's notebook
conventions. Easiest path: copy `notebooks/capella_workflow.ipynb` and
its testing variant, then swap `capella` → `<sensor>` throughout.

### Quarto frontmatter

The frontmatter cell (first cell of `notebooks/<sensor>_workflow.ipynb`)
follows this shape:

```yaml
---
title: <Sensor> Processing Workflow
description: This notebook demonstrates the complete workflow for retrieving and processing <Sensor> imagery using the `disasters-product-algorithms` package.
author:
  - Your Name (Editor, Affiliation)
date: <MMM DD, YYYY>
execute:
   freeze: true
---
```

A future notebook-conformance lint (planned, see [AUTOMATION.md §Roadmap](AUTOMATION.md#roadmap))
will assert the sensor name in the title matches the directory name AND
no other sensor names appear in the frontmatter. Today this is a manual
check — review your notebook for leftover references to whatever sensor
you copied from.

### Hub launch link + disclaimer block

Keep the existing HTML structure from `capella_workflow.ipynb`:

```html
<h3><a href="https://hub.disasters.2i2c.cloud/...">🚀 Launch in Disasters-Hub JupyterHub</a></h3>
<div class="alert alert-block">Disclaimer: ... VEDA JupyterHub ...</div>
<a href="https://binder.openveda.cloud/v2/...">[Binder badge]</a>
```

(These are identical across all sensor workflow notebooks and are
candidates for a shared `notebooks/_includes/hub_header.md` partial in a
future iteration.)

### `TARGET_CRS` block — mandatory

In the env-setup code cell, near the top:

```python
# Set CRS for COG output
TARGET_CRS = None
# TARGET_CRS = "EPSG:3857"
```

`None` (default) preserves the source projection of input rasters — fast
and faithful to the sensor's native UTM zone. Uncomment the second line
for Web Mercator output, which is required by `veda-data-airflow`'s
`build_stac` task (which trips on the WGS 84 ensemble + lat-first axis
bug when input COGs are in `EPSG:4326`).

### Forwarding `TARGET_CRS` to the CLI

In the subprocess invocation cell:

```python
process_cmd = [
    "process_<sensor>",
    "--product", PRODUCT,
    "--date", DATE,
    "--output", OUTPUT_DIR,
    ...
    "-dst_crs", TARGET_CRS if TARGET_CRS else "native",
]
```

`"native"` is the sentinel string every sensor CLI maps back to `None`
internally. Don't pass an empty string or the literal `"None"` — those
are not recognized.

---

## Conda dependencies

If your sensor introduces new Python libraries, see
[AUTOMATION.md §Dependency source-of-truth files](AUTOMATION.md#dependency-source-of-truth-files)
for the full decision tree. Short version:

- **Pip-installable wheel** → `pyproject.toml [project.dependencies]`.
- **Conda-only, already in Pangeo base** (the case for most geospatial
  libraries: gdal, rasterio, rio-cogeo, geopandas, pyproj, numpy, scipy,
  boto3, etc.) → `dev-conda-deps.txt` only.
- **Conda-only, NOT in Pangeo base** → BOTH `dev-conda-deps.txt` AND
  `hub-conda-deps.txt`.

The `sync-conda-deps.yml` workflow auto-opens a PR in
`pangeo-notebook-veda-image` to sync `hub-conda-deps.txt` into its
`environment.yml`. Review and merge that PR; next image build picks up
the new dep.

---

## CI / validation

On every push and PR to `dev` / `main`, `.github/workflows/lint.yml`
runs two jobs:

### `sensor-consistency`

Runs `python tools/check_sensor_consistency.py`. Catches:

1. Sensor dir exists without a matching `"<sensor>*"` glob in
   `[tool.setuptools.packages.find].include`.
2. Sensor dir has `process_<verb>.py` files without matching
   `[project.scripts]` entries.
3. Script target shape doesn't match `<sensor>.cli:<verb>_cli`.

### `cli-smoke`

Boots a fresh conda env from `dev-conda-deps.txt`, runs `pip install .`,
then runs two sub-loops:

1. **Import-test:** `python -c "import <sensor>"` for every sensor dir
   on disk. Forces the full module-load path — catches wrong-symbol
   star-imports in `<sensor>/__init__.py` that `--help` would short-circuit
   past (argparse exits before the broken symbol is ever resolved).
2. **`--help` loop:** iterates `[project.scripts]` and runs `<script> --help`
   on each. Catches argparse-time issues + the "shim without package"
   class (script registered, package missing from `packages.find.include`).

Both sub-loops together catch:

- Missing transitive deps (broken `import` at module load).
- Wrong-symbol imports in `__init__.py` (e.g. star-imports a function
  that doesn't exist in `<sensor>_v2`).
- Console script registered without the package being installable.
- Argparse misconfigurations.

All checks must pass before merge.

### Running locally

```bash
python tools/check_sensor_consistency.py
# OK: 6 sensor(s) consistent with pyproject.toml:
#   - capella/, landsat/, satellogic/, sentinel2/, <sensor>/, umbra/
```

**Pre-commit hook (recommended):** install once per clone —

```bash
pip install pre-commit && pre-commit install
```

— and the consistency lint then fires automatically at `git commit` time,
not just in CI. Same script, sub-second runtime, identical pass/fail
output. If the hook flags a problem, `git commit` aborts and you fix
locally before pushing.

---

## After merge

1. **CI on `dev` triggers `trigger-docker-rebuild.yml`**, which selects the
   dev token + `algorithm-updated-dev` event type based on `github.ref`
   and dispatches to `pangeo-notebook-veda-image`. Watch the action
   complete (<1 min). The same workflow handles `main` pushes (with the
   prod token + `algorithm-updated` event) — there is no separate `-dev`
   workflow anymore.
2. **Image rebuild** in `pangeo-notebook-veda-image` takes ~3-5 min: ~30s
   for the algorithms pip-install layer (the common case if you only
   added Python code) or ~2-3 min if `hub-conda-deps.txt` changed and
   the env update layer invalidated.
3. **Spawn a fresh pod** from `klesinger/disasters-jupyterhub-docker-image-dev:latest`
   (or `:<sha-12>` for a specific commit pin).
4. **Smoke test:**

   ```bash
   which process_<sensor>                # should print path inside conda env
   process_<sensor> --help               # should print argparse help
   python -c "import <sensor>"           # should succeed silently
   ```

5. **End-to-end test:** open `notebooks/<sensor>_workflow.ipynb` and run
   the cells against real data.

6. **Promote to prod:** open a PR from `dev` → `main`. The PR template
   (`.github/PULL_REQUEST_TEMPLATE.md`) reminds you of the
   `feature/* → dev → main` flow; native branch protection on `main`
   (see `.github/RULESETS.md`) is the authoritative gate. On merge,
   `trigger-docker-rebuild.yml` rebuilds the prod image.

If `process_<sensor>` is missing on a fresh pod after a green CI + image
rebuild, see the debug checklist in
[HUB_DEPLOYMENT.md](HUB_DEPLOYMENT.md). The most common cause is
"wrong branch on wrong image variant" — e.g. testing against the prod
image after only merging to `dev`.

---

## Real-world example

The Capella SAR pipeline shipped in May 2026 as the most recent worked
example. Files to study:

- `capella/__init__.py` — `__all__` shape, `_v2` re-exports.
- `capella/cli.py` — verbatim copy-pattern.
- `capella/process_capella.py` — argparse + `-dst_crs native` pattern + `convert_to_cog` call.
- `capella/capella_v2.py` — S3 retrieval, sigma0 calibration, Lee filter.
- `notebooks/capella_workflow.ipynb` — Quarto frontmatter, TARGET_CRS block, subprocess invocation.
- `notebooks/testing-notebooks/capella_workflow.ipynb` — import-based dev variant.
- `pyproject.toml` lines 60 (`[project.scripts]`) and 67 (`packages.find.include`).
- Post-mortem of the rollout bugs: [AUTOMATION.md §Post-mortems](AUTOMATION.md#post-mortems).

---

## Manual fallback

For when `tools/new_sensor.py` can't be used (you're scripting outside a
Python env, you need an unusual sensor name the validator rejects, or you
want to copy a sensor that diverged from the template). This is the old
flow, preserved for completeness.

```bash
# 1. Clone & branch
git checkout -b feat/<sensor>

# 2. Copy the freshest sensor as your template (capella as of 2026-05)
cp -r capella/ <sensor>/

# 3. Rename / implement: edit the four files (see "What you need to write" above)

# 4. Wire pyproject.toml (two lines, both required) — see "pyproject.toml wiring"

# 5. Validate locally
python tools/check_sensor_consistency.py

# 6. Create the notebook pair
cp notebooks/capella_workflow.ipynb notebooks/<sensor>_workflow.ipynb
cp notebooks/testing-notebooks/capella_workflow.ipynb \
   notebooks/testing-notebooks/<sensor>_workflow.ipynb
# Edit each to swap "capella" → "<sensor>" throughout, INCLUDING the
# Quarto frontmatter description. The cell-0 conformance check in
# tools/check_sensor_consistency.py fails if any other sensor's name
# leaks into cell 0.

# 7. Push; CI runs lint.yml automatically; merge to dev when green.
git add -A && git commit -m "feat(<sensor>): add sensor pipeline"
git push -u origin feat/<sensor>
gh pr create --base dev
```

The manual flow is more error-prone — the original Capella rollout shipped
with three different copy-paste bugs that the scaffolder would have made
structurally impossible. Prefer `tools/new_sensor.py` unless you have a
specific reason not to.
