"""
landsat_odr_functions.py

Landsat Collection 2 Level-2 processing functions, STAC/COG edition.

Reads Landsat surface-reflectance imagery directly from a STAC API
(Earth Search, collection `landsat-c2-l2`) and its cloud-optimized
assets on S3 (requester-pays, s3://usgs-landsat). The CLI and notebooks
should call these functions rather than containing the processing logic
themselves.

The module is standalone: it does not import from the Sentinel-2 module.
Shared utilities needed by Landsat (STAC access, metadata, index formulas,
resampling, display scaling, and merge/cloud-mask helpers) are implemented
locally so this file can run independently. Landsat-specific behavior includes:

    - item-id parsing and output naming (WRS-2 path/row, not MGRS tile)
    - asset reads (item-level GSD fallback, requester-pays session)
    - cloud masking from the QA_PIXEL bit field
    - the index and composite product generators

Landsat C2 L2 is surface reflectance only, so there is no Rayleigh
correction step and no processing-level choice.
"""

import os

import numpy as np
import rasterio as rio
from rasterio.enums import Resampling
from rasterio.merge import merge
from rasterio.session import AWSSession
from rasterio.warp import calculate_default_transform, reproject, transform_bounds
from pystac_client import Client
import boto3
import json
import math
import shutil
import tempfile

# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------
LANDSAT_STAC_API = "https://earth-search.aws.element84.com/v1"
LANDSAT_COLLECTION = "landsat-c2-l2"
MAX_SEARCH_ITEMS = 200
_PRODUCT_FILENAME_TOKENS = {"we": "waterExtent"}
_EVI_G = 2.5
_EVI_C1 = 6.0
_EVI_C2 = 7.5
_EVI_L = 1.0
_CLOUD_MASK_DIRNAME = "_cloud_masks"
_GEOREF_KEYS = ("crs", "transform", "width", "height")
DEFAULT_CLOUD_BITS = (1, 2, 3, 4)
COMPOSITE_LOG_LOW = 5000
COMPOSITE_LOG_HIGH = 35000
CLOUD_GRAY = 128
_aws_session_cache = {}

# ---------------------------------------------------------------------
# Local shared helpers (no Sentinel imports)
# ---------------------------------------------------------------------

def _session(unsigned=False):
    key = bool(unsigned)
    if key not in _aws_session_cache:
        _aws_session_cache[key] = (
            AWSSession(boto3.Session(), aws_unsigned=True)
            if unsigned
            else AWSSession(boto3.Session(), requester_pays=True)
        )
    return _aws_session_cache[key]

def load_algorithms(
    algorithm_file="algorithms-landsat.json",
):
    """
    Load Sentinel-2 algorithm definitions from JSON.

    Parameters
    ----------
    algorithm_file : str
        Path to algorithms-landsat.json.

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
    algorithm_file="algorithms-landsat.json",
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
        Path to algorithms-landsat.json.

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

def _to_camel_case(snake_str):
    """
    Convert a snake_case algorithm name (e.g. 'true_color') to
    camelCase (e.g. 'trueColor') for use in output filenames.
    """
    parts = snake_str.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])

def _product_token(algorithm_name):
    """Filename token for an algorithm key (see _PRODUCT_FILENAME_TOKENS)."""
    return _PRODUCT_FILENAME_TOKENS.get(
        algorithm_name,
        _to_camel_case(algorithm_name),
    )

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

def _first_with_priority_value(priority_value):
    """
    Merge method equivalent to rasterio's "first", except that pixels
    equal to `priority_value` in ANY input survive into the mosaic.
    Used to keep class 5 (cloud) from every tile in water extent.
    """

    def method(
        merged_data,
        new_data,
        merged_mask,
        new_mask,
        index=None,
        roff=None,
        coff=None,
    ):
        # Stock "first": fill only where the mosaic is still nodata
        # and the new tile has valid data.
        fill = merged_mask & ~new_mask
        np.copyto(merged_data, new_data, where=fill, casting="unsafe")

        # Priority overlay: the new tile's priority pixels always win.
        keep = ~new_mask & (new_data == priority_value)
        np.copyto(merged_data, new_data, where=keep, casting="unsafe")

    return method

def merge_products(tif_paths, output_path, method="first", priority_value=None):

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

        if priority_value is not None:
            method = _first_with_priority_value(priority_value)

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


def finalize_cloud_mask(path, gray=CLOUD_GRAY):
    """Color cloud-mask value 1 gray and make value 0 transparent."""
    with rio.open(path) as src:
        data = src.read(1)
        profile = src.profile.copy()

    profile.update(count=1, dtype="uint8", nodata=0)
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


def finalize_composite_rgba(path, mask_paths=None, gray=CLOUD_GRAY):
    """Convert a 3-band composite to RGBA: gray opaque clouds, transparent no-data."""
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
            with rio.open(cloud_source) as src:
                cloud = src.read(1) == 1

        with rio.open(path) as src:
            rgb = src.read()
            profile = src.profile.copy()

        if rgb.shape[0] != 3:
            raise ValueError(f"{path} has {rgb.shape[0]} bands; expected a 3-band composite.")
        if cloud is not None and cloud.shape != rgb.shape[1:]:
            raise RuntimeError(f"Cloud mask {cloud.shape} does not match composite {rgb.shape[1:]}.")

        no_data = (rgb == 0).all(axis=0)
        if cloud is not None:
            no_data &= ~cloud
            rgb[:, cloud] = gray

        alpha = np.where(no_data, 0, 255).astype(np.uint8)
        rgba = np.concatenate([rgb, alpha[np.newaxis, :, :]], axis=0)
        profile.update(count=4, dtype="uint8", nodata=None, photometric="RGB", alpha="YES")
        tmp_path = path + ".rgba.tmp.tif"
        try:
            with rio.open(tmp_path, "w", **profile) as dst:
                dst.write(rgba)
                dst.colorinterp = (
                    rio.enums.ColorInterp.red,
                    rio.enums.ColorInterp.green,
                    rio.enums.ColorInterp.blue,
                    rio.enums.ColorInterp.alpha,
                )
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
    finally:
        if union_path and os.path.exists(union_path):
            os.remove(union_path)
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
# Landsat constants
# ---------------------------------------------------------------------

# QA_PIXEL bit positions (Landsat C2 L2, 16-bit field).
#   0 fill          1 dilated cloud   2 cirrus (L8/9 only)
#   3 cloud         4 cloud shadow    5 snow
#   6 clear         7 water
# Bit 2 is unused on Landsat 4-7, so it is always 0 there and harmless.
#
# Default mask = dilated cloud, cirrus, cloud, cloud shadow. This is the
# Landsat analogue of the Sentinel-2 SCL set (shadow, cloud, thin
# cirrus); "dilated cloud" is the extra cloud-edge buffer Landsat
# provides and SCL does not. Drop bit 1 to get a tighter mask.

# Display stretch for composites, in RAW DN (Landsat C2 L2 scale
# 2.75e-5, offset -0.2). Carried over unchanged from the original
# composites notebook.

# ---------------------------------------------------------------------
# STAC search
# ---------------------------------------------------------------------

def search_landsat(
    start_date,
    end_date,
    bbox,
    cloud_cover=50,
    stac_api_url=LANDSAT_STAC_API,
    collection_id=LANDSAT_COLLECTION,
    max_items=MAX_SEARCH_ITEMS,
):
    """
    Search the Landsat STAC catalog.

    Parameters
    ----------
    start_date, end_date : str
        Search window (YYYY-MM-DD).

    bbox : list
        [xmin, ymin, xmax, ymax]

    cloud_cover : float
        Maximum allowed scene cloud cover percentage.

    max_items : int or None
        Hard cap on scenes returned; None disables it. Each scene is a
        full Landsat footprint that the caller will download and process.

    Returns
    -------
    list
        List of STAC Items.
    """

    catalog = connect_to_stac(stac_api_url)

    query = {"eo:cloud_cover": {"lt": cloud_cover}}

    print("Searching Landsat STAC catalog...")
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

    print(f"Found {len(items)} Landsat items.")

    if max_items is not None and len(items) == max_items:
        print(
            f"  WARNING: hit the {max_items}-scene cap. There may be "
            f"more matching scenes that are NOT being processed. "
            f"Narrow --bbox or the date range, or lower --cloud-cover."
        )

    for item in items:
        print(f"  {item.id}")

    return items

def search_landsat_with_catalog(
    start_date,
    end_date,
    bbox,
    cloud_cover=50,
    stac_api_url=LANDSAT_STAC_API,
    collection_id=LANDSAT_COLLECTION,
    max_items=MAX_SEARCH_ITEMS,
):
    """
    Search Landsat and return the search object, items, and the STAC
    ItemCollection dict (for GeoDataFrame processing / visualization).

    NOTE the different return shape from `search_landsat`, which returns
    the item list alone: this returns a 3-tuple (search, items,
    stac_json).
    """

    catalog = connect_to_stac(stac_api_url)

    query = {"eo:cloud_cover": {"lt": cloud_cover}}

    search = catalog.search(
        collections=[collection_id],
        datetime=[start_date, end_date],
        bbox=bbox,
        query=query,
        max_items=max_items,
    )

    items = list(search.items())
    stac_json = search.item_collection_as_dict()

    print(f"Found {len(items)} Landsat items.")

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
# Output naming convention
# ---------------------------------------------------------------------

def _get_sat_level_tile(item):
    """
    Parse SAT, LEVEL, and path/row from a Landsat STAC item id.

        e.g. "LC09_L2SP_018040_20240923_02_T1"
              SAT  LEVEL  PATHROW DATE   COLL TIER

    Returns
    -------
    sat : str
        e.g. "LC09"
    level : str
        e.g. "L2SP" (or "L2SR" when no thermal product exists)
    tile : str
        WRS-2 path+row, e.g. "018040". This takes the place of the
        Sentinel-2 MGRS tile token in output filenames.
    """

    parts = item.id.split("_")

    if len(parts) < 6 or not parts[2].isdigit():
        raise ValueError(
            f"Unexpected Landsat item id format: '{item.id}'"
        )

    return parts[0], parts[1], parts[2]

def _build_output_filename(
    item,
    algorithm_name,
    masked=False,
    merged=False,
    variant=None,
):
    """
    Build a Landsat output filename matching the Sentinel-2 convention:

        Plain:              SAT_LEVEL_product_PATHROW_TIMESTAMP.tif
        Masked:             SAT_LEVEL_product_PATHROW_masked_TIMESTAMP.tif
        Merged:             SAT_LEVEL_product_merged_TIMESTAMP.tif
        Merged and masked:  SAT_LEVEL_product_merged_masked_TIMESTAMP.tif

    `variant` (e.g. "NSTD_1_5") goes directly after the product so
    several parameterisations of one product on one scene cannot
    overwrite each other. When merged=True the path/row token is
    dropped, since a mosaic spans several scenes; pass the first item
    (by sort order) in that case.
    """

    sat, level, tile = _get_sat_level_tile(item)
    product = _product_token(algorithm_name)
    timestamp = item.datetime.replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    variant_token = f"_{variant}" if variant else ""
    tile_token = "" if merged else f"_{tile}"
    merged_token = "_merged" if merged else ""
    masked_token = "_masked" if masked else ""

    return (
        f"{sat}_{level}_{product}{variant_token}{tile_token}"
        f"{merged_token}{masked_token}_{timestamp}.tif"
    )

# ---------------------------------------------------------------------
# Raster reading
# ---------------------------------------------------------------------

# Intermediate GeoTIFFs are written with only the georeferencing carried
# over from the source. Landsat COGs ship with their own compression /
# predictor / interleave settings, and inheriting them is how a float32
# index ends up with an integer-only predictor. Intermediates are
# ZSTD-tiled instead (uncompressed full Landsat scenes are large and the
# DPS outdir is bounded); convert_to_cog recompresses them afterwards.

def _output_profile(src_profile, **overrides):
    profile = {key: src_profile[key] for key in _GEOREF_KEYS}

    profile.update(
        driver="GTiff",
        compress="ZSTD",
        tiled=True,
        blockxsize=512,
        blockysize=512,
    )

    profile.update(overrides)

    return profile

def read_algorithm_band(
    asset,
    item_gsd=None,
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

    item_gsd : float or None
        Item-level `gsd` (item.properties["gsd"]). Landsat assets do not
        reliably carry their own gsd, so this is the fallback. All
        Landsat C2 L2 surface-reflectance bands are 30 m, so resampling
        is a no-op in practice; it is kept so the catalog's `resample`
        flag behaves the same as in the Sentinel-2 pipeline.

    target_gsd : float or None
        Ground sample distance to resample to. None reads native.

    resample : bool
        Whether to honour target_gsd at all.

    apply_scale : bool
        If True, return float32 reflectance with source-nodata pixels
        set to NaN. If False, return the raw DN array untouched (used by
        water extent and composites, whose thresholds are defined in DN).

    Returns
    -------
    band : numpy.ndarray
    profile : dict

    Notes
    -----
    The conversion is ``raw * scale + offset`` with scale/offset read
    from the asset's raster:bands (Landsat C2 L2 SR: 2.75e-05, -0.2).
    The offset is ADDITIVE per both the STAC raster extension and GDAL.
    Source nodata (DN 0) becomes NaN rather than the physical value 0 DN
    would map to (-0.2), so off-swath fill never looks like data.
    """

    href, scale, offset, gsd, src_nodata = get_asset_metadata(asset)

    if scale is None:
        scale = 1.0
    if offset is None:
        offset = 0.0
    if gsd is None:
        gsd = item_gsd
    if gsd is None:
        raise ValueError(
            f"No GSD on STAC asset or item for: {href}"
        )

    # Landsat C2 L2 lives in a requester-pays bucket; _session() sets
    # requester-pays by default.
    with rio.Env(_session()):
        print("Trying to open:", href)

        with rio.open(href) as src:

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

                # Nearest so the 0-fill wedge is not blended into
                # valid pixels.
                band, profile = resample_array(
                    src,
                    resample_ratio,
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

def get_cloud_mask(item, out_shape, cloud_bits=DEFAULT_CLOUD_BITS):
    """
    Build a boolean cloud / cloud-shadow / cirrus mask from the Landsat
    QA_PIXEL asset, resampled with nearest-neighbor (QA is a categorical
    bit field) to match out_shape.

    Parameters
    ----------
    item : pystac.Item
        Landsat C2 L2 STAC item. Raises if it has no 'qa_pixel' asset.

    out_shape : tuple
        (height, width) to match the array the mask is applied to.

    cloud_bits : iterable of int
        QA_PIXEL bit positions that count as masked. See
        DEFAULT_CLOUD_BITS.

    Returns
    -------
    numpy.ndarray (bool)
        True where any selected QA bit is set.
    """

    if "qa_pixel" not in item.assets:
        raise KeyError(
            f"Asset 'qa_pixel' not found in STAC item '{item.id}'. "
            "Cloud masking requires the Landsat QA_PIXEL band."
        )

    href, _, _, _, _ = get_asset_metadata(item.assets["qa_pixel"])

    print(f"Building cloud mask from QA_PIXEL asset: {href}")

    bitmask = 0
    for bit in cloud_bits:
        bitmask |= 1 << int(bit)

    with rio.Env(_session()):
        with rio.open(href) as src:
            qa = src.read(
                1,
                out_shape=out_shape,
                resampling=Resampling.nearest,
            )

    return (qa.astype(np.uint16) & bitmask) != 0

def _bounds_wgs84(profile):
    """Scene bounds (xmin, ymin, xmax, ymax) in EPSG:4326."""

    transform = profile["transform"]

    left = transform.c
    top = transform.f
    right = left + profile["width"] * transform.a
    bottom = top + profile["height"] * transform.e

    return transform_bounds(
        profile["crs"],
        "EPSG:4326",
        left,
        bottom,
        right,
        top,
    )

# ---------------------------------------------------------------------
# Index processing
# ---------------------------------------------------------------------

def generate_index(
    item,
    algorithm,
    algorithm_name,
    output_dir="./s3_temp",
    nodata=-9999,
    cloud_mask=False,
):
    """
    Generate a Landsat index and save it as a GeoTIFF.

    Parameters
    ----------
    item : pystac.Item
        Landsat STAC item.

    algorithm : dict
        Algorithm configuration (assets, formula, min/max, etc.).

    algorithm_name : str
        Product key as used in the catalog (e.g. "ndvi"); used for the
        output filename.

    output_dir : str
        Local directory to write the output GeoTIFF.

    nodata : float
        Output nodata value.

    cloud_mask : bool
        If True, set cloud / shadow / cirrus pixels (QA_PIXEL) to
        `nodata`.

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

    bands = []

    for band_name in algorithm_assets:

        if band_name not in item.assets:
            raise KeyError(
                f"Asset '{band_name}' not found in STAC item "
                f"'{item.id}'."
            )

        band, profile = read_algorithm_band(
            item.assets[band_name],
            item_gsd=item.properties.get("gsd"),
            target_gsd=algorithm.get("gsd"),
            resample=algorithm.get("resample", False),
            apply_scale=True,
        )

        bands.append(band)

    print(f"Calculating index (formula: {formula_name})...")

    values, valid = formula["fn"](bands)

    minimum = algorithm.get("min", -1.0)
    maximum = algorithm.get("max", 1.0)

    if formula["out_of_range"] == "clip":
        values = np.clip(values, minimum, maximum)
    else:
        valid = valid & (values >= minimum) & (values <= maximum)

    index = np.full(values.shape, nodata, dtype=np.float32)

    index[valid] = values[valid]

    mask = None

    if cloud_mask:
        print("Applying cloud mask...")
        mask = get_cloud_mask(item, out_shape=index.shape)
        index[mask] = nodata

    profile = _output_profile(
        profile,
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

def generate_composite(
    item,
    algorithm,
    algorithm_name,
    output_dir="./s3_temp",
    cloud_mask=False,
):
    """
    Generate a Landsat 3-band composite and save it as a uint8 GeoTIFF.

    Bands are read as RAW DN and log-stretched (COMPOSITE_LOG_LOW /
    COMPOSITE_LOG_HIGH) to 0-255, as in the original composites
    notebook. Nodata and masked pixels come out as 0.

    Parameters
    ----------
    item : pystac.Item
        Landsat STAC item.

    algorithm : dict
        Algorithm configuration (assets, gsd, resample).

    algorithm_name : str
        Product key as used in the catalog (e.g. "true_color").

    output_dir : str
        Local directory to write the output GeoTIFF.

    cloud_mask : bool
        If True, zero cloud / shadow / cirrus pixels (QA_PIXEL) in each
        source band before stretching, so they fall out as nodata=0.

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
            item_gsd=item.properties.get("gsd"),
            target_gsd=algorithm.get("gsd"),
            resample=algorithm.get("resample", False),
            apply_scale=False,
        )

        bands.append(band)

    mask = None

    if cloud_mask:
        print("Applying cloud mask...")
        mask = get_cloud_mask(item, out_shape=bands[0].shape)
        for band in bands:
            band[mask] = 0

    print("Applying logarithmic display scaling...")

    rgb = np.asarray(bands)

    array = apply_log_scale(
        rgb,
        low=COMPOSITE_LOG_LOW,
        high=COMPOSITE_LOG_HIGH,
    )

    # apply_log_scale returns NaN for nodata. Resolve it BEFORE the
    # uint8 cast: NaN -> integer is undefined in numpy.
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

    profile = _output_profile(
        profile,
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
