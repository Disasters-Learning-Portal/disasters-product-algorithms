"""
Reprojection module - handles coordinate system transformations.
Single responsibility: Reprojection and coordinate transformation.
"""

import os
import numpy as np
import rasterio
from rasterio.warp import calculate_default_transform, reproject, Resampling, transform_bounds
from rasterio.windows import Window
import gc
from tqdm import tqdm
from shared_utils.memory_management import get_memory_usage
from shared_utils.cog_validation import check_and_fix_nan_values

# All-CPU parallelism for rasterio.warp.reproject (param requires an integer,
# unlike GDAL's "ALL_CPUS" string).
_NUM_THREADS = os.cpu_count() or 1

# Web Mercator (EPSG:3857) is only defined within ±85.05113° latitude.
# Source rasters whose lat footprint extends past this band (global Mollweide,
# polar stereographic, etc.) blow up when warped to 3857 without an output
# extent clip — gdalwarp will either error with "Point outside of projection
# domain" or produce a 50+ GB mostly-nodata canvas.
WEBMERC_VALID_LAT = 85.05112878
WEBMERC_EXTENT_M = 20037508.342789244
WEBMERC_EPSGS = frozenset({
    'EPSG:3857', 'EPSG:900913', 'EPSG:102100', 'EPSG:102113',
})


def _crs_is_webmerc(crs) -> bool:
    """Check whether a CRS string maps to Web Mercator."""
    if crs is None:
        return False
    s = str(crs).strip().upper()
    return s in WEBMERC_EPSGS


def needs_webmerc_clip(src, dst_crs) -> bool:
    """
    Decide whether a warp to `dst_crs` requires a ±85° latitude clip to stay
    inside Web Mercator's valid domain.

    Args:
        src: An open `rasterio.DatasetReader` (preferred — bounds + CRS read
            from metadata) OR a path string. If a path is given, the file
            is opened transiently.
        dst_crs: Target CRS string (e.g. 'EPSG:3857'). Anything that doesn't
            map to Web Mercator returns False immediately.

    Returns:
        True iff dst_crs is Web Mercator AND the source's reprojected
        geographic bounds extend past ±WEBMERC_VALID_LAT. For the 99% case
        (regional rasters) this is False and no behavior change is induced.

    Falls back to True if bounds projection raises — when in doubt, clip.
    """
    if not _crs_is_webmerc(dst_crs):
        return False

    # Accept either a path or an open dataset.
    opened_here = False
    if isinstance(src, (str, bytes, os.PathLike)):
        src = rasterio.open(src)
        opened_here = True

    try:
        if src.crs is None:
            return True
        try:
            _, south, _, north = transform_bounds(
                src.crs, 'EPSG:4326', *src.bounds, densify_pts=21
            )
        except Exception:
            # Curved or pathological source CRS (e.g. world Mollweide whose
            # bounding-box corners fall outside the valid ellipse) — clip.
            return True
        return south < -WEBMERC_VALID_LAT or north > WEBMERC_VALID_LAT
    finally:
        if opened_here:
            src.close()


def process_whole_file(src, dst, src_crs, dst_crs, transform, width, height, src_nodata, dst_nodata=None):
    """
    Process entire file at once without chunking - for small to medium files.
    Much faster than chunked processing for files under 1.5GB.

    Args:
        src: Source dataset
        dst: Destination dataset
        src_crs: Source CRS
        dst_crs: Destination CRS
        transform: Destination transform
        width: Destination width
        height: Destination height
        src_nodata: Source nodata value
        dst_nodata: Destination nodata value (optional, defaults to src_nodata)

    Returns:
        None
    """
    # Default dst_nodata to src_nodata if not specified
    if dst_nodata is None:
        dst_nodata = src_nodata

    print(f"   [WHOLE-FILE] Processing entire file at once ({width}x{height} pixels)")
    if src_nodata != dst_nodata and src_nodata is not None:
        print(f"   [WHOLE-FILE] Remapping nodata: {src_nodata} → {dst_nodata}")

    # Process each band
    for band_idx in range(1, src.count + 1):
        print(f"   [BAND {band_idx}/{src.count}] Reprojecting entire band...")

        # Create destination array for the whole band
        dst_array = np.full(
            (height, width),
            dst_nodata if dst_nodata is not None else 0,
            dtype=src.dtypes[0]
        )

        # Reproject entire band at once
        try:
            reproject(
                source=rasterio.band(src, band_idx),
                destination=dst_array,
                src_transform=src.transform,
                src_crs=src_crs,
                dst_transform=transform,
                dst_crs=dst_crs,
                resampling=Resampling.nearest,
                src_nodata=src_nodata,
                dst_nodata=dst_nodata,
                num_threads=_NUM_THREADS,
            )

            # Check and fix NaN values if needed
            if np.isnan(dst_array).any():
                print(f"      [FIX] Found NaN values in band {band_idx}, replacing with nodata")
                dst_array = np.nan_to_num(dst_array, nan=dst_nodata)

            # Write to destination
            dst.write(dst_array, band_idx)
            print(f"      ✓ Band {band_idx} complete")

            # Clean up memory
            del dst_array
            gc.collect()

        except Exception as e:
            print(f"   [ERROR] Failed to reproject band {band_idx}: {e}")
            raise

    # For COGs, we need to close and reopen to add overviews properly
    print(f"   [WHOLE-FILE] ✅ Processing complete")


# Re-open file to add overviews in COG-compliant way
def add_cog_overviews(file_path, verbose=True):
    """Add overviews to make the file a valid COG."""
    if verbose:
        print(f"   [OVERVIEWS] Building COG overviews...")

    with rasterio.open(file_path, 'r+') as dst:
        factors = calculate_overview_factors(dst.width, dst.height)
        dst.build_overviews(factors, Resampling.average)
        dst.update_tags(ns='rio_overview', resampling='average')

    if verbose:
        print(f"   [OVERVIEWS] ✅ Overviews built with factors: {factors}")


def calculate_overview_factors(width, height):
    """Calculate appropriate overview factors based on image size."""
    factors = []
    max_dim = max(width, height)
    factor = 2
    while max_dim / factor > 256:  # Continue until smallest overview is ~256 pixels
        factors.append(factor)
        factor *= 2
    return factors if factors else [2, 4, 8]


def calculate_transform_parameters(src, dst_crs='EPSG:4326'):
    """
    Calculate transformation parameters for reprojection.

    Args:
        src: Source rasterio dataset
        dst_crs: Destination CRS

    Returns:
        tuple: (transform, width, height)
    """
    transform, width, height = calculate_default_transform(
        src.crs, dst_crs, src.width, src.height, *src.bounds
    )
    return transform, width, height


def reproject_chunk(src, band_idx, src_window, dst_window, src_transform,
                   dst_transform, src_crs, dst_crs, src_nodata, chunk_data):
    """
    Reproject a single chunk of data.

    Args:
        src: Source dataset
        band_idx: Band index to reproject
        src_window: Source window
        dst_window: Destination window
        src_transform: Source transform
        dst_transform: Destination transform
        src_crs: Source CRS
        dst_crs: Destination CRS
        src_nodata: Source nodata value
        chunk_data: Array to fill with reprojected data

    Returns:
        bool: True if successful, False if error occurred
    """
    try:
        reproject(
            source=rasterio.band(src, band_idx),
            destination=chunk_data,
            src_transform=src_transform,
            src_crs=src_crs,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            resampling=Resampling.nearest,
            src_nodata=src_nodata,
            dst_nodata=src_nodata,
            num_threads=_NUM_THREADS,
        )
        return True

    except Exception as e:
        print(f"   [REPROJECT ERROR] Failed to reproject chunk: {e}")
        return False


def process_with_fixed_chunks(src, dst, src_crs, dst_crs, transform, width, height,
                             chunk_size, src_nodata, chunk_config, initial_memory, dst_nodata=None):
    """
    Process file with FIXED chunk size throughout the entire operation.
    This prevents the striping issue caused by changing chunk sizes mid-loop.

    Args:
        src: Source dataset
        dst: Destination dataset
        src_crs: Source CRS
        dst_crs: Destination CRS
        transform: Destination transform
        width: Destination width
        height: Destination height
        chunk_size: FIXED chunk size to use
        src_nodata: Source nodata value
        chunk_config: Chunk configuration
        initial_memory: Initial memory usage
        dst_nodata: Destination nodata value (optional, defaults to src_nodata)

    Returns:
        None
    """
    # Default dst_nodata to src_nodata if not specified
    if dst_nodata is None:
        dst_nodata = src_nodata

    # Ensure chunk_size stays fixed
    FIXED_CHUNK_SIZE = chunk_size
    print(f"   [CHUNKS] Using FIXED chunk size: {FIXED_CHUNK_SIZE}x{FIXED_CHUNK_SIZE}")

    if src_nodata != dst_nodata and src_nodata is not None:
        print(f"   [CHUNKS] Remapping nodata: {src_nodata} → {dst_nodata}")

    # Calculate total chunks
    total_chunks_x = (width + FIXED_CHUNK_SIZE - 1) // FIXED_CHUNK_SIZE
    total_chunks_y = (height + FIXED_CHUNK_SIZE - 1) // FIXED_CHUNK_SIZE
    total_chunks = total_chunks_x * total_chunks_y

    print(f"   [CHUNKS] Processing {total_chunks} chunks ({total_chunks_x}x{total_chunks_y}) with fixed size {FIXED_CHUNK_SIZE}x{FIXED_CHUNK_SIZE}")

    # Process each band
    for band_idx in range(1, src.count + 1):
        print(f"   [BAND {band_idx}/{src.count}] Processing...")

        chunk_iterator = tqdm(total=total_chunks, desc="Processing chunks", disable=not chunk_config.get('show_progress', True))

        for chunk_y in range(0, height, FIXED_CHUNK_SIZE):
            for chunk_x in range(0, width, FIXED_CHUNK_SIZE):
                # Calculate window size (handle edge chunks)
                win_width = min(FIXED_CHUNK_SIZE, width - chunk_x)
                win_height = min(FIXED_CHUNK_SIZE, height - chunk_y)

                # Check memory and use sub-chunking if needed
                current_memory = get_memory_usage()
                memory_safe_mode = current_memory > chunk_config.get('memory_limit_mb', 500)

                if memory_safe_mode and win_width > 128 and win_height > 128:
                    # Process in smaller sub-chunks but maintain grid alignment
                    sub_chunk_size = 128

                    for sub_y in range(0, win_height, sub_chunk_size):
                        for sub_x in range(0, win_width, sub_chunk_size):
                            sub_win_width = min(sub_chunk_size, win_width - sub_x)
                            sub_win_height = min(sub_chunk_size, win_height - sub_y)

                            # Calculate actual positions
                            x = chunk_x + sub_x
                            y = chunk_y + sub_y

                            # Create windows
                            dst_window = Window(x, y, sub_win_width, sub_win_height)

                            # Initialize chunk
                            chunk_data = np.full(
                                (sub_win_height, sub_win_width),
                                dst_nodata if dst_nodata is not None else 0,
                                dtype=src.dtypes[0]
                            )

                            # Reproject sub-chunk with error handling
                            try:
                                reproject(
                                    source=rasterio.band(src, band_idx),
                                    destination=chunk_data,
                                    src_transform=src.transform,
                                    src_crs=src_crs,
                                    dst_transform=rasterio.windows.transform(dst_window, transform),
                                    dst_crs=dst_crs,
                                    resampling=Resampling.nearest,
                                    src_nodata=src_nodata,
                                    dst_nodata=dst_nodata,
                                    num_threads=_NUM_THREADS,
                                )
                            except Exception as reproject_error:
                                print(f"\n   [CHUNK ERROR] Failed at chunk ({x}, {y}) window ({sub_x}, {sub_y})")
                                print(f"   [CHUNK ERROR] Window size: {sub_win_width}x{sub_win_height}")
                                print(f"   [CHUNK ERROR] Error: {str(reproject_error)}")

                                # If we're streaming and getting chunk errors, switch to download
                                if "chunk and warp" in str(reproject_error).lower() and "/vsis3/" in str(getattr(src, 'name', '')):
                                    print(f"   [CHUNK ERROR] Streaming error detected - need to switch to download mode")
                                    raise Exception("STREAMING_CHUNK_ERROR: Need to retry with download")

                                # Try to recover by filling with nodata
                                print(f"   [CHUNK RECOVERY] Filling failed chunk with nodata value")
                                chunk_data.fill(dst_nodata if dst_nodata is not None else 0)

                            # Fix NaN values
                            chunk_data, _ = check_and_fix_nan_values(
                                chunk_data, dst_nodata, src.dtypes[0], band_idx=None
                            )

                            # Write sub-chunk
                            dst.write(chunk_data, band_idx, window=dst_window)

                            del chunk_data
                            gc.collect()
                else:
                    # Normal processing for full chunk
                    window = Window(chunk_x, chunk_y, win_width, win_height)

                    # Initialize chunk
                    chunk_data = np.full(
                        (win_height, win_width),
                        dst_nodata if dst_nodata is not None else 0,
                        dtype=src.dtypes[0]
                    )

                    # Reproject chunk with error handling
                    try:
                        reproject(
                            source=rasterio.band(src, band_idx),
                            destination=chunk_data,
                            src_transform=src.transform,
                            src_crs=src_crs,
                            dst_transform=rasterio.windows.transform(window, transform),
                            dst_crs=dst_crs,
                            resampling=Resampling.nearest,
                            src_nodata=src_nodata,
                            dst_nodata=dst_nodata,
                            num_threads=_NUM_THREADS,
                        )
                    except Exception as reproject_error:
                        print(f"\n   [CHUNK ERROR] Failed at chunk ({chunk_x}, {chunk_y}), band {band_idx}")
                        print(f"   [CHUNK ERROR] Window: {window}")
                        print(f"   [CHUNK ERROR] Error type: {type(reproject_error).__name__}")
                        print(f"   [CHUNK ERROR] Error message: {str(reproject_error)}")

                        # Check if it's a streaming issue
                        if ("curl" in str(reproject_error).lower() or
                            "vsi" in str(reproject_error).lower() or
                            "chunk and warp" in str(reproject_error).lower()):

                            # Check if we're actually streaming
                            if "/vsis3/" in str(getattr(src, 'name', '')):
                                print(f"   [CHUNK ERROR] S3 streaming error detected")
                                raise Exception("STREAMING_CHUNK_ERROR: Need to retry with download")

                        # Try to recover by filling with nodata
                        print(f"   [CHUNK RECOVERY] Attempting recovery by filling with nodata")
                        chunk_data.fill(dst_nodata if dst_nodata is not None else 0)

                    # Fix NaN values
                    chunk_data, _ = check_and_fix_nan_values(
                        chunk_data, dst_nodata, src.dtypes[0], band_idx=None
                    )

                    # Write chunk
                    dst.write(chunk_data, band_idx, window=window)

                    del chunk_data
                    if chunk_config.get('aggressive_gc', False):
                        gc.collect()

                chunk_iterator.update(1)

        chunk_iterator.close()

        # Memory report after each band
        if chunk_config.get('enable_memory_monitoring', True):
            current_memory = get_memory_usage()
            print(f"      Memory after band {band_idx}: {current_memory:.1f} MB")

        # Aggressive GC after each band
        if chunk_config.get('aggressive_gc', False):
            gc.collect()

    # Don't build overviews here - will be done after file is closed
    print(f"   [CHUNKS] ✅ Processing complete")