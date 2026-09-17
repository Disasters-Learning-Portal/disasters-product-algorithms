import os
import re
import numpy as np
from osgeo import gdal, osr
from datetime import datetime
from typing import Union
from glob import glob
from scipy.ndimage import uniform_filter
import xml.etree.ElementTree as ET

from shared_utils.s3utils import *
from shared_utils.geotools import *
from shared_utils.file_naming import create_sar_output_filename


# Declared nodata for the sigma0 dB product. sigmaCalib writes this value into
# the zero-fill border so the declared nodata and the actual fill agree (the
# same contract Capella's CAPELLA_NODATA carries). -9999.0, never 0: the output
# is float32 dB backscatter where 0 dB is a legitimate value.
ICEYE_NODATA = -9999.0


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

    if not dates:
        raise FileNotFoundError(
            f"No ICEYE GRD .tif found under s3://{bucket}/{prefix}/"
        )

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


def lee_filter(img: np.ndarray, size: int) -> np.ndarray:
    """NaN-aware Lee speckle filter (window ``size`` x ``size``).

    Same algorithm as ``capella_v2.lee_filter`` / ``umbra_v2.lee_filter``:
    non-finite pixels are ignored rather than counted as zero, so the GRD
    zero-fill border (masked to NaN by the caller) does not bleed into the
    filtered interior, and stays NaN in the output.
    """
    valid = np.isfinite(img)
    if not valid.any():
        return img

    v = valid.astype(np.float64)
    filled = np.where(valid, img, 0.0).astype(np.float64)

    # uniform_filter returns the window MEAN; dividing the filled mean by the
    # valid-fraction recovers the mean over valid pixels only (window size
    # cancels), so invalid neighbours are ignored rather than counted as zero.
    frac = uniform_filter(v, size, mode="constant")
    safe = frac > 0

    local_mean = np.zeros_like(filled)
    local_mean[safe] = uniform_filter(filled, size, mode="constant")[safe] / frac[safe]

    local_sqr = np.zeros_like(filled)
    local_sqr[safe] = uniform_filter(filled ** 2, size, mode="constant")[safe] / frac[safe]

    local_var = np.clip(local_sqr - local_mean ** 2, 0.0, None)
    overall_var = img[valid].var()

    weights = local_var / (local_var + overall_var + 1e-12)
    out = local_mean + weights * (filled - local_mean)

    return np.where(valid, out, np.nan)


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
    """Parse an ICEYE ``coord_*`` element into ``(col, row, lat, lon)``.

    The metadata reference documents the value as ``[x(col), y(row), lat, lon]``
    and shows it bracketed + comma-separated (``[16878,1,35.17738,-118.11233]``);
    deliveries have also been seen whitespace-separated. Accept both.
    """
    parts = re.split(r"[\s,]+", value.strip().strip("[]").strip())

    if len(parts) != 4:
        raise ValueError(f"Unexpected ICEYE coordinate format: {value}")

    return int(float(parts[0])), int(float(parts[1])), float(parts[2]), float(parts[3])


def georeference_from_iceye_xml(metadata, cols, rows):
    """Affine geotransform (EPSG:4326) fitted to the four XML corner coordinates.

    Fallback for a GRD whose GeoTIFF carries no geotransform/projection (GDAL
    reports the identity transform for a GCP/RPC-only file). The four corners
    are the CENTRES of the first/last pixels in range and azimuth, so the
    GDAL origin (top-left corner of the top-left pixel) is half a pixel back
    along each axis. Approximate: ICEYE GRD is ground-range/azimuth geometry,
    not a map projection, so an affine fit is exact only at the corners.
    """
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

    # Corner coords are pixel centres; GDAL's origin is the pixel's outer
    # corner, i.e. half a pixel back along BOTH axes (not a full pixel).
    origin_lon = lon_tl - 0.5 * lon_per_col - 0.5 * lon_per_row
    origin_lat = lat_tl - 0.5 * lat_per_col - 0.5 * lat_per_row

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
               save_location: str = "/tmp/s3_temp", filter_size: int = 5) -> str:
    """Calibrate an ICEYE GRD to sigma0 in dB. Returns the output GeoTIFF path."""

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
    # float64 BEFORE any arithmetic: the vendor DN is uint16 and DN**2 would
    # silently wrap.
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

    # The GRD zero-fill border is not data. Mask it to NaN so the NaN-aware
    # filter neither smooths it into the scene nor smears the scene into it.
    dn[dn == 0] = np.nan

    # Vendor-script order (PR #79, confirmed by the calibration owner on
    # #148): Lee filter on the RAW DN first, then square, then calibrate,
    # then dB. This deliberately differs from Capella/Umbra, which filter the
    # CALIBRATED linear backscatter.
    dn_filtered = lee_filter(dn, size=filter_size)
    dn_sqr = np.power(dn_filtered, 2)
    dn_amp = dn_sqr * calib_value

    valid = np.isfinite(dn_amp) & (dn_amp > 0)
    print("[INFO] Amplitude Max: ", np.max(dn_amp[valid]) if valid.any() else None)
    print("[INFO] Amplitude Min: ", np.min(dn_amp[valid]) if valid.any() else None)

    dn_db = np.full(dn_amp.shape, ICEYE_NODATA, dtype=np.float64)
    dn_db[valid] = 10.0 * np.log10(dn_amp[valid])

    finite = dn_db[valid]
    if finite.size:
        print(f"[INFO] dB range: {np.min(finite)} -> {np.max(finite)} dB "
              f"(nodata {ICEYE_NODATA})")
    else:
        print(f"[INFO] dB: no valid pixels (all {ICEYE_NODATA})")

    dt = datetime.strptime(
        grd_in_file.split("_")[-1].split(".")[0],
        "%Y%m%dT%H%M%S"
    )

    # One shared SAR name builder (CLAUDE.md / .clinerules.md rule 51):
    #   ICEYE-X48_sigma0-dB_filtered5_2026-05-13T15:48:16Z.tif
    outfile = os.path.join(
        save_location,
        create_sar_output_filename(
            f"ICEYE-{grd_in_file.split('/')[-1].split('_')[1]}",
            "sigma0-dB",
            dt,
            filter_size,
        ),
    )

    dump_geotiff_float(outfile, dn_db, projref, in_geo)

    print(f"Generation of dB file completed, file saved to {outfile}")

    return outfile
