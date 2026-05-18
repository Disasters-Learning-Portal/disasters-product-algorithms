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
        normalize_wgs84_crs,
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

# Unified filename / categorization helpers (pure Python, no GDAL).
from shared_utils.file_naming import (
    DATETIME_PATTERNS,
    extract_datetime_from_filename,
    categorize_file,
    create_output_filename,
    no_change,
)

# Web Mercator clip detection (requires rasterio).
try:
    from shared_utils.reprojection import (
        needs_webmerc_clip,
        WEBMERC_VALID_LAT,
        WEBMERC_EXTENT_M,
        WEBMERC_EPSGS,
    )
except ImportError:
    pass  # rasterio not available

# COG metadata (requires rasterio, GDAL, rio-cogeo)
try:
    from shared_utils.cog_metadata import (
        create_cog_with_metadata,
        read_compression_settings,
        validate_cog_in_memory,
    )
except ImportError:
    pass  # rasterio/GDAL/rio-cogeo not available

# S3 utilities (requires boto3; optional helpers require rasterio)
try:
    from shared_utils.s3utils import (
        retrieve_s3_file_list,
        read_s3_file,
        download_s3_file,
        remove_s3_temp,
        parse_s3_uri,
        upload_file_to_s3,
        build_flat_s3_uri,
    )
except ImportError:
    pass  # boto3/rasterio not available

# Raster inspection helpers (requires rasterio)
try:
    from shared_utils.geotiff_analyzer import summarize_raster
except ImportError:
    pass  # rasterio not available

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
    'normalize_wgs84_crs',
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
    # COG metadata
    'create_cog_with_metadata',
    'read_compression_settings',
    'validate_cog_in_memory',
    # S3 utilities
    'retrieve_s3_file_list',
    'read_s3_file',
    'download_s3_file',
    'remove_s3_temp',
    'parse_s3_uri',
    'upload_file_to_s3',
    'build_flat_s3_uri',
    # Unified filename / categorization (file_naming.py)
    'DATETIME_PATTERNS',
    'extract_datetime_from_filename',
    'categorize_file',
    'create_output_filename',
    'no_change',
    # Web Mercator clip detection (reprojection.py)
    'needs_webmerc_clip',
    'WEBMERC_VALID_LAT',
    'WEBMERC_EXTENT_M',
    'WEBMERC_EPSGS',
    # Raster inspection (geotiff_analyzer.py)
    'summarize_raster',
]
