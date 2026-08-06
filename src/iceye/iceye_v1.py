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
from pyproj import Transformer
import geopandas as gpd
from shapely.geometry import box
import matplotlib.pyplot as plt
import json

def bytescale(arr, cmin=0, cmax=1, low=0, high=255):
  # clip the data to be in the range of cmin to cmax
  arr = np.clip(arr, cmin, cmax)
  # slope
  high = float(high)
  low = float(low)
  cmax = float(cmax)
  cmin = float(cmin)
  m = (high-low)/(cmax-cmin)
  # intercept
  b = high-(m*cmax)
  # convert to byte
  arr = np.uint8((m*arr)+b)
  return arr

def lee_filter(img, size):
    img_mean = uniform_filter(img, (size, size))
    img_sqr_mean = uniform_filter(img**2, (size, size))
    img_variance = img_sqr_mean - img_mean**2

    overall_variance = variance(img)

    img_weights = img_variance / (img_variance + overall_variance)
    img_output = img_mean + img_weights * (img - img_mean)
    return img_output

def dump_geotiff_float(filename, arr, projref, in_geo):
  format = 'GTiff'
  rows, cols = np.shape(arr)
  driver = gdal.GetDriverByName(format)
  out_ds = driver.Create(filename, cols, rows, 1, gdal.GDT_Float32)
  out_cs = osr.SpatialReference()
  out_cs.ImportFromWkt(projref)
  out_ds.SetProjection(out_cs.ExportToWkt())
  out_ds.SetGeoTransform(in_geo)
  out_ds.GetRasterBand(1).WriteArray(arr)
  out_ds = None
  return filename

def getCalibrationFactor(indir):
  # Extract solar elevation from metadata and calculate solar zenith angle
  xmlfile = glob.glob(os.path.join(indir, 'ICEYE_*GRD*.xml'))
  #print(os.path.join(indir, 'ICEYE_*GRD*.xml'))
  #print(xmlfile)
  tree = ET.parse(xmlfile[0])
  root = tree.getroot()
  #print(root)
  #for child in root:
      #print(child.tag)
  calib = root.find('calibration_factor').text
  #print(calib)
  calib_value = float(calib)
  return calib_value
  
def toWGS84(in_file):
  split = os.path.basename(os.path.join(infile[0])).split('.')
  print(split)
  outfile = os.path.join(f_path, split[0]+'_wgs84.tif')
  print(outfile)

  proj4='"+proj=longlat +ellps=WGS84 +datum=WGS84 +no_defs"'
  cmd = 'gdalwarp -of GTiff -t_srs '+proj4+' '+ infile[0] +' '+outfile
  print(cmd)
  os.system(cmd)
  return

def sigmaNaughtCalib2(infile, calib_value):
  print(infile)
  #https://sar.iceye.com/latest/foundations/radiometric/#calibration-correction
  ds = gdal.Open(infile, gdal.GA_ReadOnly)
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  dn = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  #dn = band.ReadAsArray().astype(np.float64)
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjection()
  #nodata = ds.GetNoDataValue()
  #print(in_geo)
  #print(projref)

  print(f"[INFO] Image shape : {dn.shape}")
  print(f"[INFO] DN dtype    : {dn.dtype}")
  #print(f"[INFO] NoData value: {nodata}")
  ds = None  # Close file

  dn_squared = np.power(dn, 2)
  dn_amp = dn_squared * calib_value
  print(f"[INFO] Amplitude Max: ", np.max(dn_amp))
  print(f"[INFO] Amplitude Min: ", np.min(dn_amp))

  dn_db = 10.0*np.log10(dn_amp)
  print(f"[INFO] dB Max: ", np.max(dn_db))
  print(f"[INFO] dB Min: ", np.min(dn_db))

  split = os.path.basename(os.path.join(infile)).split('.')
  print(split)
  outfile = os.path.join(f_path, split[0]+'_amp.tif')
  print(outfile)

  dump_geotiff_float(outfile, dn_amp, projref, in_geo)

  split = os.path.basename(os.path.join(infile)).split('.')
  print(split)
  outfile = os.path.join(f_path, split[0]+'_dB.tif')
  print(outfile)

  dump_geotiff_float(outfile, dn_db, projref, in_geo)

  return

###############################################################################
###############################################################################
f_path = '/mnt/disasters1/data/esops/eventData/2026/Sinlaku_Guam/iceye/20260416T225747' 
f_file = 'ICEYE_*GRD*.tif'

infile = glob.glob(os.path.join(f_path, f_file))
print(infile)
calib = getCalibrationFactor(f_path)

#f_path = '/mnt/disasters1/data/esops/eventData/CSDA_Sample/ICEYEUS/SLH_952417594_300717'
#f_file = 'ICEYE_*_wgs84.tif'

sigmaNaughtCalib2(infile[0], calib)
sigmaCalib(infile[0])

