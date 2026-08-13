import numpy as np
from osgeo import osr, gdal, gdalconst
import sys
from typing import Union

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

def dump_geotiff_float(filename, arr, projref, in_geo, nodata=None):
  # nodata defaults to None = write no nodata tag, which is what the SAR
  # callers (capella/umbra/iceye) rely on: their output is dB backscatter
  # where every finite value is legitimate, and they declare nodata at the
  # CLI instead. Pass a value only for products that reserve a fill.
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
  if nodata is not None:
    out_ds.GetRasterBand(1).SetNoDataValue(float(nodata))
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

def dump_geotiff_rgb(filename, r, g, b, projref, in_geo, alpha=None):
  """Write an 8-bit RGB GeoTIFF, optionally with a 4th alpha band.

  alpha=None (default) writes the legacy 3-band output unchanged. Pass a
  uint8 array (0 = transparent / nodata, 255 = valid) to get a 4-band RGBA
  whose band 4 is tagged GCI_AlphaBand. Use alpha instead of a scalar nodata
  whenever 0 is a legitimate sample — for an 8-bit composite it always is.
  The caller must then pass nodata=False to convert_to_cog: a scalar nodata
  declared alongside an alpha band shadows it (rasterio NodataShadowWarning)
  and masks real black pixels.
  """
  # Write a GeoTIFF
  format = 'GTiff'
  rows, cols = np.shape(r)
  driver = gdal.GetDriverByName(format)
  n_bands = 4 if alpha is not None else 3
  out_ds = driver.Create(filename, cols, rows, n_bands, gdal.GDT_Byte)
  out_cs = osr.SpatialReference()
  out_cs.ImportFromWkt(projref)
  out_ds.SetProjection(out_cs.ExportToWkt())
  out_ds.SetGeoTransform(in_geo)
  out_ds.GetRasterBand(1).WriteArray(r)
  out_ds.GetRasterBand(2).WriteArray(g)
  out_ds.GetRasterBand(3).WriteArray(b)
  if alpha is not None:
      alpha_band = out_ds.GetRasterBand(4)
      alpha_band.WriteArray(alpha)
      alpha_band.SetColorInterpretation(gdal.GCI_AlphaBand)
  out_ds = None
  return filename

def transform_geotifs_to_projection(input_filename : Union[str, list[str]], output_filename : Union[str, list[str]], destination_projection : Union[str, list[str]] = 'EPSG:4326'):
    if (type(input_filename) is list) or (type(input_filename) is list) or (type(input_filename) is list):
        if not ((type(input_filename) is list) and (type(input_filename) is list) and (type(input_filename) is list)):
            raise TypeError(f"input_filename, output_filename, and destination_projection must all be the same type (str or list[str]), but are types {type(input_filename)}, {type(output_filename)}, and {type(destination_projection)}.")
        else:
            if not len(input_filename) == len(output_filename) == len(destination_projection):
                raise IndexError(f"input_filename, output_filename, and destination_projection must all be the same length, but have lengths {len(input_filename)}, {len(output_filename)}, and {len(destination_projection)}.")

    if (type(input_filename) is str):
        input_filename = [input_filename]
        output_filename = [output_filename]
        destination_projection = [destination_projection]
        
        
    for i in range(len(input_filename)):
        gdal.Warp(output_filename[i], input_filename[i], options = gdal.WarpOptions(dstSRS = destination_projection[i]))
        print(f"Transformed {input_filename[i]} to {destination_projection[i]} and saved it to {output_filename[i]}.")

    return output_filename