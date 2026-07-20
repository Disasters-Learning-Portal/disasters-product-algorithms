import os
import sys  
import argparse
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
from scipy.ndimage import uniform_filter, variance
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
    files = retrieve_s3_file_list(bucket, prefix)
    filtered_files = [x for x in files if len(x.split("/")) > 2]
    subdirs = list(set([x.split("/")[2] for x in filtered_files]))
    dates = [datetime.strptime(x.split('_')[0], "%Y-%m-%d-%H-%M-%S") for x in subdirs]

    if type(date) is str:
        date = datetime.strptime(date, "%Y-%m-%d %H:%M:%S")

    closest_date = min(dates, key=lambda d: abs(d - date))
    date_prefix = closest_date.strftime("%Y-%m-%d-%H-%M-%S")

    selected_subdir = [x for x in subdirs if x.startswith(date_prefix)][0]

    tifs = [x for x in filtered_files if ((x.split("/")[2] == selected_subdir) and (x.lower().endswith(".tif")))]
    tifs = [f"s3://{bucket}/{x}" for x in tifs]

    return tifs

def lee_filter(img, size):
    print(f"Lee filter size = {size}")
    img_mean = uniform_filter(img, (size, size))
    img_sqr_mean = uniform_filter(img**2, (size, size))
    img_variance = img_sqr_mean - img_mean**2

    overall_variance = np.nanvar(img)

    eps = 1e-10

    img_weights = img_variance / (img_variance + overall_variance + eps)
    img_output = img_mean + img_weights * (img - img_mean)
    return img_output

def sigmaCalib(s3_image_paths : list[str], save_location : str = "/tmp/s3_temp", filter_size : int = 5):
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
    
    sigma_linear = float(sigma_val) * dn

    sigma_linear = lee_filter(sigma_linear, size=filter_size)
    
    sigma_linear = np.clip(sigma_linear, 1e-10, None)
    
    sigma_0 = 20.0 * np.log10(sigma_linear)
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
    
def betaCalib(s3_image_paths : list[str], save_location : str = "/tmp/s3_temp", filter_size : int = 5):
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
  
    beta_linear = float(beta_val) * dn
    
    beta_linear = lee_filter(beta_linear, size=filter_size)
    
    beta_0 = 20.0 * np.log10(beta_linear)
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

def gammaCalib(s3_image_paths : list[str], save_location : str = "/tmp/s3_temp", filter_size : int = 5):
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
  
    gamma_linear = float(gamma_val) * dn

    gamma_linear = lee_filter(gamma_linear, size=filter_size)
    
    gamma_linear = np.clip(gamma_linear, 1e-10, None)
    
    gamma_0 = 20.0 * np.log10(gamma_linear)
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

if __name__ == "__main__":
    
    parser = argparse.ArgumentParser(
        description="Process Umbra SAR imagery."
    )

    parser.add_argument(
        "-i",
        "--input",
        required=True,
        help="Input Umbra GEC TIFF"
    )

    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="Output directory"
    )

    parser.add_argument(
        "--filter_size",
        type=int,
        choices=[3, 5, 7],
        default=5,
        help="Lee filter window size (3, 5, or 7)"
    )

    args = parser.parse_args()

    if not os.path.exists(args.output):
        os.makedirs(args.output)

    sigmaCalib([args.input], args.output, filter_size=args.filter_size)
    betaCalib([args.input], args.output, filter_size=args.filter_size)
    gammaCalib([args.input], args.output, filter_size=args.filter_size)

    print("Processing complete.")

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
