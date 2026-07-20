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

    The calibration functions (``sigmaCalib``/``betaCalib``/``gammaCalib``/
    ``rcsCalib``) read the ``_gec.tif`` band, so each GEC file is a distinct
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

def sigmaCalib(s3_image_paths : list[str], save_location : str = "/tmp/s3_temp"):
    if save_location.endswith("/"):
        save_location = save_location[:-1]
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
    
    sigma_0 = 20. * np.log10(float(sigma_val) * dn)
    print(np.max(sigma_0), np.min(sigma_0))
    
    outfile = (
        f"{save_location}/"
        f"{datetime.strptime(in_file.split('/')[-1].split('_')[0], '%Y-%m-%d-%H-%M-%S').strftime('%Y%m')}_"
        f"{in_file.split('/')[-1].split('_')[1].capitalize()}_"
        f"sigma0"
        f"{datetime.strptime(in_file.split('/')[-1].split('_')[0], '%Y-%m-%d-%H-%M-%S').strftime('%Y-%m-%dT%H:%M:%SZ')}.tif"
    )
    dump_geotiff_float(outfile, sigma_0, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")
    return outfile
    
def betaCalib(s3_image_paths : list[str], save_location : str = "/tmp/s3_temp"):
    if save_location.endswith("/"):
        save_location = save_location[:-1]
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
  
    beta_0 = 20. * np.log10(float(beta_val) * dn)
    print(np.max(beta_0), np.min(beta_0))

    outfile = (
        f"{save_location}/"
        f"{datetime.strptime(in_file.split('/')[-1].split('_')[0], '%Y-%m-%d-%H-%M-%S').strftime('%Y%m')}_"
        f"{in_file.split('/')[-1].split('_')[1].capitalize()}_"
        f"beta0"
        f"{datetime.strptime(in_file.split('/')[-1].split('_')[0], '%Y-%m-%d-%H-%M-%S').strftime('%Y-%m-%dT%H:%M:%SZ')}.tif"
    )
    dump_geotiff_float(outfile, beta_0, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")
    return outfile

def gammaCalib(s3_image_paths : list[str], save_location : str = "/tmp/s3_temp"):
    if save_location.endswith("/"):
        save_location = save_location[:-1]
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
  
    gamma_0 = 20. * np.log10(float(gamma_val) * dn)
    print(np.max(gamma_0), np.min(gamma_0))

    outfile = (
        f"{save_location}/"
        f"{datetime.strptime(in_file.split('/')[-1].split('_')[0], '%Y-%m-%d-%H-%M-%S').strftime('%Y%m')}_"
        f"{in_file.split('/')[-1].split('_')[1].capitalize()}_"
        f"gamma0"
        f"{datetime.strptime(in_file.split('/')[-1].split('_')[0], '%Y-%m-%d-%H-%M-%S').strftime('%Y-%m-%dT%H:%M:%SZ')}.tif"
    )
    dump_geotiff_float(outfile, gamma_0, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")
    return outfile

def rcsCalib(s3_image_paths : list[str], save_location : str = "/tmp/s3_temp"):
    if save_location.endswith("/"):
        save_location = save_location[:-1]
    print("Collecting needed files...")
    in_filepath = [x for x in s3_image_paths if x.lower().endswith("_gec.tif")][0]
    if f'/tmp/s3_temp/{local_tif_basename(in_filepath)}' not in glob("/tmp/s3_temp/*"):
        print("GEC file not found, downloading from s3")
        in_file = download_s3_file(in_filepath)
    else:
        print("GEC file found, proceeding")
        in_file = f'/tmp/s3_temp/{local_tif_basename(in_filepath)}'
    print('Generating RCS Naught')
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

    rcs_val = ds.GetMetadataItem('DN_TO_RCS')
    print(rcs_val)
    print(type(rcs_val))
  
    rcs_0 = 20. * np.log10(float(rcs_val) * dn)
    print(np.max(rcs_0), np.min(rcs_0))

    outfile = (
        f"{save_location}/"
        f"{datetime.strptime(in_file.split('/')[-1].split('_')[0], '%Y-%m-%d-%H-%M-%S').strftime('%Y%m')}_"
        f"{in_file.split('/')[-1].split('_')[1].capitalize()}_"
        f"rcs0"
        f"{datetime.strptime(in_file.split('/')[-1].split('_')[0], '%Y-%m-%d-%H-%M-%S').strftime('%Y-%m-%dT%H:%M:%SZ')}.tif"
    )
    dump_geotiff_float(outfile, rcs_0, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")
    return outfile

def apply_filter(input_tif, size=5, output_tif=None):
    with rio.open(input_tif) as src:
        img = src.read(1).astype(float)
        profile = src.profile.copy()

    # preserve nodata mask
    mask = np.isnan(img)

    # percentile stretch
    if 'rcs' in input_tif:
        out_min, out_max = -40, 0
    else:
        out_min, out_max = -25, 10

    p_low, p_high = np.nanpercentile(img, (2, 98))

    img = np.interp(img, (p_low, p_high), (out_min, out_max))

    img_mean = uniform_filter(img, (size, size))
    img_sqr_mean = uniform_filter(img**2, (size, size))
    img_variance = img_sqr_mean - img_mean**2

    overall_variance = np.nanvar(img)

    eps = 1e-10
    weights = img_variance / (img_variance + overall_variance + eps)

    img_out = img_mean + weights * (img - img_mean)

    # restore nodata
    img_out[mask] = np.nan

    if output_tif is None:
        output_tif = input_tif.replace(".tif", "_filtered.tif")

    profile.update(dtype=rio.float32, nodata=np.nan)

    with rio.open(output_tif, "w", **profile) as dst:
        dst.write(img_out.astype(np.float32), 1)

    return output_tif

######################################################################
#f_path = '/mnt/disasters1/data/esops/eventData/2026/wintWeatherJan2026/umbra/Greenville'
#f_file = '*_MM.tif'
#print(os.path.join(f_path, f_file))

#infile = glob.glob(os.path.join(f_path, f_file))

#sigmaCalib(infile[0])
#betaCalib(infile[0])
#gammaCalib(infile[0])
#rcsCalib(infile[0])

#genNDVI(infile[0], cloudfile[0], sunzen)
