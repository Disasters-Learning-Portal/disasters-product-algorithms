# Disasters Product Algorithms

Unified package for processing Landsat 8/9 and Sentinel-2 satellite imagery to generate disaster response products.

## Features

### Landsat 8/9 Products
- True color imagery
- Panchromatic (Band 8)
- Natural color
- Color infrared
- NDVI (Normalized Difference Vegetation Index)
- NDWI (Normalized Difference Water Index)
- MNDWI (Modified NDWI)
- EVI (Enhanced Vegetation Index)
- NBR (Normalized Burn Ratio)
- Water extent classification
- Cloud masking
- COG (Cloud Optimized GeoTIFF) conversion

### Sentinel-2 Products
- True color imagery
- Natural color
- SWIR (Shortwave Infrared)
- Color infrared
- NDVI (Normalized Difference Vegetation Index)
- NDWI (Normalized Difference Water Index)
- MNDWI (Modified NDWI)
- NBR (Normalized Burn Ratio)
- Water extent classification
- Cloud masking (L2A only)

## Installation

### Prerequisites

**IMPORTANT:** This package requires GDAL and other geospatial libraries that MUST be installed via conda BEFORE installing this package.

#### Step 1: Install geospatial dependencies via conda

```bash
# Create a new environment:
conda create -n disasters python=3.10
conda activate disasters

# Install required geospatial packages (MUST be installed via conda).
# Single source of truth for the dep list is dev-conda-deps.txt:
mamba install -y -c conda-forge $(grep -v '^\s*#' dev-conda-deps.txt | grep -v '^\s*$' | tr '\n' ' ')
```

**Why conda?** The GDAL Python bindings must match your system's libgdal version. Installing GDAL via pip will fail or cause version conflicts. Conda ensures all geospatial libraries are compatible.

#### Step 2: Install this package

```bash
# Clone the repository
git clone https://github.com/klesinger/disasters-product-algorithms.git
cd disasters-product-algorithms

# Install in editable mode
pip install -e .
```

**For JupyterHub users:** If your environment already has GDAL, rasterio, and other geospatial libraries installed via conda, you can skip Step 1.

## Usage

### Command Line Interface

#### Landsat 8/9

```bash
# Basic usage
process_landsat89 /path/to/landsat/data -p true ndvi ndwi

# Process all products with merging and cloud masking
process_landsat89 /path/to/landsat/data -p all -merge -mask

# Process specific date
process_landsat89 /path/to/landsat/data -p true ndvi -date 20230116

# With event naming and COG options
process_landsat89 /path/to/landsat/data -p all -event 202512_Flood_WA -compression ZSTD
```

**Landsat Options:**
```
process_landsat89 input [-h] [-p [P ...]] [-we_nstd [WE_NSTD ...]]
                        [-date [DATE ...]] [-tile [TILE ...]]
                        [-merge] [-mask] [-force] [-unzip_only]
                        [-tif_only] [-nodata NODATA]
                        [-compression COMPRESSION] [-compression_level LEVEL]
                        [-event EVENT] [-zip [ZIP ...]] [-dir [DIR ...]]
```

**Products:** `all`, `true`, `pan`, `nat`, `colorIR`, `ndvi`, `ndwi`, `mndwi`, `evi`, `nbr`, `we`

#### Sentinel-2

```bash
# Basic usage
process_sentinel2 /path/to/sentinel/data -p true ndvi ndwi

# Process all products with merging and cloud masking
process_sentinel2 /path/to/sentinel/data -p all -merge -mask

# Process specific date
process_sentinel2 /path/to/sentinel/data -p true ndvi -date 20230116

# Water extent with custom thresholds
process_sentinel2 /path/to/sentinel/data -p we -we_nstd 1 1.5 2

# With event naming and COG options (same as Landsat!)
process_sentinel2 /path/to/sentinel/data -p all -event 202512_Flood_WA -compression ZSTD
```

**Sentinel-2 Options:**
```
process_sentinel2 input [-h] [-p [P ...]] [-we_nstd [WE_NSTD ...]]
                        [-date [DATE ...]] [-tile [TILE ...]]
                        [-merge] [-mask] [-force] [-unzip_only]
                        [-tif_only] [-nodata NODATA]
                        [-compression COMPRESSION] [-compression_level LEVEL]
                        [-event EVENT]
```

**Products:** `all`, `true`, `nat`, `swir`, `colorIR`, `ndvi`, `ndwi`, `mndwi`, `nbr`, `we`

### Python API

Import and use functions in your code:

```python
# Landsat functions
from landsat import genTrueColor, genNdvi, gen_water_extent, ls_merge

# Sentinel functions
from sentinel2 import gen_true_color, gen_ndvi, gen_water_extent, s2_merge

# Shared utilities
from shared_utils import convert_to_cog, rename_with_event
```

## Key Features

### Cloud Optimized GeoTIFFs (Both Landsat & Sentinel)
- **Automatic COG conversion by default** for both sensors
- Customizable compression (ZSTD, DEFLATE, LZW)
- Auto-detected no-data values
- Multi-level overviews for efficient visualization
- Use `-tif_only` to skip COG conversion if needed

### Event-Based File Naming (Both Landsat & Sentinel)
Add event prefix to organize outputs by disaster. The date is removed from the middle and reformatted at the end:

```bash
# Landsat
process_landsat89 /data/landsat8 -p all -event 202512_Flood_WA

# Sentinel-2
process_sentinel2 /data/sentinel2 -p all -event 202512_Flood_WA
```

**Filename Transformation:**
- **Landsat:** `LC08_trueColor_20250922_185617_046028.tif`
  → `202512_Flood_WA_LC08_trueColor_185617_046028_2025-09-22_day.tif`

- **Sentinel-2:** `S2B_MSIL2A_colorInfrared_20251111_161419_T17RLN.tif`
  → `202512_Flood_WA_S2B_MSIL2A_colorInfrared_161419_T17RLN_2025-11-11_day.tif`

### Smart Processing
- Automatically skips already processed files
- Use `-force` to reprocess existing outputs
- Supports merging by date and product
- Cloud masking for cleaner imagery

## Input Data

**Landsat 8/9:**
- Collection 2 Level-2 surface reflectance data in `.tar` or `.zip` format
- Download from [USGS EarthExplorer](https://earthexplorer.usgs.gov/) or [NASA Earthdata](https://earthdata.nasa.gov/)

**Sentinel-2:**
- L1C or L2A data in `.zip` format
- Download using `download_sentinel2` command or from [Copernicus Open Access Hub](https://scihub.copernicus.eu/)

### Downloading Sentinel-2 Data

Use the `download_sentinel2` command to download Sentinel-2 imagery from Copernicus:

```bash
# Download by tile and date
download_sentinel2 /output/dir -tile T36SYD -date 20230116

# Download by point coordinates
download_sentinel2 /output/dir -point 35.5 33.9 -date 20230116

# Download by polygon
download_sentinel2 /output/dir -polygon /path/to/polygon.shp -date 20230116

# Download date range
download_sentinel2 /output/dir -tile T36SYD -date 20230116 20230120
```

## Output

Processed products are saved to an `output/` directory within the input directory, organized by:
- Date directories (YYYYMMDD)
- Product type subdirectories
- GeoTIFF files with standardized naming

## Dependencies

Python >= 3.8. The canonical dep lists live in three files, each with a
distinct audience — see [docs/AUTOMATION.md](docs/AUTOMATION.md) for the
full decision tree.

| File | Audience |
|---|---|
| `pyproject.toml [project.dependencies]` | Pip-installable transitive deps (Pillow, lxml, psutil, fsspec, s3fs). Resolved by `pip install .`. |
| `dev-conda-deps.txt` | Conda deps for local development + CI smoke tests (geospatial stack: GDAL, rasterio, rio-cogeo, geopandas, pyproj, numpy, scipy, etc.). |
| `image/environment.yml` | Conda env shipped to the JupyterHub Docker image, layered on top of the Pangeo base. Build context is THIS repo (post-consolidation), so the env file lives here. |

Adding a new dep — pick the right file:
- Has a `manylinux` wheel? → `pyproject.toml [project.dependencies]`.
- Conda-only AND only for local dev / CI? → `dev-conda-deps.txt`.
- Conda-only AND needed in the hub image? → `image/environment.yml` under `dependencies:`.

## Package Structure

```
disasters-product-algorithms/
├── landsat/              # Landsat 8/9 processing
├── sentinel2/            # Sentinel-2 processing
├── satellogic/           # Satellogic optical processing
├── umbra/                # Umbra SAR processing
├── capella/              # Capella SAR processing
├── raster_tools/         # Sensor-agnostic raster utilities
├── shared_utils/         # Shared library (COG conversion, S3, validation, metadata)
├── notebooks/            # Operator-facing Jupyter templates (CLI-subprocess style)
│   └── testing-notebooks/  # Import-based variants for local dev
├── image/                # Docker image build context (Dockerfile + env)
│   ├── Dockerfile        # FROM pangeo/pangeo-notebook:<tag> → conda env → algorithms
│   ├── environment.yml   # Conda env layered on the Pangeo base
│   └── scripts/          # Image-test harness (inherited from upstream)
├── tools/                # Repo-management scripts (consistency lint, etc.)
└── docs/                 # Deployment guides, automation reference, tutorials
```

## Development workflow

### Pre-push checks

Before pushing changes that touch a sensor directory (`<sensor>/`),
`pyproject.toml [project.scripts]`, or
`[tool.setuptools.packages.find].include`, run the consistency lint locally:

```bash
python tools/check_sensor_consistency.py
# OK: 5 sensor(s) consistent with pyproject.toml:
#   - capella/, landsat/, satellogic/, sentinel2/, umbra/
```

The same script runs in CI via `.github/workflows/lint.yml`. PRs that break
the pyproject ↔ sensor-dir consistency invariant fail the `sensor-consistency`
job before merging.

A second CI job, `cli-smoke`, installs the algorithms package in a clean conda
env (deps from `dev-conda-deps.txt`), then runs two checks: a bare
`python -c "import <sensor>"` for every sensor dir (forces the full
module-load path — catches broken `__init__.py` star-imports that `--help`
would short-circuit past), followed by `--help` on every registered
console script. Together these catch the bug class where a console
script is registered in `pyproject.toml` but its package isn't listed in
`[tool.setuptools.packages.find].include` (`pip install` silently skips
the package, leaving an unresolvable shim in `bin/`), plus broken
re-exports that argparse-exit would mask.

Full automation reference: see [docs/AUTOMATION.md](docs/AUTOMATION.md).

### Pre-commit hook (optional)

To run the consistency lint automatically at `git commit` time — instead of
waiting on CI ~10 min later — install [`pre-commit`](https://pre-commit.com/)
once per clone:

```bash
pip install pre-commit
pre-commit install
```

The repo ships `.pre-commit-config.yaml` with one local hook that runs
`python tools/check_sensor_consistency.py` on every commit. Sub-second
runtime; if the lint flags an inconsistency, the commit is aborted and
you fix locally before pushing. CI still runs the same check as a backstop
for contributors who haven't installed the hook.

### Notebook conventions

All workflow notebooks under `notebooks/` declare a `TARGET_CRS` variable
near the top of their config cell:

```python
# Set CRS for COG output
TARGET_CRS = None
# TARGET_CRS = "EPSG:3857"
```

`None` (the default) preserves the source projection of the input rasters.
The commented `"EPSG:3857"` line is provided for operators who need Web
Mercator output for downstream consumers — most notably `veda-data-airflow`'s
`build_stac` task, which trips on the WGS 84 ensemble + lat-first axis bug
when input COGs are in `EPSG:4326`.

The variable forwards into the CLI invocation:

```python
process_cmd = [
    "process_capella",
    ...
    "-dst_crs", TARGET_CRS if TARGET_CRS else "native",
]
```

`"native"` is the sentinel string every sensor CLI maps back to `None`.

### Adding a new sensor

Short version: run the scaffolder, then implement the calibration math.
See [docs/ADDING_A_NEW_SENSOR.md](docs/ADDING_A_NEW_SENSOR.md) for the full
guide, including notebook conventions and conda-dep decisions.

```bash
python tools/new_sensor.py spire
# Creates spire/{__init__.py,cli.py,process_spire.py,spire_v2.py}
# Updates pyproject.toml: process_spire script + "spire*" include glob
# Creates notebooks/spire_workflow.ipynb + the testing variant
# Runs tools/check_sensor_consistency.py as a post-condition

# Implement the calibration math:
$EDITOR spire/spire_v2.py
```

`tools/new_sensor.py` validates the sensor name, refuses to clobber an
existing sensor, and rolls back all writes if the post-condition check
fails — so the scaffolder either produces a fully-wired sensor or leaves
the repo untouched. CI lint (`tools/check_sensor_consistency.py`) catches
the same bugs at PR time as a backstop. For lower-level `shared_utils`
contributions that aren't a full sensor pipeline, see
[docs/ADDING_FUNCTIONS_TUTORIAL.md](docs/ADDING_FUNCTIONS_TUTORIAL.md).

## Docker Integration

This repo builds the JupyterHub Docker image used on the Disasters Hub
(`hub.disasters.2i2c.cloud`) via the two GitHub Actions workflows in
`.github/workflows/build-and-push{,-dev}.yaml`. Pre-consolidation the image
build lived in a separate `pangeo-notebook-veda-image` repo with cross-repo
dispatch; the two repos were collapsed into this one via `git subtree`
(history preserved under `image/`).

### Automatic Rebuilds

Per-branch trigger:

| Push target | Workflow | Docker Hub tag |
|---|---|---|
| `main` | `build-and-push.yaml` | `klesinger/disasters-jupyterhub-docker-image:latest` (+ `:<sha-12>`) |
| `dev` | `build-and-push-dev.yaml` | `klesinger/disasters-jupyterhub-docker-image-dev:latest` (+ `:<sha-12>`) |

Doc-only changes (`docs/**`, `notebooks/**`, `tests/**`, `tools/**`, `**.md`)
are filtered out via `paths-ignore` so they don't trigger unnecessary
rebuilds. Use the `workflow_dispatch` button in the Actions UI to force a
rebuild manually.

**Monitor build status:** [Actions tab](https://github.com/Disasters-Learning-Portal/disasters-product-algorithms/actions).

### Using in JupyterHub

The package is pre-installed in Disasters Hub environments. All CLI commands and Python APIs are available without additional installation:

```bash
# CLI commands available in terminal
process_landsat89 --help
process_sentinel2 --help
process_capella --help
```

```python
# Python APIs available in notebooks
from landsat import genTrueColor, genNdvi
from sentinel2 import gen_true_color, gen_ndvi
from shared_utils import convert_to_cog, rename_with_event
```

### Docker Image Details

- **Base Image:** `pangeo/pangeo-notebook:<tag>` (pinned in `image/Dockerfile` line 1). Bumping is a one-line PR.
- **Registry:** Docker Hub (`klesinger/disasters-jupyterhub-docker-image{,-dev}`).
- **Build context:** repo root. `image/Dockerfile` is referenced via `docker build -f image/Dockerfile .`. `.dockerignore` at the repo root strips `notebooks/`, `docs/`, `tests/`, `.github/`, etc.
- **Algorithms install:** `pip install --no-deps /srv/repo/algorithms` where `/srv/repo/algorithms` is the COPYed local checkout (NOT cloned from GitHub — no `GH_PAT` needed).

### Adding Dependencies — Pick One File

The Dependency section above lists three places. Quick decision tree:

- **Pip-installable (has manylinux wheel):** `pyproject.toml [project.dependencies]`.
- **Conda-only, only needed locally (CI smoke / your laptop):** `dev-conda-deps.txt`.
- **Conda-only, needed in the hub image:** `image/environment.yml` under `dependencies:`.

The conda-dep comment block at the top of `pyproject.toml` is for **local dev install** convenience, not for the hub image build.

### Pulling Upstream Image Changes

The `image/` subtree was originally imported from `NASA-IMPACT/pangeo-notebook-veda-image`. To pull future upstream commits (rare; ~3 commits in 5 months historically):

```bash
git subtree pull --prefix=image \
  https://github.com/Disasters-Learning-Portal/pangeo-notebook-veda-image.git main \
  --squash
```

The archived `Disasters-Learning-Portal/pangeo-notebook-veda-image` fork's remote URL stays valid for this purpose. To upgrade the Pangeo base image, edit the `FROM pangeo/pangeo-notebook:<tag>` line in `image/Dockerfile` directly.

### Debugging Missing CLIs on the Hub

If `process_<sensor>` is missing on `$PATH` in a fresh hub session, the cause is almost always a build failure or paths-ignore mismatch — NOT a local-install issue. Order of checks:

1. **Did the latest commit on the relevant branch trigger a build?** Doc-only changes are paths-ignored. If your CLI addition was in the same commit as a doc edit, the build still fires; if it was purely a doc commit, no rebuild.
2. **Did the build workflow run pass?** `gh run list --branch <main|dev> --workflow=build-and-push.yaml --limit 3`. If it failed, fix the underlying issue (often a Dockerfile or env.yml problem); CI will rebuild on next push.
3. **Did the lint workflow pass on the same commit?** `gh run list --branch <main|dev> --workflow=lint.yml --limit 3`. If the consistency lint or smoke test failed, the new sensor's pyproject wiring or imports are broken — fix in algorithms, push, the next build picks it up.
4. **Is the entry point present in `pyproject.toml [project.scripts]`?** The consistency lint should have caught this, but worth checking on the specific branch.

Reinstalling locally with `pip install -e .` is a single-session workaround, not a fix. See `docs/HUB_DEPLOYMENT.md` for the full mechanics.

## Development in JupyterHub

If you want to modify and test the package code within a JupyterHub environment (where the package is pre-installed via Docker), you need to ensure your local edits are used instead of the pre-installed version.

### Option 1: Install in Editable Mode (Recommended)

After cloning the repo in JupyterHub, install it in editable mode:

```bash
# Clone the repository
cd ~
git clone https://github.com/Disasters-Learning-Portal/disasters-product-algorithms.git
cd disasters-product-algorithms

# Uninstall the pre-installed version
pip uninstall -y disasters-product-algorithms

# Install your local copy in editable mode
pip install -e .
```

**What `-e` does:**
- Creates a symlink to your local directory instead of copying files
- Any changes you make are immediately reflected when you run the CLI commands
- No need to reinstall after each edit

Now when you run `process_landsat89` or `process_sentinel2`, they'll use your edited code!

### Option 2: Run Scripts Directly (Quick Testing)

If you just want to test changes without reinstalling:

```bash
cd ~/disasters-product-algorithms

# Run the scripts directly with python
python landsat/process_landsat89.py /path/to/data -p true ndvi
python sentinel2/process_sentinel2.py /path/to/data -p true ndvi
```

This bypasses the CLI entry points entirely and runs your local code directly.

### Option 3: Modify PYTHONPATH (Temporary)

Add your local directory to Python's path for the current session:

```bash
cd ~/disasters-product-algorithms
export PYTHONPATH="${PWD}:${PYTHONPATH}"

# Now Python will check your local directory first
python -c "from landsat import process_landsat89; print(process_landsat89.__file__)"
```

### Verification

To verify which version is being used:

```bash
# Check where the CLI command points
which process_landsat89

# Check where Python imports from
python -c "import landsat; print(landsat.__file__)"
```

**Editable mode (Option 1) is best** because it preserves the CLI commands while using your edited code - exactly what you need for development!

## Author

Kaylee Sharp (February 2025)

## License

See repository for license information.
