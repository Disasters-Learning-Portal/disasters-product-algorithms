# Project Guide

## Overview

NASA Disasters product algorithms for satellite imagery processing. Converts raw GeoTIFF data to Cloud Optimized GeoTIFF (COG) format with proper compression, reprojection, and metadata.

## Tech Stack

- Python 3.8+, GDAL/rasterio/rio-cogeo for geospatial processing
- boto3 for AWS S3 integration
- Jupyter notebooks for operator workflows

## Project Structure

All Python packages live under `src/` (a conventional **src layout**). `src/` is only a
package-discovery root (`[tool.setuptools.packages.find] where = ["src"]`), **not** part of
the import path — so packages are still imported by their bare name (`import shared_utils`,
`from sentinel2.sentinel2_functions import *`) and the console scripts keep their
`<pkg>.cli:<verb>_cli` targets. Importing requires an install (`pip install -e .`).

- `src/shared_utils/` — Reusable processing library (COG conversion, S3 ops, validation, metadata)
- `src/landsat/`, `src/sentinel2/`, `src/satellogic/`, `src/umbra/`, `src/capella/` — Sensor-specific product generation (CLI entry points)
- `src/raster_tools/` — Standalone, sensor-agnostic raster utilities exposed as CLIs (currently: `summarize_raster`)
- `notebooks/` — Operator-facing Jupyter templates for disaster event processing
- `tests/fixtures/` — Real-data crops committed for tests (small, <500KB each; e.g. `gaia_atlanta_sample.tif` is a 256×256 GAIA Web-Mercator crop)
- `docs/` — API reference, deployment guides, resampling guide, contributor tutorial

## Key Patterns

- `shared_utils/` modules follow single-responsibility: one concern per file
- **One engine, one orchestrator**:
  - `shared_utils.cog_utils.convert_to_cog(input_tif, ..., metadata=None)` — local-file warp+COG primitive. Default: subprocess `gdalwarp` + `rio cogeo create`. When `metadata` is set, routes through in-process `rio_cogeo.cog_translate(additional_cog_metadata=...)` to embed activation-event tags at creation time.
  - `shared_utils.main_processor.convert_to_cog(name, bucket, ..., metadata=None)` — S3 download (or `/vsis3` stream) → cog_utils → S3 upload. Thin wrapper. Forwards `metadata` straight to the engine.
- **One filename module**: `shared_utils/file_naming.py` is the single source of truth. Notebooks import `extract_datetime_from_filename`, `categorize_file`, `create_output_filename` — never re-define inline. The **output-name convention** is one shared transform `cog_utils._relocate_datetime`, used by both `rename_with_event` and `get_final_filename`: a **merged** mosaic ends `_merged_YYYY-MM-DD_day` (date only — spans multiple times), an **individual scene with a time** ends `..._YYYY-MM-DDTHH:MM:SSZ` (ISO-Zulu, no `_day`), a time-less individual ends `_YYYY-MM-DD_day`. `ls_merge`/`s2_merge` take their merge date from `extract_datetime_from_filename` — don't re-inline datetime parsing (`.clinerules.md` rule 27).
- **One version module**: `shared_utils.version` exposes `__version__` (read from `importlib.metadata`, which `setuptools-scm` populates from the latest git tag) and `PROCESSOR_STRING = "NASA Disasters COG Processor v{__version__}"`. Notebooks import `from shared_utils import PROCESSOR_STRING` and stamp it into `ACTIVATION_METADATA['PROCESSOR']` — no hardcoded version strings anywhere in notebooks or `cog_metadata.py`.
- **Two-cell ACTIVATION_METADATA convention** in every operator notebook. The first cell (operator-edited) declares `EVENT_NAME = 'YYYYMM_Hazard_Location'` and `SOURCE = "<sensor default>"`. A separate cell **immediately below** (auto-populated, don't edit) imports `PROCESSOR_STRING` and constructs the `ACTIVATION_METADATA` dict. Subprocess notebooks also dump the dict to a temp JSON file (`ACTIVATION_METADATA_PATH`) which then flows to the CLI via `--metadata-json`. Direct-Python notebooks pass `metadata=ACTIVATION_METADATA` straight into `cog_utils.convert_to_cog`. The split keeps "what an operator edits per activation" cleanly separate from the plumbing that derives the dict + writes the JSON.
- **One CLI flag for metadata**: every sensor CLI (capella/landsat/sentinel2/satellogic/umbra) accepts `--metadata-json <path>`. The parsing helper is `shared_utils.cog_metadata.load_metadata_json(path)` — one line of CLI integration: `metadata = load_metadata_json(args.metadata_json)`. Same parser, same validation, same error messages. The scaffolder template at [`tools/_templates/sensor/process_name.py.tmpl`](tools/_templates/sensor/process_name.py.tmpl) already wires it for future sensors.
- **Sentinel-2 multi-tile convention**: in `notebooks/sentinel2_workflow.ipynb`, `TILE_ID` is a **list** (`["T17RLN", "T17RLM"]`, list even for one tile) and the download/process cells unpack it as `"-tile", *TILE_ID` into the `-tile nargs='*'` CLI arg — `TILE_ID` and `*TILE_ID` are a lockstep pair. The CONFIG cell also owns the credential load (`load_env_local` + `COP_USER`/`COP_PASS`) and `OUTPUT_DIR`/`os.makedirs`; downstream cells reference all three, so a config-cell refactor that drops any of them `NameError`s at runtime. Notebooks aren't linted/smoke-tested in CI — read the whole notebook after editing the config cell. Full rationale: `.clinerules.md` rule 23. The **DPS** Sentinel-2 job (`dps/sentinel2/`) is **download-from-Copernicus**, not file-input like Landsat: `run.sh` runs `download_sentinel2` (by `tile`+`download_date`) then `process_sentinel2`. Copernicus creds are pulled from **MAAP secrets** (`maap.secrets.get_secret` via `dps/_get_secret.py`, default names `COP_USER`/`COP_PASS`) at run time — **never job inputs** (so they never hit the job log). Don't reintroduce a `file_path_of_raw_data` File input or credential value inputs on the Sentinel-2 DPS configs.
- **Satellogic vendor S3 layout**: the `csda-data-vendor-satellogic/disasters/` bucket ships **two coexisting raster layouts** and the pipeline handles both — (1) **vendor scenes** `<folder>/rasters/<stem>_{TOA,CLOUD,VISUAL}_0.tif` (uppercase, single tile; L1D folders carry a 3-digit capture-id, L1B don't), and (2) **analytic-tiled** `<stem>_<zone>_<col>_<row>_{analytic,cloud,visual}.tif` (lowercase, many tiles, no `rasters/` subdir). `group_satellogic_tifs` attaches cloud/visual companions by lowercased suffix but keeps the real-case S3 key; **masking is hardcoded per product (PR #45): composites never mask, indices (NDVI/NDWI/EVI) always mask + get a fresh NaN-aware `apply_lee_filter` (`--filter_size {3,5,7}`, default 5); the DPS job hardcodes `source_label=csda`/`dst_crs=native`/ZSTD-22/per-product nodata, and `--date` accepts a comma list (multi-date).** `build_output_name` is **regex-derived, not positional** → `Satellogic_<SAT>_<product>_[<captureid>_]<col>_<row>_<ISO-Zulu>.tif` (zone/level dropped, product from `--product`). Full rationale: `.clinerules.md` rule 24.
- **Umbra SAR always-on speckle filter (PR #44)**: the Umbra pipeline mirrors the Satellogic hardcoding. `umbra_v2` calibrates `sigma`/`beta`/`gamma` only — **the RCS product was removed** (`rcsCalib` deleted, `"rcs"` dropped from `--product` choices). A dedicated NaN-aware `umbra_v2.lee_filter(img, size)` (same algorithm as `satellogic_v2.apply_lee_filter`) is applied to the **linear** backscatter **inside** each calib function *before* the `20*log10` dB conversion (then `np.clip(..., 1e-10, None)` keeps the log finite); the output name gains a `_filtered{size}` token (Capella convention). **Filtering is always on — there is no `--apply_filter` toggle anymore**; `--filter_size` is restricted to `choices=[3,5,7]` (default 5). The old separate `apply_filter()` (a post-step percentile dB stretch) was deleted — the COG now carries raw dB and any display stretch belongs at the PNG layer (`png_min`/`png_max`). The DPS job (`dps/umbra/`) drops the `apply_filter` input, validates `filter_size ∈ {3,5,7}` via `validate_in_set`, and always passes `--filter_size`. Full rationale: `.clinerules.md` rule 31.
- Notebooks should be short — import from `shared_utils`, don't inline complex logic
- All temp files go to `/tmp`, cleaned up in `finally` blocks
- All raster hot paths set `NUM_THREADS=ALL_CPUS` (gdalwarp + rio cogeo) or `num_threads=os.cpu_count()` (rasterio.warp.reproject)
- **Batch loops use threads, not processes**: `shared_utils.parallel.map_threaded(func, items, max_workers, desc)` is the one helper for fanning out N independent S3 keys / files / subprocess calls. Threads (not `ProcessPoolExecutor`) because S3 I/O (boto3) and GDAL native calls both release the GIL — and a ProcessPool would oversubscribe cores since each worker's `convert_to_cog` already uses `NUM_THREADS=ALL_CPUS` internally. Default `max_workers=4` for in-process loops (mix of S3 + GDAL); `max_workers=2` when the worker spawns a full GDAL subprocess (each subprocess saturates all cores). `process_batch_s3()` in `shared_utils.cog_processing` is the pre-baked wrapper for `[(src_key, dst_key), ...]` batches; `SimpleProcessor._process_category` parallelizes per-category via `config['max_workers']` (default 4). Operator notebooks should use `map_threaded` instead of inline `for x in items: convert_to_cog(...)` loops.

## CLI Entry Points (from pyproject.toml)

- `process_landsat89` — Landsat 8/9 product generation
- `process_sentinel2` — Sentinel-2 product generation
- `download_sentinel2` — Sentinel-2 data download
- `process_satellogic` — Satellogic processing
- `process_umbra` — Umbra SAR (sigma/beta/gamma; always-on Lee filter, `--filter_size {3,5,7}`)
- `process_capella` — Capella SAR (sigma0 + optional Lee filter)
- `summarize_raster` — Print min/max/mean/nodata stats for a single GeoTIFF band (`-b`, `-n`, `--json`)

All sensor CLIs **except Satellogic** accept `-dst_crs <EPSG:xxxx | native>`, and **all default to `native`** (maps to `None` → preserve source projection, no warp). Satellogic dropped the flag in PR #45 and always uses native. **Hint:** pass `-dst_crs EPSG:3857` (Web Mercator) when the COG is headed for the NASA VEDA dashboard — 3857 is optimal for the **titiler-pgstac** tiling API (its default `WebMercatorQuad` TMS ⇒ fastest tiles, no per-tile reproject) and sidesteps the `build_stac`/`rio_stac` geometry crash that `EPSG:4326` raster outputs trigger (see Critical Constraints). native COGs still ingest + tile (titiler reprojects on the fly) — just slower.

All sensor CLIs also accept `--metadata-json <path>`. The path points at a JSON file with `{"ACTIVATION_EVENT": ..., "SOURCE": ..., "PROCESSOR": ..., ...}`; the tags get embedded as GeoTIFF metadata on every output COG. The activation event string is auto-split into YEAR_MONTH/HAZARD/LOCATION by `shared_utils.cog_metadata.resolve_metadata` before embedding, so operators only set `ACTIVATION_EVENT='201808_Flood_TX'` and the rest is derived.

## Critical Constraints

- **Library default `dst_crs` / `target_crs` is `EPSG:3857`** (Web Mercator) — applies to `cog_utils.convert_to_cog`, `main_processor.convert_to_cog`, and `SimpleProcessor`. Reason: EPSG:4326 outputs trigger a `Point outside of projection domain` error in `veda-data-airflow`'s `build_stac` (PROJ writes the WGS 84 ensemble + lat-first axis, which `rio_stac.get_dataset_geom` can't handle). Web Mercator dodges both. Don't change without solving the ensemble + axis problem.
- **Notebook templates default `TARGET_CRS = None`** (preserve native projection, fastest — no warp), with a commented `# TARGET_CRS = "EPSG:3857"` alternative directly below. Operators opt-in to Web Mercator when they're about to push through `build_stac`. The variable is forwarded to the CLI as `"-dst_crs", TARGET_CRS if TARGET_CRS else "native"`. This is intentional: native-CRS COGs are fine for browser preview / leafmap but will fail in airflow until reprojected.
- **`needs_webmerc_clip()`** in `shared_utils/reprojection.py` auto-detects when a source's geographic lat range exceeds ±85.05° AND `dst_crs ≈ EPSG:3857`, in which case `cog_utils.convert_to_cog` injects `-te ... -te_srs EPSG:3857` into gdalwarp. Without this, global Mollweide → 3857 produces 50+ GB of nodata. Returns False for the 99% regional-raster case.
- **There is no `normalize_wgs84_crs()` helper anymore.** The old gdal_edit.py-based approach didn't work (PROJ re-canonicalizes the WKT to the ensemble on read). The replacement is just "use EPSG:3857" (see above).
- GDAL must be installed via conda (not pip) to avoid dylib version mismatches
- S3 credentials use STS assume-role via `aws_credentials.py` when available, fallback to default creds
- **`.tif` matching is case-insensitive** (CSDA vendor data ships uppercase `.TIF`). All listing/selection predicates use `name.lower().endswith(...)` — `list_s3_files` (`s3_operations.py`) plus the inline filters in `capella_v2`/`umbra_v2`/`satellogic_v2`. Returned S3 keys keep their real case (a lowercased key 404s on fetch); the **local** copy is normalized to lowercase `.tif` via `shared_utils.s3utils.local_tif_basename`, used by `download_s3_file` + the SAR download-cache checks. COG outputs are already `.tif` by construction. Full detail: `.clinerules.md` rule 20.
- COG default: ZSTD compression level 22, 512x512 tiles, 5 overview levels. The `simple_disaster_template` notebooks expose `COMPRESSION`/`COMPRESSION_LEVEL` in their INPUTS cell (default **9**), and `SimpleProcessor` now forwards `config['compression'/'compression_level']` to `convert_to_cog` (it previously dropped them → always 22). The library-wide default stays 22.
- Nodata auto-detection: uint8=0, int16=-9999, float=-9999.0
- **`-mask` (Landsat/Sentinel-2) masks index products + water extent only, never color composites** (changed in #28, 2026-07-06). The cloud mask is passed to `genNdvi/genNdwi/genmNdwi/genEvi/genNbr` (+ S2 `gen_ndwi/gen_mndwi/gen_ndvi/gen_nbr`) and `gen_water_extent`, but `None` to `genTrueColor/genNaturalColor/genColorInfrared/genPanchromatic` (+ S2 `gen_swir`). Merge-path masking is gated by an **exact product-folder basename** match (`is_index = os.path.basename(...).lower() in {ndvi,ndwi,mndwi,evi,nbr}`), not a substring search. Full rationale in `.clinerules.md` rule 22.
- `main_processor.convert_to_cog` defaults `stream_from_s3=True` — probes `/vsis3/` then falls back to `/tmp` download. Set False for ZSTD-22 heavy workloads where the up-front download avoids many small range-request round-trips.
- **GDAL 3.10+ refuses to update a COG in-place.** Post-step `gdal.Open(path, GA_Update); ds.SetMetadata(...)` returns `RuntimeError: ... has COG layout. Updating it will generally result in losing part of the optimizations.` With `IGNORE_COG_LAYOUT_BREAK=YES` the call succeeds but the result fails `cog_validate` (main IFD offset bloats, overview-IFD ordering inverted). **This is why `cog_utils.convert_to_cog` switches to in-process `rio_cogeo.cog_translate(additional_cog_metadata=...)` whenever `metadata` is provided** — embedding has to happen at creation, not as a post-step. Do not reintroduce the post-step pattern (we tried; it broke; commit `de80b1a` has the empirical validation).
- **Landsat merge path (`-merge`) invariants** (fixed 2026-06-29, commit `bef7b8e`; full rationale in `.clinerules.md` rule 18):
  - **`gen_merge` must never reopen a source scene in `'w'` mode.** Mismatched-CRS inputs reproject into a `tempfile.mkdtemp()` dir (cleaned in `finally`), never overwriting the operator's input file. The old in-place reprojection silently destroyed native-CRS scenes on cross-UTM-zone spans. Pinned by `tests/integration/test_landsat_merge_pipeline.py`.
  - **`ls_merge`** takes the merge date from `file_naming.extract_datetime_from_filename` (positional `parts[2]` is the *time* on already-renamed files), excludes prior `*merged*.tif` from its inputs, and raises `FileNotFoundError` on an empty dir.
  - **`cog_utils.rename_with_event` and `cog_utils.get_final_filename` are a lockstep predictor/actual pair** — both delegate to the single `cog_utils._relocate_datetime` transform (rule 27), so they cannot drift (multi-token Sentinel-2 products are preserved because the transform keeps all non-date/-time tokens in order). Any "already exists?" skip check must glob the post-rename name via `get_final_filename`, never the raw name.
  - The merge post-step rename is one shared helper, `rename_individual_scene_files(directory, event)` — don't re-inline it per loop.
- **Sentinel-2 merge path (`s2_merge`/`gen_merge`) mirrors the Landsat invariants** (ported 2026-07-16; full rationale in `.clinerules.md` rule 26):
  - **`sentinel2_functions.gen_merge` must never reopen a source in `'w'` mode** — mismatched-CRS inputs reproject into a `tempfile.mkdtemp()` dir (prefix `s2_merge_reproj_`, cleaned in `finally`). The old in-place `rio.open(tif, 'w')` truncated the file mid-read and corrupted the merge on cross-UTM-zone spans.
  - **`s2_merge(dir, mask=True)` returns the MASKED path** (`<prod>/masked/<...>_merged_masked_<ts>.tif`), not the unmasked merge — else `process_sentinel2` COGs/uploads the unmasked product and orphans the masked copy. Reconstruct it from the same split `apply_cloud_mask` uses. Pinned by `tests/integration/test_sentinel2_merge_pipeline.py`.
  - **`s2_merge` raises `FileNotFoundError` (not `IndexError`) on an empty product dir** (2026-07-21) — mirrors `ls_merge`; it excludes prior `*merged*` from its inputs and guards the empty case instead of `ims[0]`-crashing. A product that generated nothing (e.g. an L1C SWIR band missing → `gen_swir` raised + was caught) left an empty dir the merge choked on. **Both `process_sentinel2` merge loops (cloudMask + product) now SKIP a product dir with no non-merged tifs** so one failed product can't abort the whole merge; the Landsat loops got the same skip for parity. `extract_band_geotiffs` raises a clear `FileNotFoundError("band … not found")` (L1C + L2A) instead of a cryptic `glob(...)[0]` IndexError. Pinned by `test_s2_merge_empty_dir_raises` + `test_s2_merge_only_prior_merged_raises`.

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

## Git & Attribution

- **No AI/Claude attribution in any artifact.** Commit messages, PR titles/bodies, and code
  comments must NOT contain `Co-Authored-By: Claude` (or any `Co-authored-by: Claude ...`)
  trailers, "Generated with Claude Code", the 🤖 emoji, or a Claude avatar/co-author. The human
  is the sole author; commit/PR messages contain only real human co-authors. This applies even
  when a harness default would otherwise append such a line.

## Contributing

- New `shared_utils` function: `docs/ADDING_FUNCTIONS_TUTORIAL.md` (worked example: `summarize_raster`).
- New sensor pipeline (capella, umbra, satellogic-style): `docs/ADDING_A_NEW_SENSOR.md` — copy `capella/` as a template, run `python tools/check_sensor_consistency.py` to validate.

## Automation

### Pre-push / CI lint

The `.github/workflows/lint.yml` workflow runs on every push and PR to `dev`/`main`:

- **`sensor-consistency`**: runs `python tools/check_sensor_consistency.py`, which walks every dir under `src/` containing `cli.py` + `process_*.py` and asserts each is correctly wired into `pyproject.toml` (both `[tool.setuptools.packages.find].include` and `[project.scripts]`, with the canonical `<pkg>.cli:<verb>_cli` target shape).
- **`cli-smoke`**: bootstraps a conda env from `dev-conda-deps.txt`, runs `pip install .`, then iterates `[project.scripts]` and runs `<script> --help` on each. Catches the bug class where a console script is registered but its package isn't installable (the failure mode that broke the initial capella rollout — `ModuleNotFoundError` on a fresh hub pod despite the shim being in `bin/`). **Coverage boundary:** `--help` and the bare `import` do *not* execute the processing path, and the dispatch scripts have no importable `main()` (they run under `if __name__ == "__main__":` / bare module level) — so a runtime error in the dispatch is invisible to CI. That path is guarded separately by `tests/integration/test_sensor_mask_smoke.py` (runtime) + `test_dispatch_undefined_names.py` (symtable static check); see `.clinerules.md` rule 21 and `docs/AUTOMATION.md`.

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
| `image/environment.yml` | Hub image (MAAP `2i2c/pangeo` base + extras) | conda env file |

Adding a new dep:
- Has a manylinux wheel → `[project.dependencies]`.
- Conda-only AND only needed locally (CI smoke + laptop) → `dev-conda-deps.txt`.
- Conda-only AND needed in the hub image → add to `image/environment.yml` under `dependencies:`. Most things are already in the MAAP base image (GDAL, rasterio, rio-cogeo, geopandas, pyproj, numpy, scipy, boto3, etc. — plus `maap-py` + the MAAP JupyterLab extensions), so this file is short.

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
- **JupyterLab UI customization is baked in via `image/overrides.json`** (COPYed to `/srv/conda/envs/notebook/share/jupyter/lab/settings/overrides.json`, the sys-prefix settings-override path). Currently a **Help → Disasters Resources** submenu (bug report → repo Issues, forum → repo Discussions, email → mailto). No extension / node build — uses the built-in `help:open` command. Two non-obvious rules: menu customizations **merge** with (don't replace) the default Help menu, and external links must set `newBrowserTab: true` (else GitHub/SaaS pages hit `X-Frame-Options: DENY` in the in-lab iframe). Editing it triggers a hub rebuild (`image/**` is in the build workflows' `paths` allowlist). Full how-to + no-rebuild iteration trick in `docs/HUB_DEPLOYMENT.md`.
- **Adding JupyterLab extensions**: `docs/HUB_EXTENSIONS.md` is a curated shortlist of extensions that complement the MAAP DPS workflow (COG preview via `leafmap`/`localtileserver`, notebook productivity, MAAP ecosystem tiles), plus what's already in the base image (don't re-add) and what to skip. Nothing there is installed yet — it's a decision aid, not applied config.
- **Bumping the base image**: edit the `FROM mas.maap-project.org/root/maap-workspaces/2i2c/pangeo:<tag>` line in `image/Dockerfile`. The MAAP base (a NASA-VEDA / pangeo derivative) also ships `maap-py` + the MAAP JupyterLab extensions, so those are inherited — not pinned in `environment.yml`. `NB_USER=jovyan` and the `/srv/conda/envs/notebook` prefix are unchanged from the old pangeo base.
- **Pulling NASA-IMPACT upstream changes** (rare; ~3 commits in 5 months historically): `git subtree pull --prefix=image https://github.com/Disasters-Learning-Portal/pangeo-notebook-veda-image.git main --squash` (the archived fork's remote URL stays valid).
- **`pyproject.toml`'s conda-dep comment block** is the DEV-LOCAL install spec (what to conda-install on your laptop). The hub image gets its conda deps from `image/environment.yml`, not from that comment block.
- If `process_*` CLIs are missing on a fresh hub pod: check the `build-and-push*.yaml` Actions log first. Most common cause is the recently-pushed commit didn't trigger a rebuild — the build workflows use a **`paths` allowlist** (`image/**`, `src/**`, `pyproject.toml`, plus the workflow file itself), so a push touching only `dps/`, `docs/`, `notebooks/`, `tests/`, `tools/`, `.github/`, etc. intentionally fires no rebuild (those are all `.dockerignore`d out of the image anyway). Force one with `workflow_dispatch`. Full flow + debug checklist in `docs/HUB_DEPLOYMENT.md`.

## MAAP DPS (Data Processing System)

All 5 sensors are registerable as MAAP DPS algorithms. Plumbing lives in `dps/` (per-sensor `algorithm_config.yaml` + `run.sh` + `build-env.sh`, shared `environment.yml` + `_finalize.sh` + `register_algorithms.py`). **Full guide + every gotcha: `docs/DPS.md`.** The non-obvious essentials:

- **Registration schema is OGC/CWL**, not the legacy maap-py format. `algorithm_name` must match `^[a-z0-9_-]+$` (lowercase, no spaces/slashes — e.g. `landsat-8-9`). Use **`base_container_url`** (NOT `docker_container_url`) = `mas.maap-project.org/root/maap-workspaces/custom_images/maap_base:v4.2.0`. **`build_command`/`run_command` MUST be prefixed with the repo dir name** (`disasters-product-algorithms/dps/<sensor>/...`) — MAAP clones to `/app/<repo>/` and runs from `/app`. Resources are `ram_min`/`cores_min`/`outdir_max` (not queue/disk_space). `inputs` is a flat list `{name,label,doc,type,default}`; valid types = string/int/File/Directory/long/float/boolean/double (no enum/array); toggles = `boolean`. **Use BASE types only — do NOT add the `?` optional suffix while registering via the GUI.** Two MAAP forms conflict (both confirmed in source): the **Register Algorithm GUI** (`algorithms-jupyter-extension/src/constants.ts`) hard-codes the Type dropdown to `string/int/File/Directory/long/float/boolean/double` — a `?`-suffixed type is NOT a dropdown option, so it errors *"Please select an item in the list."* and can't register. The **Submit Job** form (`dps-jupyter-extension/SubmitJob.tsx`: `value = formInputs[key] || null; if (!input.optional && value==null) → "Valid value required."`) instead wants OGC `optional` (= `minOccurs:0`), which is only set by `?` — registrable **only via the CLI `ogc-app-pack-generator`** (`build_cwl_workflow.py` passes the type through), NOT the GUI. Net: GUI = base types, and falsy-defaulted inputs (`boolean:false`, `string:""`) will hit *"Valid value required"* at Submit Job unless you register via the CLI path with `type: X?`. `?` in a GUI-registered config is the harder failure (blocks registration), so keep it out.
- **run.sh receives `--name value` flags** (CWL), not positional `$1`. File inputs are localized to a path; booleans may arrive as presence or value (parsers handle both). **Each run.sh boolean default MIRRORS its `algorithm_config.yaml` default** (NOT a blanket `false`) — if MAAP omits a default-valued boolean, a `false` default would silently invert `merge`/`mask`/`save_png`/`delete_cog`; see `docs/DPS.md` "How run.sh receives inputs". Shared output flow is in **`dps/_finalize.sh`** (don't duplicate per sensor): `~/drcs_outputs/<event>/` → PNG (`save_png`, via `shared_utils.plotting.save_cog_png`) → `output/` (DPS uploads — COG never lost) → S3 (`enable_s3_upload` → `nasa-disasters`) → delete COG (`delete_cog` default true).
- **Guard rails (fail-fast, `dps/_validate.sh`):** the OGC/CWL schema has no enum/pattern/min-max, so `run.sh` is the only enforcement point. Every run.sh sources the shared **`dps/_validate.sh`** and validates inputs right after parsing, before conda/staging — `activation_event` must match `YYYYMM_Hazard_Location` (placeholder REJECTED); `source_label` required; `dst_crs` = `native`|`EPSG:<code>`; `compression_level` 1–22; per-sensor `date` format; optical `products`/Satellogic `level` membership; granule file exists + `.tar`/`.zip`. **Satellogic (PR #45) hardcodes `source_label`/`dst_crs`/`compression_level`/`nodata` — those validators don't run for it — and validates `filter_size ∈ {3,5,7}` instead.** Optical `-p` is checked in bash because the CLI's own check ends in `quit()` → exit 0. Assertions: `tests/integration/test_dps_validate.sh` (+ `.py` wrapper). `dst_crs` still defaults to `native` (EPSG:3857 is NOT required for `build_stac`). Scene-date discovery for SAR `--date`: see docs/DPS.md "Finding available scenes" (`aws s3 ls s3://<vendor>/disasters/`), or run the standalone **`list-dates`** discovery algorithm (`dps/list_dates/`) for an in-DPS report (**a separate registered algorithm with a `sensor` selector: capella/umbra/satellogic** — which validates the `sensor` selector then dispatches to `process_<sensor> --list_dates`, prints an aligned table of available scenes newest-first by S3 delivery time to the job log — columns `--date`/acquired/added-to-S3/**scene folder** — then exits; reuses `s3utils.retrieve_s3_file_list_with_timestamps` + each sensor's `report_<sensor>_scenes()` in `<sensor>_v2`, which carries the `scene` folder name and pre-formats `date` to that sensor's `--date` grammar. Scene folder is `parts[1]` for capella/satellogic, `parts[2]` for umbra; **satellogic's report is level-scoped** — run.sh validates `level` then forwards `--level`). The same rows are also written to **`output/available_<sensor>_dates.csv`** (run.sh passes `--output output`) — browsable via the Jobs panel's **Outputs → Open in File Browser** as a sortable grid. A DPS job is headless: it prints to the log + drops the CSV, but can't pop a UI or push text into the MAAP DPS extension panel; an auto-rendering picker must run in a live notebook kernel (`ipywidgets`).
- **Register from the MAAP hub's Register Algorithm UI** (the OGC/CWL path). `dps/register_algorithms.py` uses maap-py 4.2.0's LEGACY schema — don't use it for these configs. The GUI extensions need `maapApiUrl=https://api.maap-project.org` (NO `/api` suffix) + `maapToken` (MAAP_PGT) in Settings → MAAP Settings. `maap-py` + the 3 MAAP JupyterLab extensions are inherited from the MAAP `2i2c/pangeo` base image (no longer pinned in `image/environment.yml` — the base ships them) so registration works from the Disasters hub.
- **SAR sensors (capella/umbra/satellogic) + `list-dates` read CSDA vendor buckets** (no file input). The DPS worker (`dps-verdi-role`, acct 884094767067) can write `nasa-disasters` but lacks `s3:ListBucket` on the cross-account CSDA buckets → `AccessDenied`. Fix (2026-07-20): **`dps/_env.sh`** exports `READ_ROLE_ARN=arn:aws:iam::515966502221:role/disasters-prod` (the role the hub reads them as) and is sourced by the four vendor-reading run.sh, so `shared_utils.s3utils._read_session` **assumes `disasters-prod`** for every vendor read (list + download), **no ExternalId**. **Not a library default** (on the hub the ambient identity already *is* `disasters-prod` → an unconditional assume would self-assume and fail). **Infra prerequisite, not settable from this repo:** `disasters-prod`'s trust policy must allow `dps-verdi-role` to `sts:AssumeRole` (manual IAM edit in acct 515966502221). You **cannot test the assume from the hub notebook** (it *is* `disasters-prod`; self-assume is denied). Full detail + error taxonomy: `docs/DPS.md` "Vendor read access".
- One container is built per repo+branch (all algorithms share it, differing by `run_command`); "My Builds" shows the repo name. The **Submit Jobs Process dropdown** is the real "is it deployed" check.
- **Deploying an algorithm code change = push to `origin/dev` FIRST, THEN re-register** (Register Algorithm UI, same `algorithm_version`). MAAP builds by git-cloning from GitHub, not your local tree — unpushed changes are invisible (the #1 "re-registered but nothing changed" trap = code not on `origin/dev` yet). You do **NOT** rebuild the hub image for algorithm code: the hub image (`image/`, JupyterLab env, built by GitHub Actions) and the DPS algorithm container (built by MAAP from the branch via `build-env.sh`) are separate artifacts — `dps/` is `.dockerignore`d precisely because DPS clones it from git. `dev` is **PR-protected**: a direct `git push origin dev` is refused unless your account bypasses it (logs `Bypassed rule violations`); prefer a PR into `dev`. Full loop: `docs/DPS.md` "Deploying a code change".
