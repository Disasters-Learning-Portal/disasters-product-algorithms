"""Landsat-specific data preparation functions.

This module provides functions for processing Landsat data in the pipeline,
including date-by-date processing with QA masking applied before temporal compositing.
"""

import logging
import os
from typing import Any, Literal, TypedDict

import numpy as np
import rasterio
import rasterio.windows
from pyproj import Transformer
from rasterio.warp import Resampling, reproject, transform_bounds

from blackmarble.typing import ArrayLike, BBox

from ..logging_utils import get_logger
from .landsat_qa import create_landsat_cloud_mask, print_qa_pixel_summary
from .window_utils import calculate_window_offsets, round_window_offsets_correctly


logger = get_logger(__name__)


MARGIN_PIXELS = 120  # ~3.6km at 30m resolution


class RasterProfile(TypedDict):
    driver: str
    dtype: str
    width: int
    height: int
    count: int
    crs: Any
    transform: Any
    nodata: float | int | None


def _load_and_mask_tile(
    qa_path: str,
    band_path: str,
    window_expanded: rasterio.windows.Window,
    dilate_pixels: int,
    cloud_strategy: Literal["conservative", "moderate", "permissive"],
) -> tuple[ArrayLike, "rasterio.Affine"]:
    """
    Read matching QA_PIXEL and spectral-band windows from one tile, create a
    cloud mask, apply it immediately, and return the masked band data together
    with the transform valid for the expanded window.

    The caller is responsible for re-projecting the masked output to the
    common mosaic grid.
    """

    # --- open QA and band files in parallel ---
    with rasterio.open(qa_path) as qsrc, rasterio.open(band_path) as bsrc:
        # Safety: ensure both tiles are in the same native CRS
        if qsrc.crs != bsrc.crs:
            raise ValueError(f"CRS mismatch for tile pair:\n  QA  : {qa_path}\n  BAND: {band_path}")

        # Read the EXPANDED window (boundless so shape always matches)
        qa_arr = qsrc.read(1, window=window_expanded, boundless=True, fill_value=0).astype(
            np.uint16
        )

        band_arr = bsrc.read(1, window=window_expanded, boundless=True, fill_value=0).astype(
            np.float32
        )

        # Window-specific transform (identical for QA and band)
        win_transform = rasterio.windows.transform(window_expanded, bsrc.transform)

    # --- per-tile cloud mask ---
    mask = create_landsat_cloud_mask(
        qa_pixel=qa_arr,
        strategy=cloud_strategy,
        dilate_pixels=dilate_pixels,
    )

    # --- apply mask *in-place* ---
    band_arr[~mask] = np.nan
    return band_arr, win_transform


def process_landsat_date(
    bands_data: dict[str, tuple[list[str], list[str], list[str], list[str]]],
    bbox: BBox,
    processing_crs: Any,
    reference_transform: Any,
    reference_shape: tuple[int, int],
    apply_qa_mask: bool = True,
    dilate_pixels: int = 3,
    cloud_strategy: Literal["conservative", "moderate", "permissive"] = "moderate",
    save_diagnostics: bool = False,
    date_str: str | None = None,
    diagnostics_dir: str | None = None,
) -> dict[str, tuple[ArrayLike, Any]]:
    """Process all Landsat bands for a single date with QA masking.

    This function loads and processes multiple Landsat bands from the same
    acquisition date, applying QA masks before the data is used for temporal
    compositing. This ensures temporal consistency in cloud masking.

    Args:
        bands_data: Dictionary from download_all_bands_for_date containing
                   band data for a single date
        bbox: Bounding box for cropping (min_lon, min_lat, max_lon, max_lat)
        processing_crs: Precomputed processing CRS to reuse
        reference_transform: Precomputed processing transform to reuse
        reference_shape: Precomputed processing shape (height, width) to reuse
        apply_qa_mask: Whether to apply QA-based cloud masking
        dilate_pixels: Number of pixels to dilate cloud mask
        cloud_strategy: Cloud masking strategy ("conservative", "moderate", or "permissive")
        save_diagnostics: Whether to save diagnostic outputs
        date_str: Date string for diagnostic filenames
        diagnostics_dir: Base diagnostics directory (e.g., "./data/diagnostics")

    Returns:
        Dictionary mapping band names to tuples of (masked_data, profile)

    Example:
        >>> bands_data = download_all_bands_for_date(2023, 9, 15, bbox)
        >>> processed = process_landsat_date(
        ...     bands_data,
        ...     bbox,
        ...     processing_crs,
        ...     reference_transform,
        ...     reference_shape,
        ... )
        >>> b2_masked = processed['B2'][0]  # Masked Band 2 data
    """
    result: dict[str, tuple[ArrayLike, Any]] = {}

    output_crs = processing_crs
    transform = reference_transform
    height, width = reference_shape

    # First, process QA_PIXEL if available and masking is requested
    cloud_mask = None
    qa_profile: RasterProfile | None = None
    qa_files: list[str] = []

    if apply_qa_mask and "QA_PIXEL" in bands_data:
        qa_files, _, _, _ = bands_data["QA_PIXEL"]

        if qa_files:
            logger.debug("Processing QA_PIXEL from %d tiles", len(qa_files))

            # For multiple tiles, always use windowed mosaic approach
            if len(qa_files) > 1:
                logger.debug("Creating windowed mosaic from %d QA tiles...", len(qa_files))

                # Use precomputed processing grid

                logger.debug("Using locally-optimized Albers Equal Area projection")
                logger.debug("Grid dimensions: %d x %d at 30m resolution", width, height)

                qa_data = np.zeros((height, width), dtype=np.uint16)
                qa_profile = {
                    "driver": "GTiff",
                    "dtype": "uint16",
                    "width": width,
                    "height": height,
                    "count": 1,
                    "crs": output_crs,
                    "transform": transform,
                    "nodata": None,
                }

                # Process each file with windowed read and reproject
                for i, qa_tile_file in enumerate(qa_files):
                    logger.debug(
                        "Processing QA tile %d/%d: %s",
                        i + 1,
                        len(qa_files),
                        os.path.basename(qa_tile_file),
                    )

                    # Read window from source file
                    with rasterio.open(qa_tile_file) as src:
                        # Transform bbox to source CRS
                        if src.crs and src.crs.to_epsg() != 4326:
                            transformer = Transformer.from_crs(
                                "EPSG:4326", str(src.crs), always_xy=True
                            )
                            minx, miny = transformer.transform(bbox[0], bbox[1])
                            maxx, maxy = transformer.transform(bbox[2], bbox[3])
                            bbox_src = (minx, miny, maxx, maxy)
                        else:
                            bbox_src = bbox

                        # Get window for bbox
                        window = rasterio.windows.from_bounds(
                            bbox_src[0],
                            bbox_src[1],
                            bbox_src[2],
                            bbox_src[3],
                            transform=src.transform,
                        )

                        # Expand window by margin to avoid gaps at UTM zone boundaries
                        window_expanded = rasterio.windows.Window(
                            col_off=window.col_off - MARGIN_PIXELS,
                            row_off=window.row_off - MARGIN_PIXELS,
                            width=window.width + 2 * MARGIN_PIXELS,
                            height=window.height + 2 * MARGIN_PIXELS,
                        )

                        # Read expanded window
                        window_data = src.read(
                            1, window=window_expanded, boundless=True, fill_value=0
                        )

                        # Calculate the destination window in output grid (for EXPANDED window)

                        # Get bounds of the EXPANDED window in source CRS
                        src_window_bounds = rasterio.windows.bounds(window_expanded, src.transform)

                        # Transform to output CRS
                        if src.crs is None:
                            raise ValueError(f"Missing CRS for QA source tile: {qa_tile_file}")

                        dst_bounds = transform_bounds(
                            src.crs,
                            qa_profile["crs"],
                            *src_window_bounds,
                        )
                        dst_window = rasterio.windows.from_bounds(
                            *dst_bounds, transform=qa_profile["transform"]
                        )

                        # Round the window to integer pixels using correct rounding
                        # for north-up rasters
                        dst_window_int = round_window_offsets_correctly(
                            dst_window, qa_profile["transform"]
                        )

                        # Reproject to a temporary array matching the destination window size
                        dst_height = int(dst_window_int.height)
                        dst_width = int(dst_window_int.width)
                        tile_data = np.zeros((dst_height, dst_width), dtype=qa_data.dtype)

                        # Calculate transform for the expanded window
                        window_transform = rasterio.windows.transform(
                            window_expanded, src.transform
                        )

                        reproject(
                            source=window_data,
                            destination=tile_data,
                            src_transform=window_transform,
                            src_crs=src.crs,
                            dst_transform=rasterio.windows.transform(
                                dst_window, qa_profile["transform"]
                            ),
                            dst_crs=qa_profile["crs"],
                            resampling=Resampling.nearest,
                        )

                        # Calculate where to place this tile in the full mosaic
                        # DEBUG: Log exact offsets before rounding
                        logger.debug("Exact dst_window.row_off = %.6f", dst_window.row_off)
                        logger.debug("Exact dst_window.col_off = %.6f", dst_window.col_off)
                        logger.debug(
                            "Delta from rounding - row: %.6f, col: %.6f",
                            dst_window.row_off - dst_window_int.row_off,
                            dst_window.col_off - dst_window_int.col_off,
                        )

                        # Use the already-rounded window offsets
                        dst_row_off = int(dst_window_int.row_off)
                        dst_col_off = int(dst_window_int.col_off)

                        logger.debug("Final dst_row_off = %d", dst_row_off)
                        logger.debug("Final dst_col_off = %d", dst_col_off)

                        # Calculate window offsets for array slicing
                        offsets = calculate_window_offsets(
                            dst_row_off, dst_col_off, dst_height, dst_width, qa_data.shape
                        )

                        # Extract slices
                        mosaic_slice = qa_data[
                            offsets.mosaic_row_start : offsets.mosaic_row_end,
                            offsets.mosaic_col_start : offsets.mosaic_col_end,
                        ]

                        tile_data_cropped = tile_data[
                            offsets.tile_row_start : offsets.tile_row_start
                            + (offsets.mosaic_row_end - offsets.mosaic_row_start),
                            offsets.tile_col_start : offsets.tile_col_start
                            + (offsets.mosaic_col_end - offsets.mosaic_col_start),
                        ]

                        # Fill only where we don't have data
                        if tile_data_cropped.shape == mosaic_slice.shape:
                            # 0 or NaN in the mosaic means "needs data"
                            nodata_mask = (mosaic_slice == 0) | np.isnan(mosaic_slice)
                            # any finite (non-NaN) value from the tile is valid
                            valid_tile_mask = ~np.isnan(tile_data_cropped)
                            fill_mask = nodata_mask & valid_tile_mask

                            if fill_mask.any():
                                mosaic_slice[fill_mask] = tile_data_cropped[fill_mask]
                                logger.debug("Filled %d pixels", fill_mask.sum())
                        else:
                            logger.warning(
                                "Shape mismatch after cropping - mosaic: %s, tile: %s",
                                mosaic_slice.shape,
                                tile_data_cropped.shape,
                            )
            else:
                # Single file case - still need to reproject to processing grid for consistency
                logger.debug("Processing single QA_PIXEL tile...")

                # Use precomputed processing grid

                qa_profile = {
                    "driver": "GTiff",
                    "dtype": "uint16",
                    "width": width,
                    "height": height,
                    "count": 1,
                    "crs": output_crs,
                    "transform": transform,
                    "nodata": None,
                }

                # Read and reproject the single file
                with rasterio.open(qa_files[0]) as src:
                    # Read the entire file
                    src_data = src.read(1)

                    # Create output array
                    qa_data = np.zeros((height, width), dtype=np.uint16)

                    # Reproject to processing grid
                    reproject(
                        source=src_data,
                        destination=qa_data,
                        src_transform=src.transform,
                        src_crs=src.crs,
                        dst_transform=qa_profile["transform"],
                        dst_crs=qa_profile["crs"],
                        resampling=Resampling.nearest,
                    )

                    logger.debug("Reprojected from %s to %s", src.crs, qa_profile["crs"])

            # Debug: check data shape
            logger.debug("QA data shape: %s", qa_data.shape)
            logger.debug("QA data dtype: %s", qa_data.dtype)

            # Data is already cropped by windowed read, no need for additional cropping

            # Print QA pixel summary only in diagnostics/debug mode
            if save_diagnostics or logger.isEnabledFor(logging.DEBUG):
                print_qa_pixel_summary(qa_data)

            # Check if QA data is empty
            if qa_data.size == 0:
                logger.warning("QA_PIXEL data is empty after loading/cropping!")
                cloud_mask = None
            else:
                # Create cloud mask using bit-based approach
                cloud_mask = create_landsat_cloud_mask(
                    qa_pixel=qa_data,
                    strategy=cloud_strategy,
                    dilate_pixels=dilate_pixels,
                )

                # Debug cloud mask
                mask_clear = cloud_mask.sum()
                mask_total = cloud_mask.size

                if mask_total > 0:
                    logger.debug("Cloud mask coverage: %.1f%% clear", mask_clear / mask_total * 100)
                else:
                    logger.warning("Cloud mask is empty!")

                # Save cloud mask diagnostic if requested
                if save_diagnostics and date_str:
                    diag_base = diagnostics_dir or "data/diagnostics"
                    diag_dir = f"{diag_base}/cloud_masks"
                    os.makedirs(diag_dir, exist_ok=True)

                    # Save as GeoTIFF
                    mask_path = f"{diag_dir}/cloud_mask_{date_str}.tif"
                    with rasterio.open(mask_path, mode="w", **qa_profile) as dst:
                        dst.write(cloud_mask.astype(np.uint8), 1)
                    logger.debug("Saved cloud mask to: %s", mask_path)

    # Process each spectral band
    for band_name, (band_files, _, _, _) in bands_data.items():
        if band_name == "QA_PIXEL" or not band_files:
            continue

        logger.debug("Processing %s from %d tiles", band_name, len(band_files))
        # For multiple tiles, always use windowed mosaic approach
        if len(band_files) > 1:
            logger.debug("Creating windowed mosaic from %d tiles...", len(band_files))

            # Use the same grid as QA_PIXEL if available
            if qa_profile is not None:
                # Reuse the same grid
                band_profile: RasterProfile = {
                    "driver": qa_profile["driver"],
                    "dtype": "float32",
                    "width": qa_profile["width"],
                    "height": qa_profile["height"],
                    "count": qa_profile["count"],
                    "crs": qa_profile["crs"],
                    "transform": qa_profile["transform"],
                    "nodata": 0,
                }
                width = band_profile["width"]
                height = band_profile["height"]
            else:
                # Define common output grid (same logic as QA)

                band_profile = {
                    "driver": "GTiff",
                    "dtype": "float32",
                    "width": width,
                    "height": height,
                    "count": 1,
                    "crs": output_crs,
                    "transform": transform,
                    "nodata": 0,
                }

            band_data = np.zeros((height, width), dtype=np.float32)

            # Process each file with windowed read and reproject
            for i, band_file in enumerate(band_files):
                logger.debug(
                    "Processing %s tile %d/%d: %s",
                    band_name,
                    i + 1,
                    len(band_files),
                    os.path.basename(band_file),
                )

                matched_qa_file: str | None = qa_files[i] if i < len(qa_files) else None
                if matched_qa_file is None:
                    logger.warning("No matching QA_PIXEL file for %s - skipping tile", band_file)
                    continue

                with rasterio.open(band_file) as src:
                    # Transform bbox to source CRS
                    if src.crs and src.crs.to_epsg() != 4326:
                        transformer = Transformer.from_crs(
                            "EPSG:4326", str(src.crs), always_xy=True
                        )
                        minx, miny = transformer.transform(bbox[0], bbox[1])
                        maxx, maxy = transformer.transform(bbox[2], bbox[3])
                        bbox_src = (minx, miny, maxx, maxy)
                    else:
                        bbox_src = bbox

                    # Build ORIGINAL window then expand symmetrically (allow negatives)
                    window = rasterio.windows.from_bounds(
                        bbox_src[0], bbox_src[1], bbox_src[2], bbox_src[3], transform=src.transform
                    )
                    window_expanded = rasterio.windows.Window(
                        col_off=window.col_off - MARGIN_PIXELS,
                        row_off=window.row_off - MARGIN_PIXELS,
                        width=window.width + 2 * MARGIN_PIXELS,
                        height=window.height + 2 * MARGIN_PIXELS,
                    )

                # --- NEW: load + mask this tile BEFORE any reprojection ---
                band_tile_data, window_transform = _load_and_mask_tile(
                    qa_path=matched_qa_file,
                    band_path=band_file,
                    window_expanded=window_expanded,
                    dilate_pixels=dilate_pixels,
                    cloud_strategy=cloud_strategy,
                )

                # Calculate the destination window in output grid (for EXPANDED window)
                if src.crs is None:
                    raise ValueError(f"Missing CRS for source tile: {band_file}")

                dst_bounds = transform_bounds(
                    src.crs,
                    band_profile["crs"],
                    *rasterio.windows.bounds(window_expanded, src.transform),
                )
                dst_window = rasterio.windows.from_bounds(
                    *dst_bounds, transform=band_profile["transform"]
                )

                dst_window_int = round_window_offsets_correctly(
                    dst_window, band_profile["transform"]
                )

                dst_height = int(dst_window_int.height)
                dst_width = int(dst_window_int.width)
                tile_data_reprojected = np.zeros((dst_height, dst_width), dtype=band_data.dtype)

                reproject(
                    source=band_tile_data,
                    destination=tile_data_reprojected,
                    src_transform=window_transform,
                    src_crs=src.crs,
                    dst_transform=band_profile["transform"],
                    dst_crs=band_profile["crs"],
                    resampling=Resampling.nearest,
                    src_nodata=np.nan,
                    dst_nodata=0,
                )

                # Calculate where to place this tile in the full mosaic
                # DEBUG: Log exact offsets before rounding
                logger.debug("Exact dst_window.row_off = %.6f", dst_window.row_off)
                logger.debug("Exact dst_window.col_off = %.6f", dst_window.col_off)
                logger.debug(
                    "Delta from rounding - row: %.6f, col: %.6f",
                    dst_window.row_off - dst_window_int.row_off,
                    dst_window.col_off - dst_window_int.col_off,
                )

                # Use the already-rounded window offsets
                dst_row_off = int(dst_window_int.row_off)
                dst_col_off = int(dst_window_int.col_off)

                logger.debug("Final dst_row_off = %d", dst_row_off)
                logger.debug("Final dst_col_off = %d", dst_col_off)

                # Calculate window offsets for array slicing
                offsets = calculate_window_offsets(
                    dst_row_off, dst_col_off, dst_height, dst_width, band_data.shape
                )

                # Extract slices
                band_mosaic_slice = band_data[
                    offsets.mosaic_row_start : offsets.mosaic_row_end,
                    offsets.mosaic_col_start : offsets.mosaic_col_end,
                ]

                band_tile_data_cropped = tile_data_reprojected[
                    offsets.tile_row_start : offsets.tile_row_start
                    + (offsets.mosaic_row_end - offsets.mosaic_row_start),
                    offsets.tile_col_start : offsets.tile_col_start
                    + (offsets.mosaic_col_end - offsets.mosaic_col_start),
                ]

                # Fill only where we don't have data
                if band_tile_data_cropped.shape == band_mosaic_slice.shape:
                    # 0 or NaN in the mosaic means "needs data"
                    nodata_mask = (band_mosaic_slice == 0) | np.isnan(band_mosaic_slice)
                    # any finite (non-NaN) value from the tile is valid
                    valid_tile_mask = ~np.isnan(band_tile_data_cropped)
                    fill_mask = nodata_mask & valid_tile_mask

                    if fill_mask.any():
                        band_mosaic_slice[fill_mask] = band_tile_data_cropped[fill_mask]
                        logger.debug("Filled %d pixels", fill_mask.sum())
                else:
                    logger.warning(
                        "Shape mismatch after cropping - mosaic: %s, tile: %s",
                        band_mosaic_slice.shape,
                        band_tile_data_cropped.shape,
                    )

            logger.debug(f"    Final mosaic has {(band_data > 0).sum()} valid pixels")
            logger.debug(f"    Final mosaic shape: {band_data.shape}")
        else:
            # Single file case - still need to reproject to processing grid for consistency
            logger.debug("Processing single %s tile...", band_name)

            # Use the same grid as QA_PIXEL if available
            if qa_profile is not None:
                # Reuse the same grid
                band_profile = {
                    "driver": qa_profile["driver"],
                    "dtype": "float32",
                    "width": qa_profile["width"],
                    "height": qa_profile["height"],
                    "count": qa_profile["count"],
                    "crs": qa_profile["crs"],
                    "transform": qa_profile["transform"],
                    "nodata": 0,
                }
                width = band_profile["width"]
                height = band_profile["height"]
            else:
                # Define common output grid

                band_profile = {
                    "driver": "GTiff",
                    "dtype": "float32",
                    "width": width,
                    "height": height,
                    "count": 1,
                    "crs": output_crs,
                    "transform": transform,
                    "nodata": 0,
                }

            # Read and reproject the single file
            if qa_files and apply_qa_mask:
                # Apply cloud masking
                with rasterio.open(band_files[0]) as src_band, rasterio.open(qa_files[0]):
                    # Read entire file (no bbox window) and mask immediately
                    full_window = rasterio.windows.Window(0, 0, src_band.width, src_band.height)
                    tile_data, window_transform = _load_and_mask_tile(
                        qa_path=qa_files[0],
                        band_path=band_files[0],
                        window_expanded=full_window,
                        dilate_pixels=dilate_pixels,
                        cloud_strategy=cloud_strategy,
                    )

                # Create output array and reproject masked tile to processing grid
                band_data = np.zeros((height, width), dtype=np.float32)
                reproject(
                    source=tile_data,
                    destination=band_data,
                    src_transform=window_transform,
                    src_crs=src_band.crs,
                    dst_transform=band_profile["transform"],
                    dst_crs=band_profile["crs"],
                    resampling=Resampling.nearest,
                    src_nodata=np.nan,
                    dst_nodata=0,
                )

                logger.debug("Reprojected from %s to %s", src_band.crs, band_profile["crs"])
            else:
                # No cloud masking - read directly
                with rasterio.open(band_files[0]) as src:
                    # Read the entire file
                    src_data = src.read(1)

                    # Create output array
                    band_data = np.zeros((height, width), dtype=np.float32)

                    # Reproject to processing grid
                    reproject(
                        source=src_data,
                        destination=band_data,
                        src_transform=src.transform,
                        src_crs=src.crs,
                        dst_transform=band_profile["transform"],
                        dst_crs=band_profile["crs"],
                        resampling=Resampling.nearest,
                        src_nodata=0,
                        dst_nodata=0,
                    )

                    logger.debug("Reprojected from %s to %s", src.crs, band_profile["crs"])

        # Data is already cropped by windowed read, no need for additional cropping

        # Check if band data is empty
        if band_data.size == 0:
            logger.warning("%s data is empty after loading/cropping!", band_name)
            masked_data = band_data
        else:
            # Data were cloud-masked per tile before mosaicking
            masked_data = band_data

        result[band_name] = (masked_data, band_profile)

    # Log summary of processed data
    logger.debug("Processing summary:")
    for band_name, (data, _profile) in result.items():
        valid_pixels = ~np.isnan(data) if np.issubdtype(data.dtype, np.floating) else data != 0
        coverage = np.sum(valid_pixels) / data.size * 100 if data.size > 0 else 0
        logger.debug(
            f"  {band_name}: shape={data.shape}, dtype={data.dtype}, coverage={coverage:.1f}%"
        )
        if coverage > 0:
            valid_data = data[valid_pixels]
            logger.debug(f"    Range: [{np.min(valid_data):.3f}, {np.max(valid_data):.3f}]")

    return result
