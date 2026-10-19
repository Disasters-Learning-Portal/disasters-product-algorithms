import os
import re
import shutil
import tempfile
import numpy as np
from osgeo import gdal, osr
from datetime import datetime
from typing import Union
from scipy.ndimage import uniform_filter
import xml.etree.ElementTree as ET

from shared_utils.s3utils import *
from shared_utils.file_naming import create_sar_output_filename
from shared_utils.product_paths import product_output_dir


# Declared nodata for the sigma0 dB product. sigmaCalib writes this value into
# the zero-fill border so the declared nodata and the actual fill agree (the
# same contract Capella's CAPELLA_NODATA carries). -9999.0, never 0: the output
# is float32 dB backscatter where 0 dB is a legitimate value.
ICEYE_NODATA = -9999.0


def retrieve_iceye_resources(date: Union[str, datetime], bucket="csdap-iceye-delivery", prefix="disasters"):
    files = retrieve_s3_file_list(bucket, prefix)
    filtered_files = [x for x in files if len(x.split("/")) > 2]

    # Case-insensitive (CSDA ships uppercase .TIF). endswith() also keeps the
    # .tif.sha256 / .tif.sig siblings the bucket lists next to each file out.
    datestrings = set([
        x.split("/")[-1] for x in filtered_files
        if x.split("/")[-1].lower().endswith(".tif") and "grd" in x.split("/")[-1].lower()
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
        if x.split("/")[-1].split(".")[0].endswith(closest_date.strftime("%Y%m%dT%H%M%S"))
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

    Works in float32 and in place where it can: an SLF scene is ~600M pixels,
    so every full-size float64 temporary costs ~5 GB. ``uniform_filter``
    accumulates each window in double internally, so float32 storage loses
    nothing that matters for a dB product.
    """
    img = np.asarray(img, dtype=np.float32)
    valid = np.isfinite(img)
    if not valid.any():
        return img

    # Before the big temporaries exist: img[valid] is a full-size copy.
    overall_var = np.float32(img[valid].var())

    filled = np.where(valid, img, np.float32(0.0))

    # uniform_filter returns the window MEAN; dividing the filled mean by the
    # valid-fraction recovers the mean over valid pixels only (window size
    # cancels), so invalid neighbours are ignored rather than counted as zero.
    frac = uniform_filter(valid.astype(np.float32), size, mode="constant")
    # A valid pixel always has frac >= 1/size**2; the half-step threshold keeps
    # float32 round-off around an all-invalid window from counting as "some".
    safe = frac > 0.5 / (size * size)

    local_mean = uniform_filter(filled, size, mode="constant")
    np.divide(local_mean, frac, out=local_mean, where=safe)

    local_var = np.multiply(filled, filled)
    local_var = uniform_filter(local_var, size, mode="constant")
    np.divide(local_var, frac, out=local_var, where=safe)
    del frac, safe

    # local_var = E[x^2] - E[x]^2, clipped at 0 against round-off.
    local_var -= local_mean * local_mean
    np.maximum(local_var, 0.0, out=local_var)

    # weights = var / (var + overall_var), reusing local_var's buffer.
    local_var /= local_var + (overall_var + np.float32(1e-12))

    # out = mean + weights * (x - mean), built in `filled`.
    filled -= local_mean
    filled *= local_var
    filled += local_mean
    del local_mean, local_var

    filled[~valid] = np.nan
    return filled


def get_grd_xml(s3_metadata_paths, download_dir: str = "/tmp/s3_temp"):
    grd_xml_paths = [
        x for x in s3_metadata_paths
        if x.lower().endswith(".xml") and "grd" in x.split("/")[-1].lower()
    ]

    if not grd_xml_paths:
        raise FileNotFoundError("No ICEYE GRD XML metadata file was found.")

    xml_in_filepath = grd_xml_paths[0]
    local_xml = os.path.join(download_dir, local_tif_basename(xml_in_filepath))

    if not os.path.exists(local_xml):
        print("XML file not found, downloading from s3")
        return download_s3_file(xml_in_filepath, download_dir)

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
    """Affine geotransform (EPSG:4326) least-squares fitted to the XML corners.

    LAST-RESORT fallback (see ``resolve_georeference``): only for a GRD that
    carries neither a geotransform nor GCPs. ICEYE GRD is ground-range/azimuth
    geometry, not a map projection, so no affine fits it: measured against the
    vendor GCP grid, this was 9-18 m off on flat SM terrain and 118-406 m off
    on ScanSAR over relief.

    Each ``coord_*`` carries its own ``(col, row)``: 1-based indices of a pixel
    CENTRE (``coord_first_near`` is ``1 1 lat lon``). In GDAL pixel coordinates
    (origin at the outer corner of the first pixel) index ``i`` is ``i - 0.5``,
    so XML (1, 1) is GDAL (0.5, 0.5). Fitting on those parsed positions, rather
    than assuming which corner is which, keeps a flipped delivery honest.
    ``cols``/``rows`` are not needed for the fit; they are kept for the caller.
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

    corners = [parse_corner_coordinate(metadata[key]) for key in required]

    print("[WARNING] " + "=" * 66)
    print("[WARNING] ICEYE GRD carries NO geotransform and NO GCPs.")
    print("[WARNING] Falling back to a 4-corner AFFINE fit from the XML. This is")
    print("[WARNING] APPROXIMATE: expect tens to hundreds of metres of error in")
    print("[WARNING] the interior, worst over terrain.")
    print("[WARNING] " + "=" * 66)
    for key, (col, row, lat, lon) in zip(required, corners):
        print(f"[INFO] {key}: col={col}, row={row}, lat={lat}, lon={lon}")

    design = np.array([[1.0, col - 0.5, row - 0.5] for col, row, _, _ in corners])
    lons = np.array([c[3] for c in corners])
    lats = np.array([c[2] for c in corners])

    lon_coef, _, rank, _ = np.linalg.lstsq(design, lons, rcond=None)
    if rank < 3:
        raise ValueError(
            "ICEYE GRD XML corner (col, row) indices are degenerate; cannot fit "
            f"a geotransform: {[(c[0], c[1]) for c in corners]}"
        )
    lat_coef, _, _, _ = np.linalg.lstsq(design, lats, rcond=None)

    in_geo = (
        float(lon_coef[0]), float(lon_coef[1]), float(lon_coef[2]),
        float(lat_coef[0]), float(lat_coef[1]), float(lat_coef[2]),
    )

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    projref = srs.ExportToWkt()

    print(f"[INFO] XML-derived GeoTransform: {in_geo}")
    print("[INFO] XML-derived Projection: EPSG:4326")

    return projref, in_geo


def resolve_georeference(ds, metadata):
    """Pick the georeferencing for an opened GRD, best source first.

    Returns ``{"kind", "projection", "geotransform", "gcps"}`` where ``kind`` is

    * ``"geotransform"`` -- the file's own geotransform + projection (SLH/SLF).
      May be rotated; ``write_georeferenced`` warps it north-up.
    * ``"gcps"`` -- the file's GCP grid (SM/ScanSAR carry no geotransform but
      a dense EPSG:4326 GCP grid). Kept as GCPs and warped with a thin-plate
      spline, which honours every GCP.
    * ``"xml"`` -- neither: the approximate XML 4-corner affine, with a warning.
    """
    in_geo = ds.GetGeoTransform(can_return_null=True)
    projref = ds.GetProjection()

    identity = (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    if projref and in_geo is not None and tuple(in_geo) != identity:
        print(f"[INFO] Using embedded GeoTransform: {in_geo}")
        return {"kind": "geotransform", "projection": projref,
                "geotransform": tuple(in_geo), "gcps": None}

    gcps = ds.GetGCPs()
    if gcps:
        gcp_proj = ds.GetGCPProjection()
        if not gcp_proj:
            # ICEYE documents its GCPs as WGS84 lon/lat.
            print("[WARNING] GCPs carry no projection; assuming EPSG:4326.")
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(4326)
            gcp_proj = srs.ExportToWkt()
        # ICEYE GCP pixel/line are pixel CENTRES: GCP (0, 0) matches the XML's
        # coord_first_near ("1 1", centre of the first pixel) to <1 m on real
        # SM scenes. GDAL reads GCP pixel/line as pixel CORNERS, so shift by
        # half a pixel or the whole product lands half a pixel off.
        # Skip it for a file tagged PixelIsPoint: GDAL's GTiff reader already
        # adds the half pixel there, and shifting again would double it.
        if ds.GetMetadataItem("AREA_OR_POINT") != "Point":
            gcps = [gdal.GCP(g.GCPX, g.GCPY, g.GCPZ,
                             g.GCPPixel + 0.5, g.GCPLine + 0.5)
                    for g in gcps]
        print(f"[INFO] Using {len(gcps)} embedded GCPs (thin-plate-spline warp).")
        return {"kind": "gcps", "projection": gcp_proj,
                "geotransform": None, "gcps": gcps}

    projref, in_geo = georeference_from_iceye_xml(
        metadata, ds.RasterXSize, ds.RasterYSize
    )
    return {"kind": "xml", "projection": projref,
            "geotransform": in_geo, "gcps": None}


def _write_float(path, arr, georef, creation_options=()):
    rows, cols = arr.shape
    out = gdal.GetDriverByName("GTiff").Create(
        path, cols, rows, 1, gdal.GDT_Float32, list(creation_options)
    )
    if georef["gcps"] is not None:
        out.SetGCPs(georef["gcps"], georef["projection"])
    else:
        out.SetProjection(georef["projection"])
        out.SetGeoTransform(georef["geotransform"])
    band = out.GetRasterBand(1)
    band.SetNoDataValue(ICEYE_NODATA)
    band.WriteArray(arr)
    out = None


def write_georeferenced(outfile, arr, georef):
    """Write ``arr`` to ``outfile`` as a north-up GeoTIFF using ``georef``.

    A north-up geotransform is written directly. GCPs and rotated
    geotransforms are first written with that georeferencing attached to a
    per-call temp file, then ``gdal.Warp``ed north-up into ``outfile``:
    GCPs to EPSG:4326 with a thin-plate spline (``tps=True``) -- with a dense
    grid (230-1505 GCPs per scene) it reproduces each GCP exactly and bends
    smoothly between them, where a global polynomial of order 1-3 would
    still leave residuals at the GCPs over terrain; a rotated geotransform
    stays in its own CRS. Bilinear resampling (continuous dB), with
    ``ICEYE_NODATA`` as source and destination nodata so the border never
    blends into the data.
    """
    gt = georef["geotransform"]
    if georef["gcps"] is None and gt[2] == 0 and gt[4] == 0:
        _write_float(outfile, arr, georef)
        return outfile

    tmpdir = tempfile.mkdtemp(prefix="iceye_georef_")
    try:
        src = os.path.join(tmpdir, "georef_src.tif")
        _write_float(src, arr, georef,
                     ["TILED=YES", "BIGTIFF=IF_SAFER"])

        options = gdal.WarpOptions(
            format="GTiff",
            dstSRS="EPSG:4326" if georef["gcps"] is not None else georef["projection"],
            tps=georef["gcps"] is not None,
            resampleAlg="bilinear",
            srcNodata=ICEYE_NODATA,
            dstNodata=ICEYE_NODATA,
            multithread=True,
            warpOptions=["NUM_THREADS=ALL_CPUS"],
            creationOptions=["TILED=YES", "BIGTIFF=IF_SAFER"],
        )
        warped = gdal.Warp(outfile, src, options=options)
        if warped is None:
            raise RuntimeError(f"gdal.Warp failed georeferencing {outfile}")
        warped = None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return outfile


def sigmaCalib(s3_image_paths: list[str], s3_metadata_paths: list[str],
               save_location: str = "/tmp/s3_temp", filter_size: int = 5,
               download_dir: str = "/tmp/s3_temp") -> str:
    """Calibrate an ICEYE GRD to sigma0 in dB. Returns the output GeoTIFF path."""

    if save_location.endswith("/"):
        save_location = save_location[:-1]
    os.makedirs(save_location, exist_ok=True)

    print("Collecting needed files...")

    grd_in_filepath = [
        x for x in s3_image_paths
        if x.lower().endswith(".tif") and "grd" in x.split("/")[-1].lower()
    ][0]

    # The raw (licensed) GRD goes to `download_dir`, never `save_location`: the
    # CLI hands in a per-run scratch dir it deletes afterwards
    # (shared_utils.scratch), and save_location is the notebook OUTPUT_DIR
    # whose every .tif gets published.
    local_grd = os.path.join(download_dir, local_tif_basename(grd_in_filepath))

    if not os.path.exists(local_grd):
        print("GRD file not found, downloading from s3")
        grd_in_file = download_s3_file(grd_in_filepath, download_dir)
    else:
        print("GRD file found, proceeding")
        grd_in_file = local_grd

    xml_in_file = get_grd_xml(s3_metadata_paths, download_dir)
    metadata = parse_grd_metadata(xml_in_file)
    calib_value = metadata["calibration_factor"]

    print(f"[INFO] Metadata-sourced calibration factor : {calib_value}")
    print("Generating Sigma Naught")

    ds = gdal.Open(grd_in_file, gdal.GA_ReadOnly)

    if ds is None:
        raise RuntimeError(f"GDAL could not open ICEYE GRD: {grd_in_file}")

    georef = resolve_georeference(ds, metadata)

    # Read straight into float32 BEFORE any arithmetic: the vendor DN is
    # UInt16 for SM/ScanSAR (DN**2 would wrap) and already Float32 for SLF.
    # 65535**2 ~ 4.3e9 is far inside float32 range and its ~7 significant
    # digits are ample for dB; float64 would double a ~600M-pixel scene.
    dn = ds.GetRasterBand(1).ReadAsArray(buf_type=gdal.GDT_Float32)

    print(f"[INFO] Image shape : {dn.shape}")
    print(f"[INFO] DN dtype    : {dn.dtype}")

    ds = None

    # The GRD zero-fill border is not data. Mask it to NaN so the NaN-aware
    # filter neither smooths it into the scene nor smears the scene into it.
    dn[dn == 0] = np.nan

    # Vendor-script order (PR #79, confirmed by the calibration owner on
    # #148): Lee filter on the RAW DN first, then square, then calibrate,
    # then dB. This deliberately differs from Capella/Umbra, which filter the
    # CALIBRATED linear backscatter. Done in place from here on.
    dn = lee_filter(dn, size=filter_size)
    dn *= dn
    dn *= np.float32(calib_value)

    valid = np.isfinite(dn) & (dn > 0)
    print("[INFO] Amplitude Max: ", np.max(dn, where=valid, initial=-np.inf) if valid.any() else None)
    print("[INFO] Amplitude Min: ", np.min(dn, where=valid, initial=np.inf) if valid.any() else None)

    np.log10(dn, out=dn, where=valid)
    dn *= np.float32(10.0)
    dn[~valid] = ICEYE_NODATA

    if valid.any():
        print(f"[INFO] dB range: {np.min(dn, where=valid, initial=np.inf)} -> "
              f"{np.max(dn, where=valid, initial=-np.inf)} dB "
              f"(nodata {ICEYE_NODATA})")
    else:
        print(f"[INFO] dB: no valid pixels (all {ICEYE_NODATA})")
    del valid

    grd_name = os.path.basename(grd_in_file)
    dt = datetime.strptime(
        grd_name.split("_")[-1].split(".")[0],
        "%Y%m%dT%H%M%S"
    )

    # One shared SAR name builder (CLAUDE.md / .clinerules.md rule 51):
    #   ICEYE-X48_sigma0-dB_filtered5_2026-05-13T15:48:16Z.tif
    outfile = os.path.join(
        product_output_dir(save_location, "iceye", "sigma0-dB"),
        create_sar_output_filename(
            f"ICEYE-{grd_name.split('_')[1]}",
            "sigma0-dB",
            dt,
            filter_size,
        ),
    )

    write_georeferenced(outfile, dn, georef)

    print(f"Generation of dB file completed, file saved to {outfile}")

    return outfile
