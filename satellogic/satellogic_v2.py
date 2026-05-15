import os
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

    filtered_files = [x for x in files if f"_{level}_" in x.split("/")[1]]

    subdirs = list(set([x.split("/")[1] for x in filtered_files]))

    dates = [datetime.strptime(f"{x.split('_')[0]}_{x.split('_')[1]}", "%Y%m%d_%H%M%S") for x in subdirs]

    if isinstance(date, str):
        date = datetime.strptime(date, "%Y-%m-%d %H:%M:%S")

    closest_date = min(dates, key=lambda d: abs(d - date))

    date_prefix = closest_date.strftime("%Y%m%d_%H%M%S")

    selected = [x for x in subdirs if x.startswith(date_prefix)][0]

    metadata = [x for x in filtered_files if (x.split("/")[1] == selected and x.split("/")[2] != "rasters")]

    tifs = [x for x in filtered_files if (x.split("/")[1] == selected and x.split("/")[2] == "rasters" and x.endswith(".tif"))]

    return ([f"s3://{bucket}/{x}" for x in metadata], [f"s3://{bucket}/{x}" for x in tifs])

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


def prepare_scene(paths, meta):
    level = infer_processing_level(paths)
    print(f"Detected processing level: {level}")

    scale_factor = getScaleFactor(meta)
    sunzen = getSolarZenithAngle(meta)

    in_file = download_s3_file([x for x in paths if x.endswith("_TOA_0.tif")][0])
    cloud_file = download_s3_file([x for x in paths if x.endswith("_CLOUD_0.tif")][0])

    ds = gdal.Open(in_file)
    dc = gdal.Open(cloud_file)

    cloud = dc.GetRasterBand(1).ReadAsArray()

    return (ds, cloud, in_file, level, scale_factor, sunzen)


def maybe_correct(arrays, level, sunzen):
    if level == "L1B":
        return apply_solar_correction(arrays, sunzen)

    print("Skipping solar correction for L1D.")
    return arrays


# Proper file naming conventions
def build_output_name(in_file, out_dir, product):
    fname = in_file.split("/")[-1]

    dt = datetime.strptime("_".join(fname.split("_")[0:2]), "%Y%m%d_%H%M%S")

    return f"{out_dir}/{dt.strftime('%Y%m')}_Satellogic_{fname.split('_')[4]}_{product}{dt.strftime('%Y-%m-%dT%H:%M:%SZ')}.tif"


# Functions for specific products

def genTrueColor(paths, meta, out="./s3_temp", use_mask=True, visualize=True, gamma=0.7):
    ds, cloud, in_file, level, scale_factor, sunzen = prepare_scene(paths, meta)

    red = load_reflectance_band(ds, 3, scale_factor)
    green = load_reflectance_band(ds, 2, scale_factor)
    blue = load_reflectance_band(ds, 1, scale_factor)

    red, green, blue = maybe_correct([red, green, blue], level, sunzen)

    if use_mask:
        red, green, blue = apply_mask([red, green, blue], cloud)

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


def gencolorIR(paths, meta, out="./s3_temp", use_mask=True, visualize=True, gamma=0.7):
    ds, cloud, in_file, level, scale_factor, sunzen = prepare_scene(paths, meta)

    nir = load_reflectance_band(ds, 4, scale_factor)
    red = load_reflectance_band(ds, 3, scale_factor)
    green = load_reflectance_band(ds, 2, scale_factor)

    nir, red, green = maybe_correct([nir, red, green], level, sunzen)

    if use_mask:
        nir, red, green = apply_mask([nir, red, green], cloud)

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


def genNDVI(paths, meta, out="./s3_temp", use_mask=True):
    ds, cloud, in_file, level, scale_factor, sunzen = prepare_scene(paths, meta)

    nir = load_reflectance_band(ds, 4, scale_factor)
    red = load_reflectance_band(ds, 3, scale_factor)

    nir, red = maybe_correct([nir, red], level, sunzen)

    if use_mask:
        nir, red = apply_mask([nir, red], cloud)

    ndvi = (nir - red) / (nir + red + 1e-10)
    ndvi = np.clip(ndvi, -1, 1)
    ndvi[np.isnan(ndvi)] = NODATA_FLOAT

    outfile = build_output_name(in_file, out, "ndvi")

    dump_geotiff_float(outfile, ndvi, ds.GetProjection(), ds.GetGeoTransform())

    return outfile


def genNDWI(paths, meta, out="./s3_temp", use_mask=True):
    ds, cloud, in_file, level, scale_factor, sunzen = prepare_scene(paths, meta)

    nir = load_reflectance_band(ds, 4, scale_factor)
    green = load_reflectance_band(ds, 2, scale_factor)

    nir, green = maybe_correct([nir, green], level, sunzen)

    if use_mask:
        nir, green = apply_mask([nir, green], cloud)

    ndwi = (green - nir) / (green + nir + 1e-10)
    ndwi = np.clip(ndwi, -1, 1)
    ndwi[np.isnan(ndwi)] = NODATA_FLOAT

    outfile = build_output_name(in_file, out, "ndwi")

    dump_geotiff_float(outfile, ndwi, ds.GetProjection(), ds.GetGeoTransform())

    return outfile


def genEVI(paths, meta, out="./s3_temp", use_mask=True):
    ds, cloud, in_file, level, scale_factor, sunzen = prepare_scene(paths, meta)

    blue = load_reflectance_band(ds, 1, scale_factor)
    red = load_reflectance_band(ds, 3, scale_factor)
    nir = load_reflectance_band(ds, 4, scale_factor)

    blue, red, nir = maybe_correct([blue, red, nir], level, sunzen)

    if use_mask:
        blue, red, nir = apply_mask([blue, red, nir], cloud)

    denom = nir + 6 * red - 7.5 * blue + 1

    evi = 2.5 * (nir - red) / (denom + 1e-10)
    evi = np.clip(evi, -1, 1)
    evi[np.isnan(evi)] = NODATA_FLOAT

    outfile = build_output_name(in_file, out, "evi")

    dump_geotiff_float(outfile, evi, ds.GetProjection(), ds.GetGeoTransform())

    return outfile