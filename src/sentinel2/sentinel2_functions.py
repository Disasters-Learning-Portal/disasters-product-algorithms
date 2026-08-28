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

import glob
import time
import requests
import geopandas as gpd
import shutil
import zipfile

import boto3
from botocore import UNSIGNED
from botocore.client import Config
import numpy as np
import rasterio as rio

from scipy.signal import medfilt2d
from shapely.geometry import box
from pystac_client import Client
from rio_cogeo.cogeo import cog_translate
from rasterio.enums import Resampling
from rasterio.merge import merge
from rasterio.warp import calculate_default_transform, reproject
from rasterio.session import AWSSession

import logging
logging.getLogger("pyspectral.rsr_reader").setLevel(logging.ERROR)

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

# Cached Rayleigh correction objects, keyed by platform name. Rayleigh()
# initialization reads (and may fetch/verify) LUT and RSR data from disk,
# so building one per platform once -- rather than once per band per item
# -- avoids repeated redundant I/O across a run with many scenes.
_rayleigh_instances = {}


def _get_rayleigh_instance(platform_name):
    if platform_name not in _rayleigh_instances:
        from pyspectral.rayleigh import Rayleigh
        _rayleigh_instances[platform_name] = Rayleigh(platform_name, "msi")
    return _rayleigh_instances[platform_name]

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
# Water Extent Reference Data
# ---------------------------------------------------------------------

def _match_raster_to_reference(
    source_path,
    reference_profile,
    output_path,
    dst_nodata,
):
    """
    Reproject and resample a raster so that it exactly matches
    the reference raster's CRS, transform, width, and height.

    This replaces the old workflow's match_geotiff() function.
    """

    with rio.open(source_path) as src:

        profile = reference_profile.copy()

        profile.update(
            {
                "driver": "GTiff",
                "count": 1,
                "dtype": "uint16",
                "nodata": dst_nodata,
                "width": reference_profile["width"],
                "height": reference_profile["height"],
                "crs": reference_profile["crs"],
                "transform": reference_profile["transform"],
                "compress": "ZSTD",
            }
        )

        with rio.open(output_path, "w", **profile) as dst:

            reproject(
                source=rio.band(src, 1),
                destination=rio.band(dst, 1),
                src_transform=src.transform,
                src_crs=src.crs,
                src_nodata=src.nodata,
                dst_transform=reference_profile["transform"],
                dst_crs=reference_profile["crs"],
                dst_nodata=dst_nodata,
                resampling=Resampling.nearest,
            )

    return output_path



def _download_cdl_for_water_extent(
    nir_profile,
    year,
    outname,
    reference_dir,
):
    """
    Download the USDA NASS CDL, extract only the portion needed for
    the Sentinel-2 scene, and resample it to the B08 grid.

    All CDL intermediate files are temporary and are removed after
    the requested Sentinel-2-matched CDL is created.
    """

    from rasterio.windows import from_bounds
    from rasterio.warp import transform_bounds

    # ------------------------------------------------------------
    # CDL year
    # ------------------------------------------------------------

    year = min(int(year), 2024)

    if year < 2008:
        raise ValueError(
            f"CDL year {year} is not supported."
        )

    os.makedirs(
        reference_dir,
        exist_ok=True,
    )

    # ------------------------------------------------------------
    # Temporary paths
    #
    # These are NOT intended to remain after processing.
    # ------------------------------------------------------------

    zip_path = os.path.join(
        reference_dir,
        f"_CDL_{year}_30m_download.zip",
    )

    national_cdl_path = os.path.join(
        reference_dir,
        f"_CDL_{year}_30m.tif",
    )

    subset_path = os.path.join(
        reference_dir,
        f"_CDL_{year}_subset.tif",
    )

    try:

        # --------------------------------------------------------
        # Download national CDL
        # --------------------------------------------------------

        if not os.path.exists(zip_path):

            print()
            print(
                f"Downloading national {year} 30-m CDL..."
            )
            print(
                "This is approximately 1.6 GB for the 2024 CDL."
            )

            cdl_url = (
                "https://www.nass.usda.gov/"
                "Research_and_Science/Cropland/Release/datasets/"
                f"{year}_30m_cdls.zip"
            )

            print(
                f"CDL URL: {cdl_url}"
            )

            try:

                with requests.get(
                    cdl_url,
                    stream=True,
                    timeout=(30, 1800),
                ) as response:

                    response.raise_for_status()

                    with open(
                        zip_path,
                        "wb",
                    ) as f:

                        for chunk in response.iter_content(
                            chunk_size=1024 * 1024
                        ):

                            if chunk:
                                f.write(chunk)

                print(
                    "CDL download complete."
                )

            except requests.exceptions.RequestException as e:

                if os.path.exists(zip_path):
                    os.remove(zip_path)

                raise RuntimeError(
                    f"Failed to download the {year} CDL: {e}"
                ) from e

        # --------------------------------------------------------
        # Extract national CDL TIFF
        # --------------------------------------------------------

        print(
            "Extracting CDL TIFF..."
        )

        with zipfile.ZipFile(
            zip_path,
            "r",
        ) as z:

            tif_members = [
                name
                for name in z.namelist()
                if name.lower().endswith(".tif")
            ]

            if len(tif_members) == 0:
                raise RuntimeError(
                    f"No TIFF found inside {zip_path}"
                )

            tif_member = tif_members[0]

            with z.open(tif_member) as src_file:

                with open(
                    national_cdl_path,
                    "wb",
                ) as dst_file:

                    shutil.copyfileobj(
                        src_file,
                        dst_file,
                    )

        print(
            "CDL TIFF extracted."
        )

        # --------------------------------------------------------
        # Determine Sentinel-2 bounds
        # --------------------------------------------------------

        src_crs = nir_profile["crs"]
        transform = nir_profile["transform"]
        width = nir_profile["width"]
        height = nir_profile["height"]

        left = transform.c
        top = transform.f

        right = (
            left
            + width * transform.a
        )

        bottom = (
            top
            + height * transform.e
        )

        # --------------------------------------------------------
        # Transform scene bounds to CDL CRS
        # --------------------------------------------------------

        cdl_crs = "EPSG:5070"

        cdl_left, cdl_bottom, cdl_right, cdl_top = (
            transform_bounds(
                src_crs,
                cdl_crs,
                left,
                bottom,
                right,
                top,
            )
        )

        # --------------------------------------------------------
        # Extract only required CDL subset
        # --------------------------------------------------------

        print(
            "Extracting CDL subset for Sentinel-2 scene..."
        )

        with rio.open(
            national_cdl_path
        ) as src:

            window = from_bounds(
                cdl_left,
                cdl_bottom,
                cdl_right,
                cdl_top,
                transform=src.transform,
            )

            full_window = rio.windows.Window(
                0,
                0,
                src.width,
                src.height,
            )

            window = window.intersection(
                full_window
            )

            if (
                window.width <= 0
                or window.height <= 0
            ):
                raise RuntimeError(
                    "Sentinel-2 scene does not overlap "
                    "the national CDL."
                )

            cdl_array = src.read(
                1,
                window=window,
            )

            subset_transform = src.window_transform(
                window
            )

            subset_profile = src.profile.copy()

            subset_profile.update(
                {
                    "driver": "GTiff",
                    "height": cdl_array.shape[0],
                    "width": cdl_array.shape[1],
                    "count": 1,
                    "transform": subset_transform,
                    "crs": src.crs,
                    "compress": "ZSTD",
                }
            )

            with rio.open(
                subset_path,
                "w",
                **subset_profile,
            ) as dst:

                dst.write(
                    cdl_array,
                    1,
                )

        # --------------------------------------------------------
        # Match subset to Sentinel-2 B08
        # --------------------------------------------------------

        print(
            "Resampling CDL to match Sentinel-2 B08..."
        )

        _match_raster_to_reference(
            subset_path,
            nir_profile,
            outname,
            dst_nodata=0,
        )

        print(
            f"CDL reference created: {outname}"
        )

        return outname

    finally:

        # --------------------------------------------------------
        # Remove ALL temporary CDL files
        # --------------------------------------------------------

        for temporary_path in (
            zip_path,
            national_cdl_path,
            subset_path,
        ):

            if os.path.exists(temporary_path):

                try:
                    os.remove(
                        temporary_path
                    )
                except OSError:
                    pass


def _download_worldcover_for_water_extent(
    nir_profile,
    year,
    output_path,
):
    """
    Download ESA WorldCover tiles intersecting the Sentinel-2 scene
    and match the result to the Sentinel-2 B08 grid.

    This follows the legacy WorldCover workflow.
    """

    if year not in ("2020", "2021"):
        year = "2021"

    # -------------------------------------------------------------
    # WorldCover tile grid
    # -------------------------------------------------------------

    grid_url = (
        "https://esa-worldcover.s3.eu-central-1.amazonaws.com/"
        "v100/2020/esa_worldcover_2020_grid.geojson"
    )

    print("Loading WorldCover tile grid...")

    grid = gpd.read_file(grid_url)

    # -------------------------------------------------------------
    # Get Sentinel-2 image bounds in WGS84
    # -------------------------------------------------------------

    src_crs = nir_profile["crs"]
    transform = nir_profile["transform"]
    width = nir_profile["width"]
    height = nir_profile["height"]

    left = transform.c
    top = transform.f
    right = left + width * transform.a
    bottom = top + height * transform.e

    from rasterio.warp import transform_bounds

    xmin, ymin, xmax, ymax = transform_bounds(
        src_crs,
        "EPSG:4326",
        left,
        bottom,
        right,
        top,
    )

    geom = box(
        xmin,
        ymin,
        xmax,
        ymax,
    )

    tiles = grid[
        grid.intersects(geom)
    ]

    if len(tiles) == 0:
        raise RuntimeError(
            "No WorldCover tiles intersect the Sentinel-2 scene."
        )

    print(
        f"Found {len(tiles)} WorldCover tile(s)."
    )

    versions = {
        "2020": "v100",
        "2021": "v200",
    }

    version = versions[year]

    # -------------------------------------------------------------
    # Download tiles
    # -------------------------------------------------------------

    s3 = boto3.client(
        "s3",
        config=Config(
            signature_version=UNSIGNED
        ),
    )

    downloaded_tiles = []

    bucket = "esa-worldcover"
    bucket_dir = f"{version}/{year}/map/"

    worldcover_dir = os.path.dirname(
        output_path
    )

    os.makedirs(
        worldcover_dir,
        exist_ok=True,
    )

    for tile in tiles.ll_tile:

        filename = (
            f"ESA_WorldCover_10m_{year}_"
            f"{version}_{tile}_Map.tif"
        )

        tile_path = os.path.join(
            worldcover_dir,
            filename,
        )

        if not os.path.exists(tile_path):

            print(
                f"Downloading WorldCover tile: {tile}"
            )

            s3.download_file(
                bucket,
                bucket_dir + filename,
                tile_path,
            )

        else:

            print(
                f"WorldCover tile already exists: {tile}"
            )

        downloaded_tiles.append(
            tile_path
        )

    # -------------------------------------------------------------
    # Merge WorldCover tiles if necessary
    # -------------------------------------------------------------

    if len(downloaded_tiles) == 1:

        merged_worldcover = downloaded_tiles[0]

    else:

        merged_worldcover = output_path.replace(
            ".tif",
            "_merged_raw.tif",
        )

        print(
            "Merging WorldCover tiles..."
        )

        arrays, merge_transform = merge(
            downloaded_tiles,
        )

        with rio.open(
            downloaded_tiles[0]
        ) as src:

            profile = src.profile.copy()

        profile.update(
            {
                "driver": "GTiff",
                "height": arrays.shape[1],
                "width": arrays.shape[2],
                "transform": merge_transform,
                "count": 1,
                "dtype": arrays.dtype,
            }
        )

        with rio.open(
            merged_worldcover,
            "w",
            **profile,
        ) as dst:

            dst.write(
                arrays[0],
                1,
            )

    # -------------------------------------------------------------
    # Match WorldCover to B08
    # -------------------------------------------------------------

    print(
        "Matching WorldCover to Sentinel-2 B08 grid..."
    )

    _match_raster_to_reference(
        merged_worldcover,
        nir_profile,
        output_path,
        dst_nodata=0,
    )

    # -------------------------------------------------------------
    # Cleanup downloaded tiles
    # -------------------------------------------------------------

    for tile_path in downloaded_tiles:

        if os.path.exists(tile_path):
            os.remove(tile_path)

    if (
        merged_worldcover != downloaded_tiles[0]
        and os.path.exists(merged_worldcover)
    ):
        os.remove(merged_worldcover)

    print(
        f"WorldCover created: {output_path}"
    )

    return output_path


def _reclass_cdl_array(
    cdl_array,
    nir_nodata_mask,
):
    """
    Reclassify CDL using the exact classes from the legacy
    water-extent workflow.

    Classes:
        1 = cropland/grassland
        2 = developed
        3 = other vegetation
        4 = permanent water
        999 = no data
    """

    codes_dict = {
        1: 1, 2: 1, 3: 1, 4: 1, 5: 1,
        6: 1, 10: 1, 11: 1, 12: 1,
        13: 1, 14: 1, 21: 1, 22: 1,
        23: 1, 24: 1, 25: 1, 26: 1,
        27: 1, 28: 1, 29: 1, 30: 1,
        31: 1, 32: 1, 33: 1, 34: 1,
        35: 1, 36: 1, 37: 1, 38: 1,
        39: 1, 41: 1, 42: 1, 43: 1,
        44: 1, 45: 1, 46: 1, 47: 1,
        48: 1, 49: 1, 50: 1, 51: 1,
        52: 1, 53: 1, 54: 1, 55: 1,
        56: 1, 57: 1, 58: 1, 59: 1,
        60: 1, 61: 1, 66: 1, 67: 1,
        68: 1, 69: 1, 70: 1, 71: 1,
        72: 1, 74: 1, 75: 1, 76: 1,
        77: 1, 204: 1, 205: 1,
        206: 1, 207: 1, 208: 1,
        209: 1, 210: 1, 211: 1,
        212: 1, 213: 1, 214: 1,
        215: 1, 216: 1, 217: 1,
        218: 1, 219: 1, 220: 1,
        221: 1, 222: 1, 223: 1,
        224: 1, 225: 1, 226: 1,
        227: 1, 228: 1, 229: 1,
        230: 1, 231: 1, 232: 1,
        233: 1, 234: 1, 235: 1,
        236: 1, 237: 1, 238: 1,
        239: 1, 240: 1, 241: 1,
        242: 1, 243: 1, 244: 1,
        245: 1, 246: 1, 247: 1,
        248: 1, 249: 1, 250: 1,
        254: 1, 176: 1,

        121: 2, 122: 2, 123: 2, 124: 2,

        131: 3, 141: 3, 142: 3,
        143: 3, 152: 3, 190: 3,
        195: 3,

        111: 4, 112: 4, 92: 4,

        0: 999,
        81: 999,
        88: 999,
    }

    translated = np.full(
        cdl_array.shape,
        999,
        dtype=np.uint16,
    )

    for source_code, target_code in codes_dict.items():

        translated[
            cdl_array == source_code
        ] = target_code

    translated[
        nir_nodata_mask
    ] = 999

    return translated


def _reclass_worldcover_array(
    worldcover_array,
    nir_nodata_mask,
):
    """
    Reclassify WorldCover using the exact classes from the legacy
    water-extent workflow.

    Classes:
        1 = cropland/grassland
        2 = developed
        3 = other vegetation
        4 = permanent water
        999 = no data
    """

    codes_dict = {
        0: 999,
        10: 3,
        20: 3,
        30: 1,
        40: 1,
        50: 2,
        60: 3,
        70: 999,
        80: 4,
        90: 3,
        95: 3,
        100: 3,
    }

    translated = np.full(
        worldcover_array.shape,
        999,
        dtype=np.uint16,
    )

    for source_code, target_code in codes_dict.items():

        translated[
            worldcover_array == source_code
        ] = target_code

    translated[
        nir_nodata_mask
    ] = 999

    return translated

# ---------------------------------------------------------------------
# Water Extent
# ---------------------------------------------------------------------

def generate_water_extent(
    item,
    algorithm,
    algorithm_name,
    output_dir="./s3_temp",
    cloud_mask=False,
):
    """
    Generate the Sentinel-2 Water Extent product using the same
    methodology as the legacy gen_water_extent() workflow.

    Algorithm:

        1. Read Sentinel-2 B08 at native resolution.
        2. Obtain CDL or WorldCover reference data.
        3. Reclassify reference data:
               1 = cropland/grassland
               2 = developed
               3 = vegetation
               4 = permanent water
             999 = no data
        4. Identify cloud-free permanent-water pixels.
        5. Calculate:
               threshold = mean(water NIR)
                         + nstd * std(water NIR)
        6. Classify pixels below threshold as water.
        7. Apply 5x5 median filtering.
        8. Classify:
               0 = no data
               1 = permanent water
               1 = flooded developed
               1 = flooded vegetation
               1 = flooded crop/grassland
               0 = cloud/cloud shadow
    """

    os.makedirs(
        output_dir,
        exist_ok=True,
    )

    # -------------------------------------------------------------
    # Get Sentinel-2 B08
    # -------------------------------------------------------------

    if "nir" not in item.assets:
        raise KeyError(
            f"Asset 'nir' not found in STAC item "
            f"'{item.id}'."
        )

    nir_asset = item.assets["nir"]

    href, scale, offset, gsd = get_asset_metadata(
        nir_asset
    )

    print(
        f"NIR asset: {href}"
    )

    print(
        "NIR extra_fields:"
    )

    if hasattr(nir_asset, "extra_fields"):
        print(
            nir_asset.extra_fields
        )

    print(
        "raster:bands:"
    )

    if hasattr(nir_asset, "extra_fields"):
        print(
            nir_asset.extra_fields.get(
                "raster:bands"
            )
        )

    # -------------------------------------------------------------
    # IMPORTANT:
    #
    # The legacy algorithm uses raw B08 values. Therefore:
    #
    #     apply_scale=False
    #
    # Do NOT convert the B08 values to reflectance here.
    # -------------------------------------------------------------

    nir_array, nir_profile = read_algorithm_band(
        nir_asset,
        target_gsd=algorithm.get(
            "gsd",
            10,
        ),
        resample=algorithm.get(
            "resample",
            False,
        ),
        apply_scale=False,
    )

    nir_array = np.asarray(
        nir_array
    )

    print()
    print("RAW NIR statistics:")
    print(
        f"  dtype: {nir_array.dtype}"
    )
    print(
        f"  min: {np.nanmin(nir_array)}"
    )
    print(
        f"  max: {np.nanmax(nir_array)}"
    )
    print(
        f"  mean: {np.nanmean(nir_array)}"
    )
    print(
        f"  median: {np.nanmedian(nir_array)}"
    )

    # -------------------------------------------------------------
    # NIR no-data
    #
    # Sentinel-2 B08 has nodata = 0.
    # -------------------------------------------------------------

    nir_nodata = 0

    nir_nd_mask = (
        nir_array == nir_nodata
    )

    print(
        f"  nodata pixels: "
        f"{np.count_nonzero(nir_nd_mask)}"
    )

    # -------------------------------------------------------------
    # Determine year
    # -------------------------------------------------------------

    year = item.datetime.year

    # -------------------------------------------------------------
    # Determine geographic bounds
    # -------------------------------------------------------------

    from rasterio.warp import transform_bounds

    bounds = nir_profile

    left = bounds["transform"].c
    top = bounds["transform"].f

    right = (
        left
        + bounds["width"]
        * bounds["transform"].a
    )

    bottom = (
        top
        + bounds["height"]
        * bounds["transform"].e
    )

    xmin_wgs84, ymin_wgs84, xmax_wgs84, ymax_wgs84 = (
        transform_bounds(
            bounds["crs"],
            "EPSG:4326",
            left,
            bottom,
            right,
            top,
        )
    )

    print()
    print("Scene bounds:")
    print(
        f"  WGS84: "
        f"{xmin_wgs84}, "
        f"{ymin_wgs84}, "
        f"{xmax_wgs84}, "
        f"{ymax_wgs84}"
    )

    # -------------------------------------------------------------
    # U.S. boundaries from legacy workflow
    # -------------------------------------------------------------

    xmax_us = -66.9513812
    xmin_us = -124.7844079
    ymax_us = 49.3457868
    ymin_us = 24.7433195

    inside_us = (
        (xmax_wgs84 < xmax_us)
        and
        (xmin_wgs84 > xmin_us)
        and
        (ymax_wgs84 < ymax_us)
        and
        (ymin_wgs84 > ymin_us)
    )

    # -------------------------------------------------------------
    # Reference data directory
    # -------------------------------------------------------------

    reference_dir = os.path.join(
        output_dir,
        "water_extent_reference",
    )

    os.makedirs(
        reference_dir,
        exist_ok=True,
    )

    # -------------------------------------------------------------
    # Get reference land-cover data
    # -------------------------------------------------------------

    if inside_us:

        cdl_year = min(
            year,
            2024,
        )
    
        # Temporary CDL matched to the Sentinel-2 B08 grid.
        # It is deleted immediately after reading.
        cdl_path = os.path.join(
            reference_dir,
            f"_CDL_{cdl_year}_matched.tif",
        )
    
        _download_cdl_for_water_extent(
            nir_profile,
            cdl_year,
            cdl_path,
            reference_dir,
        )
    
        print(
            f"Using CDL reference: {cdl_path}"
        )
    
        with rio.open(
            cdl_path
        ) as ref:
    
            ref_array = ref.read(1)
    
        ref_simple_array = _reclass_cdl_array(
            ref_array,
            nir_nd_mask,
        )
    
        # The matched CDL is no longer needed after it has
        # been converted to the simplified classification.
        if os.path.exists(cdl_path):
    
            os.remove(cdl_path)

    else:

        wc_year = 2021

        wc_path = os.path.join(
            reference_dir,
            f"WorldCover_{wc_year}_"
            f"{_get_sat_level_tile(item)[2]}.tif",
        )

        if not os.path.exists(wc_path):

            wc_raw_path = os.path.join(
                reference_dir,
                f"WorldCover_{wc_year}_"
                f"{_get_sat_level_tile(item)[2]}_raw.tif",
            )

            _download_worldcover_for_water_extent(
                nir_profile,
                str(wc_year),
                wc_raw_path,
            )

            if (
                os.path.exists(wc_raw_path)
                and wc_raw_path != wc_path
            ):
                os.rename(
                    wc_raw_path,
                    wc_path,
                )

        print(
            f"Using WorldCover reference: {wc_path}"
        )

        with rio.open(
            wc_path
        ) as ref:

            ref_array = ref.read(1)

        ref_simple_array = _reclass_worldcover_array(
            ref_array,
            nir_nd_mask,
        )

    # -------------------------------------------------------------
    # Cloud mask
    # -------------------------------------------------------------

    if cloud_mask:

        print(
            "Applying cloud mask..."
        )

        cloud_mask_array = get_cloud_mask(
            item,
            out_shape=nir_array.shape,
        )

    else:

        print(
            "Cloud mask not requested."
        )

        cloud_mask_array = np.zeros(
            nir_array.shape,
            dtype=bool,
        )

    # -------------------------------------------------------------
    # Identify permanent-water pixels
    #
    # Exact equivalent of:
    #
    # water = np.where(
    #     (ref_simple_array == 4)
    #     & (cloudMask == 0)
    #     & (nir_array != 0)
    # )
    # -------------------------------------------------------------

    permanent_water_mask = (
        (ref_simple_array == 4)
        &
        (~cloud_mask_array)
        &
        (~nir_nd_mask)
    )

    water_count = np.count_nonzero(
        permanent_water_mask
    )

    print()
    print(
        f"Permanent-water reference pixels: "
        f"{water_count:,}"
    )

    if water_count == 0:

        raise RuntimeError(
            "No valid permanent-water pixels were found "
            "after applying the land-cover and cloud masks. "
            "Cannot calculate the NIR water threshold."
        )

    # -------------------------------------------------------------
    # Calculate dynamic NIR threshold
    #
    # IMPORTANT:
    #
    # This is the actual logic from the old workflow.
    # -------------------------------------------------------------

    water_nir = nir_array[
        permanent_water_mask
    ]

    mean = np.nanmean(
        water_nir
    )

    std = np.nanstd(
        water_nir
    )

    # -------------------------------------------------------------
    # nstd comes from the algorithm configuration.
    #
    # If it isn't supplied, use 1.0.
    # -------------------------------------------------------------

    nstd = algorithm.get(
        "nstd",
        1.0,
    )

    nir_thresh = (
        mean
        + (
            nstd
            * std
        )
    )

    print()
    print("Water NIR statistics:")
    print(
        f"  mean: {mean}"
    )
    print(
        f"  std: {std}"
    )
    print(
        f"  nstd: {nstd}"
    )
    print(
        f"  NIR threshold: {nir_thresh}"
    )

    # -------------------------------------------------------------
    # Create binary water extent
    #
    # Exact equivalent of:
    #
    # water_extent = np.zeros(nir_array.shape)
    # water_extent[nir_array <= nir_thresh] = 1
    # -------------------------------------------------------------

    water_extent = np.zeros(
        nir_array.shape,
        dtype=np.uint8,
    )

    water_extent[
        nir_array <= nir_thresh
    ] = 1

    # -------------------------------------------------------------
    # Median filter
    #
    # Exact legacy operation:
    #
    # medfilt2d(water_extent, kernel_size=5)
    # -------------------------------------------------------------

    print(
        "Applying 5x5 median filter..."
    )

    water_extent = medfilt2d(
        water_extent,
        kernel_size=5,
    ).astype(
        np.uint8
    )

    # -------------------------------------------------------------
    # Final binary water mask
    #
    # 0 = not water
    # 1 = water
    # -------------------------------------------------------------
    
    classified_flood = np.zeros(
        water_extent.shape,
        dtype=np.uint8,
    )
    
    # Permanent water
    classified_flood[
        permanent_water_mask
    ] = 1
    
    # Flooded developed
    flood_dev = (
        (ref_simple_array == 2)
        &
        (water_extent == 1)
    )
    
    classified_flood[
        flood_dev
    ] = 1
    
    # Flooded vegetation
    flood_veg = (
        (ref_simple_array == 3)
        &
        (water_extent == 1)
    )
    
    classified_flood[
        flood_veg
    ] = 1
    
    # Flooded crop/grassland
    flood_crop = (
        (ref_simple_array == 1)
        &
        (water_extent == 1)
    )
    
    classified_flood[
        flood_crop
    ] = 1
    
    # Clouds are NOT classified as water
    if cloud_mask:
        classified_flood[
            cloud_mask_array
        ] = 0
    
    # NIR no-data is NOT classified as water
    classified_flood[
        nir_nd_mask
    ] = 0

    # -------------------------------------------------------------
    # Print class statistics
    # -------------------------------------------------------------

    print()
    print("Water extent classes:")

    for value, label in [
        (0, "Not Water"),
        (1, "Water"),
    ]:

        count = np.count_nonzero(
            classified_flood == value
        )

        print(
            f"  {value}: {label}: "
            f"{count:,} pixels"
        )

    # -------------------------------------------------------------
    # Output filename
    # -------------------------------------------------------------

    output_name = _build_output_filename(
        item,
        algorithm_name,
        masked=cloud_mask,
    )

    outfile = os.path.join(
        output_dir,
        output_name,
    )

    # -------------------------------------------------------------
    # Write GeoTIFF
    #
    # IMPORTANT:
    # The legacy workflow uses 0 as nodata.
    # -------------------------------------------------------------

    profile = nir_profile.copy()

    profile.update(
        driver="GTiff",
        count=1,
        dtype="uint8",
        nodata=0,
    )

    print()
    print(
        f"Writing Water Extent GeoTIFF: "
        f"{outfile}"
    )

    with rio.open(
        outfile,
        "w",
        **profile,
    ) as dst:

        dst.write(
            classified_flood,
            1,
        )

    print(
        f"Generation completed: {outfile}"
    )

    return outfile

# ---------------------------------------------------------------------
# Rayleigh correction
# ---------------------------------------------------------------------

# Maps Earth Search's common asset names to raw Sentinel-2 band codes.
# pyspectral's Rayleigh model only defines correction coefficients for
# B01-B07, matching the band restriction in the legacy SAFE-based
# pipeline's get_rayleigh_correction.
_ASSET_TO_BAND_CODE = {
    "coastal": "B01",
    "blue": "B02",
    "green": "B03",
    "red": "B04",
    "rededge1": "B05",
    "rededge2": "B06",
    "rededge3": "B07",
    "nir": "B08",
    "nir08": "B8A",
    "nir09": "B09",
    "swir16": "B11",
    "swir22": "B12",
}

_RAYLEIGH_CORRECTABLE_BANDS = {"B01", "B02", "B03", "B04", "B05", "B06", "B07"}


def get_rayleigh_correction(item, asset_name):
    """
    Compute a Rayleigh-scattering reflectance correction for one
    Sentinel-2 band, mirroring the legacy SAFE-based pipeline's
    get_rayleigh_correction, but sourced from STAC item properties
    instead of SAFE XML metadata.

    Only bands B01-B07 are corrected (pyspectral has no coefficients
    for the others) -- for any other band this returns 0.

    Parameters
    ----------
    item : pystac.Item
        Sentinel-2 STAC item.

    asset_name : str
        Common asset name as used in the algorithm catalog (e.g.
        "red", "nir"). Mapped to a raw band code via
        _ASSET_TO_BAND_CODE.

    Returns
    -------
    float
        Reflectance correction (0-1 scale) to subtract from the band.
        Returns 0 if the band is not correctable, geometry is
        unavailable, or pyspectral is not installed.
    """

    try:
        from pyspectral.rayleigh import Rayleigh  # noqa: F401 -- import check only
    except ImportError:
        print(
            "\t* Rayleigh correction error. pyspectral package must be "
            "installed (>=0.12.5)."
        )
        return 0

    band_code = _ASSET_TO_BAND_CODE.get(asset_name)

    if band_code not in _RAYLEIGH_CORRECTABLE_BANDS:
        return 0

    props = item.properties

    sun_elevation = props.get("view:sun_elevation")
    sun_azimuth = props.get("view:sun_azimuth")

    if sun_elevation is None or sun_azimuth is None:
        print(
            f"\t* Rayleigh correction skipped for {band_code}: STAC item "
            f"'{item.id}' has no view:sun_elevation/view:sun_azimuth."
        )
        return 0

    sun_zenith = 90.0 - sun_elevation

    sat_zenith = props.get("view:incidence_angle")
    sat_azimuth = props.get("view:azimuth")

    if sat_zenith is None or sat_azimuth is None:
        sat_zenith = 0.0
        azidiff = sun_azimuth
    else:
        azidiff = abs(sun_azimuth - sat_azimuth)

    platform = props.get("platform", "sentinel-2a")
    platform_name = "Sentinel-2A" if platform.endswith("a") else "Sentinel-2B"

    print(f"\t* Applying Rayleigh correction to: {band_code}")

    s2 = _get_rayleigh_instance(platform_name)   # <-- changed: cached, not rebuilt

    ray = 0.01 * s2.get_reflectance(
        np.asarray(sun_zenith),
        np.asarray(sat_zenith),
        np.asarray(azidiff),
        band_code,
    )

    return float(ray)


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
    nodata=-9999,
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

    # Rayleigh scattering correction applies automatically for L1C
    # (Top-Of-Atmosphere) products, since they have no atmospheric
    # correction applied. L2A already has Rayleigh correction baked in
    # via Sen2Cor, so no additional correction is applied there.
    _, level, _ = _get_sat_level_tile(item)
    apply_rayleigh = level == "MSIL1C"

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

        if apply_rayleigh:
            correction = get_rayleigh_correction(item, band_name)
            if correction:
                band = band - correction

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

    # Rayleigh scattering correction applies automatically for L1C
    # (Top-Of-Atmosphere) products, since they have no atmospheric
    # correction applied. L2A already has Rayleigh correction baked in
    # via Sen2Cor, so no additional correction is applied there.
    _, level, _ = _get_sat_level_tile(item)
    apply_rayleigh = level == "MSIL1C"

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

        if apply_rayleigh:
            correction = get_rayleigh_correction(item, band_name)
            if correction:
                # Scale reflectance-space correction (0-1) up to raw
                # DN units (0-10000) to match this band's units.
                band = band - (correction * 10000)

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

    if len(tif_paths) == 1:
        shutil.copy(tif_paths[0], output_path)
        return output_path

    # Determine dominant CRS
    with rio.open(tif_paths[0]) as src:
        crs_list = [rio.open(p).crs for p in tif_paths]

    crs_dom = max(set(crs_list), key=crs_list.count)

    merge_inputs = list(tif_paths)
    tmp_dir = None

    try:
        # Reproject inputs that don't match the dominant CRS
        if any(crs != crs_dom for crs in crs_list):

            tmp_dir = tempfile.mkdtemp(prefix="s2_merge_reproj_")

            for idx, tif in enumerate(tif_paths):

                with rio.open(tif) as src:

                    if src.crs == crs_dom:
                        continue

                    transform, width, height = calculate_default_transform(
                        src.crs,
                        crs_dom,
                        src.width,
                        src.height,
                        *src.bounds,
                    )

                    kwargs = src.meta.copy()

                    kwargs.update(
                        {
                            "crs": crs_dom,
                            "transform": transform,
                            "width": width,
                            "height": height,
                            "nodata": src.nodata,
                        }
                    )

                    reproj_path = os.path.join(
                        tmp_dir,
                        f"{idx}_{os.path.basename(tif)}",
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
                                src_nodata=src.nodata,
                                dst_nodata=src.nodata,
                                resampling=Resampling.nearest,
                            )

                    merge_inputs[idx] = reproj_path

        # ---------------------------------------------------------
        # Merge
        # ---------------------------------------------------------

        with rio.open(merge_inputs[0]) as ref:
            nodata_val = ref.nodata
            count = ref.count
            dtype = ref.dtypes[0]

        array, transform = merge(
            merge_inputs,
            method=method,
            nodata=nodata_val,
        )

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
        "water_extent",
    ):
        raise ValueError(
            "algorithm_type must be either "
            "'index', 'composite', or 'water_extent'."
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

        if algorithm_type == "water_extent":
            local_path = generate_water_extent(
                item,
                algorithm,
                algorithm_name,
                output_dir=tmp_dir,
                cloud_mask=cloud_mask,
            )
        
        elif algorithm_type == "index":
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