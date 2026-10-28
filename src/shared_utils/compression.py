"""
Compression module - handles compression settings and nodata values.
Single responsibility: Compression configuration and data type handling.
"""

import numpy as np


# Known float32 "extreme value" nodata patterns we've seen ship from upstream
# satellite pipelines. Some vendors stamp FLT_MAX (or close approximations)
# into the GeoTIFF nodata tag. Those values:
#   - Look like nodata, and gdalwarp / rio cogeo honor them as such.
#   - But downstream `rio_stac.get_dataset_geom` (in veda-data-airflow's
#     build_stac task) treats them as real data, producing
#     "Point outside of projection domain" errors that block STAC ingest.
# The fix is to detect them in cog_utils.convert_to_cog's "use existing source
# nodata" branch and remap to the dtype-appropriate default before they
# propagate.
EXTREME_FLOAT_NODATA = frozenset({
    -3.4028234663852886e+38,
    3.4028234663852886e+38,
    -3.40282346638529e+38,
    3.40282346638529e+38,
    -3.3999999521443642e+38,
    3.3999999521443642e+38,
})


def is_extreme_float_nodata(value) -> bool:
    """
    True if `value` matches one of the known FLT_MAX-class nodata corruption
    patterns. None, non-numeric, and NaN inputs return False (NaN is a
    legitimate nodata sentinel for float bands).

    Tolerant comparison: matches if `abs(value - bad) / abs(bad) < 1e-6` for
    any pattern in `EXTREME_FLOAT_NODATA` — vendors stamp slightly different
    binary representations of "max float" depending on the writer.
    """
    if value is None:
        return False
    if not isinstance(value, (int, float, np.floating, np.integer)):
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    if np.isnan(v) or np.isinf(v):
        return False
    return any(abs(v - bad) / abs(bad) < 1e-6 for bad in EXTREME_FLOAT_NODATA)


# `is_extreme_float_nodata` guards the nodata TAG. This threshold guards the
# other half of the same problem: a file that declares a perfectly sane nodata
# (e.g. -9999) while its fill PIXELS actually hold FLT_MAX. Nothing masks those
# pixels — rio-tiler masks on the declared value — so they reach the renderer as
# real data and clamp to the top of any rescale, painting the masked area solid.
#
# 1e30 is far above any physically meaningful raster value we carry (LOS
# displacement in cm, reflectance, NDVI, building area in m^2) and far below
# FLT_MAX, so it separates fill from data without needing an exact match
# against EXTREME_FLOAT_NODATA's specific binary representations.
EXTREME_FLOAT_ABS_THRESHOLD = 1e30


def detect_extreme_float_fill(src, max_probe_px: int = 4_000_000):
    """
    Look for FLT_MAX-class values in the PIXEL DATA of a float raster.

    Complements `is_extreme_float_nodata`, which only inspects the nodata tag.
    A source can declare nodata=-9999 and still be full of FLT_MAX fill; that
    combination passes every tag-level check and produces a COG whose masked
    area renders as valid data.

    Cost is bounded: reads a decimated window (nearest-neighbour, so fill values
    are never averaged away) capped at `max_probe_px` pixels, pulling from an
    overview when the file has one. That is a small constant read rather than a
    full-raster load.

    Args:
        src: An open rasterio dataset.
        max_probe_px: Approximate ceiling on pixels read.

    Returns:
        The detected fill value as a float, or None if the raster is not a
        float type or carries no extreme values.
    """
    if not str(src.dtypes[0]).startswith('float'):
        return None

    h, w = src.height, src.width
    if h == 0 or w == 0:
        return None

    scale = 1.0
    if h * w > max_probe_px:
        scale = (max_probe_px / float(h * w)) ** 0.5
    out_h = max(1, int(h * scale))
    out_w = max(1, int(w * scale))

    try:
        # Default decimated-read resampling is nearest, which preserves the
        # sentinel exactly. `average` would blend fill with data and could push
        # a genuinely-corrupt file back under the threshold.
        a = src.read(1, out_shape=(out_h, out_w)).astype('float64')
    except Exception:
        return None

    extreme = a[np.isfinite(a) & (np.abs(a) >= EXTREME_FLOAT_ABS_THRESHOLD)]
    if extreme.size == 0:
        return None

    # Return the dominant sentinel rather than the max: a file may carry both
    # +FLT_MAX and a handful of other extremes, and gdalwarp's -srcnodata takes
    # a single value. Use `list_extreme_float_fills` to see them all — a raster
    # with more than one keeps the non-dominant ones after a remap.
    values, counts = np.unique(extreme, return_counts=True)
    return float(values[int(np.argmax(counts))])


def list_extreme_float_fills(src, max_probe_px: int = 4_000_000):
    """
    Every distinct FLT_MAX-class value present in the pixel data, sorted.

    `detect_extreme_float_fill` returns only the dominant one because a single
    gdalwarp `-srcnodata` can name a single value. This reports the full set so
    callers can warn when a remap will necessarily leave some fill behind.

    Args:
        src: An open rasterio dataset.
        max_probe_px: Approximate ceiling on pixels read.

    Returns:
        A sorted list of floats. Empty when the raster is not float or carries
        no extreme values.
    """
    if not str(src.dtypes[0]).startswith('float'):
        return []

    h, w = src.height, src.width
    if h == 0 or w == 0:
        return []

    scale = 1.0
    if h * w > max_probe_px:
        scale = (max_probe_px / float(h * w)) ** 0.5

    try:
        a = src.read(
            1, out_shape=(max(1, int(h * scale)), max(1, int(w * scale)))
        ).astype('float64')
    except Exception:
        return []

    extreme = a[np.isfinite(a) & (np.abs(a) >= EXTREME_FLOAT_ABS_THRESHOLD)]
    if extreme.size == 0:
        return []
    return sorted(float(v) for v in np.unique(extreme))


def validate_nodata_for_dtype(nodata_value, dtype):
    """
    Validate if a no-data value is appropriate for the data type.

    Args:
        nodata_value: No-data value to validate
        dtype: Data type string

    Returns:
        dict: Validation results with 'valid' and 'error' keys
    """
    dtype_str = str(dtype)

    try:
        if dtype_str == 'uint8':
            if not (0 <= nodata_value <= 255):
                return {'valid': False, 'error': f"Value {nodata_value} out of range for uint8 [0, 255]"}
        elif dtype_str == 'uint16':
            if not (0 <= nodata_value <= 65535):
                return {'valid': False, 'error': f"Value {nodata_value} out of range for uint16 [0, 65535]"}
        elif dtype_str == 'int8':
            if not (-128 <= nodata_value <= 127):
                return {'valid': False, 'error': f"Value {nodata_value} out of range for int8 [-128, 127]"}
        elif dtype_str == 'int16':
            if not (-32768 <= nodata_value <= 32767):
                return {'valid': False, 'error': f"Value {nodata_value} out of range for int16 [-32768, 32767]"}
        elif dtype_str == 'int32':
            if not (-2147483648 <= nodata_value <= 2147483647):
                return {'valid': False, 'error': f"Value {nodata_value} out of range for int32"}
        # Float types can handle any numeric value including NaN
        elif dtype_str in ['float32', 'float64']:
            # Accept any numeric value or NaN
            if not (isinstance(nodata_value, (int, float)) or np.isnan(nodata_value)):
                return {'valid': False, 'error': f"Value {nodata_value} must be numeric for float types"}

        return {'valid': True, 'error': None}

    except Exception as e:
        return {'valid': False, 'error': str(e)}


def get_predictor_for_dtype(dtype):
    """
    Determine the appropriate predictor based on data type.

    Args:
        dtype: numpy dtype or string representation of dtype

    Returns:
        int: Predictor value (1, 2, or 3)
    """
    dtype_str = str(dtype)

    # Integer types use predictor 2 (horizontal differencing)
    if dtype_str in ['uint8', 'uint16', 'uint32', 'int8', 'int16', 'int32']:
        return 2

    # Floating-point types use predictor 3 (floating point predictor)
    elif dtype_str in ['float32', 'float64']:
        return 3

    # Default to no predictor
    else:
        return 1


def set_nodata_value(ds):
    """
    Set appropriate nodata value based on data type for a dataset object.

    Args:
        ds: Dataset object with dtype attribute

    Returns:
        Appropriate nodata value for the data type
    """
    print(f"   [NODATA] Data type: {ds.dtype}")

    if ds.dtype == 'uint8':
        # For uint8 data, use 0 as nodata
        nodata_value = 0
        print(f"   [NODATA] Using nodata value {nodata_value} for uint8 data")

    elif ds.dtype == 'uint16':
        # For uint16, use 0 as nodata
        nodata_value = 0
        print(f"   [NODATA] Using nodata value {nodata_value} for uint16 data")

    elif ds.dtype == 'int8':
        # For int8, must use value within -128 to 127 range
        nodata_value = -128
        print(f"   [NODATA] Using nodata value {nodata_value} for int8 data")

    elif ds.dtype == 'int16':
        # For int16, -9999 is fine
        nodata_value = -9999
        print(f"   [NODATA] Using nodata value {nodata_value} for int16 data")

    else:
        # For float32, int32, etc., use -9999
        nodata_value = -9999
        print(f"   [NODATA] Using nodata value {nodata_value} for {ds.dtype} data")

    return nodata_value


def set_nodata_value_src(src, manual_nodata=None):
    """
    Set appropriate nodata value based on data type for a rasterio source.

    Args:
        src: Rasterio source object with dtypes attribute
        manual_nodata: Optional manual no-data value to use

    Returns:
        Appropriate nodata value for the data type
    """
    print(f"   [NODATA] Data type: {src.dtypes[0]}")

    # Use manual no-data if provided and valid
    if manual_nodata is not None:
        validation = validate_nodata_for_dtype(manual_nodata, src.dtypes[0])
        if validation['valid']:
            print(f"   [NODATA] Using manual nodata value {manual_nodata}")
            return manual_nodata
        else:
            print(f"   [NODATA] WARNING: Manual nodata {manual_nodata} invalid for {src.dtypes[0]}")
            print(f"   [NODATA] Reason: {validation['error']}")
            print(f"   [NODATA] Falling back to automatic selection")

    if src.dtypes[0] == 'uint8':
        # For uint8 data, use 0 as nodata
        nodata_value = 0
        print(f"   [NODATA] Using nodata value {nodata_value} for uint8 data")

    elif src.dtypes[0] == 'uint16':
        # For uint16, use 0 as nodata
        nodata_value = 0
        print(f"   [NODATA] Using nodata value {nodata_value} for uint16 data")

    elif src.dtypes[0] == 'int8':
        # For int8, must use value within -128 to 127 range
        nodata_value = -128
        print(f"   [NODATA] Using nodata value {nodata_value} for int8 data")

    elif src.dtypes[0] == 'int16':
        # For int16, -9999 is fine
        nodata_value = -9999
        print(f"   [NODATA] Using nodata value {nodata_value} for int16 data")

    else:
        # For float32, int32, etc., use -9999
        nodata_value = -9999
        print(f"   [NODATA] Using nodata value {nodata_value} for {src.dtypes[0]} data")

    return nodata_value


def get_compression_config(file_size_gb=0, dtype='float32'):
    """
    Get optimal compression configuration based on file size and data type.

    Args:
        file_size_gb: File size in gigabytes
        dtype: Data type string

    Returns:
        dict: Compression configuration
    """
    # Base configuration
    config = {
        'driver': 'GTiff',
        'compress': 'zstd',
        'zstd_level': 22,  # Maximum compression
        'tiled': True,
        'blockxsize': 512,
        'blockysize': 512,
        'bigtiff': 'YES' if file_size_gb > 3 else 'IF_SAFER',
        'num_threads': 'ALL_CPUS'
    }

    # Add predictor based on data type
    config['predictor'] = get_predictor_for_dtype(dtype)

    # Adjust for very large files
    if file_size_gb > 10:
        config['blockxsize'] = 256
        config['blockysize'] = 256

    return config


def export_cog_profile():
    """
    Export standard COG profile configuration.

    Returns:
        dict: COG profile settings
    """
    return {
        'driver': 'GTiff',
        'compress': 'ZSTD',
        'zstd_level': 22,
        'predictor': 2,
        'tiled': True,
        'blockxsize': 512,
        'blockysize': 512,
        'bigtiff': 'IF_SAFER',
        'num_threads': 'ALL_CPUS'
    }


def remap_nodata_value(data, original_nodata, new_nodata, dtype):
    """
    Remap nodata values in an array from original to new value.

    Args:
        data: numpy array with data
        original_nodata: Original nodata value (can be None)
        new_nodata: New nodata value to set
        dtype: Data type of the array

    Returns:
        numpy array with remapped nodata values
    """
    if original_nodata is None or original_nodata == new_nodata:
        return data

    # Create a copy to avoid modifying original
    remapped_data = data.copy()

    # Handle NaN for float types
    dtype_str = str(dtype)
    if 'float' in dtype_str:
        if np.isnan(original_nodata):
            # Replace NaN with new nodata
            mask = np.isnan(remapped_data)
            remapped_data[mask] = new_nodata
        else:
            # Replace original nodata value
            mask = np.isclose(remapped_data, original_nodata, rtol=1e-9, atol=1e-9)
            remapped_data[mask] = new_nodata
    else:
        # For integer types, direct comparison
        mask = (remapped_data == original_nodata)
        remapped_data[mask] = new_nodata

    return remapped_data