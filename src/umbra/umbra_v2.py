import os
import sys                                     
import numpy as np
from osgeo import gdal, osr
from PIL import Image, ImageEnhance
import xml.etree.ElementTree as ET
import shutil
import rasterio as rio
from rasterio.merge import merge
from rasterio.warp import calculate_default_transform, reproject
from rasterio.enums import Resampling
from pathlib import Path
import requests
import shutil
from scipy.signal import medfilt2d
from scipy.ndimage import uniform_filter
from pyproj import Transformer
import geopandas as gpd
from shapely.geometry import box
import matplotlib.pyplot as plt
import json

from glob import glob
from typing import Union
from datetime import datetime
from shared_utils.geotools import *
from shared_utils.s3utils import *
from shared_utils.file_naming import create_sar_output_filename

def retrieve_umbra_resources(date : Union[str, datetime], bucket : str = "csda-data-vendor-umbra", prefix : str = "disasters") -> list[str]:
    """Return every Umbra tif for the acquisition closest to ``date``.

    All scene folders whose timestamp matches are pooled into one flat list --
    the old code took only ``selected_subdir[...][0]``, silently dropping any
    others. The calibration functions read the ``_gec.tif`` band; pass the result
    through :func:`group_umbra_scenes` to split it into one group per GEC band
    (i.e. per genuine scene). Real-case S3 keys preserved so fetches don't 404.
    """
    files = retrieve_s3_file_list(bucket, prefix)
    filtered_files = [x for x in files if len(x.split("/")) > 2]
    subdirs = list(set([x.split("/")[2] for x in filtered_files]))
    dates = [datetime.strptime(x.split('_')[0], "%Y-%m-%d-%H-%M-%S") for x in subdirs]

    if type(date) is str:
        date = datetime.strptime(date, "%Y-%m-%d %H:%M:%S")

    closest_date = min(dates, key=lambda d: abs(d - date))
    date_prefix = closest_date.strftime("%Y-%m-%d-%H-%M-%S")

    selected_subdirs = [x for x in subdirs if x.startswith(date_prefix)]

    tifs = [
        f"s3://{bucket}/{x}"
        for x in filtered_files
        if ((x.split("/")[2] in selected_subdirs) and (x.lower().endswith(".tif")))
    ]

    return tifs


def group_umbra_scenes(tifs: list[str]) -> list[list[str]]:
    """Split pooled Umbra tifs into one group per scene (one per GEC band).

    The calibration functions (``sigmaCalib``/``betaCalib``/``gammaCalib``)
    read the ``_gec.tif`` band, so each GEC file is a distinct
    scene; folders without one contribute nothing. Returns one single-element
    ``[gec]`` list per GEC band so the caller loops and emits one COG per scene
    instead of silently keeping only the first.
    """
    return [[gec] for gec in tifs if gec.lower().endswith("_gec.tif")]

def report_umbra_scenes(
    bucket: str = "csda-data-vendor-umbra",
    prefix: str = "disasters",
) -> list[dict]:
    """List the Umbra scenes available in the vendor bucket, newest first.

    "Newest" is by S3 delivery time (the most recent ``LastModified`` across a
    scene's objects) -- i.e. when the vendor added it to the bucket -- so the
    top rows are the scenes closest to today. Each scene's acquisition datetime
    is parsed from the folder name (the value you pass back as ``--date``).

    Returns a list of dicts sorted by ``added_to_s3`` descending::

        {"date": "2026-04-18 19:33:05",         # pass back as --date
         "scene": "2026-04-18-19-33-05_...",     # S3 scene folder name
         "acquired": datetime(...),              # acquisition time from the key
         "added_to_s3": datetime(...)}           # newest LastModified for the scene
    """
    pairs = retrieve_s3_file_list_with_timestamps(bucket, prefix)

    # scene subdir (parts[2], as in retrieve_umbra_resources) -> newest
    # LastModified seen among its objects.
    latest: dict = {}
    for key, last_modified in pairs:
        parts = key.split("/")
        if len(parts) <= 2 or not parts[2]:
            continue
        subdir = parts[2]
        if subdir not in latest or last_modified > latest[subdir]:
            latest[subdir] = last_modified

    scenes = []
    for subdir, added in latest.items():
        try:
            acquired = datetime.strptime(subdir.split("_")[0], "%Y-%m-%d-%H-%M-%S")
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


def _umbra_output_name(in_file: str, product: str, filter_size: int) -> str:
    """Output basename for one calibrated Umbra product.

    The vendor GEC basename leads with the acquisition stamp and the satellite:
    ``2026-08-05-03-54-47_UMBRA-07_GEC.tif``. Both the ``YYYYMM`` head and the
    trailing ISO-Zulu stamp come from that one datetime, so it is parsed once
    here rather than twice per f-string as the three calib functions used to do.
    """
    basename = os.path.basename(in_file)
    acquired = datetime.strptime(basename.split("_")[0], "%Y-%m-%d-%H-%M-%S")
    platform = basename.split("_")[1].capitalize()
    return create_sar_output_filename(platform, product, acquired, filter_size)


def sigmaCalib(s3_image_paths : list[str], save_location : str = "/tmp/s3_temp", filter_size : int = 5):
    if save_location.endswith("/"):
        save_location = save_location[:-1]
    os.makedirs(save_location, exist_ok=True)
    print("Collecting needed files...")
    in_filepath = [x for x in s3_image_paths if x.lower().endswith("_gec.tif")][0]
    if f'/tmp/s3_temp/{local_tif_basename(in_filepath)}' not in glob("/tmp/s3_temp/*"):
        print("GEC file not found, downloading from s3")
        in_file = download_s3_file(in_filepath)
    else:
        print("GEC file found, proceeding")
        in_file = f'/tmp/s3_temp/{local_tif_basename(in_filepath)}'
    print('Generating Sigma Naught')
    print("\n\t* Opening GEC File")
    ds = gdal.Open(in_file)
    cols = ds.RasterXSize
    rows = ds.RasterYSize
    in_geo = ds.GetGeoTransform()
    projref = ds.GetProjectionRef()
    dn = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
    print(np.max(dn), np.min(dn))
    #print(cols, rows)
    
    metadata = ds.GetMetadata()
    print(metadata)
    
    sigma_val = ds.GetMetadataItem('DN_TO_SIGMA')
    print(sigma_val)
    print(type(sigma_val))

    # Filter the linear backscatter (always on), then convert to dB. Clipping
    # keeps log10 finite where the filtered value would otherwise hit 0.
    sigma_linear = float(sigma_val) * dn
    sigma_linear = lee_filter(sigma_linear, size=filter_size)
    sigma_linear = np.clip(sigma_linear, 1e-10, None)
    sigma_0 = 20. * np.log10(sigma_linear)
    print(np.nanmax(sigma_0), np.nanmin(sigma_0))

    outfile = os.path.join(
        save_location, _umbra_output_name(in_file, "sigma0", filter_size)
    )
    dump_geotiff_float(outfile, sigma_0, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")
    return outfile
    
def betaCalib(s3_image_paths : list[str], save_location : str = "/tmp/s3_temp", filter_size : int = 5):
    if save_location.endswith("/"):
        save_location = save_location[:-1]
    os.makedirs(save_location, exist_ok=True)
    print("Collecting needed files...")
    in_filepath = [x for x in s3_image_paths if x.lower().endswith("_gec.tif")][0]
    if f'/tmp/s3_temp/{local_tif_basename(in_filepath)}' not in glob("/tmp/s3_temp/*"):
        print("GEC file not found, downloading from s3")
        in_file = download_s3_file(in_filepath)
    else:
        print("GEC file found, proceeding")
        in_file = f'/tmp/s3_temp/{local_tif_basename(in_filepath)}'
    print('Generating Beta Naught')
    print("\n\t* Opening GEC File")
    ds = gdal.Open(in_file)
    cols = ds.RasterXSize
    rows = ds.RasterYSize
    in_geo = ds.GetGeoTransform()
    projref = ds.GetProjectionRef()
    dn = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
    print(np.max(dn), np.min(dn))
    #print(cols, rows)

    metadata = ds.GetMetadata()
    print(metadata)

    beta_val = ds.GetMetadataItem('DN_TO_BETA')
    print(beta_val)
    print(type(beta_val))

    beta_linear = float(beta_val) * dn
    beta_linear = lee_filter(beta_linear, size=filter_size)
    beta_linear = np.clip(beta_linear, 1e-10, None)
    beta_0 = 20. * np.log10(beta_linear)
    print(np.nanmax(beta_0), np.nanmin(beta_0))

    outfile = os.path.join(
        save_location, _umbra_output_name(in_file, "beta0", filter_size)
    )
    dump_geotiff_float(outfile, beta_0, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")
    return outfile

def gammaCalib(s3_image_paths : list[str], save_location : str = "/tmp/s3_temp", filter_size : int = 5):
    if save_location.endswith("/"):
        save_location = save_location[:-1]
    os.makedirs(save_location, exist_ok=True)
    print("Collecting needed files...")
    in_filepath = [x for x in s3_image_paths if x.lower().endswith("_gec.tif")][0]
    if f'/tmp/s3_temp/{local_tif_basename(in_filepath)}' not in glob("/tmp/s3_temp/*"):
        print("GEC file not found, downloading from s3")
        in_file = download_s3_file(in_filepath)
    else:
        print("GEC file found, proceeding")
        in_file = f'/tmp/s3_temp/{local_tif_basename(in_filepath)}'
    print('Generating Gamma Naught')
    print("\n\t* Opening GEC File")
    ds = gdal.Open(in_file)
    cols = ds.RasterXSize
    rows = ds.RasterYSize
    in_geo = ds.GetGeoTransform()
    projref = ds.GetProjectionRef()
    dn = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
    print(np.max(dn), np.min(dn))
    #print(cols, rows)

    metadata = ds.GetMetadata()
    print(metadata)

    gamma_val = ds.GetMetadataItem('DN_TO_GAMMA')
    print(gamma_val)
    print(type(gamma_val))

    gamma_linear = float(gamma_val) * dn
    gamma_linear = lee_filter(gamma_linear, size=filter_size)
    gamma_linear = np.clip(gamma_linear, 1e-10, None)
    gamma_0 = 20. * np.log10(gamma_linear)
    print(np.nanmax(gamma_0), np.nanmin(gamma_0))

    outfile = os.path.join(
        save_location, _umbra_output_name(in_file, "gamma0", filter_size)
    )
    dump_geotiff_float(outfile, gamma_0, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")
    return outfile

######################################################################
#f_path = '/mnt/disasters1/data/esops/eventData/2026/wintWeatherJan2026/umbra/Greenville'
#f_file = '*_MM.tif'
#print(os.path.join(f_path, f_file))

#infile = glob.glob(os.path.join(f_path, f_file))

#sigmaCalib(infile[0])
#betaCalib(infile[0])
#gammaCalib(infile[0])
