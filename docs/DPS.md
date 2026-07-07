# Running on MAAP DPS

All five sensor pipelines (landsat-8-9, sentinel-2, capella, umbra, satellogic)
are registerable as algorithms on the MAAP
[Data Processing System (DPS)](https://docs.maap-project.org/en/latest/technical_tutorials/dps_tutorial/dps_tutorial_demo.html).
The plumbing lives in [`dps/`](../dps/). This page is the source of truth for the
**non-obvious** parts; the per-sensor files are self-documenting otherwise.

## Layout

```
dps/
├── environment.yml          # lean conda env (name: disasters_dps) + matplotlib-base
├── _finalize.sh             # shared output handling, sourced by every run.sh
├── register_algorithms.py   # maap-py registration helper (legacy schema; see below)
├── README.md
└── <sensor>/                # landsat, sentinel2, capella, umbra, satellogic
    ├── build-env.sh         # conda env update + pip install repo (+ scm guard)
    ├── run.sh               # parses --name flags, runs the CLI, sources _finalize.sh
    └── algorithm_config.yaml
```

## The OGC/CWL registration schema (this is the part that bites)

MAAP migrated registration to OGC Application Packages / CWL. The current schema
differs from the old maap-py `register_algorithm_from_yaml_file` format in ways
that each cost a failed registration if you get them wrong:

- **`algorithm_name` must match `^[a-z0-9_-]+$`** — lowercase letters, digits,
  hyphens, underscores only. No capitals, spaces, or slashes. (So "Landsat 8/9"
  is invalid; we use `landsat-8-9`.)
- **`base_container_url`**, NOT `docker_container_url`. Value =
  `mas.maap-project.org/root/maap-workspaces/custom_images/maap_base:v4.2.0`
  (the OPS default; confirm the tag in the registration UI's Container URL
  dropdown). MAAP installs Miniconda during the build and runs `build_command` on
  top, so a minimal base is fine.
- **`build_command` / `run_command` MUST be prefixed with the repo directory
  name** — e.g. `disasters-product-algorithms/dps/landsat/build-env.sh`. MAAP
  clones the repo to `/app/<repo-name>/` and runs the command from `/app`.
- **`code_repository`**, not `repository_url`.
- **Resources: `ram_min` / `cores_min` / `outdir_max`**, not `queue` /
  `disk_space`.
- **`inputs` is a flat list** of `{name, label, doc, type, default}`. Valid
  `type`: `string, int, File, Directory, long, float, boolean, double` — **no
  enum, no array**. So:
  - multi-choice fields (`product`, `level`) and `products` stay free-text
    strings (run.sh word-splits `products` into multiple `-p` values);
  - optional inputs use `type: string` with `default: ""` (the `string?` optional
    suffix is NOT a dropdown option and loads blank);
  - toggles use `type: boolean` (renders a true/false control).
- **Metadata fields** (`author`, `contributor`, `license`, `release_notes`,
  `citation`, `keywords`) pre-fill the registration form.

## How run.sh receives inputs (CWL contract)

DPS passes every input as a **named flag** `--name value` via `"$@"` — NOT
positional `$1 $2`. `File`/`Directory` inputs are localized to a path. Booleans
may arrive as a bare `--flag` (presence) or `--flag true|false` (value), so each
run.sh's boolean parser accepts both and **defaults booleans to false** (presence
sets true) so a config default of `true` round-trips correctly.

Per-sensor run.sh maps the flags onto the `process_<sensor>` CLI (note the CLIs
use mixed spelling: `--date/--product/--output` double-dash, `-dst_crs/-nodata/
-compression_level` single-dash) and then `source dps/_finalize.sh`.

## Two input models

- **Optical (landsat, sentinel2):** take a **`file_path_of_raw_data`** File input
  (a `.tar`/`.zip` granule). run.sh stages it into a dir, runs the CLI (which
  writes products to `<input>/output/`), copies those to `OUT_HOME`.
- **SAR/vendor (capella, umbra, satellogic):** **no file input** — the CLI
  fetches source rasters from a CSDA vendor bucket keyed by `--date`/`--bucket`/
  `--prefix` (capella `csdap-capellaspace-delivery`, umbra `csda-data-vendor-umbra`,
  satellogic `csda-data-vendor-satellogic`). The **DPS-worker IAM role must have
  read access** to that bucket (you can't set that role from this repo or the
  hub — it's MAAP infra; request it from MAAP, or grant cross-account on the
  bucket). Optional code lever: set **`READ_ROLE_ARN`** (+ `READ_ROLE_EXTERNAL_ID`)
  so `shared_utils.s3utils` assumes a role with vendor access (see s3utils
  `_read_session`). Satellogic's bucket/prefix are hardcoded in the CLI (the
  inputs are informational only).

## Output flow (dps/_finalize.sh)

Products → `~/drcs_outputs/<activation_event>/` → optional PNG quicklook →
**copied to `output/`** (DPS uploads this — the COG is never lost) → optional
publish to `s3://nasa-disasters/drcs_activations_new/<event>/` → **COG deleted
from `~/drcs_outputs`** (default; frees home-dir space — the PNG and the `output/`
copy are kept). Controlled by inputs `save_png` (default true), `png_min`/
`png_max` (blank = auto 2–98 pct, or 0–255 for uint8), `enable_s3_upload`,
`delete_cog` (default true). PNGs come from
`shared_utils.plotting.save_cog_png` (needs `matplotlib-base`, in the DPS env).

The S3 destination (`S3_BUCKET=nasa-disasters`, `S3_DEST_BASE=drcs_activations_new`)
is **locked per algorithm_version** — it is intentionally NOT a job input and NOT
parsed from a flag, so operators can't redirect output. To change the target,
publish a new `algorithm_version` with the two constants edited at the top of each
`run.sh`. Only `enable_s3_upload` (the on/off toggle) is operator-facing.

## Guard rails in run.sh

- `activation_event` default is the placeholder **`YYYYMM_Hazard_Location`**, which
  run.sh **rejects** — operators must set a real event (e.g. `202511_Flood_TX`).
- `source_label` is **required** (no default; the form marks it `*`).
- `dst_crs` defaults to **`native`** (no warp). EPSG:3857/4326 are per-job opts.
  (EPSG:3857 is NOT required for VEDA `build_stac`.)

## Registering (from the MAAP hub)

Registration needs MAAP auth. Two realities:

1. **Use the Register Algorithm UI** (Algorithms → Register Algorithm in the MAAP
   JupyterLab). It's the OGC/CWL path that this schema targets. The CLI helper
   `dps/register_algorithms.py` uses maap-py 4.2.0's **legacy** path (old
   `config/file/positional` schema) — it does NOT consume the new flat schema, so
   prefer the UI (or upgrade to maap-py ≥4.3 CWL deploy).
2. **The GUI extensions need config**, set in Settings → **MAAP Settings**:
   - `maapApiUrl` = `https://api.maap-project.org` (NO `/api` suffix — the
     extensions append `api/ogc/...` themselves; a wrong base returns HTML →
     "Unexpected token '<' … not valid JSON").
   - `maapToken` = your `MAAP_PGT` (a per-user, expiring JWT). It is read from the
     SettingRegistry, so Settings Editor is sufficient (no server env var needed
     outside the MAAP ADE).

The three MAAP JupyterLab extensions (`maap-algorithms-jupyter-extension`,
`maap-dps-jupyter-extension`, `maap-jupyter-server-extension`) and `maap-py` are
baked into the hub image via `image/environment.yml` so they're available on the
Disasters hub. They surface as **Launcher tiles** ("MAAP Plugins"), not menu-bar
tabs (the menu-bar version is the archived JL2 `maap-jupyter-ide`).

`algorithm_version: dev` makes DPS clone the `dev` branch; pin a tag for
reproducible production runs.

## Platform v6.0.0 / OGC assessment (2026-07-07) — no repo change needed

MAAP announced platform **v6.0.0** (OGC-compliant API, new Algorithm Catalog,
`maap-py` v5.0.0+, refreshed R/Isce3/Pangeo/TF/PyTorch workspace images) and
"recommended" reregistering existing algorithms. Verified this does **not**
require any code change here — our stack is already OGC-native:

- **Our `algorithm_config.yaml` files are already the OGC/CWL schema** (see the
  section above). No schema migration. Reregistering is a **runtime GUI action**
  (re-run Register Algorithm so it shows in the v6 OGC catalog), not a repo edit.
- **Do NOT blind-bump `maap-py==4.2.0`** (pinned in `image/environment.yml`).
  v5.0.0+ is snakecase + OGC-only — a breaking change that would break the two
  *legacy, non-primary* client usages: `register_algorithms.py`
  (`register_algorithm_from_yaml_file`) and the `submitJob`/`getJobStatus`
  snippet in `dps/README.md`. The real registration path is the GUI extensions
  (already on OGC endpoints), so 4.2.0 stays until MAAP ops forces a client bump.
- **`base_container_url: maap_base:v4.2.0`** is NOT deprecated by the announcement
  and only supplies Miniconda (build-env.sh installs on top). Only action: confirm
  the tag still resolves in the UI's Container URL dropdown; bump if MAAP retires it.
- **The workspace image updates are irrelevant** — those are MAAP's own hub images.
  Our operator hub image is built *in this repo* (`image/Dockerfile`) on a Pangeo
  base we control, independent of MAAP's workspaces.
- **The pinned JL extensions already target OGC** (`api/ogc/...`). Bumping them
  would only add usability improvements — optional, not required.

Bottom line: existing jobs keep running; the only *someday* actions are non-code
(GUI reregister, possible base-tag bump).

## One build per repo

MAAP builds ONE container per repository+branch
(`container-disasters-product-algorithms:dev`) with all `process_*` CLIs in the
`disasters_dps` env; every algorithm reuses it (differing only by `run_command`).
"My Builds" shows the repo name, not the per-product algorithm name — expected.
CLI-registered algorithms may not appear in "My Builds" at all (it tracks
UI-initiated builds); the **Process dropdown** in Submit Jobs is the real "is it
deployed" check.

## Local smoke test

```bash
WORK=$(mktemp -d); cd "$WORK"; mkdir -p input
cp /path/to/granule.tar input/          # optical only
bash <repo>/dps/landsat/build-env.sh
bash <repo>/dps/landsat/run.sh \
  --file_path_of_raw_data input/granule.tar \
  --activation_event 202511_Flood_TX --source_label USGS --products "true ndvi"
ls -la "$HOME/drcs_outputs/202511_Flood_TX/" output/
```

## Gotchas
- **setuptools-scm on a shallow clone**: `build-env.sh` exports a
  `SETUPTOOLS_SCM_PRETEND_VERSION` fallback when no tag is reachable, or `pip
  install` of the repo fails → `process_*: command not found`.
- **conda env name** `disasters_dps` must match across `environment.yml`,
  `build-env.sh`, and every `conda run --name` in run.sh / `_finalize.sh`.
- **`dps/` is excluded from the hub image** (`.dockerignore`); DPS clones git
  directly, so that's fine.
