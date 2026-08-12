# Running on MAAP DPS

All five sensor pipelines (landsat-8-9, capella, umbra, satellogic, and
disasters-sentinel2-process) are registerable as algorithms on the MAAP
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
├── delete_algorithm.ipynb   # undeploy a process via the OGC API (see "Deleting")
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
  `disk_space`. These are `_min` scheduling **floors** MAAP uses to pick a
  worker — **not runtime caps**, and there is **no `cores_max`/`ram_max`**. The
  processing code sets `NUM_THREADS=ALL_CPUS` (gdalwarp / rio-cogeo) and
  `os.cpu_count()` (rasterio reproject), so a job uses **every core on whatever
  worker it lands on**, regardless of `cores_min` — the declared value only
  biases instance selection, it doesn't throttle the process. `outdir_max` (GB)
  bounds the **aggregated** output: a job keeps *all* the COGs it produces
  (multi-product / multi-scene runs accumulate — `dps/_finalize.sh` globs every
  `**/*.tif`), and they must all fit under `outdir_max` before the
  always-on scratch-COG delete frees the home dir.
- **`inputs` is a flat list** of `{name, label, doc, type, default}`. Valid
  `type`: `string, int, File, Directory, long, float, boolean, double` — **no
  enum, no array**. So:
  - multi-choice fields (`product`, `level`) and `products` stay free-text
    strings (run.sh word-splits `products` into multiple `-p` values);
  - toggles use `type: boolean` (renders a true/false control).
- **Two registration paths disagree on the type vocabulary — this repo targets the
  GUI, so use BASE types only (no `?` suffix).** There are two conflicting MAAP
  constraints, confirmed in source:
  - The **Register Algorithm GUI** (`algorithms-jupyter-extension/src/constants.ts`)
    hard-codes the Type dropdown to exactly `string, int, File, Directory, long,
    float, boolean, double`. A `?`-suffixed type (`boolean?`, `string?`) is **not a
    dropdown option**, so importing a config that uses it leaves Type unselected and
    the form errors *"Please select an item in the list."* → **the GUI cannot
    register `?` types.**
  - The **Submit Job** form (`dps-jupyter-extension/src/components/SubmitJob/
    SubmitJob.tsx`) does `value = formInputs[key] || null; if (!input.optional &&
    value == null) → "Valid value required."` — so a falsy value (`false`, `""`,
    `0`) on a **non-`optional`** input blocks submission. OGC `optional` (=
    `minOccurs: 0`) is only set by the `?` suffix, which is only registrable via the
    **CLI** `ogc-app-pack-generator` path (`build_cwl_workflow.py` passes the type
    through verbatim — see its `type: string?` example CWL), **not** the GUI.
  - **Net:** with GUI registration you're limited to base types, and inputs whose
    default is falsy (`boolean: false`, `string: ""`) will hit *"Valid value
    required"* at Submit Job. To fully avoid that you must register via the CLI
    generator with `type: X?`. Don't put `?` in these configs while the GUI is the
    registration path — it breaks registration outright (the harder failure).
- **Metadata fields** (`author`, `contributor`, `license`, `release_notes`,
  `citation`, `keywords`) pre-fill the registration form.

## How run.sh receives inputs (CWL contract)

DPS passes every input as a **named flag** `--name value` via `"$@"` — NOT
positional `$1 $2`. `File`/`Directory` inputs are localized to a path. Booleans
may arrive as a bare `--flag` (presence) or `--flag true|false` (value), so each
run.sh's boolean parser accepts both. Each boolean's **run.sh default MIRRORS its
`algorithm_config.yaml` default** (e.g. landsat `merge`/`mask` default
`true`; satellogic `visualize` default `false` — its `use_mask` boolean was removed
in PR #45, masking is now hardcoded).
This way an input left at its form default round-trips correctly **whether or not**
MAAP re-emits the flag for a default-valued boolean — omitted → run.sh keeps the
config default; explicitly toggled → `--flag true|false` overrides. (Do NOT set the
run.sh defaults all to `false`: if MAAP omits default-`true` booleans, that would
silently invert `merge`/`mask`.) The publish + scratch-delete booleans
(`ENABLE_S3_UPLOAD`/`STAGING_UPLOAD`/`DELETE_COG`) are **locked internal constants**,
not job inputs — see "Output flow" below.

Per-sensor run.sh maps the flags onto the `process_<sensor>` CLI (note the CLIs
use mixed spelling: `--date/--product/--output` double-dash, `-dst_crs/-nodata/
-compression_level` single-dash) and then `source dps/_finalize.sh`.

## Four input models

- **File-input (landsat):** takes a **`file_path_of_raw_data`** File input (a
  `.tar`/`.zip` Collection-2 granule the operator downloaded from USGS). run.sh
  stages it into a dir, runs the CLI (which writes products to `<input>/output/`),
  copies those to `OUT_HOME`.
- **SAR/vendor (capella, umbra, satellogic):** **no file input** — the CLI
  fetches source rasters from a CSDA vendor bucket keyed by `--date`/`--bucket`/
  `--prefix` (capella `csdap-capellaspace-delivery`, umbra `csda-data-vendor-umbra`,
  satellogic `csda-data-vendor-satellogic`). Satellogic's bucket/prefix are
  hardcoded in the CLI and have **no job inputs at all** (removed in PR #45, along
  with `use_mask`/`dst_crs`/`compression_level`/`nodata`/`source_label`/`png_min`/`png_max`).
- **Download-from-Copernicus (sentinel2):** **no file input** — run.sh downloads
  L2A/L1C scenes from the Copernicus Data Space (CDSE) by MGRS `tile`(s) +
  `download_date` via the `download_sentinel2` CLI, then runs `process_sentinel2`
  on the downloaded dir. Copernicus credentials are read from **MAAP secrets** at
  run time (see "Sentinel-2 credentials via MAAP secrets" below) — never job inputs,
  so they never appear in the job parameters or log. **If a job downloads fewer
  scenes than you expect for the tile list**, check the per-tile search lines the
  download prints to the job log (`tile T17RLN → N product(s)` / `→ 0 products ⚠` /
  `→ search FAILED (…) ⚠`, plus a missing-tiles summary — added 2026-07-29). A
  `0 products` tile usually just had no acquisition on that `download_date`
  (Sentinel-2 revisit ≈ 5 days ⇒ a single date only catches that day's orbit
  swath — widen the date, or check `level` 1=L1C vs 2=L2A); a `search FAILED`
  line is a transient CDSE API error — re-run. There is **no per-file cap** (the
  `limit` input is the OData `$top` page size, default 50). See `.clinerules.md`
  rule 34.
- **Download-from-Earthdata/STAC/OSM (blackmarble):** **no file input** — run.sh
  takes a WGS84 `bbox` + `date` and the `blackmarble` pipeline downloads VIIRS
  VNP46A2 (NASA Earthdata), Landsat (STAC), and OSM roads, fusing them into an
  urban-focused COG. The NASA Earthdata token is read from **MAAP secrets** at run
  time (default name `EARTHDATA_TOKEN`) — never a job input. The pipeline is an
  **upstream package** (`NASA-IMPACT/veda-black-marble`, pip-installed into the DPS
  env), not a `process_*` sensor; see "Black Marble (VEDA nighttime lights)" below.

### Vendor read access

A DPS worker runs as **`dps-verdi-role`** (account `884094767067`). It reads the
CSDA vendor buckets with its **ambient credentials** — `s3utils._read_session()`
returns a plain `boto3.Session` (default credential chain), and the SAR / list-dates
run.sh no longer set any read-role env. So the **worker role must have direct read
access** (`s3:ListBucket` + `s3:GetObject`) on the cross-account CSDA buckets; if it
doesn't, `list-dates` / SAR fetch dies with `AccessDenied ... s3:ListBucket ...
csdap-*`, and the fix is an **IAM grant on `dps-verdi-role` (or the bucket policy)**,
not anything in this repo.

> **History (removed 2026-07-31).** An earlier approach (2026-07-20) had
> `dps/_env.sh` export `READ_ROLE_ARN=…:role/disasters-prod` and had
> `_read_session()` **assume** that role for every vendor read. That path **broke
> vendor reads** (on the hub the ambient identity already *is* `disasters-prod`, so
> the assume self-assumed and failed; on DPS it depended on an untestable manual
> trust-policy edit) and was **removed entirely** — `_env.sh` is deleted, the
> `READ_ROLE_ARN`/`sts:AssumeRole` branch is gone from `_read_session`, and the
> per-sensor run.sh no longer source `_env.sh`. There is no role assumption in the
> vendor-read path anymore. (The unrelated `aws_credentials.py` **upload** assume in
> `s3_operations.py` — used by notebook/hub uploads to `nasa-disasters` — is a
> separate mechanism and was left in place.)

The `_finalize.sh` **upload** path is likewise plain ambient `boto3` (writes to
`nasa-disasters`) / MAAP workspace creds (writes to `nasa-disasters-staging`) — it
never used the removed read-role.

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

### Scene-date discovery: the standalone `list-dates` algorithm

When you *don't* have local read creds to the vendor bucket (only the DPS worker
role does), you can discover dates **from a DPS job itself**. Discovery is the dedicated
**`list-dates`** algorithm (`dps/list_dates/`) — a **separate registered
algorithm**, not a per-sensor toggle. Pick a **`sensor`** input (capella | umbra |
satellogic) and Submit: `run.sh` **normalizes** the selector (trims whitespace and
folds case via `normalize_token`, so `Capella`/` umbra ` work) then **validates**
it — an unrecognized `sensor`/`level` **aborts in ~0s, before any S3 listing or
dispatch**, so the job never "runs" on bad criteria. On a clean selector it runs
the algorithm's own **`report_dates.py --sensor <sensor> --output output`**, a
**report-only** job — no COG, no upload. Discovery lives ONLY here now: the
per-sensor `process_<sensor>` CLIs no longer carry a `--list_dates` flag.
(Satellogic's report is **level-scoped**: `level` is
validated up front too, so set it before submitting; for capella/umbra a
deliberately-set `level` is ignored with a `NOTE` in the log.) If the DPS worker
lacks read access to the vendor bucket, the report path prints **one actionable
line** (via `shared_utils.s3utils.explain_s3_read_failure`: missing credentials /
access denied / no-such-bucket) and exits non-zero — no raw boto3 traceback; a
valid selector that simply returns nothing prints a "no scenes (read access OK)"
hint so an empty result isn't confused with a permissions problem.
The report is an **aligned table**, one row per scene, **newest first by S3
delivery time** (`LastModified` — the top rows are the scenes most recently added
to the bucket, i.e. closest to today): columns are the `--date` value to copy,
acquisition time (UTC), S3-delivery time (UTC), and the **scene folder** name (so
you can see which scene each `--date` maps to). The same rows are also written to
**`output/available_<sensor>_dates.csv`**, which DPS uploads — open it from the
**Jobs** panel via **Outputs → Open in File Browser** and JupyterLab renders the
CSV as a **sortable grid** (cleaner than scrolling the raw `_stdout.txt` log). Copy
a `--date`, then submit that sensor's processing algorithm with it. The
`list-dates` algorithm's only inputs are non-falsy strings (`sensor`/`level`), so
it registers + submits cleanly through the normal Register-Algorithm GUI — no
"Valid value required" trap (see the schema section above).

Note: a DPS job is **headless/async** — it prints to the log and drops the CSV
artifact, but **cannot** auto-open a browser UI or push text into the MAAP DPS
extension panel (the extension only links to a job's output folder; it has no
inline text/HTML view). So there is no auto-popup either way — you still submit the
discovery job, wait, then open the log or the CSV.

Mechanics (`dps/list_dates/report_dates.py` calls each sensor's
`report_<sensor>_scenes()` helper directly):
`shared_utils.s3utils.retrieve_s3_file_list_with_timestamps(bucket, prefix)`
returns `(key, LastModified)` pairs; each sensor's
`report_<sensor>_scenes()` (`capella_v2` / `umbra_v2` / `satellogic_v2`) groups by
scene folder — Capella `parts[1]`, Umbra `parts[2]`, Satellogic `parts[1]` filtered
by `level` — keeps the newest `LastModified` per scene, parses the acquisition date
from the folder name, and returns dicts (`date`/`scene`/`acquired`/`added_to_s3`)
sorted most-recent-delivered first; `report_dates.py` formats the
table and writes the CSV to `--output` (run.sh passes `--output output`). The
`date` field is pre-formatted to the sensor's own `--date` grammar (Capella
`YYYYMMDDHHMMSS`, Umbra/Satellogic `YYYY-MM-DD HH:MM:SS`) so it round-trips as
`--date` verbatim. For a truly interactive (auto-rendering) date-picker, run
`report_<sensor>_scenes()` in a live notebook kernel (needs local/hub S3 creds) and
render it with `ipywidgets`/`IPython.display` — that is the only path that appears
inline without a submit-and-wait round-trip.

## Output flow (dps/_finalize.sh)

Products → `~/drcs_outputs/<activation_event>/` →
**copied to `output/`** (DPS uploads this to the job's own bucket — the COG is never
lost) → **published to `s3://nasa-disasters-staging/dps_output/<event>/`** (always on,
via short-lived MAAP workspace credentials — see below) → **scratch COG deleted from
`~/drcs_outputs`** (always on; frees home-dir space — the `output/` copy is kept).
**No PNG quicklooks are produced** — the `save_png`/`png_min`/`png_max` inputs and the
`_finalize.sh` PNG step were removed. Publishing and the scratch-delete are **not**
operator inputs either — they're locked on (the old `enable_s3_upload` / `delete_cog`
toggles were removed: with a hard-coded destination, a per-run on/off switch was just
confusing).

`_finalize.sh` globs **every** `**/*.tif` under `~/drcs_outputs`, so a
job that produces many COGs (Satellogic multi-tile, Landsat/S2 multi-product,
Capella/Umbra multi-scene) keeps them all. The S3 key is the **OUT_HOME-relative
path** (`os.path.relpath`), not the bare basename — so same-named products in
different subdirs don't overwrite each other in the bucket (this nests optical
products under `<date>/<product>/` and Capella/Umbra multi-scene output under
`scene_1/`, `scene_2/`). All of it must fit under `outdir_max` before deletion.

The S3 destination (`STAGING_BUCKET=nasa-disasters-staging`,
`STAGING_DEST_BASE=dps_output`) is **locked per algorithm_version** — it is NOT a job
input and NOT parsed from a flag, so operators can't redirect output. To change the
target, publish a new `algorithm_version` with the constants edited at the top of each
`run.sh`.

### All sensors → `nasa-disasters-staging` via MAAP workspace credentials

The DPS worker's own role (`dps-verdi-role`) can write `nasa-disasters` but **not**
`nasa-disasters-staging` — an ambient `boto3` upload there `AccessDenied`s. MAAP
issues a job short-lived credentials for the org buckets its team was authorized on
via `maap.aws.workspace_bucket_credentials()` (docs: *Accessing bucket data*), and
the MAAP + Data Services group enabled write for `nasa-disasters-staging`
(disasters-portal#342). **Every** sensor `run.sh` (landsat, sentinel2, capella, umbra,
satellogic) therefore sets the same locked staging constants — there is no
`S3_BUCKET`/`S3_DEST_BASE` and no `enable_s3_upload` / `delete_cog` job input anymore:

```sh
ENABLE_S3_UPLOAD="true"                  # locked internal, not a job input/flag
STAGING_UPLOAD="true"
STAGING_BUCKET="nasa-disasters-staging"
STAGING_DEST_BASE="dps_output"          # @anayeaye's requested prefix
DELETE_COG="true"                        # locked: scratch COG always removed post-upload
```

With `STAGING_UPLOAD=true`, `_finalize.sh` step 3 routes the publish through
`shared_utils.staging_upload.upload_dir_to_staging(out_home, bucket, "dps_output/<event>")`
(the ambient `upload_file_to_s3` branch is retained only as a `${STAGING_UPLOAD:-false}`
fallback for a future non-staging sensor). That helper: requests the workspace
credentials, builds a boto3 session from `resp["credentials"]`, confirms
`nasa-disasters-staging` is present in `resp["authorized_s3_paths"]` with
`access == "read_write"` (**fails loud**, listing what *was* authorized, if the grant
is missing/read-only or the response shape is unexpected — never silently uploads
nothing), then uploads every `**/*.tif`+`**/*.png` under `~/drcs_outputs` keyed by its
OUT_HOME-relative path → `s3://nasa-disasters-staging/dps_output/<event>/<rel>`.

`maap-py` is a **DPS-only** dep (pinned in `dps/environment.yml`), so
`staging_upload.py` defers `from maap.maap import MAAP` into the function — importing
`shared_utils` never requires maap-py; only a live DPS job invokes it (auth is ambient
via the injected `MAAP_PGT`, same as `dps/_get_secret.py`). This is the fan-out of the
disasters-portal#342 POC to all five sensors.
This is the POC for disasters-portal#342 (acceptance criterion A); fan out to the
other sensors once a Sentinel-2 job confirms objects land in the staging bucket.

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
| `require_nonempty source_label` | non-empty | all except Satellogic¹, Sentinel-2³ |
| `validate_dst_crs` | `native` or `EPSG:<code>` | all except Satellogic¹, Sentinel-2³ |
| `validate_int_range compression_level … 1 22` | integer 1–22 (ZSTD range) | all except Satellogic¹, Sentinel-2³ |
| `validate_number` (nodata / gamma / we_nstd) | numeric when set | all (Sentinel-2: `we_nstd` only³) |
| `validate_granule` | file exists + `.tar`/`.zip` (Landsat) / `.zip` (S2), case-insensitive | optical |
| `validate_in_set products …` | token in the sensor's accepted set (the CLI's own check ends in `quit()` → exit 0, so bash catches it first) | optical |
| `validate_regex process_date/process_tile` | `YYYYMMDD`; path/row `NNNNNN` (Landsat) or MGRS `T\d\d[A-Z]{3}` (S2) | optical |
| `validate_regex date` | `YYYYMMDDHHMMSS` (Capella) / `YYYY-MM-DD HH:MM:SS` (Umbra, Satellogic) | SAR |
| `validate_in_set level "L1D L1B"` | valid processing level | Satellogic |
| `validate_int_range filter_size … 1 101` | integer window when `apply_filter` | Capella |
| `validate_in_set filter_size "3 5 7"` | odd window 3/5/7 (always-on Lee filter) | Satellogic¹, Umbra² |
| `normalize_token` + `validate_in_set sensor "capella umbra satellogic"` | case/space-tolerant selector; unknown value aborts before any S3 listing | list-dates |

`--product` on the SAR sensors is already enforced by argparse `choices=` in the
CLI, so bash does not re-check it. Assertions live in
`tests/integration/test_dps_validate.sh` (run in CI via the `.py` wrapper).

- `activation_event` default is the placeholder **`YYYYMM_Hazard_Location`**, which
  run.sh **rejects** — operators must set a real event (e.g. `202511_Flood_TX`).
- `source_label` is **required** (no default; the form marks it `*`) — **except
  Satellogic**, which hardcodes `csda` and dropped the input (PR #45), and
  **Sentinel-2**, which hardcodes `Copernicus` (see ³).
- `dst_crs` defaults to **`native`** (no warp). EPSG:3857/4326 are per-job opts.
  (EPSG:3857 is NOT required for VEDA `build_stac`.)
- **¹ Satellogic (PR #45) hardcodes `source_label=csda`, `dst_crs=native`, ZSTD/22
  compression, and per-product nodata (composites `0`, indices `-9999`)** — those
  inputs were removed, so their validators don't run for it. It adds `filter_size`
  (Lee filter on indices, `{3,5,7}`, default 5) and accepts a comma-separated
  `--date` (multi-date).
- **² Umbra (PR #44)** made speckle filtering **always-on** — folded into the
  `sigma`/`beta`/`gamma` calib functions, with the standalone `apply_filter` and the
  DPS `apply_filter` boolean input both removed — and restricted `filter_size` to
  `{3,5,7}` (default 5). It also **dropped the RCS product** (`sigma`/`beta`/`gamma`
  only). The COG now carries raw dB (the old per-product percentile stretch is gone);
  apply any display stretch downstream at the visualization layer (VEDA/leafmap). Both **Capella & Umbra** default
  `-nodata` to **-9999.0** (float32 dB backscatter — 0 dB is a legitimate value, so
  nodata is never 0); leave the DPS `nodata` input blank to use it.
- **³ Sentinel-2 (2026-08-11)** hardcodes `source_label=Copernicus` (a
  download-from-CDSE job has no other origin), `dst_crs=native`, ZSTD
  `compression_level=9`, and `limit=50`; all four inputs were removed, so their
  validators no longer run for it. `limit` is `download_sentinel2`'s OData `$top`
  — a **per-tile page size, not a total cap**, and the loop doesn't follow
  `@odata.nextLink` — so 50 is a ceiling no realistic tile+date query reaches, and
  exposing it only invited operators to "fix" a low scene count with the wrong
  knob (the real cause is the ~5-day revisit or a wrong `level`). **`nodata` was removed WITHOUT a replacement constant** — the
  CLI's `-nodata` applies ONE value to EVERY product, but S2 output is mixed-dtype:
  uint8 color composites (fill `0`) alongside float32 indices, where
  `dump_geotiff_float` writes **`-9999.0`** and `0` is a legitimate NDVI/NDWI value.
  The old `nodata` input defaulted to `0`, which mis-declared nodata on every index
  product. Omitting `-nodata` lets `cog_utils.set_nodata_value` auto-detect per
  dtype (uint8 → `0`, float → `-9999.0`) and get both right. **Do not reintroduce a
  blanket `-nodata` on Sentinel-2.** `we_nstd` survives as an input (a real
  water-extent algorithm knob) and now defaults to `1` rather than blank — a blank
  string is falsy and trips the Submit Job form's *"Valid value required"* check on
  the GUI-registered path.

### Not yet hardened (documented follow-ups)

- **Landsat & Satellogic exit 0 when a valid `date` matches no scene** (Sentinel-2
  correctly `sys.exit(1)`). Fixing needs a one-line Python change in each processor
  (raise / `sys.exit(1)` on an empty match) — out of scope for the bash guard layer.
- **Minimal-inputs trims:** Satellogic (PR #45) is **done** — dropped `bucket`/`prefix`
  plus `use_mask`/`dst_crs`/`compression_level`/`nodata`/`png_min`/`png_max`/`source_label`
  (all hardcoded) and added `filter_size` {3,5,7}. **Still deferred:** Capella `product`
  is single-valued (`sigma`); Capella/Umbra `bucket`/`prefix` could be locked run.sh
  constants like `STAGING_BUCKET`/`STAGING_DEST_BASE`.

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

## Registering via the OGC / GitHub Action path (the only way to get OPTIONAL inputs)

The Register Algorithm GUI can only emit **required** inputs (base types, no `?`),
so any falsy default (`boolean: false`, `string: ""`) hits *"Valid value required"*
at Submit Job (see "The OGC/CWL registration schema" above). To register inputs as
**optional** — so the Submit form never blocks — register via MAAP's
**`ogc-app-pack-generator`**, driven by **`.github/workflows/register-dps.yml`**
(manual `workflow_dispatch`).

**How it works:** pick an **`algorithm`** (dropdown → `dps/ogc/<name>.yml`), an
**`algorithm_version`** (default `dev`), and **`register_to_maap`** (checkbox;
unchecked = safe dry-run). Via `dockerfile-path: dps/Dockerfile` the action
**builds the DPS worker image in-workflow and pushes it to ghcr.io**, generates +
validates the CWL (injecting that image), and — if checked — registers it to the
prod OGC processes API. `run-name` shows which algorithm/version is registering.

Note the Action reads **`dps/ogc/<name>.yml`** — *not* `dps/<sensor>/algorithm_config.yaml`.
Editing only the latter changes nothing about what this path registers.

### ⚠️ Register ONE algorithm at a time (a queued second run is silently cancelled)

`register-dps.yml` and `sync-deploy-algorithm.yml` share
`concurrency.group: sync-dev-to-deploy-algorithm` with `cancel-in-progress: false`,
so they never overlap. The trap: GitHub keeps at most **one pending run per group**,
and whatever queues *next* **evicts the run already waiting**. Two registrations
dispatched back-to-back — or one dispatched while a merge into `dev` is imminent —
means the earlier queued one dies. Observed 2026-08-11:

| time | event |
|---|---|
| 17:35:20 | `blackmarble` dispatched → parks at the approval gate, holding the group |
| 17:35:29 | `umbra` dispatched → queues as the single *pending* run |
| 17:35:36 | PR #91 merged into `dev` |
| 17:35:39 | `Sync dev → deploy-algorithm` created → takes the pending slot, **cancels `umbra`** |

The workflow header warns about the *opposite* hazard (a sync push making the
register's CWL push non-fast-forward). Eviction is the one that actually bites.
**Dispatch → wait for `completed` → dispatch the next.**

**A cancelled run does not fail `gh run watch --exit-status`** — it exits **0**, so a
watcher reports success for a registration that never happened. Confirm with
`gh run view <id> --json conclusion` (want `success`, not `cancelled`), and confirm
the registration itself from the `{"title": "<name>", …, "status": "accepted"}` JSON
the deploy step prints.

### `deploy-algorithm` stays one file ahead of `dev` — don't merge it back

Every register run commits the generated
`cwl_workflows/process_<repo>_<branch>.cwl` to the branch it ran on. That filename is
**branch-derived** and its content is **overwritten on every run**, so it is a build
artifact that belongs only on `deploy-algorithm` — `git ls-tree origin/dev` has no
`cwl_workflows/` at all, deliberately. A `deploy-algorithm → dev` PR therefore
contributes nothing but that artifact; close it. (PR #90 was opened and closed for
exactly this.) Land real changes on `dev` and let the sync carry them the other way.

**The `dps/ogc/<name>.yml` configs** mirror `dps/<sensor>/algorithm_config.yaml`
but mark inputs **`type: X?`** (OGC-optional → `minOccurs:0`), carry **no
container field** (the Action supplies the built image), and use an **absolute
in-image `run_command`** (`/app/disasters-product-algorithms/dps/<sensor>/run.sh`).
Registered names are `<sensor>-ogc-test` (except the three below), so they coexist
with the GUI-registered `capella`/`landsat-8-9`/etc. The dropdown covers **capella,
umbra, satellogic, list_dates, landsat, sentinel2**.

**Exceptions — three algorithms use `disasters-<name>-process`, not `<name>-ogc-test`**
(2026-08-11). For each, `dps/ogc/<name>.yml` and `dps/<name>/algorithm_config.yaml`
carry the **same** canonical name, so either registration path targets the one
process (the GUI path still can't express optional inputs — prefer the Action):

| configs | registered as |
|---|---|
| `dps/ogc/sentinel2.yml` + `dps/sentinel2/algorithm_config.yaml` | `disasters-sentinel2-process` |
| `dps/ogc/blackmarble.yml` + `dps/blackmarble/algorithm_config.yaml` | `disasters-blackmarble-process` |
| `dps/ogc/umbra.yml` + `dps/umbra/algorithm_config.yaml` | `disasters-umbra-process` |

⚠️ **A rename registers a NEW process; it does not rename the old one.** The old
`sentinel-2` was deleted by hand before its rename, but Black Marble's and Umbra's
predecessors (GUI-registered `black-marble` / `umbra`, and OGC-registered
`black-marble-ogc-test` / `umbra-ogc-test`) are **still live** unless someone
deletes them in MAAP. They point at the same `run.sh`, so a job submitted against
either still runs — delete them so operators can't pick a stale entry out of the
Process dropdown. How: [Deleting (undeploying) an
algorithm](#deleting-undeploying-an-algorithm) / `dps/delete_algorithm.ipynb`.

**`landsat` is the one file-input OGC config** — its granule `file_path_of_raw_data`
is a **required** `File` (no `?`, `minOccurs:1`); nothing can run without a granule,
so the Submit form should require one, while every *other* input stays optional so
no falsy default trips *"Valid value required"*. (`type: File` validated cleanly in
the OGC path — the landsat dry-run passed.) The SAR/discovery configs **and
`sentinel2`** have no File input, so they mark literally every input optional.
`sentinel2` downloads its scenes from Copernicus (see the download-from-Copernicus
model above) rather than taking a granule.

### Sentinel-2 credentials via MAAP secrets

Sentinel-2 downloads from CDSE, which needs a Copernicus username + password. These
are **never job inputs** (that would store the password in MAAP's job parameters and
log). Instead they live in **MAAP's encrypted secret store** and are fetched at run
time. The whole mechanism:

- **Store once** (from any MAAP notebook — the ADE or the Disasters hub):
  ```python
  from maap.maap import MAAP
  maap = MAAP()
  maap.secrets.add_secret("COP_USER", "you@example.com")
  maap.secrets.add_secret("COP_PASS", "your-copernicus-password")
  ```
  Use a **dedicated CDSE account** for the team, not a personal login. Register at
  <https://dataspace.copernicus.eu/>.
- **run.sh reads them at run time** via `dps/_get_secret.py`
  (`maap.secrets.get_secret("COP_USER"/"COP_PASS")`), exports them as env vars, and
  passes them to `download_sentinel2` **through the environment, not `-u/-p`** — so
  the password never lands in argv/`ps`/the job log. Auth is ambient: the DPS wrapper
  injects a proxy ticket (`MAAP_PGT`) that maap-py sends automatically, so no token
  handling is needed. `maap-py` is in `dps/environment.yml` for this.
- **Fixed secret names:** run.sh reads the secrets by the hardcoded names `COP_USER`
  / `COP_PASS` (not job inputs — nothing credential-related appears on the Submit
  form). Store them under exactly those names. To use different names, edit
  `COP_USER_SECRET` / `COP_PASS_SECRET` at the top of `dps/sentinel2/run.sh`.
- If a secret is missing/unreadable, run.sh **fails fast** with a message telling you
  to store it with `add_secret`.

**Non-obvious operational rules — each costs a failed or confusing run if missed:**

- **Run it from the UNPROTECTED `deploy-algorithm` branch.** The generator commits
  the generated CWL and pushes it to the run branch; `dev`/`main` are protected and
  reject the bot's push (`GH006: Protected branch update failed`) *after* the image
  is already built. `deploy-algorithm` is protected against **deletion only**
  (admins bypass), so normal pushes still work.
- **The ghcr image must be PUBLIC** — MAAP's ADES pulls it anonymously at job time.
  New ghcr packages default private, and the org may **disable public packages**
  outright (org Settings → Packages must allow public first, then the package →
  Change visibility → Public). This gates SUBMITTING A JOB, not registration.
- **Registration is ASYNC.** A successful run returns `status: "accepted"` + a
  `deploymentJobID` + a `deploy-ogc-hysds` pipeline link; the process appears in the
  **Submit Jobs** dropdown only after that pipeline finishes (a few min). The
  `maap-dps-jupyter-extension` DOES list OGC-registered processes (confirmed —
  `cardamom-ogc-process` and our six show up there).
- **ONE-BEHIND DEPLOY — FIXED via our pinned fork of the generator.** Upstream's
  deploy step registered the CWL at the **checkout** commit (`github.sha`) = the
  *previous* run's CWL (the filename is branch-based, `process_<repo>_<branch>.cwl`,
  overwritten each run), so registering **N algorithms needed N+1 runs** and a
  **single** one needed **two**. `register-dps.yml` now pins
  **`Disasters-Learning-Portal/ogc-app-pack-generator@<sha>`**, which deploys at the
  just-pushed `HEAD` (`git rev-parse HEAD`) — so a **single run registers its own
  CWL**. Re-pin the SHA when pulling upstream changes into the fork.
- **FORK PATCH #2 — node24 action pins.** The fork also bumps the generator's four
  JavaScript actions to their node24 majors (`actions/checkout@v7`,
  `actions/setup-python@v7`, `docker/login-action@v4`,
  `docker/build-push-action@v7`). Upstream is on v4/v5/v3/v5, all of which target
  **Node 20** — [deprecated on GitHub-hosted
  runners](https://github.blog/changelog/2025-09-19-deprecation-of-node-20-on-github-actions-runners/),
  so every register run ended with *"Node.js 20 is deprecated. The following actions
  target Node.js 20 but are being forced to run on Node.js 24: …"* and would break
  outright once the shim is removed. Both patches are marked `# FORK PATCH #N` in the
  fork's `action.yml`, and the fork's monthly Jules reconcile is told to preserve
  both. Retiring the fork (below) gives the node20 pins back unless upstream has
  bumped too — the retire job's prompt says so.
- **The fork is SELF-RETIRING (two monthly Jules jobs, one per repo).** Jules opens
  PRs in the repo it's invoked from, so the automation is split:
  - the **fork's** `.github/workflows/check-upstream.yml` (monthly) reconciles the
    fork via Jules **only while upstream is still buggy** (it greps upstream's deploy
    step for `github.sha`); if upstream is fixed it no-ops.
  - **this repo's** `.github/workflows/retire-fork-if-upstream-fixed.yml` (monthly,
    **on `main`** — scheduled/dispatch workflows only run from the *default* branch)
    detects that upstream fixed the bug **and** that `register-dps.yml` still pins the
    fork, then opens a Jules PR **to `dev`** switching `uses:` back to
    `MAAP-Project/ogc-app-pack-generator@<sha>` and updating these docs.
  Jules mechanics (both jobs): `google-labs-code/jules-action@v1.0.0` + repo secret
  `JULES_API_KEY` + the Jules GitHub app installed on the repo. Its PRs are **async**
  (a session is created; the PR lands minutes later — the action exposes no
  session-URL output). **Jules honors `starting_branch` as the PR base** (verified:
  `starting_branch: dev` → PR base `dev`). Canonical bug write-up (also fileable as an
  upstream issue): the fork's `ONE_BEHIND_BUG.md`.
- **Admin-approval gate:** the job sets `environment: maap-registration` when
  `register_to_maap` is checked. Create that **GitHub Environment with Required
  reviewers** (Settings → Environments) to actually gate live registrations —
  until it exists *with reviewers*, GitHub auto-creates it unprotected and runs
  ungated. **Managing approvers** (max 6; any **one** approval releases a run —
  it's OR, not unanimous): Settings → Environments → `maap-registration` → Required
  reviewers, or via API —
  `gh api -X PUT repos/<owner>/<repo>/environments/maap-registration` with a
  `reviewers` array. Footgun: the PUT **REPLACES the whole reviewer list**, so
  include every existing reviewer's *numeric user id* plus the new one
  (`{"type":"User","id":<id>}`; get an id via `gh api users/<login> --jq .id`), and
  re-send `wait_timer`/`prevent_self_review`/`deployment_branch_policy` to avoid
  resetting them. A reviewer **must have ≥ write access** to the repo. The env's
  **`can_admins_bypass` defaults `true`** — repo admins can deploy without waiting
  for approval; set it `false` in the same PUT to make approval mandatory even for
  admins. Current approvers: kyle-lesinger, gwlayne, alex-melancon, acblackford.
- **`gh workflow run` validates inputs against the target `--ref`'s** workflow file
  (not only the default branch), so a new input (e.g. `algorithm`) must be pushed to
  the branch you run from before dispatching.

Prereq: repo secret **`MAAP_PGT`** (only used when `register_to_maap` is checked).
This path uses pure GitHub Actions + the MAAP OGC API — no `maap-py`, no hub UI.

## Deleting (undeploying) an algorithm

**The Register Algorithm GUI can register but not delete.** Undeploying is a
`DELETE` against the OGC processes API. The ready-made notebook is
[`dps/delete_algorithm.ipynb`](../dps/delete_algorithm.ipynb) — list → filter →
dry-run → delete → verify. Run it on the MAAP hub / ADE with the **`disasters_dps`**
kernel (the env `build-env.sh` creates, which pins `maap-py`).

```python
import requests
from maap.maap import MAAP

# NOTE the /api suffix — this is NOT the extensions' `maapApiUrl` (which takes none)
BASE = "https://api.maap-project.org/api/ogc/processes"
headers = MAAP()._get_api_header()

procs = requests.get(BASE, headers=headers).json()["processes"]
for p in procs:                       # find the processID — you cannot delete by name
    print(p["processID"], f'{p["id"]}:{p["version"]}', p["deployedBy"])

r = requests.delete(f"{BASE}/34", headers=headers)
print(r.status_code, r.text)          # 200 {"detail": "Deleted process"}
```

The parts that bite:

- **You delete by `processID`** — an int MAAP assigns at registration — **not by
  `algorithm_name`**. Always list first; a name in the URL just returns `404`.
- **Response codes:** `200` undeployed · `403` you are not the deployer · `404` no
  such `processID`.
- **Only the deployer can delete their own process.** A `403` on something you
  believe is yours usually means the token belongs to a different MAAP account than
  the one that registered it (registered from the ADE, deleting from the Disasters
  hub, or vice versa) — check the `deployedBy` field.
- **`processID` is per name+version.** Deleting `foo:dev` leaves `foo:v1.2.0` alone.
- **The base URL has an `/api` suffix here** (`.../api/ogc/processes`), unlike the
  `maapApiUrl` JupyterLab setting, which must have **none** (the extensions append
  it themselves). Getting this backwards returns HTML → `Unexpected token '<'`.
- **Delete removes the registration only.** Running jobs are not cancelled, job
  history stays, outputs already in `nasa-disasters-staging/dps_output/<event>/`
  stay, and the built container image stays. Re-registering the same name afterwards
  gets a **new** `processID`.
- Confirm the result in the **Submit Jobs → Process dropdown** — same "is it really
  deployed" check as registration.

**Why this comes up: a rename is not a move.** Changing `algorithm_name` in
`dps/<sensor>/algorithm_config.yaml` / `dps/ogc/<sensor>.yml` and re-registering
creates a **second** process — the old name is left registered and still runs the
code its container was built with. That is exactly what the `<sensor>-ogc-test` →
`disasters-<sensor>-process` consolidation produces, so each rename leaves up to two
stale entries (the GUI-registered bare name, e.g. `umbra`, and the Action-registered
`umbra-ogc-test`) to clean up here. The old GUI-registered `sentinel-2` was deleted
this way on 2026-08-11.

## Black Marble (VEDA nighttime lights)

`disasters-blackmarble-process` (`dps/blackmarble/`, OGC descriptor
`dps/ogc/blackmarble.yml`) is the one processing algorithm that is **not** a
`process_*` sensor — and the one whose processing code **does not live in this repo
at all**.

The **VEDA Black Marble** pipeline is maintained upstream by NASA-IMPACT at
<https://github.com/NASA-IMPACT/veda-black-marble>. Given a WGS84 `bbox` + `date` it
downloads VIIRS VNP46A2 nighttime lights (NASA Earthdata), Landsat scenes (STAC), and
OSM roads, and fuses them into an urban-focused Cloud Optimized GeoTIFF
(inferno-colormap RGB). Everything under `dps/blackmarble/` is a **thin wrapper**:
validate the inputs → fetch the Earthdata token from a MAAP secret → run upstream's CLI
unmodified → publish the output COG to S3 via the shared `_finalize.sh`.

### How it's installed

`dps/environment.yml` pip-installs the package straight from its repo:

```yaml
  - pip:
    - git+https://github.com/NASA-IMPACT/veda-black-marble@20e7d782a6c826d19db73e35d501a17a25609e56
```

- **Pinned to a commit SHA** because upstream has **no tagged release yet**. When
  NASA-IMPACT cuts one, change this to `@vX.Y.Z` — or to a plain `blackmarble==X.Y.Z`
  if it reaches PyPI/conda. That is the whole upgrade: one line.
- **A VCS URL is required.** The bare name `blackmarble` on PyPI is an *unrelated*
  World Bank package — never depend on it by name.
- **Do not vendor the package back into this repo.** An earlier iteration copied it to
  `src/blackmarble/` (~19 MB, 18 MB of which was the WRS2 shapefile) from a personal
  fork. It was removed because Black Marble has its own repo and release path, and a
  private copy would silently drift from upstream while making Disasters responsible for
  maintaining it.

### Version floors are load-bearing

`pip` only reinstalls a dependency it considers **unsatisfied** — and if it decided
conda's `rasterio`/`geopandas` were too old it would install PyPI wheels over them,
breaking the GDAL dylib match that the whole stack depends on (see CLAUDE.md "Critical
Constraints"). To prevent that, the conda deps in `dps/environment.yml` carry `>=` floors
**mirroring upstream's declared minimums**: `rasterio>=1.4.3`, `geopandas>=1.1.1`,
`shapely>=2.1.0`, `numpy>=2`, `scipy>=1.15.3`, `matplotlib-base>=3.10.3`, `boto3>=1.38`,
`tqdm>=4.67.1`, `duckdb>=1.0.0`. Keep them in sync when bumping the pin.

Verify after any bump:

```bash
conda list -n disasters_dps | grep -E 'rasterio|geopandas|gdal|numpy|shapely|scipy'
```

Every row must show channel **conda-forge**. A `pypi` row means a floor is too low and
pip replaced a conda build — raise that floor.

blackmarble's **pip-only** deps (`earthaccess`, `osmnx`, `pystac-client`, `typer`,
`obstore`) are no longer hand-listed; pip resolves them from upstream's own metadata.
The `dps/Dockerfile` build-time smoke (`import blackmarble` + `blackmarble --help`) is
the only gate proving the VCS install and its deps resolved — including that the WRS2
shapefile (`blackmarble/data/WRS2_descending/*`, loaded via a plain `Path(__file__)…`
lookup) came along; upstream ships it via its `[tool.hatch.build] artifacts`.

### Other ways it deviates from the sensor pattern

- **Called through upstream's own console script**, `blackmarble` (a single-command typer
  app — no subcommand), with the flags exactly as its README documents them.
- **Writes its own COG** — it does **not** route through `shared_utils.convert_to_cog`,
  so there's no `-dst_crs`/`-compression`/`-nodata`/`--metadata-json` knob.
  `activation_event` is used only for the S3 output path (`dps_output/<event>/`); the
  pipeline embeds its own GeoTIFF metadata.
- `build-env.sh` still runs `pip install "${repo_root}"` even though no Black Marble code
  lives here — `_finalize.sh` imports `shared_utils.staging_upload` to publish the COG.

**Earthdata token via MAAP secrets** — exactly the Sentinel-2 credential mechanism.
The NASA Earthdata token is **never a job input** (it would land in the job log). Store
it once from any MAAP notebook, then run.sh reads it at run time via
`dps/_get_secret.py` and exports `EARTHDATA_TOKEN` (which the pipeline reads from the
environment):

```python
from maap.maap import MAAP
MAAP().secrets.add_secret("EARTHDATA_TOKEN", "<your-earthdata-token>")
```

Get a token at <https://urs.earthdata.nasa.gov/> (Profile → Generate Token). The
`earthdata_secret_name` input only names *which* secret to read (default
`EARTHDATA_TOKEN`; names are not sensitive) — unlike Sentinel-2's hardcoded
`COP_USER`/`COP_PASS`, blackmarble exposes the secret name as an input so an operator
can point at a differently-named secret without a re-register.

**Inputs** (all optional in the OGC descriptor; run.sh validates after parsing):
`bbox` (WGS84, lat span ≥ 0.05° — enforced by `validate_bbox` in `dps/_validate.sh`),
`activation_event`, `date` (YYYY-MM-DD), `config` (`fast`|`default`|`high_quality`),
`osm_source` (`overpass`|`layercake`), `wgs84` (also emit an EPSG:4326 COG), `basename`
(output filename stem), `earthdata_secret_name`. Output publishes to
`nasa-disasters-staging/dps_output/<activation_event>/` via the shared `_finalize.sh`
(same locked staging path as every other algorithm).

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
- **maap-py client versions (updated 2026-07-21).** The **hub image** no longer pins
  maap-py — it inherits the OGC-compliant client from the `2i2c/pangeo:v6.0.0` base
  (re-pinning `4.2.0` here would DOWNGRADE it; see `image/CHANGELOG.md`). The **DPS
  worker** (`dps/environment.yml`) is plain miniforge, so it does NOT inherit maap-py
  and must pin it — now **`maap-py==5.1.0`**, the OGC-compliant client (MAAP team
  guidance: "use 5.1.0 for OGC"). This is safe: the v5 "breaking" changes are in the
  *legacy* client usages (`register_algorithms.py` `register_algorithm_from_yaml_file`,
  the `submitJob`/`getJobStatus` README snippet), NOT the secrets API — `from maap.maap
  import MAAP` + `MAAP().secrets.get_secret()` are unchanged in 5.1.0, so
  `dps/_get_secret.py` needs no edit. The GUI registration extensions were already on
  OGC endpoints.
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
- **Sentinel-2 "empty success" = missing JP2 driver.** A green Sentinel-2 job that
  downloads + unzips scenes but produces **zero** COGs (its output folder has only
  logs/JSON, no `.tif`) is almost always the OpenJPEG driver. S2 bands are JPEG2000
  (`.jp2`); conda-forge GDAL 3.9+ ships the JP2 driver as a **separate plugin**
  (`libgdal-jp2openjpeg`) that a bare `gdal` install doesn't pull in. Without it every
  band read fails `gdal_JP2OpenJPEG.so is not available`, `process_sentinel2` catches
  it per-product, and you get `✗ ERROR processing … .jp2 not recognized` → all products
  skipped → 0 files uploaded. It's pinned in `dps/environment.yml`. The hub image gets
  JP2 for free from the MAAP base, so this only ever bites the DPS worker env. Confirm
  from a worker/job log: `grep -i jp2 _stderr.txt`.
