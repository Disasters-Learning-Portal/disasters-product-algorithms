# Disasters Hub — candidate JupyterLab extensions

A reference catalog of JupyterLab extensions that **complement the MAAP DPS workflow** on the
Disasters Hub (`image/environment.yml`, built on the MAAP `2i2c/pangeo` base image
`mas.maap-project.org/root/maap-workspaces/2i2c/pangeo` — a NASA-VEDA / pangeo derivative,
JupyterLab 4 / Python 3.13). The operator loop these should smooth is:

> **discover scenes → register / submit a DPS job → monitor → COG lands in `output/` + S3/STAC → inspect it**

This is a **shortlist for a later decision, not a change that's been applied.** Nothing here is
installed yet — adding any of it means editing `image/environment.yml` (which triggers a hub
rebuild; see [HUB_DEPLOYMENT.md](HUB_DEPLOYMENT.md)). The catalog exists so that decision can be
made quickly and without re-doing the survey.

> **On version numbers:** the candidate versions below were sourced from the web and are **not
> verified against a live hub pod.** Exact pins + JupyterLab-4 wheel availability must be confirmed
> at build time. Follow the existing `environment.yml` convention — prefer **floors, not exact
> pins**, for the pure-Python geospatial libs, and let the conda solver match whatever GDAL/JL the
> base image ships (an over-tight pin is what broke the `2025.08.14 → 2026.06.04` base bump).

---

## 1. Already installed (context)

The current hub stack these recommendations build on top of:

| Package | Role |
|---|---|
| `maap-dps-jupyter-extension` | Submit Jobs / View My Jobs tiles — *from the MAAP base* |
| `maap-algorithms-jupyter-extension` | Algorithm Catalog / Register / My Builds tiles — *from the MAAP base* |
| `maap-jupyter-server-extension` | server bridge (`MAAP_API_HOST` / `MAAP_PGT`) — *from the MAAP base* |
| `stac_ipyleaflet` | STAC layer browser on an ipyleaflet map — pip-pinned in `environment.yml` |
| `maap-py` | MAAP DPS Python client — *from the MAAP base* |

> The MAAP-Plugins tiles + `maap-py` are **inherited from the MAAP `2i2c/pangeo` base image**, not
> pinned in `environment.yml` (they used to be pip-pinned; the base swap dropped those pins so we
> track MAAP's maintained versions). See [HUB_DEPLOYMENT.md](HUB_DEPLOYMENT.md).

---

## 2. Already in the base image — do **NOT** re-add

These ship in the MAAP `2i2c/pangeo` base (a pangeo derivative). Adding them to `environment.yml`
is redundant (and risks a solver conflict):

- `jupyterlab-git` — Git integration
- `jupyterlab-lsp` + `python-lsp-server` — autocomplete / linting for editing `run.sh` / Python
- `jupyterlab-code-formatter` — Black / isort buttons
- `jupyter-resource-usage` — CPU / memory status bar
- `jupyterlab-myst` — rich Markdown rendering
- `dask-labextension` — Dask cluster dashboard
- `jupyter-server-proxy` — proxies in-notebook servers (already used by `jupyter-vscode-proxy` /
  `jupyter-sshd-proxy`; **also what `localtileserver` below rides on**)
- **built-in `CSVViewer`** — JupyterLab 4 renders `output/available_<sensor>_dates.csv` (the
  `list-dates` artifact) as a sortable DataGrid with **no extension at all**. A CSV-editor
  extension is not needed.

---

## 3. Recommended candidates

Grouped by value to the DPS loop. "Prebuilt" = ships as a wheel, no Node build step.

### Group A — COG / map preview (HIGH value — closes the "inspect my DPS output" gap)

Today a COG must be pushed to STAC and previewed remotely (openveda TiTiler) before an operator
can eyeball it. These let it be previewed **in the notebook, straight from `output/` or S3.**
Add via **conda-forge** (top-level `dependencies:` in `environment.yml`) so the GDAL-linked deps
stay ABI-consistent with the base image — the repo's standing rule is "GDAL via conda, not pip".

| Extension | What it does | Why it fits DPS | Install target |
|---|---|---|---|
| **leafmap** | One-line in-notebook map: `m.add_raster(path)`, `add_vector`, STAC layers. Pure-Python wrapper over `ipyleaflet` (already in base). | Previews a DPS COG in one line; no manual `ipyleaflet` layer plumbing. | `conda-forge::leafmap` (floor) |
| **localtileserver** | Serves a **local or S3 COG** as XYZ tiles via `jupyter-server-proxy` (already present). | Preview a COG **before/without** a STAC push — the missing "did my job produce a good COG?" check. `leafmap.add_raster` uses it under the hood. | `conda-forge::localtileserver` (floor) |

Usage sketch (once installed):

```python
import leafmap
m = leafmap.Map()
m.add_raster("output/<event>/<scene>.tif")   # local preview via localtileserver
m
```

### Group B — Notebook productivity (MEDIUM value)

| Extension | What it does | Why it fits DPS | Install target |
|---|---|---|---|
| **jupyterlab-execute-time** | Per-cell run time in the cell margin. | Long GDAL/warp cells + DPS-authoring notebooks — see which step is slow. | pip or `conda-forge` (prebuilt) |
| **jupyter-archive** | Right-click a folder → *Download as zip*. | Bundle a multi-file COG `output/` dir for hand-off without shelling out to `tar`. | pip or `conda-forge` (prebuilt server ext) |

### Group C — MAAP ecosystem tiles (LOW / OPTIONAL — verify JL4 wheel at build time)

MAAP extensions were historically **JupyterLab-3-only**; confirm a JL4 wheel exists before adding.

| Extension | What it does | Caveat |
|---|---|---|
| **maap-edsc-jupyter-extension** | Earthdata Search Client panel for dataset discovery. | Disasters SAR sensors pull from **CSDA vendor buckets, not Earthdata** — marginal for this workflow. |
| **maap-libs-jupyter-extension** | Scaffolds MAAP/`maap-py` boilerplate into a notebook. | Nice-to-have, not workflow-critical. |
| **maap-help-jupyter-extension** | MAAP onboarding tour + help-tab entries. | Onboarding only; overlaps the existing Help → *Disasters Resources* submenu. |

---

## 4. Skip (evaluated, not worth it)

| Extension | Reason |
|---|---|
| `jupyterlab-tabular-data-editor` | Unmaintained; JL4 unconfirmed. The built-in `CSVViewer` already covers `available_<sensor>_dates.csv`. |
| `jupyterlab-bxplorer` | **Removed 2026-08-11.** Imports Bootstrap 5's dist CSS *unscoped* into Lab's global stylesheet, so Bootstrap's Reboot overrides `body{font-size}` app-wide and Lab's fixed-height chrome overflows into scrollbar stepper arrows everywhere (2i2c-org/infrastructure#8770). Do not re-add until upstream scopes it — [BXPLORER_BOOTSTRAP_ISSUE.md](BXPLORER_BOOTSTRAP_ISSUE.md), `.clinerules.md` rule 39. |
| `jupyterlab-s3-browser` | Unmaintained (IBM; last release 0.12.0, May 2022) and JL2/3-era — still builds on `jupyter-packaging ~=0.7.9`, JL4 unconfirmed. Lab's own file browser plus `s3fs`/`boto3` in a notebook covers the vendor-bucket browsing these workflows need. |
| `xarray-leaflet` | Stale (~2023); `leafmap` supersedes it. |
| `jupyterlab-slurm` / `jupyterlab-system-monitor` | JL3-era / archived. DPS isn't SLURM; `jupyter-resource-usage` (in base) covers monitoring. |
| MAAP `umf` / `ipycmc` / `che-*` / `maap-jupyter-ide` | Eclipse-Che-only, require a Node build, or archived (JL2/3). |

---

## If/when you decide to add any of these

1. Edit `image/environment.yml` — Group A under `dependencies:` (conda-forge); Groups B/C under the
   existing `pip:` block. Keep the one-line rationale-comment style used for every other entry, and
   prefer floors over exact pins for the geospatial libs.
2. Push per the deploy flow in [HUB_DEPLOYMENT.md](HUB_DEPLOYMENT.md) — the dev image rebuilds from
   `build-and-push-dev.yaml`; watch the Actions log for a clean conda solve.
3. Smoke-test on a fresh dev pod: `python -c "import leafmap, localtileserver"` and
   `jupyter labextension list` (execute-time / jupyter-archive enabled, no load errors); confirm
   `stac_ipyleaflet` + the MAAP tiles still load.
