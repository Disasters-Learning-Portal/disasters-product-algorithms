# 🚀 v1.0.0 — First stable release

🛰️ NASA Disasters product algorithms for satellite imagery processing. This library converts raw GeoTIFFs from multiple sensors into Cloud Optimized GeoTIFFs (COGs) ready for downstream STAC ingestion and web map serving, with batteries-included CLIs, operator Jupyter templates, and a reproducible Pangeo-based JupyterHub image.

This is the first stable release. ✨ The API, CLI surface, and hub deployment have settled into a single, well-tested shape — from here forward, breaking changes will follow semver.

---

## 📦 What's included

### 🛰️ Sensors supported

| Sensor | CLI | Output |
|---|---|---|
| 🌎 Landsat 8/9 | `process_landsat89` | COG with disaster-product bands |
| 🌍 Sentinel-2 | `process_sentinel2`, `download_sentinel2` | COG + downloader |
| 🛰️ Satellogic | `process_satellogic` | COG |
| 📡 Umbra (SAR) | `process_umbra` | COG |
| 📡 Capella (SAR) | `process_capella` | sigma0 COG, optional Lee filter |

### 🔧 General-purpose tools

- 📊 **`summarize_raster`** — print min / max / mean / nodata stats for a GeoTIFF band (`-b`, `-n`, `--json`).
- 🧰 **`shared_utils`** library — reusable building blocks for COG conversion, S3 I/O, validation, metadata, reprojection, chunked processing, and compression. See [`docs/SHARED_UTILS_API.md`](docs/SHARED_UTILS_API.md).

### 📓 Operator Jupyter templates

12 ready-to-run notebooks under [`notebooks/`](notebooks/) covering per-sensor workflows, local-file processing, CSV-driven batch jobs, file renaming, and COG metadata inspection. Designed to be short — heavy logic lives in `shared_utils`.

---

## 🏗️ Architecture highlights

- ⚙️ **One engine, one orchestrator** for COG conversion:
  - `shared_utils.cog_utils.convert_to_cog` — local-file warp + COG primitive (`gdalwarp` + `rio cogeo create`).
  - `shared_utils.main_processor.convert_to_cog` — S3 download (or `/vsis3` stream) → engine → S3 upload. Streams from S3 by default.
- 📛 **Single source of truth for filenames**: `shared_utils.file_naming` (`extract_datetime_from_filename`, `categorize_file`, `create_output_filename`). Notebooks import it; nothing redefines.
- 🌐 **Smart Web Mercator handling**: `needs_webmerc_clip()` auto-detects global rasters (lat range > ±85.05°) heading to EPSG:3857 and injects a `-te / -te_srs` clip in `gdalwarp` — prevents 50+ GB nodata blowups on global Mollweide → 3857 reprojections.
- 🗺️ **EPSG:3857 library default**: side-steps a PROJ ensemble + lat-first axis bug in `veda-data-airflow`'s `build_stac` that bites EPSG:4326 outputs.
- 🧵 **Threaded everywhere**: hot paths set `NUM_THREADS=ALL_CPUS` (gdalwarp + rio cogeo) and `num_threads=os.cpu_count()` (rasterio.warp.reproject).
- 📐 **COG defaults**: ZSTD level 22, 512×512 tiles, 5 overview levels, auto-nodata by dtype (uint8 → 0, int16/float → -9999).

---

## 🐳 Reproducible JupyterHub image

The Disasters JupyterHub Pangeo image is built from this repo (consolidated from `pangeo-notebook-veda-image` in 2026-05):

- 🟢 `klesinger/disasters-jupyterhub-docker-image:latest` — built from `main` (push to `main` triggers `.github/workflows/build-and-push.yaml`).
- 🟡 `klesinger/disasters-jupyterhub-docker-image-dev:latest` — built from `dev` (push to `dev` triggers `.github/workflows/build-and-push-dev.yaml`).
- 📌 Per-SHA tags (`:<sha-12>`) are also pushed so the hub can pin to a specific commit.

Image conda dependencies live in [`image/environment.yml`](image/environment.yml); the Dockerfile in [`image/Dockerfile`](image/Dockerfile) layers `pip install --no-deps /srv/repo/algorithms` on top of the Pangeo base. See [`docs/HUB_DEPLOYMENT.md`](docs/HUB_DEPLOYMENT.md) for the full build flow, cache strategy, and debug checklist.

---

## 🤝 Contributor tooling

- 🪄 **`tools/new_sensor.py`** — scaffolder that generates a new sensor pipeline (package, CLI shim, notebook stub, pyproject entries). Includes an orphan-detection pre-check so re-runs after partial rollbacks don't trip the consistency lint. See [`docs/ADDING_A_NEW_SENSOR.md`](docs/ADDING_A_NEW_SENSOR.md).
- ✅ **`tools/check_sensor_consistency.py`** — sub-second lint that walks every top-level sensor dir and asserts each is correctly wired into `pyproject.toml`. Runs in CI via [`.github/workflows/lint.yml`](.github/workflows/lint.yml).
- 💨 **CLI smoke test** — CI bootstraps a conda env from `dev-conda-deps.txt`, runs `pip install .`, and `--help`s every registered CLI. Catches the "console script registered but package not installable" failure mode.

---

## 💾 Install

```bash
# Conda is required for GDAL (avoids dylib version mismatches)
mamba install -y -c conda-forge $(grep -v '^\s*#' dev-conda-deps.txt | grep -v '^\s*$' | tr '\n' ' ')
pip install -e .

# Verify
process_landsat89 --help
process_capella --help
```

For the JupyterHub image, see [`docs/HUB_DEPLOYMENT.md`](docs/HUB_DEPLOYMENT.md).

---

## 📚 Documentation

| Doc | What it covers |
|---|---|
| 🏠 [`README.md`](README.md) | Tour, install, getting started |
| 📖 [`docs/SHARED_UTILS_API.md`](docs/SHARED_UTILS_API.md) | Full public API reference |
| 🎚️ [`docs/RESAMPLING_GUIDE.md`](docs/RESAMPLING_GUIDE.md) | Resampling strategy by dtype |
| 🐳 [`docs/HUB_DEPLOYMENT.md`](docs/HUB_DEPLOYMENT.md) | JupyterHub image build + debug |
| 🤖 [`docs/AUTOMATION.md`](docs/AUTOMATION.md) | CI workflows + dependency source-of-truth |
| ➕ [`docs/ADDING_A_NEW_SENSOR.md`](docs/ADDING_A_NEW_SENSOR.md) | New-sensor scaffolder walk-through |
| 🛠️ [`docs/ADDING_FUNCTIONS_TUTORIAL.md`](docs/ADDING_FUNCTIONS_TUTORIAL.md) | Adding to `shared_utils` |

---

## 🧪 Compatibility

- 🐍 Python 3.8+
- 🗺️ GDAL via conda (Pangeo base image or `dev-conda-deps.txt`)
- ☁️ AWS S3 (boto3, STS assume-role supported via `aws_credentials.py`)
- ✅ Tested on Ubuntu 24.04, Pangeo notebook image

---

## ⚠️ Known limitations

- 📝 Notebook templates default `TARGET_CRS = None` (preserve native projection — fast, no warp). Native-CRS COGs are fine for browser preview and `leafmap`, but **will fail in `veda-data-airflow`'s `build_stac`** until reprojected. Operators opt into Web Mercator (`TARGET_CRS = "EPSG:3857"`) when pushing to airflow.
- 🔄 `landsat` and `sentinel2` CLIs still default to `EPSG:4326` for back-compat. Pass `-dst_crs native` to preserve source projection, or `-dst_crs EPSG:3857` to match the library default.

---

## 🙏 Acknowledgments

Built for the NASA Disasters program. Hub image derived from [`pangeo-data/pangeo-docker-images`](https://github.com/pangeo-data/pangeo-docker-images) via [`Disasters-Learning-Portal/pangeo-notebook-veda-image`](https://github.com/Disasters-Learning-Portal/pangeo-notebook-veda-image) (now consolidated into this repo).
