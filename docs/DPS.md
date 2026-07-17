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
├── _validate.sh             # shared fail-fast input validators, sourced by every run.sh
├── _finalize.sh             # shared output handling, sourced by every run.sh
├── register_algorithms.py   # maap-py registration helper (legacy schema; see below)
├── README.md
└── <sensor>/                # landsat, sentinel2, capella, umbra, satellogic
    ├── build-env.sh         # conda env update + pip install repo (+ scm guard)
    ├── run.sh               # validate inputs -> run the CLI -> source _finalize.sh
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
  - toggles use `type: boolean` (renders a true/false control).
- **CRITICAL — mark every defaulted/optional input `optional` with the `?`
  type-suffix** (`boolean?`, `string?`, `int?`, `float?`). A `default:` alone does
  **not** make an input optional in the OGC process description — only the `?`
  suffix sets OGC `minOccurs: 0` → `optional: true`. The MAAP **Submit Job** form
  (`dps-jupyter-extension/SubmitJob.tsx`) does `const value = formInputs[key] ||
  null; if (!input.optional && value == null) errors[key] = "Valid value
  required."` — so **any input whose value is falsy (`false`, `""`, `0`) and is
  NOT `optional` blocks submission** with *"Valid value required."* That means a
  `boolean` toggle defaulting `false`, or a `string` with `default: ""`, jams the
  form unless its type carries `?`. The OGC description keeps `type` and `optional`
  as **separate** fields, so `boolean?` still renders as a real toggle (type stays
  `boolean`) — it just also loads as optional. **Rule of thumb: if it has a
  `default:`, give it a `?`.** Keep the `?` OFF the genuinely-required inputs
  (`file_path_of_raw_data`, SAR `date`, `source_label`, `activation_event`) so the
  form's required-asterisk still nudges operators to fill them.
- **Metadata fields** (`author`, `contributor`, `license`, `release_notes`,
  `citation`, `keywords`) pre-fill the registration form.

## How run.sh receives inputs (CWL contract)

DPS passes every input as a **named flag** `--name value` via `"$@"` — NOT
positional `$1 $2`. `File`/`Directory` inputs are localized to a path. Booleans
may arrive as a bare `--flag` (presence) or `--flag true|false` (value), so each
run.sh's boolean parser accepts both. Each boolean's **run.sh default MIRRORS its
`algorithm_config.yaml` default** (e.g. landsat `merge`/`mask`/`save_png`/
`delete_cog` default `true`; satellogic `use_mask`/`visualize` default `false`).
This way an input left at its form default round-trips correctly **whether or not**
MAAP re-emits the flag for a default-valued boolean — omitted → run.sh keeps the
config default; explicitly toggled → `--flag true|false` overrides. (Do NOT set the
run.sh defaults all to `false`: if MAAP omits default-`true` booleans, that would
silently invert `merge`/`mask`/`save_png`/`delete_cog`.)

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

## Finding available scenes / dates (`aws s3 ls`)

The SAR/vendor `--date` input must name a scene that actually exists in the vendor
bucket (the CLI picks the **closest** scene to the date you give). Discover valid
dates with the AWS CLI before submitting a job. **Access:** these are private CSDA
delivery buckets — you need AWS creds with read access (locally: `aws sso login` /
`aws configure`; on DPS: the worker role). If a listing returns `AccessDenied` only
because the bucket is requester-pays, append `--request-payer requester`.

**Vendor buckets** (prefix `disasters/` on every one; ✅ = wired as a pipeline in
this repo, the rest are reference/future):

| Vendor | Bucket URI | Pipeline |
|---|---|---|
| Capella (SAR) | `s3://csdap-capellaspace-delivery/disasters/` | ✅ capella |
| Umbra (SAR) | `s3://csda-data-vendor-umbra/disasters/` | ✅ umbra |
| Satellogic (optical) | `s3://csda-data-vendor-satellogic/disasters/` | ✅ satellogic |
| GHGSat | `s3://csdap-ghgsat-delivery/disasters/` | — |
| Airbus SAR | `s3://csdap-airbus-delivery/disasters/` | — |
| BlackSky | `s3://csdap-blacksky-delivery/disasters/` | — |
| SkySat | `s3://csdap-planet-skysat-delivery/disasters/` | — |
| Airbus optical | `s3://csda-data-vendor-airbus-optical/disasters/` | — |
| ICEYE | `s3://csdap-iceye-delivery/disasters/` | — |

**Option 1 — list the date "folders" (common prefixes)** — fast, and the folder
names are what you turn into `--date`:

```bash
aws s3 ls s3://csdap-capellaspace-delivery/disasters/
#   PRE 20231107120000_.../     <- each PRE line is a scene/date prefix
```

**Option 2 — list everything recursively** (add `--summarize` for a count/total,
`grep` to keep only rasters):

```bash
aws s3 ls s3://csda-data-vendor-umbra/disasters/ --recursive --human-readable --summarize
aws s3 ls s3://csda-data-vendor-umbra/disasters/ --recursive | grep -iE '\.tiff?$'
```

**Option 3 — nearest to today by UPLOAD time** (server-side sort by `LastModified`;
the last rows are the most recently delivered):

```bash
aws s3api list-objects-v2 --bucket csdap-capellaspace-delivery --prefix disasters/ \
  --query 'sort_by(Contents,&LastModified)[-10:].[LastModified,Key]' --output text
```

**Option 4 — nearest to today by ACQUISITION date in the key** (client-side; the
date is embedded in the folder name, so sort the prefixes and take the tail):

```bash
aws s3 ls s3://csdap-capellaspace-delivery/disasters/ | awk '{print $NF}' | sort | tail
```

Then format the chosen date for the sensor's `--date` (validated in run.sh):
Capella `YYYYMMDDHHMMSS` (e.g. `20231107120000`), Umbra & Satellogic
`'YYYY-MM-DD HH:MM:SS'` (e.g. `'2023-11-07 12:00:00'`).

### In-DPS discovery: `list_dates=true` (all 3 SAR/vendor sensors)

When you *don't* have local read creds to the vendor bucket (only the DPS worker
role does), you can discover dates **from a DPS job itself**. **Capella, Umbra, and
Satellogic** each expose `list_dates` as their **first** input (boolean, default
`false`). Submit the algorithm with `list_dates=true` and it runs a **report-only**
job: `run.sh` short-circuits *before* validation/staging (so
`date`/`activation_event`/`source_label` are NOT required), calls
`process_<sensor> --list_dates`, prints the report to the **job log**, and exits —
no COG, no upload. (Satellogic's report is **level-scoped**: it validates `level`
and lists only that level's scenes, so pick the `level` toggle before submitting.)
The report is an **aligned table**, one row per scene, **newest first by S3
delivery time** (`LastModified` — the top rows are the scenes most recently added
to the bucket, i.e. closest to today): columns are the `--date` value to copy,
acquisition time (UTC), S3-delivery time (UTC), and the **scene folder** name (so
you can see which scene each `--date` maps to). The same rows are also written to
**`output/available_<sensor>_dates.csv`**, which DPS uploads — open it from the
**Jobs** panel via **Outputs → Open in File Browser** and JupyterLab renders the
CSV as a **sortable grid** (cleaner than scrolling the raw `_stdout.txt` log). Copy
a `--date`, then submit again with `list_dates=false`.

Note: a DPS job is **headless/async** — it prints to the log and drops the CSV
artifact, but **cannot** auto-open a browser UI or push text into the MAAP DPS
extension panel (the extension only links to a job's output folder; it has no
inline text/HTML view). So there is no auto-popup either way — you still submit the
discovery job, wait, then open the log or the CSV.

Mechanics (the reusable pattern, wired for all three SAR sensors):
`shared_utils.s3utils.retrieve_s3_file_list_with_timestamps(bucket, prefix)`
returns `(key, LastModified)` pairs; each sensor's
`report_<sensor>_scenes()` (`capella_v2` / `umbra_v2` / `satellogic_v2`) groups by
scene folder — Capella `parts[1]`, Umbra `parts[2]`, Satellogic `parts[1]` filtered
by `level` — keeps the newest `LastModified` per scene, parses the acquisition date
from the folder name, and returns dicts (`date`/`scene`/`acquired`/`added_to_s3`)
sorted most-recent-delivered first; `process_<sensor> --list_dates` formats the
table and writes the CSV to `--output` (run.sh passes `--output output`). The
`date` field is pre-formatted to the sensor's own `--date` grammar (Capella
`YYYYMMDDHHMMSS`, Umbra/Satellogic `YYYY-MM-DD HH:MM:SS`) so it round-trips as
`--date` verbatim. For a truly interactive (auto-rendering) date-picker, run
`report_<sensor>_scenes()` in a live notebook kernel (needs local/hub S3 creds) and
render it with `ipywidgets`/`IPython.display` — that is the only path that appears
inline without a submit-and-wait round-trip.

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

## Guard rails: fail-fast input validation (`dps/_validate.sh`)

The OGC/CWL input schema is `{name,label,doc,type,default}` only — **no `enum`,
`pattern`, or `min/max`** — so the MAAP form cannot enforce valid values. `run.sh`
is therefore the ONLY place a bad arg can be caught. Every `run.sh` sources
**`dps/_validate.sh`** (a shared function library, mirroring `_finalize.sh`) and
calls the validators **right after flag parsing, before conda/staging/S3** — so a
bad job fails in ~0s with one clear, actionable message instead of failing late in
gdalwarp or, worse, "succeeding" with zero output.

Validators (in `dps/_validate.sh`) and what each run.sh enforces:

| Check | Rule | Applies to |
|---|---|---|
| `validate_activation_event` | reject placeholder; require `YYYYMM_Hazard_Location` (`^[0-9]{4}(0[1-9]\|1[0-2])_[^_]+_.+$`) | all |
| `require_nonempty source_label` | non-empty | all |
| `validate_dst_crs` | `native` or `EPSG:<code>` | all |
| `validate_int_range compression_level … 1 22` | integer 1–22 (ZSTD range) | all |
| `validate_number` (nodata / png_min / png_max / gamma / we_nstd) | numeric when set | all |
| `validate_granule` | file exists + `.tar`/`.zip` (Landsat) / `.zip` (S2), case-insensitive | optical |
| `validate_in_set products …` | token in the sensor's accepted set (the CLI's own check ends in `quit()` → exit 0, so bash catches it first) | optical |
| `validate_regex process_date/process_tile` | `YYYYMMDD`; path/row `NNNNNN` (Landsat) or MGRS `T\d\d[A-Z]{3}` (S2) | optical |
| `validate_regex date` | `YYYYMMDDHHMMSS` (Capella) / `YYYY-MM-DD HH:MM:SS` (Umbra, Satellogic) | SAR |
| `validate_in_set level "L1D L1B"` | valid processing level | Satellogic |
| `validate_int_range filter_size … 1 101` | integer window when `apply_filter` | Capella, Umbra |

`--product` on the SAR sensors is already enforced by argparse `choices=` in the
CLI, so bash does not re-check it. Assertions live in
`tests/integration/test_dps_validate.sh` (run in CI via the `.py` wrapper).

- `activation_event` default is the placeholder **`YYYYMM_Hazard_Location`**, which
  run.sh **rejects** — operators must set a real event (e.g. `202511_Flood_TX`).
- `source_label` is **required** (no default; the form marks it `*`).
- `dst_crs` defaults to **`native`** (no warp). EPSG:3857/4326 are per-job opts.
  (EPSG:3857 is NOT required for VEDA `build_stac`.)

### Not yet hardened (documented follow-ups)

- **Landsat & Satellogic exit 0 when a valid `date` matches no scene** (Sentinel-2
  correctly `sys.exit(1)`). Fixing needs a one-line Python change in each processor
  (raise / `sys.exit(1)` on an empty match) — out of scope for the bash guard layer.
- **Minimal-inputs trims (deferred):** Satellogic `bucket`/`prefix` are informational
  (CLI hardcodes them — no effect); Capella `product` is single-valued (`sigma`);
  Capella/Umbra `bucket`/`prefix` could be locked run.sh constants like `S3_BUCKET`.

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

## Deploying a code change (push → re-register)

A DPS algorithm code change goes live in **two steps, in this order**:

1. **Commit + push to the branch the algorithm tracks** (`origin/dev` for
   `algorithm_version: dev`). MAAP builds the algorithm container by
   **git-cloning the repo from GitHub** at build time — it does NOT see your
   local working tree, so uncommitted / unpushed changes are invisible to the
   build. This is the #1 *"I re-registered but nothing changed"* trap: the code
   simply wasn't on `origin/dev` yet.
2. **Re-register the algorithm** (Register Algorithm UI, same
   `algorithm_version`). Registration kicks off a fresh build that re-clones
   `origin/<branch>@HEAD` and re-runs `build-env.sh`, so it picks up whatever you
   just pushed. Confirm via the **Submit Jobs Process dropdown** (the real "is it
   deployed" check — more reliable than "My Builds", which only tracks
   UI-initiated builds).

**You do NOT rebuild the hub image for an algorithm code change** — two distinct
artifacts, don't conflate them:

- **Hub image** (`image/Dockerfile` + `image/environment.yml`) = the JupyterLab
  environment operators use; built by GitHub Actions on push to `main`/`dev`.
  Unrelated to what executes inside a DPS job.
- **DPS algorithm container** = built by MAAP from the repo branch via
  `dps/<sensor>/build-env.sh` when you register/build; this is what actually runs
  `process_<sensor>`. (`dps/` is `.dockerignore`d from the hub image precisely
  because DPS clones it from git directly — see "Gotchas" below.)

**`dev` is PR-protected.** GitHub branch protection requires changes to `dev` go
through a pull request; a direct `git push origin dev` is refused unless your
account can bypass the rule (the push log then shows
`Bypassed rule violations for refs/heads/dev`). Prefer opening a PR into `dev`
for anything non-trivial.

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
