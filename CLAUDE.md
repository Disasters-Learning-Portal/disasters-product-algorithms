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
- `summarize_raster` — Print min/max/mean/nodata stats for a single GeoTIFF band (`-b`, `-n`, `--json`)

## Critical Constraints

- **Default `dst_crs` / `target_crs` is `EPSG:3857`** (Web Mercator). Reason: EPSG:4326 outputs trigger a `Point outside of projection domain` error in `veda-data-airflow`'s `build_stac` (PROJ writes the WGS 84 ensemble + lat-first axis, which `rio_stac.get_dataset_geom` can't handle). Web Mercator dodges both. Don't change without solving the ensemble + axis problem.
- **`needs_webmerc_clip()`** in `shared_utils/reprojection.py` auto-detects when a source's geographic lat range exceeds ±85.05° AND `dst_crs ≈ EPSG:3857`, in which case `cog_utils.convert_to_cog` injects `-te ... -te_srs EPSG:3857` into gdalwarp. Without this, global Mollweide → 3857 produces 50+ GB of nodata. Returns False for the 99% regional-raster case.
- **There is no `normalize_wgs84_crs()` helper anymore.** The old gdal_edit.py-based approach didn't work (PROJ re-canonicalizes the WKT to the ensemble on read). The replacement is just "use EPSG:3857" (see above).
- GDAL must be installed via conda (not pip) to avoid dylib version mismatches
- S3 credentials use STS assume-role via `aws_credentials.py` when available, fallback to default creds
- COG default: ZSTD compression level 22, 512x512 tiles, 5 overview levels
- Nodata auto-detection: uint8=0, int16=-9999, float=-9999.0
- `main_processor.convert_to_cog` defaults `stream_from_s3=True` — probes `/vsis3/` then falls back to `/tmp` download. Set False for ZSTD-22 heavy workloads where the up-front download avoids many small range-request round-trips.

## How to Run

```bash
# Install (conda recommended for GDAL)
conda install -c conda-forge gdal rasterio rio-cogeo geopandas numpy boto3
pip install -e .

# CLI usage
process_landsat89 --help
process_sentinel2 --help

# Notebooks — run from notebooks/ directory
jupyter notebook notebooks/
```

## API Reference

See `docs/SHARED_UTILS_API.md` for complete function signatures.

## Contributing

See `docs/ADDING_FUNCTIONS_TUTORIAL.md` for the end-to-end walkthrough of adding a new `shared_utils` function and wiring it up as a CLI entry point (worked example: `summarize_raster`).
