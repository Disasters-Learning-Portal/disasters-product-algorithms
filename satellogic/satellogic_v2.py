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

#def getSolarZenithAngle(indir):
  # Extract solar elevation from metadata and calculate solar zenith angle
#  with open(glob.glob(os.path.join(indir, '*_angles.geojson'))[0], "r") as read_file:
#      data = json.load(read_file)
#  solarZenithAngle = float(data['features'][0]['properties']['solar']['zenith'])
#  return solarZenithAngle

def retrieve_satellogic_resources(date : Union[str, datetime], level : str, bucket : str = "csda-data-vendor-satellogic", prefix : str = "disasters") -> list[list[str]]:
    files = retrieve_s3_file_list(bucket, prefix)
    filtered_files = [x for x in files if f"_{level}_" in x.split("/")[1]]
    subdirs = list(set([x.split("/")[1] for x in filtered_files]))
    dates = [datetime.strptime(f"{x.split('_')[0]}_{x.split('_')[1]}", "%Y%m%d_%H%M%S") for x in subdirs]

    if type(date) is str:
        date = datetime.strptime(date, "%Y-%m-%d %H:%M:%S")

    closest_date = min(dates, key=lambda d: abs(d - date))
    date_prefix = closest_date.strftime("%Y%m%d_%H%M%S")

    selected_subdir = [x for x in subdirs if x.startswith(date_prefix)][0]

    metadata = [x for x in filtered_files if ((x.split("/")[1] == selected_subdir) and (x.split("/")[2] != "rasters"))]
    tifs = [x for x in filtered_files if ((x.split("/")[1] == selected_subdir) and (x.split("/")[2] == "rasters") and (x.endswith(".tif")))]

    metadata = [f"s3://{bucket}/{x}" for x in metadata]
    tifs = [f"s3://{bucket}/{x}" for x in tifs]

    return metadata, tifs

def getSolarZenithAngle(s3_metadata : list[str]):
    filepath = [x for x in s3_metadata if x.endswith("_angles.geojson")][0]
    data = json.loads(read_s3_file(filepath, "utf-8"))
    solarZenithAngle = float(data['features'][0]['properties']['solar']['zenith'])
    return solarZenithAngle

def genTrueColor(s3_image_paths : list[str], s3_metadata : list[str], save_location : str = "./s3_temp"):
    if save_location.endswith("/"):
        save_location = save_location[:-1]
    print("Collecting needed files...")
    sunzen = getSolarZenithAngle(s3_metadata)
    in_filepath = [x for x in s3_image_paths if x.endswith("_TOA_0.tif")][0]
    cloud_filepath = [x for x in s3_image_paths if x.endswith("_CLOUD_0.tif")][0]
    if f'./s3_temp/{in_filepath.split("/")[-1]}' not in glob("./s3_temp/*"):
        print("TOA file not found, downloading from s3")
        in_file = download_s3_file(in_filepath)
    else:
        print("TOA file found, proceeding")
        in_file = f'./s3_temp/{in_filepath.split("/")[-1]}'
    if f'./s3_temp/{cloud_filepath.split("/")[-1]}' not in glob("./s3_temp/*"):
        print("Cloud file not found, downloading from s3")
        cloud_file = download_s3_file(cloud_filepath)
    else:
        print("Cloud file found, proceeding")
        cloud_file = f'./s3_temp/{cloud_filepath.split("/")[-1]}'
    print('Generating True Color')
    print("\n\t* Opening Red File")
    ds = gdal.Open(in_file)
    cols = ds.RasterXSize
    rows = ds.RasterYSize
    in_geo = ds.GetGeoTransform()
    projref = ds.GetProjectionRef()
    red_array = ds.GetRasterBand(3).ReadAsArray(0, 0, cols, rows)
    
    print(np.max(red_array))
    red_array = red_array / 10000.
    red_array = red_array / np.cos(np.radians(sunzen))
    print(np.max(red_array))

    print("\n\t* Opening Green File")
    green_array = ds.GetRasterBand(2).ReadAsArray(0, 0, cols, rows)
    print(np.max(green_array))
    green_array = green_array / 10000.
    green_array = green_array / np.cos(np.radians(sunzen))
    print(np.max(green_array))
    
    print("\n\t* Opening Blue File")
    blue_array = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
    print(np.max(blue_array))
    blue_array = blue_array / 10000.
    blue_array = blue_array / np.cos(np.radians(sunzen))
    print(np.max(blue_array))

    # Cloud Mask 
    print("\n\t* Opening Cloud/QA File")
    dc = gdal.Open(cloud_file)
    cloud_array = dc.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
    no_data = np.where(cloud_array != 1)

    red_array[no_data] = 0
    green_array[no_data] = 0
    blue_array[no_data] = 0

    ### Convert from reflectance to 8-bit values ###
    print("\t* Converting reflectance to 8-bit values")
    r = bytescale(red_array, cmin=0, cmax=1, low=1 ,high=255)
    g = bytescale(green_array, cmin=0, cmax=1, low=1, high=255)
    b = bytescale(blue_array, cmin=0, cmax=1, low=1, high=255)

    # Use PIL to brighten it up
    print("\t* Enhancing image")
    rgbArray = np.ones( (rows,cols,3), 'uint8' )
    rgbArray[...,0] = r
    rgbArray[...,1] = g
    rgbArray[...,2] = b
    rgb = Image.fromarray(rgbArray)
    enhancer = ImageEnhance.Brightness(rgb).enhance(3)  # increase this number to increase the brightness
    r = np.reshape(enhancer.getdata(band=0), (rows,cols))
    g = np.reshape(enhancer.getdata(band=1), (rows,cols))
    b = np.reshape(enhancer.getdata(band=2), (rows,cols))

    r = np.clip(r, 1, 255)
    g = np.clip(g, 1, 255)
    b = np.clip(b, 1, 255)

    r[no_data] = 0
    g[no_data] = 0 
    b[no_data] = 0
    
    outfile = f"{save_location}/{in_file.split("/")[-1].replace("_TOA_0.tif", "_trueColor.tif")}"
    dump_geotiff_rgb(outfile, r, g, b, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")
    return outfile

def gencolorIR(s3_image_paths : list[str], s3_metadata : list[str], save_location : str = "./s3_temp"):
    if save_location.endswith("/"):
        save_location = save_location[:-1]
    print("Collecting needed files...")
    sunzen = getSolarZenithAngle(s3_metadata)
    in_filepath = [x for x in s3_image_paths if x.endswith("_TOA_0.tif")][0]
    cloud_filepath = [x for x in s3_image_paths if x.endswith("_CLOUD_0.tif")][0]
    if f'./s3_temp/{in_filepath.split("/")[-1]}' not in glob("./s3_temp/*"):
        print("TOA file not found, downloading from s3")
        in_file = download_s3_file(in_filepath)
    else:
        print("TOA file found, proceeding")
        in_file = f'./s3_temp/{in_filepath.split("/")[-1]}'
    if f'./s3_temp/{cloud_filepath.split("/")[-1]}' not in glob("./s3_temp/*"):
        print("Cloud file not found, downloading from s3")
        cloud_file = download_s3_file(cloud_filepath)
    else:
        print("Cloud file found, proceeding")
        cloud_file = f'./s3_temp/{cloud_filepath.split("/")[-1]}'
    print('Generating Color Infrared')
    print("\n\t* Opening NIR File")
    ds = gdal.Open(in_file)
    cols = ds.RasterXSize
    rows = ds.RasterYSize
    in_geo = ds.GetGeoTransform()
    projref = ds.GetProjectionRef()
    nir_array = ds.GetRasterBand(4).ReadAsArray(0, 0, cols, rows)
    print(np.max(nir_array))
    nir_array = nir_array / 10000.
    nir_array = nir_array / np.cos(np.radians(sunzen))
    print(np.max(nir_array))

    print("\n\t* Opening Red File")
    red_array = ds.GetRasterBand(3).ReadAsArray(0, 0, cols, rows)
    print(np.max(red_array))
    red_array = red_array / 10000.
    red_array = red_array / np.cos(np.radians(sunzen))
    print(np.max(red_array))

    print("\n\t* Opening Green File")
    green_array = ds.GetRasterBand(2).ReadAsArray(0, 0, cols, rows)
    print(np.max(green_array))
    green_array = green_array / 10000.
    green_array = green_array / np.cos(np.radians(sunzen))
    print(np.max(green_array))

    # Cloud Mask
    print("\n\t* Opening Cloud/QA File")
    dc = gdal.Open(cloud_file)
    cloud_array = dc.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
    no_data = np.where(cloud_array != 1)

    nir_array[no_data] = 0
    red_array[no_data] = 0
    green_array[no_data] = 0

    ### Convert from reflectance to 8-bit values ###
    print("\t* Converting reflectance to 8-bit values")
    r = bytescale(nir_array, cmin=0, cmax=1, low=1 ,high=255)
    g = bytescale(red_array, cmin=0, cmax=1, low=1, high=255)
    b = bytescale(green_array, cmin=0, cmax=1, low=1, high=255)

    # Use PIL to brighten it up
    print("\t* Enhancing image")
    rgbArray = np.ones( (rows,cols,3), 'uint8' )
    rgbArray[...,0] = r
    rgbArray[...,1] = g
    rgbArray[...,2] = b
    rgb = Image.fromarray(rgbArray)
    enhancer = ImageEnhance.Brightness(rgb).enhance(3)  # increase this number to increase the brightness
    r = np.reshape(enhancer.getdata(band=0), (rows,cols))
    g = np.reshape(enhancer.getdata(band=1), (rows,cols))
    b = np.reshape(enhancer.getdata(band=2), (rows,cols))

    r = np.clip(r, 1, 255)
    g = np.clip(g, 1, 255)
    b = np.clip(b, 1, 255)

    r[no_data] = 0
    g[no_data] = 0
    b[no_data] = 0
    
    outfile = f"{save_location}/{in_file.split("/")[-1].replace("_TOA_0.tif", "_colorIR.tif")}"
    dump_geotiff_rgb(outfile, r, g, b, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")
    return outfile

def genNDVI(s3_image_paths : list[str], s3_metadata : list[str], save_location : str = "./s3_temp"):
    if save_location.endswith("/"):
        save_location = save_location[:-1]
    print("Collecting needed files...")
    sunzen = getSolarZenithAngle(s3_metadata)
    in_filepath = [x for x in s3_image_paths if x.endswith("_TOA_0.tif")][0]
    cloud_filepath = [x for x in s3_image_paths if x.endswith("_CLOUD_0.tif")][0]
    if f'./s3_temp/{in_filepath.split("/")[-1]}' not in glob("./s3_temp/*"):
        print("TOA file not found, downloading from s3")
        in_file = download_s3_file(in_filepath)
    else:
        print("TOA file found, proceeding")
        in_file = f'./s3_temp/{in_filepath.split("/")[-1]}'
    if f'./s3_temp/{cloud_filepath.split("/")[-1]}' not in glob("./s3_temp/*"):
        print("Cloud file not found, downloading from s3")
        cloud_file = download_s3_file(cloud_filepath)
    else:
        print("Cloud file found, proceeding")
        cloud_file = f'./s3_temp/{cloud_filepath.split("/")[-1]}'
    print('Generating NDVI')
    print("\n\t* Opening NIR File")
    ds = gdal.Open(in_file)
    cols = ds.RasterXSize
    rows = ds.RasterYSize
    in_geo = ds.GetGeoTransform()
    projref = ds.GetProjectionRef()
    nir_array = ds.GetRasterBand(4).ReadAsArray(0, 0, cols, rows)
    print(np.max(nir_array))
    nir_array = nir_array / 10000.
    nir_array = nir_array / np.cos(np.radians(sunzen))
    print(np.max(nir_array))

    print("\n\t* Opening Red File")
    red_array = ds.GetRasterBand(3).ReadAsArray(0, 0, cols, rows)
    print(np.max(red_array))
    red_array = red_array / 10000.
    red_array = red_array / np.cos(np.radians(sunzen))
    print(np.max(red_array))

    # Cloud Mask
    print("\n\t* Opening Cloud/QA File")
    dc = gdal.Open(cloud_file)
    cloud_array = dc.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
    no_data = np.where(cloud_array != 1)

    print("\n\t* Calculating NDVI")
    ndvi = np.empty(np.shape(red_array), 'float')
    ok = np.where( (red_array >= 0) & (nir_array >= 0) )
    ndvi[ok] = (nir_array[ok]-red_array[ok])/(nir_array[ok]+red_array[ok])
    
    # Clip to a reasonable range
    print("\t* Clipping to a reasonable range")
    ndvi = np.clip(ndvi, -1.0, 1.0)
    ndvi[no_data] = 999

    outfile = f"{save_location}/{in_file.split("/")[-1].replace("_TOA_0.tif", "_ndvi.tif")}"
    dump_geotiff_float(outfile, ndvi, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")
    return outfile

def genNDWI(s3_image_paths : list[str], s3_metadata : list[str], save_location : str = "./s3_temp"):
    if save_location.endswith("/"):
        save_location = save_location[:-1]
    print("Collecting needed files...")
    sunzen = getSolarZenithAngle(s3_metadata)
    in_filepath = [x for x in s3_image_paths if x.endswith("_TOA_0.tif")][0]
    cloud_filepath = [x for x in s3_image_paths if x.endswith("_CLOUD_0.tif")][0]
    if f'./s3_temp/{in_filepath.split("/")[-1]}' not in glob("./s3_temp/*"):
        print("TOA file not found, downloading from s3")
        in_file = download_s3_file(in_filepath)
    else:
        print("TOA file found, proceeding")
        in_file = f'./s3_temp/{in_filepath.split("/")[-1]}'
    if f'./s3_temp/{cloud_filepath.split("/")[-1]}' not in glob("./s3_temp/*"):
        print("Cloud file not found, downloading from s3")
        cloud_file = download_s3_file(cloud_filepath)
    else:
        print("Cloud file found, proceeding")
        cloud_file = f'./s3_temp/{cloud_filepath.split("/")[-1]}'
    print('Generating NDWI')
    print("\n\t* Opening NIR File")
    ds = gdal.Open(in_file)
    cols = ds.RasterXSize
    rows = ds.RasterYSize
    in_geo = ds.GetGeoTransform()
    projref = ds.GetProjectionRef()
    nir_array = ds.GetRasterBand(4).ReadAsArray(0, 0, cols, rows)
    print(np.max(nir_array))
    nir_array = nir_array / 10000.
    nir_array = nir_array / np.cos(np.radians(sunzen))
    print(np.max(nir_array))

    print("\n\t* Opening Green File")
    green_array = ds.GetRasterBand(2).ReadAsArray(0, 0, cols, rows)
    print(np.max(green_array))
    green_array = green_array / 10000.
    green_array = green_array / np.cos(np.radians(sunzen))
    print(np.max(green_array))

    # Cloud Mask
    print("\n\t* Opening Cloud/QA File")
    dc = gdal.Open(cloud_file)
    cloud_array = dc.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
    no_data = np.where(cloud_array != 1)

    print("\n\t* Calculating NDWI")
    ndwi = np.empty(np.shape(nir_array), 'float')
    ok = np.where( (green_array >= 0) & (nir_array >= 0) )
    ndwi[ok] = (nir_array[ok]-green_array[ok])/(nir_array[ok]+green_array[ok])
    
    # Clip to a reasonable range
    print("\t* Clipping to a reasonable range")
    ndwi = np.clip(ndwi, -1.0, 1.0)
    ndwi[no_data] = 999

    outfile = f"{save_location}/{in_file.split("/")[-1].replace("_TOA_0.tif", "_ndwi.tif")}"
    dump_geotiff_float(outfile, ndwi, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")
    return outfile
######################################################################

#f_path = '/mnt/disasters1/data/esops/eventData/2026/wintWeatherJan2026/satellogic/Oxford/20260128_155747_002_SN33_L1D_SR_MS_341271'
#f_file = '*_MS_TOA_0.tif'
#c_file = '*_CLOUD_0.tif'
#print(os.path.join(f_path, f_file))

#sunzen = getSolarZenithAngle(f_path)
#print(sunzen)

#infile = glob.glob(os.path.join(f_path, 'rasters', f_file))
#print(os.path.basename(os.path.join(infile[0])))
#print(os.path.basename(os.path.join(infile[0])).split('.'))

#cloudfile = glob.glob(os.path.join(f_path, 'rasters', c_file))

#genTrueColor(infile[0], cloudfile[0], sunzen)
#gencolorIR(infile[0], cloudfile[0], sunzen)
#genNDVI(infile[0], cloudfile[0], sunzen)
#genNDWI(infile[0], cloudfile[0], sunzen)
