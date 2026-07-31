"""Window rounding utilities for correct pixel placement."""

import math
from typing import Any, NamedTuple

from rasterio.windows import Window


class WindowOffsets(NamedTuple):
    """Calculated offsets for window-based array operations."""

    tile_row_start: int
    tile_col_start: int
    mosaic_row_start: int
    mosaic_col_start: int
    mosaic_row_end: int
    mosaic_col_end: int


def calculate_window_offsets(
    dst_row_off: int,
    dst_col_off: int,
    dst_height: int,
    dst_width: int,
    mosaic_shape: tuple[int, int],
) -> WindowOffsets:
    """Calculate window offsets for handling negative window positions.

    When placing a tile into a mosaic, negative destination offsets indicate
    the tile extends beyond the mosaic bounds. This function calculates the
    correct array slicing offsets for both the tile and mosaic arrays.

    Args:
        dst_row_off: Destination row offset (can be negative)
        dst_col_off: Destination column offset (can be negative)
        dst_height: Height of the destination window
        dst_width: Width of the destination window
        mosaic_shape: Shape of the mosaic array (height, width)

    Returns:
        WindowOffsets with start/end positions for both tile and mosaic arrays
    """
    # Handle negative row offsets
    if dst_row_off < 0:
        # Skip negative rows from tile_data
        tile_row_start = -dst_row_off
        mosaic_row_start = 0
    else:
        tile_row_start = 0
        mosaic_row_start = dst_row_off

    # Handle negative column offsets
    if dst_col_off < 0:
        # Skip negative cols from tile_data
        tile_col_start = -dst_col_off
        mosaic_col_start = 0
    else:
        tile_col_start = 0
        mosaic_col_start = dst_col_off

    # Calculate end positions
    mosaic_row_end = min(mosaic_row_start + (dst_height - tile_row_start), mosaic_shape[0])
    mosaic_col_end = min(mosaic_col_start + (dst_width - tile_col_start), mosaic_shape[1])

    return WindowOffsets(
        tile_row_start=tile_row_start,
        tile_col_start=tile_col_start,
        mosaic_row_start=mosaic_row_start,
        mosaic_col_start=mosaic_col_start,
        mosaic_row_end=mosaic_row_end,
        mosaic_col_end=mosaic_col_end,
    )


def round_window_offsets_correctly(window: Window, transform: Any) -> Window:
    """Round window offsets correctly for north-up vs south-up rasters.

    For north-up rasters (negative pixel height):
    - col_off: use floor (leftmost pixel)
    - row_off: use ceil (topmost pixel)

    For south-up rasters (positive pixel height):
    - col_off: use floor (leftmost pixel)
    - row_off: use floor (topmost pixel)

    Args:
        window: rasterio Window with float offsets
        transform: rasterio Affine transform

    Returns:
        Window with integer offsets
    """
    if transform.e < 0:  # North-up (most common)
        # For north-up: ceil for rows to get topmost pixel
        row_off = int(math.ceil(window.row_off))
        col_off = int(math.floor(window.col_off))
    else:  # South-up (rare)
        # For south-up: floor for both
        row_off = int(math.floor(window.row_off))
        col_off = int(math.floor(window.col_off))

    # Round lengths using ceiling to ensure full coverage
    height = int(math.ceil(window.height))
    width = int(math.ceil(window.width))

    return Window(col_off=col_off, row_off=row_off, width=width, height=height)
