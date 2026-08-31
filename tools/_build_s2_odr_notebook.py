"""One-shot generator for notebooks/sentinel2_odr_workflow.ipynb.

Kept in tools/ so the notebook can be regenerated deterministically rather
than hand-edited as JSON. Run:

    python tools/_build_s2_odr_notebook.py
"""
import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, "notebooks", "sentinel2_odr_workflow.ipynb")


def md(text):
    return {"cell_type": "markdown", "metadata": {}, "source": text.strip("\n").splitlines(keepends=True)}


def code(text):
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": text.strip("\n").splitlines(keepends=True),
    }


CELLS = [
    md("""
---
title: Sentinel-2 STAC Processing Workflow
description: Generate Sentinel-2 disaster-response COGs by querying a STAC API (Earth Search) over a bounding box and date range, reading the cloud-optimized assets directly from S3 — no scene download and no Copernicus credentials.
author:
  - Ethan Kerr (Editor, UAH)
date: August 31, 2026
execute:
   freeze: true
---
"""),
    md("""
# Sentinel-2 STAC Processing Workflow

This notebook drives **`process_sentinel2_odr`**, the STAC/COG Sentinel-2
pipeline. It queries [Earth Search](https://earth-search.aws.element84.com/v1)
for scenes intersecting a bounding box and reads the assets straight from AWS
Open Data.

> **There are two Sentinel-2 pipelines right now**, on purpose.
> `sentinel2_workflow.ipynb` drives the older `process_sentinel2`, which
> downloads `.SAFE` archives from the Copernicus Data Space and needs
> `COP_USER` / `COP_PASS` credentials. **This** notebook needs no credentials
> and no download step. Both are live until the migration finishes — see
> [issue #144](https://github.com/Disasters-Learning-Portal/disasters-product-algorithms/issues/144).

## Steps

1. **Configure** the activation — event, AOI, dates, products.
2. **Preview** the AOI on a map.
3. **Process** each selected product.
4. **Review** the outputs.
5. **Upload** to S3 (opt-in, off by default).
"""),
    md("""
## 1. Configure the activation

Edit this cell for each new activation. Everything below it is plumbing.
"""),
    code('''
# ==============================================================================
# ACTIVATION OPTIONS -- edit for each activation
# ==============================================================================

# ------------------------------------------------------------------------------
# Metadata
# ------------------------------------------------------------------------------

# MUST be YYYYMM_Hazard_Location. shared_utils.cog_metadata.resolve_metadata
# splits this into the YEAR_MONTH / HAZARD / LOCATION GeoTIFF tags, so a
# free-text name silently produces incomplete metadata on every product.
EVENT_NAME = "202601_Flood_ExampleCity"

# Sentinel-2 is an ESA Copernicus mission. Earth Search only redistributes it
# on AWS, so the provenance is still Copernicus -- NOT "CSDA", which is the
# commercial smallsat program (Satellogic / Umbra / Capella).
SOURCE = "Copernicus"

# ------------------------------------------------------------------------------
# Sentinel-2 data selection
# ------------------------------------------------------------------------------

# "1" = Level-1C (top-of-atmosphere, gets a Rayleigh correction)
# "2" = Level-2A (surface reflectance) -- recommended
LEVEL = "2"

# Inclusive, YYYY-MM-DD. Sentinel-2 revisit is ~5 days, so a single day
# catches only one orbit swath -- widen the window if a run finds no scenes.
START_DATE = "2025-08-12"
END_DATE = "2025-08-14"

# [MIN_LON, MIN_LAT, MAX_LON, MAX_LAT] in WGS84.
# Any tile intersecting this box is processed IN FULL (110 km), so a small
# box is fine -- it selects scenes, it does not crop them.
BBOX = [-93.70, 42.00, -93.55, 42.10]

# Only scenes below this scene-level cloud percentage are processed.
CLOUD_COVER = 50

# ------------------------------------------------------------------------------
# Products
# ------------------------------------------------------------------------------

PRODUCTS = {
    "true_color": True,
    "natural_color": False,
    "color_infrared": False,
    "swir": False,
    "ndvi": True,
    "ndwi": False,
    "mndwi": False,
    "nbr": False,
    "evi": False,
    "we": False,
}

# Water-extent NIR threshold(s), in standard deviations above the mean NIR of
# the permanent-water reference sample. One COG per value; the filename
# carries an NSTD_<value> token. Higher = MORE pixels classified as water.
# Only used when "we" is enabled above.
WE_NSTD = [1]

# ------------------------------------------------------------------------------
# Processing toggles
# ------------------------------------------------------------------------------

# Mask cloud / cloud-shadow / thin-cirrus using the L2A Scene Classification
# Layer. Requires LEVEL = "2" -- L1C has no SCL asset.
CLOUD_MASK = True

# Mosaic all matched tiles into a single product instead of one file per tile.
MERGE = False

# ------------------------------------------------------------------------------
# Output
# ------------------------------------------------------------------------------

OUTPUT_DIR = "/tmp/s2_stac_output"

# ------------------------------------------------------------------------------
# S3 upload -- OFF by default.
#
# Deliberately off: this publishes to the production bucket. Turn it on only
# once you have reviewed the outputs below AND set a real EVENT_NAME.
# ------------------------------------------------------------------------------

ENABLE_S3_UPLOAD = False
S3_BUCKET = "nasa-disasters"
S3_DEST_BASE = "drcs_activations_new"
S3_PREFIX = f"{S3_DEST_BASE}/{EVENT_NAME}"
'''),
    code('''
# ==============================================================================
# VALIDATION + ACTIVATION METADATA (auto-populated -- do not edit)
# ==============================================================================
import json
import os
import re
import tempfile

from shared_utils import PROCESSOR_STRING

# --- fail loudly on the mistakes that are expensive to notice later ---

if not re.match(r"^\\d{6}_[A-Za-z]+_.+$", EVENT_NAME):
    raise ValueError(
        f"EVENT_NAME {EVENT_NAME!r} must be YYYYMM_Hazard_Location "
        f"(e.g. 202601_Flood_TX). resolve_metadata parses it into the "
        f"YEAR_MONTH / HAZARD / LOCATION tags, so anything else ships "
        f"products with incomplete metadata."
    )

if EVENT_NAME == "202601_Flood_ExampleCity":
    raise ValueError(
        "EVENT_NAME is still the template placeholder. Set a real "
        "activation event before processing."
    )

if CLOUD_MASK and LEVEL == "1":
    raise ValueError(
        "CLOUD_MASK requires Sentinel-2 L2A (LEVEL = '2'); the Scene "
        "Classification Layer needed for cloud masking does not exist "
        "for L1C products."
    )

if not any(PRODUCTS.values()):
    raise ValueError("No products selected -- enable at least one in PRODUCTS.")

if len(BBOX) != 4 or BBOX[0] >= BBOX[2] or BBOX[1] >= BBOX[3]:
    raise ValueError(
        f"BBOX {BBOX} must be [MIN_LON, MIN_LAT, MAX_LON, MAX_LAT] with "
        f"min < max. A transposed box silently returns zero scenes."
    )

# --- metadata embedded as GeoTIFF tags on every output COG ---

ACTIVATION_METADATA = {
    "ACTIVATION_EVENT": EVENT_NAME,
    "SOURCE": SOURCE,
    "PROCESSOR": PROCESSOR_STRING,
}

_meta_fd, ACTIVATION_METADATA_PATH = tempfile.mkstemp(
    prefix="activation_meta_", suffix=".json"
)
with os.fdopen(_meta_fd, "w") as _f:
    json.dump(ACTIVATION_METADATA, _f, indent=2)

os.makedirs(OUTPUT_DIR, exist_ok=True)

print("Activation metadata:")
for key, value in ACTIVATION_METADATA.items():
    print(f"  {key}: {value}")
print(f"\\nMetadata file: {ACTIVATION_METADATA_PATH}")
print(f"Output dir:    {OUTPUT_DIR}")
print(f"Products:      {[p for p, on in PRODUCTS.items() if on]}")
'''),
    md("""
## 2. Preview the area of interest

Optional. Plots the bounding box against the Sentinel-2 MGRS tiles it
intersects, so you can see how much imagery a run will pull.
"""),
    code('''
# ==============================================================================
# PREVIEW THE AOI
# ==============================================================================
# Each matched tile is processed IN FULL (~110 km square), so a small box can
# still mean several large scenes.
try:
    import matplotlib.pyplot as plt
    from pystac_client import Client

    catalog = Client.open("https://earth-search.aws.element84.com/v1")
    collection = "sentinel-2-c1-l2a" if LEVEL == "2" else "sentinel-2-l1c"

    search = catalog.search(
        collections=[collection],
        datetime=[START_DATE, END_DATE],
        bbox=BBOX,
        query={"eo:cloud_cover": {"lt": CLOUD_COVER}},
        max_items=50,
    )
    items = list(search.items())

    print(f"{len(items)} scene(s) match:")
    for item in items:
        print(
            f"  {item.id}  cloud={item.properties.get('eo:cloud_cover', '?'):.1f}%"
        )

    fig, ax = plt.subplots(figsize=(8, 8))
    for item in items:
        b = item.bbox
        ax.add_patch(
            plt.Rectangle(
                (b[0], b[1]), b[2] - b[0], b[3] - b[1],
                fill=False, edgecolor="tab:blue", lw=1,
            )
        )
        ax.text(b[0], b[3], item.id.split("_")[1], fontsize=7, color="tab:blue")

    ax.add_patch(
        plt.Rectangle(
            (BBOX[0], BBOX[1]), BBOX[2] - BBOX[0], BBOX[3] - BBOX[1],
            fill=False, edgecolor="red", lw=2, label="AOI",
        )
    )
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title(f"AOI and matching {collection} tiles")
    ax.legend()
    ax.autoscale_view()
    plt.tight_layout()
    plt.show()

except ImportError as exc:
    print(f"Preview skipped ({exc}). Processing does not need this cell.")
'''),
    md("""
## 3. Process

One `process_sentinel2_odr` call per enabled product — the STAC CLI takes a
single `--product` per invocation.
"""),
    code('''
# ==============================================================================
# PROCESS SELECTED PRODUCTS
# ==============================================================================
import subprocess

# Record what existed BEFORE this run, so the review and upload cells below can
# scope themselves to what THIS run produced. Globbing the whole output dir
# would sweep up leftovers from a previous activation and publish them under
# the current event's prefix.
import glob

_pre_existing = set(glob.glob(os.path.join(OUTPUT_DIR, "*.tif")))

failures = []

for product, enabled in PRODUCTS.items():

    if not enabled:
        continue

    print()
    print("=" * 70)
    print(f"PROCESSING: {product}")
    print("=" * 70)

    process_cmd = [
        "process_sentinel2_odr",
        "--level", LEVEL,
        "--product", product,
        "--start-date", START_DATE,
        "--end-date", END_DATE,
        "--bbox", *[str(v) for v in BBOX],
        "--cloud-cover", str(CLOUD_COVER),
        "--output", OUTPUT_DIR,
        "--metadata-json", ACTIVATION_METADATA_PATH,
    ]

    if CLOUD_MASK:
        process_cmd.append("--cloud-mask")

    if MERGE:
        process_cmd.append("--merge")

    if product == "we" and WE_NSTD:
        process_cmd += ["--we-nstd", *[str(n) for n in WE_NSTD]]

    print(f"Command: {' '.join(process_cmd)}\\n")

    process = subprocess.Popen(
        process_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )
    for line in process.stdout:
        print(line, end="")

    return_code = process.wait()

    if return_code == 0:
        print(f"\\n[ok] {product} completed")
    else:
        failures.append((product, return_code))
        print(f"\\n[FAILED] {product} exited {return_code}")

# Products written by THIS run.
NEW_COGS = sorted(set(glob.glob(os.path.join(OUTPUT_DIR, "*.tif"))) - _pre_existing)

print()
print("=" * 70)
print(f"{len(NEW_COGS)} COG(s) produced by this run:")
for f in NEW_COGS:
    print(f"  {os.path.basename(f)}")
if failures:
    print(f"\\n{len(failures)} product(s) FAILED: {failures}")
'''),
    md("""
## 4. Review the results

**Check these before uploading.** A COG that opens is not the same as a COG
that is correct.
"""),
    code('''
# ==============================================================================
# REVIEW
# ==============================================================================
import rasterio as rio

for path in NEW_COGS:
    with rio.open(path) as src:
        print(f"{os.path.basename(path)}")
        print(
            f"   {src.count} band(s)  {src.dtypes[0]}  nodata={src.nodata}  "
            f"{src.width}x{src.height}  {src.crs}"
        )
        tags = src.tags()
        missing = [
            k for k in ("ACTIVATION_EVENT", "SOURCE", "PROCESSOR")
            if not tags.get(k)
        ]
        if missing:
            print(f"   [WARN] missing metadata tags: {missing}")
        else:
            print(f"   event={tags['ACTIVATION_EVENT']}  source={tags['SOURCE']}")
        print()
'''),
    code('''
# ==============================================================================
# PREVIEW THE COGs
# ==============================================================================
from shared_utils.plotting import preview_cogs

if NEW_COGS:
    print(f"Previewing {len(NEW_COGS)} COG(s)...")
    preview_cogs(NEW_COGS, sample_n=10)
else:
    print("No COGs produced by this run.")
'''),
    md("""
## Output naming

```
<SAT>_<LEVEL>_<product>[_NSTD_<n>]_<TILE>[_merged][_masked]_<TIMESTAMP>.tif
```

e.g. `S2C_MSIL2A_ndvi_T15TVG_2025-08-13T17:12:26Z.tif`

**The activation event is NOT in the filename.** It lives in the GeoTIFF tags
and the S3 prefix — the same convention `bake_event_metadata.ipynb` and
`simple_disaster_staging.ipynb` use. (The older `.SAFE` pipeline prefixes the
event onto filenames instead, so names differ between the two.)
"""),
    md("""
## 5. Upload to S3 — opt-in

Off by default. Set `ENABLE_S3_UPLOAD = True` in the config cell **after**
reviewing the outputs above.
"""),
    code('''
# ==============================================================================
# UPLOAD TO S3 (optional)
# ==============================================================================
# Publishes to s3://{S3_BUCKET}/{S3_PREFIX}/sentinel-2/<product_folder>/<filename>
from shared_utils import upload_file_to_s3

# Maps the filename product token -> its S3 folder. Keys must match the
# camelCase token _build_output_filename writes, and every product in PRODUCTS
# needs an entry or its COGs are silently skipped.
PRODUCT_FOLDERS = {
    "trueColor": "trueColor",
    "naturalColor": "naturalColor",
    "colorInfrared": "colorIR",
    "swir": "SWIR",
    "ndvi": "NDVI",
    "ndwi": "NDWI",
    "mndwi": "MNDWI",
    "nbr": "NBR",
    "evi": "EVI",
    "waterExtent": "waterExtent",
}

_unmapped = {
    p for p, on in PRODUCTS.items() if on
} - {
    "true_color", "natural_color", "color_infrared", "swir", "ndvi",
    "ndwi", "mndwi", "nbr", "evi", "we",
}
if _unmapped:
    raise ValueError(f"products with no folder mapping: {sorted(_unmapped)}")

if not ENABLE_S3_UPLOAD:
    print(
        "S3 upload OFF. Review the outputs above, then set "
        "ENABLE_S3_UPLOAD = True in the config cell to publish."
    )

else:
    # Scoped to THIS run (NEW_COGS), and to top-level *.tif only. The
    # water-extent product caches CDL / WorldCover reference rasters under
    # OUTPUT_DIR/water_extent_reference/ -- those are hundreds of MB, are not
    # products, and a recursive glob would publish them.
    cogs = [f for f in NEW_COGS if not f.endswith(".tmp.tif")]

    if MERGE:
        cogs = [f for f in cogs if "merged" in os.path.basename(f)]

    uploaded, skipped = 0, []

    for f in cogs:
        filename = os.path.basename(f)

        subfolder = None
        # Longest token first, so "colorInfrared" is not shadowed by a
        # shorter key that happens to be a substring.
        for token in sorted(PRODUCT_FOLDERS, key=len, reverse=True):
            if f"_{token}_" in filename:
                subfolder = PRODUCT_FOLDERS[token]
                break

        if subfolder is None:
            skipped.append(filename)
            continue

        upload_file_to_s3(
            f, f"s3://{S3_BUCKET}/{S3_PREFIX}/sentinel-2/{subfolder}/{filename}"
        )
        uploaded += 1

    print(f"\\nUploaded {uploaded} COG(s) to s3://{S3_BUCKET}/{S3_PREFIX}/sentinel-2/")
    if skipped:
        raise RuntimeError(
            f"{len(skipped)} COG(s) had no product folder and were NOT "
            f"uploaded: {skipped}. Add the token to PRODUCT_FOLDERS -- a "
            f"silent skip is how a whole product goes missing."
        )
'''),
]


def main():
    nb = {
        "cells": CELLS,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3.12"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    with open(OUT, "w") as fh:
        json.dump(nb, fh, indent=1)
        fh.write("\n")
    print(f"wrote {OUT} ({len(CELLS)} cells)")


if __name__ == "__main__":
    main()
