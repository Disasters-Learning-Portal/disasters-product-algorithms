"""
Cloud Optimized GeoTIFF (COG) utilities for Landsat processing.

Based on implementation patterns from disasters-aws-conversion repository.
"""

import os
import shutil
import subprocess
import tempfile
import rasterio
import numpy as np
import re
from datetime import datetime
from typing import Dict, Optional, Union, Tuple


_INTEGER_DTYPE_DEFAULTS = {
    'uint8': 0,
    'uint16': 0,
    'uint32': 0,
    'uint64': 0,
    'int8': -128,
    'int16': -9999,
    'int32': -9999,
    'int64': -9999,
}

# Matches a trailing ISO 8601 Zulu datetime, e.g. "...2025-09-22T18:56:17Z".
# Its presence at the end of a filename is itself the "already renamed"
# marker for the new convention - no _day suffix needed alongside it.
_ISO_ZULU_END_RE = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')


def set_nodata_value(dtype: str, manual_nodata: Optional[Union[int, float]] = None) -> Union[int, float]:
    """
    Automatically select appropriate no-data value based on data type.

    Args:
        dtype: Rasterio/numpy data type string (e.g., 'uint8', 'int16', 'float32')
        manual_nodata: Optional manual no-data value. If provided and valid
            for the given dtype, returned as-is. If provided and invalid,
            ignored — falls through to auto-detect (legacy behavior).

    Returns:
        Appropriate no-data value for the data type.

    Raises:
        ValueError: dtype isn't a recognized integer or float family
            (e.g. 'complex64', 'bool', or a typo). Caller must pass a
            valid `manual_nodata` explicitly for those dtypes.
    """
    # Use manual no-data if provided and valid
    if manual_nodata is not None:
        validation = validate_nodata_for_dtype(manual_nodata, dtype)
        if validation['valid']:
            return manual_nodata

    dtype_str = str(dtype).lower()

    if dtype_str in _INTEGER_DTYPE_DEFAULTS:
        return _INTEGER_DTYPE_DEFAULTS[dtype_str]
    if 'float' in dtype_str:
        return -9999.0
    # complex64, complex128, bool, weird typos, ... — refuse to guess.
    raise ValueError(
        f"No default nodata for dtype {dtype!r}. Supported dtypes: "
        f"{sorted(_INTEGER_DTYPE_DEFAULTS)} + float* family. "
        f"Pass manual_nodata=<value> explicitly to override."
    )


def validate_nodata_for_dtype(nodata: Union[int, float], dtype: str) -> dict:
    """
    Validate that a no-data value is valid for the given data type.

    Args:
        nodata: No-data value to validate
        dtype: Rasterio/numpy data type string

    Returns:
        dict with keys: 'valid' (bool), 'error' (str or None)

    Based on disasters-aws-conversion/lib/core/compression.py:validate_nodata_for_dtype()
    """
    dtype_str = str(dtype).lower()

    try:
        nodata = float(nodata)
    except (TypeError, ValueError):
        return {'valid': False, 'error': f"Cannot convert {nodata} to numeric value"}

    if dtype_str == 'uint8':
        if not (0 <= nodata <= 255):
            return {'valid': False, 'error': f"Value {nodata} out of range for uint8 [0, 255]"}
    elif dtype_str == 'uint16':
        if not (0 <= nodata <= 65535):
            return {'valid': False, 'error': f"Value {nodata} out of range for uint16 [0, 65535]"}
    elif dtype_str == 'int8':
        if not (-128 <= nodata <= 127):
            return {'valid': False, 'error': f"Value {nodata} out of range for int8 [-128, 127]"}
    elif dtype_str == 'int16':
        if not (-32768 <= nodata <= 32767):
            return {'valid': False, 'error': f"Value {nodata} out of range for int16 [-32768, 32767]"}
    elif dtype_str == 'int32':
        if not (-2147483648 <= nodata <= 2147483647):
            return {'valid': False, 'error': f"Value {nodata} out of range for int32"}
    elif 'float' in dtype_str:
        # Float types can use any numeric value including NaN
        if not (isinstance(nodata, (int, float)) or np.isnan(nodata)):
            return {'valid': False, 'error': f"Value {nodata} must be numeric for float types"}
    else:
        # Strict for unknown dtypes — previously this branch silently returned
        # valid=True for typos like 'WeirdType', which let invalid nodata
        # values through unchecked.
        return {
            'valid': False,
            'error': (
                f"Unknown dtype {dtype!r} — supported: uint8/uint16/uint32/"
                f"uint64, int8/int16/int32/int64, float*. If this dtype is real, "
                f"add it to validate_nodata_for_dtype's ladder."
            ),
        }

    return {'valid': True, 'error': None}


def determine_resampling_method(src_path: str) -> Tuple[str, str]:
    """
    Auto-detect appropriate resampling method based on data characteristics.

    Args:
        src_path: Path to source raster file

    Returns:
        Tuple of (resampling_method, overview_resampling):
        - resampling_method: 'cubic', 'bilinear', or 'nearest'
        - overview_resampling: 'average' or 'mode'

    Logic:
        - 3-band data (RGB imagery) -> cubic / average
        - Single-band continuous data -> bilinear / average
        - Single-band categorical data -> nearest / mode
    """
    def _overview_for(method: str) -> str:
        if method == 'nearest':
            return 'mode'
        return 'average'

    try:
        with rasterio.open(src_path) as src:
            # 3-band imagery (RGB products)
            if src.count == 3:
                method = 'cubic'
                return method, _overview_for(method)

            # Single-band data - determine if categorical or continuous
            elif src.count == 1:
                dtype = str(src.dtypes[0]).lower()
                nodata = src.nodata
                filename = os.path.basename(src_path).lower()

                # Heuristics for categorical data
                # Check filename for categorical indicators
                categorical_keywords = ['mask', 'extent', 'classification', 'scl', 'qa']
                if any(keyword in filename for keyword in categorical_keywords):
                    return 'nearest', 'mode'

                # Check nodata value (999 and 255 common for categorical)
                if nodata in [999, 255] and dtype in ['uint8', 'uint16', 'int8']:
                    return 'nearest', 'mode'

                # Default to bilinear for continuous single-band data
                return 'bilinear', 'average'

            # Multi-band but not 3 (rare case)
            return 'bilinear', 'average'

    except Exception as e:
        print(f"  Warning: Could not determine resampling method: {e}")
        print("  Defaulting to bilinear resampling")
        return 'bilinear', 'average'


def get_compression_profile(
    compression: str = 'ZSTD',
    compression_level: int = 22,
    dtype: Optional[str] = None,
    file_size_gb: Optional[float] = None,
) -> dict:
    """
    Get compression profile for COG creation.

    Args:
        compression: Compression type (DEFLATE, LZW, ZSTD, etc.)
        compression_level: Compression level (default: 22 for ZSTD, 9 for others)
        dtype: Optional data type string. If it contains 'float', predictor 3
            (floating point) is used; if 'int'/'uint', predictor 2 (horizontal
            differencing). Default (None) uses predictor 2.
        file_size_gb: Optional file size in GB. Files >10 GB get 256x256 block
            size; files >3 GB get bigtiff='YES'.

    Returns:
        Dictionary of compression options for rio cogeo
    """
    valid_compressions = ['DEFLATE', 'LZW', 'ZSTD', 'JPEG', 'WEBP', 'NONE']

    if compression.upper() not in valid_compressions:
        print(f"Warning: Invalid compression '{compression}', using ZSTD")
        compression = 'ZSTD'

    # Determine predictor based on dtype
    if dtype is not None:
        dtype_lower = str(dtype).lower()
        if 'float' in dtype_lower:
            predictor = '3'  # Floating point predictor
        elif 'int' in dtype_lower or 'uint' in dtype_lower:
            predictor = '2'  # Horizontal differencing
        else:
            predictor = '2'
    else:
        predictor = '2'  # Default: horizontal differencing

    profile = {
        'compress': compression.upper(),
        'predictor': predictor,
        'level': compression_level,
    }

    # Adjust for large files
    if file_size_gb is not None:
        if file_size_gb > 10:
            profile['blockxsize'] = 256
            profile['blockysize'] = 256
        if file_size_gb > 3:
            profile['bigtiff'] = 'YES'

    return profile


def _build_cog_translate_profile(compression: str, compression_level: int) -> dict:
    """
    Build a rio_cogeo profile dict that matches what the subprocess
    `rio cogeo create ... --co PREDICTOR=2 --co ZSTD_LEVEL=N` path produces.

    Used only when `convert_to_cog` is invoked with `metadata`, because
    embedding GeoTIFF tags requires the Python `cog_translate` API
    (the CLI has no flag for arbitrary metadata). Empirical: post-step
    `gdal.SetMetadata` on a finished COG breaks the COG layout in
    GDAL 3.10+.
    """
    from rio_cogeo.profiles import cog_profiles

    profile = dict(cog_profiles.get(compression.lower()))
    if compression.upper() == 'DEFLATE':
        profile['PREDICTOR'] = 2
        profile['ZLEVEL'] = compression_level
    elif compression.upper() == 'LZW':
        profile['PREDICTOR'] = 2
    elif compression.upper() == 'ZSTD':
        profile['PREDICTOR'] = 2
        profile['ZSTD_LEVEL'] = compression_level
    return profile


def convert_to_cog(
    input_tif: str,
    output_cog: Optional[str] = None,
    nodata: Optional[Union[int, float]] = None,
    dst_crs: Optional[str] = 'EPSG:3857',
    resampling_method: Optional[str] = None,
    clip_to_webmerc: Optional[bool] = None,
    compression: str = 'ZSTD',
    compression_level: int = 22,
    overview_levels: int = 5,
    quiet: bool = False,
    backend: str = 'rio',
    metadata: Optional[Dict[str, str]] = None,
    strict_nodata: bool = True,
) -> str:
    """
    Convert a GeoTIFF to Cloud Optimized GeoTIFF (COG) format with optional reprojection.

    Args:
        input_tif: Path to input GeoTIFF file
        output_cog: Path to output COG file (if None, replaces input file)
        nodata: No-data value (if None, auto-detects from file or data type)
        dst_crs: Target CRS (default: 'EPSG:3857', None to preserve native CRS).
            Web Mercator avoids the WGS 84 ensemble / lat-first axis bug that
            breaks rio_stac.get_dataset_geom in veda-data-airflow build_stac.
        resampling_method: Warp resampling ('near', 'bilinear', 'cubic',
            'average'). If None, auto-detected via determine_resampling_method().
        clip_to_webmerc: Clip output extent to Web Mercator's valid domain
            (±20037508.34 m / ±85.05° latitude). True/False to force; None to
            auto-detect via reprojection.needs_webmerc_clip() — required for
            world-extent sources (e.g. global Mollweide) when dst_crs is
            EPSG:3857, no-op for regional rasters that already fit.
        compression: Compression type (default: ZSTD)
        compression_level: Compression level (default: 22 for ZSTD)
        overview_levels: Number of overview levels (default: 5, minimum)
        quiet: Suppress output messages
        backend: Backend to use for COG creation. 'rio' (default) uses rio-cogeo
            CLI, 'gdal' delegates to shared_utils.gdal_cog_processor.create_cog_gdal.
        metadata: Optional dict of activation-event tags to embed in the output
            COG (e.g. {'ACTIVATION_EVENT': '202501_Flood_CA', 'SOURCE': 'USGS',
            'PROCESSOR': 'NASA Disasters COG Processor v1.0.0'}). When provided,
            convert_to_cog routes through the in-process `rio_cogeo.cog_translate`
            (not the `rio cogeo create` subprocess) so the tags land at COG
            creation time. Auto-augments YEAR_MONTH/HAZARD/LOCATION/PROCESSING_DATE
            via shared_utils.cog_metadata.resolve_metadata. Not supported on the
            'gdal' backend yet.
        strict_nodata: When True (default), an out-of-range caller-supplied
            `nodata` raises ValueError up front instead of printing a warning
            and pretending to continue. Set False for the legacy warn-only
            behavior — though in practice rasterio itself rejects out-of-range
            nodata values at the gdalwarp / rio_cogeo step, so `strict_nodata=
            False` mostly just moves the crash site downstream. The kwarg
            exists as a forward-looking signal in case a future rasterio
            relaxes those checks. The default was flipped 2026-06-01 because
            the prior silent-fail mode produced a confusing warning then a
            cryptic rasterio crash several stack frames later.

    Returns:
        Path to created COG file
    """
    if metadata is not None and backend == 'gdal':
        raise NotImplementedError(
            "metadata embedding is not yet supported on backend='gdal'. "
            "Use backend='rio' (the default)."
        )

    # Normalize `dst_crs`: treat string 'None' / 'none' / '' as actual None.
    # Mirrors the same coercion in main_processor.convert_to_cog so notebooks
    # don't have to repeat the `_dst_crs = TARGET_CRS if (...) else None`
    # boilerplate at every call site.
    if isinstance(dst_crs, str) and dst_crs.strip().lower() in ('none', ''):
        dst_crs = None
    # GDAL virtual filesystem prefixes (/vsis3/, /vsicurl/, /vsigs/, ...) bypass
    # the existence check — rasterio + gdalwarp open them natively when the
    # caller is streaming a remote object instead of downloading first.
    is_vsi = isinstance(input_tif, str) and input_tif.startswith('/vsi')
    if not is_vsi and not os.path.exists(input_tif):
        raise FileNotFoundError(f"Input file not found: {input_tif}")

    # GDAL backend delegation
    if backend == 'gdal':
        from shared_utils.gdal_cog_processor import create_cog_gdal
        final_output = output_cog if output_cog is not None else input_tif
        success = create_cog_gdal(
            input_path=input_tif,
            output_path=final_output,
            nodata=nodata,
            compress=compression,
            compress_level=compression_level,
            reproject_to_4326=(dst_crs == 'EPSG:4326') if dst_crs else False,
            verbose=not quiet,
        )
        if not success:
            raise RuntimeError(f"GDAL backend failed to create COG for {input_tif}")
        return final_output

    # Determine output path
    if output_cog is None:
        # Replace input file with COG version
        output_cog = input_tif
        temp_output = os.path.join('/tmp', os.path.basename(input_tif) + '.cog.tmp.tif')
    else:
        temp_output = output_cog

    # Read input file metadata and check if reprojection is needed
    warped_file = None
    input_for_cog = input_tif

    from shared_utils.reprojection import (
        needs_webmerc_clip, WEBMERC_EXTENT_M,
    )

    with rasterio.open(input_tif) as src:
        dtype = src.dtypes[0]
        existing_nodata = src.nodata
        src_crs = src.crs

        # Determine no-data value
        if nodata is None:
            from shared_utils.compression import is_extreme_float_nodata
            if existing_nodata is not None and is_extreme_float_nodata(existing_nodata):
                # Known FLT_MAX corruption pattern — remap before it
                # propagates to gdalwarp / veda-data-airflow.
                remapped = set_nodata_value(dtype)
                print(
                    f"  WARNING: source nodata={existing_nodata!r} matches "
                    f"FLT_MAX corruption pattern; remapping to dtype default "
                    f"({remapped}). See shared_utils.compression."
                    f"EXTREME_FLOAT_NODATA for the known-bad value set."
                )
                nodata = remapped
            elif existing_nodata is not None:
                nodata = existing_nodata
                if not quiet:
                    print(f"  Using existing no-data value: {nodata}")
            else:
                nodata = set_nodata_value(dtype)
                if not quiet:
                    print(f"  Auto-selected no-data value for {dtype}: {nodata}")
        else:
            # Validate user-provided no-data
            validation = validate_nodata_for_dtype(nodata, dtype)
            if not validation['valid']:
                if strict_nodata:
                    raise ValueError(
                        f"nodata={nodata} is invalid for dtype {dtype}: "
                        f"{validation['error']}. Pass a valid value, or set "
                        f"strict_nodata=False to suppress (legacy behavior)."
                    )
                print(f"  Warning: No-data value {nodata} may be invalid for {dtype}")

        # Check if reprojection is needed
        needs_reprojection = (dst_crs is not None and
                             src_crs is not None and
                             str(src_crs).upper() != dst_crs.upper())

        # Decide whether to clip output to Web Mercator's valid domain.
        # `clip_to_webmerc=None` (default) defers to auto-detect; pass True/False
        # to force-override.
        if clip_to_webmerc is None:
            clip_webmerc = needs_reprojection and needs_webmerc_clip(src, dst_crs)
        else:
            clip_webmerc = bool(clip_to_webmerc) and needs_reprojection

        if not quiet:
            print(f"  Data type: {dtype}")
            print(f"  No-data value: {nodata}")
            print(f"  Source CRS: {src_crs}")
            if dst_crs:
                print(f"  Target CRS: {dst_crs}")
                if needs_reprojection:
                    print(f"  Reprojection: Required")
                    if clip_webmerc:
                        print(f"  Web Mercator clip: enabled (source exceeds ±85° lat)")
                else:
                    print(f"  Reprojection: Not needed (already in target CRS)")
            print(f"  Compression: {compression} (level {compression_level})")
            print(f"  Overview levels: {overview_levels}")

    # Default overview resampling (may be overridden during reprojection)
    overview_resampling = 'average'

    # Step 1: Reproject if needed (warp to dst_crs)
    if needs_reprojection:
        warped_file = os.path.join('/tmp', os.path.basename(input_tif) + '.warped.tmp.tif')

        # Resolve resampling: explicit override > auto-detect from file content.
        if resampling_method is None:
            resampling_method, overview_resampling = determine_resampling_method(input_tif)
            if not quiet:
                print(f"  Resampling method: {resampling_method} (auto-detected)")
        else:
            # If caller specified resampling, also use it for overview building.
            overview_resampling = resampling_method
            if not quiet:
                print(f"  Resampling method: {resampling_method} (caller-supplied)")

        if not quiet:
            print(f"  Warping to {dst_crs}...")

        # Build gdalwarp command (chosen over `rio warp` so we can use
        # NUM_THREADS=ALL_CPUS; rio warp's --threads only accepts integers).
        warp_cmd = [
            'gdalwarp',
            '-t_srs', dst_crs,
            '-r', resampling_method,
            '-multi',
            '-wo', 'NUM_THREADS=ALL_CPUS',
            '--config', 'GDAL_NUM_THREADS', 'ALL_CPUS',
            '-overwrite',
        ]

        # Clamp output extent to Web Mercator's valid domain when source
        # exceeds it (global Mollweide, polar stereographic, etc.).
        if clip_webmerc:
            warp_cmd.extend([
                '-te',
                f'-{WEBMERC_EXTENT_M}', f'-{WEBMERC_EXTENT_M}',
                f'{WEBMERC_EXTENT_M}', f'{WEBMERC_EXTENT_M}',
                '-te_srs', 'EPSG:3857',
            ])

        # Add nodata to warp command (gdalwarp uses -srcnodata/-dstnodata)
        if nodata is not None:
            warp_cmd.extend(['-srcnodata', str(nodata)])
            warp_cmd.extend(['-dstnodata', str(nodata)])

        warp_cmd.extend([input_tif, warped_file])

        try:
            result = subprocess.run(
                warp_cmd,
                capture_output=True,
                text=True,
                check=True
            )

            if not quiet and result.stdout:
                print(f"  {result.stdout.strip()}")

            # Use warped file as input for COG conversion
            input_for_cog = warped_file

        except subprocess.CalledProcessError as e:
            error_msg = f"Error warping to {dst_crs}: {e.stderr}"
            print(error_msg)
            raise RuntimeError(error_msg)

    # Step 2: Build rio cogeo create command (using warped file if reprojected)
    cmd = [
        'rio', 'cogeo', 'create',
        input_for_cog,  # Use warped file if reprojection occurred
        temp_output,
        '--cog-profile', compression.lower(),
        '--overview-level', str(overview_levels),
        '--overview-resampling', overview_resampling,
        '--co', 'NUM_THREADS=ALL_CPUS',
    ]

    # Add no-data value
    if nodata is not None:
        cmd.extend(['--nodata', str(nodata)])

    # Add compression-specific options with level
    if compression.upper() == 'DEFLATE':
        cmd.extend(['--co', 'PREDICTOR=2'])
        cmd.extend(['--co', f'ZLEVEL={compression_level}'])
    elif compression.upper() == 'LZW':
        cmd.extend(['--co', 'PREDICTOR=2'])
    elif compression.upper() == 'ZSTD':
        cmd.extend(['--co', 'PREDICTOR=2'])
        cmd.extend(['--co', f'ZSTD_LEVEL={compression_level}'])

    # Execute COG creation.
    #
    # Two paths:
    #   - `metadata is None` (default): subprocess `rio cogeo create`. Fast,
    #     unchanged from prior behavior.
    #   - `metadata is not None`: in-process `rio_cogeo.cogeo.cog_translate`
    #     with `additional_cog_metadata=...`. Required because:
    #       (a) `rio cogeo create` CLI has no flag for arbitrary tags, and
    #       (b) reopening a finished COG with `gdal.Open(GA_Update)` +
    #           `SetMetadata(...)` breaks the COG layout in GDAL 3.10+
    #           (`cog_validate` returns valid=False with IFD-offset errors).
    if not quiet:
        if metadata is not None:
            print(f"  Creating COG with embedded metadata: {os.path.basename(temp_output)}")
        else:
            print(f"  Creating COG: {os.path.basename(temp_output)}")

    try:
        if metadata is not None:
            from rio_cogeo.cogeo import cog_translate

            # Auto-augment with YEAR_MONTH/HAZARD/LOCATION/PROCESSING_DATE
            # if the filename matches the activation-event pattern.
            try:
                from shared_utils.cog_metadata import resolve_metadata
                full_metadata = resolve_metadata(
                    os.path.basename(input_tif),
                    mode='manual',
                    manual_metadata=metadata,
                )
            except ImportError:
                full_metadata = dict(metadata)

            if not quiet:
                print(f"  Embedded tags: {sorted(full_metadata.keys())}")

            profile = _build_cog_translate_profile(compression, compression_level)
            cog_translate(
                input_for_cog,
                temp_output,
                profile,
                nodata=nodata,
                overview_level=overview_levels,
                overview_resampling=overview_resampling,
                web_optimized=False,
                additional_cog_metadata=full_metadata,
                quiet=quiet,
            )
        else:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
            )
            if not quiet and result.stdout:
                print(f"  {result.stdout.strip()}")

        # If we created a temp file, replace the original.
        # Use shutil.move (not os.rename): temp_output lives in /tmp while
        # output_cog is usually on a different mount (e.g. the Hub's /home or a
        # shared volume). os.rename across filesystems raises
        # OSError(EXDEV, 'Invalid cross-device link'); shutil.move falls back to
        # copy + delete.
        if temp_output != output_cog:
            if os.path.exists(output_cog):
                os.remove(output_cog)
            shutil.move(temp_output, output_cog)

        # Clean up warped temp file if it was created
        if warped_file and os.path.exists(warped_file):
            os.remove(warped_file)

        if not quiet:
            print(f"  ✓ COG created: {os.path.basename(output_cog)}")

        return output_cog

    except subprocess.CalledProcessError as e:
        error_msg = f"Error creating COG: {e.stderr}"
        print(error_msg)
        # Clean up temp files if they exist
        if os.path.exists(temp_output):
            os.remove(temp_output)
        if warped_file and os.path.exists(warped_file):
            os.remove(warped_file)
        raise RuntimeError(error_msg)
    except Exception:
        if os.path.exists(temp_output):
            os.remove(temp_output)
        if warped_file and os.path.exists(warped_file):
            os.remove(warped_file)
        raise


def validate_cog(cog_path: str) -> Tuple[bool, dict]:
    """
    Validate that a file is a valid Cloud Optimized GeoTIFF.

    Tries the rio_cogeo Python library first for richer detail, then falls
    back to the ``rio cogeo validate`` CLI subprocess.

    Args:
        cog_path: Path to COG file

    Returns:
        Tuple of (is_valid, details) where details is a dict with keys:
            'valid' (bool), 'errors' (list[str]), 'warnings' (list[str])
    """
    empty_details = {'valid': False, 'errors': [], 'warnings': []}

    if not os.path.exists(cog_path):
        empty_details['errors'].append(f"File not found: {cog_path}")
        return False, empty_details

    # Try the Python library first
    try:
        from rio_cogeo.cogeo import cog_validate
        is_valid, errors, warnings = cog_validate(cog_path, quiet=True)
        details = {
            'valid': is_valid,
            'errors': errors if errors else [],
            'warnings': warnings if warnings else [],
        }
        return is_valid, details
    except ImportError:
        pass
    except Exception:
        pass

    # Fallback to CLI subprocess
    try:
        result = subprocess.run(
            ['rio', 'cogeo', 'validate', cog_path],
            capture_output=True,
            text=True,
            check=False
        )

        is_valid = result.returncode == 0
        details = {
            'valid': is_valid,
            'errors': [] if is_valid else [result.stderr.strip() or "COG validation failed"],
            'warnings': [],
        }
        return is_valid, details

    except Exception as e:
        print(f"Error validating COG: {e}")
        empty_details['errors'].append(str(e))
        return False, empty_details


def get_final_filename(original_path: str, event_name: Optional[str] = None, tif_only: bool = False) -> str:
    """
    Predict what the final filename will be after COG conversion and/or event renaming.

    This is used to check if a file already exists before processing.

    Args:
        original_path: Original TIF file path (before COG/rename)
        event_name: Event name for renaming (None if no renaming)
        tif_only: If True, COG conversion will be skipped

    Returns:
        Predicted final file path

    Examples:
        >>> get_final_filename("/path/LC08_trueColor_20250922_185617_046028.tif", None, False)
        "/path/LC08_trueColor_20250922_185617_046028.tif"  # COG converts in place

        >>> get_final_filename("/path/LC08_trueColor_20250922_185617_046028.tif", "202512_Flood_WA", False)
        "/path/LC08_trueColor_185617_046028_2025-09-22_day.tif"  # event name no longer prefixed
    """
    if event_name is None:
        # No renaming, COG converts in place or stays as TIF
        return original_path

    # If event name is provided, simulate the rename logic
    directory = os.path.dirname(original_path)
    filename = os.path.basename(original_path)
    name_parts = os.path.splitext(filename)
    base_name = name_parts[0]
    extension = name_parts[1]

    # Split filename by underscore to extract date
    parts = base_name.split('_')

    if len(parts) < 3:
        # If filename doesn't match pattern, return original
        return original_path

    # Find the date (8-digit number that parses as YYYYMMDD)
    # Check multiple positions to support both Landsat and Sentinel-2
    date_str = None
    date_index = None
    for i, part in enumerate(parts):
        if len(part) == 8 and part.isdigit():
            try:
                from datetime import datetime
                datetime.strptime(part, '%Y%m%d')
                date_str = part
                date_index = i
                break
            except ValueError:
                continue

    if date_str is None or date_index is None:
        # If no valid date found, return original
        return original_path

    # Parse and format date
    try:
        from datetime import datetime
        date_obj = datetime.strptime(date_str, '%Y%m%d')
        formatted_date = date_obj.strftime('%Y-%m-%d')
    except ValueError:
        # If date parsing fails, return original
        return original_path

    # Check if this is a merged file
    is_merged = 'merged' in parts

    # Build predicted filename based on whether it's merged or individual
    if is_merged:
        # Merged file: sensor_product_merged_YYYY-MM-DD_day.tif (no event prefix)
        sensor = parts[0]
        # Product is every token between the sensor prefix and the date token, so
        # multi-token sensor IDs (e.g. Sentinel-2 "S2B_MSIL2A_<product>") keep the
        # real product instead of collapsing to the processing-level token.
        product = '_'.join(parts[1:date_index])
        new_filename = f"{sensor}_{product}_merged_{formatted_date}_day{extension}"
    else:
        # Individual file: Remove the date from parts and rejoin
        parts.pop(date_index)
        base_name_without_date = '_'.join(parts)
        new_filename = f"{base_name_without_date}_{formatted_date}_day{extension}"

    return os.path.join(directory, new_filename)


def rename_with_event(file_path: str, event_name: str, quiet: bool = False) -> str:
    """
    Rename a file to relocate its date/time to the end.
 
    NOTE: the event name is intentionally NOT added to the filename. The
    `event_name` parameter is retained for backward compatibility and as the
    rename trigger (callers invoke this only when an event was supplied); only
    the date/time relocation (+ _day suffix, for legacy shapes) is applied.
 
    Two distinct filename shapes are supported, disambiguated structurally so
    callers outside the Landsat89 pipeline (e.g. Sentinel-2) are unaffected:
 
    1) Landsat89 shape - date and time are the *last* two tokens, nothing
       trails them (sensor_product_tile[_masked]_YYYYMMDD_HHMMSS, or
       sensor_product_merged[_masked]_YYYYMMDD_HHMMSS). Date+time are combined
       into a single ISO 8601 Zulu datetime and everything before them
       (sensor, product, tile, merged/masked) is preserved verbatim as a
       prefix. No _day suffix is added here - the trailing "Z" already marks
       the file as fully renamed/idempotency-safe:
        Original: LC08_trueColor_046028_20250922_185617.tif
        New:      LC08_trueColor_046028_2025-09-22T18:56:17Z.tif
        Original: LC08_trueColor_merged_masked_20250922_185617.tif
        New:      LC08_trueColor_merged_masked_2025-09-22T18:56:17Z.tif
 
    2) (everything else) - behavior is UNCHANGED from the
       original implementation, so Sentinel-2 and any other existing caller
       keep producing exactly the filenames they always have (still using
       the dashed date + _day suffix as their completion marker):
 
       Sentinel-2 individual (date/time NOT last - tile trails):
        Original: S2B_MSIL2A_colorInfrared_20251111_161419_T17RLN.tif
        New:      S2B_MSIL2A_colorInfrared_161419_T17RLN_2025-11-11_day.tif
 
       Old-style Landsat individual (date/time NOT last - tile trails):
        Original: LC08_trueColor_20250922_185617_046028.tif
        New:      LC08_trueColor_185617_046028_2025-09-22_day.tif
 
       Merged file with no time component at all:
        Original: LC08_trueColor_20250922_merged.tif
        New:      LC08_trueColor_merged_2025-09-22_day.tif
 
    Args:
        file_path: Path to the file to rename
        event_name: Retained for backward compatibility / rename trigger; not added to the name
        quiet: Suppress output messages
 
    Returns:
        New file path after renaming
 
    Raises:
        ValueError: If date cannot be extracted from filename
        FileNotFoundError: If file doesn't exist
    """
    import os
 
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")
 
    directory = os.path.dirname(file_path)
    filename = os.path.basename(file_path)
    name_parts = os.path.splitext(filename)
    base_name = name_parts[0]
    extension = name_parts[1]
 
    # New-convention files are done once they end in an
    # ISO Zulu datetime (Z is the completion marker, no _day needed).
    # Legacy-shape files are done once they end in _day.
    if _ISO_ZULU_END_RE.search(base_name) or base_name.endswith('_day'):
        if not quiet:
            print(f"  Already renamed, skipping: {filename}")
        return file_path
 
    parts = base_name.split('_')
    if len(parts) < 3:
        raise ValueError(f"Filename doesn't match expected pattern: {filename}")
 
    is_merged = 'merged' in parts
 
    # Look for an adjacent date(8-digit YYYYMMDD)+time(6-digit HHMMSS) pair.
    date_index = None
    date_str = None
    time_str = None
    for i in range(len(parts) - 1):
        p, nxt = parts[i], parts[i + 1]
        if len(p) == 8 and p.isdigit() and len(nxt) == 6 and nxt.isdigit():
            try:
                datetime.strptime(p, '%Y%m%d')
                date_str, time_str, date_index = p, nxt, i
                break
            except ValueError:
                continue
 
    new_filename = None
 
    if date_str is not None and date_index + 2 == len(parts):
        # Landsat89 shape: nothing trails the time token. Combine
        # date+time into ISO 8601 Zulu, keep everything before them verbatim.
        # No _day suffix - the trailing Z is the completion marker.
        date_obj = datetime.strptime(date_str, '%Y%m%d')
        iso_datetime = f"{date_obj.strftime('%Y-%m-%d')}T{time_str[0:2]}:{time_str[2:4]}:{time_str[4:6]}Z"
        prefix = '_'.join(parts[0:date_index])
        new_filename = f"{prefix}_{iso_datetime}{extension}"
 
    elif date_str is not None:
        # Adjacent date+time pair found, but something trails it (e.g.
        # Sentinel-2 / old-Landsat tile-after-time). LEGACY behavior,
        # unchanged: only remove the date token; time and any trailing
        # tokens stay exactly where they were; formatted date + _day is
        # appended at the very end.
        date_obj = datetime.strptime(date_str, '%Y%m%d')
        formatted_date = date_obj.strftime('%Y-%m-%d')
        remaining = list(parts)
        remaining.pop(date_index)
        new_filename = f"{'_'.join(remaining)}_{formatted_date}_day{extension}"
 
    else:
        # No adjacent date+time pair at all (e.g. a merged file with only a
        # date, no time - LEGACY behavior, unchanged).
        lone_date_index = None
        for i, p in enumerate(parts):
            if len(p) == 8 and p.isdigit():
                try:
                    datetime.strptime(p, '%Y%m%d')
                    lone_date_index = i
                    break
                except ValueError:
                    continue
        if lone_date_index is None:
            raise ValueError(f"Could not find valid date (YYYYMMDD) in filename: {filename}")
        date_obj = datetime.strptime(parts[lone_date_index], '%Y%m%d')
        formatted_date = date_obj.strftime('%Y-%m-%d')
        if is_merged:
            sensor = parts[0]
            product = '_'.join(parts[1:lone_date_index])
            new_filename = f"{sensor}_{product}_merged_{formatted_date}_day{extension}"
        else:
            remaining = list(parts)
            remaining.pop(lone_date_index)
            new_filename = f"{'_'.join(remaining)}_{formatted_date}_day{extension}"
 
    new_path = os.path.join(directory, new_filename)
 
    if not quiet:
        print(f"  Renaming: {filename}")
        print(f"        to: {new_filename}")
 
    try:
        os.rename(file_path, new_path)
        return new_path
    except Exception as e:
        raise RuntimeError(f"Failed to rename file: {e}")