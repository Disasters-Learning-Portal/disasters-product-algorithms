"""
Shared Utilities Module
=======================

Shared utilities for COG conversion, GDAL operations, and geospatial processing.
"""

# COG utilities (requires rasterio)
try:
    from shared_utils.cog_utils import (
        set_nodata_value,
        validate_nodata_for_dtype,
        determine_resampling_method,
        get_compression_profile,
        convert_to_cog,
        validate_cog,
        get_final_filename,
        rename_with_event,
    )
except ImportError:
    pass  # rasterio not available

# Geotools (requires osgeo/GDAL)
try:
    from shared_utils.geotools import (
        bytescale,
        match_geotiff,
        get_geo,
        dump_geotiff_float,
        dump_geotiff_byte,
        dump_geotiff_rgb,
    )
except (ImportError, ModuleNotFoundError):
    pass  # GDAL/osgeo not available

# AWS-conversion modules (optional dependencies such as boto3)
try:
    from shared_utils.main_processor import convert_to_cog as convert_to_cog_s3
    from shared_utils.compression import (
        set_nodata_value as set_nodata_value_src,
        get_predictor_for_dtype,
        remap_nodata_value,
    )
    from shared_utils.cog_validation import (
        check_and_fix_nan_values,
        check_cog_with_warnings,
    )
except ImportError:
    pass  # These modules have optional dependencies (boto3, etc.)

__all__ = [
    # COG utilities
    'set_nodata_value',
    'validate_nodata_for_dtype',
    'determine_resampling_method',
    'get_compression_profile',
    'convert_to_cog',
    'validate_cog',
    'get_final_filename',
    'rename_with_event',
    # Geotools
    'bytescale',
    'match_geotiff',
    'get_geo',
    'dump_geotiff_float',
    'dump_geotiff_byte',
    'dump_geotiff_rgb',
    # AWS-conversion modules (available when dependencies are installed)
    'convert_to_cog_s3',
    'set_nodata_value_src',
    'get_predictor_for_dtype',
    'remap_nodata_value',
    'check_and_fix_nan_values',
    'check_cog_with_warnings',
]
