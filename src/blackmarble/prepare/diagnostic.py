"""Diagnostic output functions for debugging pipeline issues."""

import os
from typing import Any

import numpy as np
import rasterio
from rasterio.transform import Affine

from blackmarble.typing import ArrayLike

from ..logging_utils import get_logger


logger = get_logger(__name__)


def save_diagnostic(
    data: ArrayLike,
    profile: dict[str, Any] | Any,
    output_path: str,
    description: str = "",
    crs: Any = None,
) -> None:
    """Save diagnostic output for debugging.

    Args:
        data: Array data to save
        profile: Rasterio profile dict or transform
        output_path: Output file path
        description: Description of what this data represents
        crs: Optional CRS to use (if profile is just a transform)
    """

    # Ensure data is a numpy array
    data = np.asarray(data)

    # Create directory if needed
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Handle case where profile is just a transform
    if hasattr(profile, "a"):  # It's a transform
        transform = profile
        profile = {
            "driver": "GTiff",
            "height": data.shape[0],
            "width": data.shape[1],
            "count": 1,
            "dtype": data.dtype,
            "transform": transform,
            "compress": "lzw",
        }
        if crs is not None:
            profile["crs"] = crs
    else:
        # Handle case where profile is None
        if profile is None:
            profile = {
                "driver": "GTiff",
                "height": data.shape[0],
                "width": data.shape[1],
                "count": 1,
                "dtype": data.dtype,
                "compress": "lzw",
            }
            if crs is not None:
                profile["crs"] = crs
        else:
            # Update profile for single band
            profile = profile.copy()
            profile.update({"driver": "GTiff", "count": 1, "dtype": data.dtype, "compress": "lzw"})

        # Ensure height and width are set
        if "height" not in profile or profile["height"] is None:
            profile["height"] = data.shape[0]
        if "width" not in profile or profile["width"] is None:
            profile["width"] = data.shape[1]

        # Ensure transform is set (use identity if missing)
        if "transform" not in profile or profile["transform"] is None:
            profile["transform"] = Affine.identity()

    # Validate required fields
    required_fields = ["driver", "height", "width", "count", "dtype", "transform"]
    missing_fields = [f for f in required_fields if f not in profile or profile[f] is None]
    if missing_fields:
        raise ValueError(f"Missing required fields in profile: {missing_fields}")

    # Write file
    try:
        with rasterio.open(output_path, mode="w", **profile) as dst:
            dst.write(data, 1)
            if description:
                dst.set_band_description(1, description)
    except Exception as e:
        logger.error("Error saving diagnostic %s: %s", output_path, e)
        logger.error("Profile: %s", profile)
        raise

    logger.info("Saved diagnostic: %s", output_path)
    logger.info("  Description: %s", description)
    logger.info("  Shape: %s", data.shape)
    logger.info("  Range: %.2f to %.2f", data.min(), data.max())
    logger.info("  Non-zero pixels: %d", (data > 0).sum())
