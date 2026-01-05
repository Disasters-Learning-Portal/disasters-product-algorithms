"""
Cloud Optimized GeoTIFF (COG) utilities for Landsat processing.

Based on implementation patterns from disasters-aws-conversion repository.
"""

import os
import subprocess
import rasterio
from typing import Optional, Union


def set_nodata_value(dtype: str) -> Union[int, float]:
    """
    Automatically select appropriate no-data value based on data type.

    Args:
        dtype: Rasterio/numpy data type string (e.g., 'uint8', 'int16', 'float32')

    Returns:
        Appropriate no-data value for the data type

    Based on disasters-aws-conversion/lib/core/compression.py:set_nodata_value()
    """
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
        return -9999
    else:
        # Default fallback
        return -9999


def validate_nodata_for_dtype(nodata: Union[int, float], dtype: str) -> bool:
    """
    Validate that a no-data value is valid for the given data type.

    Args:
        nodata: No-data value to validate
        dtype: Rasterio/numpy data type string

    Returns:
        True if valid, False otherwise

    Based on disasters-aws-conversion/lib/core/compression.py:validate_nodata_for_dtype()
    """
    dtype_str = str(dtype).lower()

    try:
        nodata = float(nodata)
    except (TypeError, ValueError):
        return False

    if dtype_str == 'uint8':
        return 0 <= nodata <= 255
    elif dtype_str == 'uint16':
        return 0 <= nodata <= 65535
    elif dtype_str == 'int8':
        return -128 <= nodata <= 127
    elif dtype_str == 'int16':
        return -32768 <= nodata <= 32767
    elif dtype_str == 'int32':
        return -2147483648 <= nodata <= 2147483647
    elif 'float' in dtype_str:
        # Float types can use any value including NaN
        return True
    else:
        # Unknown type, be permissive
        return True


def get_compression_profile(compression: str = 'ZSTD', compression_level: int = 22) -> dict:
    """
    Get compression profile for COG creation.

    Args:
        compression: Compression type (DEFLATE, LZW, ZSTD, etc.)
        compression_level: Compression level (default: 22 for ZSTD, 9 for others)

    Returns:
        Dictionary of compression options for rio cogeo
    """
    valid_compressions = ['DEFLATE', 'LZW', 'ZSTD', 'JPEG', 'WEBP', 'NONE']

    if compression.upper() not in valid_compressions:
        print(f"Warning: Invalid compression '{compression}', using ZSTD")
        compression = 'ZSTD'

    return {
        'compress': compression.upper(),
        'predictor': '2',  # Horizontal differencing (good for most satellite data)
        'level': compression_level,
    }


def convert_to_cog(
    input_tif: str,
    output_cog: Optional[str] = None,
    nodata: Optional[Union[int, float]] = None,
    compression: str = 'ZSTD',
    compression_level: int = 22,
    overview_levels: int = 5,
    quiet: bool = False
) -> str:
    """
    Convert a GeoTIFF to Cloud Optimized GeoTIFF (COG) format.

    Args:
        input_tif: Path to input GeoTIFF file
        output_cog: Path to output COG file (if None, replaces input file)
        nodata: No-data value (if None, auto-detects from file or data type)
        compression: Compression type (default: ZSTD)
        compression_level: Compression level (default: 22 for ZSTD)
        overview_levels: Number of overview levels (default: 5, minimum)
        quiet: Suppress output messages

    Returns:
        Path to created COG file

    Based on disasters-aws-conversion/scripts/update_nodata_cog.py
    """
    if not os.path.exists(input_tif):
        raise FileNotFoundError(f"Input file not found: {input_tif}")

    # Determine output path
    if output_cog is None:
        # Replace input file with COG version
        output_cog = input_tif
        temp_output = input_tif + '.cog.tmp.tif'
    else:
        temp_output = output_cog

    # Read input file metadata
    with rasterio.open(input_tif) as src:
        dtype = src.dtypes[0]
        existing_nodata = src.nodata

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
            if not validate_nodata_for_dtype(nodata, dtype):
                print(f"  Warning: No-data value {nodata} may be invalid for {dtype}")

        if not quiet:
            print(f"  Data type: {dtype}")
            print(f"  No-data value: {nodata}")
            print(f"  Compression: {compression} (level {compression_level})")
            print(f"  Overview levels: {overview_levels}")

    # Build rio cogeo create command
    cmd = [
        'rio', 'cogeo', 'create',
        input_tif,
        temp_output,
        '--cog-profile', compression.lower(),
        '--overview-level', str(overview_levels),
        '--overview-resampling', 'average',
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

        if not quiet:
            print(f"  ✓ COG created: {os.path.basename(output_cog)}")

        return output_cog

    except subprocess.CalledProcessError as e:
        error_msg = f"Error creating COG: {e.stderr}"
        print(error_msg)
        # Clean up temp file if it exists
        if os.path.exists(temp_output):
            os.remove(temp_output)
        raise RuntimeError(error_msg)
    except Exception as e:
        if os.path.exists(temp_output):
            os.remove(temp_output)
        raise


def validate_cog(cog_path: str) -> bool:
    """
    Validate that a file is a valid Cloud Optimized GeoTIFF.

    Args:
        cog_path: Path to COG file

    Returns:
        True if valid COG, False otherwise
    """
    if not os.path.exists(cog_path):
        return False

    try:
        result = subprocess.run(
            ['rio', 'cogeo', 'validate', cog_path],
            capture_output=True,
            text=True,
            check=False
        )

        # rio cogeo validate returns 0 if valid
        return result.returncode == 0

    except Exception as e:
        print(f"Error validating COG: {e}")
        return False


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
