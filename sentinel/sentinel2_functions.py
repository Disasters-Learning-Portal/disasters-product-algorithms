#!/usr/local/anaconda3/bin/python

"""
sentinel2_functions.py

Name:       Kaylee Sharp
Date:       February 2024
"""

import os
import glob
import numpy as np
from PIL import Image, ImageEnhance
from xml.dom import minidom
from shared_utils.geotools import *
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
import boto3
from botocore import UNSIGNED
from botocore.config import Config

def dump_geotiff(data_array, crs, trans, nodata_val, outfile):
    # write geotiff with inputted CRS, transform, and no data value
    # data_array should be n x m array
    with rio.open(
        outfile, "w", 
        driver = 'GTiff', 
        count = 1,
        dtype = data_array.dtype,
        height = data_array.shape[0],
        width = data_array.shape[1],
        crs = crs, 
        nodata = nodata_val,
        transform = trans) as dest:

        dest.write(data_array, 1)

def extract_band_geotiffs(band, safe, level, res):
  if level == 'MSIL2A':
    # find .jp2 file matching band/resolution
    jp2_file = glob.glob(safe + f'/GRANULE/*/IMG_DATA/R{res}m/*{band}*.jp2')[0]

    # create output filename
    outname = os.path.basename(safe).replace('.SAFE', f'_{band}_{res}m.tif')
    outfile = os.path.join(safe, outname)

    # open band file
    band_file = rio.open(jp2_file)
    band_geo = band_file.profile

    # quality file gets special treatment
    if band == 'SCL':
      band_array=band_file.read(1).astype('uint16')
      # no data value of 999
      nd_val = 999
    else:
      # convert from DN to reflectance
      band_array = band_file.read(1) / 10000
      nd_val = 0
    
    # write band to file
    dump_geotiff(band_array, band_geo['crs'], band_geo['transform'], nd_val, outfile)

  elif level == 'MSIL1C':
    # find .jp2 file matching band
    jp2_file = glob.glob(safe + f'/GRANULE/*/IMG_DATA/*{band}*.jp2')[0]

    # create output filename
    outname = os.path.basename(safe).replace('.SAFE', f'_{band}_{res}m.tif')
    outfile = os.path.join(safe, outname)

    # convert from native resolution to desired resolution
    native_res = rio.open(jp2_file).transform[0]
    if int(native_res) != int(res):
      scale_factor = int(native_res)/int(res)
      with rio.open(jp2_file) as dataset:
        band_array = dataset.read(1,
          out_shape=(dataset.count,
                    int(dataset.height * scale_factor),
                    int(dataset.width * scale_factor)
          ),
          resampling=Resampling.bilinear
        )
        transform = dataset.transform * dataset.transform.scale(
          (dataset.width / band_array.shape[-1]),
          (dataset.height / band_array.shape[-2])
        )
        band_array = band_array / 10000
        dump_geotiff(band_array, dataset.crs, transform, 0, outfile)

    else:
      band_file = rio.open(jp2_file)
      band_geo = band_file.profile
      band_array = band_file.read(1) / 10000
      dump_geotiff(band_array, band_geo['crs'], band_geo['transform'], 0, outfile)

  return outfile

def get_rayleigh_correction(band_file, band_name):
  try:
    # required package
    # create a conda environment and install pyspectral>=0.12.5
    from pyspectral.rayleigh import Rayleigh
  except:
    print('\t* Rayleigh correction error. Pyspectral package must be >=0.12.5')
    return 0
  else:
    if band_name in ['B01', 'B02', 'B03', 'B04', 'B05', 'B06', 'B07']:
      print('\t* Applying Rayleigh correction to:', band_name)

      # initialize rayleigh correction class for Sentinel-2
      s2 = Rayleigh('Sentinel-2A', 'msi')
      
      # parse metadata for mean sun angle
      safe = os.path.dirname(band_file)
      gran_xml = glob.glob(os.path.join(safe, 'GRANULE', '*', 'MTD_*.xml'))[0]
      xmldoc = minidom.parse(gran_xml)
      nodes = xmldoc.getElementsByTagName('Mean_Sun_Angle')
      for node in nodes:
        sunz = node.getElementsByTagName('ZENITH_ANGLE')[0].firstChild.data
        sunaz = node.getElementsByTagName('AZIMUTH_ANGLE')[0].firstChild.data
      sunz = np.asarray(float(sunz))
      sunaz = np.asarray(float(sunaz))

      # parse metadata for mean viewing incidence angle
      nodes = xmldoc.getElementsByTagName('Mean_Viewing_Incidence_Angle')
      for node in nodes:
        satz = node.getElementsByTagName('ZENITH_ANGLE')[0].firstChild.data
        sataz = node.getElementsByTagName('AZIMUTH_ANGLE')[0].firstChild.data
      satz = np.asarray(float(satz))
      sataz = np.asarray(float(sataz))

      # compute rayleigh contribution
      ssadiff = np.asarray(np.abs(sunaz-sataz))
      ray = 0.01*s2.get_reflectance(sunz, satz, ssadiff, band_name)
      return ray
    else:
      return 0

def gen_rgb(r_file, g_file, b_file, rayleigh=False, enhance=2):
  # open RGB bands
  r, in_geo, projref = get_geo(r_file)
  r_band = r_file.split('_')[-2]
  g, in_geo, projref = get_geo(g_file)
  g_band = g_file.split('_')[-2]
  b, in_geo, projref = get_geo(b_file)
  b_band = b_file.split('_')[-2]

  # determine shape
  rows, cols = np.shape(b)

  # apply rayleigh correction 
  if rayleigh:
    ray_blue = get_rayleigh_correction(b_file, b_band)
    ray_green = get_rayleigh_correction(g_file, g_band)
    ray_red = get_rayleigh_correction(r_file, r_band)
    b = b-ray_blue
    g = g-ray_green
    r = r-ray_red

  # clip each band
  b = np.clip(b, 0, 1)
  g = np.clip(g, 0, 1)
  r = np.clip(r, 0, 1)

  # bytescale bands
  rgb_min = 0.04
  rgb_max = 1
  b_enhanced = ((b - rgb_min) / (rgb_max - rgb_min))*255
  g_enhanced = ((g - rgb_min) / (rgb_max - rgb_min))*255
  r_enhanced = ((r - rgb_min) / (rgb_max - rgb_min))*255

  # determine no data locations
  b_no_data = np.where(b == 0.0)
  g_no_data = np.where(g == 0.0)
  r_no_data = np.where(r == 0.0)

  # increase brightness
  print('\t* Enhancing image')
  rgbArray = np.zeros( (rows,cols,3), 'uint8' )
  rgbArray[...,0] = r_enhanced
  rgbArray[...,1] = g_enhanced
  rgbArray[...,2] = b_enhanced
  rgb = Image.fromarray(rgbArray)
  rgb_enhanced = ImageEnhance.Brightness(rgb).enhance(enhance)
  r = np.reshape(rgb_enhanced.getdata(band=0), (rows,cols))
  g = np.reshape(rgb_enhanced.getdata(band=1), (rows,cols))
  b = np.reshape(rgb_enhanced.getdata(band=2), (rows,cols))

  # assign a value of 0 to each no data location
  r[r_no_data] = 0
  g[g_no_data] = 0
  b[b_no_data] = 0

  # return bands and geographic information
  return r,g,b,projref,in_geo

def gen_cloudMask(safe, outname, level):
  # check for quality file
  scl_check = glob.glob(os.path.join(safe, '*SCL_20m.tif'))
  if scl_check:
    # open quality file
    scl_file = scl_check[0]
    print('\t* Opening SCL file')
  else:
    # extract quality data from .jp2 file
    print('\t* Extracting SCL')
    scl_file = extract_band_geotiffs('SCL', safe, level, '20')

  # read quality file
  print('\t* Generating Cloud Mask geotiff')
  scl_rst = rio.open(scl_file)
  scl = scl_rst.read(1)

  # cloud = 1, no cloud = 0, no data = 999
  bin_map = {0:999, 1:0, 2:0, 3:1, 4:0, 5:0, 6:0, 7:0, 8:1, 9:1, 10:1, 11:0}
  clouds_bin = np.vectorize(bin_map.get)(scl).astype('uint16')

  # write cloud mask to file
  dump_geotiff(clouds_bin, scl_rst.crs, scl_rst.transform, 999, outname)

  return outname

def apply_cloud_mask(tif_to_mask, cloud_mask):
  # create masked directory within product directory 
  masked_dir = os.path.join(Path(tif_to_mask).parent, 'masked')
  if not os.path.isdir(masked_dir):
    os.mkdir(masked_dir)
  
  # masked output filename
  masked_out_file = os.path.basename(tif_to_mask).replace('.tif', '_masked.tif')
  masked_path = os.path.join(masked_dir, masked_out_file)
  
  # open geotiff to mask and cloud mask file
  tif_to_mask_rst = rio.open(tif_to_mask)
  mask_rst = rio.open(cloud_mask)

  # check that resolutions match
  tif_to_mask_res = tif_to_mask_rst.transform[0]
  mask_res = mask_rst.transform[0]
  if tif_to_mask_res != mask_res:
    # resample cloud mask (in memory) to match geotiff to mask
    with rio.open(cloud_mask) as dataset:
      mask_array = dataset.read(1,
          out_shape=(dataset.count,
                    int(dataset.height * 2),
                    int(dataset.width * 2)
          ),
          resampling=Resampling.nearest
      )
  else:
    mask_array = mask_rst.read(1)

  if tif_to_mask_rst.count == 1:
    # mask images with one band (e.g., NDVI, EVI, etc.)
    tif_to_mask_array = tif_to_mask_rst.read(1)
    tif_to_mask_array[mask_array == 1] = tif_to_mask_rst.nodata
    dump_geotiff(tif_to_mask_array, tif_to_mask_rst.crs, tif_to_mask_rst.transform, tif_to_mask_rst.nodata, masked_path)
  else:
    # mask images with three bands (e.g., true color, color infrared, etc.)
    # outputs a 4-band image with the fourth band being an alpha band

    # create alpha band array
    band1_array = tif_to_mask_rst.read(1)
    nodata_array = np.full(band1_array.shape, 255)
    nodata_array[band1_array == tif_to_mask_rst.nodata] = tif_to_mask_rst.nodata
    nodata_array[mask_array == 1] = tif_to_mask_rst.nodata

    # write new file with the addition of the alpha band
    with rio.open(masked_path, mode="w", 
                  driver='GTiff', 
                  width=nodata_array.shape[1],
                  height=nodata_array.shape[0],
                  count=4,
                  crs=tif_to_mask_rst.crs,
                  transform=tif_to_mask_rst.transform,
                  dtype=band1_array.dtype) as dest:
      dest.write(tif_to_mask_rst.read(1), 1)
      dest.write(tif_to_mask_rst.read(2), 2)
      dest.write(tif_to_mask_rst.read(3), 3)
      dest.write(nodata_array, 4)
  
def gen_true_color(safe, outname, level, mask=None, rayleigh=False):
  # check for band geotiffs
  # extract bands from .jp2 file if necessary
  b_check = glob.glob(os.path.join(safe, '*B02_10m.tif'))
  if b_check:
    b_file = b_check[0]
    print('\t* Opening B2 file')
  else:
    print('\t* Extracting B2')
    b_file = extract_band_geotiffs('B02', safe, level, '10')
  g_check = glob.glob(os.path.join(safe, '*B03_10m.tif'))
  if g_check:
    g_file = g_check[0]
    print('\t* Opening B3 file')
  else:
    print('\t* Extracting B3')
    g_file = extract_band_geotiffs('B03', safe, level, '10')
  r_check = glob.glob(os.path.join(safe, '*B04_10m.tif'))
  if r_check:
    r_file = r_check[0]
    print('\t* Opening B4 file')
  else:
    print('\t* Extracting B4')
    r_file = extract_band_geotiffs('B04', safe, level, '10')

  # different enhancement level if applying rayleigh correction
  if rayleigh:
    enhance_level = 3.5
  else:
    enhance_level = 3

  # process bands
  r,g,b,projref,in_geo = gen_rgb(r_file, g_file, b_file, rayleigh, enhance_level)

  # write true color image to file
  print('\t* Generating true color geotiff')
  result = dump_geotiff_rgb(outname, r, g, b, projref, in_geo)

  # update no data value in metadata
  cmd = f"python /usr/local/anaconda3/bin/gdal_edit.py -a_nodata 0 {outname}"
  os.system(cmd)

  # apply cloud mask
  if mask:
    print('\t* Applying cloud mask')
    apply_cloud_mask(outname, mask)

def gen_natural_color(safe, outname, level, mask=None, rayleigh=False):
  # check for band geotiffs
  # extract bands from .jp2 file if necessary
  r_check = glob.glob(os.path.join(safe, '*B11_20m.tif'))
  if r_check:
    r_file = r_check[0]
    print('\t* Opening B11 file')
  else:
    print('\t* Extracting B11')
    r_file = extract_band_geotiffs('B11', safe, level, 20)
  g_check = glob.glob(os.path.join(safe, '*B8A_20m.tif'))
  if g_check:
    g_file = g_check[0]
    print('\t* Opening B8A file')
  else:
    print('\t* Extracting B8A')
    g_file = extract_band_geotiffs('B8A', safe, level, 20)
  b_check = glob.glob(os.path.join(safe, '*B04_20m.tif'))
  if b_check:
    b_file = b_check[0]
    print('\t* Opening B4 file')
  else:
    print('\t* Extracting B4')
    b_file = extract_band_geotiffs('B04', safe, level, 20)

  # set enhancement level
  enhance_level = 2

  # process bands
  r,g,b,projref,in_geo = gen_rgb(r_file, g_file, b_file, rayleigh, enhance_level)
  
  # write natural color image to file
  print('\t* Generating natural color geotiff')
  result = dump_geotiff_rgb(outname, r, g, b, projref, in_geo)

  # update no data value in metadata
  cmd = f"python /usr/local/anaconda3/bin/gdal_edit.py -a_nodata 0 {outname}"
  os.system(cmd)

  # apply cloud mask
  if mask:
    print('\t* Applying cloud mask')
    apply_cloud_mask(outname, mask)

def gen_swir(safe, outname, level, mask=None, rayleigh=False):
  # check for band geotiffs
  # extract bands from .jp2 file if necessary
  r_check = glob.glob(os.path.join(safe, '*B12_20m.tif'))
  if r_check:
    r_file = r_check[0]
    print('\t* Opening B12 file')
  else:
    print('\t* Extracting B12')
    r_file = extract_band_geotiffs('B12', safe, level,'20')
  g_check = glob.glob(os.path.join(safe, '*B8A_20m.tif'))
  if g_check:
    g_file = g_check[0]
    print('\t* Opening B8A file')
  else:
    print('\t* Extracting B8A')
    g_file = extract_band_geotiffs('B8A', safe, level, '20')
  b_check = glob.glob(os.path.join(safe, '*B04_20m.tif'))
  if b_check:
    b_file = b_check[0]
    print('\t* Opening B4 file')
  else:
    print('\t* Extracting B4')
    b_file = extract_band_geotiffs('B04', safe, level, '20')

  # process bands
  r,g,b,projref,in_geo = gen_rgb(r_file, g_file, b_file, rayleigh)

  # write short wave infrared image to file
  print('\t* Generating short wave infrared geotiff')
  result = dump_geotiff_rgb(outname, r, g, b, projref, in_geo)

  # update no data value in metadata
  cmd = f"python /usr/local/anaconda3/bin/gdal_edit.py -a_nodata 0 {outname}"
  os.system(cmd)

  # apply cloud mask
  if mask:
    print('\t* Applying cloud mask')
    apply_cloud_mask(outname, mask)

def gen_color_infrared(safe, outname, level, mask=None, rayleigh=False):
  # check for band geotiffs
  # extract bands from .jp2 file if necessary
  r_check = glob.glob(os.path.join(safe, '*B08_10m.tif'))
  if r_check:
    r_file = r_check[0]
    print('\t* Opening B8 file')
  else:
    print('\t* Extracting B8')
    r_file = extract_band_geotiffs('B08', safe, level, '10')
  g_check = glob.glob(os.path.join(safe, '*B04_10m.tif'))
  if g_check:
    g_file = g_check[0]
    print('\t* Opening B4 file')
  else:
    print('\t* Extracting B4')
    g_file = extract_band_geotiffs('B04', safe, level,'10')
  b_check = glob.glob(os.path.join(safe, '*B03_10m.tif'))
  if b_check:
    b_file = b_check[0]
    print('\t* Opening B3 file')
  else:
    print('\t* Extracting B3')
    b_file = extract_band_geotiffs('B03', safe, level, '10')

  # process bands
  r,g,b,projref,in_geo = gen_rgb(r_file, g_file, b_file, rayleigh)

  # write color infrared image to file
  print('\t* Generating color infrared geotiff')
  result = dump_geotiff_rgb(outname, r, g, b, projref, in_geo)

  # update no data value in metadata
  cmd = f"python /usr/local/anaconda3/bin/gdal_edit.py -a_nodata 0 {outname}"
  os.system(cmd)

  # apply cloud mask
  if mask:
    print('\t* Applying cloud mask')
    apply_cloud_mask(outname, mask)

def gen_ndwi(safe, outname, level, mask=None, rayleigh=False):
  # check for band geotiffs
  # extract bands from .jp2 file if necessary
  g_check = glob.glob(os.path.join(safe, '*B03_10m.tif'))
  if g_check:
    g_file = g_check[0]
    print('\t* Opening B3 file')
  else:
    print('\t* Extracting B3')
    g_file = extract_band_geotiffs('B03', safe, level, '10')
  nir_check = glob.glob(os.path.join(safe, '*B08_10m.tif'))
  if nir_check:
    nir_file = nir_check[0]
    print('\t* Opening B8 file')
  else:
    print('\t* Extracting B8')
    nir_file = extract_band_geotiffs('B08', safe, level, '10')

  # read NIR and green band files
  nir, in_geo, projref = get_geo(nir_file)
  g, in_geo, projref = get_geo(g_file)

  # apply rayleigh correction to green band
  if rayleigh:
    g_ray = get_rayleigh_correction(g_file, 'B03')
    g = g - g_ray

  # determine shape
  rows, cols = np.shape(g)

  # clip green and NIR bands
  g = np.clip(g, 0, 1)
  nir = np.clip(nir, 0, 1)

  # calculate NDWI
  print('\t* Calculating NDWI')
  ndwi = np.zeros((rows,cols))
  ndwi[:] = 999
  valid = np.where((g > 0) & (nir > 0))
  ndwi[valid] = (g[valid] - nir[valid]) / (g[valid] + nir[valid])
  
  # write NDWI image to file
  print('\t* Generating NDWI geotiff')
  result = dump_geotiff_float(outname, ndwi, projref, in_geo)

  # update no data value in metadata
  cmd = f"python /usr/local/anaconda3/bin/gdal_edit.py -a_nodata 999 {outname}"
  os.system(cmd)

  # apply cloud mask
  if mask:
    print('\t* Applying cloud mask')
    apply_cloud_mask(outname, mask) 

def gen_mndwi(safe, outname, level, mask=None, rayleigh=False):
  # check for band geotiffs
  # extract bands from .jp2 file if necessary
  g_check = glob.glob(os.path.join(safe, '*B03_20m.tif'))
  if g_check:
    g_file = g_check[0]
    print('\t* Opening B3 file')
  else:
    print('\t* Extracting B3')
    g_file = extract_band_geotiffs('B03', safe, level, '20')
  swir_check = glob.glob(os.path.join(safe, '*B11_20m.tif'))
  if swir_check:
    swir_file = swir_check[0]
    print('\t* Opening B11 file')
  else:
    print('\t* Extracting B11')
    swir_file = extract_band_geotiffs('B11', safe, level, '20')
 
  # read SWIR and green band
  swir, in_geo, projref = get_geo(swir_file) 
  g, in_geo, projref = get_geo(g_file)

  # apply rayleigh correction to green band
  if rayleigh:
    g_ray = get_rayleigh_correction(g_file, 'B03')
    g = g - g_ray

  # determine shape
  rows, cols = np.shape(g)

  # clip bands
  g = np.clip(g, 0, 1)
  swir = np.clip(swir, 0, 1)

  # calculate mNDWI
  print('\t* Calculating MNDWI')
  mndwi = np.zeros((rows,cols))
  mndwi[:] = 999
  valid = np.where((g > 0) & (swir >0))
  mndwi[valid] = (g[valid] - swir[valid])/(g[valid] + swir[valid])

  # write mNDWI image to file
  print('\t* Generating MNDWI geotiff')
  result = dump_geotiff_float(outname, mndwi, projref, in_geo)
  
  # update no data value in metadata
  cmd = f"python /usr/local/anaconda3/bin/gdal_edit.py -a_nodata 999 {outname}"
  os.system(cmd)

  # apply cloud mask
  if mask:
    print('\t* Applying cloud mask')
    apply_cloud_mask(outname, mask)

def gen_ndvi(safe, outname, level, mask=None, rayleigh=False):
  # check for band geotiffs
  # extract bands from .jp2 file if necessary
  r_check = glob.glob(os.path.join(safe, '*B04_10m.tif'))
  if r_check:
    r_file = r_check[0]
    print('\t* Opening B4 file')
  else:
    print('\t* Extracting B4')
    r_file = extract_band_geotiffs('B04', safe, level, '10')
  nir_check = glob.glob(os.path.join(safe, '*B08_10m.tif'))
  if nir_check:
    nir_file = nir_check[0]
    print('\t* Opening B8 file')
  else:
    print('\t* Extracting B8')
    nir_file = extract_band_geotiffs('B08', safe, level, '10')
  
  # read in NIR and red bands
  nir, in_geo, projref = get_geo(nir_file)
  r, in_geo, projref = get_geo(r_file)
  
  # apply rayleigh correction to red band
  if rayleigh:
    r_ray = get_rayleigh_correction(r_file, 'B04')
    r = r - r_ray
  
  # determine shape
  rows, cols = np.shape(r)
  
  # clip bands
  r = np.clip(r, 0, 1)
  nir = np.clip(nir, 0, 1)

  # calculate NDVI
  print('\t* Calculating NDVI')
  ndvi = np.zeros((rows,cols))
  ndvi[:] = 999
  valid = np.where((r > 0) & (nir > 0))
  ndvi[valid] = (nir[valid]-r[valid])/(nir[valid]+r[valid])
  
  # write NDVI image to file
  print('\t* Generating NDVI geotiff')
  result = dump_geotiff_float(outname, ndvi, projref, in_geo)

  # update no data value in metadata
  cmd = f"python /usr/local/anaconda3/bin/gdal_edit.py -a_nodata 999 {outname}"
  os.system(cmd)

  # apply cloud mask
  if mask:
    print('\t* Applying cloud mask')
    apply_cloud_mask(outname, mask)

def gen_nbr(safe, outname, level, mask=None):
  # check for band geotiffs
  # extract bands from .jp2 file if necessary
  nir_check = glob.glob(os.path.join(safe, '*B8A_20m.tif'))
  if nir_check:
    nir_file = nir_check[0]
    print('\t* Opening B8A file')
  else:
    print('t* Extracting B8A')
    nir_file = extract_band_geotiffs('B8A', safe, level, '20')
  swir_check = glob.glob(os.path.join(safe, '*B12_20m.tif'))
  if swir_check:
    swir_file = swir_check[0]
    print('\t* Opening B12 file')
  else:
    print('\t* Extracting B12')
    swir_file = extract_band_geotiffs('B12', safe, level, '20')

  # read in SWIR and NIR bands
  swir, in_geo, projref = get_geo(swir_file)
  nir, in_geo, projref = get_geo(nir_file)

  # determine shape
  rows, cols = np.shape(nir)

  # clip bands
  nir = np.clip(nir, 0, 1)
  swir = np.clip(swir, 0, 1)

  # calculate NBR
  print('\t* Calculating NBR')
  nbr = np.zeros((rows,cols))
  nbr[:] = 999
  valid = np.where((nir > 0) & (swir > 0))
  nbr[valid] = (nir[valid] - swir[valid]) / (nir[valid] + swir[valid])

  # write NBR image to file
  print('\t* Generating NBR geotiff')
  result = dump_geotiff_float(outname, nbr, projref, in_geo)

  # update no data value in metadata
  cmd = f"python /usr/local/anaconda3/bin/gdal_edit.py -a_nodata 999 {outname}"
  os.system(cmd) 

  # apply cloud mask
  if mask:
    print('\t* Applying cloud mask')
    apply_cloud_mask(outname, mask)

def download_cdl(image, year, outname):
  ## Getting corners of image in Albers projection
    im_rst = gdal.Open(image)
    im_albers = image.replace('.tif', '_albers.tif')
    warp = gdal.Warp(im_albers, im_rst, dstSRS='EPSG:5072')
    warp = None
    im_albers_rst = gdal.Open(im_albers)
    ulx, xres, xskew, uly, yskew, yres  = im_albers_rst.GetGeoTransform()
    lrx = ulx + (im_albers_rst.RasterXSize * xres)
    lry = uly + (im_albers_rst.RasterYSize * yres)
    os.remove(im_albers)

    ## Getting CDL from Web Geo-Processing Service
    cdl_file_request_url = f"https://nassgeodata.gmu.edu/axis2/services/CDLService/GetCDLFile?year={year}&bbox={round(ulx)},{round(lry)},{round(lrx)},{round(uly)}"
    tif_url_response = requests.get(cdl_file_request_url, stream=True)
    response_content = tif_url_response.text
    # get data URL returned from API
    url_start = response_content.index("https")
    url_end = response_content.index("tif")+3
    tif_url = response_content[url_start : url_end]
    del tif_url_response
    # request actual CDL data
    cdl_tif_request = requests.get(tif_url, stream = True)
    
    # write CDL data to file
    with open(outname, 'wb') as out_file:
        cdl_tif_request.raw.decode_content = True
        shutil.copyfileobj(cdl_tif_request.raw, out_file)
        del cdl_tif_request
    
    # reproject, resample CDL to match inputted image
    match_geotiff(outname, image, outname)

def reclass_cdl(cdl_path, no_data_locs):
    # WARNING: AOIs that extend outside of the U.S. boundaries (ocean, Canada, Mexico) will have no data
    # cropland/grassland = 1, developed = 2, other vegetation =3, permanent water=4, no data = 999
    codes_dict = {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1, 10: 1, 11: 1, 12: 1, 13: 1, \
    14: 1, 21: 1, 22: 1, 23: 1, 24: 1, 25: 1, 26: 1, 27: 1, 28: 1, 29: 1, 30: 1, \
    31: 1, 32: 1, 33: 1, 34: 1, 35: 1, 36: 1, 37: 1, 38: 1, 39: 1, 41: 1, 42: 1, \
    43: 1, 44: 1, 45: 1, 46: 1, 47: 1, 48: 1, 49: 1, 50: 1, 51: 1, 52: 1, 53: 1, \
    54: 1, 55: 1, 56: 1, 57: 1, 58: 1, 59: 1, 60: 1, 61: 1, 66: 1, 67: 1, 68: 1, \
    69: 1, 70: 1, 71: 1, 72: 1, 74: 1, 75: 1, 76: 1, 77: 1, 204: 1, 205: 1, 206: 1, \
    207: 1, 208: 1, 209: 1, 210: 1, 211: 1, 212: 1, 213: 1, 214: 1, 215: 1, 216: 1, \
    217: 1, 218: 1, 219: 1, 220: 1, 221: 1, 222: 1, 223: 1, 224: 1, 225: 1, 226: 1, \
    227: 1, 228: 1, 229: 1, 230: 1, 231: 1, 232: 1, 233: 1, 234: 1, 235: 1, 236: 1, \
    237: 1, 238: 1, 239: 1, 240: 1, 241: 1, 242: 1, 243: 1, 244: 1, 245: 1, 246: 1, \
    247: 1, 248: 1, 249: 1, 250: 1, 254: 1, 176: 1, \
    121: 2, 122: 2, 123: 2, 124: 2, \
    131: 3, 141: 3, 142: 3, 143: 3, 152: 3, 190: 3, 195: 3, \
    111: 4, 112: 4, 92: 4, \
    0: 999, 81: 999, 88: 999}

    # open full CDL file
    cdl_rst = rio.open(cdl_path)
    cdl_array = cdl_rst.read()[0]

    # reclassify the CDL
    translated_codes = np.vectorize(codes_dict.get)(cdl_array).astype('uint16')
    translated_codes[no_data_locs] = 999

    # write the reclassified CDL to file
    cdl_reclass_out_file = cdl_path.replace('.tif', '_reclass.tif')
    dump_geotiff(translated_codes, cdl_rst.crs, cdl_rst.transform, 999, cdl_reclass_out_file)
    return cdl_reclass_out_file

def download_worldcover(image, year, outname):
  outdir = Path(outname).parent
  
  # convert image to WGS 84
  im_rst = gdal.Open(image)
  im_4326_path = image.replace('.tif', '_4326.tif')
  warp = gdal.Warp(im_4326_path, im_rst, dstSRS='EPSG:4326')
  del warp
  im_4326 = gdal.Open(im_4326_path)

  if year in ['2015','2016', '2017', '2018', '2019']:
    print('\t* Incomplete. Mostly written though. Just needs a few tweaks probably.')
    quit()
    '''
    # function for getting formatted longitude
    def get_lon(x):
        if x >= 0:
            direction = 'E'
        else: 
            direction = 'W'
        lon = direction + str(abs(x)).zfill(3)
        return lon
    
    # function for getting formatted latitude
    def get_lat(y):
        if y >= 0:
            direction = 'N'
        else:
            direction = 'S'
        lat = direction + str(abs(y)).zfill(2)
        return lat
    # get bounds
    ulx, xres, xskew, uly, yskew, yres  = im_4326.GetGeoTransform()
    lrx = ulx + (im_4326.RasterXSize * xres)
    lry = uly + (im_4326.RasterYSize * yres)
    
    # Get longitudes and latitudes
    lon_values = list(range(math.floor(ulx/20) * 20, math.floor(lrx/20) * 20 + 1, 20))
    lat_values = list(range(math.ceil(lry/20) * 20, math.ceil(uly/20) * 20 +1, 20))

    # Grab all GLC tiles from S3 buckets
    for x in lon_values:
        for y in lat_values:
            lat_lon = get_lon(x) + get_lat(y)
            s3_url_base = f's3://vito.landcover.global/v3.0.1/{year}/{lat_lon}/'
            sys_output = subprocess.check_output(f"aws s3 ls --no-sign-request {s3_url_base} | grep Discrete-Classification-map_EPSG-4326.tif", shell=True)
            filename = sys_output.decode().split(" ")[-1].rstrip()
            com = f"aws s3 cp --no-sign-request {s3_url_base}{filename} {outdir}"
            os.system(com)
            print('\t* ', lat_lon, "Done")

    print("All available GLC tiles downloaded.")
    glc_tiles = glob.glob(os.path.join(outdir, "*LC100*.tif"))
    '''

  elif year in ['2020', '2021']:
    
    # load worldcover grid
    s3_url_prefix = "https://esa-worldcover.s3.eu-central-1.amazonaws.com"
    grid_url = f'{s3_url_prefix}/v100/2020/esa_worldcover_2020_grid.geojson'
    grid = gpd.read_file(grid_url)
    
    # get bounds of image
    bounds = rio.open(im_4326_path).bounds
    geom = box(*bounds)
    
    # find where SAR image bounds intersect worldcover grid
    tiles = grid[grid.intersects(geom)]
    
    # set algorithm version
    versions = {'2020': 'v100', '2021': 'v200'}
    version = versions[year]
    
    s3 = boto3.client('s3', config=Config(signature_version=UNSIGNED))
    # download each GLC tile that intersects the SAR image
    for tile in tiles.ll_tile:
        bucket = 'esa-worldcover'
        bucket_dir = f'{version}/{year}/map/'
        filename = f'ESA_WorldCover_10m_{year}_{version}_{tile}_Map.tif'
        outfile = os.path.join(outdir, filename)
        s3.download_file(bucket, bucket_dir+filename, outfile)

    glc_tiles = glob.glob(os.path.join(outdir, "ESA_WorldCover*.tif"))
  
  else:
    print(f'\t* No WorldCover for {year}')
    quit()
  
  # delete WGS84 version
  os.remove(im_4326_path)

  # merging tiles
  if len(tiles) > 1:
    gen_merge(glc_tiles, outname)
    for tile in glc_tiles:
        os.remove(tile)
  else:
    os.rename(glc_tiles[0], outname)
  
  # reproject, resample WorldCover to match inputted image
  match_geotiff(outname, image, outname)
  
def reclass_worldcover(wc_path, no_data_locs):
  # cropland/grassland = 1, developed = 2, other vegetation =3, permanent water=4, no data = 999
    codes_dict = {0: 999, 10: 3, 20:3, 30:1, 40:1, 50:2, 60:3, 70:999, 80:4, 90:3, 95:3, 100:3}
    wc_rst = rio.open(wc_path)
    wc_array = wc_rst.read()[0]

    # reclassify WorldCover
    translated_codes = np.vectorize(codes_dict.get)(wc_array).astype('uint16')
    translated_codes[no_data_locs] = 999

    # write reclassified WorldCover to file
    wc_reclass_out_file = wc_path.replace('.tif', '_reclass.tif')
    dump_geotiff(translated_codes, wc_rst.crs, wc_rst.transform, 999, wc_reclass_out_file)
    return wc_reclass_out_file

def gen_water_extent(indir, nstd, outname, mask):
  # get nir band
  safes = glob.glob(os.path.join(indir, '*SAFE'))
  nir_files = glob.glob(os.path.join(indir, '*/*B08_10m.tif'))
  if len(safes) != len(nir_files):
    print('\t* Extracting B8')
    nir_files = []
    for safe in safes:
      nir_file = extract_band_geotiffs('B08', safe, 'MSIL2A', '10')
      nir_files.append(nir_file)
  
  # merge NIR files
  nir_merged = os.path.join(Path(outname).parent, 'B8_merged.tif')
  if not os.path.isfile(nir_merged):
    print('\t* Merging NIR files.')
    gen_merge(nir_files, nir_merged)

  # get year
  year = os.path.basename(indir)[:4]

  # get no data locs
  nir_rst = rio.open(nir_merged)
  nir_nd_val = nir_rst.nodata
  nir_array = nir_rst.read()[0]
  nd_locs = np.where(nir_array == nir_nd_val)

  # get image bounds in WGS84
  xmin, ymin, xmax, ymax = nir_rst.bounds
  transformer = Transformer.from_crs(nir_rst.crs, "EPSG:4326", always_xy=True)
  xmin_wgs84,  ymax_wgs84= transformer.transform(xmin,ymax)
  xmax_wgs84,  ymin_wgs84= transformer.transform(xmax,ymin)

  # U.S. boundaries
  xmax_us = -66.9513812
  xmin_us = -124.7844079
  ymax_us = 49.3457868
  ymin_us = 24.7433195

  # check if image lies copletely within U.S. boundaries
  # if yes, download CDl
  # if no, download WorldCover
  if (xmax_wgs84 < xmax_us) & (xmin_wgs84 > xmin_us) & (ymax_wgs84 < ymax_us) & (ymin_wgs84 > ymin_us):
    # this year will need to be updated annually to reflect mostly recent CDL year
    if int(year) > 2024:
          year = 2024
    
    # create CDL directory
    cdl_dir = os.path.join(Path(outname).parents[1], 'CDL')
    if not os.path.isdir(cdl_dir):
      os.mkdir(cdl_dir)
    
    # check for existing CDL files
    cdl_name = os.path.join(cdl_dir, f'CDL_{year}.tif')
    cdl_check = glob.glob(cdl_name.replace('.tif', '_reclass.tif'))
    if len(cdl_check) == 0:
      # download and resample CDL
      print('\t* Downloading CDL')
      download_cdl(nir_merged, year, cdl_name)
      ref_simple = reclass_cdl(cdl_name, nd_locs)
    else:
      print('\t* CDL already downloaded!')
      ref_simple = cdl_check[0]
    
  else:
    # most recent WorldCover year is 2021
    if int(year) > 2021:
          year = 2021

    # create WorldCover directory
    wc_dir = os.path.join(Path(outname).parents[1], 'WorldCover')
    if not os.path.isdir(wc_dir):
      os.mkdir(wc_dir)
    
    # check for existing WorldCover files
    wc_name = os.path.join(wc_dir, f'WorldCover_{year}.tif')
    wc_check = glob.glob(wc_name.replace('.tif', '_reclass.tif'))
    if len(wc_check) == 0:
      if not os.path.isfile(wc_name):
        # download WorldCover
        print('\t* Downloading WorldCover')
        download_worldcover(nir_merged, str(year), wc_name)
      # reclassify WorldCover
      ref_simple = reclass_worldcover(wc_name, nd_locs)
    else:
      print('\t* WorldCover already downloaded!')
      ref_simple = wc_check[0]

  # open reclassified CDl and cloud mask
  ref_simple_array = rio.open(ref_simple).read(1)
  cloudMask_rst = rio.open(mask)

  # resample cloud mask to match NIR band
  cloudMask = cloudMask_rst.read(1,
          out_shape=(cloudMask_rst.count,
                    int(cloudMask_rst.height * 2),
                    int(cloudMask_rst.width * 2)
          ),
          resampling=Resampling.nearest
      )

  # water pixels from reference data that are cloud-free and valid  in the NIR data
  water = np.where((ref_simple_array == 4) & (cloudMask == 0) & (nir_array != 0))
  mean = np.nanmean(nir_array[water])   # mean of water pixels
  std = np.nanstd(nir_array[water])     # standard deviation of water pixels
  nir_thresh = mean + (nstd * std)      # NIR water threshold is a given number of standard deviations above the mean
  print('\t* NIR Threshold:', nir_thresh)

  # create water extent array
  water_extent = np.zeros(nir_array.shape)
  water_extent[nir_array <= nir_thresh] = 1   # NIR values below the threshold are classified as water
  water_extent = medfilt2d(water_extent, kernel_size=5)  # filtering small areas

  # reclassify water pixels as permanent, flooded developed, flooded vegetation, etc. 

  classified_flood = np.zeros(water_extent.shape, dtype=np.byte)

  classified_flood[water] = 1  # permanent water

  flood_dev = np.where((ref_simple_array == 2) & (water_extent == 1)) # flooded developed
  classified_flood[flood_dev] = 2

  flood_veg = np.where((ref_simple_array == 3) & (water_extent == 1)) # flooded vegetation
  classified_flood[flood_veg] = 3

  flood_crop = np.where((ref_simple_array ==1) & (water_extent == 1)) # flooded cropland/grassland
  classified_flood[flood_crop] = 4 

  clouds_shadow = np.where(cloudMask == 1) # clouds and cloud shadow
  classified_flood[clouds_shadow] = 5 

  classified_flood[nir_array == 0] = 0    # no data

  # write reclassified water extent to file
  print('\t* Generating Water Extent geotiff')
  dump_geotiff(classified_flood, nir_rst.crs, nir_rst.transform, 0, outname)

def gen_merge(list_of_files, outfile, method='first'):
  # get unique CRS found in the inputted list of files to merge
  crs_list = [rio.open(im).crs for im in list_of_files]
  crs_list_unique = list(set(crs_list))
  if len(crs_list_unique) != 1:
      crs_dom = max(set(crs_list), key=crs_list.count)    # most common CRS
      # reproject each file to most common CRS
      for tif in list_of_files:
          tif_array = rio.open(tif)
          tif_crs = tif_array.crs
          if tif_crs != crs_dom:
              with rio.open(tif) as src:
                  transform, width, height = calculate_default_transform(
                      src.crs, crs_dom, src.width, src.height, *src.bounds)
                  kwargs = src.meta.copy()
                  kwargs.update({
                      'crs': crs_dom,
                      'transform': transform,
                      'width': width,
                      'height': height})
                  with rio.open(tif, 'w', **kwargs) as dst:
                      for i in range(1, src.count +1):
                          reproject(
                              source=rio.band(src, i),
                              destination=rio.band(dst, i),
                              src_transform=src.transform,
                              src_crs=src.crs,
                              dst_transform=transform,
                              dst_crs=crs_dom)
  else:
    crs_dom = crs_list_unique[0]

  # determine no data value
  nodata_val = rio.open(list_of_files[0]).nodata
  if not nodata_val:
      nodata_val = 0
  
  # number of bands
  bands = rio.open(list_of_files[0]).count

  # merge the files
  im_array, im_trans = merge(list_of_files, method=method)
  
  # write the merged images to file
  with rio.open(
      outfile, "w",
      driver = 'GTiff',
      count = bands,
      dtype = im_array.dtype,
      height = im_array.shape[1],
      width = im_array.shape[2],
      nodata = nodata_val,
      crs = crs_dom,
      transform = im_trans) as dest:
      dest.write(im_array)

def s2_merge(dir_to_merge, mask=False, method='first'): 
  # create output filename
  ims = glob.glob(os.path.join(dir_to_merge, '*tif'))
  sat = os.path.basename(ims[0]).split('_')[0]
  prod_type = os.path.basename(ims[0]).split('_')[2]
  im_date = os.path.basename(ims[0]).split('_')[3]
  merged_output = os.path.join(dir_to_merge, f'{sat}_{prod_type}_{im_date}_merged.tif')

  # merge images
  gen_merge(ims, merged_output, method)

  if mask:
    print('\t* Applying Cloud Mask')
    # find cloud mask that matches the merged image
    cm_merged = glob.glob(os.path.join(Path(dir_to_merge).parent, 'cloudMask', '*merged.tif'))[0]
    # apply cloud mask
    apply_cloud_mask(merged_output, cm_merged)
  return merged_output