"""OpenStreetMap road network data acquisition.

This module handles fetching road network data from OpenStreetMap using OSMnx,
and converting it to raster format for integration with satellite imagery.
"""

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Literal

import geopandas as gpd
import osmnx as ox
import pyproj
import rasterio
import rasterio.features
from rasterio.crs import CRS
from rasterio.warp import transform_geom
from shapely.geometry import mapping, shape
from shapely.ops import transform

from blackmarble.typing import ArrayLike, BBox

from ..logging_utils import get_logger


logger = get_logger(__name__)

# Import default buffer sizes from fractional module

# Type aliases
Transform = Any  # rasterio.transform.Affine


LAYERCAKE_DEFAULT_URL = "https://data.openstreetmap.us/layercake/highways.parquet"


def _get_osm_source_from_env() -> str:
    """Return configured OSM source backend."""
    source = os.environ.get("BLACKMARBLE_OSM_SOURCE", "overpass")
    source_normalized = str(source).strip().lower()
    return source_normalized if source_normalized else "overpass"


def _parse_highway_types_from_filter(custom_filter: str | None) -> set[str] | None:
    """Extract highway types from filter like ["highway"~"a|b|c"]."""
    if not custom_filter:
        return None

    match = re.search(r'\["highway"~"([^"]+)"\]', custom_filter)
    if not match:
        return None

    parsed = {item.strip() for item in match.group(1).split("|") if item.strip()}
    return parsed or None


def _prepare_lines_geodataframe(lines_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Normalize road line features for downstream buffering and rasterization."""
    cleaned = lines_gdf[lines_gdf.geometry.notna() & (~lines_gdf.geometry.is_empty)].copy()
    cleaned = cleaned.explode(index_parts=False)
    cleaned = cleaned[cleaned.geometry.geom_type == "LineString"].copy()

    if cleaned.crs is None:
        cleaned = cleaned.set_crs("EPSG:4326")
    elif cleaned.crs.to_string() != "EPSG:4326":
        cleaned = cleaned.to_crs("EPSG:4326")

    return cleaned.reset_index(drop=True)


def download_layercake_roads(
    north: float,
    south: float,
    east: float,
    west: float,
    road_filter: str | None,
) -> gpd.GeoDataFrame:
    """Download roads from Layercake parquet with DuckDB spatial filtering."""
    try:
        import duckdb
    except ImportError as e:
        raise ImportError(
            "Layercake source requires duckdb. Install dependencies including duckdb "
            "or set BLACKMARBLE_OSM_SOURCE=overpass."
        ) from e

    min_lon, min_lat, max_lon, max_lat = west, south, east, north
    allowed_types = _parse_highway_types_from_filter(road_filter)

    logger.info("Fetching OSM roads from Layercake source: %s", LAYERCAKE_DEFAULT_URL)

    conn = duckdb.connect()
    try:
        conn.execute("INSTALL spatial;")
        conn.execute("LOAD spatial;")

        filters = [
            f"bbox.xmax >= {min_lon}",
            f"bbox.xmin <= {max_lon}",
            f"bbox.ymax >= {min_lat}",
            f"bbox.ymin <= {max_lat}",
            "type = 'way'",
        ]

        if allowed_types:
            safe_values = ", ".join(
                f"'{value.replace("'", "''")}'" for value in sorted(allowed_types)
            )
            filters.append(f"highway IN ({safe_values})")

        query = (
            "SELECT type AS osm_type, id AS osm_id, highway, name, "
            "ST_AsWKB(geometry) AS geometry "
            f"FROM '{LAYERCAKE_DEFAULT_URL}' "
            f"WHERE {' AND '.join(filters)}"
        )

        table = conn.execute(query).fetch_arrow_table()
    finally:
        conn.close()

    if table.num_rows == 0:
        logger.warning(
            "Layercake query returned no roads for bbox %s",
            (min_lon, min_lat, max_lon, max_lat),
        )
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    dataframe = table.to_pandas()
    geometries = gpd.array.from_wkb(table["geometry"].to_numpy())
    lines_gdf = gpd.GeoDataFrame(dataframe, geometry=geometries, crs="EPSG:4326")
    lines_gdf = _prepare_lines_geodataframe(lines_gdf)
    logger.info(
        "Loaded %d road segments from Layercake",
        len(lines_gdf),
    )
    return lines_gdf


def download_roads(
    north: float,
    south: float,
    east: float,
    west: float,
    road_filter: str | None = None,
    source: str | None = None,
) -> gpd.GeoDataFrame:
    """Download OSM road network for a bounding box."""
    selected_source = source.strip().lower() if source else _get_osm_source_from_env()
    if selected_source == "layercake":
        return download_layercake_roads(north, south, east, west, road_filter)
    if selected_source != "overpass":
        logger.warning("Unknown OSM source=%r. Falling back to overpass.", selected_source)

    return download_osm_roads(north, south, east, west, road_filter)


def download_osm_roads(
    north: float,
    south: float,
    east: float,
    west: float,
    road_filter: str | None = None,
) -> gpd.GeoDataFrame:
    """Download OSM road network for a bounding box."""
    logger.info("Downloading roads for bbox: N=%s, S=%s, E=%s, W=%s", north, south, east, west)
    if road_filter:
        logger.info("Using filter: %s", road_filter)

    logger.info("Fetching OSM road network...")
    try:
        # osmnx expects bbox as (west, south, east, north)
        graph = ox.graph_from_bbox(
            bbox=(west, south, east, north),
            retain_all=True,
            truncate_by_edge=True,
            simplify=True,
            network_type="all",
            custom_filter=road_filter if road_filter else None,
        )

        edges_gdf = ox.graph_to_gdfs(graph, nodes=False)
        if edges_gdf.crs is None:
            edges_gdf = edges_gdf.set_crs("EPSG:4326")

        logger.info(
            "Downloaded %d road segments from Overpass",
            len(edges_gdf),
        )

        return edges_gdf.reset_index()
    except Exception as e:
        logger.error("Error downloading roads: %s: %s", type(e).__name__, e)
        raise


def get_osm_cache_path(
    bbox: BBox,
    network_type: str,
    custom_filter: str | None,
    cache_dir: str | Path = "data/osm_cache",
) -> Path:
    """Generate a cache file path for OSM data based on parameters."""
    # Create a unique hash for the parameters
    params_str = f"{bbox}_{network_type}_{custom_filter}"
    params_hash = hashlib.md5(params_str.encode()).hexdigest()[:12]

    # Create cache directory
    cache_dir_path = Path(cache_dir)
    cache_dir_path.mkdir(parents=True, exist_ok=True)

    # Generate filename with bbox info for readability
    min_lon, min_lat, max_lon, max_lat = bbox
    # Use .gpkg extension for GeoPackage
    filename = f"osm_{min_lon:.2f}_{min_lat:.2f}_{max_lon:.2f}_{max_lat:.2f}_{params_hash}.gpkg"

    return cache_dir_path / filename


def fetch_road_network(
    bbox: BBox,
    network_type: str = "all",
    custom_filter: str | None = None,
    use_cache: bool = True,
    cache_dir: str | Path = "data/osm_cache",
    source: str | None = None,
) -> gpd.GeoDataFrame:
    """Fetch road network from OpenStreetMap with caching support.

    Args:
        bbox: Bounding box (min_lon, min_lat, max_lon, max_lat)
        network_type: Type of road network to fetch
        custom_filter: Custom OSM filter string
        use_cache: Whether to use cached data if available
        source: OSM backend source ('overpass' or 'layercake')

    Returns:
        GeoDataFrame of road segments
    """
    # Check cache first
    if use_cache:
        cache_path = get_osm_cache_path(bbox, network_type, custom_filter, cache_dir=cache_dir)

        if cache_path.exists():
            logger.info("Loading OSM data from cache: %s", cache_path.name)
            try:
                edges_gdf = gpd.read_file(cache_path, layer="edges")
                edges_gdf = _prepare_lines_geodataframe(edges_gdf)

                logger.info("Loaded %d road segments from cache", len(edges_gdf))
                return edges_gdf
            except Exception as e:
                logger.warning("Failed to load cache: %s. Downloading fresh data...", e)

    # Download fresh data using the configured road source backend.
    logger.info("Downloading road network data")
    roads_gdf = download_roads(
        bbox[3],
        bbox[1],
        bbox[2],
        bbox[0],
        custom_filter,
        source=source,
    )

    # Save to cache
    if use_cache and len(roads_gdf) > 0:
        cache_path = get_osm_cache_path(bbox, network_type, custom_filter, cache_dir=cache_dir)
        try:
            roads_gdf.to_file(cache_path, layer="edges", driver="GPKG")
            logger.info("Saved OSM data to cache: %s", cache_path.name)
            logger.info("  Cached %d road segments", len(roads_gdf))
        except Exception as e:
            logger.warning("Failed to save cache: %s", e)
            # Continue without caching - the download succeeded

    return roads_gdf


def calculate_utm_zone(lon: float, lat: float) -> tuple[int, Literal["north", "south"]]:
    """Calculate UTM zone and hemisphere for coordinates."""
    zone = min(60, int((lon + 180) / 6) + 1)
    hemisphere: Literal["north", "south"] = "north" if lat >= 0 else "south"
    return zone, hemisphere


def buffer_roads_in_utm(
    geometry: dict[str, Any], buffer_meters: float, lon_center: float, lat_center: float
) -> dict[str, Any]:
    """Buffer a road geometry in meters using UTM projection."""
    zone, hemisphere = calculate_utm_zone(lon_center, lat_center)
    epsg_code = 32600 + zone if hemisphere == "north" else 32700 + zone

    wgs84_to_utm = pyproj.Transformer.from_crs("EPSG:4326", f"EPSG:{epsg_code}", always_xy=True)
    utm_to_wgs84 = pyproj.Transformer.from_crs(f"EPSG:{epsg_code}", "EPSG:4326", always_xy=True)

    geom = shape(geometry)
    geom_utm = transform(wgs84_to_utm.transform, geom)
    buffered_utm = geom_utm.buffer(buffer_meters)
    buffered_wgs84 = transform(utm_to_wgs84.transform, buffered_utm)

    return mapping(buffered_wgs84)


def rasterize_road_shapes_with_crs(
    shapes: list[dict[str, Any]],
    reference_transform: Transform,
    shape: tuple[int, int],
    src_crs: str | CRS,
    dst_crs: str | CRS,
    all_touched: bool = False,
) -> ArrayLike:
    """Rasterize road shapes with coordinate system transformation."""
    if isinstance(src_crs, str):
        src_crs = CRS.from_string(src_crs)
    if isinstance(dst_crs, str):
        dst_crs = CRS.from_string(dst_crs)

    should_transform = src_crs != dst_crs
    rasterize_shapes = []
    for shape_dict in shapes:
        geom = shape_dict["geometry"]
        value = shape_dict.get("value", 1)

        if should_transform:
            geom = transform_geom(src_crs, dst_crs, geom)

        rasterize_shapes.append((geom, value))

    height, width = shape
    return rasterio.features.rasterize(
        rasterize_shapes,
        out_shape=(height, width),
        transform=reference_transform,
        fill=0,
        all_touched=all_touched,
        dtype="uint8",
    )
