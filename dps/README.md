# MAAP DPS integration

This folder wires this repo's `process_*` CLIs into the MAAP
[Data Processing System (DPS)](https://docs.maap-project.org/en/latest/technical_tutorials/dps_tutorial/dps_tutorial_demo.html)
as **OGC/CWL algorithms**. Each algorithm is one subfolder that follows the same
three-file pattern; adding a new one means copying that pattern, not touching any
shared plumbing.

**[`docs/DPS.md`](../docs/DPS.md) is the full reference** for the non-obvious details
(the OGC/CWL schema quirks, the boolean default-mirroring rule, the locked S3
destination, MAAP Settings). This README is the working guide: what the pattern is
and how to add to it.

## The model

DPS clones this (public) git repo to `/app/<repo>/`, runs an algorithm's **build
script** once to create a conda env, then runs its **run script** per job. Registration
uses the OGC/CWL schema, so every input reaches `run.sh` as a **named flag**
(`--name value`, never positional `$1 $2`); `File` inputs are localized to a path.
The run script maps those flags onto a `process_*` CLI, writes products to
`~/drcs_outputs/<event>/`, then sources the shared output flow. Anything left in the
relative `output/` dir is uploaded to S3 by DPS.

## Layout

```
dps/
├── environment.yml          # SHARED lean conda env (name: disasters_dps)
├── _finalize.sh             # SHARED output flow: output/ -> S3 -> delete COG (no PNGs)
├── register_algorithms.py   # legacy maap-py registration helper (see "Registering")
├── delete_algorithm.ipynb   # undeploy a registered process (see "Deleting an algorithm")
├── README.md
└── <name>/                  # one subfolder per algorithm
    ├── build-env.sh         # conda env update + pip install repo (boilerplate)
    ├── run.sh               # parse --flags -> process_<name> CLI -> source _finalize.sh
    └── algorithm_config.yaml # OGC/CWL manifest (name, resources, inputs)
```

The three **shared** files (`environment.yml`, `_finalize.sh`, `register_algorithms.py`)
are used by every algorithm. Everything else lives in a per-algorithm `<name>/` dir.
The existing algorithms (`landsat`, `sentinel2`, `capella`, `umbra`, `satellogic`) are
concrete examples of the pattern — copy the closest one when adding a new algorithm.

**Two Sentinel-2 algorithms are registered on purpose.** `sentinel2/` downloads
`.SAFE` archives from Copernicus (needs `COP_USER`/`COP_PASS` MAAP secrets and
`p7zip`); `sentinel2_odr/` queries a STAC API and reads COGs straight from the AWS
Open Data Registry (ODR) bucket
(no credentials, no download). They carry DIFFERENT `algorithm_name`s so both
stay runnable and can be compared on real activations before the `.SAFE` one is
retired — a rename would replace rather than add. See issue #144 and
"Sentinel-2: TWO algorithms during the STAC migration" in [docs/DPS.md](../docs/DPS.md).

One algorithm deliberately deviates: **`list_dates/`** (registered as `list-dates`)
is a **discovery** tool, not a processing algorithm — it takes a `sensor` selector
(capella|umbra|satellogic) and runs its own `report_dates.py`, which calls each
sensor's `report_<sensor>_scenes()` helper to print available vendor-bucket scene
dates. Discovery lives ONLY here — the per-sensor `process_<sensor>` CLIs no longer
carry a `--list_dates` flag. It has **no `_finalize.sh` step** (no COG; only an
`available_<sensor>_dates.csv` artifact). See `docs/DPS.md` "Scene-date discovery".

A second deviates in a different way: **`blackmarble/`** (registered as `black-marble`)
wraps the **VEDA Black Marble** nighttime-lights pipeline, which is **not code in this
repo**. It is maintained upstream by NASA-IMPACT at
`github.com/NASA-IMPACT/veda-black-marble` and is **pip-installed into the DPS worker
env** by a `git+https://…` entry in `dps/environment.yml` (pinned to a commit SHA until
upstream tags a release). Everything under `dps/blackmarble/` is a thin wrapper that
follows the normal run.sh contract and the shared `_finalize.sh` output flow, but it
(a) is **bbox + date** driven (downloads VIIRS VNP46A2 from Earthdata, Landsat from a
STAC catalog, and OSM roads — no vendor-bucket file input), (b) calls upstream's own
**`blackmarble` console script** unmodified, and (c) writes its **own** COG (not via
`shared_utils.convert_to_cog`). Its NASA Earthdata token comes from a **MAAP secret** at
run time (default name `EARTHDATA_TOKEN`, via `dps/_get_secret.py`), never a job input.

> **Don't re-vendor it.** An earlier iteration copied the package into `src/blackmarble/`
> (~19 MB, from a personal fork). That was removed: Black Marble has its own repo and
> release path, and a local copy would silently drift from upstream.

See `docs/DPS.md` "Black Marble (VEDA nighttime lights)".

## The run.sh contract

Every `run.sh` has the same skeleton, whatever the CLI underneath:

1. **Defaults block** — one shell variable per input. **Boolean defaults MUST mirror
   the `algorithm_config.yaml` default** so an input left at its form default
   round-trips whether or not MAAP re-emits the flag (see `docs/DPS.md` for why).
2. **`while/case` flag parser** — one `--name)` arm per input. Boolean arms accept
   both a bare `--flag` (presence) and `--flag true|false` (value) via the
   `=~ ^(true|false)$` test.
3. **Guard rails** — reject the placeholder `activation_event`
   (`YYYYMM_Hazard_Location`) and require `source_label`; both must be real values.
4. **Build the CLI arg list** and run it under `conda run --name disasters_dps
   process_<name> ...`, writing products into `${OUT_HOME}` (`~/drcs_outputs/<event>/`).
5. **`source "${basedir}/../_finalize.sh"`** — the shared output flow: copy to
   `output/` (DPS uploads this, so the COG is never lost) → publish to
   `s3://nasa-disasters-staging/dps_output/<event>/` (always on, via short-lived MAAP
   workspace credentials) → scratch-COG delete (always on). No operator toggles and
   **no PNG quicklooks**: destination, publish, and scratch-delete are **locked per
   algorithm_version** (the `enable_s3_upload` / `delete_cog` / `save_png` inputs were
   all removed). See `docs/DPS.md` "All sensors → nasa-disasters-staging".

Two input archetypes: a **file-input** algorithm takes a `File` granule
(`--file_path_of_raw_data`, e.g. landsat/sentinel2); a **fetch** algorithm takes no
file and its CLI pulls source rasters from a bucket keyed by `--date` etc. (e.g. the
SAR sensors). A fetch algorithm needs the **DPS-worker role (`dps-verdi-role`) to have
direct read access** to that bucket — vendor reads use the worker's ambient
credentials (no role assumption). If the worker lacks read on a cross-account CSDA
bucket, the fix is an IAM grant on that role, not a repo change. See `docs/DPS.md`
"Vendor read access".

## Adding a new algorithm

Prereq: a `process_<name>` console script must already exist in this repo (see
[docs/ADDING_A_NEW_SENSOR.md](../docs/ADDING_A_NEW_SENSOR.md)). DPS only invokes that
CLI — it doesn't add processing logic.

1. **Create the dir:** `mkdir dps/<name>/`.
2. **`build-env.sh`** — copy any existing one verbatim. It's path-agnostic boilerplate
   (derives `repo_root` itself, `conda env update` against the shared `environment.yml`,
   `pip install` the repo, plus the setuptools-scm fallback). Only the header comment
   names the tool.
3. **`run.sh`** — copy the closest archetype (a file-input one like `landsat/` or a
   fetch one like `capella/`), then edit the four algorithm-specific spots: the
   **defaults block**, the **`case` arms**, the **CLI arg list**, and (fetch only) the
   required-input guard. Keep the shared guard rails and the final `source ../_finalize.sh`.
4. **`algorithm_config.yaml`** — the OGC/CWL manifest. Copy an existing one and change:
   - `algorithm_name` — must match `^[a-z0-9_-]+$` (lowercase, digits, `-`/`_`; no
     spaces/slashes).
   - `build_command` / `run_command` — **prefixed with the repo dir name**, e.g.
     `disasters-product-algorithms/dps/<name>/run.sh` (MAAP runs from `/app`).
   - `base_container_url` — the MAAP OPS base (`custom_images/maap_base:<tag>`); confirm
     the tag in the registration UI's Container URL dropdown. (It's `base_container_url`,
     not `docker_container_url`.)
   - resources — `ram_min` / `cores_min` / `outdir_max`.
   - `inputs` — a **flat** list of `{name, label, doc, type, default}`. Each input's
     `name` is exactly the `--flag` your `run.sh` parses. Valid `type`: `string, int,
     File, Directory, long, float, boolean, double` (no enum/array — multi-choice fields
     stay free-text strings; optional = `string` + `default: ""`). **A boolean's
     `default` here MUST equal its `run.sh` default** (step 3's mirroring rule).
   - `outputs` — keep `[{name: output, type: Directory}]`.
5. **Smoke-test locally** (next section), then **register** it.

That's it — no shared file changes. (Optionally add `<name>` to the `SENSORS` list in
`register_algorithms.py` if you use that legacy helper; the GUI path doesn't need it.)

## Test locally before registering

Reproduce the DPS working dir and run the scripts exactly as DPS will — **named
`--flag value` args**, not positional:

```bash
WORK=$(mktemp -d); cd "$WORK"; mkdir -p input
cp /path/to/granule.tar input/                        # file-input algorithms only

bash /path/to/repo/dps/<name>/build-env.sh            # creates the disasters_dps env
bash /path/to/repo/dps/<name>/run.sh \
  --activation_event 202512_Flood_WA --source_label USGS \
  --file_path_of_raw_data input/granule.tar           # omit for fetch algorithms; pass --date instead

ls -la "$HOME/drcs_outputs/202512_Flood_WA/" output/  # both must be NON-EMPTY
conda run -n disasters_dps which process_<name>       # console script on PATH
gdalinfo output/<one>.tif | grep -E 'ACTIVATION_EVENT|SOURCE|PROCESSOR'
```

Pass criteria: non-empty `output/` COGs, activation GeoTIFF tags present, and a real
`PROCESSOR` version (proves setuptools-scm resolved).

## Registering

**Register from the MAAP hub's Register Algorithm GUI** — it speaks the OGC/CWL schema
these `algorithm_config.yaml` files use. See the
[Registering section of docs/DPS.md](../docs/DPS.md#registering-from-the-maap-hub) for
the MAAP Settings config (`maapApiUrl` / `maapToken`).

`register_algorithms.py` uses maap-py's **legacy** schema and does **not** consume the
flat OGC config — it's kept for reference only; prefer the GUI.

## Deleting an algorithm

The GUI registers but does **not** delete. Undeploy with
[`delete_algorithm.ipynb`](delete_algorithm.ipynb) (kernel: `disasters_dps`), which
lists the OGC processes, dry-runs the selection, then
`DELETE /api/ogc/processes/<processID>`. You delete by the numeric **`processID`**, not
by `algorithm_name`, and only the deployer can delete their own process (`403`
otherwise). **Renaming an algorithm does not move it** — re-registering under a new
`algorithm_name` leaves the old process registered and runnable, so delete it here.
Details: [docs/DPS.md → Deleting (undeploying) an algorithm](../docs/DPS.md#deleting-undeploying-an-algorithm).

`algorithm_version` in each manifest is the git ref DPS clones — `dev` tracks active
development; pin a tag (e.g. `v0.10.0`) for reproducible production runs. Either way the
clone must include git tags so setuptools-scm can resolve a version (`build-env.sh` has
a fallback if it can't).
