# Test fixtures

Small crops of **real** activation products, committed so tests can assert against the
shapes vendors and our own pipelines actually emit rather than against synthetic rasters
that only exercise the happy path.

Rules for anything added here:

- **< 500 KB each.** Pinned by `tests/unit/test_fixture_inventory.py`.
- **Crop, don't downsample.** Resampling changes dtype ranges and can invent or destroy
  the nodata/fill pixels that are usually the whole point of the fixture.
- **Record what it pins** in the table below and in the inventory test. A fixture nobody
  can explain gets deleted the first time it is in the way.

Most were cut with `gdal_translate -srcwin <x> <y> 256 256 -co COMPRESS=DEFLATE
-co PREDICTOR=2`, on a window chosen to be fully valid data (see the size/validity
assertions in the inventory test).

## Why the 256x256 crops cannot test COG-ness

`rio_cogeo.cog_validate` only requires internal tiling once an image exceeds 512 px in
**both** dimensions. So every 256x256 crop here passes COG validation regardless of how it
was written, and none of them can stand in for a non-COG input (e.g. for
`notebooks/tools/bake_event_metadata.ipynb`, which flags and converts non-COG sources).

`satellogic_truecolor_striped_noncog_600.tif` exists for exactly that: 600x600 and
untiled, so it genuinely fails validation with
`The file is greater than 512xH or 512xW, but is not tiled`.

## Inventory

| File | Profile | What it pins |
|---|---|---|
| `gaia_atlanta_sample.tif` | 256², 1x float32, nodata -9999 | GAIA Web-Mercator crop; `summarize_raster` + `bake_event_metadata` stats |
| `blackmarble_sf_misregistered_crop.tif` | 3x uint8, unnamed local Albers | Frozen evidence of the upstream Landsat georeferencing defect (`dps/blackmarble/bm_georef.py`) |
| `umbra_guam_sar_db_crop.tif` | 256², 1x float32, EPSG:4326, **no nodata**, **rotated** | SAR dB where 0 dB is real data. Source carries genuine geotransform rotation **and a negative x-scale** — a north-up assumption breaks on it |
| `iceye_guam_sar_db_crop.tif` | 256², 1x float32, EPSG:4326, **no nodata** | float32 SAR with no nodata tag → the dtype auto-detect path |
| `satellogic_truecolor_nodata0_crop.tif` | 256², 3x uint8, EPSG:32655, nodata 0 | `is_bare_8bit_imagery` says "no nodata" while the source tag says `0` — pins which wins |
| `skysat_colorir_nodata0_crop.tif` | 256², 3x uint8, EPSG:32655, nodata 0 | Same conflict on SkySat SR, the vendor that genuinely reserves 0 as collect-geometry fill |
| `satellogic_colorir_tagged_cog_crop.tif` | 256², 3x uint8, real COG | Already carries all six activation tags → the idempotent-skip path in `bake_event_metadata` |
| `dswx_s1_wtr_classcodes_crop.tif` | 256², 1x uint8, EPSG:3857, nodata 0 | OPERA DSWx S1 WTR class codes `{1,3,251,255}` — the raster whose AVERAGE overviews invented class 2 == (1+3)/2. Categorical with high sentinel codes |
| `distalert_vegdiststatus_crop.tif` | 256², 1x uint8, EPSG:3857, nodata 0 | DIST-ALERT VEG-DIST-STATUS: 3 class codes in only 2,135 valid pixels → **categorical while sparse**. Pins that the valid-pixel floor does not misfire on a sparse class layer |
| `distalert_veganommax_crop.tif` | 512², 1x uint8, EPSG:3857, nodata 0 | DIST-ALERT VEG-ANOM-MAX: 64 distinct values → **continuous**. Same source directory as the row above, opposite verdict — the pair is what forbids per-directory or per-collection resampling |
| `distalert_status_palette_crop.tif` | 256², 1x uint8, EPSG:3857, nodata 255, **color table** | Palette raster: the pixel values ARE legend indices, so it is categorical with no pixel read at all |
| `hydrosar_watermask_int8_crop.tif` | 256², 1x **int8**, EPSG:32617, **no nodata** | HydroSAR water mask `{0,1,2,3,4}`. `get_resampling_for_dtype` buckets int8 as "probably continuous" → AVERAGE over class codes; the dtype-only rule cannot see this |
| `blackmarble_brdf_tagged_cog_crop.tif` | 256², 1x float32, **EPSG:3857**, nodata -9999 | The only Web-Mercator + already-tagged COG fixture |
| `mwir_rotated_geotransform_crop.tif` | 256², 3x uint8, EPSG:4326, nodata 0, **rotated** | Non-north-up 8-bit imagery (airborne MWIR scan) |
| `cloudmask_byte_nodata255.tif` | 327x543, 1x uint8, **nodata 255** | Single-band uint8 whose nodata is *not* 0 — the counter-case to the bare-8-bit carve-out |
| `satellogic_truecolor_striped_noncog_600.tif` | 600², 3x uint8, untiled | The one fixture that genuinely **fails** `cog_validate` |

## Provenance

| Fixture | Cut from |
|---|---|
| `umbra_guam_sar_db_crop.tif` | `202604_Guam_Umbra_BackscatterdB_20260414T115251Z.tif` |
| `iceye_guam_sar_db_crop.tif` | `202604_Guam_Iceye_BackscatterdB_20260414T131650Z.tif` |
| `satellogic_truecolor_nodata0_crop.tif`, `satellogic_truecolor_striped_noncog_600.tif` | `202604_Guam_Satellogic_TrueColor_20260422T010755.tif` |
| `skysat_colorir_nodata0_crop.tif` | `202604_Guam_SkySat_ColorInfrared_20260419T045141.tif` |
| `satellogic_colorir_tagged_cog_crop.tif` | `Satellogic_SNXX_colorir_2026-04-22T01_07_55Z.tif` (our own published COG) |
| `blackmarble_brdf_tagged_cog_crop.tif` | `pre_event_blackmarble_BRDF_2026-03_monthly.tif` (our own published COG) |
| `cloudmask_byte_nodata255.tif` | `2026_Apr_TyphoonSinlaku_A2BRDFCloudMask_20260425.tif` (recompressed whole, not cropped) |
| `mwir_rotated_geotransform_crop.tif` | `202507_Flood_TX_scan_0002__191_23_40_46_927_mwir_4384_2025-07-10_day.tif` |

All are from the 202604 Typhoon Sinlaku (Guam) activation except the MWIR scan, which is
from 202507 Flood TX.
