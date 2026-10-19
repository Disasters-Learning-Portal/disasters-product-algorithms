# Product S3 layout

Where every product this repo produces is published, and what its filename looks like.

The directory and filename strings below were taken from a listing of the live bucket,
not from this repo's code — the two had drifted apart. Where they disagree, the bucket
wins, and this document records the bucket.

The destinations are hard-coded in [`src/shared_utils/product_paths.py`](../src/shared_utils/product_paths.py).
Change them there, not in a processor; every processor and the DPS staging upload read
from that one table.

## Canonical location

```
s3://nasa-disasters-staging/ProgramData/<Sensor>/<Product>/<filename>.tif
```

Exactly three segments under `ProgramData/`. There is **no event, date, or `Output/`
level** in the key — the activation event is carried in the GeoTIFF tags
(`ACTIVATION_EVENT`, embedded by `convert_to_cog`), not in the path.

Processors still write locally into `<out_home>/<date>/<Product>/`, because the `<date>`
level is what groups scenes for merging. That level is dropped at upload time by
`staging_upload.program_data_key`; it never reaches S3.

## Sensor directories

`Landsat/` `Sentinel-2/` `Satellogic/` `Skysat/` `Umbra/` `Capella/` `Iceye/`

Two of these are not spelled the way the code spells them elsewhere: the bucket uses
**`Skysat`** (not `SkySat`) and **`Iceye`** (not `ICEYE`).

## Product directories

Product directories are **PascalCase**. The product token *inside the filename* is a
different string — camelCase, and for two products a longer word. Both are deliberate;
changing one does not change the other.

| Product | Directory | Filename token |
|---|---|---|
| True Color | `TrueColor/` | `trueColor` |
| Color IR | `ColorIR/` | `colorInfrared` |
| Natural Color | `NaturalColor/` | `naturalColor` |
| Shortwave IR | `ShortwaveIR/` | `shortwaveInfrared` |
| Cloud Mask | `CloudMask/` | `cloudMask` |
| Panchromatic | `Panchromatic/` | `panchromatic` |
| Water Extent | `WaterExtent/` | `waterExtent` |
| NDVI | `NDVI/` | `NDVI` |
| NDWI | `NDWI/` | `NDWI` |
| MNDWI | `mNDWI/` | `MNDWI` |
| NBR | `NBR/` | `NBR` |
| dNBR | `dNBR/` | `dNBR` |
| EVI | `EVI/` | `EVI` |

`mNDWI/` has a lowercase leading `m`, matching the conventional spelling of the index.

Satellogic and SkySat spell their filename tokens differently again — Satellogic uses
all-lowercase (`truecolor`, `colorir`), SkySat uses PascalCase (`TrueColor`).

## Timestamps

Two conventions, both intentional:

- **Landsat, Sentinel-2** — date only, with a temporal-resolution suffix:
  `..._2025-04-14_day.tif`
- **Satellogic, Skysat, Umbra, Capella, Iceye** — full ISO-8601 instant, no suffix:
  `..._2026-04-22T00:45:47Z.tif`

## Filename patterns

```
Landsat      [<AOI>_]<SAT>[_<LEVEL>]_<product>[_<HHMMSS>]_<PATHROW|merged>_<YYYY-MM-DD>_day.tif
             LC08_L1TP_015035_trueColor_2023-07-06_day.tif
             LC09_NDVI_merged_2025-04-08_day.tif

Sentinel-2   [<AOI>_]<SAT>[_<LEVEL>]_<product>[_<HHMMSS>]_<TILE|merged>_<YYYY-MM-DD>_day.tif
             S2A_MSIL2A_shortwaveInfrared_161221_T16RGT_2024-10-12_day.tif
             BMX_S2B_trueColor_merged_2025-02-17_day.tif

Satellogic   Satellogic_<SAT>_<product>_<ISO>.tif
             Satellogic_SNXX_truecolor_2026-04-22T00:45:47Z.tif

Skysat       SkySat_<LEVEL>_<Product>[_<scene>]_<ISO>.tif
             SkySat_SR_TrueColor_2026-04-17T04:48:08Z.tif          (vendor-made composite)
             SkySat_TOA_NDVI_ssc2_u0002_2026-04-20T21:36:58Z.tif   (process_skysat)

Umbra        Umbra-<NN>_sigma0_filtered<N>_<ISO>.tif
Capella      Capella[-<NN>]_sigma0_<ISO>.tif
Iceye        ICEYE_sigma0-dB_<ISO>.tif
```

`process_skysat` names carry two tokens the vendor composites do not. `<LEVEL>` is
`TOA` for anything derived from the `_analytic`/`_basic_analytic` asset (DN x
`reflectance_coefficients` is top-of-atmosphere reflectance, never `SR`) and `Visual`
for the `_visual` asset. `<scene>` (`ssc2_u0002`, or `ssc2d1_0001` for a
`basic_analytic` tile) is kept because one collect holds several scenes that share a
timestamp and would otherwise overwrite each other.

SAR names are built by `shared_utils.file_naming.create_sar_output_filename`, which is
also where the rationale for the shape lives (datetime last, `_`-separated product
token, no leading `<YYYYMM>_`).

## SAR product directories

| Sensor | Product | Directory |
|---|---|---|
| Umbra | sigma0 | `GEC/` |
| Umbra | beta0 | **undecided** |
| Umbra | gamma0 | **undecided** |
| Capella | sigma0 | `Backscatter/` |
| Iceye | sigma0 / -amp / -dB | `Backscatter/` |

Umbra's directory names the delivered processing level (Geocoded Ellipsoid Corrected)
rather than the radiometric product, so it cannot distinguish `beta0` and `gamma0` from
`sigma0`. Those two are recorded as `None` in `PRODUCT_DIRS` with a note in
`UNDECIDED_NOTES`. Until the team decides, `product_output_dir` writes them flat and
prints a warning naming the open decision — an undecided path blocks a release, not a
running job.

Resolving it means either giving each its own directory (`Beta0/`, `Gamma0/`) or moving
Umbra onto product directories the way Capella and Iceye already are.

## Publishing from a DPS job

Each `dps/<sensor>/run.sh` sets `PRODUCT_SENSOR` next to its locked S3 destination.
`dps/_finalize.sh` forwards it to `upload_dir_to_staging(..., sensor=...)`, which keys
every product by its canonical destination. Leaving `PRODUCT_SENSOR` unset preserves the
older behaviour — the `OUT_HOME`-relative key under `STAGING_DEST_BASE/<event>/`.

A file that is not under a recognized product directory is not dropped: it falls back to
the relative key and the upload prints a warning naming it.

## Known inconsistencies in the bucket

Current state, recorded for cleanup. These are **not** conventions to follow.

- `Sentinel-2/TestTrueColor/`, `Sentinel-2/TestShortwaveIR/` and `Umbra/GECTest/` hold
  test output inside the production prefix.
- `Sentinel-2/NDVI/` contains **only** NDVI *change* products — all 36 objects are
  `NDVIchange_<TILE>_binaryMaskFilter_<DATE>_day.tif`. No plain Sentinel-2 NDVI product
  has ever been published.
- `Sentinel-2/ColorIR/` mixes the `colorIR` and `colorInfrared` filename tokens
  (10 objects vs 355).
- `Sentinel-2/NBR/` mixes `S2A_NBR_*` and lowercase `s2_NBR_*`.
- Landsat places the path/row before the product token in some objects, after it in
  others.
- Satellogic publishes the placeholder satellite token `SNXX` on every object rather
  than the real platform id.
- Capella is inconsistent about whether the platform number is included.

## Where the notebooks live

`notebooks/` holds the six per-sensor workflows and nothing else:

```
notebooks/landsat_workflow.ipynb        notebooks/umbra_workflow.ipynb
notebooks/sentinel2_workflow.ipynb      notebooks/capella_workflow.ipynb
notebooks/sentinel2_odr_workflow.ipynb  notebooks/satellogic_workflow.ipynb
```

Everything an operator reaches for around them -- transfers, templates, one-off
utilities -- moved to `notebooks/tools/`. The fixtures under
`notebooks/testing-notebooks/` are exempt from all of this and keep their inline
values.

A sensor notebook names **no bucket and no product directory**. It declares its
sensor and lets the table resolve the rest:

```python
from shared_utils.product_paths import STAGING_BUCKET

S3_BUCKET = STAGING_BUCKET
PRODUCT_SENSOR = "landsat"
```

Uploads then key themselves: `upload_dir_ambient(..., sensor=PRODUCT_SENSOR)`
for the directory uploader, or `prefix_for_product_dir(PRODUCT_SENSOR, <dir>)`
for the per-file one. Both use **ambient** AWS credentials; the MAAP-credential
`upload_dir_to_staging` is the DPS path only and 401s on the hub. Pinned by
`tests/unit/test_notebook_layout.py`.

`sentinel2_odr_workflow.ipynb` is generated by
[`tools/_build_s2_odr_notebook.py`](../tools/_build_s2_odr_notebook.py) -- edit the
generator and re-run it, never the notebook JSON. Its old `PRODUCT_FOLDERS` map
hard-coded `colorIR`, `SWIR`, `MNDWI` and `waterExtent`, none of which match the
bucket; it now declares `_PRODUCT_KEYS` (filename token -> product key) and
resolves every directory through `product_dir()`.

Sentinel-2 `EVI` is in the table because the STAC/ODR workflow can produce it.
The legacy `process_sentinel2` CLI has no EVI product and never creates that
directory.

## Not yet corrected: the tools notebooks

Five notebooks under `notebooks/tools/` still build `ProgramData/{product}/Output`:

- `notebooks/tools/simple_disaster_staging.ipynb`
- `notebooks/tools/drcs_transfer.ipynb`
- `notebooks/tools/drcs_new_transfer.ipynb`
- `notebooks/tools/cog_metadata_template.ipynb`
- `notebooks/tools/cog_metadata_template_cli.ipynb`

That shape is wrong twice over: it omits the `<Sensor>` level and adds an
`Output/` level that matches **zero** objects in the bucket. The correct shape is
`ProgramData/<Sensor>/<Product>`.

They are left alone on purpose. These notebooks publish far more than the sensors
in `product_paths.py` -- NISAR, BlackMarble, GPM, OPERA, AVIRIS-3, UAVSAR,
Sentinel-1 -- and each needs its own `<Sensor>`/`<Product>` split confirmed before
the template can change. The bucket shows the shape those products already use
(`NISAR/GUNW/`, `Blackmarble/brdfCorrected/`, `GPM/IMERG/`, `OPERA/DSWx/`, ...),
but the mapping from a notebook *category* to that pair has not been confirmed,
and guessing it would silently publish to the wrong prefix.

Three bucket layouts also break the three-segment rule and are deliberately absent
from the table: `GAIA/` and `Sentinel-1_2/` put files directly under the sensor,
and `AVIRIS-3/dnbr/dnbrColor/` is four levels deep.

Tracked in disasters-product-algorithms#155 and disasters-portal#513.
