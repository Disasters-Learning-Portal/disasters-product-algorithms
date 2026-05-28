# Automation guide

Canonical reference for the CI / lint / dependency-management surface in
`disasters-product-algorithms`. Read this before adding a new sensor,
adding a new conda dep, or wondering why a hub pod is missing a CLI.

## Overview

What's automated today:

| Concern | Tool | Source |
|---|---|---|
| pyproject ↔ sensor-dir consistency | `tools/check_sensor_consistency.py` | Runs locally + in CI + as pre-commit hook |
| Notebook cell-0 conformance (per-sensor) | `tools/check_sensor_consistency.py` | Runs locally + in CI + as pre-commit hook |
| Pre-commit enforcement of the consistency lint | `.pre-commit-config.yaml` (local hook) | Local, opt-in via `pre-commit install` |
| New sensor scaffolding (one command) | `tools/new_sensor.py` | Local — operator-invoked |
| CLI importability after `pip install .` | `.github/workflows/lint.yml` `cli-smoke` job (import-test + `--help`) | CI only |
| Cross-repo image rebuild on algorithms push | `.github/workflows/trigger-docker-rebuild.yml` (single consolidated workflow) | Push to `dev` / `main` |
| Conda-dep sync to hub image | `.github/workflows/sync-conda-deps.yml` | Push to `main` touching `hub-conda-deps.txt` |
| PR shape conventions (target branch, test plan, checklist) | `.github/PULL_REQUEST_TEMPLATE.md` + `.github/RULESETS.md` (one-time setup doc) | Auto-populated on every PR |

What's **not** yet automated (see [Roadmap](#roadmap)):

- (none currently; Rec 3 — pre-commit + import-smoke — shipped.)

The bug that motivated most of this guide: the [Capella `ModuleNotFoundError` rollout](#capella-modulenotfounderror-cdd1c23--be6693c).

---

## CI lint (`.github/workflows/lint.yml`)

Triggers on every `push` and `pull_request` to `dev` or `main`. Two independent jobs:

### Job 1: `sensor-consistency`

Runs `python tools/check_sensor_consistency.py`. The script walks every
top-level directory in the repo, identifies "sensor directories" (those
containing both `cli.py` and at least one `process_*.py`), and asserts
three invariants per sensor:

1. **Package discovery includes the sensor**: `"<sensor>*"` appears in
   `pyproject.toml [tool.setuptools.packages.find].include`. Missing this
   is what broke the initial capella rollout — `pip install` silently
   skipped the package while the `process_capella` console-script shim
   still made it into `bin/`.
2. **Console script registered**: for every `<sensor>/process_<verb>.py` (and
   `<sensor>/download_<verb>.py`), there's a matching `[project.scripts]`
   entry. Sensors with multiple entrypoints (sentinel2 has both
   `process_sentinel2` and `download_sentinel2`) are handled correctly.
3. **Script target shape**: the entry value matches `<sensor>.cli:<verb>_cli`,
   the canonical shape used by every existing sensor.

Failure output is actionable. Example after manually removing `"capella*"`:

```
Sensor consistency check FAILED:

  1. capella/ exists (has cli.py + process_*.py) but `capella*` is not in
     [tool.setuptools.packages.find].include. pip install would skip the
     package — the console script will crash with ModuleNotFoundError.
     Add `"capella*"` to pyproject.toml.

Found 5 sensor dir(s): capella, landsat, satellogic, sentinel2, umbra
```

Exit code 0 on pass, 1 on any failure. Stdlib + `tomllib` only — no install
required to run.

#### Running locally

```bash
python tools/check_sensor_consistency.py
# OK: 5 sensor(s) consistent with pyproject.toml:
#   - capella/, landsat/, satellogic/, sentinel2/, umbra/
```

Under one second. Run it before pushing if you've touched a sensor
directory, `[project.scripts]`, or `[tool.setuptools.packages.find].include`.

### Job 2: `cli-smoke`

Catches a different bug class: when the consistency check passes (every
pyproject entry looks right) but the sensor still fails to import because
of a missing transitive dependency, broken `__init__.py`, or wrong-symbol
import. The `sensor-consistency` job is a static check on file contents;
`cli-smoke` is a runtime check.

Steps:

1. Set up miniconda (conda-forge, Python 3.12) via `conda-incubator/setup-miniconda@v3`.
2. **Install conda deps from `dev-conda-deps.txt`** (single source of truth):
   ```bash
   deps=$(grep -v '^\s*#' dev-conda-deps.txt | grep -v '^\s*$' | tr '\n' ' ')
   mamba install -y -c conda-forge $deps
   ```
3. `pip install .` (no `--no-deps`, so `[project.dependencies]` resolve
   against the conda env).
4. **Import-test every sensor package** (added in Rec 3):
   ```bash
   # Sensor dirs discovered the same way tools/check_sensor_consistency.py
   # does — top-level dirs with cli.py + at least one process_*.py.
   sensors=$(python -c "...sensor discovery...")
   for sensor in $sensors; do
       python -c "import $sensor" || failed=1
   done
   ```
   Catches the bug class where `<sensor>/__init__.py` star-imports a symbol
   that doesn't exist in `<sensor>_v2.py`, or pulls in a transitively
   missing dep — but `--help` would short-circuit through argparse before
   ever loading the broken module. A bare `import` forces the full
   module-load path.
5. **Iterate `pyproject.toml [project.scripts]` and run `--help` on each**:
   ```bash
   scripts=$(python -c "import tomllib, pathlib;
                        p = tomllib.loads(pathlib.Path('pyproject.toml').read_text());
                        print('\n'.join(p['project']['scripts']))")
   for script in $scripts; do
       $script --help > /dev/null || exit 1
   done
   ```

Neither list is hardcoded — sensor dirs come from disk-scan, scripts come
from pyproject. Adding a new sensor (via `tools/new_sensor.py`) gets both
loops exercised automatically. Combined with `sensor-consistency`, this
means a green CI = your CLIs at least import cleanly in a fresh env that
matches the hub image's dep stack.

#### When `cli-smoke` fails

| Symptom | Likely cause |
|---|---|
| `ModuleNotFoundError: No module named '<sensor>'` | `<sensor>*` missing from `packages.find.include`. (sensor-consistency should have caught this first.) |
| `ModuleNotFoundError: No module named '<dep>'` | Missing line in `dev-conda-deps.txt` (or `[project.dependencies]` for pip wheels). |
| `ImportError: cannot import name 'X' from '<sensor>.<sensor>_v2'` | `__init__.py` star-imports a symbol that doesn't exist in `_v2`. |
| `--help` succeeds but exits non-zero | Argparse misconfiguration; rare. |

---

## Dependency source-of-truth files

Three files, three audiences, three lifecycles. The semantic distinction is
real and the contributor decision tree is small enough to be manageable —
but it's brittle, so it's worth being explicit.

### Three files

| File | Audience | Format | Lifecycle |
|---|---|---|---|
| `pyproject.toml [project.dependencies]` | `pip install .` transitive | pip spec (e.g. `"Pillow"`, `"requests>=2.30"`) | Per-PR |
| `dev-conda-deps.txt` | Local dev + CI smoke | one conda spec per line (e.g. `gdal`, `rasterio>=1.3`) | Per-PR |
| `hub-conda-deps.txt` | Hub image conda deps ON TOP of Pangeo base | one conda spec per line | Per-PR + auto-sync to image repo |

### What goes where — decision tree

```
Need to add a new Python dependency?
│
├─ Does it have a manylinux wheel? (pip install <pkg> works on a clean Linux box)
│   └─ YES → pyproject.toml [project.dependencies]. Done.
│
└─ Conda-only (GDAL plugin, native lib, pinning required):
    │
    ├─ Is it already in the Pangeo base image?
    │  (gdal, rasterio, rio-cogeo, geopandas, pyproj, numpy, scipy, boto3, etc.)
    │   └─ YES → dev-conda-deps.txt only. Hub already has it.
    │
    └─ NOT in Pangeo base:
        └─ BOTH dev-conda-deps.txt AND hub-conda-deps.txt.
           The sync-conda-deps workflow handles the image-repo side.
```

### `hub-conda-deps.txt` auto-sync

The `.github/workflows/sync-conda-deps.yml` workflow:

1. Fires on **push to `main`** that touches `hub-conda-deps.txt`.
2. Clones `pangeo-notebook-veda-image` (using `PANGEO_REBUILD_TOKEN`).
3. Rewrites the managed block in that repo's `environment.yml` with the
   current contents of `hub-conda-deps.txt`.
4. Opens a PR in `pangeo-notebook-veda-image` via `peter-evans/create-pull-request`.
5. Reviewer merges. Next image build picks up the new conda dep.

No manual editing of `pangeo-notebook-veda-image` needed. The PR title and
body are auto-generated and reference the algorithms-repo commit.

### Verifying dep state locally

```bash
# What dev-conda-deps.txt actually resolves to:
grep -v '^\s*#' dev-conda-deps.txt | grep -v '^\s*$'

# What pyproject [project.dependencies] resolves to:
python -c "import tomllib, pathlib;
           print('\n'.join(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['dependencies']))"

# Re-create the dev env from scratch:
mamba create -n disasters-dev python=3.12 -y
mamba activate disasters-dev
mamba install -y -c conda-forge $(grep -v '^\s*#' dev-conda-deps.txt | grep -v '^\s*$' | tr '\n' ' ')
pip install -e .
```

---

## Cross-repo image-rebuild dispatch

### Today's workflow (consolidated — Rec 2 shipped)

A **single** workflow handles both branches:

- `.github/workflows/trigger-docker-rebuild.yml` — `on: push: branches: [main, dev]`.
  Picks the right token and `event_type` based on `github.ref`:
  ```yaml
  env:
    PANGEO_TOKEN: ${{ github.ref == 'refs/heads/main' && secrets.PANGEO_REBUILD_TOKEN || secrets.PANGEO_REBUILD_TOKEN_DEV }}
    EVENT_TYPE: ${{ github.ref == 'refs/heads/main' && 'algorithm-updated' || 'algorithm-updated-dev' }}
  ```
  Both secrets remain required: `PANGEO_REBUILD_TOKEN` for prod (`main`),
  `PANGEO_REBUILD_TOKEN_DEV` for dev (`dev`). The payload includes the
  pushed commit SHA, ref, repository, and branch name. Dispatch contract
  is preserved — image-repo listens for the same two event types as before.

The image-repo workflows (`build-and-push.yaml` / `build-and-push-dev.yaml`)
listen for those dispatch events. Each resolves `ALGORITHMS_REF` from the
dispatch payload's commit SHA (or falls back to live branch HEAD if invoked
on its own), then runs `docker build --build-arg ALGORITHMS_REF=<sha> .`.

### Two-layer Dockerfile (image repo)

The image-repo Dockerfile is split into two RUN layers:

- **Layer 1** (slow, ~2-3 min): `pangeo/pangeo-notebook` base + `environment.yml`
  updates. Cached on the content of `environment.yml`, which is itself
  derived from `hub-conda-deps.txt` via the sync workflow.
- **Layer 2** (fast, ~30s): `pip install --force-reinstall --no-deps git+https://...@<ALGORITHMS_REF>`.
  Cached per algorithms SHA.

Why two layers: algorithm-only changes (the common case) invalidate only
the small pip layer, not the heavy conda update. This is the design that
shipped after the per-variant pinning refactor in `a9cf2ea`.

### Image variants on Docker Hub

| Variant | Tag pattern | Tracks |
|---|---|---|
| Prod | `klesinger/disasters-jupyterhub-docker-image:latest` (+ `:<sha-12>` per commit) | Algorithms `main` HEAD |
| Dev | `klesinger/disasters-jupyterhub-docker-image-dev:latest` (+ `:<sha-12>`) | Algorithms `dev` HEAD |

A stale `disasters-jupyterhub-docker-image-testmerge` repo may still exist
on Docker Hub from earlier experiments. The build pipeline no longer
produces it; safe to delete via the Docker Hub UI.

---

## Branch protection (Rec 2 shipped — native config replaces bash workflows)

The two `enforce-*.yml` workflows that used to live here
(`enforce-dev-to-main.yml`, `enforce-branch-protection.yml`, ~117 lines of
bash) have been **deleted**. Their job is now done by:

- **`.github/PULL_REQUEST_TEMPLATE.md`** — auto-populates every PR with a
  structured body: target-branch reminder (`feature/* → dev → main`),
  summary, test plan, and a contributor checklist that includes the
  `tools/check_sensor_consistency.py` reminder and the conda-deps
  decision tree. Catches the same shape conventions as the deleted
  warn-only bash checks, but at PR-write time instead of post-open.
- **GitHub native branch protection rules** (configured in the repo
  Settings UI, not in code). The required configuration is documented in
  `.github/RULESETS.md` — including the equivalent `gh api` and Terraform
  payloads for future automation.

Why the change: bash-in-CI couldn't be bypassed for emergencies, ran
post-PR-open (after the contributor had already done the work), and the
"warn-only" portions provided no signal that wasn't reproducible with a PR
template. See `.github/RULESETS.md` for the full migration rationale.

---

## What `tools/check_sensor_consistency.py` does

Quick reference (full source is ~115 lines, stdlib + `tomllib` only):

- **Sensor-directory detection** (lines 32-45): top-level dirs (sorted,
  non-hidden) containing both `cli.py` and at least one `process_*.py`.
  Skips `.`-prefixed and `_`-prefixed dirs.
- **Per-sensor validation** (lines 76-108):
  - `<sensor>*` glob in `[tool.setuptools.packages.find].include`.
  - For every `process_*.py` and `download_*.py` in the sensor dir, expect
    a matching console script in `[project.scripts]` with target shape
    `<sensor>.cli:<verb>_cli`.
- **Output**: stderr-listed failures with remediation suggestions; stdout
  "OK" with sensor count on success.
- **Exit codes**: 0 OK, 1 failure.

Adding new "verb prefixes" (today: `process_`, `download_`) is a one-line
constant change at line ~50.

---

## Roadmap

These items are planned but not yet shipped. They came out of an
independent simplicity audit; same end-state, more granular planning.

### Rec 1 — `tools/new_sensor.py` scaffolder — SHIPPED

One command replaces ~8 manual edits:

```
$ python tools/new_sensor.py spire
  Created spire/__init__.py
  Created spire/cli.py
  Created spire/process_spire.py
  Created spire/spire_v2.py
  Updated pyproject.toml: added 'spire*' to [tool.setuptools.packages.find].include
  Updated pyproject.toml: added process_spire = "spire.cli:process_spire_cli" to [project.scripts]
  Created notebooks/spire_workflow.ipynb
  Created notebooks/testing-notebooks/spire_workflow.ipynb

Running tools/check_sensor_consistency.py ...
OK: 6 sensor(s) consistent with pyproject.toml:
  - capella/, landsat/, satellogic/, sentinel2/, spire/, umbra/

Next steps:
  1. Edit spire/spire_v2.py — implement retrieve_spire_resources(), sigmaCalib(), apply_filter().
  2. Add conda deps to dev-conda-deps.txt if needed.
  3. git add -A && git commit -m "feat(spire): scaffold new sensor"
  4. Open PR: feat/spire -> dev
```

Implementation:

- `tools/new_sensor.py` (~280 lines, stdlib + `tomlkit` for comment-preserving
  pyproject mutation, `nbformat` for direct `.ipynb` writing).
- `tools/_templates/sensor/{__init__.py.tmpl, cli.py.tmpl, process_name.py.tmpl, name_v2.py.tmpl}` —
  rendered via `string.Template` (`${name}` / `${Name}` / `${NAME}` / `${bucket}`).
- `tools/_templates/notebooks/{workflow.py.tmpl, testing_workflow.py.tmpl}` —
  jupytext py:percent source. Parsed into `nbformat` cells directly (no
  jupytext runtime dep, but `jupytext` is in `dev-conda-deps.txt` for
  contributors who want to round-trip the templates).
- `tools/check_sensor_consistency.py` extended with notebook conformance:
  asserts both `notebooks/<sensor>_workflow.ipynb` and the testing variant
  exist, the sensor name appears in cell 0, and no other sensor name
  appears in cell 0 (catches the Capella "Sentinel-2"/"Umbra" copy-paste
  leftover — see [Post-mortems](#post-mortems)).

Error paths covered (all exit 1 with no partial writes):

- Sensor directory already exists.
- `process_<sensor>` or `<sensor>*` already in `pyproject.toml`.
- Invalid sensor name (non-identifier, hyphen, leading digit, leading
  underscore, uppercase).
- Reserved repo dir (`tools`, `shared_utils`, `notebooks`, `docs`, `tests`,
  `raster_tools`, etc.).
- Post-condition `check_sensor_consistency.py` fails — the scaffolder
  rolls back the four sensor files, both notebooks, and the two
  `pyproject.toml` mutations.

Bugs 1, 2, and 3 are now **structurally impossible** rather than just CI-caught.

### Rec 2 — Consolidate governance workflows — SHIPPED

Two simplifications, both landed:

- **Trigger workflows consolidated.** `trigger-docker-rebuild.yml` and
  `trigger-docker-rebuild-dev.yml` collapsed into a single workflow keyed
  on `github.ref`. The dispatch contract is unchanged — image-repo still
  receives `algorithm-updated` (prod) and `algorithm-updated-dev` (dev)
  events with the same payload shape. See
  [Cross-repo image-rebuild dispatch](#cross-repo-image-rebuild-dispatch).
- **Bash-based branch governance removed.** `enforce-dev-to-main.yml` and
  `enforce-branch-protection.yml` (~117 lines of bash) deleted. Replaced
  by `.github/PULL_REQUEST_TEMPLATE.md` for shape conventions and
  `.github/RULESETS.md` documenting the GitHub-native branch protection
  rules the repo owner must configure in Settings → Branches. The
  `RULESETS.md` doc includes equivalent `gh api` and Terraform payloads
  for future automation, plus an optional Actions-based hard-enforcement
  fallback if the honor-system approach proves insufficient.

### Rec 3 — Pre-commit hook + import smoke — SHIPPED

Two small additions, both landed:

- **`.pre-commit-config.yaml`** at the repo root with one local hook that
  runs `python tools/check_sensor_consistency.py` at `git commit` time.
  `language: system` + `always_run: true` + `pass_filenames: false` — no
  framework deps beyond the `pre-commit` package itself, sub-second to
  run, reads repo state directly. Setup is opt-in per-clone:

  ```bash
  pip install pre-commit && pre-commit install
  ```

  After that, every `git commit` runs the consistency lint locally before
  the commit object is created. Same check still runs in CI as a backstop
  for contributors who haven't installed the hook.
- **`cli-smoke` job extended** with an import-test step that runs
  `python -c "import <sensor>"` for every sensor dir BEFORE the `--help`
  loop. `--help`-only catches argparse-time imports; a wrong-symbol
  star-import inside `<sensor>/__init__.py` is invisible to `--help`
  because argparse exits before the broken symbol is ever resolved. The
  bare `import` forces the full module-load path and surfaces those
  errors. Sensor list is disk-scanned (cli.py + process_*.py heuristic),
  so new sensors get exercised automatically.

### Audit-flagged simplifications (not yet actioned)

These came out of the same simplicity audit but require team discussion:

- **The `cli.py` `exec(compile(...))` trick** in every sensor dir is bizarre.
  Console scripts can point directly at `<sensor>.process_<sensor>:main`
  if `process_<sensor>.py` follows the standard
  `if __name__ == "__main__": main()` pattern. Three `cli.py` files could
  be deleted, the `[project.scripts]` entries simplified.
- **The three conda-dep files** could potentially consolidate to one
  (`conda-deps.txt` with section markers + a `pangeo-base.lock` for
  diffing). The current design forces contributors to maintain two lists
  with the relationship documented only in file headers.

---

## Post-mortems

### Capella `ModuleNotFoundError` (`cdd1c23` → `be6693c`)

**Symptom in fresh hub pod:**

```
$ process_capella -h
Traceback (most recent call last):
  File "/srv/conda/envs/notebook/bin/process_capella", line 3, in <module>
    from capella.cli import process_capella_cli
ModuleNotFoundError: No module named 'capella'
```

**Root cause:** `pyproject.toml [tool.setuptools.packages.find].include`
whitelist omitted `"capella*"`. The `[project.scripts] process_capella = "capella.cli:..."`
entry was present, so the console-script shim got installed into
`/srv/conda/envs/notebook/bin/`; but `pip install`'s setuptools step
silently skipped the `capella/` directory entirely because it didn't match
any glob in the include list.

**Affected variants:** both prod and dev image variants — the broken
pyproject.toml was on both `main` and `dev` (`main` is auto-synced from
`dev` and was 5 commits ahead of `dev` at the time of the bug).

**Fix:** one-line addition to the include list (`be6693c`):

```diff
-include = ["landsat*", "sentinel2*", "satellogic*", "umbra*", "shared_utils*", "raster_tools*"]
+include = ["landsat*", "sentinel2*", "satellogic*", "umbra*", "capella*", "shared_utils*", "raster_tools*"]
```

**Prevention:** `tools/check_sensor_consistency.py` (committed in `44eaa98`)
would have failed the PR before the broken state reached production.
Verified locally by removing `"capella*"` from include and re-running the
script — it produces exactly the diagnostic message a contributor needs.

**Cost:** two image variants shipped broken; required a separate fix commit
and a full rebuild cycle. ~30 minutes of triage and 5+ minutes of image
rebuild before the bug was visible-to-fixed.

### Capella copy-paste leftovers (`feat/capella` merge)

**Symptom:** `notebooks/capella_workflow.ipynb` shipped with frontmatter
claiming the notebook was about Sentinel-2 imagery, a section heading that
read "Process Umbra Data", and a typo "Sigmna" in the product list.

**Root cause:** the notebook was hand-authored by duplicating the umbra
template and not fully relabeling. No linter or template-substitution
mechanism caught the residual references.

**Fix:** bulk-edit during the TARGET_CRS standardization session (`cdd1c23`).

**Prevention (shipped):** `tools/new_sensor.py` renders both notebooks
from a templated, sensor-name-substituted source; the extended
`tools/check_sensor_consistency.py` (cell-0 conformance check) fails CI
when any sensor's notebook frontmatter leaks another sensor's name.

---

## See also

- [HUB_DEPLOYMENT.md](HUB_DEPLOYMENT.md) — the cross-repo image build flow,
  debug checklist when CLIs are missing on a fresh pod.
- [ADDING_A_NEW_SENSOR.md](ADDING_A_NEW_SENSOR.md) — operator-facing guide
  for shipping a new sensor product.
- [ADDING_FUNCTIONS_TUTORIAL.md](ADDING_FUNCTIONS_TUTORIAL.md) — adding a
  `shared_utils` function (lower level than a full sensor pipeline).
- [SHARED_UTILS_API.md](SHARED_UTILS_API.md) — function-signature reference.
- The plan file the audit came from: `/Users/klesinger/.claude/plans/yes-commit-iterative-muffin.md`
  (local-only; not committed).
