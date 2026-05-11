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

from scipy.ndimage.filters import uniform_filter
from scipy.ndimage.measurements import variance
import requests
import shutil
from scipy.signal import medfilt2d
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

    tifs = [x for x in filtered_files if ((x.split("/")[2] == selected_subdir) and (x.endswith(".tif")))]
    tifs = [f"s3://{bucket}/{x}" for x in tifs]

    return tifs

def lee_filter(img, size):
    img_mean = uniform_filter(img, (size, size))
    img_sqr_mean = uniform_filter(img**2, (size, size))
    img_variance = img_sqr_mean - img_mean**2

    overall_variance = variance(img)

    img_weights = img_variance / (img_variance + overall_variance)
    img_output = img_mean + img_weights * (img - img_mean)
    return img_output

def sigmaCalib(s3_image_paths : list[str], save_location : str = "./s3_temp"):
    if save_location.endswith("/"):
        save_location = save_location[:-1]
    print("Collecting needed files...")
    in_filepath = [x for x in s3_image_paths if x.endswith("_GEC.tif")][0]
    if f'./s3_temp/{in_filepath.split("/")[-1]}' not in glob("./s3_temp/*"):
        print("GEC file not found, downloading from s3")
        in_file = download_s3_file(in_filepath)
    else:
        print("GEC file found, proceeding")
        in_file = f'./s3_temp/{in_filepath.split("/")[-1]}'
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
    
    outfile = f"{save_location}/{in_file.split("/")[-1].replace("_GEC.tif", "_sigma0.tif")}"
    dump_geotiff_float(outfile, sigma_0, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")

    sigma_0_filt = lee_filter(sigma_0, 5)
    outfile_filt = f"{save_location}/{in_file.split("/")[-1].replace("_GEC.tif", "_sigma0Filt.tif")}"
    dump_geotiff_float(outfile_filt, sigma_0_filt, projref, in_geo)

    print(f"Generation (filtered) completed, file saved to {outfile_filt}")
    return outfile, outfile_filt
    
def betaCalib(s3_image_paths : list[str], save_location : str = "./s3_temp"):
    if save_location.endswith("/"):
        save_location = save_location[:-1]
    print("Collecting needed files...")
    in_filepath = [x for x in s3_image_paths if x.endswith("_GEC.tif")][0]
    if f'./s3_temp/{in_filepath.split("/")[-1]}' not in glob("./s3_temp/*"):
        print("GEC file not found, downloading from s3")
        in_file = download_s3_file(in_filepath)
    else:
        print("GEC file found, proceeding")
        in_file = f'./s3_temp/{in_filepath.split("/")[-1]}'
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

    outfile = f"{save_location}/{in_file.split("/")[-1].replace("_GEC.tif", "_beta0.tif")}"
    dump_geotiff_float(outfile, beta_0, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")

    beta_0_filt = lee_filter(beta_0, 5)
    outfile_filt = f"{save_location}/{in_file.split("/")[-1].replace("_GEC.tif", "_beta0Filt.tif")}"
    dump_geotiff_float(outfile_filt, beta_0_filt, projref, in_geo)

    print(f"Generation (filtered) completed, file saved to {outfile_filt}")
    return outfile, outfile_filt

def gammaCalib(s3_image_paths : list[str], save_location : str = "./s3_temp"):
    if save_location.endswith("/"):
        save_location = save_location[:-1]
    print("Collecting needed files...")
    in_filepath = [x for x in s3_image_paths if x.endswith("_GEC.tif")][0]
    if f'./s3_temp/{in_filepath.split("/")[-1]}' not in glob("./s3_temp/*"):
        print("GEC file not found, downloading from s3")
        in_file = download_s3_file(in_filepath)
    else:
        print("GEC file found, proceeding")
        in_file = f'./s3_temp/{in_filepath.split("/")[-1]}'
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

    outfile = f"{save_location}/{in_file.split("/")[-1].replace("_GEC.tif", "_gamma0.tif")}"
    dump_geotiff_float(outfile, gamma_0, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")

    gamma_0_filt = lee_filter(gamma_0, 5)
    outfile_filt = f"{save_location}/{in_file.split("/")[-1].replace("_GEC.tif", "_gamma0Filt.tif")}"
    dump_geotiff_float(outfile_filt, gamma_0_filt, projref, in_geo)

    print(f"Generation (filtered) completed, file saved to {outfile_filt}")
    return outfile, outfile_filt

def rcsCalib(s3_image_paths : list[str], save_location : str = "./s3_temp"):
    if save_location.endswith("/"):
        save_location = save_location[:-1]
    print("Collecting needed files...")
    in_filepath = [x for x in s3_image_paths if x.endswith("_GEC.tif")][0]
    if f'./s3_temp/{in_filepath.split("/")[-1]}' not in glob("./s3_temp/*"):
        print("GEC file not found, downloading from s3")
        in_file = download_s3_file(in_filepath)
    else:
        print("GEC file found, proceeding")
        in_file = f'./s3_temp/{in_filepath.split("/")[-1]}'
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

    outfile = f"{save_location}/{in_file.split("/")[-1].replace("_GEC.tif", "_rcs0.tif")}"
    dump_geotiff_float(outfile, rcs_0, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")

    rcs_0_filt = lee_filter(rcs_0, 5)
    outfile_filt = f"{save_location}/{in_file.split("/")[-1].replace("_GEC.tif", "_rs0Filt.tif")}"
    dump_geotiff_float(outfile_filt, rcs_0_filt, projref, in_geo)

    print(f"Generation (filtered) completed, file saved to {outfile_filt}")
    return outfile, outfile_filt

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
