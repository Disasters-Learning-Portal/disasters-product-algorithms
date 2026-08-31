import os
import numpy as np
from osgeo import gdal, osr
from datetime import datetime
from typing import Union
from glob import glob
from scipy.ndimage import uniform_filter, variance
import xml.etree.ElementTree as ET

from shared_utils.s3utils import *
from shared_utils.geotools import *


def retrieve_iceye_resources(date: Union[str, datetime], bucket="csdap-iceye-delivery", prefix="disasters"):
    files = retrieve_s3_file_list(bucket, prefix)
    filtered_files = [x for x in files if len(x.split("/")) > 2]

    datestrings = set([
        x.split("/")[-1] for x in filtered_files
        if x.split("/")[-1].endswith(".tif") and "GRD" in x.split("/")[-1]
    ])
    dates = [
        datetime.strptime(x.split("_")[-1].split(".")[0], "%Y%m%dT%H%M%S")
        for x in datestrings
    ]

    if isinstance(date, str):
        date = datetime.strptime(date, "%Y-%m-%d %H:%M:%S")

    closest_date = min(dates, key=lambda d: abs(d - date))
    selected_files = [
        x for x in filtered_files
        if x.split(".")[0].endswith(closest_date.strftime("%Y%m%dT%H%M%S"))
    ]

    tifs = [x for x in selected_files if x.lower().endswith((".tif", ".tiff"))]
    metadata = [x for x in selected_files if x.lower().endswith(".xml")]

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
    return img_mean + img_weights * (img - img_mean)


def get_grd_xml(s3_metadata_paths):
    grd_xml_paths = [
        x for x in s3_metadata_paths
        if x.lower().endswith(".xml") and "grd" in x.split("/")[-1].lower()
    ]

    if not grd_xml_paths:
        raise FileNotFoundError("No ICEYE GRD XML metadata file was found.")

    xml_in_filepath = grd_xml_paths[0]
    local_xml = f"/tmp/s3_temp/{local_tif_basename(xml_in_filepath)}"

    if local_xml not in glob("/tmp/s3_temp/*"):
        print("XML file not found, downloading from s3")
        return download_s3_file(xml_in_filepath)

    print("XML file found, proceeding")
    return local_xml


def parse_grd_metadata(xml_in_file):
    tree = ET.parse(xml_in_file)
    root = tree.getroot()

    def get_text(name, required=False):
        elem = root.find(name)
        if elem is None or elem.text is None:
            if required:
                raise ValueError(f"Required ICEYE GRD metadata field '{name}' was not found.")
            return None
        return elem.text.strip()

    def get_float(name, required=False):
        value = get_text(name, required)
        return None if value is None else float(value)

    return {
        "calibration_factor": get_float("calibration_factor", required=True),
        "geo_ref_system": get_text("geo_ref_system", required=True),
        "number_of_azimuth_samples": get_float("number_of_azimuth_samples"),
        "number_of_range_samples": get_float("number_of_range_samples"),
        "range_spacing": get_float("range_spacing"),
        "azimuth_spacing": get_float("azimuth_spacing"),
        "coord_first_near": get_text("coord_first_near"),
        "coord_first_far": get_text("coord_first_far"),
        "coord_last_near": get_text("coord_last_near"),
        "coord_last_far": get_text("coord_last_far"),
    }


def parse_corner_coordinate(value):
    parts = value.split()

    if len(parts) != 4:
        raise ValueError(f"Unexpected ICEYE coordinate format: {value}")

    return int(parts[0]), int(parts[1]), float(parts[2]), float(parts[3])


def georeference_from_iceye_xml(metadata, cols, rows):
    required = [
        "coord_first_near", "coord_first_far",
        "coord_last_near", "coord_last_far"
    ]

    missing = [key for key in required if not metadata.get(key)]
    if missing:
        raise ValueError(
            f"ICEYE GRD XML is missing required geolocation fields: {missing}"
        )

    _, _, lat_tl, lon_tl = parse_corner_coordinate(metadata["coord_first_near"])
    _, _, lat_tr, lon_tr = parse_corner_coordinate(metadata["coord_first_far"])
    _, _, lat_bl, lon_bl = parse_corner_coordinate(metadata["coord_last_near"])
    _, _, lat_br, lon_br = parse_corner_coordinate(metadata["coord_last_far"])

    print("[INFO] TIFF has no embedded georeferencing.")
    print("[INFO] Using ICEYE GRD XML corner coordinates.")
    print(f"[INFO] UL: lat={lat_tl}, lon={lon_tl}")
    print(f"[INFO] UR: lat={lat_tr}, lon={lon_tr}")
    print(f"[INFO] LL: lat={lat_bl}, lon={lon_bl}")
    print(f"[INFO] LR: lat={lat_br}, lon={lon_br}")

    dx_pixels = cols - 1
    dy_pixels = rows - 1

    lon_per_col = (
        ((lon_tr - lon_tl) / dx_pixels) +
        ((lon_br - lon_bl) / dx_pixels)
    ) / 2.0

    lat_per_row = (
        ((lat_bl - lat_tl) / dy_pixels) +
        ((lat_br - lat_tr) / dy_pixels)
    ) / 2.0

    lon_per_row = (
        ((lon_bl - lon_tl) / dy_pixels) +
        ((lon_br - lon_tr) / dy_pixels)
    ) / 2.0

    lat_per_col = (
        ((lat_tr - lat_tl) / dx_pixels) +
        ((lat_br - lat_bl) / dx_pixels)
    ) / 2.0

    origin_lon = lon_tl - lon_per_col - lon_per_row
    origin_lat = lat_tl - lat_per_col - lat_per_row

    in_geo = (
        origin_lon,
        lon_per_col,
        lon_per_row,
        origin_lat,
        lat_per_col,
        lat_per_row
    )

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    projref = srs.ExportToWkt()

    print(f"[INFO] XML-derived GeoTransform: {in_geo}")
    print("[INFO] XML-derived Projection: EPSG:4326")

    return projref, in_geo


def sigmaCalib(s3_image_paths: list[str], s3_metadata_paths: list[str],
               save_location: str = "/tmp/s3_temp", filter_size: int = 5):

    if save_location.endswith("/"):
        save_location = save_location[:-1]
    os.makedirs(save_location, exist_ok=True)

    print("Collecting needed files...")

    grd_in_filepath = [
        x for x in s3_image_paths
        if x.lower().endswith(".tif") and "grd" in x.split("/")[-1].lower()
    ][0]

    local_grd = f"/tmp/s3_temp/{local_tif_basename(grd_in_filepath)}"

    if local_grd not in glob("/tmp/s3_temp/*"):
        print("GRD file not found, downloading from s3")
        grd_in_file = download_s3_file(grd_in_filepath)
    else:
        print("GRD file found, proceeding")
        grd_in_file = local_grd

    xml_in_file = get_grd_xml(s3_metadata_paths)
    metadata = parse_grd_metadata(xml_in_file)
    calib_value = metadata["calibration_factor"]

    print(f"[INFO] Metadata-sourced calibration factor : {calib_value}")
    print("Generating Sigma Naught")

    ds = gdal.Open(grd_in_file, gdal.GA_ReadOnly)

    if ds is None:
        raise RuntimeError(f"GDAL could not open ICEYE GRD: {grd_in_file}")

    cols = ds.RasterXSize
    rows = ds.RasterYSize
    dn = ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
    dn = dn.astype(np.float64)

    in_geo = ds.GetGeoTransform()
    projref = ds.GetProjection()

    print(f"[INFO] GeoTransform      : {in_geo}")
    print(f"[INFO] Source Projection : {projref}")
    print(f"[INFO] Image shape : {dn.shape}")
    print(f"[INFO] DN dtype    : {dn.dtype}")

    ds = None

    default_geo = (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)

    has_valid_projection = bool(projref)
    has_valid_geotransform = in_geo is not None and in_geo != default_geo

    if not (has_valid_projection and has_valid_geotransform):
        print("[INFO] Embedded TIFF georeferencing is missing or invalid.")
        projref, in_geo = georeference_from_iceye_xml(
            metadata, cols, rows
        )

    dn_sqr = np.power(dn, 2)
    dn_amp = dn_sqr * calib_value
    dn_filtered = lee_filter(dn_amp, size=filter_size)

    print("[INFO] Amplitude Max: ", np.max(dn_filtered))
    print("[INFO] Amplitude Min: ", np.min(dn_filtered))

    dn_db = 10.0 * np.log10(dn_filtered)

    print("[INFO] dB Max: ", np.max(dn_db))
    print("[INFO] dB Min: ", np.min(dn_db))

    dt = datetime.strptime(
        grd_in_file.split("_")[-1].split(".")[0],
        "%Y%m%dT%H%M%S"
    )

    outfile = (
        f"{save_location}/"
        f"{dt.strftime('%Y%m')}_"
        f"ICEYE-{grd_in_file.split('/')[-1].split('_')[1]}_"
        f"sigma0-dB_"
        f"{dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"_filtered{filter_size}.tif"
    )

    dump_geotiff_float(outfile, dn_db, projref, in_geo)

    print(f"Generation of dB file completed, file saved to {outfile}")

    return outfile
