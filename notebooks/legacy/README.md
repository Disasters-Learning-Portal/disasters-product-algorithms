# Retired workflow notebooks

A notebook lives here when a newer pipeline for the same sensor has replaced it as
the operator's default. It is **still runnable**: retiring a workflow moves it out of
the way, it does not switch it off.

| Retired notebook | Pipeline it drives | Superseded by | Still needed for |
|---|---|---|---|
| `landsat_workflow.ipynb` | `process_landsat89` — USGS `.tar` archives downloaded by hand, then processed locally | [`../landsat_odr_workflow.ipynb`](../landsat_odr_workflow.ipynb) (`process_landsat_odr`, STAC) | Panchromatic, water extent and the cloud-mask product, which the STAC pipeline does not produce. The Landsat DPS job (`dps/landsat/`) also still runs this pipeline. |
| `sentinel2_workflow.ipynb` | `process_sentinel2` — `.SAFE` archives from the Copernicus Data Space (`COP_USER` / `COP_PASS`) | [`../sentinel2_odr_workflow.ipynb`](../sentinel2_odr_workflow.ipynb) (`process_sentinel2_odr`, STAC) | Comparing the two pipelines on a real activation. The `.SAFE` DPS algorithm `disasters-sentinel2-process` is still registered. |

## When to use one

Use a notebook from this folder only when the current workflow cannot do the job: it
lacks a product you need, or it has a defect that is still being fixed. Open an issue
when you do, so the gap is closed rather than worked around.

## What "retired" means here

- **The code is not retired.** `process_landsat89`, `process_sentinel2` and
  `download_sentinel2` are still installed console scripts, still tested, and still
  what the `dps/landsat/` and `dps/sentinel2/` jobs run.
- **The publishing rules still apply.** A retired notebook publishes to the same
  `ProgramData/<Sensor>/<Product>/` destinations through
  `shared_utils.product_paths`, with ambient credentials and with upload off by
  default. `tests/unit/test_notebook_layout.py` holds it to the same contract as a
  current workflow.
- **Names do not move.** A retired notebook keeps its filename and its successor
  keeps its own (`*_odr_workflow.ipynb`), so a name never changes which pipeline it
  means.

## Retiring another workflow

1. `git mv notebooks/<name>.ipynb notebooks/legacy/<name>.ipynb`
2. Add the `LEGACY WORKFLOW` banner as the cell right after the title or front
   matter, naming the successor, and change `sys.path.append('../src')` to
   `'../../src'`.
3. Move its entry from `SENSOR_NOTEBOOKS` to `LEGACY_NOTEBOOKS` in
   `tests/unit/test_notebook_layout.py`, and update the notebook count in
   `tests/unit/test_product_roundtrip_smoketest.py` if the total changed.
4. Run `python tools/check_sensor_consistency.py`. It refuses a sensor that is left
   with only a retired notebook.

Deleting a retired notebook for good is the last step of retiring the *pipeline*
(console script, DPS algorithm, docs). It is not something to do on its own.
