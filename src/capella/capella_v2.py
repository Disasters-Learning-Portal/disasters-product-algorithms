"""
capella_v2.py

Utilities for retrieving and processing Capella SAR products.
"""

import json
import os

import numpy as np
from glob import glob
from typing import Union
from datetime import datetime
from osgeo import gdal

from scipy.ndimage import uniform_filter
from scipy.ndimage import variance

from shared_utils.geotools import *
from shared_utils.s3utils import *


def retrieve_capella_resources(
    date: Union[str, datetime],
    bucket: str = "csdap-capellaspace-delivery",
    prefix: str = "disasters"
) -> list[str]:

    files = retrieve_s3_file_list(bucket, prefix)

    filtered_files = [x for x in files if len(x.split("/")) > 2]

    subdirs = list(set([x.split("/")[1] for x in filtered_files]))

    dates = [datetime.strptime(x.split("_")[5], "%Y%m%d%H%M%S") for x in subdirs]

    if isinstance(date, str):
        date = datetime.strptime(date, "%Y%m%d%H%M%S")

    closest_date = min(dates, key=lambda d: abs(d - date))

    date_prefix = closest_date.strftime("%Y%m%d%H%M%S")

    selected_subdirs = [x for x in subdirs if x.split("_")[5] == date_prefix]

    tifs = []

    for selected_subdir in selected_subdirs:
        for file in filtered_files:
            if (
                (file.split("/")[1] == selected_subdir)
                and file.lower().endswith(".tif")
                and ("_preview.tif" not in file.lower())
            ):
                tifs.append(file)

    tifs = [f"s3://{bucket}/{x}" for x in tifs]

    return tifs


def report_capella_scenes(
    bucket: str = "csdap-capellaspace-delivery",
    prefix: str = "disasters",
) -> list[dict]:
    """List the Capella scenes available in the vendor bucket, newest first.

    "Newest" is by S3 delivery time (the most recent ``LastModified`` across a
    scene's objects) -- i.e. when the vendor added it to the bucket -- so the
    top rows are the scenes closest to today. Each scene's acquisition datetime
    is also parsed from the folder name (the value you pass back as ``--date``).

    Returns a list of dicts sorted by ``added_to_s3`` descending::

        {"date": "20231107120000",          # pass back as --date
         "scene": "CAPELLA_..._20231107120000_...",  # S3 scene folder name
         "acquired": datetime(...),          # acquisition time from the key
         "added_to_s3": datetime(...)}       # newest LastModified for the scene
    """
    pairs = retrieve_s3_file_list_with_timestamps(bucket, prefix)

    # scene subdir (parts[1], as in retrieve_capella_resources) -> newest
    # LastModified seen among its objects.
    latest: dict = {}
    for key, last_modified in pairs:
        parts = key.split("/")
        if len(parts) <= 2 or not parts[1]:
            continue
        subdir = parts[1]
        if subdir not in latest or last_modified > latest[subdir]:
            latest[subdir] = last_modified

    scenes = []
    for subdir, added in latest.items():
        try:
            acquired = datetime.strptime(subdir.split("_")[5], "%Y%m%d%H%M%S")
        except (IndexError, ValueError):
            continue  # subdir doesn't carry a parseable acquisition date
        scenes.append({
            "date": acquired.strftime("%Y%m%d%H%M%S"),
            "scene": subdir,
            "acquired": acquired,
            "added_to_s3": added,
        })

    scenes.sort(key=lambda s: s["added_to_s3"], reverse=True)
    return scenes


def lee_filter(img: np.ndarray, size: int) -> np.ndarray:

    img_mean = uniform_filter(img, (size, size))

    img_sqr_mean = uniform_filter(img**2, (size, size))

    img_variance = img_sqr_mean - img_mean**2

    overall_variance = variance(img)

    img_weights = img_variance / (img_variance + overall_variance)

    img_output = img_mean + img_weights * (img - img_mean)

    return img_output


def sigmaCalib(
    s3_image_paths: list[str],
    save_location: str = "/tmp/s3_temp",
    do_filt : bool = True,
    filter_size : int = 5
) -> str:

    if save_location.endswith("/"):
        save_location = save_location[:-1]

    os.makedirs(save_location, exist_ok=True)

    print("Collecting needed files...")

    in_filepath = [x for x in s3_image_paths if "_GEO_" in x][0]

    local_file = f"{save_location}/{local_tif_basename(in_filepath)}"

    if local_file not in glob(f"{save_location}/*"):

        print("GEO file not found, downloading from S3")

        in_file = download_s3_file(in_filepath)

    else:

        print("GEO file found, proceeding")

        in_file = local_file

    print("Generating Sigma Naught")

    print("\n\t* Opening GEO File")

    ds = gdal.Open(in_file)

    cols = ds.RasterXSize
    rows = ds.RasterYSize

    in_geo = ds.GetGeoTransform()

    projref = ds.GetProjectionRef()

    dn = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)

    print("DN range: " f"{np.min(dn)} -> {np.max(dn)}")

    image_desc_str = ds.GetMetadataItem("TIFFTAG_IMAGEDESCRIPTION")

    scale_factor = None

    if image_desc_str:

        metadata_dict = json.loads(image_desc_str)

        try:

            scale_factor = metadata_dict["collect"]["image"]["scale_factor"]

            print("Scale factor: " f"{scale_factor}")

        except KeyError as e:

            raise RuntimeError(f"Could not locate scale_factor: {e}")

    if scale_factor is None:
        raise RuntimeError("scale_factor could not be parsed")

    filt = ""
    if do_filt:
        dn = lee_filter(dn, size = filter_size)
        filt = f"_filtered{filter_size}"

    sigma_0 = 20.0 * np.log10(scale_factor * dn)

    sigma_0 = np.clip(sigma_0, -60.0, np.nanmax(sigma_0))

    print("Sigma0 range: " f"{np.nanmin(sigma_0)} -> {np.nanmax(sigma_0)}")

    base = os.path.basename(in_file)

    parts = base.replace(".tif", "").split("_")

    satellite = parts[1]          # C18
    start_time = parts[5]         # 20260418193305

    dt = datetime.strptime(start_time, "%Y%m%d%H%M%S")

    outfile = (
        f"{save_location}/"
        f"{dt.strftime('%Y%m')}_"
        f"Capella-{satellite.replace('C', '')}_"
        f"sigma0"
        f"{dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"{filt}"
        ".tif"
    )

    dump_geotiff_float(outfile, sigma_0, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")

    return outfile
