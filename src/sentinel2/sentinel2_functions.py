"""
sentinel2_functions.py

Sentinel-2 processing functions.

This module contains the processing logic used by the Sentinel-2
ODR notebooks. The CLI and notebooks should call these functions
rather than containing the processing logic themselves.
"""

import json
import math
import os
import shutil
import tempfile
from pathlib import Path

import boto3
import numpy as np
import rasterio as rio

from pystac_client import Client
from rio_cogeo.cogeo import cog_translate
from rasterio.enums import Resampling
from rasterio.merge import merge
from rasterio.warp import calculate_default_transform, reproject
from rasterio.session import AWSSession

_aws_session = AWSSession(
    boto3.Session(),
    requester_pays=True,
)

_aws_unsigned_session = AWSSession(
    boto3.Session(),
    aws_unsigned=True,
)

# ---------------------------------------------------------------------
# Algorithm configuration
# ---------------------------------------------------------------------

def load_algorithms(
    algorithm_file="algorithms-sentinel2.json",
):
    """
    Load Sentinel-2 algorithm definitions from JSON.

    Parameters
    ----------
    algorithm_file : str
        Path to algorithms-sentinel2.json.

    Returns
    -------
    dict
        Dictionary containing the configured Sentinel-2 algorithms.
    """

    with open(algorithm_file, "r") as f:
        algorithms = json.load(f)

    return algorithms


def get_algorithm(
    algorithm_type,
    algorithm_name,
    algorithm_file="algorithms-sentinel2.json",
):
    """
    Retrieve a specific algorithm from the Sentinel-2 algorithm catalog.

    Parameters
    ----------
    algorithm_type : str
        Algorithm category, such as "index" or "composite".

    algorithm_name : str
        Name of the algorithm, such as "ndvi" or "swir".

    algorithm_file : str
        Path to algorithms-sentinel2.json.

    Returns
    -------
    dict
        Algorithm configuration.
    """

    algorithms = load_algorithms(algorithm_file)

    if algorithm_type not in algorithms:
        raise ValueError(
            f"Algorithm type '{algorithm_type}' was not found in "
            f"{algorithm_file}."
        )

    algorithm = algorithms[algorithm_type][0].get(algorithm_name)

    if algorithm is None:
        raise ValueError(
            f"Algorithm '{algorithm_name}' was not found under "
            f"algorithm type '{algorithm_type}'."
        )

    return algorithm


# ---------------------------------------------------------------------
# STAC
# ---------------------------------------------------------------------

def connect_to_stac(
    stac_api_url="https://earth-search.aws.element84.com/v1",
):
    """
    Connect to the Sentinel-2 STAC catalog.

    Parameters
    ----------
    stac_api_url : str
        STAC API endpoint.

    Returns
    -------
    pystac_client.Client
        Connected STAC catalog.
    """

    print(f"Connecting to STAC catalog: {stac_api_url}")

    catalog = Client.open(stac_api_url)

    return catalog


def search_sentinel2(
    start_date,
    end_date,
    bbox,
    cloud_cover=50,
    stac_api_url="https://earth-search.aws.element84.com/v1",
    collection_id="sentinel-2-c1-l2a",
):
    """
    Search the Sentinel-2 STAC catalog.

    Parameters
    ----------
    start_date : str
        Search start date.

    end_date : str
        Search end date.

    bbox : list
        Bounding box in the form:
        [xmin, ymin, xmax, ymax]

    cloud_cover : float
        Maximum allowed cloud cover percentage.

    stac_api_url : str
        STAC API endpoint.

    collection_id : str
        Sentinel-2 STAC collection.

    Returns
    -------
    list
        List of STAC Items.
    """

    catalog = connect_to_stac(stac_api_url)

    query = {
        "eo:cloud_cover": {
            "lt": cloud_cover
        }
    }

    print("Searching Sentinel-2 STAC catalog...")
    print(f"  Collection: {collection_id}")
    print(f"  Start date: {start_date}")
    print(f"  End date:   {end_date}")
    print(f"  Bounding box: {bbox}")
    print(f"  Cloud cover < {cloud_cover}%")

    search = catalog.search(
        collections=[collection_id],
        datetime=[start_date, end_date],
        bbox=bbox,
        query=query,
    )

    items = list(search.items())

    print(f"Found {len(items)} Sentinel-2 items.")

    for item in items:
        print(f"  {item.id}")

    return items


def search_sentinel2_with_catalog(
    start_date,
    end_date,
    bbox,
    cloud_cover=50,
    stac_api_url="https://earth-search.aws.element84.com/v1",
    collection_id="sentinel-2-c1-l2a",
):
    """
    Search Sentinel-2 and return both the catalog search result and items.

    This is useful when the caller needs item_collection_as_dict()
    for GeoDataFrame processing or visualization.

    Returns
    -------
    search
        STAC search object.

    items : list
        List of STAC Items.

    stac_json : dict
        STAC ItemCollection as a dictionary.
    """

    catalog = connect_to_stac(stac_api_url)

    query = {
        "eo:cloud_cover": {
            "lt": cloud_cover
        }
    }

    search = catalog.search(
        collections=[collection_id],
        datetime=[start_date, end_date],
        bbox=bbox,
        query=query,
    )

    items = list(search.items())
    stac_json = search.item_collection_as_dict()

    print(f"Found {len(items)} Sentinel-2 items.")

    for item in items:
        print(f"  {item.id}")

    return search, items, stac_json


# ---------------------------------------------------------------------
# STAC asset handling
# ---------------------------------------------------------------------

def get_asset_metadata(asset):
    """
    Extract useful metadata from a STAC asset.

    Parameters
    ----------
    asset : pystac.Asset or dict
        STAC asset.

    Returns
    -------
    tuple
        href, scale, offset, gsd
    """

    if hasattr(asset, "href"):
        # PySTAC Asset
        href = asset.href
        band_metadata = asset.extra_fields.get("raster:bands", [{}])[0]
        gsd = asset.extra_fields.get("gsd")
    else:
        # Dictionary
        href = asset.get("href")
        band_metadata = asset.get("raster:bands", [{}])[0]
        gsd = asset.get("gsd")

    scale = band_metadata.get("scale")
    offset = band_metadata.get("offset")

    return href, scale, offset, gsd


def get_item_assets(item):
    """
    Return the assets dictionary from a STAC Item.

    Parameters
    ----------
    item : pystac.Item
        Sentinel-2 STAC item.

    Returns
    -------
    dict
        Asset dictionary.
    """

    return item.assets


# ---------------------------------------------------------------------
# Output naming convention
# ---------------------------------------------------------------------

def _to_camel_case(snake_str):
    """
    Convert a snake_case algorithm name (e.g. 'true_color') to
    camelCase (e.g. 'trueColor') for use in output filenames.
    """
    parts = snake_str.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def _get_sat_level_tile(item):
    """
    Parse SAT, LEVEL, and TILE tokens from a Sentinel-2 STAC item id.

    Handles both Earth Search item-id formats currently in use across
    collections:
        Older style (sentinel-2-l1c):
            SAT_TILE_DATE_SEQ_LEVELTOKEN
            e.g. "S2A_16SED_20260423_1_L1C"  (tile has no leading "T")
        Collection 1 style (sentinel-2-c1-l2a):
            SAT_TTILE_DATETIME_LEVELTOKEN
            e.g. "S2A_T16SED_20260423T163857_L2A"  (tile already has "T")

    Only relies on the first token (SAT) and the last token (LEVEL),
    which are stable across both formats -- everything in between
    (date, optional sequence number) is ignored, so this doesn't
    depend on a fixed total token count.

    Returns
    -------
    sat : str
        e.g. "S2A"
    level : str
        e.g. "MSIL1C" or "MSIL2A"
    tile : str
        e.g. "T16SED"
    """
    parts = item.id.split("_")
    if len(parts) < 4:
        raise ValueError(
            f"Unexpected Sentinel-2 item id format: '{item.id}'"
        )

    sat = parts[0]
    level = f"MSI{parts[-1]}"

    tile_raw = parts[1]
    tile = tile_raw if tile_raw.startswith("T") else f"T{tile_raw}"

    return sat, level, tile


def _build_output_filename(item, algorithm_name, masked=False, merged=False):
    """
    Build a Sentinel-2 output filename matching the project naming
    convention:

        Plain:              SAT_LEVEL_product_TILE_TIMESTAMP.tif
        Masked:              SAT_LEVEL_product_TILE_masked_TIMESTAMP.tif
        Merged:              SAT_LEVEL_product_merged_TIMESTAMP.tif
        Merged and masked:   SAT_LEVEL_product_merged_masked_TIMESTAMP.tif

    Parameters
    ----------
    item : pystac.Item
        The STAC item supplying SAT/LEVEL/timestamp. When merged=True,
        pass the first item (by sort order) among the tiles being
        merged.

    algorithm_name : str
        Algorithm/product name as used in the algorithm catalog, e.g.
        "true_color", "ndvi". Converted to camelCase for the filename.

    masked : bool
        Whether cloud masking was applied to this output.

    merged : bool
        Whether this output is a mosaic of multiple tiles. When True,
        the tile token is omitted (a mosaic spans multiple tiles, so
        no single TILE token applies).

    Returns
    -------
    str
        Filename, e.g. "S2A_MSIL1C_trueColor_T16SED_2026-04-23T16:15:59Z.tif"
    """
    sat, level, tile = _get_sat_level_tile(item)
    product = _to_camel_case(algorithm_name)
    timestamp = item.datetime.replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    tile_token = "" if merged else f"_{tile}"
    merged_token = "_merged" if merged else ""
    masked_token = "_masked" if masked else ""

    return (
        f"{sat}_{level}_{product}{tile_token}"
        f"{merged_token}{masked_token}_{timestamp}.tif"
    )


# ---------------------------------------------------------------------
# Raster processing
# ---------------------------------------------------------------------

def resample_array(
    src,
    resample_ratio,
    resampling=Resampling.bilinear,
):
    """
    Resample a raster band.

    This is based on the resampling logic used in the notebooks.

    Parameters
    ----------
    src : rasterio DatasetReader
        Open raster dataset.

    resample_ratio : float
        Ratio between the source GSD and requested GSD.

    resampling : rasterio.enums.Resampling
        Resampling method.

    Returns
    -------
    band : numpy.ndarray
        Resampled raster.

    profile : dict
        Updated rasterio profile.
    """

    if resample_ratio <= 0:
        raise ValueError(
            f"resample_ratio must be greater than zero. "
            f"Received {resample_ratio}."
        )

    profile = src.profile.copy()

    width = int(profile["width"] / resample_ratio)
    height = int(profile["height"] / resample_ratio)

    band = src.read(
        1,
        out_shape=(height, width),
        resampling=resampling,
    )

    dst_transform = (
        src.transform
        * rio.Affine.scale(
            resample_ratio,
            resample_ratio,
        )
    )

    profile.update(
        {
            "height": height,
            "width": width,
            "transform": dst_transform,
        }
    )

    return band, profile


def read_algorithm_band(
    asset,
    target_gsd=None,
    resample=True,
    apply_scale=True,
):
    href, scale, offset, gsd = get_asset_metadata(asset)

    if scale is None:
        scale = 1.0
    if offset is None:
        offset = 0.0
    if gsd is None:
        raise ValueError(f"STAC asset does not contain a GSD: {href}")

    # Sentinel-2 L1C assets are publicly accessible from the
    # sentinel-s2-l1c bucket. Use anonymous access so the
    # disasters-prod IAM role is not used for these reads.
    if href.startswith("s3://sentinel-s2-l1c/"):
        aws_session = _aws_unsigned_session
        print("Using anonymous S3 access for Sentinel-2 L1C asset.")
    else:
        aws_session = _aws_session

    with rio.Env(aws_session):
        print("Trying to open:", href)

        with rio.open(href) as src:

            if (
                resample
                and target_gsd is not None
                and target_gsd / gsd > 1
            ):
                resample_ratio = target_gsd / gsd

                print(
                    f"Resampling band to {target_gsd} meters "
                    f"with a resampling ratio of {resample_ratio}"
                )

                band, profile = resample_array(
                    src,
                    resample_ratio,
                )

            else:
                band = src.read(1)
                profile = src.profile.copy()

    if apply_scale:
        band = band * scale - offset

    return band, profile


def get_cloud_mask(item, out_shape, cloud_classes=(3, 8, 9, 10)):
    """
    Build a boolean cloud/cloud-shadow mask from the Sentinel-2 L2A Scene
    Classification Layer (SCL) asset, resampled with nearest-neighbor
    (SCL is categorical) to match out_shape.

    cloud_classes defaults match the legacy SAFE-based pipeline's SCL
    bin map: 3 = cloud shadow, 8 = cloud medium probability,
    9 = cloud high probability, 10 = thin cirrus.

    Parameters
    ----------
    item : pystac.Item
        Sentinel-2 L2A STAC item. Raises if no 'scl' asset is present
        (i.e. this is an L1C item).

    out_shape : tuple
        (height, width) to resample the mask to, matching the array
        it will be applied against.

    Returns
    -------
    numpy.ndarray (bool)
        True where the pixel is cloud, cloud shadow, or thin cirrus.
    """

    if "scl" not in item.assets:
        raise KeyError(
            f"Asset 'scl' not found in STAC item '{item.id}'. "
            "Cloud masking requires the Sentinel-2 L2A Scene "
            "Classification Layer, which does not exist for L1C products."
        )

    href, _, _, _ = get_asset_metadata(item.assets["scl"])

    print(f"Building cloud mask from SCL asset: {href}")

    with rio.open(href) as src:
        scl = src.read(
            1,
            out_shape=out_shape,
            resampling=Resampling.nearest,
        )

    return np.isin(scl, cloud_classes)


# ---------------------------------------------------------------------
# Index processing
# ---------------------------------------------------------------------

def calculate_normalized_difference(
    band1,
    band2,
    minimum=-1.0,
    maximum=1.0,
    nodata=-9999,
):
    """
    Calculate a normalized-difference index.

    The calculation follows the implementation in the Sentinel-2
    index notebook:

        (band1 - band2) / (band1 + band2)

    Values outside the configured range are assigned nodata.

    Parameters
    ----------
    band1 : numpy.ndarray
        First input band.

    band2 : numpy.ndarray
        Second input band.

    minimum : float
        Minimum valid index value.

    maximum : float
        Maximum valid index value.

    nodata : float
        Output nodata value.

    Returns
    -------
    numpy.ndarray
        Calculated index.
    """

    band1 = np.asarray(band1)
    band2 = np.asarray(band2)

    denominator = band1 + band2

    array = np.full(
        band1.shape,
        nodata,
        dtype=np.float32,
    )

    valid = (
        np.isfinite(band1)
        & np.isfinite(band2)
        & (denominator != 0)
    )

    array[valid] = (
        (band1[valid] - band2[valid])
        / denominator[valid]
    )

    invalid_range = (
        (array > maximum)
        | (array < minimum)
    )

    array[invalid_range] = nodata

    return array


def generate_index(
    item,
    algorithm,
    algorithm_name,
    output_dir="./s3_temp",
    nodata=999,
    cloud_mask=False,
):
    """
    Generate a Sentinel-2 index and save it as a GeoTIFF.

    Parameters
    ----------
    item : pystac.Item
        Sentinel-2 STAC item.

    algorithm : dict
        Algorithm configuration (assets, min/max, etc.).

    algorithm_name : str
        Algorithm/product name as used in the algorithm catalog (e.g.
        "ndvi"), used to build the output filename.

    output_dir : str
        Local directory to write the output GeoTIFF.

    nodata : float
        Output nodata value.

    cloud_mask : bool
        If True, mask cloud, cloud-shadow, and thin-cirrus pixels using
        the Sentinel-2 L2A Scene Classification Layer (SCL), setting
        them to `nodata`. Only valid for L2A items -- SCL does not
        exist for L1C products (see get_cloud_mask).

    Returns
    -------
    str
        Path to the generated GeoTIFF.
    """

    os.makedirs(output_dir, exist_ok=True)

    algorithm_assets = algorithm["assets"]

    if len(algorithm_assets) != 2:
        raise ValueError(
            "Normalized-difference indices require exactly two assets."
        )

    bands = []

    for band_name in algorithm_assets:

        if band_name not in item.assets:
            raise KeyError(
                f"Asset '{band_name}' not found in STAC item "
                f"'{item.id}'."
            )

        band, profile = read_algorithm_band(
            item.assets[band_name],
            target_gsd=algorithm.get("gsd"),
            resample=algorithm.get("resample", False),
            apply_scale=True,
        )

        bands.append(band)

    print("Calculating normalized-difference index...")

    denominator = bands[0] + bands[1]

    index = np.full(
        bands[0].shape,
        nodata,
        dtype=np.float32,
    )

    valid = (
        np.isfinite(bands[0])
        & np.isfinite(bands[1])
        & (denominator != 0)
    )

    index[valid] = (
        (bands[0][valid] - bands[1][valid])
        / denominator[valid]
    )

    minimum = algorithm.get("min", -1.0)
    maximum = algorithm.get("max", 1.0)

    invalid = (
        (index < minimum)
        | (index > maximum)
    )

    index[invalid] = nodata

    if cloud_mask:
        print("Applying cloud mask...")
        mask = get_cloud_mask(item, out_shape=index.shape)
        index[mask] = nodata

    profile = profile.copy()

    profile.update(
        driver="GTiff",
        count=1,
        dtype="float32",
        nodata=nodata,
    )

    output_name = _build_output_filename(
        item, algorithm_name, masked=cloud_mask
    )
    outfile = os.path.join(output_dir, output_name)

    print(f"Writing GeoTIFF: {outfile}")

    with rio.open(outfile, "w", **profile) as dst:
        dst.write(index, 1)

    print(f"Generation completed: {outfile}")

    return outfile


# ---------------------------------------------------------------------
# Composite processing
# ---------------------------------------------------------------------

def apply_log_scale(
    array,
    low=750,
    high=7500,
    output_min=0,
    output_max=255,
):
    """
    Apply the logarithmic display scaling used by the composite notebook.

    The notebook uses log(750) and log(7500) as the low/high thresholds
    and maps the values to 0-255.

    Parameters
    ----------
    array : numpy.ndarray
        Input image.

    low : float
        Lower logarithmic threshold.

    high : float
        Upper logarithmic threshold.

    output_min : float
        Minimum output value.

    output_max : float
        Maximum output value.

    Returns
    -------
    numpy.ndarray
        Log-scaled image.
    """

    array = np.asarray(array)

    # Avoid warnings from log(0) and log(negative values).
    safe_array = np.ma.masked_where(
        array <= 0,
        array,
    )

    rescaled = np.ma.log(
        safe_array
    )

    low_log = math.log(low)
    high_log = math.log(high)

    diff = high_log - low_log

    rescaled[
        np.where(rescaled <= low_log)
    ] = output_min

    rescaled[
        np.where(rescaled >= high_log)
    ] = output_max

    indices = np.where(
        (rescaled > low_log)
        & (rescaled < high_log)
    )

    rescaled[indices] = (
        output_max
        * (rescaled[indices] - low_log)
        / diff
    )

    return rescaled


def generate_composite(
    item,
    algorithm,
    algorithm_name,
    output_dir="./s3_temp",
    cloud_mask=False,
):
    """
    Generate a Sentinel-2 composite and save it as a GeoTIFF.

    Parameters
    ----------
    item : pystac.Item
        Sentinel-2 STAC item.

    algorithm : dict
        Algorithm configuration (assets, gsd, resample, etc.).

    algorithm_name : str
        Algorithm/product name as used in the algorithm catalog (e.g.
        "true_color"), used to build the output filename.

    output_dir : str
        Local directory to write the output GeoTIFF.

    cloud_mask : bool
        If True, mask cloud, cloud-shadow, and thin-cirrus pixels using
        the Sentinel-2 L2A Scene Classification Layer (SCL), zeroing
        them out in each source band before log scaling (they fall out
        as nodata=0 in the output, same as true nodata/edge pixels).
        Only valid for L2A items -- SCL does not exist for L1C products
        (see get_cloud_mask).

    Returns
    -------
    str
        Path to the generated GeoTIFF.
    """

    os.makedirs(output_dir, exist_ok=True)

    algorithm_assets = algorithm["assets"]

    bands = []

    for band_name in algorithm_assets:

        if band_name not in item.assets:
            raise KeyError(
                f"Asset '{band_name}' not found in STAC item "
                f"'{item.id}'."
            )

        band, profile = read_algorithm_band(
            item.assets[band_name],
            target_gsd=algorithm.get("gsd"),
            resample=algorithm.get("resample", False),
            apply_scale=False,
        )

        bands.append(band)

    if cloud_mask:
        print("Applying cloud mask...")
        mask = get_cloud_mask(item, out_shape=bands[0].shape)
        for band in bands:
            band[mask] = 0

    print("Applying logarithmic display scaling...")

    rgb = np.asarray(bands)

    array = apply_log_scale(rgb)

    array = np.asarray(array, dtype=np.uint8)

    profile = profile.copy()

    profile.update(
        count=array.shape[0],
        dtype="uint8",
        nodata=0,
    )

    output_name = _build_output_filename(
        item, algorithm_name, masked=cloud_mask
    )
    outfile = os.path.join(output_dir, output_name)

    print(f"Writing GeoTIFF: {outfile}")

    with rio.open(outfile, "w", **profile) as dst:
        dst.write(array)

    print(f"Generation completed: {outfile}")

    return outfile


# ---------------------------------------------------------------------
# Merge processing
# ---------------------------------------------------------------------

def merge_products(tif_paths, output_path, method="first"):
    """
    Mosaic a list of local single-product GeoTIFFs (e.g. one true_color
    composite or one NDVI index per Sentinel-2 tile) into a single output
    GeoTIFF.

    Adjacent Sentinel-2 tiles can fall in different UTM zones, so any
    input not matching the dominant CRS is reprojected to a temp file
    before merging.

    Parameters
    ----------
    tif_paths : list of str
        Local paths to per-tile product GeoTIFFs to merge.

    output_path : str
        Path to write the merged GeoTIFF.

    method : str
        rasterio.merge method. "first" means the first-listed tile's
        valid (non-nodata) pixels win; nodata pixels (e.g. masked
        clouds) are filled by later tiles where available.

    Returns
    -------
    str
        output_path
    """

    if len(tif_paths) == 1:
        shutil.copy(tif_paths[0], output_path)
        return output_path

    crs_list = [rio.open(p).crs for p in tif_paths]
    crs_list_unique = list(set(crs_list))

    merge_inputs = list(tif_paths)
    tmp_dir = None
    crs_dom = crs_list_unique[0]

    try:
        if len(crs_list_unique) != 1:
            crs_dom = max(crs_list, key=crs_list.count)
            tmp_dir = tempfile.mkdtemp(prefix="s2_stac_merge_reproj_")

            for idx, tif in enumerate(tif_paths):
                with rio.open(tif) as src:
                    if src.crs == crs_dom:
                        continue

                    transform, width, height = calculate_default_transform(
                        src.crs, crs_dom, src.width, src.height, *src.bounds
                    )
                    kwargs = src.meta.copy()
                    kwargs.update(
                        {
                            "crs": crs_dom,
                            "transform": transform,
                            "width": width,
                            "height": height,
                        }
                    )

                    reproj_path = os.path.join(
                        tmp_dir, f"{idx}_{os.path.basename(tif)}"
                    )

                    with rio.open(reproj_path, "w", **kwargs) as dst:
                        for i in range(1, src.count + 1):
                            reproject(
                                source=rio.band(src, i),
                                destination=rio.band(dst, i),
                                src_transform=src.transform,
                                src_crs=src.crs,
                                dst_transform=transform,
                                dst_crs=crs_dom,
                            )

                    merge_inputs[idx] = reproj_path

        with rio.open(merge_inputs[0]) as ref:
            nodata_val = ref.nodata
            count = ref.count
            dtype = ref.dtypes[0]

        array, transform = merge(merge_inputs, method=method)

        profile = {
            "driver": "GTiff",
            "count": count,
            "dtype": dtype,
            "height": array.shape[1],
            "width": array.shape[2],
            "nodata": nodata_val,
            "crs": crs_dom,
            "transform": transform,
        }

        with rio.open(output_path, "w", **profile) as dst:
            dst.write(array)

    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return output_path


# ---------------------------------------------------------------------
# High-level processing functions
# ---------------------------------------------------------------------

def process_index(
    item,
    algorithm,
    algorithm_name,
    bucket,
    key,
    tmpfile,
    nodata=-9999,
):
    """
    Generate an index and write it directly to S3 as a COG.

    Returns
    -------
    str
        S3 URI of the generated COG.
    """

    array, profile = generate_index(
        item,
        algorithm,
        algorithm_name,
        nodata=nodata,
    )

    return write_cog_to_s3(
        array,
        profile,
        bucket,
        key,
        tmpfile,
    )


def process_composite(
    item,
    algorithm,
    algorithm_name,
    bucket,
    key,
    tmpfile,
):
    """
    Generate a composite and write it directly to S3 as a COG.

    Returns
    -------
    str
        S3 URI of the generated COG.
    """

    array, profile = generate_composite(
        item,
        algorithm,
        algorithm_name,
    )

    return write_cog_to_s3(
        array,
        profile,
        bucket,
        key,
        tmpfile,
    )


def process_items(
    items,
    algorithm,
    algorithm_name,
    algorithm_type,
    bucket,
    output_prefix="test-cog-write",
    tmp_dir=".",
    nodata=-9999,
    cloud_mask=False,
    merge=False,
    merge_method="first",
):
    """
    Process every Sentinel-2 item returned from a STAC search.

    Parameters
    ----------
    items : list
        STAC Items.

    algorithm : dict
        Algorithm configuration.

    algorithm_name : str
        Algorithm/product name as used in the algorithm catalog (e.g.
        "true_color", "ndvi"), used to build output filenames.

    algorithm_type : str
        Either "index" or "composite".

    bucket : str
        Destination S3 bucket.

    output_prefix : str
        S3 output prefix.

    tmp_dir : str
        Local directory used for temporary GeoTIFF/COG files.

    nodata : float
        Index nodata value. Ignored for composites.

    cloud_mask : bool
        If True, mask cloud/cloud-shadow/thin-cirrus pixels using the
        Sentinel-2 L2A SCL asset on each tile before COGing (and before
        merging, if merge=True). Only valid for L2A items.

    merge : bool
        If True, generate every item locally first, mosaic them into a
        single product, and COG/upload once. If False (default), each
        item is COG'd/uploaded individually, as before.

    merge_method : str
        rasterio.merge method used when merge=True. Default "first"
        means an earlier tile's valid pixels win, and nodata gaps
        (e.g. from cloud masking) are filled by later tiles.

    Returns
    -------
    list
        S3 URIs of generated products.
    """

    if algorithm_type not in (
        "index",
        "composite",
    ):
        raise ValueError(
            "algorithm_type must be either "
            "'index' or 'composite'."
        )

    os.makedirs(
        tmp_dir,
        exist_ok=True,
    )

    # -------------------------------------------------------------
    # Phase 1: generate each tile's product locally
    # -------------------------------------------------------------

    local_paths = []

    for item in items:

        print()
        print("=" * 70)
        print(f"Generating tile: {item.id}")
        print("=" * 70)

        if algorithm_type == "index":

            local_path = generate_index(
                item,
                algorithm,
                algorithm_name,
                output_dir=tmp_dir,
                nodata=nodata,
                cloud_mask=cloud_mask,
            )

        else:

            local_path = generate_composite(
                item,
                algorithm,
                algorithm_name,
                output_dir=tmp_dir,
                cloud_mask=cloud_mask,
            )

        local_paths.append(local_path)

    if not merge:
        # -----------------------------------------------------------
        # Original behavior: one COG + upload per tile
        # -----------------------------------------------------------
        outputs = []

        for item, local_path in zip(items, local_paths):

            key = f"{output_prefix}/{os.path.basename(local_path)}"
            tmpfile = local_path.replace(".tif", "_cog.tif")

            with rio.open(local_path) as src:
                array = src.read()
                profile = src.profile

            outputs.append(
                write_cog_to_s3(array, profile, bucket, key, tmpfile)
            )

            os.remove(local_path)

        return outputs

    # -----------------------------------------------------------------
    # merge=True: mosaic all tiles, COG + upload once
    # -----------------------------------------------------------------

    print()
    print("=" * 70)
    print(f"Merging {len(local_paths)} tile(s)")
    print("=" * 70)

    # Sort items/paths together for a deterministic merge order and a
    # deterministic choice of "first" item for the merged filename.
    items_sorted, local_paths_sorted = zip(
        *sorted(zip(items, local_paths), key=lambda p: p[0].id)
    )

    merged_name = _build_output_filename(
        items_sorted[0], algorithm_name, masked=cloud_mask, merged=True
    )
    merged_path = os.path.join(tmp_dir, merged_name)

    merge_products(list(local_paths_sorted), merged_path, method=merge_method)

    for p in local_paths_sorted:
        os.remove(p)

    with rio.open(merged_path) as src:
        array = src.read()
        profile = src.profile

    key = f"{output_prefix}/{merged_name}"
    tmpfile = merged_path.replace(".tif", "_cog.tif")

    output = write_cog_to_s3(array, profile, bucket, key, tmpfile)

    os.remove(merged_path)

    return [output]