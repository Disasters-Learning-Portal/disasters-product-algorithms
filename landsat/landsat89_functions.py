#!/usr/local/anaconda3/bin/python

"""
landsat89_functions.py

Name:        Kaylee Sharp
Date:        February 2025

"""

import glob
import os
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
import boto3
from botocore import UNSIGNED
from botocore.config import Config

def unzip_landsat(files_to_unzip, unpacked_dir):
    print(f'\nNumber of .tar / .zip files: {len(files_to_unzip)}')
    new_unpacked_dirs = []

    # make unpacked directory
    if not os.path.isdir(unpacked_dir):
        os.mkdir(unpacked_dir)

    for zip_file in files_to_unzip:
        # create date directory within unpacked directory
        date = os.path.basename(zip_file).split('_')[3]
        date_dir = os.path.join(unpacked_dir, date)
        if not os.path.isdir(date_dir):
          os.mkdir(date_dir)
        
        # check file ending
        file_end = zip_file.split('.')[-1]
        
        # unpack .tar file 
        if file_end =='tar':
            zip_base = os.path.basename(zip_file)
            unzip_dir = os.path.join(date_dir, zip_base.replace('.tar', '_unpacked'))
            if os.path.isdir(unzip_dir):
                print(f"\t{zip_file} already unzipped")
            else:
                print('\tUnzipping:', zip_file)
                os.mkdir(unzip_dir)
                new_unpacked_dirs.append(unzip_dir)
                cmd = f'/usr/bin/tar -xf {zip_file} -C {unzip_dir}'
                os.system(cmd)
        
        # unpack .zip file
        elif file_end == 'zip':
            zip_base = os.path.basename(zip_file)
            unzip_dir = os.path.join(date_dir, zip_base.replace('.zip', '_unpacked'))
            if os.path.isdir(unzip_dir):
                print(f"\t{zip_file} already unzipped")
            else:
                print('\tUnzipping:', zip_file)
                os.mkdir(unzip_dir)
                new_unpacked_dirs.append(unzip_dir)
                cmd = f'unzip -q {zip_file} -d {unzip_dir}'
                os.system(cmd)

                # move files in vendor_metadata to main data directory
                vendor_dir = os.path.join(unzip_dir, 'vendor_metadata')
                if os.path.isdir(vendor_dir):
                    vendor_files = glob.glob(os.path.join(vendor_dir, '*'))
                    for vendor_file in vendor_files:
                        shutil.move(vendor_file, unzip_dir)
                    os.rmdir(vendor_dir)

        # update permissions            
        cmd = '/bin/chmod -R ug+rwx ' + unzip_dir
        os.system(cmd)
    return new_unpacked_dirs

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

def match_geotiff(srcfile, matchfile, outfile):
  # open the two files
  src_ds = gdal.Open(srcfile)
  cols = src_ds.RasterXSize
  rows = src_ds.RasterYSize
  img = src_ds.GetRasterBand(1).ReadAsArray(0,0,cols,rows)
  match_ds = gdal.Open(matchfile)
  cols = match_ds.RasterXSize
  rows = match_ds.RasterYSize
  # create the result
  result_ds = gdal.GetDriverByName('GTiff').Create(outfile, match_ds.RasterXSize, match_ds.RasterYSize, 1, gdal.GDT_Float32)
  # create result's projection and transform to be the matching one
  result_ds.SetGeoTransform(match_ds.GetGeoTransform())
  result_ds.SetProjection(match_ds.GetProjection())
  # reproject
  #res = gdal.ReprojectImage(src_ds, result_ds, src_ds.GetProjection(), match_ds.GetProjection(), gdalconst.GRA_Bilinear)
  res = gdal.ReprojectImage(src_ds, result_ds, src_ds.GetProjection(), match_ds.GetProjection(), gdal.GRA_NearestNeighbour)
  img = result_ds.GetRasterBand(1).ReadAsArray()
  # http://jgomezdans.github.io/gdal_notes/reprojection.html
  #driver = gdal.GetDriverByName('GTiff')
  #dst_ds = driver.CreateCopy(outfile, result_ds, img) #0)
  result_ds.GetRasterBand(1).WriteArray(img)
  result_ds = None
  ds = gdal.Open(outfile)
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  img = ds.GetRasterBand(1).ReadAsArray(0,0,cols,rows)
  return outfile

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

def gen_cloudMask(ddir, outname):
  # check for quality file
  qa_check = glob.glob(os.path.join(ddir, '*QA_PIXEL.TIF'))
  if not qa_check:
    print('\t* Missing quality file!')
    return None
  else:
    print('\t* Opening QA file')
    qa_file = qa_check[0]
    print('\t* Generating Cloud Mask geotiff')
    qa_rst = rio.open(qa_file)
    qa = qa_rst.read(1)

    # cloud = 1, no cloud = 0, no data = 999
    mask = np.ones(qa.shape, 'uint16')
    mask[qa == 1] = 999
    mask[qa == 21824] = 0
    mask[qa == 21952] = 0
    dump_geotiff(mask, qa_rst.crs, qa_rst.transform, 999, outname)
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

def getSolarZenithAngle(indir):
  # Extract solar elevation from metadata and
  # calculate solar zenith angle
  
  metadataFile = glob.glob(os.path.join(indir, '*_MTL.xml'))[0]
  metadata = ET.parse(metadataFile)
  root = metadata.getroot()
  sun_elevation = root.find('IMAGE_ATTRIBUTES').find('SUN_ELEVATION').text
  solarZenithAngle = 90. - float(sun_elevation)
  return solarZenithAngle

def getReflectanceConstants(indir, band):  
  # Extract multiplicative scale factor and additive offset from metadata

  metadataFile = glob.glob(os.path.join(indir, '*_MTL.xml'))[0]
  level = os.path.basename(metadataFile).split('_')[1][1]
  metadata = ET.parse(metadataFile)
  root = metadata.getroot()
  if level == '1':
    params = root.find('LEVEL1_RADIOMETRIC_RESCALING') 
  if level == '2':
    params = root.find('LEVEL2_SURFACE_REFLECTANCE_PARAMETERS')
  mult_scale_factor = params.find(f'REFLECTANCE_MULT_BAND_{band}').text
  add_offset = params.find(f'REFLECTANCE_ADD_BAND_{band}').text
  return (float(mult_scale_factor), float(add_offset))

def genPanchromatic(b8_file, sunzen, outname, mask=None):
  # Function to create a Landsat 8/9 panchromatic image, based upon
  # input GeoTIFF file (full path) for B8,
  # along with appropriate metadata file (*_MTL.txt)
  ### B8 Panchromatic ###
  print(f'\n\t* Opening Band 8 file')
  indir = os.path.dirname(b8_file[0])
  ds = gdal.Open(b8_file[0], gdal.GA_ReadOnly)
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  b8 = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)

  # Convert to reflectance
  print("\t* Converting to reflectance")
  mult_scale_factor, add_offset = getReflectanceConstants(indir, '8')
  b8 = (mult_scale_factor*b8)+add_offset

  # Normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle")
  b8 = b8/np.cos(np.radians(sunzen))

  # Convert from reflectance to 8-bit values
  print("\n\t* Converting reflectance to 8-bit values")
  b8 = np.clip(b8, 0.0, 1.0)
  img = bytescale(np.sqrt(b8), cmin=0.0, cmax=1.0,low=1,high=255)

  # Use PIL to brighten it up
  print("\t* Enhancing image")
  imageArray = Image.fromarray(img)
  enhancer = ImageEnhance.Contrast(imageArray).enhance(2.5) # increase this number to increase contrast
  img = np.reshape(enhancer.getdata(band=0), (rows,cols))

  # Write a GeoTIFF
  print("\t* Generating a panchromatic geotiff")
  format = 'GTiff'
  driver = gdal.GetDriverByName(format)
  out_ds = driver.Create(outname, cols, rows, 1, gdal.GDT_Byte)
  out_cs = osr.SpatialReference()
  out_cs.ImportFromWkt(projref)
  out_ds.SetProjection(out_cs.ExportToWkt())
  out_ds.SetGeoTransform(in_geo)
  out_ds.GetRasterBand(1).WriteArray(img)
  out_ds = None

  if mask:
    print('\t* Applying cloud mask')
    apply_cloud_mask(outname, mask)

def genTrueColor( b4_file, b3_file, b2_file,qa_file, sunzen, outname, mask=None, d_o_s=True):
  # Function to create a Landsat 8/9 true color image, based upon
  # input GeoTIFF files (full path) for B4 (red), B3 (green), and
  # B2 (blue), along with appropriate metadata file (*_MTL.txt)
  
  indir = os.path.dirname(b4_file[0])

  ### Red ###
  print("\n\t* Opening Band 4 file")
  ds = gdal.Open(b4_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  r = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None
  
  # convert to reflectance
  print("\t* Converting to reflectance...")
  mult_scale_factor, add_offset = getReflectanceConstants(indir, '4')
  r_nodata = np.where(r == 0)
  r = (mult_scale_factor*r)+add_offset

  # normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle...")
  r = r/np.cos(np.radians(sunzen))
  r[r_nodata] = 0
  

  ### Green ###
  print("\n\t* Opening Band 3 file")
  ds = gdal.Open(b3_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  g = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None

  # convert to reflectance
  print("\t* Converting to reflectance")
  mult_scale_factor, add_offset = getReflectanceConstants(indir, '3')
  g_nodata = np.where(g == 0)
  g = (mult_scale_factor*g)+add_offset

  # normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle")
  g = g/np.cos(np.radians(sunzen))
  g[g_nodata] = 0
  
  ### Blue ###
  print("\n\t* Opening Band 2 file")
  ds = gdal.Open(b2_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  b = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None
  
  # convert to reflectance
  print("\t* Converting to reflectance")
  mult_scale_factor, add_offset = getReflectanceConstants(indir, '2')
  b_nodata = np.where(b == 0)
  b = (mult_scale_factor*b)+add_offset

  # normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle")
  b = b/np.cos(np.radians(sunzen))
  b[b_nodata] = 0
  

  ### Quality ###
  ds = gdal.Open(qa_file[0])
  qa = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  # Find no data pixels
  print("\n\t* Finding areas with no data pixels")
  nodatapix = np.where( qa == 1 )

  ### Convert from reflectance to 8-bit values ###
  print("\t* Converting reflectance to 8-bit values")
  r = bytescale(r, cmin=0, cmax=1, low=1 ,high=255)
  g = bytescale(g, cmin=0, cmax=1, low=1, high=255)
  b = bytescale(b, cmin=0, cmax=1, low=1, high=255)

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

  r[nodatapix] = 0
  g[nodatapix] = 0
  b[nodatapix] = 0

  # Write a GeoTIFF
  print("\n\t* Generating a true color geotiff")
  format = 'GTiff'
  driver = gdal.GetDriverByName(format)
  out_ds = driver.Create(outname, cols, rows, 3, gdal.GDT_Byte)
  out_cs = osr.SpatialReference()
  out_cs.ImportFromWkt(projref)
  out_ds.SetProjection(out_cs.ExportToWkt())
  out_ds.SetGeoTransform(in_geo)
  out_ds.GetRasterBand(1).WriteArray(r)
  out_ds.GetRasterBand(2).WriteArray(g)
  out_ds.GetRasterBand(3).WriteArray(b)
  out_ds = None


  if mask:
    print('\t* Applying cloud mask')
    apply_cloud_mask(outname, mask)

def genColorInfrared(b5_file, b4_file, b3_file,qa_file, sun_zen, outname, mask=None):
  # Function to create a Landsat 8/9 color infrared image, based upon
  # input GeoTIFF files (full path) for B5 (near infrared), B4 (red), and
  # B3 (gren), along with appropriate metadata file (*_MTL.txt)

  indir = os.path.dirname(b5_file[0])

  ### B4, RED ###
  print("\n\t* Opening B4 file")
  ds = gdal.Open(b4_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  b4 = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None
  
  # convert to reflectance
  print("\t* Converting to reflectance")
  mult_scale_factor, add_offset = getReflectanceConstants(indir, '4')
  b4 = (mult_scale_factor*b4)+add_offset
  
  # normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle")
  b4 = b4/np.cos(np.radians(sun_zen))

  ### B5 ###
  print("\n\t* Opening B5 file")
  ds = gdal.Open(b5_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  b5 = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None

  # convert to reflectance
  print("\t* Converting to reflectance")
  mult_scale_factor, add_offset = getReflectanceConstants(indir, '5')
  b5 = (mult_scale_factor*b5)+add_offset
  
  # normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle")
  b5 = b5/np.cos(np.radians(sun_zen))

  ### B3 ###
  print("\n\t* Opening B3 file")
  ds = gdal.Open(b3_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  b3 = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None
  
  # convert to reflectance
  print("\t* Converting to reflectance")
  mult_scale_factor, add_offset = getReflectanceConstants(indir, '3')
  b3 = (mult_scale_factor*b3)+add_offset
  
  # normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle")
  b3 = b3/np.cos(np.radians(sun_zen))

  ### Quality ###
  print("\n\t* Opening Quality file")
  ds = gdal.Open(qa_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  qa = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None

  # Find no data pixels
  print("\n\t* Finding areas with no data pixels")
  nodatapix = np.where( qa==1 )

  ## Convert from reflectance to 8-bit values ###
  print("\t* Converting reflectance to 8-bit values")
  no_data = 0
  r = np.clip(b5, 0.0, 1.0)
  g = np.clip(b4, 0.0, 1.0)
  b = np.clip(b3, 0.0, 1.0)
  r = bytescale(np.sqrt(r), cmin=0.0, cmax=1.0, high=255, low=1)
  g = bytescale(np.sqrt(g), cmin=0.0, cmax=1.0, high=255, low=1)
  b = bytescale(np.sqrt(b), cmin=0.0, cmax=1.0, high=255, low=1)

  # Use PIL to brighten it up
  print("\t* Enhancing image")
  rgbArray = np.zeros( (rows,cols,3), 'uint8' )
  rgbArray[...,0] = r
  rgbArray[...,1] = g
  rgbArray[...,2] = b
  rgb = Image.fromarray(rgbArray)
  enhancer = ImageEnhance.Contrast(rgb).enhance(1.5)  # increase this number to increase contrast
  r = np.reshape(enhancer.getdata(band=0), (rows,cols))
  g = np.reshape(enhancer.getdata(band=1), (rows,cols))
  b = np.reshape(enhancer.getdata(band=2), (rows,cols))
  
  r = np.clip(r, 1, 255)
  g = np.clip(g, 1, 255)
  b = np.clip(b, 1, 255)

  r[nodatapix] = no_data
  g[nodatapix] = no_data
  b[nodatapix] = no_data

  # Write a GeoTIFF
  print("\t* Generating a color infrared geotiff")
  format = 'GTiff'
  driver = gdal.GetDriverByName(format)
  out_ds = driver.Create(outname, cols, rows, 3, gdal.GDT_Byte)
  out_cs = osr.SpatialReference()
  out_cs.ImportFromWkt(projref)
  out_ds.SetProjection(out_cs.ExportToWkt())
  out_ds.SetGeoTransform(in_geo)
  out_ds.GetRasterBand(1).WriteArray(r)
  out_ds.GetRasterBand(2).WriteArray(g)
  out_ds.GetRasterBand(3).WriteArray(b)
  out_ds = None


  if mask:
    print('\t* Applying cloud mask')
    apply_cloud_mask(outname, mask)

def genNaturalColor(b6_file, b5_file, b4_file,qa_file, sun_zen, outname, mask=None):
  # Function to create a Landsat 8/9 natural color image, based upon
  # input GeoTIFF files (full path) for B6 (shortwave infrared), B5 (near infrared),
  # and B4 (red), along with appropriate metadata file (*_MTL.txt)
  
  indir = os.path.dirname(b6_file[0])

  ### B4, RED ###
  print("\n\t* Opening B4 file")
  ds = gdal.Open(b4_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  b4 = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None

  # convert to reflectance
  print("\t* Converting to reflectance")
  mult_scale_factor, add_offset = getReflectanceConstants(indir, '4')
  b4 = (mult_scale_factor*b4)+add_offset
  
  # normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle")
  b4 = b4/np.cos(np.radians(sun_zen))

  ### B5 ###
  print("\n\t* Opening B5 file")
  ds = gdal.Open(b5_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  b5 = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None

  # convert to reflectance
  print("\t* Converting to reflectance")
  mult_scale_factor, add_offset = getReflectanceConstants(indir, '5')
  b5 = (mult_scale_factor*b5)+add_offset

  # normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle")
  b5 = b5/np.cos(np.radians(sun_zen))

  ### B6 ###
  print("\n\t* Opening B6 file")
  ds = gdal.Open(b6_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  b6 = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None

  # convert to reflectance
  print("\t* Converting to reflectance")
  mult_scale_factor, add_offset = getReflectanceConstants(indir, '6')
  b6 = (mult_scale_factor*b6)+add_offset
  
  # normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle")
  b6 = b6/np.cos(np.radians(sun_zen))

  ### Quality ###
  print("\n\t* Opening Quality file")
  ds = gdal.Open(qa_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  qa = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None

  # Find no data pixels
  print("\n\t* Finding areas with no data pixels")
  nodatapix = np.where( qa == 1 )

  ### Convert from reflectance to 8-bit values ###
  print("\t* Converting reflectance to 8-bit values")
  r = np.clip(b6, 0.0, 1.0)
  g = np.clip(b5, 0.0, 1.0)
  b = np.clip(b4, 0.0, 1.0)
  r = bytescale(np.sqrt(r), cmin=0.0, cmax=1.0, high=255, low=1)
  g = bytescale(np.sqrt(g), cmin=0.0, cmax=1.0, high=255, low=1)
  b = bytescale(np.sqrt(b), cmin=0.0, cmax=1.0, high=255, low=1)

  # Use PIL to brighten it up
  print("\t* Enhancing image")
  rgbArray = np.ones( (rows,cols,3), 'uint8' )
  rgbArray[...,0] = r
  rgbArray[...,1] = g
  rgbArray[...,2] = b
  rgb = Image.fromarray(rgbArray)
  enhancer = ImageEnhance.Contrast(rgb).enhance(1.5)  # increase this number to increase contrast
  r = np.reshape(enhancer.getdata(band=0), (rows,cols))
  g = np.reshape(enhancer.getdata(band=1), (rows,cols))
  b = np.reshape(enhancer.getdata(band=2), (rows,cols))
  
  r = np.clip(r, 1, 255)
  g = np.clip(g, 1, 255)
  b = np.clip(b, 1, 255)

  r[nodatapix] = 0
  g[nodatapix] = 0
  b[nodatapix] = 0
 
  # Write a GeoTIFF
  print("\t* Generating a natural color geotiff")
  format = 'GTiff'
  driver = gdal.GetDriverByName(format)
  out_ds = driver.Create(outname, cols, rows, 3, gdal.GDT_Byte)
  out_cs = osr.SpatialReference()
  out_cs.ImportFromWkt(projref)
  out_ds.SetProjection(out_cs.ExportToWkt())
  out_ds.SetGeoTransform(in_geo)
  out_ds.GetRasterBand(1).WriteArray(r)
  out_ds.GetRasterBand(2).WriteArray(g)
  out_ds.GetRasterBand(3).WriteArray(b)
  out_ds = None

  if mask:
    print('\t* Applying cloud mask')
    apply_cloud_mask(outname, mask)

def genNdvi(b5_file, b4_file, qa_file, sun_zen, outname, mask=None):
  # Function to create a Landsat 8 NDVI image, based upon
  # input GeoTIFF files (full path) for B5 (NIR), B4 (red), 
  # and the appropriate metadata file (_MTL.txt)
 
  indir = os.path.dirname(b5_file[0])

  ### NIR ###
  print("\n\t* Opening Band 5 file")
  ds = gdal.Open(b5_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  nir = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None

  # Convert to reflectance
  print("\t* Converting to reflectance.")
  mult_scale_factor, add_offset = getReflectanceConstants(indir, '5')
  nir = (mult_scale_factor*nir)+add_offset
 
  # Normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle")
  nir = nir/np.cos(np.radians(sun_zen))

  ### Red ###
  print("\n\t* Opening Band 4 file")
  ds = gdal.Open(b4_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  r = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None

  # Convert to reflectance
  print("\t* Converting to reflectance")
  mult_scale_factor, add_offset = getReflectanceConstants(indir, '4')
  r = (mult_scale_factor*r)+add_offset
  
  # Normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle")
  r = r/np.cos(np.radians(sun_zen))

  ### Quality ###
  print("\n\t* Opening Quality file")
  ds = gdal.Open(qa_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  qa = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None
  nodatapix = np.where( qa == 1 )

  # Calculate NDVI, (NIR-R)/(NIR+R) or (B5-B4)/(B5+B4)
  print("\n\t* Calculating NDVI")
  ndvi = np.empty(np.shape(r), 'float')
  ok = np.where( (r >= 0) & (nir >= 0) )
  ndvi[ok] = (nir[ok]-r[ok])/(nir[ok]+r[ok])

  # Clip to a reasonable range
  print("\t* Clipping to a reasonable range")
  ndvi = np.clip(ndvi, -1.0, 1.0)
  ndvi[nodatapix] = 999


  # Write a Raw GeoTIFF--No Color Table
  print("\t* Generating NDVI geotiff")
  format = 'GTiff'
  driver = gdal.GetDriverByName(format)
  out_ds = driver.Create(outname, cols, rows, 1, gdal.GDT_Float32)
  out_cs = osr.SpatialReference()
  out_cs.ImportFromWkt(projref)
  out_ds.SetProjection(out_cs.ExportToWkt())
  out_ds.SetGeoTransform(in_geo)
  out_ds.GetRasterBand(1).WriteArray(ndvi)
  out_ds = None
  


  if mask:
    print('\t* Applying cloud mask')
    apply_cloud_mask(outname, mask)

def genNdwi(b3_file, b5_file, qa_file, sun_zen, outname, mask=None):
  # Function to create a Landsat 8 NDWI image, based upon
  # input GeoTIFF files (full path) for B3 (green), B5 (near infrared), 
  # and the appropriate metadata file (*_MTL.txt)
  
  indir = os.path.dirname(b3_file[0])

  ### Green ###
  print("\n\t* Opening Band 3 file")
  ds = gdal.Open(b3_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  g = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None

  # Convert to reflectance
  print("\t* Converting to reflectance")
  mult_scale_factor, add_offset = getReflectanceConstants(indir, '3')
  g = (mult_scale_factor*g)+add_offset
 
  # Normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle...")
  g = g/np.cos(np.radians(sun_zen))

  ### NIR ###
  print("\n\t* Opening Band 5 file")
  ds = gdal.Open(b5_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  nir = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None

  # Convert to reflectance
  print("\t* Converting to reflectance")
  mult_scale_factor, add_offset = getReflectanceConstants(indir, '5')
  nir = (mult_scale_factor*nir)+add_offset

  # Normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle")

  nir = nir/np.cos(np.radians(sun_zen))

  ### Quality ###
  print("\n\t* Opening Quality file")
  ds = gdal.Open(qa_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  qa = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None
  nodatapix = np.where( qa == 1 )

  # Calculate NDWI
  print("\n\t* Calculating NDWI")
  ndwi = np.empty(np.shape(g), 'float')
  ok = np.where( (g >= 0) & (nir >= 0) )
  ndwi[ok] = (g[ok]-nir[ok])/(g[ok]+nir[ok])
  
  # Clip to a reasonable range
  print("\t* Clipping to a reasonable range")
  ndwi = np.clip(ndwi, -1.0, 1.0)
  ndwi[nodatapix] = 999

  # Write a Raw GeoTIFF--No Color Table
  print("\t* Generating NDWI geotiff")
  format = 'GTiff'
  driver = gdal.GetDriverByName(format)
  out_ds = driver.Create(outname, cols, rows, 1, gdal.GDT_Float32)
  out_cs = osr.SpatialReference()
  out_cs.ImportFromWkt(projref)
  out_ds.SetProjection(out_cs.ExportToWkt())
  out_ds.SetGeoTransform(in_geo)
  out_ds.GetRasterBand(1).WriteArray(ndwi)
  out_ds = None


  if mask:
    print('\t* Applying cloud mask')
    apply_cloud_mask(outname, mask)

def genmNdwi(b3_file, b6_file, qa_file, sun_zen, outname, mask=None):
  # Function to create a Landsat 8/9 MNDWI image, based upon
  # input GeoTIFF files (full path) for B3 (green), B6 (shortwave infrared 1), 
  # and the appropriate metadata file (*_MTL.txt)
  
  indir = os.path.dirname(b3_file[0])

  ### Green ###
  print("\n\t* Opening Band 3 file")
  ds = gdal.Open(b3_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  g = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None

  # Convert to reflectance
  print("\t* Converting to reflectance")
  mult_scale_factor, add_offset = getReflectanceConstants(indir, '3')
  g = (mult_scale_factor*g)+add_offset  

  # Normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle")
  g = g/np.cos(np.radians(sun_zen))

  ### Shortwave Infrared ###
  print("\n\t* Opening Band 6 file")
  ds = gdal.Open(b6_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  swir = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None

  # Convert to reflectance
  print("\t* Converting to reflectance")
  mult_scale_factor, add_offset = getReflectanceConstants(indir, '6')
  swir = (mult_scale_factor*swir)+add_offset  
 
  # Normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle")
  swir = swir/np.cos(np.radians(sun_zen))

  ### Quality ###
  print("\n\t* Opening Quality file")
  ds = gdal.Open(qa_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  qa = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None
  nodatapix = np.where(qa == 1)

  # Calculate MNDWI
  print("\n\t* Calculating mNDWI")
  mndwi = np.empty(np.shape(g), 'float')
  ok = np.where( (g >= 0) & (swir >= 0) )
  mndwi[ok] = (g[ok]-swir[ok])/(g[ok]+swir[ok])

  # Clip to a reasonable range
  print("\t* Clipping to a reasonable range")
  mndwi = np.clip(mndwi, -1.0, 1.0)
  mndwi[nodatapix] = 999

  # Write a Raw GeoTIFF--No Color Table
  print("\t* Generating a mNDWI geotiff ")
  format = 'GTiff'
  driver = gdal.GetDriverByName(format)
  out_ds = driver.Create(outname, cols, rows, 1, gdal.GDT_Float32)
  out_cs = osr.SpatialReference()
  out_cs.ImportFromWkt(projref)
  out_ds.SetProjection(out_cs.ExportToWkt())
  out_ds.SetGeoTransform(in_geo)
  out_ds.GetRasterBand(1).WriteArray(mndwi)
  out_ds = None


  if mask:
    print('\t* Applying cloud mask')
    apply_cloud_mask(outname, mask)

def genEvi(b5_file, b4_file, b2_file, qa_file, sun_zen, outname, mask=None):
  # Function to create a Landsat 8/9 EVI image, based upon
  # input GeoTIFF files (full path) for B5 (NIR), B4 (red), B2 (blue), 
  # and the appropriate metadata file (*_MTL.txt)
  
  indir = os.path.dirname(b5_file[0])

  ### NIR ###
  print("\n\t* Opening Band 5 file")
  ds = gdal.Open(b5_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  nir = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None

  # Convert to reflectance
  print("\t* Converting to reflectance")
  mult_scale_factor, add_offset = getReflectanceConstants(indir, '5')
  nir = (mult_scale_factor*nir)+add_offset

  # Normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle")
  nir = nir/np.cos(np.radians(sun_zen))

  ### Red ###
  print("\n\t* Opening Band 4 file")
  ds = gdal.Open(b4_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  r = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None

  # Convert to reflectance
  print("\t* Converting to reflectance")
  mult_scale_factor, add_offset = getReflectanceConstants(indir, '4')
  r = (mult_scale_factor*r)+add_offset

  # Normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle")
  r = r/np.cos(np.radians(sun_zen))

  ### Blue ###
  print("\n\t* Opening Band 2 file")
  ds = gdal.Open(b2_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  b = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None

  # Convert to reflectance
  print("\t* Converting to reflectance")
  mult_scale_factor, add_offset = getReflectanceConstants(indir, '2')
  b = (mult_scale_factor*b)+add_offset

  # Normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle")
  b = b/np.cos(np.radians(sun_zen))

  ### Quality ###
  print("\n\t* Opening Quality file")
  ds = gdal.Open(qa_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  qa = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None
  nodatapix = np.where(qa == 1)

  # Calculate EVI
  print("\n\t* Calculating EVI")
  evi = np.empty(np.shape(nir), 'float')
  ok = np.where( (r >= 0) & (nir >= 0) & (b >= 0))
  num = (nir[ok] - r[ok])
  denom = (nir[ok]+(6*r[ok])- (7.5*b[ok])+1)
  evi[ok] = 2.5*(num/denom)

  # Clip to a reasonable range
  print("\t* Clipping to a reasonable range")
  evi = np.clip(evi, -1.0, 1.0)
  evi[nodatapix] = 999

  # Write a Raw GeoTIFF--No Color Table
  print("\t* Generating EVI geotiff")
  format = 'GTiff'
  driver = gdal.GetDriverByName(format)
  out_ds = driver.Create(outname, cols, rows, 1, gdal.GDT_Float32)
  out_cs = osr.SpatialReference()
  out_cs.ImportFromWkt(projref)
  out_ds.SetProjection(out_cs.ExportToWkt())
  out_ds.SetGeoTransform(in_geo)
  out_ds.GetRasterBand(1).WriteArray(evi)
  out_ds = None


  if mask:
    print('\t* Applying cloud mask')
    apply_cloud_mask(outname, mask)

def genNbr(b5_file, b7_file, qa_file, sun_zen, outname, mask=None):
  indir = os.path.dirname(b5_file[0])

  ### NIR ###
  print("\n\t* Opening Band 5 file")
  ds = gdal.Open(b5_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  nir = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None

  # Convert to reflectance
  print("\t* Converting to reflectance")
  mult_scale_factor, add_offset = getReflectanceConstants(indir, '5')
  nir = (mult_scale_factor*nir)+add_offset

  # Normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle")
  nir = nir/np.cos(np.radians(sun_zen))

  ### SWIR ###
  print("\n\t* Opening Band 7 file")
  ds = gdal.Open(b7_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  swir = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None

  # Convert to reflectance
  print("\t* Converting to reflectance")
  mult_scale_factor, add_offset = getReflectanceConstants(indir, '7')
  swir = (mult_scale_factor*swir)+add_offset

  # Normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle")
  swir = swir/np.cos(np.radians(sun_zen))

  ### Quality ###
  print("\n\t* Opening Quality file")
  ds = gdal.Open(qa_file[0])
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  qa = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
  ds = None
  nodatapix = np.where(qa == 1)

  # Calculate NBR
  print("\n\t* Calculating NBR")
  nbr = np.empty(np.shape(nir), 'float')
  ok = np.where( (nir >= 0) & (swir >= 0))
  num = (nir[ok] - swir[ok])
  denom = (nir[ok] + swir[ok])
  nbr[ok] = num/denom

  # Clip to a reasonable range
  print("\t* Clipping to a reasonable range")
  nbr = np.clip(nbr, -1.0, 1.0)
  nbr[nodatapix] = 999

  # Write a Raw GeoTIFF--No Color Table
  print("\t* Generating NBR geotiff")
  format = 'GTiff'
  driver = gdal.GetDriverByName(format)
  out_ds = driver.Create(outname, cols, rows, 1, gdal.GDT_Float32)
  out_cs = osr.SpatialReference()
  out_cs.ImportFromWkt(projref)
  out_ds.SetProjection(out_cs.ExportToWkt())
  out_ds.SetGeoTransform(in_geo)
  out_ds.GetRasterBand(1).WriteArray(nbr)
  out_ds = None


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
    translated_codes = np.vectorize(codes_dict.get)(cdl_array).astype('uint16')  # this one line does all the work :)
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
    # download each GLC tile from S3 bucket
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

def gen_water_extent(indir, nstd, outname, mask, sun_zen):
  # get nir band
  nir_files = glob.glob(os.path.join(indir, '*/*B5.TIF'))
  if not nir_files:
     nir_files = glob.glob(os.path.join(indir, '*/*B05.TIF'))
  
  # merging NIR bands
  nir_merged = os.path.join(Path(outname).parent, 'B5_merged.tif')
  if not os.path.isfile(nir_merged):
    print('\t* Merging NIR bands.')
    gen_merge(nir_files, nir_merged)

  # get no data locs
  nir_rst = rio.open(nir_merged)
  nir_nd_val = nir_rst.nodata
  nir_array = nir_rst.read()[0]
  nd_locs = np.where(nir_array == nir_nd_val)

  # Convert to reflectance
  print("\t* Converting to reflectance")
  safe = Path(nir_files[0]).parent
  mult_scale_factor, add_offset = getReflectanceConstants(safe, '5')
  nir_array = (mult_scale_factor*nir_array)+add_offset

  # Normalize by the cosine of the zenith angle
  print("\t* Normalizing by the cosine of the zenith angle")
  nir_array = nir_array/np.cos(np.radians(sun_zen))
  
  # get year
  year = os.path.basename(indir)[:4]

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
      print('\t* Downloading CDL')
      # download and resample CDL
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
        print('\t* Downloading WorldCover')
        # download WorldCover
        download_worldcover(nir_merged, str(year), wc_name)
      # reclassify WorldCover
      ref_simple = reclass_worldcover(wc_name, nd_locs)
    else:
      print('\t* WorldCover already downloaded!')
      ref_simple = wc_check[0]

  # open reclassified CDl and cloud mask
  ref_simple_array = rio.open(ref_simple).read()[0]
  cloudMask_rst = rio.open(mask)

  # resample cloud mask if it doesn't match the inputted data
  if cloudMask_rst.transform[0] != nir_rst.transform[0]:
    cloudMask = cloudMask_rst.read(1,
            out_shape=(cloudMask_rst.count,
                      int(cloudMask_rst.height * 2),
                      int(cloudMask_rst.width * 2)
            ),
            resampling=Resampling.nearest
        )
  else:
     cloudMask = cloudMask_rst.read(1)

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

def ls_merge(dir_to_merge, mask=False, method='first'):
  # create output filename
  ims = glob.glob(os.path.join(dir_to_merge, '*tif'))

  # Parse filename - handle both event-named and regular files
  basename = os.path.basename(ims[0])
  parts = basename.split('_')

  # Check if this is an event-named file (starts with YYYYMM pattern)
  if len(parts) > 3 and parts[0].isdigit() and len(parts[0]) == 6:
      # Event-named file: EVENT_NAME_LC08_product_..._YYYY-MM-DD_day.tif
      # Find the satellite (LC08 or LC09)
      sat = None
      prod_type = None
      for i, part in enumerate(parts):
          if part in ['LC08', 'LC09', 'LC8', 'LC9']:
              sat = part
              if i + 1 < len(parts):
                  prod_type = parts[i + 1]
              break

      # Extract date from end (format: YYYY-MM-DD)
      if len(parts) >= 2 and parts[-2] == 'day':
          date_part = parts[-3]  # YYYY-MM-DD
          im_date = date_part.replace('-', '')  # Convert to YYYYMMDD
      else:
          # Fallback: use today's date
          from datetime import datetime
          im_date = datetime.now().strftime('%Y%m%d')
  else:
      # Regular file: LC08_product_YYYYMMDD_...
      sat = parts[0]
      prod_type = parts[1]
      im_date = parts[2]

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