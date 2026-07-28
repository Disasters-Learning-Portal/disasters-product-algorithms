import os
import re
import numpy as np
from osgeo import gdal
from datetime import datetime
import json
from typing import Literal, Union
import rasterio
from glob import glob

from shared_utils.s3utils import *
from shared_utils.geotools import *

def retrieve_skysat_resources(date: Union[str, datetime], bucket="csdap-planet-skysat-delivery", prefix="disasters"):
    files = retrieve_s3_file_list(bucket, prefix)

    filtered_files = [x for x in files if len(x.split("/")) > 2]

    subdirs = {}
    event_dirs = sorted(set(x.split("/")[1] for x in filtered_files))
    for event_dir in event_dirs:
        event_subdirs = sorted(set(x.split("/")[2] for x in filtered_files if x.split("/")[1] == event_dir))
        for event_subdir in event_subdirs:
            superfiltered_file = [x for x in filtered_files if x.split("/")[1] == event_dir and x.split("/")[2] == event_subdir][0]
            date = datetime.strptime(f"{superfiltered_file.split("/")[-1].split("_")[0]}{superfiltered_file.split("/")[-1].split("_")[1]}", "%Y%m%d%H%M%S")
            subdirs[date] = f"{prefix}/{event_dir}/{event_subdir}"

    if isinstance(date, str):
        date = datetime.strptime(date, "%Y-%m-%d %H:%M:%S")

    closest_date = min([k for (k, v) in subdirs.items()], key=lambda d: abs(d - date))
    
    selected = subdirs[closest_date]

    selected_files = [x for x in filtered_files if x.startswith(selected)]

    metadata = [
        x for x in selected_files
        if x.lower().endswith(".json")
    ]

    tifs = [
        x for x in selected_files
        if x.lower().endswith((".tif", ".tiff"))
    ]

    print(f"Selected SkySat folder: {selected}")
    print(f"TIF files found: {len(tifs)}")
    for t in tifs:
        print("  ", t)

    return [f"s3://{bucket}/{x}" for x in tifs]


def normalize_band(band, lower_pct=2, upper_pct=98, gamma=1.0):
    valid = band[~np.isnan(band)]
    if valid.size == 0:
        return np.zeros_like(band)
    
    lo = np.percentile(valid, lower_pct)
    hi = np.percentile(valid, upper_pct)
    
    stretched = np.clip((band - lo) / (hi - lo + 1e-10), 0, 1)
    
    if gamma != 1.0:
        stretched = np.power(stretched, gamma)
        
    return stretched
    

def print_skysat_stats(name, array):
    valid = array[~np.isnan(array)]
    if valid.size > 0:
        print(f"  {name:6s} → min: {np.nanmin(array):7.4f}  max: {np.nanmax(array):7.4f}  "
              f"mean: {np.nanmean(array):7.4f}  median: {np.nanmedian(array):7.4f}  "
              f"valid px: {valid.size:,}")
    else:
        print(f"  {name:6s} → No valid data.")


def udm_mask(s3_image_paths : list[str], source: Literal["toa", "sfc"], bands: list[np.ndarray]):
    if source == "toa":
        udm_filepaths = [x for x in s3_image_paths if x.lower().endswith("_u0001_udm2.tif")]
    elif source == "sfc":
        udm_filepaths = [x for x in s3_image_paths if x.lower().endswith("_u0002_udm2.tif")]
        
    if udm_filepaths == []:
        print(f"  [!] UDM file not found for the selected date and source. Skipping mask step.")
        return bands
    else:
        in_filepath = udm_filepaths[0]
        if f'/tmp/s3_temp/{local_tif_basename(in_filepath)}' not in glob("/tmp/s3_temp/*"):
            print("UDM2 file not found locally, downloading from s3")
            in_file = download_s3_file(in_filepath)
        else:
            print("UDM2 file found, proceeding")
            in_file = f'/tmp/s3_temp/{local_tif_basename(in_filepath)}'
        with rasterio.open(UDM_PATH) as udm_src:
            clear = udm_src.read(UDM_CLEAR_BAND).astype(np.float32)

        udm_mask = (clear != 1)
        for band in bands:
            band[udm_mask] = np.nan

        total_px = udm_mask.size
        clear_px = total_px - np.sum(udm_mask)
        print(f"  Clear pixels : {clear_px:,} ({100*clear_px/total_px:.1f}%)")

        return bands


def calc_ndvi(s3_image_paths : list[str], source: Literal["toa", "sfc"], save_location : str = "/tmp/s3_temp"):
    if save_location.endswith("/"):
            save_location = save_location[:-1]

    print("Collecting needed files...")
    if source == "toa":
        in_filepath = [x for x in s3_image_paths if x.lower().endswith("_u0001_analytic.tif")][0]
        if f'/tmp/s3_temp/{local_tif_basename(in_filepath)}' not in glob("/tmp/s3_temp/*"):
            print("Analytic file not found, downloading from s3")
            in_file = download_s3_file(in_filepath)
        else:
            print("Analytic file found, proceeding")
            in_file = f'/tmp/s3_temp/{local_tif_basename(in_filepath)}'

        print("Generating NDVI")
        
        ds = gdal.Open(in_file)
        cols = ds.RasterXSize
        rows = ds.RasterYSize
        in_geo = ds.GetGeoTransform()
        projref = ds.GetProjectionRef()

        metadata = ds.GetMetadata()

        if "TIFFTAG_IMAGEDESCRIPTION" in metadata.keys():
            try:
                image_desc = metadata["TIFFTAG_IMAGEDESCRIPTION"]
                meta_json = json.loads(image_desc)
                coeffs_list = meta_json["properties"]["reflectance_coefficients"]
                print(f"  ✔ Extracted reflectance coefficients: {coeffs_list}")
            except (json.JSONDecodeError, KeyError) as e:
                raise ValueError(f"Failed to parse reflectance_coefficients from ImageDescription. Error: {e}")
        elif "ImageDescription" in metadata.keys():
            try:
                image_desc = metadata["ImageDescription"]
                meta_json = json.loads(image_desc)
                coeffs_list = meta_json["properties"]["reflectance_coefficients"]
                print(f"  ✔ Extracted reflectance coefficients: {coeffs_list}")
            except (json.JSONDecodeError, KeyError) as e:
                raise ValueError(f"Failed to parse reflectance_coefficients from ImageDescription. Error: {e}")
        else:
            raise ValueError("Could not find 'ImageDescription' tag in the GeoTIFF.")            

        band_nums = [3, 4] # blue == 1, green == 2, red == 3, nir == 4
        bands = [None, None, None, None]
        for band_num in band_nums:
            bands[band_num-1] = np.clip(ds.GetRasterBand(band_num).ReadAsArray(0, 0, cols, rows) * coeffs_list[band_num-1], 0, 1)

        bands = udm_mask(s3_image_paths, source, bands)

        blue, green, red, nir = bands
        print(f"  Loaded & converted {len([x for x in bands if x is not None])} bands from: {in_file}")

    elif source == "sfc":
        in_filepath = [x for x in s3_image_paths if x.lower().endswith("_u0002_analytic.tif")][0]
        if f'/tmp/s3_temp/{local_tif_basename(in_filepath)}' not in glob("/tmp/s3_temp/*"):
            print("Analytic file not found, downloading from s3")
            in_file = download_s3_file(in_filepath)
        else:
            print("Analytic file found, proceeding")
            in_file = f'/tmp/s3_temp/{local_tif_basename(in_filepath)}'

        print("Generating NDVI")

        ds = gdal.Open(in_file)
        cols = ds.RasterXSize
        rows = ds.RasterYSize
        in_geo = ds.GetGeoTransform()
        projref = ds.GetProjectionRef()

        metadata = ds.GetMetadata()
            
        band_nums = [3, 4] # blue == 1, green == 2, red == 3, nir == 4
        bands = [None, None, None, None]
        for band_num in band_nums:
            bands[band_num-1] = np.clip(ds.GetRasterBand(band_num).ReadAsArray(0, 0, cols, rows) / 10000, 0, 1)

        bands = udm_mask(s3_image_paths, source, bands)
                
        blue, green, red, nir = bands
        print(f"  Loaded & converted {len([x for x in bands if x is not None])} bands from: {in_file}")
    
    with np.errstate(invalid='ignore', divide='ignore'):
        denom_ndvi = nir + red
        ndvi = np.where(denom_ndvi != 0, (nir - red) / denom_ndvi, np.nan)
    print_skysat_stats("NDVI",  ndvi)

    ndvi[np.isnan(ndvi)] = -9999

    date = datetime.strptime(f"{in_file.split('/')[-1].split('_')[0]}_{in_file.split('/')[-1].split('_')[1]}", '%Y%m%d_%H%M%S')
    outfile = f"{save_location}/{date.strftime('%Y%m')}_SkySat_{source}_NDVI_{date.strftime('%Y-%m-%dT%H:%M:%SZ')}.tif"

    dump_geotiff_float(outfile, ndvi, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")
    return outfile


def calc_evi(s3_image_paths : list[str], source: Literal["toa", "sfc"], save_location : str = "/tmp/s3_temp"):
    if save_location.endswith("/"):
            save_location = save_location[:-1]

    print("Collecting needed files...")
    if source == "toa":
        in_filepath = [x for x in s3_image_paths if x.lower().endswith("_u0001_analytic.tif")][0]
        if f'/tmp/s3_temp/{local_tif_basename(in_filepath)}' not in glob("/tmp/s3_temp/*"):
            print("Analytic file not found, downloading from s3")
            in_file = download_s3_file(in_filepath)
        else:
            print("Analytic file found, proceeding")
            in_file = f'/tmp/s3_temp/{local_tif_basename(in_filepath)}'

        print("Generating EVI")
        
        ds = gdal.Open(in_file)
        cols = ds.RasterXSize
        rows = ds.RasterYSize
        in_geo = ds.GetGeoTransform()
        projref = ds.GetProjectionRef()

        metadata = ds.GetMetadata()

        if "TIFFTAG_IMAGEDESCRIPTION" in metadata.keys():
            try:
                image_desc = metadata["TIFFTAG_IMAGEDESCRIPTION"]
                meta_json = json.loads(image_desc)
                coeffs_list = meta_json["properties"]["reflectance_coefficients"]
                print(f"  ✔ Extracted reflectance coefficients: {coeffs_list}")
            except (json.JSONDecodeError, KeyError) as e:
                raise ValueError(f"Failed to parse reflectance_coefficients from ImageDescription. Error: {e}")
        elif "ImageDescription" in metadata.keys():
            try:
                image_desc = metadata["ImageDescription"]
                meta_json = json.loads(image_desc)
                coeffs_list = meta_json["properties"]["reflectance_coefficients"]
                print(f"  ✔ Extracted reflectance coefficients: {coeffs_list}")
            except (json.JSONDecodeError, KeyError) as e:
                raise ValueError(f"Failed to parse reflectance_coefficients from ImageDescription. Error: {e}")
        else:
            raise ValueError("Could not find 'ImageDescription' tag in the GeoTIFF.")            

        band_nums = [1, 3, 4] # blue == 1, green == 2, red == 3, nir == 4
        bands = [None, None, None, None]
        for band_num in band_nums:
            bands[band_num-1] = np.clip(ds.GetRasterBand(band_num).ReadAsArray(0, 0, cols, rows) * coeffs_list[band_num-1], 0, 1)

        bands = udm_mask(s3_image_paths, source, bands)

        blue, green, red, nir = bands
        print(f"  Loaded & converted {len([x for x in bands if x is not None])} bands from: {in_file}")

    elif source == "sfc":
        in_filepath = [x for x in s3_image_paths if x.lower().endswith("_u0002_analytic.tif")][0]
        if f'/tmp/s3_temp/{local_tif_basename(in_filepath)}' not in glob("/tmp/s3_temp/*"):
            print("Analytic file not found, downloading from s3")
            in_file = download_s3_file(in_filepath)
        else:
            print("Analytic file found, proceeding")
            in_file = f'/tmp/s3_temp/{local_tif_basename(in_filepath)}'

        print("Generating EVI")

        ds = gdal.Open(in_file)
        cols = ds.RasterXSize
        rows = ds.RasterYSize
        in_geo = ds.GetGeoTransform()
        projref = ds.GetProjectionRef()

        metadata = ds.GetMetadata()
            
        band_nums = [1, 3, 4] # blue == 1, green == 2, red == 3, nir == 4
        bands = [None, None, None, None]
        for band_num in band_nums:
            bands[band_num-1] = np.clip(ds.GetRasterBand(band_num).ReadAsArray(0, 0, cols, rows) / 10000, 0, 1)

        bands = udm_mask(s3_image_paths, source, bands)
                
        blue, green, red, nir = bands
        print(f"  Loaded & converted {len([x for x in bands if x is not None])} bands from: {in_file}")
    
    with np.errstate(invalid='ignore', divide='ignore'):
        EVI_G  = 2.5
        EVI_C1 = 6.0
        EVI_C2 = 7.5
        EVI_L  = 1.0

        denom_evi = nir + EVI_C1 * red - EVI_C2 * blue + EVI_L
        evi = np.where(denom_evi != 0, EVI_G * (nir - red) / denom_evi, np.nan)
        evi = np.clip(evi, -1, 1)
    print_skysat_stats("EVI",   evi)

    evi[np.isnan(evi)] = -9999

    date = datetime.strptime(f"{in_file.split('/')[-1].split('_')[0]}_{in_file.split('/')[-1].split('_')[1]}", '%Y%m%d_%H%M%S')
    outfile = f"{save_location}/{date.strftime('%Y%m')}_SkySat_{source}_EVI_{date.strftime('%Y-%m-%dT%H:%M:%SZ')}.tif"

    dump_geotiff_float(outfile, evi, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")
    return outfile


def calc_ndwi(s3_image_paths : list[str], source: Literal["toa", "sfc"], save_location : str = "/tmp/s3_temp"):
    if save_location.endswith("/"):
            save_location = save_location[:-1]

    print("Collecting needed files...")
    if source == "toa":
        in_filepath = [x for x in s3_image_paths if x.lower().endswith("_u0001_analytic.tif")][0]
        if f'/tmp/s3_temp/{local_tif_basename(in_filepath)}' not in glob("/tmp/s3_temp/*"):
            print("Analytic file not found, downloading from s3")
            in_file = download_s3_file(in_filepath)
        else:
            print("Analytic file found, proceeding")
            in_file = f'/tmp/s3_temp/{local_tif_basename(in_filepath)}'

        print("Generating NDWI")
        
        ds = gdal.Open(in_file)
        cols = ds.RasterXSize
        rows = ds.RasterYSize
        in_geo = ds.GetGeoTransform()
        projref = ds.GetProjectionRef()

        metadata = ds.GetMetadata()

        if "TIFFTAG_IMAGEDESCRIPTION" in metadata.keys():
            try:
                image_desc = metadata["TIFFTAG_IMAGEDESCRIPTION"]
                meta_json = json.loads(image_desc)
                coeffs_list = meta_json["properties"]["reflectance_coefficients"]
                print(f"  ✔ Extracted reflectance coefficients: {coeffs_list}")
            except (json.JSONDecodeError, KeyError) as e:
                raise ValueError(f"Failed to parse reflectance_coefficients from ImageDescription. Error: {e}")
        elif "ImageDescription" in metadata.keys():
            try:
                image_desc = metadata["ImageDescription"]
                meta_json = json.loads(image_desc)
                coeffs_list = meta_json["properties"]["reflectance_coefficients"]
                print(f"  ✔ Extracted reflectance coefficients: {coeffs_list}")
            except (json.JSONDecodeError, KeyError) as e:
                raise ValueError(f"Failed to parse reflectance_coefficients from ImageDescription. Error: {e}")
        else:
            raise ValueError("Could not find 'ImageDescription' tag in the GeoTIFF.")            

        band_nums = [2, 4] # blue == 1, green == 2, red == 3, nir == 4
        bands = [None, None, None, None]
        for band_num in band_nums:
            bands[band_num-1] = np.clip(ds.GetRasterBand(band_num).ReadAsArray(0, 0, cols, rows) * coeffs_list[band_num-1], 0, 1)

        bands = udm_mask(s3_image_paths, source, bands)

        blue, green, red, nir = bands
        print(f"  Loaded & converted {len([x for x in bands if x is not None])} bands from: {in_file}")

    elif source == "sfc":
        in_filepath = [x for x in s3_image_paths if x.lower().endswith("_u0002_analytic.tif")][0]
        if f'/tmp/s3_temp/{local_tif_basename(in_filepath)}' not in glob("/tmp/s3_temp/*"):
            print("Analytic file not found, downloading from s3")
            in_file = download_s3_file(in_filepath)
        else:
            print("Analytic file found, proceeding")
            in_file = f'/tmp/s3_temp/{local_tif_basename(in_filepath)}'

        print("Generating NDWI")

        ds = gdal.Open(in_file)
        cols = ds.RasterXSize
        rows = ds.RasterYSize
        in_geo = ds.GetGeoTransform()
        projref = ds.GetProjectionRef()

        metadata = ds.GetMetadata()
            
        band_nums = [2, 4] # blue == 1, green == 2, red == 3, nir == 4
        bands = [None, None, None, None]
        for band_num in band_nums:
            bands[band_num-1] = np.clip(ds.GetRasterBand(band_num).ReadAsArray(0, 0, cols, rows) / 10000, 0, 1)

        bands = udm_mask(s3_image_paths, source, bands)
                
        blue, green, red, nir = bands
        print(f"  Loaded & converted {len([x for x in bands if x is not None])} bands from: {in_file}")
    
    with np.errstate(invalid='ignore', divide='ignore'):
        denom_ndwi = green + nir
        ndwi = np.where(denom_ndwi != 0, (green - nir) / denom_ndwi, np.nan)
    print_skysat_stats("NDWI",  ndwi)

    ndwi[np.isnan(ndwi)] = -9999

    date = datetime.strptime(f"{in_file.split('/')[-1].split('_')[0]}_{in_file.split('/')[-1].split('_')[1]}", '%Y%m%d_%H%M%S')
    outfile = f"{save_location}/{date.strftime('%Y%m')}_SkySat_{source}_NDWI_{date.strftime('%Y-%m-%dT%H:%M:%SZ')}.tif"

    dump_geotiff_float(outfile, ndwi, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")
    return outfile


def produce_truecolor(s3_image_paths : list[str], source: Literal["toa", "sfc"], save_location : str = "/tmp/s3_temp", gamma: float = 0.7):
    if save_location.endswith("/"):
            save_location = save_location[:-1]

    print("Collecting needed files...")
    if source == "toa":
        in_filepath = [x for x in s3_image_paths if x.lower().endswith("_u0001_analytic.tif")][0]
        if f'/tmp/s3_temp/{local_tif_basename(in_filepath)}' not in glob("/tmp/s3_temp/*"):
            print("Analytic file not found, downloading from s3")
            in_file = download_s3_file(in_filepath)
        else:
            print("Analytic file found, proceeding")
            in_file = f'/tmp/s3_temp/{local_tif_basename(in_filepath)}'

        print("Loading bands")
        
        ds = gdal.Open(in_file)
        cols = ds.RasterXSize
        rows = ds.RasterYSize
        in_geo = ds.GetGeoTransform()
        projref = ds.GetProjectionRef()

        metadata = ds.GetMetadata()

        if "TIFFTAG_IMAGEDESCRIPTION" in metadata.keys():
            try:
                image_desc = metadata["TIFFTAG_IMAGEDESCRIPTION"]
                meta_json = json.loads(image_desc)
                coeffs_list = meta_json["properties"]["reflectance_coefficients"]
                print(f"  ✔ Extracted reflectance coefficients: {coeffs_list}")
            except (json.JSONDecodeError, KeyError) as e:
                raise ValueError(f"Failed to parse reflectance_coefficients from ImageDescription. Error: {e}")
        elif "ImageDescription" in metadata.keys():
            try:
                image_desc = metadata["ImageDescription"]
                meta_json = json.loads(image_desc)
                coeffs_list = meta_json["properties"]["reflectance_coefficients"]
                print(f"  ✔ Extracted reflectance coefficients: {coeffs_list}")
            except (json.JSONDecodeError, KeyError) as e:
                raise ValueError(f"Failed to parse reflectance_coefficients from ImageDescription. Error: {e}")
        else:
            raise ValueError("Could not find 'ImageDescription' tag in the GeoTIFF.")            

        band_nums = [1, 2, 3] # blue == 1, green == 2, red == 3, nir == 4
        bands = [None, None, None, None]
        for band_num in band_nums:
            bands[band_num-1] = normalize_band(np.clip(ds.GetRasterBand(band_num).ReadAsArray(0, 0, cols, rows) * coeffs_list[band_num-1], 0, 1), gamma = gamma)

        blue, green, red, nir = bands
        print(f"  Loaded & converted {len([x for x in bands if x is not None])} bands from: {in_file}")

    elif source == "sfc":
        in_filepath = [x for x in s3_image_paths if x.lower().endswith("_u0002_analytic.tif")][0]
        if f'/tmp/s3_temp/{local_tif_basename(in_filepath)}' not in glob("/tmp/s3_temp/*"):
            print("Analytic file not found, downloading from s3")
            in_file = download_s3_file(in_filepath)
        else:
            print("Analytic file found, proceeding")
            in_file = f'/tmp/s3_temp/{local_tif_basename(in_filepath)}'

        print("Loading bands")

        ds = gdal.Open(in_file)
        cols = ds.RasterXSize
        rows = ds.RasterYSize
        in_geo = ds.GetGeoTransform()
        projref = ds.GetProjectionRef()

        metadata = ds.GetMetadata()
            
        band_nums = [1, 2, 3] # blue == 1, green == 2, red == 3, nir == 4
        bands = [None, None, None, None]
        for band_num in band_nums:
            bands[band_num-1] = normalize_band(np.clip(ds.GetRasterBand(band_num).ReadAsArray(0, 0, cols, rows) / 10000, 0, 1), gamma = gamma)
                
        blue, green, red, nir = bands
        print(f"  Loaded & converted {len([x for x in bands if x is not None])} bands from: {in_file}")

    for band in bands:
        if band is not None:
            band[np.isnan(band)] = 0

    date = datetime.strptime(f"{in_file.split('/')[-1].split('_')[0]}_{in_file.split('/')[-1].split('_')[1]}", '%Y%m%d_%H%M%S')
    outfile = f"{save_location}/{date.strftime('%Y%m')}_SkySat_{source}_TrueColor_{date.strftime('%Y-%m-%dT%H:%M:%SZ')}.tif"

    dump_geotiff_rgb(outfile, red, green, blue, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")
    return outfile


def produce_colorir(s3_image_paths : list[str], source: Literal["toa", "sfc"], save_location : str = "/tmp/s3_temp", gamma: float = 0.7):
    if save_location.endswith("/"):
            save_location = save_location[:-1]

    print("Collecting needed files...")
    if source == "toa":
        in_filepath = [x for x in s3_image_paths if x.lower().endswith("_u0001_analytic.tif")][0]
        if f'/tmp/s3_temp/{local_tif_basename(in_filepath)}' not in glob("/tmp/s3_temp/*"):
            print("Analytic file not found, downloading from s3")
            in_file = download_s3_file(in_filepath)
        else:
            print("Analytic file found, proceeding")
            in_file = f'/tmp/s3_temp/{local_tif_basename(in_filepath)}'

        print("Loading bands")
        
        ds = gdal.Open(in_file)
        cols = ds.RasterXSize
        rows = ds.RasterYSize
        in_geo = ds.GetGeoTransform()
        projref = ds.GetProjectionRef()

        metadata = ds.GetMetadata()

        if "TIFFTAG_IMAGEDESCRIPTION" in metadata.keys():
            try:
                image_desc = metadata["TIFFTAG_IMAGEDESCRIPTION"]
                meta_json = json.loads(image_desc)
                coeffs_list = meta_json["properties"]["reflectance_coefficients"]
                print(f"  ✔ Extracted reflectance coefficients: {coeffs_list}")
            except (json.JSONDecodeError, KeyError) as e:
                raise ValueError(f"Failed to parse reflectance_coefficients from ImageDescription. Error: {e}")
        elif "ImageDescription" in metadata.keys():
            try:
                image_desc = metadata["ImageDescription"]
                meta_json = json.loads(image_desc)
                coeffs_list = meta_json["properties"]["reflectance_coefficients"]
                print(f"  ✔ Extracted reflectance coefficients: {coeffs_list}")
            except (json.JSONDecodeError, KeyError) as e:
                raise ValueError(f"Failed to parse reflectance_coefficients from ImageDescription. Error: {e}")
        else:
            raise ValueError("Could not find 'ImageDescription' tag in the GeoTIFF.")            

        band_nums = [2, 3, 4] # blue == 1, green == 2, red == 3, nir == 4
        bands = [None, None, None, None]
        for band_num in band_nums:
            bands[band_num-1] = normalize_band(np.clip(ds.GetRasterBand(band_num).ReadAsArray(0, 0, cols, rows) * coeffs_list[band_num-1], 0, 1), gamma = gamma)

        blue, green, red, nir = bands
        print(f"  Loaded & converted {len([x for x in bands if x is not None])} bands from: {in_file}")

    elif source == "sfc":
        in_filepath = [x for x in s3_image_paths if x.lower().endswith("_u0002_analytic.tif")][0]
        if f'/tmp/s3_temp/{local_tif_basename(in_filepath)}' not in glob("/tmp/s3_temp/*"):
            print("Analytic file not found, downloading from s3")
            in_file = download_s3_file(in_filepath)
        else:
            print("Analytic file found, proceeding")
            in_file = f'/tmp/s3_temp/{local_tif_basename(in_filepath)}'

        print("Loading bands")

        ds = gdal.Open(in_file)
        cols = ds.RasterXSize
        rows = ds.RasterYSize
        in_geo = ds.GetGeoTransform()
        projref = ds.GetProjectionRef()

        metadata = ds.GetMetadata()
            
        band_nums = [2, 3, 4] # blue == 1, green == 2, red == 3, nir == 4
        bands = [None, None, None, None]
        for band_num in band_nums:
            bands[band_num-1] = normalize_band(np.clip(ds.GetRasterBand(band_num).ReadAsArray(0, 0, cols, rows) / 10000, 0, 1), gamma = gamma)
                
        blue, green, red, nir = bands
        print(f"  Loaded & converted {len([x for x in bands if x is not None])} bands from: {in_file}")

    for band in bands:
        if band is not None:
            band[np.isnan(band)] = 0

    date = datetime.strptime(f"{in_file.split('/')[-1].split('_')[0]}_{in_file.split('/')[-1].split('_')[1]}", '%Y%m%d_%H%M%S')
    outfile = f"{save_location}/{date.strftime('%Y%m')}_SkySat_{source}_ColorIR_{date.strftime('%Y-%m-%dT%H:%M:%SZ')}.tif"

    dump_geotiff_rgb(outfile, nir, red, green, projref, in_geo)

    print(f"Generation completed, file saved to {outfile}")
    return outfile