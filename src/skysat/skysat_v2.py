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
        event_subdirs = sorted(
            set(
                x.split("/")[2]
                for x in filtered_files
                if x.split("/")[1] == event_dir
            )
        )

        for event_subdir in event_subdirs:
            folder_files = [
                x for x in filtered_files
                if x.split("/")[1] == event_dir
                and x.split("/")[2] == event_subdir
            ]

            if not folder_files:
                continue

            filename = folder_files[0].split("/")[-1]
            date_string = f"{filename.split('_')[0]}_{filename.split('_')[1]}"
            acquisition_date = datetime.strptime(date_string, "%Y%m%d_%H%M%S")

            subdirs[acquisition_date] = f"{prefix}/{event_dir}/{event_subdir}"

    if isinstance(date, str):
        date = datetime.strptime(date, "%Y-%m-%d %H:%M:%S")

    closest_date = min(
        subdirs,
        key=lambda d: abs(d - date)
    )

    selected = subdirs[closest_date]

    selected_files = [
        x for x in filtered_files
        if x.startswith(selected)
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
        stretched = np.power(stretched, 1.0/gamma)
        
    return stretched
    

def print_skysat_stats(name, array):
    valid = array[~np.isnan(array)]
    if valid.size > 0:
        print(f"  {name:6s} → min: {np.nanmin(array):7.4f}  max: {np.nanmax(array):7.4f}  "
              f"mean: {np.nanmean(array):7.4f}  median: {np.nanmedian(array):7.4f}  "
              f"valid px: {valid.size:,}")
    else:
        print(f"  {name:6s} → No valid data.")


def udm_mask(s3_image_paths: list[str], image_filepath: str, bands: list[np.ndarray]):
    """
    Apply the UDM2 mask corresponding to the specific SkySat image.
    u0001/u0002 identify different scenes, not TOA/SFC.
    """
    image_name = os.path.basename(image_filepath)

    udm_name = re.sub(
        r"_(?:analytic|visual|basic_analytic)\.(?:tif|tiff)$",
        "_udm2.tif",
        image_name,
        flags=re.IGNORECASE,
    )

    udm_filepaths = [
        x for x in s3_image_paths
        if os.path.basename(x).lower() == udm_name.lower()
    ]

    if not udm_filepaths:
        print(f"  [!] UDM2 file not found for {image_name}. Skipping mask step.")
        return bands

    in_filepath = udm_filepaths[0]
    local_path = f"/tmp/s3_temp/{local_tif_basename(in_filepath)}"

    if local_path not in glob("/tmp/s3_temp/*"):
        print("UDM2 file not found locally, downloading from s3")
        in_file = download_s3_file(in_filepath)
    else:
        print("UDM2 file found, proceeding")
        in_file = local_path

    with rasterio.open(in_file) as udm_src:
        clear = udm_src.read(UDM_CLEAR_BAND).astype(np.float32)

    mask = (clear != 1)

    for band in bands:
        if band is not None:
            band[mask] = np.nan

    total_px = mask.size
    clear_px = total_px - np.sum(mask)
    print(f"  Clear pixels : {clear_px:,} ({100*clear_px/total_px:.1f}%)")

    return bands


def get_skysat_product_files(
    s3_image_paths: list[str],
    product_type: Literal["visual", "analytic", "basic_analytic"],
):
    """
    Select SkySat files for the requested product.

    Regular analytic/visual:
        ..._ssc1_u0001_analytic.tif
        ..._ssc1_u0002_analytic.tif
        ..._ssc1_u0001_visual.tif
        ..._ssc1_u0002_visual.tif

    u0001/u0002 are separate scenes and BOTH are processed.

    basic_analytic:
        ..._ssc1d1_0001_basic_analytic.tif
        ..._ssc1d1_0002_basic_analytic.tif
        ...

    The tiled ssc1d files are ignored for regular analytic/visual products,
    but all basic_analytic tiles are included when basic_analytic is selected.
    """
    if product_type in ("analytic", "visual"):
        pattern = re.compile(
            rf"_ssc1_u\d+_{re.escape(product_type)}\.(?:tif|tiff)$",
            re.IGNORECASE,
        )
    elif product_type == "basic_analytic":
        pattern = re.compile(
            r"_ssc1d\d+_\d+_basic_analytic\.(?:tif|tiff)$",
            re.IGNORECASE,
        )
    else:
        raise ValueError(f"Unsupported SkySat product type: {product_type}")

    files = sorted(
        x for x in s3_image_paths
        if pattern.search(os.path.basename(x))
    )

    if not files:
        raise ValueError(
            f"No SkySat {product_type} files found for the selected acquisition."
        )

    print(f"SkySat {product_type} files selected: {len(files)}")
    for filepath in files:
        print(f"  {filepath}")

    return files


def get_reflectance_coefficients(ds):
    """Extract SkySat reflectance coefficients from ImageDescription metadata."""
    metadata = ds.GetMetadata()

    for key in ("TIFFTAG_IMAGEDESCRIPTION", "ImageDescription"):
        if key in metadata:
            try:
                meta_json = json.loads(metadata[key])
                coeffs = meta_json["properties"]["reflectance_coefficients"]
                print(f"  ✔ Extracted reflectance coefficients: {coeffs}")
                return coeffs
            except (json.JSONDecodeError, KeyError) as e:
                raise ValueError(
                    f"Failed to parse reflectance_coefficients from ImageDescription. "
                    f"Error: {e}"
                )

    raise ValueError("Could not find 'ImageDescription' tag in the GeoTIFF.")


def georeference_rgb_with_rpc(
    in_file: str,
    outfile: str,
    band1: np.ndarray,
    band2: np.ndarray,
    band3: np.ndarray,
    cols: int,
    rows: int,
):
    """
    Create an RGB GeoTIFF and georeference it using the RPC metadata
    from the original SkySat image.

    band1, band2, band3 are uint8 arrays representing the output
    RGB channels.
    """

    # Temporary RGB file before RPC orthorectification
    temp_rgb = f"{outfile}.tmp.tif"

    print("  Creating temporary RGB image...")

    driver = gdal.GetDriverByName("GTiff")

    out_ds = driver.Create(
        temp_rgb,
        cols,
        rows,
        3,
        gdal.GDT_Byte,
        options=[
            "COMPRESS=LZW",
            "TILED=YES"
        ]
    )

    if out_ds is None:
        raise RuntimeError(f"Could not create temporary RGB file: {temp_rgb}")

    # Write RGB bands
    out_ds.GetRasterBand(1).WriteArray(band1)
    out_ds.GetRasterBand(2).WriteArray(band2)
    out_ds.GetRasterBand(3).WriteArray(band3)

    # Set color interpretation
    out_ds.GetRasterBand(1).SetColorInterpretation(gdal.GCI_RedBand)
    out_ds.GetRasterBand(2).SetColorInterpretation(gdal.GCI_GreenBand)
    out_ds.GetRasterBand(3).SetColorInterpretation(gdal.GCI_BlueBand)

    out_ds.FlushCache()
    out_ds = None

    # ---------------------------------------------------------
    # Copy RPC metadata from original SkySat image
    # ---------------------------------------------------------

    print("  Copying RPC metadata...")

    src_ds = gdal.Open(in_file)

    if src_ds is None:
        raise RuntimeError(f"Could not open source file: {in_file}")

    rpc_metadata = src_ds.GetMetadata("RPC")

    if not rpc_metadata:
        src_ds = None
        raise ValueError(
            "No RPC metadata found in the SkySat source image."
        )

    rgb_ds = gdal.Open(temp_rgb, gdal.GA_Update)

    if rgb_ds is None:
        src_ds = None
        raise RuntimeError(
            f"Could not reopen temporary RGB file: {temp_rgb}"
        )

    rgb_ds.SetMetadata(rpc_metadata, "RPC")

    rgb_ds.FlushCache()
    rgb_ds = None
    src_ds = None

    # ---------------------------------------------------------
    # RPC orthorectification
    # ---------------------------------------------------------

    print("  Orthorectifying using SkySat RPC metadata...")

    warp_options = gdal.WarpOptions(
        format="GTiff",
        rpc=True,
        dstSRS="EPSG:4326",
        resampleAlg="bilinear",
        creationOptions=[
            "COMPRESS=LZW",
            "TILED=YES"
        ]
    )

    result = gdal.Warp(
        outfile,
        temp_rgb,
        options=warp_options
    )

    if result is None:
        raise RuntimeError(
            "GDAL RPC orthorectification failed."
        )

    result.FlushCache()
    result = None

    # Remove temporary file
    try:
        os.remove(temp_rgb)
    except OSError:
        pass

    print(f"  RPC georeferencing completed: {outfile}")

def calc_ndvi(s3_image_paths: list[str], save_location: str = "/tmp/s3_temp"):
    if save_location.endswith("/"):
        save_location = save_location[:-1]

    analytic_files = get_skysat_product_files(s3_image_paths, "analytic")
    output_files = []

    for in_filepath in analytic_files:
        print(f"\nGenerating NDVI for {in_filepath}")

        local_path = f"/tmp/s3_temp/{local_tif_basename(in_filepath)}"
        if local_path not in glob("/tmp/s3_temp/*"):
            print("Analytic file not found, downloading from s3")
            in_file = download_s3_file(in_filepath)
        else:
            print("Analytic file found, proceeding")
            in_file = local_path

        ds = gdal.Open(in_file)
        cols = ds.RasterXSize
        rows = ds.RasterYSize
        in_geo = ds.GetGeoTransform()
        projref = ds.GetProjectionRef()

        coeffs_list = get_reflectance_coefficients(ds)

        red = np.clip(
            ds.GetRasterBand(3).ReadAsArray(0, 0, cols, rows)
            * coeffs_list[2], 0, 1
        )
        nir = np.clip(
            ds.GetRasterBand(4).ReadAsArray(0, 0, cols, rows)
            * coeffs_list[3], 0, 1
        )

        red, nir = udm_mask(s3_image_paths, in_filepath, [red, nir])

        with np.errstate(invalid="ignore", divide="ignore"):
            denom_ndvi = nir + red
            ndvi = np.where(
                denom_ndvi != 0,
                (nir - red) / denom_ndvi,
                np.nan,
            )

        print_skysat_stats("NDVI", ndvi)
        ndvi[np.isnan(ndvi)] = -9999

        scene_name = os.path.splitext(os.path.basename(in_filepath))[0]
        outfile = f"{save_location}/{scene_name}_NDVI.tif"

        dump_geotiff_float(outfile, ndvi, projref, in_geo)
        print(f"Generation completed, file saved to {outfile}")
        output_files.append(outfile)

    return output_files


def calc_evi(s3_image_paths: list[str], save_location: str = "/tmp/s3_temp"):
    if save_location.endswith("/"):
        save_location = save_location[:-1]

    analytic_files = get_skysat_product_files(s3_image_paths, "analytic")
    output_files = []

    for in_filepath in analytic_files:
        print(f"\nGenerating EVI for {in_filepath}")

        local_path = f"/tmp/s3_temp/{local_tif_basename(in_filepath)}"
        if local_path not in glob("/tmp/s3_temp/*"):
            print("Analytic file not found, downloading from s3")
            in_file = download_s3_file(in_filepath)
        else:
            print("Analytic file found, proceeding")
            in_file = local_path

        ds = gdal.Open(in_file)
        cols = ds.RasterXSize
        rows = ds.RasterYSize
        in_geo = ds.GetGeoTransform()
        projref = ds.GetProjectionRef()

        coeffs_list = get_reflectance_coefficients(ds)

        blue = np.clip(
            ds.GetRasterBand(1).ReadAsArray(0, 0, cols, rows)
            * coeffs_list[0], 0, 1
        )
        red = np.clip(
            ds.GetRasterBand(3).ReadAsArray(0, 0, cols, rows)
            * coeffs_list[2], 0, 1
        )
        nir = np.clip(
            ds.GetRasterBand(4).ReadAsArray(0, 0, cols, rows)
            * coeffs_list[3], 0, 1
        )

        blue, red, nir = udm_mask(
            s3_image_paths, in_filepath, [blue, red, nir]
        )

        with np.errstate(invalid="ignore", divide="ignore"):
            denom_evi = nir + 6.0 * red - 7.5 * blue + 1.0
            evi = np.where(
                denom_evi != 0,
                2.5 * (nir - red) / denom_evi,
                np.nan,
            )
            evi = np.clip(evi, -1, 1)

        print_skysat_stats("EVI", evi)
        evi[np.isnan(evi)] = -9999

        scene_name = os.path.splitext(os.path.basename(in_filepath))[0]
        outfile = f"{save_location}/{scene_name}_EVI.tif"

        dump_geotiff_float(outfile, evi, projref, in_geo)
        print(f"Generation completed, file saved to {outfile}")
        output_files.append(outfile)

    return output_files


def calc_ndwi(s3_image_paths: list[str], save_location: str = "/tmp/s3_temp"):
    if save_location.endswith("/"):
        save_location = save_location[:-1]

    analytic_files = get_skysat_product_files(s3_image_paths, "analytic")
    output_files = []

    for in_filepath in analytic_files:
        print(f"\nGenerating NDWI for {in_filepath}")

        local_path = f"/tmp/s3_temp/{local_tif_basename(in_filepath)}"
        if local_path not in glob("/tmp/s3_temp/*"):
            print("Analytic file not found, downloading from s3")
            in_file = download_s3_file(in_filepath)
        else:
            print("Analytic file found, proceeding")
            in_file = local_path

        ds = gdal.Open(in_file)
        cols = ds.RasterXSize
        rows = ds.RasterYSize
        in_geo = ds.GetGeoTransform()
        projref = ds.GetProjectionRef()

        coeffs_list = get_reflectance_coefficients(ds)

        green = np.clip(
            ds.GetRasterBand(2).ReadAsArray(0, 0, cols, rows)
            * coeffs_list[1], 0, 1
        )
        nir = np.clip(
            ds.GetRasterBand(4).ReadAsArray(0, 0, cols, rows)
            * coeffs_list[3], 0, 1
        )

        green, nir = udm_mask(
            s3_image_paths, in_filepath, [green, nir]
        )

        with np.errstate(invalid="ignore", divide="ignore"):
            denom_ndwi = green + nir
            ndwi = np.where(
                denom_ndwi != 0,
                (green - nir) / denom_ndwi,
                np.nan,
            )

        print_skysat_stats("NDWI", ndwi)
        ndwi[np.isnan(ndwi)] = -9999

        scene_name = os.path.splitext(os.path.basename(in_filepath))[0]
        outfile = f"{save_location}/{scene_name}_NDWI.tif"

        dump_geotiff_float(outfile, ndwi, projref, in_geo)
        print(f"Generation completed, file saved to {outfile}")
        output_files.append(outfile)

    return output_files


def produce_truecolor(
    s3_image_paths: list[str],
    product_type: Literal["visual", "analytic", "basic_analytic"],
    save_location: str = "/tmp/s3_temp",
    gamma: float = 2.2,
):
    if save_location.endswith("/"):
        save_location = save_location[:-1]

    files = get_skysat_product_files(s3_image_paths, product_type)
    output_files = []

    for in_filepath in files:
        print(f"\nProcessing True Color: {in_filepath}")

        local_path = f"/tmp/s3_temp/{local_tif_basename(in_filepath)}"
        if local_path not in glob("/tmp/s3_temp/*"):
            print(f"{product_type} file not found, downloading from s3")
            in_file = download_s3_file(in_filepath)
        else:
            print(f"{product_type} file found, proceeding")
            in_file = local_path

        ds = gdal.Open(in_file)
        if ds is None:
            raise RuntimeError(f"Could not open SkySat file: {in_file}")

        cols = ds.RasterXSize
        rows = ds.RasterYSize

        band_arrays = []

        if product_type == "visual":
            print("  Visual file selected; reflectance coefficients will not be applied.")
            for band_num in [1, 2, 3]:
                values = ds.GetRasterBand(band_num).ReadAsArray(
                    0, 0, cols, rows
                )
                values = np.clip(values, 0, 255) / 255.0
                band_arrays.append(normalize_band(values, gamma=gamma))
        else:
            coeffs_list = get_reflectance_coefficients(ds)
            for band_num in [1, 2, 3]:
                values = ds.GetRasterBand(band_num).ReadAsArray(
                    0, 0, cols, rows
                )
                values = np.clip(
                    values * coeffs_list[band_num - 1],
                    0, 1
                )
                band_arrays.append(normalize_band(values, gamma=gamma))

        blue, green, red = band_arrays

        for band in band_arrays:
            band[np.isnan(band)] = 0

        red = (red * 255).astype(np.uint8)
        green = (green * 255).astype(np.uint8)
        blue = (blue * 255).astype(np.uint8)

        scene_name = os.path.splitext(os.path.basename(in_filepath))[0]
        outfile = f"{save_location}/{scene_name}_TrueColor.tif"

        if product_type == "basic_analytic":
            georeference_rgb_with_rpc(
                in_file, outfile, red, green, blue, cols, rows
            )
        else:
            in_geo = ds.GetGeoTransform()
            projref = ds.GetProjectionRef()
            dump_geotiff_rgb(
                outfile, red, green, blue, projref, in_geo
            )

        print(f"Generation completed, file saved to {outfile}")
        output_files.append(outfile)

    return output_files


def produce_colorir(
    s3_image_paths: list[str],
    product_type: Literal["analytic", "basic_analytic"],
    save_location: str = "/tmp/s3_temp",
    gamma: float = 2.2,
):
    if save_location.endswith("/"):
        save_location = save_location[:-1]

    if product_type == "visual":
        raise ValueError(
            "Color IR requires an analytic or basic_analytic file "
            "because the visual product does not provide the NIR band."
        )

    files = get_skysat_product_files(s3_image_paths, product_type)
    output_files = []

    for in_filepath in files:
        print(f"\nProcessing Color IR: {in_filepath}")

        local_path = f"/tmp/s3_temp/{local_tif_basename(in_filepath)}"
        if local_path not in glob("/tmp/s3_temp/*"):
            print(f"{product_type} file not found, downloading from s3")
            in_file = download_s3_file(in_filepath)
        else:
            print(f"{product_type} file found, proceeding")
            in_file = local_path

        ds = gdal.Open(in_file)
        if ds is None:
            raise RuntimeError(f"Could not open SkySat file: {in_file}")

        cols = ds.RasterXSize
        rows = ds.RasterYSize
        coeffs_list = get_reflectance_coefficients(ds)

        # Color IR = NIR, Red, Green.
        nir = ds.GetRasterBand(4).ReadAsArray(
            0, 0, cols, rows
        )
        red = ds.GetRasterBand(3).ReadAsArray(
            0, 0, cols, rows
        )
        green = ds.GetRasterBand(2).ReadAsArray(
            0, 0, cols, rows
        )

        nir = normalize_band(
            np.clip(nir * coeffs_list[3], 0, 1),
            gamma=gamma,
        )
        red = normalize_band(
            np.clip(red * coeffs_list[2], 0, 1),
            gamma=gamma,
        )
        green = normalize_band(
            np.clip(green * coeffs_list[1], 0, 1),
            gamma=gamma,
        )

        for band in [nir, red, green]:
            band[np.isnan(band)] = 0

        nir = (nir * 255).astype(np.uint8)
        red = (red * 255).astype(np.uint8)
        green = (green * 255).astype(np.uint8)

        scene_name = os.path.splitext(os.path.basename(in_filepath))[0]
        outfile = f"{save_location}/{scene_name}_ColorIR.tif"

        if product_type == "basic_analytic":
            georeference_rgb_with_rpc(
                in_file, outfile, nir, red, green, cols, rows
            )
        else:
            in_geo = ds.GetGeoTransform()
            projref = ds.GetProjectionRef()
            dump_geotiff_rgb(
                outfile, nir, red, green, projref, in_geo
            )

        print(f"Generation completed, file saved to {outfile}")
        output_files.append(outfile)

    return output_files