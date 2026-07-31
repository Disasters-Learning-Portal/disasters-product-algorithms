"""Fractional road rasterization using aligned sub-pixel grids.

This module provides fractional coverage values (0.0-1.0) for roads
by rasterizing at 6m resolution (5x5 sub-pixels per 30m Landsat pixel)
and averaging to get the fraction of sub-pixels covered.
"""

import logging
import os
from typing import Any

import geopandas as gpd
import numpy as np
import rasterio
import rasterio.features
from rasterio.crs import CRS
from rasterio.transform import Affine
from shapely.geometry import mapping

from blackmarble.typing import ArrayLike, BBox


# Create logger for spatial debugging
log = logging.getLogger("bm.spatial")

# Enable debug logging if environment variable is set

if os.environ.get("BM_SPATIAL_DEBUG", "").lower() in ("1", "true", "yes"):
    log.setLevel(logging.DEBUG)

# Default buffer sizes by road type (in meters)
DEFAULT_BUFFER_BY_TYPE = {
    "motorway": 15,  # ~30m total width
    "motorway_link": 12,
    "trunk": 12,
    "trunk_link": 10,
    "primary": 8,  # ~16m total width
    "primary_link": 6,
    "secondary": 6,
    "secondary_link": 5,
    "tertiary": 5,
    "tertiary_link": 4,
    "residential": 3,  # ~6m total width
    "service": 2,
    "unclassified": 2,
    "road": 2,
    "living_street": 3,
    "pedestrian": 2,
    "track": 2,
    "bus_guideway": 4,
    "busway": 4,
    "footway": 1,
    "cycleway": 1,
    "path": 1,
}


def buffer_and_rasterize_roads_fractional(
    roads_gdf: gpd.GeoDataFrame,
    bbox: BBox,
    transform: Affine,
    crs: str | CRS,
    shape: tuple[int, int],
    buffer_meters: float | dict[str, float] = 30.0,
    sub_pixels: int = 5,
    hierarchical: bool = False,
) -> ArrayLike:
    """Buffer and rasterize roads with fractional coverage values.

    Uses aligned sub-pixel rasterization: creates a 6m grid (30m/5) that
    perfectly aligns with the 30m Landsat grid. Each 30m pixel contains
    exactly 5x5 6m sub-pixels. The fractional coverage is the mean of
    these 25 sub-pixels.

    Args:
        roads_gdf: GeoDataFrame of road segments
        bbox: Bounding box (min_lon, min_lat, max_lon, max_lat)
        transform: Target transform for 30m output
        crs: Target CRS
        shape: Target shape (height, width) at 30m
        buffer_meters: Buffer distance in meters or dict mapping road types to buffer distances
        sub_pixels: Sub-pixels per dimension (5 = 5x5 = 25 total)
        hierarchical: If True, raises NotImplementedError

    Returns:
        Array with fractional coverage values 0.0-1.0
    """
    log.debug("Entering buffer_and_rasterize_roads_fractional")
    log.debug(f"  CRS parameter: {crs}")
    log.debug(f"  Transform: {transform}")
    log.debug(f"  Shape: {shape}")

    if hierarchical:
        raise NotImplementedError(
            "Hierarchical road values not yet implemented for fractional rasterization. "
            "Set hierarchical=False to use binary fractional coverage."
        )

    edges_gdf = roads_gdf.copy()
    if edges_gdf.crs is None:
        log.warning("Road GeoDataFrame missing CRS, assuming EPSG:4326")
        edges_gdf = edges_gdf.set_crs("EPSG:4326")

    log.debug(f"Got {len(edges_gdf)} edges with CRS: {edges_gdf.crs}")

    if len(edges_gdf) == 0:
        log.error("No edges in GeoDataFrame")
        return np.zeros(shape, dtype=np.float32)

    # Log initial state
    log.debug("STEP 1: edges_gdf created from graph")
    log.debug(f"  - CRS: {edges_gdf.crs}")
    log.debug(f"  - Bounds: {edges_gdf.total_bounds}")
    log.debug(f"  - Num edges: {len(edges_gdf)}")
    if len(edges_gdf) > 0:
        centroid = edges_gdf.union_all().centroid
        log.debug(f"  - Centroid: ({centroid.x:.6f}, {centroid.y:.6f})")

    # Buffer roads in meters (project to UTM for accuracy)
    utm_crs = edges_gdf.estimate_utm_crs()
    log.debug(f"Estimated UTM CRS for buffering: {utm_crs}")
    edges_utm = edges_gdf.to_crs(utm_crs)

    # Apply variable buffering by road type
    if isinstance(buffer_meters, dict):
        # Use type-specific buffers
        buffer_dict = buffer_meters
        default_buffer = 3.0  # Default for unknown types

        # Apply buffer based on highway type
        def apply_buffer(row: Any) -> Any:
            highway_type = row.get("highway", "unclassified")
            if isinstance(highway_type, list):
                highway_type = highway_type[0]
            buffer_size = buffer_dict.get(str(highway_type), default_buffer)
            return row.geometry.buffer(buffer_size)

        edges_utm["geometry"] = edges_utm.apply(apply_buffer, axis=1)
        log.debug(f"Applied variable buffering to {len(edges_utm)} road segments")

        # Log buffer statistics
        unique_types = edges_utm["highway"].value_counts() if "highway" in edges_utm.columns else {}
        log.debug("Buffer sizes by road type:")
        for road_type, count in unique_types.items():
            if isinstance(road_type, list):
                road_type = road_type[0]
            buffer = buffer_dict.get(str(road_type), default_buffer)
            log.debug(f"  {road_type}: {buffer}m ({count} segments)")
    else:
        # Use uniform buffer
        edges_utm["geometry"] = edges_utm.geometry.buffer(buffer_meters)
        log.debug(f"Buffered {len(edges_utm)} road segments with uniform {buffer_meters}m buffer")

    # Convert to target CRS for rasterization
    # Ensure CRS is properly formatted
    target_crs = CRS.from_string(crs) if isinstance(crs, str) else crs

    log.debug(f"Target CRS for rasterization: {target_crs}")

    edges_buffered = edges_utm.to_crs(target_crs)
    log.debug("After projection to target CRS:")
    log.debug(f"  Bounds: {edges_buffered.total_bounds}")
    target_centroid = edges_buffered.union_all().centroid
    log.debug(f"  Centroid: ({target_centroid.x:.1f}, {target_centroid.y:.1f})")

    # Check the transform bounds and overlap
    raster_bounds = rasterio.transform.array_bounds(shape[0], shape[1], transform)

    log.debug("STEP 5: Checking overlap between roads and raster grid")
    log.debug("  - Raster grid info:")
    log.debug(f"    - Shape: {shape}")
    log.debug(f"    - Transform: {transform}")
    log.debug(f"    - Bounds: {raster_bounds}")

    # Calculate raster centroid
    raster_minx, raster_miny, raster_maxx, raster_maxy = raster_bounds
    raster_center_x = (raster_minx + raster_maxx) / 2
    raster_center_y = (raster_miny + raster_maxy) / 2
    log.debug(f"    - Centroid: ({raster_center_x:.1f}, {raster_center_y:.1f})")

    # Calculate distance between centroids
    if len(edges_buffered) > 0:
        distance = np.sqrt(
            (target_centroid.x - raster_center_x) ** 2 + (target_centroid.y - raster_center_y) ** 2
        )
        log.debug(f"  - Distance between centroids: {distance:.1f}m ({distance / 1000:.1f}km)")

    # Sanity check: ensure bounds overlap
    edges_minx, edges_miny, edges_maxx, edges_maxy = edges_buffered.total_bounds
    raster_minx, raster_miny, raster_maxx, raster_maxy = raster_bounds

    # Check for overlap
    x_overlap = not (edges_maxx < raster_minx or edges_minx > raster_maxx)
    y_overlap = not (edges_maxy < raster_miny or edges_miny > raster_maxy)

    log.debug("  - Overlap check:")
    log.debug(f"    - X-axis overlap: {x_overlap}")
    log.debug(f"      Roads X: [{edges_minx:.1f}, {edges_maxx:.1f}]")
    log.debug(f"      Raster X: [{raster_minx:.1f}, {raster_maxx:.1f}]")
    left_gap = max(0, raster_minx - edges_maxx)
    right_gap = max(0, edges_minx - raster_maxx)
    log.debug(f"      Gap: {left_gap:.1f}m (left), {right_gap:.1f}m (right)")
    log.debug(f"    - Y-axis overlap: {y_overlap}")
    log.debug(f"      Roads Y: [{edges_miny:.1f}, {edges_maxy:.1f}]")
    log.debug(f"      Raster Y: [{raster_miny:.1f}, {raster_maxy:.1f}]")
    bottom_gap = max(0, raster_miny - edges_maxy)
    top_gap = max(0, edges_miny - raster_maxy)
    log.debug(f"      Gap: {bottom_gap:.1f}m (bottom), {top_gap:.1f}m (top)")

    if not (x_overlap and y_overlap):
        log.warning("⚠️  WARNING: No overlap between roads and raster!")
        log.warning("  This will result in zero roads being rasterized")

        # This will result in zero roads being rasterized

    # Convert to shapes
    shapes = [mapping(geom) for geom in edges_buffered.geometry]
    log.debug(f"Processing {len(shapes):,} buffered road segments")

    # Create sub-pixel grid (6m for 30m/5)
    height_30m, width_30m = shape
    height_6m = height_30m * sub_pixels
    width_6m = width_30m * sub_pixels

    # Check memory requirements
    memory_gb = (height_6m * width_6m) / (1024**3)
    log.debug(
        f"Sub-pixel grid size: {height_6m:,} x {width_6m:,} = {height_6m * width_6m:,} pixels"
    )
    log.debug(f"Estimated memory requirement: {memory_gb:.2f} GB")

    if memory_gb > 4.0:
        log.warning("Large memory requirement!")
        log.warning("Consider using binary rasterization (osm_fractional: false) for large areas")

    # For extremely large areas, process in chunks
    if memory_gb > 8.0 or len(shapes) > 2_000_000:
        print("stats: ", memory_gb, len(shapes))
        log.error("Area too large for fractional rasterization")
        log.error("Please use binary rasterization instead (set osm_fractional: false)")
        return np.zeros(shape, dtype=np.float32)

    # Scale transform for 6m resolution
    transform_6m = transform * Affine.scale(1 / sub_pixels, 1 / sub_pixels)

    # Rasterize at 6m resolution
    # Use all_touched=False for more precise sub-pixel coverage
    try:
        raster_6m = rasterio.features.rasterize(
            shapes,
            out_shape=(height_6m, width_6m),
            transform=transform_6m,
            fill=0,
            all_touched=False,
            dtype="uint8",
        )
    except MemoryError:
        log.error("Out of memory during rasterization!")
        log.error("Falling back to zeros array")
        return np.zeros(shape, dtype=np.float32)

    # Calculate fractional coverage for each 30m pixel
    # Reshape to group sub-pixels, then take mean
    raster_30m_fractional = (
        raster_6m.reshape(height_30m, sub_pixels, width_30m, sub_pixels)
        .mean(axis=(1, 3))
        .astype(np.float32)
    )

    # Log statistics
    pixels_with_roads = np.sum(raster_30m_fractional > 0)
    if pixels_with_roads > 0:
        min_frac = raster_30m_fractional[raster_30m_fractional > 0].min()
        max_frac = raster_30m_fractional.max()
        log.info(f"Fractional road rasterization: {pixels_with_roads} pixels with roads")
        log.info(f"Coverage range: {min_frac:.3f} to {max_frac:.3f}")
    else:
        log.warning("No roads rasterized at 30m resolution")
        log.warning(f"  6m raster had {np.sum(raster_6m > 0)} pixels with roads")
        log.warning("  Check projection and buffer size")

    return raster_30m_fractional
