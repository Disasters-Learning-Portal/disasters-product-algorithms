"""
Shared Utilities Module
=======================

Shared utilities for COG conversion, GDAL operations, and geospatial processing.
"""

from shared_utils.cog_utils import (
    set_nodata_value,
    validate_nodata_for_dtype,
    get_compression_profile,
    convert_to_cog,
    validate_cog,
    get_final_filename,
    rename_with_event,
)

from shared_utils.geotools import (
    bytescale,
    match_geotiff,
    get_geo,
    dump_geotiff_float,
    dump_geotiff_byte,
    dump_geotiff_rgb,
)

__all__ = [
    # COG utilities
    'set_nodata_value',
    'validate_nodata_for_dtype',
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
]
