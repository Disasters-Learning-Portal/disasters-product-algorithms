import os
import re
import numpy as np
from osgeo import gdal
from datetime import datetime
import json

from shared_utils.s3utils import *
from shared_utils.geotools import *

# Constants
DEFAULT_SCALE_FACTOR = 0.0001
NODATA_FLOAT = -9999.0

# Retieving satellogic data from S3
def retrieve_satellogic_resources(date, level, bucket="csda-data-vendor-satellogic", prefix="disasters"):
    files = retrieve_s3_file_list(bucket, prefix)

    filtered_files = [
        x for x in files
        if len(x.split("/")) > 1 and f"_{level}_" in x.split("/")[1]
    ]

    subdirs = sorted(set(x.split("/")[1] for x in filtered_files))

    dates = [
        datetime.strptime(f"{x.split('_')[0]}_{x.split('_')[1]}", "%Y%m%d_%H%M%S")
        for x in subdirs
    ]

    if isinstance(date, str):
        date = datetime.strptime(date, "%Y-%m-%d %H:%M:%S")

    closest_date = min(dates, key=lambda d: abs(d - date))
    date_prefix = closest_date.strftime("%Y%m%d_%H%M%S")

    selected = [x for x in subdirs if x.startswith(date_prefix)][0]

    selected_files = [
        x for x in filtered_files
        if len(x.split("/")) > 1 and x.split("/")[1] == selected
    ]

    metadata = [
        x for x in selected_files
        if not x.lower().endswith((".tif", ".tiff"))
    ]

    tifs = [
        x for x in selected_files
        if x.lower().endswith((".tif", ".tiff"))
    ]

    print(f"Selected Satellogic folder: {selected}")
    print(f"Metadata files found: {len(metadata)}")
    print(f"TIF files found: {len(tifs)}")
    for t in tifs:
        print("  ", t)

    return (
        [f"s3://{bucket}/{x}" for x in metadata],
        [f"s3://{bucket}/{x}" for x in tifs],
    )


def report_satellogic_scenes(
    level,
    bucket="csda-data-vendor-satellogic",
    prefix="disasters",
) -> list[dict]:
    """List the Satellogic scenes available in the vendor bucket, newest first.

    Only scenes matching the given processing ``level`` (e.g. ``L1D``/``L1B``)
    are reported -- the level is encoded in the scene folder name, mirroring
    ``retrieve_satellogic_resources``. "Newest" is by S3 delivery time (the most
    recent ``LastModified`` across a scene's objects) -- i.e. when the vendor
    added it to the bucket -- so the top rows are the scenes closest to today.
    Each scene's acquisition datetime is parsed from the folder name (the value
    you pass back as ``--date``).

    Returns a list of dicts sorted by ``added_to_s3`` descending::

        {"date": "2026-04-18 19:33:05",         # pass back as --date
         "scene": "20260418_193305_..._L1D_...", # S3 scene folder name
         "acquired": datetime(...),              # acquisition time from the key
         "added_to_s3": datetime(...)}           # newest LastModified for the scene
    """
    pairs = retrieve_s3_file_list_with_timestamps(bucket, prefix)

    # scene subdir (parts[1], filtered by level as in
    # retrieve_satellogic_resources) -> newest LastModified among its objects.
    latest: dict = {}
    for key, last_modified in pairs:
        parts = key.split("/")
        if len(parts) <= 1 or f"_{level}_" not in parts[1]:
            continue
        subdir = parts[1]
        if subdir not in latest or last_modified > latest[subdir]:
            latest[subdir] = last_modified

    scenes = []
    for subdir, added in latest.items():
        tokens = subdir.split("_")
        try:
            acquired = datetime.strptime(f"{tokens[0]}_{tokens[1]}", "%Y%m%d_%H%M%S")
        except (IndexError, ValueError):
            continue  # subdir doesn't carry a parseable acquisition date
        scenes.append({
            "date": acquired.strftime("%Y-%m-%d %H:%M:%S"),
            "scene": subdir,
            "acquired": acquired,
            "added_to_s3": added,
        })

    scenes.sort(key=lambda s: s["added_to_s3"], reverse=True)
    return scenes

# Functions to retrieve metedata
def getSolarZenithAngle(meta):
    angle_files = [x for x in meta if x.endswith("_angles.geojson")]

    if not angle_files:
        print("WARNING: No solar angle metadata found.")
        return None

    try:
        data = json.loads(read_s3_file(angle_files[0], "utf-8"))
        return float(data["features"][0]["properties"]["solar"]["zenith"])

    except Exception as e:
        print(f"WARNING: Failed to parse solar angles: {e}")
        return None


def getScaleFactor(meta):
    metadata_json = [x for x in meta if (x.endswith(".json") or x.endswith(".geojson"))]

    for fp in metadata_json:
        try:
            data = json.loads(read_s3_file(fp, "utf-8"))

            if "radiometric_scale_factor" in data:
                sf = float(data["radiometric_scale_factor"])
                print(f"Using metadata scale factor: {sf}")
                return sf

        except Exception:
            pass

    print(f"Using default scale factor: {DEFAULT_SCALE_FACTOR}")
    return DEFAULT_SCALE_FACTOR


def infer_processing_level(paths):
    joined = " ".join(paths)

    if "_L1D_" in joined:
        return "L1D"
    if "_L1B_" in joined:
        return "L1B"

    return "UNKNOWN"


# Loading reflectance
def load_reflectance_band(ds, band_num, scale_factor):
    arr = ds.GetRasterBand(band_num).ReadAsArray().astype(np.float32)
    arr[arr == 0] = np.nan
    arr *= scale_factor
    return np.clip(arr, 0, 1)


# Applying solar zenith correction
def apply_solar_correction(arrays, sunzen):
    if sunzen is None:
        print("Skipping solar zenith correction.")
        return arrays

    scale = np.cos(np.radians(sunzen))

    if scale <= 0:
        print("Invalid solar correction scale.")
        return arrays

    print(f"Applying solar zenith correction: {sunzen:.2f}°")
    return [a / scale for a in arrays]


# Applying cloud mask
def apply_mask(arrays, cloud):
    mask = cloud != 1
    return [np.where(mask, np.nan, a) for a in arrays]


# Normalizing the bands for composite imagery
def normalize_band(band, p_low=2, p_high=98):
    valid = band[np.isfinite(band)]

    if valid.size == 0:
        return np.zeros_like(band)

    lo = np.percentile(valid, p_low)
    hi = np.percentile(valid, p_high)

    if hi <= lo:
        return np.zeros_like(band)

    return np.clip((band - lo) / (hi - lo), 0, 1)


# Applying a Gamma correction to enhance image contrast
def apply_gamma(img, gamma=1.0):
    if gamma == 1.0:
        return img

    return np.power(np.clip(img, 0, 1), 1.0 / gamma)


def prepare_scene(paths, meta, use_mask=True):
    level = infer_processing_level(paths)
    print(f"Detected processing level: {level}")

    scale_factor = getScaleFactor(meta)
    sunzen = getSolarZenithAngle(meta)

    print("Available paths:")
    for p in paths:
        print(p)

    # Support both newer L1D_SR products and older L1B products
    image_files = [
        x for x in paths
        if (
            x.lower().endswith("_analytic.tif")
            or x.lower().endswith("_toa_0.tif")
        )
    ]

    if not image_files:
        raise FileNotFoundError(
            "No Analytic or TOA tif found. Available paths:\n"
            + "\n".join(paths)
        )

    in_file = download_s3_file(image_files[0])
    ds = gdal.Open(in_file)

    # Only fetch + open the cloud band when it will actually be applied.
    # Color composites (truecolor/colorir) pass use_mask=False and never touch
    # `cloud`, so skip the download/open entirely. When masking is requested but
    # no cloud tif exists, `cloud` stays None (never gdal.Open(None)).
    cloud = None
    if use_mask:
        cloud_files = [
            x for x in paths
            if (
                x.lower().endswith("_cloud.tif")
                or x.lower().endswith("_cloud_0.tif")
            )
        ]

        if cloud_files:
            cloud_file = download_s3_file(cloud_files[0])
            dc = gdal.Open(cloud_file)
            cloud = dc.GetRasterBand(1).ReadAsArray()
        else:
            print("No cloud tif found for this scene; proceeding without a mask.")

    return (ds, cloud, in_file, level, scale_factor, sunzen)


def maybe_correct(arrays, level, sunzen):
    if level == "L1B":
        return apply_solar_correction(arrays, sunzen)

    print("Skipping solar correction for L1D.")
    return arrays


# Proper file naming conventions
def build_output_name(in_file, out_dir, product):
    """Derive the output COG name from a Satellogic image basename.

    Target (analytic-tiled, verified against real outputs):
      20260627_140714_051_SN33_L1D_MS_19N_724_1158_analytic.tif
        -> Satellogic_SN33_<product>_051_724_1158_2026-06-27T14:07:14Z.tif

    Fields are extracted by pattern, not fixed index: satellite (SN\\d+),
    capture-id (the 3-digit token right after the timestamp; absent on L1B),
    and the UTM tile's col/row (the two numeric groups of \\d+[A-Z]_(\\d+)_(\\d+);
    the zone and level are dropped). The band token (TOA/analytic) is ignored —
    the product comes from `product`. Empty tokens are dropped (no `__`), and the
    trailing ISO-8601-Zulu is the "already-named" completion marker. Vendor TOA
    scenes have no UTM tile, so they degrade cleanly to
    Satellogic_<SAT>_<product>_<capture>_<ISO>.tif with no bogus col/row.
    """
    fname = os.path.basename(in_file)
    parts = fname.split("_")

    dt = datetime.strptime("_".join(parts[0:2]), "%Y%m%d_%H%M%S")

    sat_m = re.search(r"SN\d+", fname)
    satellite = sat_m.group(0) if sat_m else "SNXX"

    # capture-id: 3-digit token after the timestamp (L1D layouts); absent on L1B
    capture_id = parts[2] if len(parts) > 2 and parts[2].isdigit() else ""

    # UTM tile col_row (analytic-tiled layout); zone dropped. Absent on TOA scenes.
    tile_m = re.search(r"\d+[A-Z]_(\d+)_(\d+)", fname)
    col_row = f"{tile_m.group(1)}_{tile_m.group(2)}" if tile_m else ""

    tokens = [t for t in ("Satellogic", satellite, product, capture_id, col_row) if t]
    stamp = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    return f"{out_dir}/{'_'.join(tokens)}_{stamp}.tif"


# Functions for specific products

def genTrueColor(paths, meta, out="/tmp/s3_temp", visualize=True, gamma=0.7):
    ds, cloud, in_file, level, scale_factor, sunzen = prepare_scene(paths, meta, use_mask)

    red = load_reflectance_band(ds, 3, scale_factor)
    green = load_reflectance_band(ds, 2, scale_factor)
    blue = load_reflectance_band(ds, 1, scale_factor)

    red, green, blue = maybe_correct([red, green, blue], level, sunzen)

    if visualize:
        r = normalize_band(red)
        g = normalize_band(green)
        b = normalize_band(blue)
        rgb = apply_gamma(np.dstack([r, g, b]), gamma)

    else:
        rgb = np.clip(np.dstack([red, green, blue]), 0, 1)

    out_img = (rgb * 255).astype(np.uint8)

    outfile = build_output_name(in_file, out, "truecolor")

    dump_geotiff_rgb(outfile, out_img[..., 0], out_img[..., 1], out_img[..., 2], ds.GetProjection(), ds.GetGeoTransform())

    return outfile


def gencolorIR(paths, meta, out="/tmp/s3_temp", visualize=True, gamma=0.7):
    ds, cloud, in_file, level, scale_factor, sunzen = prepare_scene(paths, meta, use_mask)

    nir = load_reflectance_band(ds, 4, scale_factor)
    red = load_reflectance_band(ds, 3, scale_factor)
    green = load_reflectance_band(ds, 2, scale_factor)

    nir, red, green = maybe_correct([nir, red, green], level, sunzen)

    if visualize:
        r = normalize_band(nir)
        g = normalize_band(red)
        b = normalize_band(green)
        rgb = apply_gamma(np.dstack([r, g, b]), gamma)

    else:
        rgb = np.clip(np.dstack([nir, red, green]), 0, 1)

    out_img = (rgb * 255).astype(np.uint8)

    outfile = build_output_name(in_file, out, "colorir")

    dump_geotiff_rgb(outfile, out_img[..., 0], out_img[..., 1], out_img[..., 2], ds.GetProjection(), ds.GetGeoTransform())

    return outfile


def genNDVI(paths, meta, out="/tmp/s3_temp"):
    ds, cloud, in_file, level, scale_factor, sunzen = prepare_scene(paths, meta, use_mask)

    nir = load_reflectance_band(ds, 4, scale_factor)
    red = load_reflectance_band(ds, 3, scale_factor)

    nir, red = maybe_correct([nir, red], level, sunzen)

    if cloud is not None:
        nir, red = apply_mask([nir, red], cloud)

    ndvi = (nir - red) / (nir + red + 1e-10)
    ndvi = np.clip(ndvi, -1, 1)
    ndvi[np.isnan(ndvi)] = NODATA_FLOAT

    outfile = build_output_name(in_file, out, "ndvi")

    dump_geotiff_float(outfile, ndvi, ds.GetProjection(), ds.GetGeoTransform())

    return outfile


def genNDWI(paths, meta, out="/tmp/s3_temp"):
    ds, cloud, in_file, level, scale_factor, sunzen = prepare_scene(paths, meta, use_mask)

    nir = load_reflectance_band(ds, 4, scale_factor)
    green = load_reflectance_band(ds, 2, scale_factor)

    nir, green = maybe_correct([nir, green], level, sunzen)

    if cloud is not None:
        nir, green = apply_mask([nir, green], cloud)

    ndwi = (green - nir) / (green + nir + 1e-10)
    ndwi = np.clip(ndwi, -1, 1)
    ndwi[np.isnan(ndwi)] = NODATA_FLOAT

    outfile = build_output_name(in_file, out, "ndwi")

    dump_geotiff_float(outfile, ndwi, ds.GetProjection(), ds.GetGeoTransform())

    return outfile


def genEVI(paths, meta, out="/tmp/s3_temp"):
    ds, cloud, in_file, level, scale_factor, sunzen = prepare_scene(paths, meta, use_mask)

    blue = load_reflectance_band(ds, 1, scale_factor)
    red = load_reflectance_band(ds, 3, scale_factor)
    nir = load_reflectance_band(ds, 4, scale_factor)

    blue, red, nir = maybe_correct([blue, red, nir], level, sunzen)

    if cloud is not None:
        blue, red, nir = apply_mask([blue, red, nir], cloud)

    denom = nir + 6 * red - 7.5 * blue + 1

    evi = 2.5 * (nir - red) / (denom + 1e-10)
    evi = np.clip(evi, -1, 1)
    evi[np.isnan(evi)] = NODATA_FLOAT

    outfile = build_output_name(in_file, out, "evi")

    dump_geotiff_float(outfile, evi, ds.GetProjection(), ds.GetGeoTransform())

    return outfile