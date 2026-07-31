"""Landsat QA_PIXEL bit manipulation utilities.

This module provides functions for interpreting Landsat Collection 2 QA_PIXEL
bit-packed quality assessment data. The bit positions follow the official USGS
Collection 2 schema.
"""

from enum import Enum
from typing import Literal

import numpy as np
from scipy.ndimage import binary_dilation


class QALevel(Enum):
    """Quality assurance masking levels."""

    PERMISSIVE = "permissive"
    MODERATE = "moderate"
    CONSERVATIVE = "conservative"


# Landsat Collection 2 QA_PIXEL bit positions (LSB = bit 0)
# Based on USGS Collection 2 Product Guide Table 13
FILL_BIT = 0  # No-data / fill
DILATED_BIT = 1  # Dilated cloud (buffer around detected clouds)
CIRRUS_BIT = 2  # Thin, high cirrus
CLOUD_BIT = 3  # Opaque cloud
SHADOW_BIT = 4  # Cloud shadow
SNOW_BIT = 5  # Snow / ice
CLEAR_BIT = 6  # Pixel deemed clear by the algorithm
WATER_BITS = (7, 8)  # Two-bit water/land code: 00 land, 01 water, 10 maybe
CLOUD_CONF_BITS = (9, 10)  # Cloud confidence: 0 none, 1 low, 2 mid, 3 high
SHADOW_CONF_BITS = (11, 12)  # Shadow confidence: 0 none, 1 low, 2 mid, 3 high
SNOW_CONF_BITS = (13, 14)  # Snow confidence: 0 none, 1 low, 2 mid, 3 high
CIRRUS_CONF_BIT = 15  # Cirrus confidence: 0 low, 1 high

# Known clear values for conservative mode
CLEAR_VALUES = {21824, 21888}


def create_landsat_cloud_mask(
    qa_pixel: np.ndarray,
    strategy: Literal["conservative", "moderate", "permissive"] = "moderate",
    dilate_pixels: int = 0,
) -> np.ndarray:
    """Create cloud mask from Landsat QA_PIXEL band using bit operations.

    Args:
        qa_pixel: QA_PIXEL band from Landsat Collection 2
        strategy: Masking strategy:
            - "conservative": Only accept known clear values
            - "moderate": Exclude fill, cloud, shadow (default)
            - "permissive": Only exclude high-confidence clouds
        dilate_pixels: Number of pixels to dilate cloud mask

    Returns:
        Boolean mask where True = clear (valid), False = cloudy/bad (masked)
    """
    # Handle empty arrays
    if qa_pixel.size == 0:
        return np.array([], dtype=np.bool_)

    # Convert string strategy to enum
    level = QALevel(strategy)

    # Call the main qa_mask function
    mask = qa_mask(qa_pixel, level)

    # Apply dilation if requested
    if dilate_pixels > 0:
        structure = np.ones((2 * dilate_pixels + 1, 2 * dilate_pixels + 1), dtype=np.int32)
        # Invert for dilation (dilate the bad pixels), then invert back
        mask = ~binary_dilation(~mask, structure=structure)

    return mask


def qa_mask(qa_array: np.ndarray, level: QALevel = QALevel.MODERATE) -> np.ndarray:
    """Create QA mask based on quality level using bit manipulation.

    Quality levels:
    - CONSERVATIVE: Only accepts known clear values or pixels with CLEAR_BIT set
    - MODERATE: Excludes fill, dilated cloud, cloud, shadow, cirrus, snow
    - PERMISSIVE: Excludes only fill and high-confidence issues

    Args:
        qa_array: QA_PIXEL array
        level: Quality assurance level

    Returns:
        Boolean mask where True = clear/valid pixels
    """

    if level == QALevel.CONSERVATIVE:
        # Option 1: Trust known clear values
        is_known_clear = np.isin(qa_array, list(CLEAR_VALUES))

        # Option 2: Trust the CLEAR_BIT
        has_clear_bit = (qa_array & (1 << CLEAR_BIT)) != 0
        no_fill = (qa_array & (1 << FILL_BIT)) == 0

        # Accept either known values OR (clear bit set AND not fill)
        return is_known_clear | (has_clear_bit & no_fill)

    elif level == QALevel.MODERATE:
        # Create a bitmask of all problematic bits
        bad_bits = (
            (1 << FILL_BIT)
            | (1 << DILATED_BIT)
            | (1 << CIRRUS_BIT)
            | (1 << CLOUD_BIT)
            | (1 << SHADOW_BIT)
            | (1 << SNOW_BIT)
        )

        # Pixel is clear if none of the bad bits are set
        no_bad_bits = (qa_array & bad_bits) == 0

        # Also exclude definite water (01) for land analysis
        water_code = (qa_array >> WATER_BITS[0]) & 0b11
        not_water = water_code != 0b01

        return no_bad_bits & not_water

    elif level == QALevel.PERMISSIVE:
        # Always exclude fill
        no_fill = (qa_array & (1 << FILL_BIT)) == 0

        # Check individual quality issues with their confidence
        has_cloud = (qa_array & (1 << CLOUD_BIT)) != 0
        cloud_conf = (qa_array >> CLOUD_CONF_BITS[0]) & 0b11
        high_conf_cloud = has_cloud & (cloud_conf >= 2)  # mid or high

        has_shadow = (qa_array & (1 << SHADOW_BIT)) != 0
        shadow_conf = (qa_array >> SHADOW_CONF_BITS[0]) & 0b11
        high_conf_shadow = has_shadow & (shadow_conf >= 2)

        has_cirrus = (qa_array & (1 << CIRRUS_BIT)) != 0
        cirrus_conf = (qa_array >> CIRRUS_CONF_BIT) & 0b1
        high_conf_cirrus = has_cirrus & (cirrus_conf == 1)

        # Exclude only high-confidence issues
        return no_fill & ~high_conf_cloud & ~high_conf_shadow & ~high_conf_cirrus


def composite_masked(
    image_stack: np.ndarray, qa_stack: np.ndarray, level: QALevel = QALevel.MODERATE
) -> np.ndarray:
    """Mask each image in stack, then take median composite.

    Args:
        image_stack: 3D array (time, bands, height, width) or 4D array
        qa_stack: 3D array (time, height, width) of QA_PIXEL data
        level: Quality assurance level for masking

    Returns:
        Median composite with shape (bands, height, width)
    """
    # Handle different input shapes
    if image_stack.ndim == 3:
        # Single band time series: (time, height, width)
        n_times, _, _ = image_stack.shape
        n_bands = 1
        image_stack = image_stack[:, np.newaxis, :, :]  # Add band dimension
    else:
        # Multi-band: (time, bands, height, width)
        n_times, n_bands, _, _ = image_stack.shape

    # Create masked copy
    masked_stack = image_stack.copy().astype(np.float32)

    # Apply QA mask to each time step
    for t in range(n_times):
        mask = qa_mask(qa_stack[t], level)
        for b in range(n_bands):
            masked_stack[t, b][~mask] = np.nan

    # Take median across time dimension
    composite = np.nanmedian(masked_stack, axis=0)

    # Return single band if that's what we started with
    if n_bands == 1:
        return composite[0]
    return composite


def decode_qa_pixel(qa_pixel: np.ndarray) -> dict[str, np.ndarray]:
    """Decode all bits from QA_PIXEL into separate arrays for inspection.

    Note: This function extracts all bits for diagnostic purposes.
    For performance-critical masking, use qa_mask() which only
    extracts the bits it needs.

    Args:
        qa_pixel: QA_PIXEL band array

    Returns:
        Dictionary with decoded bit fields
    """
    # Handle empty arrays
    if qa_pixel.size == 0:
        empty_bool = np.array([], dtype=bool)
        empty_int = np.array([], dtype=np.int32)
        return {
            "fill": empty_bool,
            "dilated_cloud": empty_bool,
            "cirrus": empty_bool,
            "cloud": empty_bool,
            "cloud_shadow": empty_bool,
            "snow": empty_bool,
            "clear": empty_bool,
            "water": empty_bool,
            "water_class": empty_int,
            "cloud_confidence": empty_int,
            "shadow_confidence": empty_int,
            "snow_confidence": empty_int,
            "cirrus_confidence": empty_int,
        }

    # Single bit fields
    decoded = {
        "fill": (qa_pixel & (1 << FILL_BIT)) != 0,
        "dilated_cloud": (qa_pixel & (1 << DILATED_BIT)) != 0,
        "cirrus": (qa_pixel & (1 << CIRRUS_BIT)) != 0,
        "cloud": (qa_pixel & (1 << CLOUD_BIT)) != 0,
        "cloud_shadow": (qa_pixel & (1 << SHADOW_BIT)) != 0,
        "snow": (qa_pixel & (1 << SNOW_BIT)) != 0,
        "clear": (qa_pixel & (1 << CLEAR_BIT)) != 0,
    }

    # Two-bit water class: 0 land, 1 water, 2 maybe water, 3 reserved
    water_class = (qa_pixel >> WATER_BITS[0]) & 0b11
    decoded["water"] = water_class == 0b01
    decoded["water_class"] = water_class

    # Confidence fields (2-bit each, except cirrus which is 1-bit)
    decoded["cloud_confidence"] = (qa_pixel >> CLOUD_CONF_BITS[0]) & 0b11
    decoded["shadow_confidence"] = (qa_pixel >> SHADOW_CONF_BITS[0]) & 0b11
    decoded["snow_confidence"] = (qa_pixel >> SNOW_CONF_BITS[0]) & 0b11
    decoded["cirrus_confidence"] = (qa_pixel >> CIRRUS_CONF_BIT) & 0b1

    return decoded


def print_qa_pixel_summary(qa_pixel: np.ndarray) -> None:
    """Pretty-print a summary of QA_PIXEL content for quick sanity checks.

    Args:
        qa_pixel: QA_PIXEL band array
    """
    total = int(qa_pixel.size)
    print(f"QA_PIXEL summary (total pixels = {total:,}):")

    if total == 0:
        print("  — empty array —")
        return

    unique = np.unique(qa_pixel)
    print(f"  Unique codes: {len(unique):,}  (range {unique.min()}–{unique.max()})")

    decoded = decode_qa_pixel(qa_pixel)

    # Print single-bit fields
    print("\nBit flags:")
    for key in [
        "fill",
        "dilated_cloud",
        "cirrus",
        "cloud",
        "cloud_shadow",
        "snow",
        "clear",
    ]:
        if key in decoded:
            pct = decoded[key].mean() * 100
            print(f"  {key:<18} {pct:5.1f}% true")

    # Print water class
    print("\nWater classification:")
    water_class = decoded.get("water_class")
    if water_class is not None:
        vals, counts = np.unique(water_class, return_counts=True)
        water_names = {0: "land", 1: "water", 2: "maybe water", 3: "reserved"}
        for v, c in zip(vals, counts, strict=True):
            name = water_names.get(v, f"unknown({v})")
            print(f"  {name:<18} {c / total * 100:5.1f}%")

    # Print confidence fields
    print("\nConfidence levels:")
    conf_names = ["none", "low", "medium", "high"]
    for conf_field in ["cloud_confidence", "shadow_confidence", "snow_confidence"]:
        if conf_field in decoded:
            vals, counts = np.unique(decoded[conf_field], return_counts=True)
            parts = []
            for v, c in zip(vals, counts, strict=True):
                name = conf_names[v] if v < len(conf_names) else f"{v}"
                parts.append(f"{name}: {c / total * 100:.1f}%")
            print(f"  {conf_field:<18} {', '.join(parts)}")

    # Cirrus confidence (1-bit)
    if "cirrus_confidence" in decoded:
        vals, counts = np.unique(decoded["cirrus_confidence"], return_counts=True)
        parts = []
        for v, c in zip(vals, counts, strict=True):
            name = "low" if v == 0 else "high"
            parts.append(f"{name}: {c / total * 100:.1f}%")
        print(f"  cirrus_confidence  {', '.join(parts)}")
