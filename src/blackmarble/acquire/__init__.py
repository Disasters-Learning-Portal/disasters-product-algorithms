"""Data acquisition module for Black Marble pipeline.

This module handles fetching data from various sources:
- Landsat imagery from AWS
- VIIRS nighttime lights from NASA LAADS
- OpenStreetMap road networks
"""

from .osm import fetch_road_network
from .osm_fractional import buffer_and_rasterize_roads_fractional


__all__ = [
    "fetch_road_network",
    "buffer_and_rasterize_roads_fractional",
]
