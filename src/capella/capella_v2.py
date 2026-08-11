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


# Single source of truth for Capella's nodata sentinel: sigmaCalib writes it
# into the border pixels and process_capella declares it on the COG. They must
# agree or the border is unmaskable. -9999.0 (not 0) because the product is
# float32 dB backscatter, where 0 dB is a legitimate value -- see CLAUDE.md
# "Capella & Umbra SAR CLIs default -nodata to -9999.0".
CAPELLA_NODATA = -9999.0


def retrieve_capella_resources(
    date: Union[str, datetime],
    bucket: str = "csdap-capellaspace-delivery",
    prefix: str = "disasters"
) -> list[str]:
    """Return every Capella tif for the acquisition closest to ``date``.

    One acquisition can appear under more than one folder (different processing
    levels -- e.g. ``_GEO_`` and ``_SLC_``), so all folders whose timestamp
    matches are pooled into one flat list. ``sigmaCalib`` reads the ``_GEO_``
    band; pass the result through :func:`group_capella_scenes` to split it into
    one group per GEO band (i.e. per genuine scene). Real-case S3 keys preserved.
    """

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


def group_capella_scenes(tifs: list[str]) -> list[list[str]]:
    """Split pooled Capella tifs into one group per scene (one per GEO band).

    ``sigmaCalib`` only reads the ``_GEO_`` band, so each GEO file is a distinct
    scene; folders without a GEO band (e.g. an ``_SLC_``-only level) contribute
    nothing. Returns one single-element ``[geo]`` list per GEO band so the caller
    loops and emits one COG per scene instead of silently keeping only the first.
    """
    return [[geo] for geo in tifs if "_GEO_" in geo]


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
    """NaN-aware Lee speckle filter (window ``size`` x ``size``).

    Mirrors ``satellogic_v2.apply_lee_filter``: invalid (non-finite) pixels are
    ignored rather than counted as zero, so a nodata border doesn't bleed into
    the filtered interior. Applied to the linear backscatter before the dB
    conversion in the calibration functions below.
    """
    valid = np.isfinite(img)
    if not valid.any():
        return img

    v = valid.astype(np.float64)
    filled = np.where(valid, img, 0.0).astype(np.float64)

    # uniform_filter returns the window MEAN; dividing the filled mean by the
    # valid-fraction recovers the mean over valid pixels only (window size
    # cancels), so invalid neighbours are ignored rather than counted as zero.
    frac = uniform_filter(v, size, mode="constant")
    safe = frac > 0

    local_mean = np.zeros_like(filled)
    local_mean[safe] = uniform_filter(filled, size, mode="constant")[safe] / frac[safe]

    local_sqr = np.zeros_like(filled)
    local_sqr[safe] = uniform_filter(filled ** 2, size, mode="constant")[safe] / frac[safe]

    local_var = np.clip(local_sqr - local_mean ** 2, 0.0, None)
    overall_var = img[valid].var()

    weights = local_var / (local_var + overall_var + 1e-12)
    out = local_mean + weights * (filled - local_mean)

    return np.where(valid, out, np.nan)


def sigmaCalib(
    s3_image_paths: list[str],
    save_location: str = "/tmp/s3_temp",
    filter_size: int = 5,
) -> tuple[str, str]:

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

    print(f"DN range: {np.min(dn)} -> {np.max(dn)}")

    image_desc_str = ds.GetMetadataItem("TIFFTAG_IMAGEDESCRIPTION")

    scale_factor = None

    if image_desc_str:

        metadata_dict = json.loads(image_desc_str)

        try:

            scale_factor = metadata_dict["collect"]["image"]["scale_factor"]

            print(f"Scale factor: {scale_factor}")

        except KeyError as e:

            raise RuntimeError(f"Could not locate scale_factor: {e}")

    if scale_factor is None:
        raise RuntimeError("scale_factor could not be parsed")

    # ----------------------------------------------------
    # Convert to linear sigma first
    # ----------------------------------------------------
    sigma_linear = scale_factor * dn

    # Speckle filtering is ALWAYS applied -- there is no opt-out, only a kernel
    # size. Same treatment as Umbra (.clinerules.md rule 31): it runs on the
    # LINEAR backscatter, before the dB conversion, so the filter averages
    # physical power rather than logarithms.
    sigma_linear = lee_filter(sigma_linear, size=filter_size)
    filt = f"_filtered{filter_size}"

    # Prevent log10(0)
    sigma_linear = np.clip(sigma_linear, 1e-10, None)

    # Convert to dB
    sigma_0 = 20.0 * np.log10(sigma_linear)

    # The vendor GEO border is 0, which the 1e-10 clip above turns into -200 dB.
    # Write the declared nodata sentinel there so the border is actually
    # maskable downstream. Two earlier approaches both produced a
    # plausible-looking but NON-maskable value: clipping to a -60 dB floor, and
    # substituting the smallest valid sample. Neither matched the nodata the COG
    # declares, so consumers rendered the border as real backscatter and it
    # dragged the display stretch down.
    sigma_0[~np.isfinite(sigma_0) | (sigma_linear <= 1e-10)] = CAPELLA_NODATA

    finite = sigma_0[sigma_0 != CAPELLA_NODATA]
    if finite.size:
        print(f"Sigma0 range: {np.nanmin(finite)} -> {np.nanmax(finite)} dB "
              f"(nodata {CAPELLA_NODATA})")
    else:
        print(f"Sigma0: no valid pixels (all {CAPELLA_NODATA})")

    base = os.path.basename(in_file)

    parts = base.replace(".tif", "").split("_")

    satellite = parts[1]
    start_time = parts[5]

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

    return outfile, in_file
