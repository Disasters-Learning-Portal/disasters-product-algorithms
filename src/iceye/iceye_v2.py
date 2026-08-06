import os
import numpy as np
from osgeo import gdal
from datetime import datetime
from typing import Literal, Union
from glob import glob
from scipy.ndimage import uniform_filter, variance
import xml.etree.ElementTree as ET

from shared_utils.s3utils import *
from shared_utils.geotools import *


def retrieve_iceye_resources(date: Union[str, datetime], bucket="csdap-iceye-delivery", prefix="disasters"):
    files = retrieve_s3_file_list(bucket, prefix)

    filtered_files = [x for x in files if len(x.split("/")) > 2]

    datestrings = set([x.split("/")[-1] for x in filtered_files if (x.split("/")[-1].endswith(".tif") and ("GRD" in x.split("/")[-1]))])
    dates = [datetime.strptime(x.split("_")[-1].split(".")[0], "%Y%m%dT%H%M%S") for x in datestrings]

    if isinstance(date, str):
        date = datetime.strptime(date, "%Y-%m-%d %H:%M:%S")

    closest_date = min(dates, key=lambda d: abs(d - date))

    selected_files = [x for x in filtered_files if x.split(".")[0].endswith(closest_date.strftime("%Y%m%dT%H%M%S"))]
    # Much more rudimentary file selection method since the prefix depth seems to vary for Iceye, but the filenames do not
    # This can be updated in the future if things break, but for now it's capturing all the data that's in there fine

    tifs = [
        x for x in selected_files
        if x.lower().endswith((".tif", ".tiff"))
    ]

    metadata = [
        x for x in selected_files
        if x.lower().endswith((".xml"))
    ]

    print(f"TIF files found: {len(tifs)}")
    for t in tifs:
        print("  ", t)

    print(f"Metadata files found: {len(metadata)}")
    for m in metadata:
        print("  ", m)

    return [f"s3://{bucket}/{x}" for x in metadata], [f"s3://{bucket}/{x}" for x in tifs]


def lee_filter(img, size):
    img_mean = uniform_filter(img, (size, size))
    img_sqr_mean = uniform_filter(img**2, (size, size))
    img_variance = img_sqr_mean - img_mean**2

    overall_variance = variance(img)

    img_weights = img_variance / (img_variance + overall_variance)
    img_output = img_mean + img_weights * (img - img_mean)
    return img_output


def getCalibrationFactor(s3_metadata_paths : list[str]):
    xml_in_filepath = [x for x in s3_metadata_paths if (x.lower().endswith(".xml") and ("grd" in x.split("/")[-1].lower()))][0]
    if f'/tmp/s3_temp/{local_tif_basename(xml_in_filepath)}' not in glob("/tmp/s3_temp/*"):
        print("XML file not found, downloading from s3")
        xml_in_file = download_s3_file(xml_in_filepath)
    else:
        print("XML file found, proceeding")
        xml_in_file = f'/tmp/s3_temp/{local_tif_basename(xml_in_filepath)}'

    tree = ET.parse(xml_in_file)
    root = tree.getroot()

    return float(root.find('calibration_factor').text)
    

def sigmaCalib(s3_image_paths : list[str], s3_metadata_paths : list[str], save_products : Literal["amp", "db", "both"] = "both", save_location : str = "/tmp/s3_temp", filter_size : int = 5):
    if save_location.endswith("/"):
        save_location = save_location[:-1]
    os.makedirs(save_location, exist_ok=True)
    
    print("Collecting needed files...")
    grd_in_filepath = [x for x in s3_image_paths if (x.lower().endswith(".tif") and ("grd" in x.split("/")[-1].lower()))][0]
    if f'/tmp/s3_temp/{local_tif_basename(grd_in_filepath)}' not in glob("/tmp/s3_temp/*"):
        print("GRD file not found, downloading from s3")
        grd_in_file = download_s3_file(grd_in_filepath)
    else:
        print("GRD file found, proceeding")
        grd_in_file = f'/tmp/s3_temp/{local_tif_basename(grd_in_filepath)}'

    calib_value = getCalibrationFactor(s3_metadata_paths)
    
    print('Generating Sigma Naught')
    
    ds = gdal.Open(grd_in_file, gdal.GA_ReadOnly)
    cols = ds.RasterXSize
    rows = ds.RasterYSize
    dn = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
    in_geo = ds.GetGeoTransform()
    projref = ds.GetProjection()

    print(f"[INFO] Image shape : {dn.shape}")
    print(f"[INFO] DN dtype    : {dn.dtype}")
    ds = None  # Close file

    dn_calib = dn * calib_value
    dn_filtered = lee_filter(dn_calib, size=filter_size)
    dn_amp = np.power(dn_filtered, 2)

    print(f"[INFO] Amplitude Max: ", np.max(dn_amp))
    print(f"[INFO] Amplitude Min: ", np.min(dn_amp))
    
    dn_db = 10.0*np.log10(dn_amp)
    print(f"[INFO] dB Max: ", np.max(dn_db))
    print(f"[INFO] dB Min: ", np.min(dn_db))

    ret_list = []
    outfile_amp = grd_in_file.replace(".tif", "_amp.tif")
    outfile_dB = grd_in_file.replace(".tif", "_dB.tif")

    if save_products in ["amp", "both"]:
        dump_geotiff_float(outfile_amp, dn_amp, projref, in_geo)
        print(f"Generation of amplitude file completed, file saved to {outfile_amp}")
        ret_list.append(outfile_amp)
    
    if save_products in ["db", "both"]:
        dump_geotiff_float(outfile_dB, dn_db, projref, in_geo)
        print(f"Generation of dB file completed, file saved to {outfile_dB}")
        ret_list.append(outfile_dB)

    return ret_list