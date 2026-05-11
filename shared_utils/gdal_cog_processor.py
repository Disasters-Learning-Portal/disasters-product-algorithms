"""
Optimized COG processor using GDAL COG driver via subprocess.
Leverages GDAL 3.10.3's native COG driver for maximum performance.
"""

import os
import subprocess
import uuid
from typing import Optional, Dict, List, Tuple
import logging
import rasterio
import numpy as np

# Setup logging
logger = logging.getLogger(__name__)


def get_resampling_for_dtype(dtype: str) -> Tuple[str, str]:
    """
    Get appropriate resampling methods based on data type.

    Args:
        dtype: NumPy data type string (e.g., 'float32', 'uint8', 'int16')

    Returns:
        Tuple of (resampling_method, overview_resampling)
        - resampling_method: For reprojection ('bilinear', 'nearest', 'cubic')
        - overview_resampling: For overview generation ('average', 'mode', 'nearest')

    Logic:
        - Floating point data → continuous → bilinear + average
        - Integer data (uint8) → likely categorical → nearest + mode
        - Integer data (int16/int32) → likely continuous → bilinear + average
    """
    dtype_str = str(dtype).lower()

    # Floating point data is continuous
    if 'float' in dtype_str or 'complex' in dtype_str:
        # Continuous data (NDVI, temperature, precipitation, etc.)
        return 'bilinear', 'average'

    # Byte data (uint8) is often categorical
    elif 'uint8' in dtype_str or 'byte' in dtype_str:
        # Likely categorical (land cover, classification, masks)
        return 'nearest', 'mode'

    # Larger integers can be either, default to continuous
    elif 'int' in dtype_str:
        # Could be elevation (continuous) or classes (categorical)
        # Default to continuous as it's safer for most remote sensing
        return 'bilinear', 'average'

    else:
        # Unknown type - use safe defaults
        return 'bilinear', 'average'


def set_optimal_gdal_env() -> Dict[str, str]:
    """
    Set optimal GDAL environment variables for performance.

    Returns:
        Dictionary of environment variables
    """
    env = os.environ.copy()

    # Threading and parallel processing
    env['GDAL_NUM_THREADS'] = 'ALL_CPUS'

    # Memory cache (8GB for 24.7GB system)
    env['GDAL_CACHEMAX'] = '8192'

    # Temp directory - use /tmp as the base
    temp_base = os.environ.get('COG_TEMP_DIR', '/tmp')
    env['CPL_TMPDIR'] = temp_base
    os.makedirs(temp_base, exist_ok=True)

    # S3 and VSI optimizations
    env['GDAL_DISABLE_READDIR_ON_OPEN'] = 'TRUE'
    env['VSI_CACHE'] = 'TRUE'
    env['VSI_CACHE_SIZE'] = '5000000'  # 5MB chunks
    env['CPL_VSIL_CURL_ALLOWED_EXTENSIONS'] = '.tif,.tiff,.TIF,.TIFF'
    env['AWS_REQUEST_PAYER'] = 'requester'  # For requester-pays buckets

    # GDAL config for better error handling
    env['CPL_DEBUG'] = 'OFF'  # Set to 'ON' for debugging

    return env


def create_cog_gdal(
    input_path: str,
    output_path: str,
    nodata: Optional[float] = None,
    compress: str = 'ZSTD',
    compress_level: int = 22,
    blocksize: int = 512,
    reproject_to_4326: bool = True,
    verbose: bool = True
) -> bool:
    """
    Create COG using GDAL's native COG driver for maximum performance.
    Automatically selects appropriate resampling based on data type.

    Args:
        input_path: Input file path (can be /vsis3/ path)
        output_path: Output COG path
        nodata: Optional nodata value
        compress: Compression type (ZSTD, LZW, DEFLATE, etc.)
        compress_level: Compression level (1-22 for ZSTD)
        blocksize: Tile block size
        reproject_to_4326: Whether to reproject to EPSG:4326
        verbose: Print progress messages

    Returns:
        True if successful, False otherwise
    """
    try:
        # Ensure output directory exists
        output_dir = os.path.dirname(output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        if verbose:
            print(f"   [GDAL-COG] Creating COG with native GDAL driver...")

        # Detect data type and get appropriate resampling
        try:
            with rasterio.open(input_path) as src:
                dtype = src.dtypes[0]
            resampling, overview_resampling = get_resampling_for_dtype(dtype)
            if verbose:
                print(f"   [GDAL-COG] Data type: {dtype} → Resampling: {resampling}, Overviews: {overview_resampling}")
        except:
            # Fallback if can't detect
            resampling = 'bilinear'
            overview_resampling = 'average'

        # Set optimal environment
        env = set_optimal_gdal_env()

        # Handle reprojection if needed
        if reproject_to_4326:
            # Use two-stage process for reprojection
            return create_cog_with_reprojection(
                input_path, output_path, nodata,
                compress, compress_level, blocksize,
                resampling, overview_resampling,
                env, verbose
            )

        # Direct COG creation without reprojection
        cmd = build_gdal_translate_command(
            input_path, output_path, nodata,
            compress, compress_level, blocksize,
            overview_resampling=overview_resampling
        )

        if verbose:
            print(f"   [GDAL-COG] Executing: {' '.join(cmd[:3])}...")

        # Run the command
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            check=False
        )

        if result.returncode != 0:
            logger.error(f"GDAL error: {result.stderr}")
            if verbose:
                print(f"   [GDAL-COG] ❌ Error: {result.stderr}")
            return False

        if verbose:
            print(f"   [GDAL-COG] ✅ COG created successfully")
        return True

    except Exception as e:
        logger.error(f"Failed to create COG: {e}")
        if verbose:
            print(f"   [GDAL-COG] ❌ Failed: {e}")
        return False


def create_cog_with_reprojection(
    input_path: str,
    output_path: str,
    nodata: Optional[float],
    compress: str,
    compress_level: int,
    blocksize: int,
    resampling: str,
    overview_resampling: str,
    env: Dict[str, str],
    verbose: bool
) -> bool:
    """
    Create COG with reprojection using two-stage process.
    Stage 1: gdalwarp for reprojection
    Stage 2: gdal_translate for COG creation

    Uses appropriate resampling based on data type.
    Handles nodata remapping: detects original nodata and remaps to specified value.
    """
    temp_file = None
    try:
        # Ensure output directory exists
        output_dir = os.path.dirname(output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        # Create temp file for reprojected data in local directory
        temp_base = os.environ.get('COG_TEMP_DIR', '/tmp')
        os.makedirs(temp_base, exist_ok=True)
        temp_file = os.path.join(temp_base, f"reproj_{uuid.uuid4().hex}.tif")

        # Detect original nodata value for proper remapping
        original_nodata = None
        try:
            with rasterio.open(input_path) as src:
                original_nodata = src.nodata
                if verbose and original_nodata is not None:
                    print(f"   [GDAL-COG] Detected original nodata: {original_nodata}")
        except:
            pass

        if verbose:
            print(f"   [GDAL-COG] Stage 1: Reprojecting to EPSG:4326 using {resampling} resampling...")

        # Stage 1: Reproject with gdalwarp (supports multi-threading)
        warp_cmd = [
            'gdalwarp',
            '-t_srs', 'EPSG:4326',
            '-r', resampling,  # Use appropriate resampling method
            '-multi',  # Enable multi-threading
            '-wo', 'NUM_THREADS=ALL_CPUS',
            '-wo', 'OPTIMIZE_SIZE=YES',
            '-co', 'TILED=YES',
            '-co', 'BLOCKXSIZE=512',
            '-co', 'BLOCKYSIZE=512',
            '-co', 'COMPRESS=NONE',  # No compression for temp file (faster)
        ]

        # Handle nodata remapping properly
        if nodata is not None:
            # If we detected an original nodata that differs from desired nodata, remap it
            if original_nodata is not None and original_nodata != nodata:
                if verbose:
                    print(f"   [GDAL-COG] Remapping nodata: {original_nodata} → {nodata}")
                warp_cmd.extend(['-srcnodata', str(original_nodata)])
                warp_cmd.extend(['-dstnodata', str(nodata)])
            else:
                # Just set the nodata value
                warp_cmd.extend(['-dstnodata', str(nodata)])
                if original_nodata is not None:
                    warp_cmd.extend(['-srcnodata', str(original_nodata)])
        elif original_nodata is not None:
            # Preserve original nodata
            warp_cmd.extend(['-srcnodata', str(original_nodata)])
            warp_cmd.extend(['-dstnodata', str(original_nodata)])

        warp_cmd.extend([input_path, temp_file])

        # Run gdalwarp
        result = subprocess.run(
            warp_cmd,
            env=env,
            capture_output=True,
            text=True,
            check=False
        )

        if result.returncode != 0:
            logger.error(f"gdalwarp error: {result.stderr}")
            if verbose:
                print(f"   [GDAL-COG] ❌ Reprojection failed: {result.stderr}")
            return False

        if verbose:
            print(f"   [GDAL-COG] Stage 2: Creating COG...")

        # Stage 2: Create COG from reprojected file
        cog_cmd = build_gdal_translate_command(
            temp_file, output_path, nodata,
            compress, compress_level, blocksize,
            overview_resampling=overview_resampling
        )

        result = subprocess.run(
            cog_cmd,
            env=env,
            capture_output=True,
            text=True,
            check=False
        )

        if result.returncode != 0:
            logger.error(f"gdal_translate error: {result.stderr}")
            if verbose:
                print(f"   [GDAL-COG] ❌ COG creation failed: {result.stderr}")
            return False

        if verbose:
            print(f"   [GDAL-COG] ✅ COG with reprojection created successfully")
        return True

    finally:
        # Cleanup temp file
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except:
                pass


def build_gdal_translate_command(
    input_path: str,
    output_path: str,
    nodata: Optional[float],
    compress: str,
    compress_level: int,
    blocksize: int,
    overview_resampling: str = 'average'
) -> List[str]:
    """
    Build gdal_translate command with optimal COG parameters.
    Uses appropriate overview resampling based on data type.
    """
    cmd = [
        'gdal_translate',
        '-of', 'COG',  # Use COG driver
        '-co', f'COMPRESS={compress}',
        '-co', 'NUM_THREADS=ALL_CPUS',
        '-co', 'BIGTIFF=IF_SAFER',
        '-co', f'BLOCKSIZE={blocksize}',
        '-co', f'OVERVIEW_RESAMPLING={overview_resampling}',  # Use appropriate method
        '-co', 'OVERVIEW_COUNT=5'
    ]

    # Add compression-specific options
    # Note: COG driver doesn't support ZSTD_LEVEL, it's only for GTiff driver
    # COG driver uses ZSTD compression but with default settings
    if compress == 'ZSTD':
        cmd.extend(['-co', 'PREDICTOR=YES'])  # Auto-select predictor
    elif compress == 'LZW' or compress == 'DEFLATE':
        cmd.extend(['-co', 'PREDICTOR=2'])  # Horizontal differencing

    # Add nodata if specified
    if nodata is not None:
        cmd.extend(['-a_nodata', str(nodata)])

    # Add input and output paths
    cmd.extend([input_path, output_path])

    return cmd


def process_file_optimized(
    input_path: str,
    output_path: str,
    nodata: Optional[float] = None,
    file_size_gb: float = 0,
    reproject: bool = True,
    verbose: bool = True
) -> bool:
    """
    Process a single file with optimal strategy based on size.

    Args:
        input_path: Input file path
        output_path: Output COG path
        nodata: Optional nodata value
        file_size_gb: File size in GB for strategy selection
        reproject: Whether to reproject to EPSG:4326
        verbose: Print progress messages

    Returns:
        True if successful
    """
    # Select strategy based on file size
    if file_size_gb < 1.0:
        # Small files: Use maximum compression
        return create_cog_gdal(
            input_path, output_path, nodata,
            compress='ZSTD', compress_level=22,
            reproject_to_4326=reproject, verbose=verbose
        )
    elif file_size_gb < 3.0:
        # Medium files: Balance speed and compression
        return create_cog_gdal(
            input_path, output_path, nodata,
            compress='ZSTD', compress_level=15,
            reproject_to_4326=reproject, verbose=verbose
        )
    else:
        # Large files: Prioritize speed
        return create_cog_gdal(
            input_path, output_path, nodata,
            compress='ZSTD', compress_level=9,
            blocksize=256,  # Smaller blocks for large files
            reproject_to_4326=reproject, verbose=verbose
        )


def validate_cog_gdal(file_path: str) -> Tuple[bool, str]:
    """
    Validate COG using GDAL.

    Args:
        file_path: Path to COG file

    Returns:
        Tuple of (is_valid, message)
    """
    try:
        cmd = ['gdalinfo', '-checksum', file_path]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False
        )

        if result.returncode != 0:
            return False, f"Invalid file: {result.stderr}"

        # Check for COG structure
        output = result.stdout
        if 'LAYOUT=COG' in output or 'Cloud Optimized' in output:
            return True, "Valid COG"

        # Check for tiling and overviews (indicators of COG)
        has_tiles = 'Block=' in output and 'Block=1x' not in output
        has_overviews = 'Overviews:' in output

        if has_tiles and has_overviews:
            return True, "Valid COG structure"

        return False, "Not a valid COG (missing tiles or overviews)"

    except Exception as e:
        return False, f"Validation error: {e}"