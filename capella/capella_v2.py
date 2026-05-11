import glob
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
from scipy.ndimage.filters import uniform_filter
from scipy.ndimage.measurements import variance
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

def retrieve_capella_resources(date : Union[str, datetime], bucket : str = "csdap-capellaspace-delivery", prefix : str = "disasters") -> list[str]:
    files = retrieve_s3_file_list(bucket, prefix)
    filtered_files = [x for x in files if len(x.split("/")) > 2]
    subdirs = list(set([x.split("/")[1] for x in filtered_files]))
    dates = [datetime.strptime(x.split('_')[5], "%Y%m%d%H%M%S") for x in subdirs]

    if type(date) is str:
        date = datetime.strptime(date, "%Y%m%d%H%M%S")

    closest_date = min(dates, key=lambda d: abs(d - date))
    date_prefix = closest_date.strftime("%Y%m%d%H%M%S")

    selected_subdirs = [x for x in subdirs if x.split("_")[5] == date_prefix]
    tifs = []
    for selected_subdir in selected_subdirs:
        for file in filtered_files:
            if ((file.split("/")[1] == selected_subdir) and (file.endswith(".tif")) and ("_preview.tif" not in file)):
                tifs.append(file)
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
    in_filepath = [x for x in s3_image_paths if "_GEO_" in x][0]
    if f'./s3_temp/{in_filepath.split("/")[-1]}' not in glob("./s3_temp/*"):
        print("GEO file not found, downloading from s3")
        in_file = download_s3_file(in_filepath)
    else:
        print("GEO file found, proceeding")
        in_file = f'./s3_temp/{in_filepath.split("/")[-1]}'
    print('Generating Sigma Naught')
    print("\n\t* Opening GEO File")
    ds = gdal.Open(in_file)
    cols = ds.RasterXSize
    rows = ds.RasterYSize
    in_geo = ds.GetGeoTransform()
    projref = ds.GetProjectionRef()
    dn = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
    print(np.max(dn), np.min(dn))

    metadata = ds.GetMetadata()
    print(metadata)

    image_desc_str = ds.GetMetadataItem('TIFFTAG_IMAGEDESCRIPTION')
    #print(coeff_val)
    if image_desc_str:
        # Parse the JSON string
        metadata_dict = json.loads(image_desc_str)

        # Navigate the dictionary to grab the scale_factor
        try:
            scale_factor = metadata_dict['collect']['image']['scale_factor']

            # Now you have it as its own float variable
            print(f"The scale factor is: {scale_factor}")

        except KeyError as e:
            print(f"Could not find the expected keys in the metadata: {e}")
            scale_factor = None
    print(scale_factor)
    sigma_0 = 20. * np.log10((scale_factor * dn))
    print(np.max(sigma_0), np.min(sigma_0))
    sigma_0 = np.clip(sigma_0, -60., np.max(sigma_0))

    outfile = f"{save_location}/{in_file.split("/")[-1].replace(".tif", "_sigma0.tif")}"
    dump_geotiff_float(outfile, sigma_0, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")

    sigma_0_filt = lee_filter(sigma_0, 5)
    outfile_filt = f"{save_location}/{in_file.split("/")[-1].replace(".tif", "_sigma0Filt.tif")}"
    dump_geotiff_float(outfile_filt, sigma_0_filt, projref, in_geo)

    print(f"Generation (filtered) completed, file saved to {outfile_filt}")
    return outfile, outfile_filt

######################################################################
#f_path = '/mnt/disasters1/data/esops/eventData/2026/Sinlaku_Guam/capella/20260418T193305'
#f_file = 'CAPELLA*GEO*.tif'
#print(os.path.join(f_path, f_file))

#infile = glob.glob(os.path.join(f_path, f_file))

#sigmaCalib(infile[0])
