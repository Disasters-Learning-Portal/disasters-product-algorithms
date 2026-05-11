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
- `landsat/`, `sentinel2/`, `satellogic/` — Sensor-specific product generation (CLI entry points)
- `docs/` — API reference, deployment guides, resampling guide

## Key Patterns

- `shared_utils/` modules follow single-responsibility: one concern per file
- Two COG creation paths: CLI subprocess (`rio cogeo create`) and Python API (`cog_translate`)
- Notebooks should be short — import from `shared_utils`, don't inline complex logic
- Processing profiles auto-scale by file size: standard (<3GB), large (3-7GB), ultra-large (>7GB)
- All temp files go to `/tmp`, cleaned up in `finally` blocks

## CLI Entry Points (from pyproject.toml)

- `process_landsat89` — Landsat 8/9 product generation
- `process_sentinel2` — Sentinel-2 product generation
- `download_sentinel2` — Sentinel-2 data download
- `process_satellogic` — Satellogic processing

## Critical Constraints

- GDAL must be installed via conda (not pip) to avoid dylib version mismatches
- S3 credentials use STS assume-role via `aws_credentials.py` when available, fallback to default creds
- COG default: ZSTD compression level 22, 512x512 tiles, 5 overview levels
- Nodata auto-detection: uint8=0, int16=-9999, float=-9999.0

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
