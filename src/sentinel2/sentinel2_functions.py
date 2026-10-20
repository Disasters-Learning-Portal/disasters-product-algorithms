#!/usr/local/anaconda3/bin/python

"""
sentinel2_functions.py

Name:           Kaylee Sharp
Edited:         Aaron Serre

Date Created:   February 2024
Date Edited:    July 2026       

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
import tempfile
from scipy.signal import medfilt2d
from pyproj import Transformer
import geopandas as gpd
from shapely.geometry import box
from shared_utils.product_paths import product_dir
from shared_utils.landcover import (
    read_reclass, reclass_cdl_array, reclass_path, reclass_worldcover_array,
    select_water_sample, write_reclass)
import boto3
from botocore import UNSIGNED
from botocore.config import Config
from tqdm import tqdm

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

def get_radiometric_scaling(safe, level, band):
  """Return (offset, quantification) for converting `band`'s DN to reflectance.

  ESA's conversion is ``reflectance = (DN + offset) / quantification``, with
  the values read from the product-level metadata (MTD_MSIL1C.xml /
  MTD_MSIL2A.xml at the .SAFE root):

    L1C: QUANTIFICATION_VALUE     + Radiometric_Offset_List/RADIO_ADD_OFFSET
    L2A: BOA_QUANTIFICATION_VALUE + BOA_ADD_OFFSET_VALUES_LIST/BOA_ADD_OFFSET

  The offset (-1000 for every band) was introduced with processing baseline
  04.00 on 2022-01-25 and is carried by every reprocessed Collection-1
  product. Products from earlier baselines have no offset list, so the offset
  is 0 there -- it must be read per product, never hardcoded. Ignoring it adds
  +0.1 to every band, which does not cancel in a normalized difference.
  Offsets are keyed by band_id, which the metadata's Spectral_Information
  list maps to the physical band name (B1..B12, B8A).

  Refs: https://sentiwiki.copernicus.eu/web/s2-processing
        https://forum.step.esa.int/t/info-introduction-of-additional-radiometric-offset-in-pb04-00-products/35431
  """
  mtd = os.path.join(safe, f'MTD_{level}.xml')
  if not os.path.isfile(mtd):
    raise FileNotFoundError(f'product metadata {mtd} not found; cannot convert DN to reflectance')
  doc = minidom.parse(mtd)

  if level == 'MSIL2A':
    quant_tag, offset_tag = 'BOA_QUANTIFICATION_VALUE', 'BOA_ADD_OFFSET'
  else:
    quant_tag, offset_tag = 'QUANTIFICATION_VALUE', 'RADIO_ADD_OFFSET'

  quant_nodes = doc.getElementsByTagName(quant_tag)
  if not quant_nodes:
    raise ValueError(f'{quant_tag} missing from {mtd}')
  quantification = float(quant_nodes[0].firstChild.data)

  # 'B08' -> 'B8', 'B8A' -> 'B8A', 'B11' -> 'B11' (the metadata's spelling)
  physical = 'B8A' if band == 'B8A' else f'B{int(band[1:])}'
  band_id = None
  for node in doc.getElementsByTagName('Spectral_Information'):
    if node.getAttribute('physicalBand') == physical:
      band_id = node.getAttribute('bandId')
      break

  offset = 0.0
  for node in doc.getElementsByTagName(offset_tag):
    if node.getAttribute('band_id') == band_id:
      offset = float(node.firstChild.data)
      break

  return offset, quantification

def dn_to_reflectance(dn, offset, quantification):
  """Convert Sentinel-2 DN to reflectance, with the NODATA DN (0) as NaN.

  NaN, not 0, marks nodata: once the offset is applied, a real dark pixel
  (e.g. clear water in NIR) can legitimately be <= 0 -- preserving that noise
  instead of truncating it is why ESA added the offset -- so a reflectance
  value can no longer double as the fill flag.
  """
  refl = (dn.astype(np.float32) + np.float32(offset)) / np.float32(quantification)
  refl[dn == 0] = np.nan
  return refl

def extract_band_geotiffs(band, safe, level, res):
  if level == 'MSIL2A':
    # find .jp2 file matching band/resolution
    jp2_matches = glob.glob(safe + f'/GRANULE/*/IMG_DATA/R{res}m/*{band}*.jp2')
    if not jp2_matches:
      raise FileNotFoundError(f'band {band} ({res}m) not found in {safe} (GRANULE/*/IMG_DATA/R{res}m/)')
    jp2_file = jp2_matches[0]

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
      band_array = dn_to_reflectance(band_file.read(1), *get_radiometric_scaling(safe, level, band))
      nd_val = np.nan
    
    # write band to file
    dump_geotiff(band_array, band_geo['crs'], band_geo['transform'], nd_val, outfile)

  elif level == 'MSIL1C':
    # find .jp2 file matching band
    jp2_matches = glob.glob(safe + f'/GRANULE/*/IMG_DATA/*{band}*.jp2')
    if not jp2_matches:
      raise FileNotFoundError(f'band {band} ({res}m) not found in {safe} (GRANULE/*/IMG_DATA/) -- verify the L1C product contains this band')
    jp2_file = jp2_matches[0]

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
        band_array = dn_to_reflectance(band_array, *get_radiometric_scaling(safe, level, band))
        dump_geotiff(band_array, dataset.crs, transform, np.nan, outfile)

    else:
      band_file = rio.open(jp2_file)
      band_geo = band_file.profile
      band_array = dn_to_reflectance(band_file.read(1), *get_radiometric_scaling(safe, level, band))
      dump_geotiff(band_array, band_geo['crs'], band_geo['transform'], np.nan, outfile)

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

  # Validity for the alpha band: a pixel is valid only where ALL THREE bands
  # carry data -- an incomplete triple is not renderable colour. Mirrors
  # Satellogic's `isfinite(red) & isfinite(green) & isfinite(blue)`.
  #
  # Nodata is NaN (extract_band_geotiffs), so validity comes from finiteness,
  # never from the value: with ESA's radiometric offset applied, and after
  # Rayleigh subtraction, a real dark pixel can be <= 0. It is also derived
  # BEFORE the stretch, not from the enhanced uint8 output, which can drive a
  # legitimately dark pixel to 0.
  valid = np.isfinite(r) & np.isfinite(g) & np.isfinite(b)
  alpha = (valid * 255).astype(np.uint8)
  r = np.nan_to_num(r, nan=0.0)
  g = np.nan_to_num(g, nan=0.0)
  b = np.nan_to_num(b, nan=0.0)

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

  # bytescale bands. Clamp to [0, 255] BEFORE the uint8 assignment below:
  # reflectance in (0, rgb_min) stretches negative, and a negative float cast
  # to uint8 wraps (0.01 -> 249), rendering dark targets such as water bright.
  rgb_min = 0.04
  rgb_max = 1
  b_enhanced = np.clip(((b - rgb_min) / (rgb_max - rgb_min))*255, 0, 255)
  g_enhanced = np.clip(((g - rgb_min) / (rgb_max - rgb_min))*255, 0, 255)
  r_enhanced = np.clip(((r - rgb_min) / (rgb_max - rgb_min))*255, 0, 255)

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
  r[~valid] = 0
  g[~valid] = 0
  b[~valid] = 0

  # return bands, alpha mask and geographic information
  return r,g,b,alpha,projref,in_geo

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
  base = os.path.splitext(os.path.basename(tif_to_mask))[0]
  parts = base.split("_") 
  timestamp = parts[-1]
  prefix = "_".join(parts[:-1])
  masked_out_file = f"{prefix}_masked_{timestamp}.tif"

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

  # The source raster may have no nodata set (nodata is None) -- common for the
  # 8-bit true-color product. NumPy can't assign None into an int array, so use
  # a concrete fill (0) for the masked / transparent pixels in that case.
  nd = tif_to_mask_rst.nodata if tif_to_mask_rst.nodata is not None else 0

  if tif_to_mask_rst.count == 1:
    # mask images with one band (e.g., NDVI, EVI, etc.)
    tif_to_mask_array = tif_to_mask_rst.read(1)
    tif_to_mask_array[mask_array == 1] = nd
    dump_geotiff(tif_to_mask_array, tif_to_mask_rst.crs, tif_to_mask_rst.transform, nd, masked_path)
  else:
    # mask images with three bands (e.g., true color, color infrared, etc.)
    # outputs a 4-band image with the fourth band being an alpha band

    # create alpha band array
    band1_array = tif_to_mask_rst.read(1)
    nodata_array = np.full(band1_array.shape, 255)
    # Only flag existing-nodata pixels when the source actually defines a nodata
    # value; otherwise there are none to flag (don't mistakenly treat 0 as nodata).
    if tif_to_mask_rst.nodata is not None:
      nodata_array[band1_array == tif_to_mask_rst.nodata] = nd
    nodata_array[mask_array == 1] = nd

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
  r,g,b,alpha,projref,in_geo = gen_rgb(r_file, g_file, b_file, rayleigh, enhance_level)

  # write true color image to file
  print('\t* Generating true color geotiff')
  result = dump_geotiff_rgb(outname, r, g, b, projref, in_geo, alpha=alpha)

  # apply cloud mask
  if mask is not None:
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
  r,g,b,alpha,projref,in_geo = gen_rgb(r_file, g_file, b_file, rayleigh, enhance_level)
  
  # write natural color image to file
  print('\t* Generating natural color geotiff')
  result = dump_geotiff_rgb(outname, r, g, b, projref, in_geo, alpha=alpha)

  # apply cloud mask
  if mask is not None:
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
  r,g,b,alpha,projref,in_geo = gen_rgb(r_file, g_file, b_file, rayleigh)

  # write short wave infrared image to file
  print('\t* Generating short wave infrared geotiff')
  result = dump_geotiff_rgb(outname, r, g, b, projref, in_geo, alpha=alpha)

  # apply cloud mask
  if mask is not None:
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
  r,g,b,alpha,projref,in_geo = gen_rgb(r_file, g_file, b_file, rayleigh)

  # write color infrared image to file
  print('\t* Generating color infrared geotiff')
  result = dump_geotiff_rgb(outname, r, g, b, projref, in_geo, alpha=alpha)

  # apply cloud mask
  if mask is not None:
    print('\t* Applying cloud mask')
    apply_cloud_mask(outname, mask)

def normalized_difference(a, b, nodata=-9999.0):
  """(a - b) / (a + b), nodata where either input is nodata (NaN).

  Inputs are clamped to the physical range [0, 1] first. With ESA's offset
  applied, real dark pixels can carry small negative reflectance (measured:
  84% of clear Lake Tahoe water has B08 < 0 in a PB 05.11 L2A scene), which
  would push the index past +/-1. Reflectance cannot be negative, so the clamp
  projects that noise onto its physical floor: clear water with NIR ~0 gives
  NDWI = 1, its correct limit, instead of a nodata hole. A zero denominator
  (both bands at 0) is nodata -- the ratio is undefined there.
  """
  valid = np.isfinite(a) & np.isfinite(b)
  a = np.clip(a, 0, 1)
  b = np.clip(b, 0, 1)
  denom = a + b
  valid &= denom > 0
  out = np.full(np.shape(a), nodata, dtype=np.float64)
  out[valid] = (a[valid] - b[valid]) / denom[valid]
  return out

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

  # calculate NDWI
  print('\t* Calculating NDWI')
  ndwi = normalized_difference(g, nir)

  # write NDWI image to file
  print('\t* Generating NDWI geotiff')
  result = dump_geotiff_float(outname, ndwi, projref, in_geo, nodata=-9999.0)

  # apply cloud mask
  if mask is not None:
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

  # calculate mNDWI
  print('\t* Calculating MNDWI')
  mndwi = normalized_difference(g, swir)

  # write mNDWI image to file
  print('\t* Generating MNDWI geotiff')
  result = dump_geotiff_float(outname, mndwi, projref, in_geo, nodata=-9999.0)

  # apply cloud mask
  if mask is not None:
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
  
  # calculate NDVI
  print('\t* Calculating NDVI')
  ndvi = normalized_difference(nir, r)
  
  # write NDVI image to file
  print('\t* Generating NDVI geotiff')
  result = dump_geotiff_float(outname, ndvi, projref, in_geo, nodata=-9999.0)

  # apply cloud mask
  if mask is not None:
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

  # calculate NBR
  print('\t* Calculating NBR')
  nbr = normalized_difference(nir, swir)

  # write NBR image to file
  print('\t* Generating NBR geotiff')
  result = dump_geotiff_float(outname, nbr, projref, in_geo, nodata=-9999.0)

  # apply cloud mask
  if mask is not None:
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
    # Buckets and the threshold-sample codes come from the ONE shared table
    # (shared_utils.landcover, .clinerules.md rule 65).
    cdl_rst = rio.open(cdl_path)
    cdl_array = cdl_rst.read(1)
    nd_mask = np.zeros(cdl_array.shape, dtype=bool)
    nd_mask[no_data_locs] = True
    buckets, sample = reclass_cdl_array(cdl_array, nd_mask)
    return write_reclass(reclass_path(cdl_path), buckets, sample, cdl_rst.crs, cdl_rst.transform)

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
    # Buckets and the threshold-sample codes come from the ONE shared table
    # (shared_utils.landcover, .clinerules.md rule 65).
    wc_rst = rio.open(wc_path)
    wc_array = wc_rst.read(1)
    nd_mask = np.zeros(wc_array.shape, dtype=bool)
    nd_mask[no_data_locs] = True
    buckets, sample = reclass_worldcover_array(wc_array, nd_mask)
    return write_reclass(reclass_path(wc_path), buckets, sample, wc_rst.crs, wc_rst.transform)

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
  # Band nodata is NaN (extract_band_geotiffs); a real dark-water reflectance
  # can be <= 0 once ESA's offset is applied, so finiteness is the only valid
  # nodata test. (`== nodata` is always False for NaN.)
  nir_valid = np.isfinite(nir_array)
  if nir_nd_val is not None and not np.isnan(nir_nd_val):
    nir_valid &= nir_array != nir_nd_val
  nd_locs = np.where(~nir_valid)

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
    cdl_check = glob.glob(reclass_path(cdl_name))
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
    wc_check = glob.glob(reclass_path(wc_name))
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
  ref_simple_array, ref_sample_mask = read_reclass(ref_simple)
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
  # Two masks, as in the STAC path (.clinerules.md rules 54/65): every
  # bucket-4 pixel is LABELLED permanent water, but only the strict
  # endmember (CDL 111 / WorldCover 80) SEEDS the NIR threshold --
  # aquaculture is real water but turbid and periodically drained.
  permanent_water = (ref_simple_array == 4) & (cloudMask == 0) & nir_valid
  sample = ref_sample_mask & (cloudMask == 0) & nir_valid
  water = np.where(select_water_sample(permanent_water, sample))
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

  classified_flood[permanent_water] = 1  # permanent water

  flood_dev = np.where((ref_simple_array == 2) & (water_extent == 1)) # flooded developed
  classified_flood[flood_dev] = 2

  flood_veg = np.where((ref_simple_array == 3) & (water_extent == 1)) # flooded vegetation
  classified_flood[flood_veg] = 3

  flood_crop = np.where((ref_simple_array ==1) & (water_extent == 1)) # flooded cropland/grassland
  classified_flood[flood_crop] = 4 

  clouds_shadow = np.where(cloudMask == 1) # clouds and cloud shadow
  classified_flood[clouds_shadow] = 5 

  classified_flood[~nir_valid] = 0    # no data

  # write reclassified water extent to file
  print('\t* Generating Water Extent geotiff')
  dump_geotiff(classified_flood, nir_rst.crs, nir_rst.transform, 0, outname)

def gen_merge(list_of_files, outfile, method='first'):
  # get unique CRS found in the inputted list of files to merge
  crs_list = [rio.open(im).crs for im in list_of_files]
  crs_list_unique = list(set(crs_list))

  # Files actually fed to merge(). When CRSs disagree, each mismatched scene is
  # reprojected to the dominant CRS in a TEMP directory and swapped in here. We
  # never reopen a source scene in 'w' mode: doing so used to overwrite (and
  # truncate mid-read) the input file in place, corrupting the merge. Mirrors the
  # Landsat gen_merge fix (landsat89_functions.py).
  merge_inputs = list(list_of_files)
  tmp_dir = None
  try:
    if len(crs_list_unique) != 1:
        crs_dom = max(crs_list, key=crs_list.count)    # most common CRS
        tmp_dir = tempfile.mkdtemp(prefix='s2_merge_reproj_')
        for idx, tif in enumerate(list_of_files):
            with rio.open(tif) as src:
                if src.crs == crs_dom:
                    continue
                transform, width, height = calculate_default_transform(
                    src.crs, crs_dom, src.width, src.height, *src.bounds)
                kwargs = src.meta.copy()
                kwargs.update({
                    'crs': crs_dom,
                    'transform': transform,
                    'width': width,
                    'height': height})
                reproj_path = os.path.join(tmp_dir, f'{idx}_{os.path.basename(tif)}')
                with rio.open(reproj_path, 'w', **kwargs) as dst:
                    for i in range(1, src.count + 1):
                        reproject(
                            source=rio.band(src, i),
                            destination=rio.band(dst, i),
                            src_transform=src.transform,
                            src_crs=src.crs,
                            dst_transform=transform,
                            dst_crs=crs_dom)
                merge_inputs[idx] = reproj_path
    else:
      crs_dom = crs_list_unique[0]

    # determine no data value
    nodata_val = rio.open(merge_inputs[0]).nodata
    if not nodata_val:
        nodata_val = 0

    # number of bands
    bands = rio.open(merge_inputs[0]).count

    # merge the files
    im_array, im_trans = merge(merge_inputs, method=method)

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
  finally:
    if tmp_dir is not None:
        shutil.rmtree(tmp_dir, ignore_errors=True)

def s2_merge(dir_to_merge, mask=False, method='first'):
  # Gather inputs, excluding any prior merged output so a re-merge (e.g. -force)
  # doesn't fold the previous mosaic back into the new one. Raise (not IndexError
  # on ims[0]) when the product dir has no output -- mirrors ls_merge so the merge
  # loop can skip a product that failed to generate instead of crashing the job.
  ims = sorted(
      im for im in glob.glob(os.path.join(dir_to_merge, '*tif'))
      if 'merged' not in os.path.basename(im)
  )
  if not ims:
      raise FileNotFoundError(f'No (non-merged) .tif files to merge in: {dir_to_merge}')

  parts = os.path.splitext(os.path.basename(ims[0]))[0].split("_")

  sat = parts[0]
  level = parts[1]
  product = parts[2]
  timestamp = parts[-1]

  merged_output = os.path.join(
      dir_to_merge,
      f"{sat}_{level}_{product}_merged_{timestamp}.tif"
  )

  # merge images
  gen_merge(ims, merged_output, method)

  if mask:
    print('\t* Applying Cloud Mask')
    # Find the merged cloud mask. Use *merged*.tif (not *merged.tif): the merged
    # cloud mask is renamed to e.g. S2B_cloudMask_merged_2026-06-06_day.tif
    # (date moved to end + _day), so it no longer *ends* in "merged.tif".
    # *merged*.tif matches both the pre-rename and renamed forms.
    cm_merged = glob.glob(os.path.join(Path(dir_to_merge).parent, product_dir('sentinel2', 'cloudMask'), '*merged*.tif'))[0]
    # apply cloud mask
    apply_cloud_mask(merged_output, cm_merged)
    # apply_cloud_mask writes <dir_to_merge>/masked/<prefix>_masked_<timestamp>.tif.
    # Return THAT path (not the unmasked merge) so the caller COGs/uploads the
    # masked product. Reconstructed from the same split apply_cloud_mask uses, so
    # it is byte-identical to what was written. Mirrors Landsat ls_merge.
    base = os.path.splitext(os.path.basename(merged_output))[0]
    mparts = base.split("_")
    masked_basename = f"{'_'.join(mparts[:-1])}_masked_{mparts[-1]}.tif"
    return os.path.join(Path(merged_output).parent, 'masked', masked_basename)
  return merged_output