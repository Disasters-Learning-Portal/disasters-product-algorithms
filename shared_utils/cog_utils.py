"""
Cloud Optimized GeoTIFF (COG) utilities for Landsat processing.

Based on implementation patterns from disasters-aws-conversion repository.
"""

import os
import subprocess
import tempfile
import rasterio
import numpy as np
from typing import Optional, Union, Tuple


def set_nodata_value(dtype: str, manual_nodata: Optional[Union[int, float]] = None) -> Union[int, float]:
    """
    Automatically select appropriate no-data value based on data type.

    Args:
        dtype: Rasterio/numpy data type string (e.g., 'uint8', 'int16', 'float32')
        manual_nodata: Optional manual no-data value to use. If provided and valid
            for the given dtype, it will be used instead of auto-detection.

    Returns:
        Appropriate no-data value for the data type

    Based on disasters-aws-conversion/lib/core/compression.py:set_nodata_value()
    """
    # Use manual no-data if provided and valid
    if manual_nodata is not None:
        validation = validate_nodata_for_dtype(manual_nodata, dtype)
        if validation['valid']:
            return manual_nodata

    dtype_str = str(dtype).lower()

    if dtype_str == 'uint8':
        return 0
    elif dtype_str == 'uint16':
        return 0
    elif dtype_str == 'int8':
        return -128
    elif dtype_str == 'int16':
        return -9999
    elif dtype_str == 'int32':
        return -9999
    elif 'float' in dtype_str:
        return -9999.0
    else:
        # Default fallback
        return -9999.0


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
    # Unknown type, be permissive

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

    Returns:
        Path to created COG file
    """
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
            if existing_nodata is not None:
                nodata = existing_nodata
                if not quiet:
                    print(f"  Using existing no-data value: {nodata}")
            else:
                nodata = set_nodata_value(dtype)
                if not quiet:
                    print(f"  Auto-selected no-data value for {dtype}: {nodata}")
        else:
            # Validate user-provided no-data
            if not validate_nodata_for_dtype(nodata, dtype)['valid']:
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

    # Execute command
    if not quiet:
        print(f"  Creating COG: {os.path.basename(temp_output)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True
        )

        if not quiet and result.stdout:
            print(f"  {result.stdout.strip()}")

        # If we created a temp file, replace the original
        if temp_output != output_cog:
            if os.path.exists(output_cog):
                os.remove(output_cog)
            os.rename(temp_output, output_cog)

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
    except Exception as e:
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
        "/path/202512_Flood_WA_LC08_trueColor_185617_046028_2025-09-22_day.tif"
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
        # Merged file: EVENT_NAME_sensor_product_merged_YYYY-MM-DD_day.tif
        sensor = parts[0]
        product = parts[1] if date_index > 1 else parts[2]
        new_filename = f"{event_name}_{sensor}_{product}_merged_{formatted_date}_day{extension}"
    else:
        # Individual file: Remove the date from parts and rejoin
        parts.pop(date_index)
        base_name_without_date = '_'.join(parts)
        new_filename = f"{event_name}_{base_name_without_date}_{formatted_date}_day{extension}"

    return os.path.join(directory, new_filename)


def rename_with_event(file_path: str, event_name: str, quiet: bool = False) -> str:
    """
    Rename a file to include event name prefix and formatted date suffix.
    The date is removed from its original position in the middle and added at the end.

    Supports both Landsat and Sentinel-2 naming patterns.

    Landsat individual file format:
        Original: LC08_trueColor_20250922_185617_046028.tif
        New: EVENT_NAME_LC08_trueColor_185617_046028_2025-09-22_day.tif

    Sentinel-2 individual file format:
        Original: S2B_MSIL2A_colorInfrared_20251111_161419_T17RLN.tif
        New: EVENT_NAME_S2B_MSIL2A_colorInfrared_161419_T17RLN_2025-11-11_day.tif

    Merged file format:
        Original: LC08_trueColor_20250922_merged.tif
        New: EVENT_NAME_LC08_trueColor_merged_2025-09-22_day.tif

    Args:
        file_path: Path to the file to rename
        event_name: Event name to use as prefix
        quiet: Suppress output messages

    Returns:
        New file path after renaming

    Raises:
        ValueError: If date cannot be extracted from filename
        FileNotFoundError: If file doesn't exist
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    # Get directory and filename
    directory = os.path.dirname(file_path)
    filename = os.path.basename(file_path)
    name_parts = os.path.splitext(filename)
    base_name = name_parts[0]
    extension = name_parts[1]

    # Split filename by underscore to extract date
    parts = base_name.split('_')

    if len(parts) < 3:
        raise ValueError(f"Filename doesn't match expected pattern: {filename}")

    # Check if this is a merged file
    is_merged = 'merged' in parts

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
        raise ValueError(f"Could not find valid date (YYYYMMDD) in filename: {filename}")

    # Parse and format date
    try:
        from datetime import datetime
        date_obj = datetime.strptime(date_str, '%Y%m%d')
        formatted_date = date_obj.strftime('%Y-%m-%d')
    except ValueError as e:
        raise ValueError(f"Could not parse date '{date_str}': {e}")

    # Build new filename based on whether it's merged or individual
    if is_merged:
        # Merged file: EVENT_NAME_sensor_product_merged_YYYY-MM-DD_day.tif
        sensor = parts[0]
        # Product is at index 1 for Landsat, or index 2 for Sentinel (which has level at index 1)
        product = parts[1] if date_index > 1 else parts[2]
        new_filename = f"{event_name}_{sensor}_{product}_merged_{formatted_date}_day{extension}"
    else:
        # Individual file: Remove the date from the original parts and rejoin
        parts.pop(date_index)  # Remove date at the detected index
        base_name_without_date = '_'.join(parts)
        new_filename = f"{event_name}_{base_name_without_date}_{formatted_date}_day{extension}"

    new_path = os.path.join(directory, new_filename)

    # Rename the file
    if not quiet:
        print(f"  Renaming: {filename}")
        print(f"        to: {new_filename}")

    try:
        os.rename(file_path, new_path)
        return new_path
    except Exception as e:
        raise RuntimeError(f"Failed to rename file: {e}")
