# Running on MAAP DPS

This repo's sensor pipelines can run as jobs on the MAAP
[Data Processing System (DPS)](https://docs.maap-project.org/en/latest/technical_tutorials/dps_tutorial/dps_tutorial_demo.html).
All the DPS plumbing lives under [`dps/`](../dps/); this page explains how it
works, how to test it, and the one open question blocking the SAR sensors.

## How DPS runs an algorithm

DPS clones the registered (public) git repo, then:

1. Runs the registered **build command** once per worker image build — our
   `dps/<sensor>/build-env.sh` creates the `disasters_dps` conda env from the
   shared [`dps/environment.yml`](../dps/environment.yml) and `pip install`s this
   repo so the `process_*` console scripts exist.
2. Runs the registered **run command** per job — our `dps/<sensor>/run.sh`.

The DPS I/O contract:

| DPS provides | Where | How we use it |
|---|---|---|
| `file` inputs | downloaded into relative `input/` | the granule archive (`.tar`/`.zip`) |
| `positional` inputs | `$1 $2 …` in registration order | event, products, CRS, source label |
| `output/` dir | uploaded to S3 after the job | we copy products here |
| stdout / stderr | captured to `_stdout` / `_stderr` | normal logging |

## Why only Landsat + Sentinel-2 (the file-input model)

The two optical sensors take a **local directory of granule archives** as their
positional `input` arg — a clean 1:1 fit for DPS's file-input model:

- DPS downloads the granule into `input/`.
- `run.sh` calls `process_landsat89 input/ …` (or `process_sentinel2`), which
  unpacks to `input/unpacked/` and writes product COGs to **`input/output/`**
  (`src/landsat/process_landsat89.py:178`, `src/sentinel2/process_sentinel2.py:257`).
- `run.sh` then copies `input/output/.` → `output/` so DPS uploads the COGs.
  **This copy is load-bearing** — without it the job "succeeds" but uploads
  nothing.

### Activation metadata

`run.sh` writes a small `activation_metadata.json` into `input/` (so it is not
itself uploaded) and passes it via `--metadata-json`. The COG engine embeds
`ACTIVATION_EVENT` / `SOURCE` / `PROCESSOR` as GeoTIFF tags at creation
(`shared_utils.cog_metadata.load_metadata_json`); `PROCESSOR` auto-stamps from
`shared_utils.version`.

### CRS

Both CLIs default to `EPSG:4326`, but `run.sh` and the config default the
`dst_crs` positional to **`EPSG:3857`** because VEDA `build_stac` rejects 4326
COGs (see the CRS constraint in `CLAUDE.md`). Pass `EPSG:4326` or `native`
explicitly for browser-only previews.

## Test locally before registering

This reproduces the DPS working directory and runs the exact build + run scripts:

```bash
WORK=$(mktemp -d); cd "$WORK"; mkdir -p input
cp /path/to/LC09_..._02_T1.tar input/

bash /path/to/repo/dps/landsat/build-env.sh
bash /path/to/repo/dps/landsat/run.sh \
  "202512_Flood_WA" "true ndvi" "EPSG:3857" "Landsat 8/9 Collection 2 Level-2"

ls -la output/                                       # NON-EMPTY = copy step worked
conda run -n disasters_dps which process_landsat89   # console script on PATH
gdalinfo output/<one>.tif | grep -E 'ACTIVATION_EVENT|SOURCE|PROCESSOR'
```

## Register + submit

From a MAAP ADE workspace (maap-py installed):

```bash
python dps/register_algorithms.py            # landsat + sentinel2
```

`submitJob` example and the full recipe are in [`dps/README.md`](../dps/README.md).

## Gotchas baked into the scripts

- **setuptools-scm on a shallow clone.** If DPS clones with `--depth 1
  --no-tags`, the repo install can't resolve a version and fails (→ `process_*:
  command not found`). With `algorithm_version: dev` (a branch), setuptools-scm
  derives a dev version from the latest reachable tag — fine as long as tags are
  in the clone; the `SETUPTOOLS_SCM_PRETEND_VERSION` fallback in `build-env.sh`
  covers the tagless case. Pin a tag for reproducible production runs.
- **conda env-name drift.** `name: disasters_dps` in `environment.yml` must match
  every `conda run --name disasters_dps`. Single source of truth: the env file.
- **`docker_container_url`.** Must be a real MAAP base image with conda on PATH.
  Confirm the exact vanilla image URL/tag in the ADE registration UI before
  registering.
- **No p7zip.** Sentinel-2 unpacks `.zip` with Python `zipfile`; Landsat uses
  `tarfile`/`zipfile`. No system unzip binary is needed.

## Out of scope: SAR / vendor sensors (phase 2, blocked)

Capella, Umbra, and Satellogic do **not** take a file input. They fetch source
rasters from CSDA **vendor S3 buckets** keyed by `--date`/`--bucket`/`--prefix`,
using the ambient AWS credential chain (`shared_utils/s3utils.py`,
`shared_utils/s3_operations.py`) — there is no vendor-read assume-role. Running
them on DPS requires the **DPS worker IAM role** to be granted cross-account /
requester-pays read on those vendor buckets, which is not granted by default.

Resolve that platform question with MAAP/CSDA before scaffolding
`dps/{capella,umbra,satellogic}/`. If cross-account reads aren't permitted, the
alternative is a file-input variant: stage the vendor raster outside DPS and hand
it in as a `file` input, exactly like Landsat/Sentinel-2.
