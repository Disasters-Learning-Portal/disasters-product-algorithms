"""Temporal analysis and compositing functions.

This module provides the median composite function used in the pipeline.
"""

import logging
import warnings
from typing import Literal

import numpy as np

from blackmarble.typing import ArrayLike

from .streaming import streaming_percentile_85


logger = logging.getLogger(__name__)


def create_index_temporal_composite(
    index_stack: list[ArrayLike],
    method: Literal["median", "mean", "percentile"] = "median",
    percentile: float | None = None,
    mask_nan: bool = True,
    min_valid_observations: int = 1,
) -> ArrayLike:
    """Create temporal composite from pre-calculated spectral indices.

    This expects indices (NDVI, NDWI) already calculated per date.
    This ensures mathematical correctness since median(ratio) ≠ ratio(median).

    IMPORTANT: This function assumes all input arrays share the same coordinate
    system and that their upper-left corners are aligned. Arrays of different
    sizes will be padded with NaN to the maximum size, but spatial alignment
    must be ensured by the caller through proper reprojection.

    Args:
        index_stack: List of 2D arrays containing pre-calculated indices,
                    all must be in the same CRS with aligned upper-left origins
        method: Compositing method - "median", "mean", or "percentile"
        percentile: Percentile value if method="percentile" (0-100)
        mask_nan: Whether to mask output where insufficient observations
        min_valid_observations: Minimum valid observations required

    Returns:
        Composite array with same shape as inputs

    Note:
        For NDUI calculations, using 85th percentile NDVI better captures
        vegetation's light-dimming potential than median values.
    """
    if not index_stack:
        raise ValueError("No index data provided for compositing")

    # Validate percentile parameter
    if method == "percentile" and percentile is None:
        raise ValueError("percentile parameter must be provided when method='percentile'")

    # Use streaming algorithm for 85th percentile
    if method == "percentile" and percentile is not None and 84.5 <= percentile <= 85.5:
        composite, valid_count = streaming_percentile_85(index_stack, min_valid_observations)
        return composite

    shapes = [arr.shape for arr in index_stack]
    max_height = max(s[0] for s in shapes)
    max_width = max(s[1] for s in shapes)

    logger.info(
        f"Index stack shapes: min=({min(s[0] for s in shapes)}, {min(s[1] for s in shapes)}), "
        f"max=({max_height}, {max_width})"
    )
    padded_stack = []
    for i, arr in enumerate(index_stack):
        if arr.shape[0] < max_height or arr.shape[1] < max_width:
            padded = np.full((max_height, max_width), np.nan, dtype=np.float32)
            padded[: arr.shape[0], : arr.shape[1]] = arr.astype(np.float32)
            logger.debug(f"  Padded array {i}: {arr.shape} -> {padded.shape}")
        else:
            padded = arr.astype(np.float32)
        padded_stack.append(padded)
    stacked = np.stack(padded_stack, axis=0)
    valid_count = np.sum(~np.isnan(stacked), axis=0)

    # Compute composite along time axis
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", r"All-NaN slice encountered")

        if method == "median":
            composite = np.nanmedian(stacked, axis=0)
        elif method == "mean":
            composite = np.nanmean(stacked, axis=0)
        elif method == "percentile":
            if percentile is None:
                raise ValueError("percentile parameter must be provided when method='percentile'")
            composite = np.nanpercentile(stacked, percentile, axis=0)
        else:
            raise ValueError(f"Unknown method: {method}. Use 'median', 'mean', or 'percentile'.")

    # Mask pixels with insufficient observations
    if mask_nan and min_valid_observations > 1:
        composite[valid_count < min_valid_observations] = np.nan

    return composite
