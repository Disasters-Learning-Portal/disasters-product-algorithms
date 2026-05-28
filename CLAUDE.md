# Project Guide

## Overview

NASA Disasters product algorithms for satellite imagery processing. Converts raw GeoTIFF data to Cloud Optimized GeoTIFF (COG) format with proper compression, reprojection, and metadata.

## Tech Stack

- Python 3.8+, GDAL/rasterio/rio-cogeo for geospatial processing
- boto3 for AWS S3 integration
- Jupyter notebooks for operator workflows

## Project Structure

- `shared_utils/` — Reusable processing library (COG conversion, S3 ops, validation, metadata)
- `notebooks/` — Operator-facing Jupyter templates for disaster event processing
- `landsat/`, `sentinel2/`, `satellogic/`, `umbra/` — Sensor-specific product generation (CLI entry points)
- `raster_tools/` — Standalone, sensor-agnostic raster utilities exposed as CLIs (currently: `summarize_raster`)
- `tests/fixtures/` — Real-data crops committed for tests (small, <500KB each; e.g. `gaia_atlanta_sample.tif` is a 256×256 GAIA Web-Mercator crop)
- `docs/` — API reference, deployment guides, resampling guide, contributor tutorial

## Key Patterns

- `shared_utils/` modules follow single-responsibility: one concern per file
- **One engine, one orchestrator**:
  - `shared_utils.cog_utils.convert_to_cog(input_tif, ...)` — local-file warp+COG primitive (subprocess `gdalwarp` + `rio cogeo create`)
  - `shared_utils.main_processor.convert_to_cog(name, bucket, ...)` — S3 download (or `/vsis3` stream) → cog_utils → S3 upload. Thin wrapper, ~216 lines.
- **One filename module**: `shared_utils/file_naming.py` is the single source of truth. Notebooks import `extract_datetime_from_filename`, `categorize_file`, `create_output_filename` — never re-define inline.
- Notebooks should be short — import from `shared_utils`, don't inline complex logic
- All temp files go to `/tmp`, cleaned up in `finally` blocks
- All raster hot paths set `NUM_THREADS=ALL_CPUS` (gdalwarp + rio cogeo) or `num_threads=os.cpu_count()` (rasterio.warp.reproject)

## CLI Entry Points (from pyproject.toml)

- `process_landsat89` — Landsat 8/9 product generation
- `process_sentinel2` — Sentinel-2 product generation
- `download_sentinel2` — Sentinel-2 data download
- `process_satellogic` — Satellogic processing
- `process_umbra` — Umbra SAR processing
- `process_capella` — Capella SAR (sigma0 + optional Lee filter)
- `summarize_raster` — Print min/max/mean/nodata stats for a single GeoTIFF band (`-b`, `-n`, `--json`)

All sensor CLIs accept `-dst_crs <EPSG:xxxx | native>`. `native` (default on capella/satellogic/umbra) maps to `None` → preserve source projection. `landsat` and `sentinel2` still default to `EPSG:4326` for back-compat; pass `-dst_crs native` to skip the warp.

## Critical Constraints

- **Library default `dst_crs` / `target_crs` is `EPSG:3857`** (Web Mercator) — applies to `cog_utils.convert_to_cog`, `main_processor.convert_to_cog`, and `SimpleProcessor`. Reason: EPSG:4326 outputs trigger a `Point outside of projection domain` error in `veda-data-airflow`'s `build_stac` (PROJ writes the WGS 84 ensemble + lat-first axis, which `rio_stac.get_dataset_geom` can't handle). Web Mercator dodges both. Don't change without solving the ensemble + axis problem.
- **Notebook templates default `TARGET_CRS = None`** (preserve native projection, fastest — no warp), with a commented `# TARGET_CRS = "EPSG:3857"` alternative directly below. Operators opt-in to Web Mercator when they're about to push through `build_stac`. The variable is forwarded to the CLI as `"-dst_crs", TARGET_CRS if TARGET_CRS else "native"`. This is intentional: native-CRS COGs are fine for browser preview / leafmap but will fail in airflow until reprojected.
- **`needs_webmerc_clip()`** in `shared_utils/reprojection.py` auto-detects when a source's geographic lat range exceeds ±85.05° AND `dst_crs ≈ EPSG:3857`, in which case `cog_utils.convert_to_cog` injects `-te ... -te_srs EPSG:3857` into gdalwarp. Without this, global Mollweide → 3857 produces 50+ GB of nodata. Returns False for the 99% regional-raster case.
- **There is no `normalize_wgs84_crs()` helper anymore.** The old gdal_edit.py-based approach didn't work (PROJ re-canonicalizes the WKT to the ensemble on read). The replacement is just "use EPSG:3857" (see above).
- GDAL must be installed via conda (not pip) to avoid dylib version mismatches
- S3 credentials use STS assume-role via `aws_credentials.py` when available, fallback to default creds
- COG default: ZSTD compression level 22, 512x512 tiles, 5 overview levels
- Nodata auto-detection: uint8=0, int16=-9999, float=-9999.0
- `main_processor.convert_to_cog` defaults `stream_from_s3=True` — probes `/vsis3/` then falls back to `/tmp` download. Set False for ZSTD-22 heavy workloads where the up-front download avoids many small range-request round-trips.

## How to Run

```bash
# Install (conda recommended for GDAL). Single source of truth for the
# dep list is dev-conda-deps.txt at the repo root:
mamba install -y -c conda-forge $(grep -v '^\s*#' dev-conda-deps.txt | grep -v '^\s*$' | tr '\n' ' ')
pip install -e .

# CLI usage
process_landsat89 --help
process_sentinel2 --help
process_capella --help

# Notebooks — run from notebooks/ directory
jupyter notebook notebooks/
```

## API Reference

See `docs/SHARED_UTILS_API.md` for complete function signatures.

## Contributing

- New `shared_utils` function: `docs/ADDING_FUNCTIONS_TUTORIAL.md` (worked example: `summarize_raster`).
- New sensor pipeline (capella, umbra, satellogic-style): `docs/ADDING_A_NEW_SENSOR.md` — copy `capella/` as a template, run `python tools/check_sensor_consistency.py` to validate.

## Automation

### Pre-push / CI lint

The `.github/workflows/lint.yml` workflow runs on every push and PR to `dev`/`main`:

- **`sensor-consistency`**: runs `python tools/check_sensor_consistency.py`, which walks every top-level dir containing `cli.py` + `process_*.py` and asserts each is correctly wired into `pyproject.toml` (both `[tool.setuptools.packages.find].include` and `[project.scripts]`, with the canonical `<pkg>.cli:<verb>_cli` target shape).
- **`cli-smoke`**: bootstraps a conda env from `dev-conda-deps.txt`, runs `pip install .`, then iterates `[project.scripts]` and runs `<script> --help` on each. Catches the bug class where a console script is registered but its package isn't installable (the failure mode that broke the initial capella rollout — `ModuleNotFoundError` on a fresh hub pod despite the shim being in `bin/`).

Run locally before pushing:

```bash
python tools/check_sensor_consistency.py  # <1s
```

### Dependency source-of-truth files

Two files post-consolidation, each with a different audience:

| File | Audience | Format |
|---|---|---|
| `pyproject.toml [project.dependencies]` | `pip install .` transitive deps | pip spec |
| `dev-conda-deps.txt` | Local dev + CI smoke (geospatial stack) | one conda spec per line |
| `image/environment.yml` | Hub image (Pangeo base + extras) | conda env file |

Adding a new dep:
- Has a manylinux wheel → `[project.dependencies]`.
- Conda-only AND only needed locally (CI smoke + laptop) → `dev-conda-deps.txt`.
- Conda-only AND needed in the hub image → add to `image/environment.yml` under `dependencies:`. Most things are already in the Pangeo base image (GDAL, rasterio, rio-cogeo, geopandas, pyproj, numpy, scipy, boto3, etc.), so this file is short.

Full reference: [docs/AUTOMATION.md](docs/AUTOMATION.md).

Pre-consolidation there was a third file, `hub-conda-deps.txt`, which was auto-synced into a separate `pangeo-notebook-veda-image` repo via a `sync-conda-deps.yml` workflow. Both are gone — the image is built here now.

## Disasters Hub deployment (single repo)

- **The Dockerfile, conda env, and build workflows all live in this repo** under `image/` and `.github/workflows/build-and-push{,-dev}.yaml`. Pre-consolidation they lived in a separate `pangeo-notebook-veda-image` repo with cross-repo dispatch; the consolidation collapsed them in here (subtree-imported under `image/` so future `git subtree pull` can mechanically port upstream commits from NASA-IMPACT or `pangeo-data/pangeo-docker-images`).
- **Variant → branch mapping:**
  - Prod image `klesinger/disasters-jupyterhub-docker-image:latest` is built by `.github/workflows/build-and-push.yaml` on push to `main`.
  - Dev image `klesinger/disasters-jupyterhub-docker-image-dev:latest` is built by `.github/workflows/build-and-push-dev.yaml` on push to `dev`.
  - Per-SHA tags `:<sha-12>` are also pushed for both, so the hub can pin to a specific commit when needed.
- **Build context is the repo root** (`docker build -f image/Dockerfile .`). The algorithms code IS the context — no more `git+https://...@$ALGORITHMS_REF` indirection. `.dockerignore` at the repo root strips `notebooks/`, `docs/`, `tests/`, `.github/`, etc. so the image stays lean.
- **Two cache layers preserved:**
  - Layer 1 (~2-3 min): `conda env update` against `image/environment.yml`. Cache key = env.yml content.
  - Layer 2 (~30s): `pip install --no-deps /srv/repo/algorithms` against the COPYed code. Cache key = the COPYed files (minus `.dockerignore` exclusions).
- **Adding a new Python dep**: see the dependency-source-of-truth table above.
- **Bumping the Pangeo base image**: edit the `FROM pangeo/pangeo-notebook:<tag>` line in `image/Dockerfile`. Repo-agnostic; works the same way it did pre-consolidation.
- **Pulling NASA-IMPACT upstream changes** (rare; ~3 commits in 5 months historically): `git subtree pull --prefix=image https://github.com/Disasters-Learning-Portal/pangeo-notebook-veda-image.git main --squash` (the archived fork's remote URL stays valid).
- **`pyproject.toml`'s conda-dep comment block** is the DEV-LOCAL install spec (what to conda-install on your laptop). The hub image gets its conda deps from `image/environment.yml`, not from that comment block.
- If `process_*` CLIs are missing on a fresh hub pod: check the `build-and-push*.yaml` Actions log first. Most common cause is the recently-pushed commit didn't trigger a rebuild (check the `paths-ignore:` filter — doc-only changes intentionally skip the build). Full flow + debug checklist in `docs/HUB_DEPLOYMENT.md`.
