"""Coordinate Reference System utilities for Black Marble pipeline.

This module provides functions for creating locally-optimized projections
that minimize distortion for each area of interest.
"""

import hashlib
import logging
from functools import lru_cache
from typing import Any

import numpy as np
from pyproj import CRS, Transformer
from rasterio.transform import from_bounds


logger = logging.getLogger(__name__)


@lru_cache(maxsize=128)
def make_local_albers(west: float, south: float, east: float, north: float) -> CRS:
    """Create a locally-optimized Albers Equal Area projection for an AOI.

    This creates an Albers Equal Area projection centered on the bounding box
    with standard parallels placed at 1/6 and 5/6 of the latitude range.
    This minimizes distortion within the area of interest while preserving
    area for accurate scientific calculations.

    Args:
        west: Western longitude bound (-180 to 180)
        south: Southern latitude bound (-90 to 90)
        east: Eastern longitude bound (-180 to 180)
        north: Northern latitude bound (-90 to 90)

    Returns:
        CRS object for the locally-optimized Albers projection

    Raises:
        ValueError: If bounds are invalid
    """
    # Validation
    if south >= north:
        raise ValueError(f"South ({south}) must be less than north ({north})")
    if west >= east:
        raise ValueError(f"West ({west}) must be less than east ({east})")

    lat_span = north - south
    if lat_span < 0.05:
        raise ValueError(f"Latitude span ({lat_span}°) too small for stable projection")

    # Warn about extreme latitudes
    if abs(south) > 80 or abs(north) > 80:
        logger.warning(
            f"Latitude range ({south:.1f}° to {north:.1f}°) includes extreme values. "
            "Consider using a polar projection for better accuracy."
        )

    if west > 0 and east < 0:
        logger.warning(
            f"Bounding box may cross antimeridian (west={west}, east={east}). "
            "Consider normalizing longitudes or splitting the AOI."
        )

    # Calculate projection parameters
    lon_0 = (west + east) / 2
    lat_0 = (south + north) / 2
    lat_1 = south + lat_span / 6
    lat_2 = south + 5 * lat_span / 6

    # Create CRS
    return CRS.from_dict(
        {
            "proj": "aea",
            "lon_0": lon_0,
            "lat_0": lat_0,
            "lat_1": lat_1,
            "lat_2": lat_2,
            "datum": "WGS84",
            "units": "m",
            "no_defs": None,
            "type": "crs",
        }
    )


def get_processing_crs(bbox: tuple[float, float, float, float]) -> CRS:
    """Get the processing CRS for a given bounding box.

    This is the main entry point for getting the CRS to use for processing.
    Currently returns a locally-optimized Albers Equal Area projection.

    Args:
        bbox: Bounding box (west, south, east, north) in EPSG:4326

    Returns:
        CRS object for processing
    """
    return make_local_albers(*bbox)


def get_crs_hash(crs: CRS) -> str:
    """Get a short hash of a CRS for cache keys or filenames.

    Args:
        crs: The CRS to hash

    Returns:
        8-character hash string
    """
    wkt = crs.to_wkt()
    return hashlib.md5(wkt.encode()).hexdigest()[:8]


def create_processing_grid(
    bbox: tuple[float, float, float, float], resolution: float = 30.0
) -> tuple[CRS, Any, tuple[int, int]]:
    """Create the standard processing grid for a bounding box.

    This creates:
    1. A locally-optimized Albers Equal Area CRS
    2. A transform that snaps to exact pixel boundaries
    3. The dimensions (height, width) of the grid

    All processing in the pipeline should use this exact grid to ensure
    perfect alignment between Landsat, VIIRS, roads, and all other layers.

    Args:
        bbox: Bounding box (west, south, east, north) in EPSG:4326
        resolution: Grid resolution in meters (default: 30m)

    Returns:
        Tuple of (crs, transform, shape) where:
        - crs: The locally-optimized Albers CRS
        - transform: Rasterio affine transform for the grid
        - shape: (height, width) tuple for array dimensions
    """
    # Get the processing CRS
    crs = get_processing_crs(bbox)

    # Transform bbox to processing CRS
    transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    west_albers, south_albers = transformer.transform(bbox[0], bbox[1])
    east_albers, north_albers = transformer.transform(bbox[2], bbox[3])

    # Snap to pixel grid (align to multiples of resolution)
    # This ensures consistent grids even with slightly different bboxes
    min_x = np.floor(west_albers / resolution) * resolution
    min_y = np.floor(south_albers / resolution) * resolution
    max_x = np.ceil(east_albers / resolution) * resolution
    max_y = np.ceil(north_albers / resolution) * resolution

    # Calculate dimensions
    width = int((max_x - min_x) / resolution)
    height = int((max_y - min_y) / resolution)

    # Create transform
    transform = from_bounds(min_x, min_y, max_x, max_y, width, height)
    shape = (height, width)

    # Log grid details
    logger.info("Created processing grid:")
    center_lon = (bbox[0] + bbox[2]) / 2
    center_lat = (bbox[1] + bbox[3]) / 2
    logger.info(f"  CRS: Local Albers centered at {center_lon:.3f}°, {center_lat:.3f}°")
    logger.info(f"  Resolution: {resolution}m")
    logger.info(f"  Dimensions: {width} x {height} pixels")
    logger.info(f"  Bounds (Albers): ({min_x:.1f}, {min_y:.1f}, {max_x:.1f}, {max_y:.1f})")

    return crs, transform, shape
