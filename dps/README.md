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
├── _finalize.sh             # SHARED output flow: PNG -> output/ -> S3 -> delete COG
├── register_algorithms.py   # legacy maap-py registration helper (see "Registering")
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
5. **`source "${basedir}/../_finalize.sh"`** — the shared output flow: optional PNG
   quicklook → copy to `output/` (DPS uploads this, so the COG is never lost) →
   optional publish to `s3://nasa-disasters/drcs_activations_new/<event>/` → optional
   COG delete. Toggled by the `save_png` / `enable_s3_upload` / `delete_cog` inputs.
   The S3 destination is **locked per algorithm_version** (not a job input).

Two input archetypes: a **file-input** algorithm takes a `File` granule
(`--file_path_of_raw_data`, e.g. landsat/sentinel2); a **fetch** algorithm takes no
file and its CLI pulls source rasters from a bucket keyed by `--date` etc. (e.g. the
SAR sensors). A fetch algorithm needs the DPS-worker role to have read access to that
bucket — set the optional `READ_ROLE_ARN` (+ `READ_ROLE_EXTERNAL_ID`) env to assume a
read role when the ambient role lacks it.

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

`algorithm_version` in each manifest is the git ref DPS clones — `dev` tracks active
development; pin a tag (e.g. `v0.10.0`) for reproducible production runs. Either way the
clone must include git tags so setuptools-scm can resolve a version (`build-env.sh` has
a fallback if it can't).
