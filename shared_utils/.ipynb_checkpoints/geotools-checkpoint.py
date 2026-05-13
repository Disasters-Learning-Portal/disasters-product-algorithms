import numpy as np
from osgeo import osr, gdal, gdalconst
import sys

def bytescale(arr, cmin=0, cmax=1, low=0, high=255):
  # in this scenario, 'low and high' are y-value (byte)
  # and 'cmin, cmax' are x-value (inputs)
  m = (high-low)/(cmax-cmin)
  b = high-(m*cmax)
  out = np.clip( (m*arr)+b, low, high )
  out = np.uint8(out)
  return out

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
  result_ds = gdal.GetDriverByName('GTiff').Create(outfile, match_ds.RasterXSize, match_ds.RasterYSize, 1, gdalconst.GDT_Float32)
  # create result's projection and transform to be the matching one
  result_ds.SetGeoTransform(match_ds.GetGeoTransform())
  result_ds.SetProjection(match_ds.GetProjection())
  # reproject
  #res = gdal.ReprojectImage(src_ds, result_ds, src_ds.GetProjection(), match_ds.GetProjection(), gdalconst.GRA_Bilinear)
  res = gdal.ReprojectImage(src_ds, result_ds, src_ds.GetProjection(), match_ds.GetProjection(), gdalconst.GRA_NearestNeighbour)
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

def get_geo(f, band=1):
  ds = gdal.Open(f, gdal.GA_ReadOnly)
  cols = ds.RasterXSize
  rows = ds.RasterYSize
  img = ds.GetRasterBand(band).ReadAsArray(0,0,cols,rows)
  in_geo = ds.GetGeoTransform()
  projref = ds.GetProjectionRef()
  return img, in_geo, projref

def dump_geotiff_float(filename, arr, projref, in_geo):
  format = 'GTiff'
  rows, cols = np.shape(arr)
  driver = gdal.GetDriverByName(format)
  out_ds = driver.Create(filename, \
                         cols, rows, 1, gdal.GDT_Float32)
  out_cs = osr.SpatialReference()
  out_cs.ImportFromWkt(projref)
  out_ds.SetProjection(out_cs.ExportToWkt())
  out_ds.SetGeoTransform(in_geo)
  out_ds.GetRasterBand(1).WriteArray(arr)
  out_ds = None
  return filename

def dump_geotiff_byte(filename, arr, projref, in_geo):
  format = 'GTiff'
  rows, cols = np.shape(arr)
  driver = gdal.GetDriverByName(format)
  out_ds = driver.Create(filename, \
                         cols, rows, 1, gdal.GDT_Byte)
  out_cs = osr.SpatialReference()
  out_cs.ImportFromWkt(projref)
  out_ds.SetProjection(out_cs.ExportToWkt())
  out_ds.SetGeoTransform(in_geo)
  out_ds.GetRasterBand(1).WriteArray(arr)
  out_ds = None
  return filename

def dump_geotiff_rgb(filename, r, g, b, projref, in_geo):
  # Write a GeoTIFF
  format = 'GTiff'
  rows, cols = np.shape(r)
  driver = gdal.GetDriverByName(format)
  out_ds = driver.Create(filename, cols, rows, 3, gdal.GDT_Byte)
  out_cs = osr.SpatialReference()
  out_cs.ImportFromWkt(projref)
  out_ds.SetProjection(out_cs.ExportToWkt())
  out_ds.SetGeoTransform(in_geo)
  out_ds.GetRasterBand(1).WriteArray(r)
  out_ds.GetRasterBand(2).WriteArray(g)
  out_ds.GetRasterBand(3).WriteArray(b)
  out_ds = None
  return filename
