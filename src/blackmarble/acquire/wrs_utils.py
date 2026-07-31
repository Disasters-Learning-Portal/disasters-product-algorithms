"""WRS-2 utilities for Landsat tile calculations using real USGS data."""

from pathlib import Path

import geopandas as gpd
from shapely.geometry import box

from blackmarble.typing import BBox


def get_wrs_tiles_for_bbox_tuple(
    bbox: BBox,
) -> list[dict[str, int]]:
    """Get Landsat WRS-2 tiles that intersect with a bounding box.

    Uses the WRS-2 shapefile to find only tiles that actually intersect
    the given bounding box, dramatically reducing unnecessary API calls.

    Args:
        bbox: Bounding box (min_lon, min_lat, max_lon, max_lat)

    Returns:
        List of dictionaries with 'path' and 'row' keys for intersecting tiles
    """
    # Load WRS-2 shapefile from data directory
    wrs_path = Path(__file__).parent.parent / "data" / "WRS2_descending" / "WRS2_descending.shp"

    if not wrs_path.exists():
        raise FileNotFoundError(
            f"WRS-2 shapefile not found. Expected at: {wrs_path}\n"
            "Please ensure the WRS2_descending shapefile is available in blackmarble/data/"
        )

    # Load shapefile and find intersecting tiles
    wrs_gdf = gpd.read_file(wrs_path)

    # Create bbox polygon
    min_lon, min_lat, max_lon, max_lat = bbox
    bbox_poly = box(min_lon, min_lat, max_lon, max_lat)

    # Find intersecting tiles
    intersecting = wrs_gdf[wrs_gdf.intersects(bbox_poly)]

    # Extract path/row info
    tiles: list[dict[str, int]] = []
    for _, row in intersecting.iterrows():
        if "PATH" in row and "ROW" in row:
            path = int(row["PATH"])
            row_num = int(row["ROW"])
            tiles.append({"path": path, "row": row_num})

    if not tiles:
        raise ValueError(
            f"No WRS-2 tiles found for bbox {bbox}. Please check your bounding box coordinates."
        )

    return tiles


__all__ = ["get_wrs_tiles_for_bbox_tuple"]
