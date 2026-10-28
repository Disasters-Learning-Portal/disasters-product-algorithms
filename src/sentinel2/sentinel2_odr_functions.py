"""
sentinel2_odr_functions.py

Sentinel-2 processing functions, STAC/COG edition.

Reads Sentinel-2 imagery directly from a STAC API (Earth Search) and
its cloud-optimized assets on S3, rather than from downloaded
Copernicus .SAFE archives. The CLI and notebooks should call these
functions rather than containing the processing logic themselves.

Coexists with the legacy `sentinel2_functions` during the STAC
migration; see issue #144.
"""

import json
import logging
import math
import os
import re
import shutil
import tempfile
import zipfile

import boto3
import geopandas as gpd
import numpy as np
import rasterio as rio
import requests
from botocore import UNSIGNED
from botocore.client import Config
from pystac_client import Client
from rasterio.enums import ColorInterp, Resampling
from rasterio.merge import merge
from rasterio.session import AWSSession
from rasterio.warp import calculate_default_transform, reproject
from scipy.signal import medfilt2d
from shapely.geometry import box

logging.getLogger("pyspectral.rsr_reader").setLevel(logging.ERROR)


# Built lazily rather than at import time. AWSSession.__init__ resolves
# the boto3 credential chain immediately, which on a host with no other
# credential source means an EC2 metadata probe to 169.254.169.254 --
# fired by merely importing this module, including on `--help`.
_aws_session_cache = {}


def _session(unsigned=False):
    key = bool(unsigned)
    if key not in _aws_session_cache:
        _aws_session_cache[key] = (
            AWSSession(boto3.Session(), aws_unsigned=True)
            if unsigned
            else AWSSession(boto3.Session(), requester_pays=True)
        )
    return _aws_session_cache[key]


# Hard cap on scenes returned by one search. pystac_client paginates
# until the result set is exhausted, and every scene returned is a full
# 10980x10980 tile that the caller downloads and processes -- so an
# over-wide bbox (say CONUS for a month) silently fans out to thousands
# of tiles with no confirmation step. The cap is deliberately generous
# for a real activation footprint, and hitting it prints a warning
# rather than failing, so a legitimate large job is not blocked.
MAX_SEARCH_ITEMS = 200

# Newest national CDL published by USDA NASS (the 2025 CDL, released Feb
# 2026). Bump each February when the next year's CDL comes out. Used both
# where the scene year is chosen and inside the CDL downloader, which
# re-clamps its argument -- changing only one of them has no effect.
LATEST_CDL_YEAR = 2025

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
    max_items=MAX_SEARCH_ITEMS,
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

    max_items : int or None
        Hard cap on the number of scenes returned. None disables the
        cap. See MAX_SEARCH_ITEMS -- pystac_client paginates until
        exhausted, and each returned scene is a full tile the caller
        will download and process.

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
        max_items=max_items,
    )

    items = list(search.items())

    print(f"Found {len(items)} Sentinel-2 items.")

    if max_items is not None and len(items) == max_items:
        print(
            f"  WARNING: hit the {max_items}-scene cap. There may be "
            f"more matching scenes that are NOT being processed. "
            f"Narrow --bbox or the date range, or lower --cloud-cover."
        )

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
    max_items=MAX_SEARCH_ITEMS,
):
    """
    Search Sentinel-2 and return both the catalog search result and items.

    This is useful when the caller needs item_collection_as_dict()
    for GeoDataFrame processing or visualization.

    NOTE the different return shape from `search_sentinel2`, which
    returns the item list alone. Mixing the two up gives you a 3-tuple
    where you expected a list, and `len()` of it is 3 regardless of how
    many scenes matched.

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
        max_items=max_items,
    )

    items = list(search.items())
    stac_json = search.item_collection_as_dict()

    print(f"Found {len(items)} Sentinel-2 items.")

    if max_items is not None and len(items) == max_items:
        print(
            f"  WARNING: hit the {max_items}-scene cap. There may be "
            f"more matching scenes that are NOT being processed. "
            f"Narrow --bbox or the date range, or lower --cloud-cover."
        )

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
        href, scale, offset, gsd, nodata
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
    nodata = band_metadata.get("nodata")

    return href, scale, offset, gsd, nodata


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


# Algorithm keys whose filename token differs from the key itself.
#
# The catalog/CLI key stays short because it is what an operator types
# and what the DPS `products` input carries (matching the legacy
# pipeline's `-p we`), but the FILENAME must say `waterExtent`: that is
# the published product name, it is what the legacy pipeline wrote, and
# it is what the operator notebooks' CATEGORIZATION_PATTERNS match on
# (`r'WaterExtent|waterextent|water_extent'` -- a file named `_we_`
# silently fails to categorize).
_PRODUCT_FILENAME_TOKENS = {
    "we": "waterExtent",
}


def _product_token(algorithm_name):
    """Filename token for an algorithm key (see _PRODUCT_FILENAME_TOKENS)."""
    return _PRODUCT_FILENAME_TOKENS.get(
        algorithm_name,
        _to_camel_case(algorithm_name),
    )


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


def _build_output_filename(
    item,
    algorithm_name,
    masked=False,
    merged=False,
    variant=None,
):
    """
    Build a Sentinel-2 output filename matching the project naming
    convention:

        Plain:              SAT_LEVEL_product_TILE_TIMESTAMP.tif
        Masked:              SAT_LEVEL_product_TILE_masked_TIMESTAMP.tif
        Merged:              SAT_LEVEL_product_ORBIT_merged_DATE_day.tif
        Merged and masked:   SAT_LEVEL_product_ORBIT_merged_masked_DATE_day.tif

    A merged mosaic follows the repo convention (.clinerules.md rule 27):
    DATE only, then `_day` -- it spans several tile sensing times, so no
    single time is true of it (the old name carried the first tile's time,
    chosen by item-id order). DATE is the pass's own sensing-start date
    from the datatake id, the same date ESA puts in its product name, so a
    pass straddling 00:00 UTC still gets one date. ORBIT is ESA's relative
    orbit (`R112`, from `s2:product_uri`): with the time gone, it is what
    keeps two passes of one satellite on one day (high latitudes) from
    writing the same file.

    Parameters
    ----------
    item : pystac.Item
        The STAC item supplying SAT/LEVEL/timestamp. When merged=True,
        pass any item of the acquisition being merged -- the orbit and
        date are properties of the pass, not of the tile.

    algorithm_name : str
        Algorithm/product name as used in the algorithm catalog, e.g.
        "true_color", "ndvi". Converted to a filename token via
        _product_token (camelCase, except where the published product
        name differs from the catalog key -- "we" -> "waterExtent").

    masked : bool
        Whether cloud masking was applied to this output.

    merged : bool
        Whether this output is a mosaic of multiple tiles. When True,
        the tile token is omitted (a mosaic spans multiple tiles, so
        no single TILE token applies).

    variant : str or None
        Optional parameter token inserted directly after the product,
        e.g. "NSTD_1_5" for a water extent generated at nstd=1.5. This
        is what keeps multiple parameterisations of the same product on
        the same scene from overwriting each other -- the legacy
        Sentinel-2 pipeline computed this token and then failed to use
        it, so `-we_nstd 1 1.5 2` wrote all three to one path.

    Returns
    -------
    str
        Filename, e.g. "S2A_MSIL1C_trueColor_T16SED_2026-04-23T16:15:59Z.tif"
    """
    sat, level, tile = _get_sat_level_tile(item)
    product = _product_token(algorithm_name)

    variant_token = f"_{variant}" if variant else ""
    masked_token = "_masked" if masked else ""

    if merged:
        orbit = _orbit_token(item)
        orbit_token = f"_{orbit}" if orbit else ""
        return (
            f"{sat}_{level}_{product}{variant_token}{orbit_token}"
            f"_merged{masked_token}_{_acquisition_date(item)}_day.tif"
        )

    timestamp = item.datetime.replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    return (
        f"{sat}_{level}_{product}{variant_token}_{tile}"
        f"{masked_token}_{timestamp}.tif"
    )


def _orbit_token(item):
    """
    ESA relative orbit of the item's pass, e.g. "R112".

    Read from `s2:product_uri` (the ESA product name, `..._N0511_R112_T...`),
    else `sat:relative_orbit`. Falls back to the ABSOLUTE orbit embedded in
    `s2:datatake_id` ("A002799") -- still unique per pass -- and finally to
    nothing, with a warning.
    """
    match = re.search(r"_R(\d{3})_", item.properties.get("s2:product_uri", ""))
    if match:
        return f"R{match.group(1)}"
    relative = item.properties.get("sat:relative_orbit")
    if relative is not None:
        return f"R{int(relative):03d}"
    match = re.search(r"_(\d{6})_N\d{2}\.\d{2}$", item.properties.get("s2:datatake_id", ""))
    if match:
        return f"A{match.group(1)}"
    print(f"WARNING: no orbit metadata on {item.id}; merged name has no orbit token.")
    return ""


def _acquisition_date(item):
    """
    YYYY-MM-DD of the pass: the sensing-start date in `s2:datatake_id`
    ("GS2C_20250319T171041_..."), else the item's own UTC date.
    """
    match = re.match(r"GS2[A-Z]_(\d{4})(\d{2})(\d{2})T", item.properties.get("s2:datatake_id", ""))
    if match:
        return "-".join(match.groups())
    return item.datetime.strftime("%Y-%m-%d")


def acquisition_key(item):
    """
    Identify the satellite pass (datatake) a STAC item belongs to.

    `--merge` mosaics one ACQUISITION, never several: tiles from different
    passes differ in date, illumination and atmosphere, and a first-wins
    mosaic across them silently stitches pre-event pixels into a post-event
    product. A datatake is the natural unit -- one pass of one satellite --
    and is safer than the UTC calendar date, which can split a single pass:
    Sentinel-2 crosses ~10:30 local solar time, i.e. ~00:00 UTC near 157 E
    (eastern Australia: real passes at 23:54 and 00:04 UTC).

    `s2:datatake_id` (e.g. "GS2A_20210715T165901_031669_N05.00") ends in the
    processing baseline. That suffix is DROPPED: Earth Search lists the same
    acquisition at more than one baseline (sentinel-2-l1c `_0` at N03.01 and
    `_1` at N05.00, identical datetime), and those copies must land in the
    same group -- as separate groups they would be written to the same
    merged filename and overwrite each other.

    Falls back to (platform, UTC date) when the property is absent.
    """
    datatake = item.properties.get("s2:datatake_id")
    if datatake:
        return re.sub(r"_N\d{2}\.\d{2}$", "", datatake)
    print(
        f"WARNING: {item.id} has no s2:datatake_id; grouping it by "
        f"platform + UTC date for --merge."
    )
    return (
        f"{item.properties.get('platform', '')}_"
        f"{item.datetime.strftime('%Y%m%d')}"
    )


def processing_baseline(item):
    """
    The item's processing baseline as a float (05.11 -> 5.11), or 0.0 when
    unknown -- read from `s2:processing_baseline`, else from the
    `_Nxx.xx` suffix of `s2:datatake_id`.
    """
    value = item.properties.get("s2:processing_baseline")
    if value is None:
        match = re.search(
            r"_N(\d{2}\.\d{2})$",
            item.properties.get("s2:datatake_id", ""),
        )
        value = match.group(1) if match else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def drop_superseded_baselines(items):
    """
    Keep only the newest processing baseline of each (acquisition, tile).

    Earth Search lists some acquisitions more than once -- sentinel-2-l1c
    carries `_0` (e.g. N03.01) and `_1` (N05.00) copies of the same tile
    with an identical datetime. Both copies produce the SAME per-tile output
    filename, so processing both lets whichever runs last overwrite the
    other (search order, not science, decides). The later baseline is the
    reprocessing with the newer calibration, so it is the one kept. The
    radiometric offset is still read per item (rule 52), so either copy
    converts correctly; this only removes the duplicate.
    """
    best = {}
    for item in items:
        key = (acquisition_key(item), _get_sat_level_tile(item)[2])
        if key not in best or (
            processing_baseline(item) > processing_baseline(best[key])
        ):
            best[key] = item
    kept_ids = {id(item) for item in best.values()}
    for item in items:
        if id(item) not in kept_ids:
            print(
                f"Skipping {item.id}: superseded by a newer processing "
                f"baseline of the same acquisition and tile."
            )
    return [item for item in items if id(item) in kept_ids]


def _nstd_variant_token(nstd):
    """
    Filename token for a water-extent standard-deviation multiplier.

    Mirrors the legacy convention (`NSTD_1_5` for 1.5) -- the decimal
    point is replaced because it would otherwise read as a file
    extension. An integral value renders without a trailing `_0`, so
    the common nstd=1 case stays `NSTD_1`.
    """
    value = float(nstd)
    text = str(int(value)) if value.is_integer() else str(value)
    return f"NSTD_{text.replace('.', '_')}"


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

    if width < 1 or height < 1:
        raise ValueError(
            f"resample_ratio {resample_ratio} reduces a "
            f"{profile['width']}x{profile['height']} raster to "
            f"{width}x{height}."
        )

    band = src.read(
        1,
        out_shape=(height, width),
        resampling=resampling,
    )

    # Scale the transform by the ratio ACTUALLY achieved, not the one
    # requested. `out_shape` decimates the full source extent into the
    # requested shape, so when width does not divide evenly the
    # effective pixel size is src.width/width, not resample_ratio. Using
    # the requested ratio declares a georeference the data does not
    # have: a 10981-px 10 m source at ratio 2 yields 5490 px of 20.0018
    # m, but would be labelled 20.0 m -- a one-pixel shift at the far
    # edge, growing with raster size.
    dst_transform = (
        src.transform
        * rio.Affine.scale(
            src.width / width,
            src.height / height,
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
    """
    Read one STAC asset as an array, optionally resampled and converted
    to physical units.

    Parameters
    ----------
    asset : pystac.Asset or dict
        The STAC asset to read.

    target_gsd : float or None
        Ground sample distance to resample to. None reads native.

    resample : bool
        Whether to honour target_gsd at all.

    apply_scale : bool
        If True, return float32 physical units (0-1 reflectance for
        Sentinel-2) with source-nodata pixels set to NaN. If False,
        return the raw array untouched -- used by the water-extent
        product, whose threshold is defined in raw DN.

    Returns
    -------
    band : numpy.ndarray
    profile : dict

    Notes
    -----
    The conversion is ``raw * scale + offset``. Both the STAC raster
    extension (v1.1.0: "number to be added to the pixel value (after
    scaling)") and GDAL ("Units value = (raw value * scale) + offset")
    define offset as ADDITIVE. Earth Search publishes
    ``scale=0.0001, offset=-0.1`` for Sentinel-2, which is exactly ESA's
    baseline-04.00 ``BOA_ADD_OFFSET=-1000`` over
    ``QUANTIFICATION_VALUE=10000``. Subtracting it instead adds +0.2
    reflectance to every band; measured on a real Iowa corn pixel, that
    moves NDVI from 0.81 to 0.39.

    The offset is published per ITEM, not per collection -- pre-baseline
    04.00 items carry offset 0 and the same tile reprocessed to baseline
    05.00 carries -0.1 -- so it must be read from the asset every time
    and never hardcoded.

    Source nodata becomes NaN rather than a physical value. Sentinel-2
    declares ``nodata: 0``, and 0 DN maps to -0.1 reflectance under the
    current offset; left unmasked, the off-swath wedge that every
    Sentinel-2 tile carries produces a finite, plausible-looking index
    value instead of nodata. The index formulas all gate on
    ``np.isfinite``, so NaN propagates correctly with no further
    plumbing.
    """

    href, scale, offset, gsd, src_nodata = get_asset_metadata(asset)

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
        aws_session = _session(unsigned=True)
        print("Using anonymous S3 access for Sentinel-2 L1C asset.")
    else:
        aws_session = _session()

    with rio.Env(aws_session):
        print("Trying to open:", href)

        with rio.open(href) as src:

            # Fall back to the file's own nodata when the STAC asset
            # does not declare one.
            if src_nodata is None:
                src_nodata = src.nodata

            if (
                resample
                and target_gsd is not None
                and target_gsd != gsd
            ):
                resample_ratio = target_gsd / gsd

                print(
                    f"Resampling band from {gsd} m to {target_gsd} m "
                    f"(ratio {resample_ratio})"
                )

                band, profile = resample_array(
                    src,
                    resample_ratio,
                    # Nearest for the nodata-preserving reason below:
                    # bilinear blends the 0-fill wedge into valid
                    # pixels, turning edge samples into artificially
                    # dark but finite reflectance.
                    resampling=Resampling.nearest,
                )

            else:
                band = src.read(1)
                profile = src.profile.copy()

    if apply_scale:

        invalid = (
            None if src_nodata is None else (band == src_nodata)
        )

        band = band.astype(np.float32) * scale + offset

        if invalid is not None:
            band[invalid] = np.nan

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

    href, _, _, _, _ = get_asset_metadata(item.assets["scl"])

    print(f"Building cloud mask from SCL asset: {href}")

    # Inside rio.Env for the same reason every band read is: an
    # s3:// SCL href (an alternate asset, or a non-Earth-Search
    # catalog) needs the session's credential configuration. Earth
    # Search currently serves https, which is why the bare open has
    # worked so far.
    with rio.Env(_session()):
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

    year = min(int(year), LATEST_CDL_YEAR)

    if year < 2008:
        raise ValueError(
            f"CDL year {year} is not supported."
        )

    os.makedirs(
        reference_dir,
        exist_ok=True,
    )

    # ------------------------------------------------------------
    # Paths
    #
    # The national CDL zip (~1.6 GB) and its extracted GeoTIFF are
    # CACHED, not temporary. An activation routinely spans several
    # Sentinel-2 tiles, and every one of them needs the same national
    # raster; deleting it in the `finally` -- as an earlier revision
    # did -- re-downloaded 1.6 GB per scene. Only the per-scene subset
    # is genuinely temporary.
    #
    # The cache lives in reference_dir, which the caller places under
    # the run's output directory, so it is still cleaned up with the
    # run and never accumulates across activations.
    # ------------------------------------------------------------

    zip_path = os.path.join(
        reference_dir,
        f"CDL_{year}_30m_download.zip",
    )

    national_cdl_path = os.path.join(
        reference_dir,
        f"CDL_{year}_30m.tif",
    )

    subset_path = os.path.join(
        reference_dir,
        f"_CDL_{year}_subset.tif",
    )

    try:

        # --------------------------------------------------------
        # Download national CDL
        # --------------------------------------------------------

        if os.path.exists(zip_path) or os.path.exists(
            national_cdl_path
        ):

            print(
                f"Reusing cached national {year} CDL download."
            )

        else:

            print()
            print(
                f"Downloading national {year} 30-m CDL..."
            )
            print(
                "This is approximately 1.6 GB for the 2024 CDL. It is "
                "cached and reused across every scene in this run."
            )

            cdl_url = (
                "https://www.nass.usda.gov/"
                "Research_and_Science/Cropland/Release/datasets/"
                f"{year}_30m_cdls.zip"
            )

            print(
                f"CDL URL: {cdl_url}"
            )

            # Download to a partial name and rename on success, so an
            # interrupted transfer cannot leave a truncated archive
            # that the next scene treats as a valid cache hit.
            partial_zip = zip_path + ".partial"

            try:

                with requests.get(
                    cdl_url,
                    stream=True,
                    timeout=(30, 1800),
                ) as response:

                    response.raise_for_status()

                    with open(
                        partial_zip,
                        "wb",
                    ) as f:

                        for chunk in response.iter_content(
                            chunk_size=1024 * 1024
                        ):

                            if chunk:
                                f.write(chunk)

                os.replace(partial_zip, zip_path)

                print(
                    "CDL download complete."
                )

            except requests.exceptions.RequestException as e:

                if os.path.exists(partial_zip):
                    os.remove(partial_zip)

                raise RuntimeError(
                    f"Failed to download the {year} CDL: {e}"
                ) from e

        # --------------------------------------------------------
        # Extract national CDL TIFF
        # --------------------------------------------------------

        if os.path.exists(national_cdl_path):

            print(
                f"Reusing cached CDL TIFF: {national_cdl_path}"
            )

        else:

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

                # Extract to a temp name and rename into place, so an
                # interrupted extraction cannot leave a truncated file
                # that the next scene happily treats as cached.
                partial_path = national_cdl_path + ".partial"

                with z.open(tif_member) as src_file:

                    with open(
                        partial_path,
                        "wb",
                    ) as dst_file:

                        shutil.copyfileobj(
                            src_file,
                            dst_file,
                        )

                os.replace(partial_path, national_cdl_path)

        print(
            "CDL TIFF ready."
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

            # Window.intersection RAISES WindowError on an empty
            # intersection rather than returning a zero-size window, so
            # the size check below can never fire on its own. Catch it
            # and re-raise with the diagnostic the operator needs --
            # otherwise a non-CONUS scene surfaces as a bare
            # rasterio.errors.WindowError after a 1.6 GB download.
            try:
                window = window.intersection(
                    full_window
                )
            except rio.errors.WindowError as exc:
                raise RuntimeError(
                    "Sentinel-2 scene does not overlap "
                    "the national CDL."
                ) from exc

            if (
                window.width <= 0
                or window.height <= 0
            ):
                raise RuntimeError(
                    "Sentinel-2 scene does not overlap "
                    "the national CDL."
                )

            # Snap to whole pixels before reading. from_bounds returns
            # fractional offsets/lengths; src.read and window_transform
            # round them independently, which can disagree by a pixel
            # and shift the reference layer against the NIR grid.
            window = window.round_offsets(
                op="floor"
            ).round_lengths(
                op="ceil"
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
        # Remove only the per-scene subset.
        #
        # zip_path and national_cdl_path are intentionally KEPT: they
        # are the same for every tile in the activation, and deleting
        # them here means re-downloading 1.6 GB per scene. They live
        # under the run's output directory and go away with it.
        # --------------------------------------------------------

        if os.path.exists(subset_path):

            try:
                os.remove(
                    subset_path
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

    # The v200 grid, NOT the year-stamped v100/2020 one. The old file
    # lists 2631 tiles; both versions actually ship 2651 on S3, so the
    # stale grid makes 20 real tiles unreachable -- overwhelmingly
    # small-island and coastal (Galapagos N00W090, Fiji/Tonga S24W180,
    # Comoros S12E051, NW Australia S15E120), which is exactly the kind
    # of AOI a flood pipeline must not be blind to. There is no
    # esa_worldcover_2021_grid.geojson; the v200 file is not
    # year-stamped.
    grid_url = (
        "https://esa-worldcover.s3.eu-central-1.amazonaws.com/"
        "v200/2021/esa_worldcover_grid.geojson"
    )

    print("Loading WorldCover tile grid...")

    grid = gpd.read_file(grid_url)

    _EXPECTED_GRID_TILES = 2651

    if len(grid) != _EXPECTED_GRID_TILES:
        print(
            f"  WARNING: WorldCover grid has {len(grid)} tiles, "
            f"expected {_EXPECTED_GRID_TILES}. The published grid may "
            f"have changed; verify tile coverage for this AOI."
        )

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


# The land-cover crosswalks live in shared_utils.landcover -- one table for
# every pipeline (.clinerules.md rule 65). Aliased under their historical
# private names.
from shared_utils.landcover import (  # noqa: E402
    CDL_WATER_SAMPLE_CODES as _CDL_WATER_SAMPLE_CODES,
    WORLDCOVER_WATER_SAMPLE_CODES as _WORLDCOVER_WATER_SAMPLE_CODES,
    reclass_cdl_array as _reclass_cdl_array,
    reclass_worldcover_array as _reclass_worldcover_array,
    pooled_water_statistics,
    select_water_sample,
)

# ---------------------------------------------------------------------
# Water Extent
# ---------------------------------------------------------------------

def generate_water_extent(
    item,
    algorithm,
    algorithm_name,
    output_dir="./s3_temp",
    cloud_mask=False,
    nstd=None,
    water_stats=None,
    sample_only=False,
):
    """
    Generate the Sentinel-2 Water Extent product using the same
    methodology as the legacy gen_water_extent() workflow.

    Algorithm:

        1. Read Sentinel-2 B08 at native resolution, as raw DN.
        2. Obtain CDL (CONUS) or WorldCover (elsewhere) reference data.
        3. Reclassify reference data:
               1 = cropland/grassland
               2 = developed
               3 = other vegetation
               4 = permanent water
             999 = no data / excluded
        4. Identify cloud-free permanent-water pixels.
        5. Calculate:
               threshold = mean(water NIR)
                         + nstd * std(water NIR)
        6. Classify pixels at or below the threshold as water.
        7. Apply 5x5 median filtering to despeckle.
        8. Emit the six-class product:
               0 = no data
               1 = permanent water
               2 = flooded developed
               3 = flooded vegetation
               4 = flooded crop/grassland
               5 = cloud / cloud shadow

    The six-class encoding is deliberate and matches the legacy
    published product. Collapsing it to a binary water mask -- as an
    earlier revision of this module did -- discards the distinction
    between water that is normally there and water that is NOT, which
    is the operative question for a flood activation, and makes the
    land-cover reference pointless beyond seeding the threshold.

    Parameters
    ----------
    item : pystac.Item
        Sentinel-2 STAC item.

    algorithm : dict
        Algorithm configuration. Supplies `gsd`, `resample`, and the
        default `nstd`.

    algorithm_name : str
        Algorithm/product key, e.g. "we".

    output_dir : str
        Local directory to write the output GeoTIFF.

    cloud_mask : bool
        If True, mask cloud/cloud-shadow/thin-cirrus using the L2A
        Scene Classification Layer and emit them as class 5.

    nstd : float or None
        Standard-deviation multiplier for the NIR water threshold.
        Falls back to the algorithm config's `nstd`, then to 1.0.

    water_stats : (float, float) or None
        (mean, std) of the water-sample NIR POOLED over every tile of this
        item's acquisition (shared_utils.landcover.pooled_water_statistics).
        When given, the threshold is mean + nstd*std from these, so all
        tiles of one pass classify with one threshold. None computes them
        from this tile alone.

    sample_only : bool
        If True, return (permanent_water_nir, sample_nir) -- this tile's
        contribution to the pooled statistics -- and write nothing.

    Returns
    -------
    str
        Path to the generated GeoTIFF.
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

    href, scale, offset, gsd, nir_src_nodata = get_asset_metadata(
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

    nir_nodata = 0 if nir_src_nodata is None else nir_src_nodata

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
            LATEST_CDL_YEAR,
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
    
        ref_simple_array, ref_sample_mask = _reclass_cdl_array(
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

        ref_simple_array, ref_sample_mask = _reclass_worldcover_array(
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
    # Two DIFFERENT masks, deliberately:
    #
    #   permanent_water_mask -- what gets LABELLED class 1 in the
    #       product. All of bucket 4.
    #
    #   water_sample_mask -- what SEEDS the NIR mean/std. A strict
    #       subset (CDL 111 / WorldCover 80), because the threshold is
    #       only as good as its reflectance endmember. Aquaculture
    #       ponds are real water and belong in the label, but they are
    #       turbid, algal and periodically drained, so they make a poor
    #       reference.
    # -------------------------------------------------------------

    permanent_water_mask = (
        (ref_simple_array == 4)
        &
        (~cloud_mask_array)
        &
        (~nir_nd_mask)
    )

    water_sample_mask = (
        ref_sample_mask
        &
        (~cloud_mask_array)
        &
        (~nir_nd_mask)
    )

    # sample_only: return this tile's contribution to the acquisition's
    # POOLED threshold (shared_utils.landcover.pooled_water_statistics)
    # and stop. No per-tile empty-sample check -- a tile without
    # reference water is normal; the pooled totals decide.
    if sample_only:
        return (
            nir_array[permanent_water_mask].astype(np.float64),
            nir_array[water_sample_mask].astype(np.float64),
        )

    if water_stats is None:
        # Falls back to the full bucket below MIN_WATER_SAMPLE px; raises
        # on an empty sample (shared with the .SAFE and Landsat paths).
        water_sample_mask = select_water_sample(
            permanent_water_mask,
            water_sample_mask,
        )

    # -------------------------------------------------------------
    # Calculate dynamic NIR threshold
    #
    # IMPORTANT:
    #
    # This is the actual logic from the old workflow.
    # -------------------------------------------------------------

    if water_stats is not None:
        # Pooled over every tile of this acquisition by the caller, so
        # all tiles of one pass classify with ONE threshold.
        mean, std = water_stats
    else:
        water_nir = nir_array[
            water_sample_mask
        ]

        mean = np.nanmean(
            water_nir
        )

        std = np.nanstd(
            water_nir
        )

    # -------------------------------------------------------------
    # nstd: caller wins, then the algorithm configuration, then 1.0.
    #
    # Compared with `is None`, not truthiness -- 0.0 is a meaningless
    # threshold but it must not silently fall through to the default.
    # (It is rejected by the CLI, which requires a positive value.)
    # -------------------------------------------------------------

    if nstd is None:
        nstd = algorithm.get(
            "nstd",
            1.0,
        )

    nstd = float(nstd)

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

    # Nodata is not water. It must be cleared BEFORE the median filter,
    # not after: nodata DN is 0, which is trivially <= any threshold, so
    # the whole off-swath wedge enters the filter voting "water" and
    # drags genuinely dry pixels along the (diagonal, irregular) swath
    # boundary over to water.
    water_extent[
        nir_nd_mask
    ] = 0

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
    # Six-class water extent (legacy published encoding)
    #
    #   0 = no data
    #   1 = permanent water
    #   2 = flooded developed
    #   3 = flooded vegetation
    #   4 = flooded crop/grassland
    #   5 = cloud / cloud shadow
    #
    # Classes 2-4 are "water was detected here AND the land-cover
    # reference says this is not normally water", i.e. NEW flooding --
    # which is what an activation is actually asking about.
    # -------------------------------------------------------------

    classified_flood = np.zeros(
        water_extent.shape,
        dtype=np.uint8,
    )

    # Permanent water. Taken from the reference layer rather than from
    # the threshold, matching legacy: these are the pixels that defined
    # the threshold in the first place.
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
    ] = 2

    # Flooded vegetation
    flood_veg = (
        (ref_simple_array == 3)
        &
        (water_extent == 1)
    )

    classified_flood[
        flood_veg
    ] = 3

    # Flooded crop/grassland
    flood_crop = (
        (ref_simple_array == 1)
        &
        (water_extent == 1)
    )

    classified_flood[
        flood_crop
    ] = 4

    # Cloud / cloud shadow gets its own class rather than being folded
    # into "not water": an obscured pixel is unobserved, not dry.
    if cloud_mask:
        classified_flood[
            cloud_mask_array
        ] = 5

    # NIR no-data is no data. Applied last so it wins over every class
    # above.
    classified_flood[
        nir_nd_mask
    ] = 0

    # -------------------------------------------------------------
    # Print class statistics
    # -------------------------------------------------------------

    print()
    print("Water extent classes:")

    for value, label in [
        (0, "No data"),
        (1, "Permanent water"),
        (2, "Flooded developed"),
        (3, "Flooded vegetation"),
        (4, "Flooded crop/grassland"),
        (5, "Cloud / cloud shadow"),
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
        variant=_nstd_variant_token(nstd),
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

# pyspectral's Rayleigh LUT is a wavelength grid spanning 400-800 nm,
# not a per-band coefficient table; the gate is simply whether a band's
# effective wavelength lands inside it. B01-B07 do (442-783 nm); B08
# onward do not (828 nm+). True for S2A, S2B and S2C alike.
_RAYLEIGH_CORRECTABLE_BANDS = {"B01", "B02", "B03", "B04", "B05", "B06", "B07"}

# STAC `platform` -> the exact platform string pyspectral expects.
#
# The string is interpolated straight into `rsr_msi_{platform}.h5`, so
# it must match exactly -- "S2C", "sentinel-2c" and "Sentinel2C" all
# raise FileNotFoundError. Sentinel-2C RSRs exist (RSR data v1.4.0+,
# shipped in the v1.6.1 tarball) and genuinely differ from S2A's:
# B02 is centred at 486.0 nm vs 489.8 nm, roughly a 3% swing in
# Rayleigh reflectance at that band. So an unknown platform must
# RAISE rather than fall back to Sentinel-2A -- a silent fallback
# would apply the wrong instrument's spectral response and there is no
# way to tell from the output that it happened.
_PLATFORM_TO_PYSPECTRAL = {
    "sentinel-2a": "Sentinel-2A",
    "sentinel-2b": "Sentinel-2B",
    "sentinel-2c": "Sentinel-2C",
}


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
    except ImportError as exc:
        # Deliberately fatal. Returning 0 here -- as an earlier revision
        # did -- ships uncorrected top-of-atmosphere data under a
        # product name that claims Rayleigh correction, with nothing in
        # the output to say so. pyspectral is absent from every
        # environment file in this repo's history, so that path was not
        # a rare fallback: it was what always happened.
        raise ImportError(
            "Rayleigh correction requires pyspectral, which is not "
            "installed. Add `pyspectral` to the environment (it is on "
            "conda-forge) and provision its RSR/LUT data offline -- see "
            "docs/DPS.md. To process without Rayleigh correction, use "
            "L2A (--level 2), which is already surface reflectance."
        ) from exc

    band_code = _ASSET_TO_BAND_CODE.get(asset_name)

    if band_code not in _RAYLEIGH_CORRECTABLE_BANDS:
        return 0

    props = item.properties

    sun_elevation = props.get("view:sun_elevation")
    sun_azimuth = props.get("view:sun_azimuth")

    if sun_elevation is None or sun_azimuth is None:
        raise ValueError(
            f"Cannot Rayleigh-correct {band_code}: STAC item "
            f"'{item.id}' declares no view:sun_elevation / "
            f"view:sun_azimuth, so the illumination geometry is "
            f"unknown. Use --level 2 (L2A surface reflectance) instead."
        )

    sun_zenith = 90.0 - sun_elevation

    sat_zenith = props.get("view:incidence_angle")
    sat_azimuth = props.get("view:azimuth")

    if sat_zenith is None or sat_azimuth is None:
        # Earth Search L1C items carry no viewing geometry. Nadir is a
        # sound approximation for Sentinel-2 (max ~10 deg off-nadir),
        # and at sat_zenith=0 the scattering angle loses its dependence
        # on the azimuth difference entirely (sin(0) = 0), so the
        # azidiff value passed alongside it does not matter.
        sat_zenith = 0.0
        azidiff = 0.0
    else:
        # Wrap into [0, 180]. The scattering geometry is symmetric
        # about 180 deg, and the LUT is only gridded over that half --
        # a raw difference of, say, 350 deg is really 10 deg.
        azidiff = abs(sun_azimuth - sat_azimuth) % 360.0
        if azidiff > 180.0:
            azidiff = 360.0 - azidiff

    platform = str(props.get("platform", "")).strip().lower()
    platform_name = _PLATFORM_TO_PYSPECTRAL.get(platform)

    if platform_name is None:
        raise ValueError(
            f"STAC item '{item.id}' reports platform '{platform}', "
            f"which has no known pyspectral spectral response. Known: "
            f"{sorted(_PLATFORM_TO_PYSPECTRAL)}. Refusing to guess -- "
            f"applying another satellite's response would silently "
            f"bias the correction."
        )

    print(
        f"\t* Applying Rayleigh correction to {band_code} "
        f"({platform_name})"
    )

    s2 = _get_rayleigh_instance(platform_name)

    # pyspectral returns reflectance in PERCENT (get_reflectance ends
    # in np.clip(res, 0, 100)); scale to the 0-1 fraction this module
    # works in.
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

# EVI coefficients (Huete et al. 2002), the canonical MODIS/Landsat
# set, shared with landsat89_functions.genEvi and satellogic_v2.genEVI.
_EVI_G = 2.5
_EVI_C1 = 6.0
_EVI_C2 = 7.5
_EVI_L = 1.0


def calculate_normalized_difference(bands):
    """
    Normalized difference of two bands: (b1 - b2) / (b1 + b2).

    Parameters
    ----------
    bands : sequence of numpy.ndarray
        Exactly two arrays, in catalog `assets` order.

    Returns
    -------
    values : numpy.ndarray (float32)
        The index. Entries where `valid` is False are unspecified.
    valid : numpy.ndarray (bool)
        True where the result is meaningful.
    """

    band1, band2 = (np.asarray(b, dtype=np.float32) for b in bands)

    denominator = band1 + band2

    valid = (
        np.isfinite(band1)
        & np.isfinite(band2)
        & (denominator != 0)
    )

    values = np.zeros(band1.shape, dtype=np.float32)

    np.divide(
        band1 - band2,
        denominator,
        out=values,
        where=valid,
    )

    return values, valid


def calculate_evi(bands):
    """
    Enhanced Vegetation Index (Huete et al. 2002):

        EVI = G * (NIR - RED) / (NIR + C1*RED - C2*BLUE + L)

    with G=2.5, C1=6, C2=7.5, L=1.

    Parameters
    ----------
    bands : sequence of numpy.ndarray
        Exactly three arrays in catalog `assets` order: nir, red, blue.

    Returns
    -------
    values, valid : see calculate_normalized_difference.

    Notes
    -----
    Requires 0-1 reflectance, NOT raw DN. The `+ L` term is an absolute
    offset in reflectance units; on a 0-10000 DN scale it is negligible
    and the index silently becomes a different quantity.

    Unlike a normalized difference, this denominator is not a sum of
    non-negative reflectances -- a large blue drives it negative, so it
    passes THROUGH zero rather than merely touching it. That is why the
    guard is on magnitude (`|denom| > eps`) rather than the `denom +
    1e-10` epsilon used elsewhere in this repo: adding an epsilon to a
    denominator approaching zero from below does not prevent the blow-up,
    it just moves it.
    """

    nir, red, blue = (np.asarray(b, dtype=np.float32) for b in bands)

    denominator = nir + _EVI_C1 * red - _EVI_C2 * blue + _EVI_L

    valid = (
        np.isfinite(nir)
        & np.isfinite(red)
        & np.isfinite(blue)
        & (np.abs(denominator) > 1e-6)
    )

    values = np.zeros(nir.shape, dtype=np.float32)

    np.divide(
        _EVI_G * (nir - red),
        denominator,
        out=values,
        where=valid,
    )

    return values, valid


# Index formula registry.
#
# `assets` is the exact number of bands the formula consumes, checked
# against the catalog entry so a miswired config fails loudly instead of
# reading the wrong band.
#
# `out_of_range` says what to do with a value outside the catalog's
# min/max:
#   "nodata" -- for a normalized difference, where |value| > 1 is
#               arithmetically impossible and therefore marks a bad
#               pixel.
#   "clip"   -- for EVI, whose true range extends past 1 over dense
#               vegetation. Nodata-ing those would punch holes in the
#               healthiest canopy. Matches landsat89_functions.genEvi
#               and satellogic_v2.genEVI, which both np.clip.
_INDEX_FORMULAS = {
    "normalized_difference": {
        "assets": 2,
        "fn": calculate_normalized_difference,
        "out_of_range": "nodata",
    },
    "evi": {
        "assets": 3,
        "fn": calculate_evi,
        "out_of_range": "clip",
    },
}


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

    formula_name = algorithm.get("formula", "normalized_difference")

    if formula_name not in _INDEX_FORMULAS:
        raise ValueError(
            f"Unknown index formula '{formula_name}' for algorithm "
            f"'{algorithm_name}'. Known formulas: "
            f"{sorted(_INDEX_FORMULAS)}."
        )

    formula = _INDEX_FORMULAS[formula_name]

    if len(algorithm_assets) != formula["assets"]:
        raise ValueError(
            f"Algorithm '{algorithm_name}' uses formula "
            f"'{formula_name}', which requires exactly "
            f"{formula['assets']} assets, but its catalog entry lists "
            f"{len(algorithm_assets)}: {algorithm_assets}."
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

    print(f"Calculating index (formula: {formula_name})...")

    values, valid = formula["fn"](bands)

    minimum = algorithm.get("min", -1.0)
    maximum = algorithm.get("max", 1.0)

    if formula["out_of_range"] == "clip":
        values = np.clip(values, minimum, maximum)
    else:
        valid = valid & (values >= minimum) & (values <= maximum)

    index = np.full(
        values.shape,
        nodata,
        dtype=np.float32,
    )

    index[valid] = values[valid]

    mask = None

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

    # Sidecar so --merge can keep this tile's cloud mask (see
    # apply_cloud_union).
    if mask is not None:
        write_cloud_mask_sidecar(outfile, mask, profile)

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
    Apply logarithmic display scaling, mapping [low, high] to 0-255.

    Parameters
    ----------
    array : numpy.ndarray
        Input image, in RAW DN. Values <= 0 become NaN in the output
        (nodata / off-swath), for the caller to fill.

    low : float
        Lower logarithmic threshold, in the same units as `array`.

    high : float
        Upper logarithmic threshold, in the same units as `array`.

    output_min : float
        Minimum output value.

    output_max : float
        Maximum output value.

    Returns
    -------
    numpy.ndarray
        Log-scaled image.
    """

    array = np.asarray(array, dtype=np.float32)

    low_log = math.log(low)
    high_log = math.log(high)

    diff = high_log - low_log

    # log(0) and log(negative) are the nodata / off-swath cases.
    with np.errstate(divide="ignore", invalid="ignore"):
        logged = np.log(
            np.where(array > 0, array, np.nan)
        )

    # Every selection mask is derived from `logged` BEFORE anything is
    # written into the output.
    #
    # The previous form mutated its working array in place and then
    # re-tested it: step 1 wrote `output_min` into the below-range
    # pixels, and step 2's `>= high_log` test then re-selected those
    # very pixels and painted them `output_max`. That is only harmless
    # while `output_min` happens to fall below `low_log` -- true for the
    # raw-DN thresholds this shipped with (log(750) = 6.6 > 0) and false
    # for any threshold below 1.0, where every dark pixel comes out
    # white. A stretch function must not depend on its own output values
    # landing outside its own input domain.
    below = logged <= low_log
    above = logged >= high_log
    inside = ~below & ~above & np.isfinite(logged)

    rescaled = np.full(array.shape, output_min, dtype=np.float32)

    rescaled[above] = output_max

    rescaled[inside] = output_min + (
        (output_max - output_min)
        * (logged[inside] - low_log)
        / diff
    )

    # Propagate nodata as NaN so the caller decides how to fill it,
    # rather than silently rendering it as the darkest valid value.
    rescaled[~np.isfinite(logged)] = np.nan

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

        # RAW DN, deliberately. The display stretch below is calibrated
        # in DN, and re-tuning a published product's appearance is not
        # a change to make silently.
        #
        # KNOWN ISSUE (not fixed here): a fixed DN stretch is not
        # baseline-invariant. From processing baseline 04.00 (Jan 2022)
        # every value carries a +1000 additive offset, so the same
        # scene renders differently before and after that date, and the
        # legacy 750/7500 thresholds no longer mean what they meant
        # when they were chosen. Moving the stretch into reflectance
        # fixes that but visibly changes every composite, so it needs a
        # product decision rather than a bug fix. See issue #144.
        band, profile = read_algorithm_band(
            item.assets[band_name],
            target_gsd=algorithm.get("gsd"),
            resample=algorithm.get("resample", False),
            apply_scale=False,
        )

        if apply_rayleigh:
            correction = get_rayleigh_correction(item, band_name)
            if correction:
                # Scale the reflectance-space correction (0-1) up to
                # raw DN units to match this band.
                band = band - (correction * 10000)

        bands.append(band)

    mask = None

    if cloud_mask:
        print("Applying cloud mask...")
        mask = get_cloud_mask(item, out_shape=bands[0].shape)
        for band in bands:
            band[mask] = 0

    print("Applying logarithmic display scaling...")

    rgb = np.asarray(bands)

    array = apply_log_scale(rgb)

    # apply_log_scale returns NaN for nodata / off-swath. Resolve it
    # BEFORE the uint8 cast: casting NaN to an integer dtype is
    # undefined in numpy and yields arbitrary values, not 0.
    array = np.nan_to_num(
        array,
        nan=0.0,
        posinf=255.0,
        neginf=0.0,
    )

    array = np.asarray(
        np.clip(array, 0, 255),
        dtype=np.uint8,
    )

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

    # Sidecar so --merge can keep this tile's cloud mask (see
    # apply_cloud_union).
    if mask is not None:
        write_cloud_mask_sidecar(outfile, mask, profile)

    print(f"Generation completed: {outfile}")

    return outfile


# ---------------------------------------------------------------------
# Merge processing
# ---------------------------------------------------------------------

def merge_products(tif_paths, output_path, method="first"):

    if len(tif_paths) == 1:
        shutil.copy(tif_paths[0], output_path)
        return output_path

    # Determine dominant CRS. Each dataset is opened in its own `with`
    # so it is actually closed -- the previous form leaked a handle per
    # input inside an outer `with` that served no purpose.
    crs_list = []

    for path in tif_paths:
        with rio.open(path) as src:
            crs_list.append(src.crs)

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
            # A mosaic of several Sentinel-2 tiles is large, and this is
            # an intermediate that convert_to_cog reads back. Writing it
            # uncompressed and untiled costs real disk and IO on a DPS
            # worker with a bounded outdir.
            "compress": "ZSTD",
            "tiled": True,
            "blockxsize": 512,
            "blockysize": 512,
        }

        with rio.open(output_path, "w", **profile) as dst:
            dst.write(array)

    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return output_path


# Water-extent class for cloud / cloud shadow (generate_water_extent).
WATER_EXTENT_CLOUD_CLASS = 5


def water_extent_merge_method(merged_data, new_data, merged_mask, new_mask, **kwargs):
    """
    rasterio.merge method for water-extent tiles of ONE acquisition:
    first valid pixel, except that a CLOUD flag from any tile wins.

    Overlapping tiles of one pass are the same observation, but Sen2Cor
    classifies each tile separately and their SCL disagrees on 0.7-9.7% of
    overlap pixels (12 real Iowa passes, 2024). The disputed pixels are
    82% thin cirrus and 13% cloud SHADOW; a shadow published as "clear"
    has low NIR and reads as flood water. A first-wins merge settled the
    dispute by item-id order; taking the union of the cloud flags is the
    conservative, order-independent rule. Nodata (0, masked) still yields
    to data, exactly as rasterio's copy_first.
    """
    fill = merged_mask & ~new_mask
    np.copyto(merged_data, new_data, where=fill, casting="unsafe")
    cloud = ~new_mask & (new_data == WATER_EXTENT_CLOUD_CLASS)
    np.copyto(merged_data, new_data, where=cloud, casting="unsafe")


# ---------------------------------------------------------------------
# Cloud-mask preservation across a merge (indices and composites)
#
# For these products a cloud pixel is written as plain nodata, which is
# indistinguishable from off-swath nodata, so merge(method="first") fills
# one tile's cloud holes with another tile's valid pixels. To keep every
# tile's cloud mask, each tile's boolean mask is saved as a sidecar
# raster, the sidecars are mosaicked with "max" (cloud in ANY tile = 1),
# and that union is stamped onto the merged product as nodata. True
# off-swath nodata is untouched, so it is still backfilled by other tiles.
# Water extent does not need this: its cloud is a real class (5) handled
# by water_extent_merge_method.
# ---------------------------------------------------------------------

_CLOUD_MASK_DIRNAME = "_cloud_masks"


def cloud_mask_sidecar_path(outfile):
    """Sidecar path for a per-tile product GeoTIFF."""
    return os.path.join(
        os.path.dirname(outfile),
        _CLOUD_MASK_DIRNAME,
        os.path.basename(outfile),
    )


def write_cloud_mask_sidecar(outfile, mask, profile):
    """
    Save a tile's boolean cloud mask on the same grid as `outfile`.
    Only the georeferencing is taken from `profile`.
    """
    path = cloud_mask_sidecar_path(outfile)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    sidecar_profile = {
        key: profile[key] for key in ("crs", "transform", "width", "height")
    }
    sidecar_profile.update(
        driver="GTiff", count=1, dtype="uint8", compress="ZSTD"
    )

    with rio.open(path, "w", **sidecar_profile) as dst:
        dst.write(np.asarray(mask, dtype=np.uint8), 1)

    return path


def apply_cloud_union(merged_path, mask_paths):
    """
    Set every pixel that is cloud in ANY input tile to the mosaic's
    nodata value. Call after merge_products() with the same tiles'
    sidecar paths.
    """
    union_path = merged_path + ".cloudunion.tmp.tif"

    try:
        merge_products(mask_paths, union_path, method="max")

        with rio.open(union_path) as m:
            cloud = m.read(1) == 1

        with rio.open(merged_path, "r+") as dst:

            if dst.nodata is None:
                raise ValueError(
                    "Merged product has no nodata value to stamp the "
                    "cloud mask with."
                )

            if (dst.height, dst.width) != cloud.shape:
                raise RuntimeError(
                    f"Cloud-mask mosaic {cloud.shape} does not match the "
                    f"merged product ({dst.height}, {dst.width})."
                )

            for band_index in range(1, dst.count + 1):
                band = dst.read(band_index)
                band[cloud] = dst.nodata
                dst.write(band, band_index)

    finally:
        if os.path.exists(union_path):
            os.remove(union_path)

    return merged_path


# ---------------------------------------------------------------------
# Cloud-mask product
#
# The cloud / cloud-shadow / thin-cirrus mask on its own, from the L2A
# Scene Classification Layer, at the SCL's native 20 m. Strictly binary:
#
#   1 = cloud, cloud shadow or thin cirrus (SCL 3, 8, 9, 10 -- the same
#       classes get_cloud_mask uses)
#   0 = everything else, which is also the file's nodata value
#
# Because 0 is nodata, clear sky and the off-swath fill are BOTH
# transparent in any viewer, and only cloud is drawn. (A GeoTIFF colour
# table cannot carry transparency, so nodata is what makes the "else"
# transparent; the two cannot be told apart in this product.)
# finalize_cloud_mask() attaches the gray colour for class 1.
# ---------------------------------------------------------------------

CLOUD_MASK_NODATA = 0

# Gray (R=G=B) the cloud class renders in. Used for both the cloud-mask
# colour table and the cloud pixels in RGBA composites.
CLOUD_GRAY = 128


def generate_cloud_mask(
    item,
    output_dir="./s3_temp",
    cloud_classes=(3, 8, 9, 10),
):
    """
    Generate the binary cloud-mask product for one Sentinel-2 L2A scene.

    Parameters
    ----------
    item : pystac.Item
        Sentinel-2 L2A STAC item. Raises if it has no 'scl' asset.

    output_dir : str
        Local directory to write the output GeoTIFF.

    cloud_classes : iterable of int
        SCL classes counted as cloud. Defaults match get_cloud_mask.

    Returns
    -------
    str
        Path to the generated GeoTIFF (uint8: 1 cloud, 0 otherwise and
        nodata).
    """

    os.makedirs(output_dir, exist_ok=True)

    if "scl" not in item.assets:
        raise KeyError(
            f"Asset 'scl' not found in STAC item '{item.id}'. "
            "The cloud-mask product requires the Sentinel-2 L2A Scene "
            "Classification Layer, which does not exist for L1C products."
        )

    href, _, _, _, _ = get_asset_metadata(item.assets["scl"])

    print(f"Reading SCL asset: {href}")

    with rio.Env(_session()):
        with rio.open(href) as src:
            scl = src.read(1)
            src_profile = src.profile

    mask = np.zeros(scl.shape, dtype=np.uint8)

    mask[np.isin(scl, cloud_classes)] = 1

    profile = {
        key: src_profile[key] for key in ("crs", "transform", "width", "height")
    }

    profile.update(
        driver="GTiff",
        count=1,
        dtype="uint8",
        nodata=CLOUD_MASK_NODATA,
        compress="ZSTD",
        tiled=True,
        blockxsize=512,
        blockysize=512,
    )

    outfile = os.path.join(
        output_dir,
        _build_output_filename(item, "cloud_mask"),
    )

    print()
    print("Cloud mask classes:")
    print(f"  1: Cloud:               {np.count_nonzero(mask == 1):,} pixels")
    print(f"  0: Not cloud / no data: {np.count_nonzero(mask == 0):,} pixels")

    print(f"Writing GeoTIFF: {outfile}")

    with rio.open(outfile, "w", **profile) as dst:
        dst.write(mask, 1)

    print(f"Generation completed: {outfile}")

    return outfile


def finalize_cloud_mask(path, gray=CLOUD_GRAY):
    """
    Attach the display colour table to a cloud-mask GeoTIFF (per-tile or
    merged): cloud (1) is gray. Everything else is value 0, which is the
    file's nodata and therefore transparent. Rewrites the file in place.
    Needed after the merge, which does not carry a colour table through.
    """

    with rio.open(path) as src:
        data = src.read(1)
        profile = src.profile.copy()

    colormap = {i: (0, 0, 0, 0) for i in range(256)}
    colormap[1] = (gray, gray, gray, 255)

    tmp_path = path + ".colormap.tmp.tif"

    try:
        with rio.open(tmp_path, "w", **profile) as dst:
            dst.write(data, 1)
            dst.write_colormap(1, colormap)

        os.replace(tmp_path, path)

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return path


# ---------------------------------------------------------------------
# RGBA composites: gray clouds, transparent no data
#
# generate_composite writes cloud and off-swath pixels both as 0, i.e.
# black, and the COG carries no nodata tag. This converts a finished
# composite (per-tile or merged) to 4-band RGBA instead:
#
#   cloud            -> gray RGB, opaque
#   other no data    -> transparent (alpha 0)
#   everything else  -> unchanged, opaque
#
# It runs AFTER the merge because "first" merging cannot be trusted with
# an alpha band: alpha is always valid inside a tile's footprint, so a
# tile's transparent edge would block later tiles from filling it.
# ---------------------------------------------------------------------

def finalize_composite_rgba(path, mask_paths=None, gray=CLOUD_GRAY):
    """
    Convert a 3-band uint8 composite to RGBA in place.

    Parameters
    ----------
    path : str
        The composite GeoTIFF (3 bands, 0 = cloud/no data).

    mask_paths : list of str or None
        Cloud-mask sidecars (see write_cloud_mask_sidecar) for the
        tile(s) in `path`. Cloud in ANY of them is painted gray. None
        when no cloud mask was applied: no-data is still made
        transparent, nothing is painted gray.

    gray : int
        Gray level for cloud pixels.

    Notes
    -----
    A pixel is no-data when all three bands are 0 and it is not cloud.
    """

    cloud = None
    union_path = None

    try:

        if mask_paths:

            if len(mask_paths) == 1:
                cloud_source = mask_paths[0]
            else:
                union_path = path + ".cloudunion.tmp.tif"
                merge_products(list(mask_paths), union_path, method="max")
                cloud_source = union_path

            with rio.open(cloud_source) as m:
                cloud = m.read(1) == 1

        with rio.open(path) as src:
            rgb = src.read()
            src_profile = src.profile

        if rgb.shape[0] != 3:
            raise ValueError(
                f"{path} has {rgb.shape[0]} bands; RGBA conversion "
                f"expects a 3-band composite."
            )

        if cloud is not None and cloud.shape != rgb.shape[1:]:
            raise RuntimeError(
                f"Cloud mask {cloud.shape} does not match the composite "
                f"{rgb.shape[1:]}."
            )

        no_data = (rgb == 0).all(axis=0)

        if cloud is not None:
            no_data &= ~cloud

        alpha = np.where(no_data, 0, 255).astype(np.uint8)

        if cloud is not None:
            rgb[:, cloud] = gray
            alpha[cloud] = 255

        profile = {
            key: src_profile[key]
            for key in ("crs", "transform", "width", "height")
        }

        profile.update(
            driver="GTiff",
            count=4,
            dtype="uint8",
            compress="ZSTD",
            tiled=True,
            blockxsize=512,
            blockysize=512,
        )

        tmp_path = path + ".rgba.tmp.tif"

        try:
            with rio.open(tmp_path, "w", **profile) as dst:
                dst.write(rgb, [1, 2, 3])
                dst.write(alpha, 4)
                dst.colorinterp = (
                    ColorInterp.red,
                    ColorInterp.green,
                    ColorInterp.blue,
                    ColorInterp.alpha,
                )

            os.replace(tmp_path, path)

        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    finally:
        if union_path is not None and os.path.exists(union_path):
            os.remove(union_path)

    return path


# The high-level process_index / process_composite / process_items
# helpers that lived here were removed. They were unreachable dead
# code that could never have run: all three called write_cog_to_s3,
# which is defined nowhere in this repo (NameError on first call),
# and process_index/process_composite additionally unpacked a
# 2-tuple from generate_index/generate_composite, which return a
# single path string. process_sentinel2_odr.main() implements the
# per-item loop directly; publishing to S3 is dps/_finalize.sh's job.
