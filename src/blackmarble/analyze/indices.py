"""Spectral indices and scientific calculations.

This module provides functions for calculating various spectral indices
and derived products used in the Black Marble algorithm.
"""

import logging

import numpy as np

from blackmarble.typing import ArrayLike


logger = logging.getLogger(__name__)

# Constants
LANDSAT_EPSILON = 0.02  # 2% reflectance threshold for Landsat C2 SR data

# Use adaptive denominator floor to handle near-zero denominators
# This prevents division instability while preserving valid edge pixels
# The floor combines absolute noise floor with relative signal threshold
INDEX_EPSILON = 0.05  # 5% of signal magnitude


def _calculate_normalized_difference(
    band_a: ArrayLike,
    band_b: ArrayLike,
    epsilon: float = LANDSAT_EPSILON,
    mask_low_denom: bool = False,
) -> ArrayLike:
    """Calculate normalized-difference index: (A - B) / (A + B)."""
    numerator = band_a - band_b
    denominator = band_a + band_b

    if mask_low_denom:
        # Preserve NaN over low-denominator pixels and avoid eager invalid divisions.
        valid_mask = np.abs(denominator) >= epsilon
        index = np.full_like(denominator, np.nan, dtype=denominator.dtype)
        np.divide(numerator, denominator, out=index, where=valid_mask)
    else:
        # Adaptive denominator floor to reduce instability around near-zero sums.
        signal_magnitude = (np.abs(band_a) + np.abs(band_b)) / 2
        adaptive_floor = np.maximum(epsilon, INDEX_EPSILON * signal_magnitude)
        safe_denominator = np.where(
            np.abs(denominator) < adaptive_floor,
            np.copysign(adaptive_floor, denominator),
            denominator,
        )
        index = numerator / safe_denominator

    # Keep output inside the theoretical normalized-difference bounds.
    if mask_low_denom:
        mask = ~np.isnan(index)
        index[mask] = np.clip(index[mask], -1.0, 1.0)
    else:
        index = np.clip(index, -1.0, 1.0)

    return index


def calculate_ndvi(
    nir: ArrayLike, red: ArrayLike, epsilon: float = LANDSAT_EPSILON, mask_low_denom: bool = False
) -> ArrayLike:
    """Calculate Normalized Difference Vegetation Index (NDVI).

    The NDVI is a fundamental vegetation index that measures the health and density
    of vegetation using the difference between near-infrared and red reflectance.
    Healthy vegetation strongly reflects NIR light while absorbing red light,
    resulting in high NDVI values.

    Formula: NDVI = (NIR - Red) / (NIR + Red)

    Note: Negative reflectance values are masked as NaN to handle Landsat offset artifacts.
    A minimum denominator threshold is applied to prevent extreme values.

    NDVI values interpretation:
    - [0, 0.2]: Sparse vegetation, bare soil
    - [0.2, 0.4]: Shrubs and grasslands
    - [0.4, 0.8]: Dense vegetation, forests
    - [0.8, 1.0]: Very dense vegetation

    Args:
        nir: Near-infrared band reflectance (Band 5 for Landsat 8/9).
            Expected range: [0, 1] for surface reflectance.
        red: Red band reflectance (Band 4 for Landsat 8/9).
            Expected range: [0, 1] for surface reflectance.
        epsilon: Minimum denominator threshold to prevent extreme NDVI values.
            Default is 0.02 (2% reflectance), which prevents artifacts from
            snow/cloud edges and near-zero denominators.
        mask_low_denom: If True, return NaN for pixels with denominator < epsilon
            instead of using a safe denominator. This preserves mathematical
            correctness for temporal compositing.

    Returns:
        NDVI array with values mathematically bounded to [-1, 1].

    Raises:
        ValueError: If input arrays have incompatible shapes.

    Example:
        >>> # Calculate NDVI from Landsat bands
        >>> ndvi = calculate_ndvi(landsat_band5, landsat_band4)
        >>> # Mask vegetation areas
        >>> vegetation_mask = ndvi > 0.3

    Note:
        This function preserves NaN values in the input arrays, making it
        suitable for masked data. Output is NOT clamped to [-1, 1] to
        preserve extreme values that may indicate processing issues.

        For NDUI calculations in the Black Marble pipeline, negative NDVI
        values (water/bare soil/artificial surfaces) should be clamped to 0
        as they should not contribute to the vegetation signal.
    """

    # Input validation
    if nir.shape != red.shape:
        raise ValueError(f"NIR and Red arrays must have same shape: {nir.shape} vs {red.shape}")

    return _calculate_normalized_difference(
        band_a=nir,
        band_b=red,
        epsilon=epsilon,
        mask_low_denom=mask_low_denom,
    )


def calculate_ndwi(
    green: ArrayLike, nir: ArrayLike, epsilon: float = LANDSAT_EPSILON, mask_low_denom: bool = False
) -> ArrayLike:
    """Calculate Normalized Difference Water Index (NDWI).

    The NDWI is designed to detect water features and measure water content
    in vegetation. Water strongly absorbs near-infrared radiation while
    reflecting green light, resulting in positive NDWI values for water bodies.

    Formula: NDWI = (Green - NIR) / (Green + NIR)

    Note: Negative reflectance values are masked as NaN to handle Landsat offset artifacts.
    A minimum denominator threshold is applied to prevent extreme values.

    NDWI values interpretation:
    - [0.3, 1.0]: Open water, wet surfaces
    - [0.0, 0.3]: Moist soil, wet vegetation
    - [-0.2, 0.0]: Dry vegetation, moderate moisture
    - [-1.0, -0.2]: Dry soil, built-up areas

    This index is crucial for the Black Marble algorithm as it helps
    distinguish water bodies from land surfaces, enabling proper masking
    and preventing false urban light detection over water.

    Args:
        green: Green band reflectance (Band 3 for Landsat 8/9).
            Expected range: [0, 1] for surface reflectance.
        nir: Near-infrared band reflectance (Band 5 for Landsat 8/9).
            Expected range: [0, 1] for surface reflectance.
        epsilon: Minimum denominator threshold to prevent extreme NDWI values.
            Default is 0.02 (2% reflectance), which prevents artifacts from
            sunglint/cloud edges and near-zero denominators.
        mask_low_denom: If True, return NaN for pixels with denominator < epsilon
            instead of using a safe denominator. This preserves mathematical
            correctness for temporal compositing.

    Returns:
        NDWI array with values mathematically bounded to [-1, 1].

    Raises:
        ValueError: If input arrays have incompatible shapes.

    Example:
        >>> # Calculate NDWI for water detection
        >>> ndwi = calculate_ndwi(landsat_band3, landsat_band5)
        >>> # Create water mask
        >>> water_mask = ndwi > 0.0
        >>> # Find highly confident water pixels
        >>> water_confident = ndwi > 0.3

    Note:
        NDWI is used extensively in the prepare.quality module for
        water masking operations. Positive values indicate water presence.

        For the Black Marble pipeline, negative NDWI values (dry soil,
        built-up areas) should be clamped to 0 to focus on water features
        and prevent negative values from affecting subsequent analysis.
    """

    # Input validation
    if green.shape != nir.shape:
        raise ValueError(f"Green and NIR arrays must have same shape: {green.shape} vs {nir.shape}")

    return _calculate_normalized_difference(
        band_a=green,
        band_b=nir,
        epsilon=epsilon,
        mask_low_denom=mask_low_denom,
    )


def calculate_ndui(
    ntl: ArrayLike,
    ndvi: ArrayLike,
    ndwi: ArrayLike | None = None,
    ntl_floor: float = 0.1,
    ntl_ceiling: float = 10.0,
    ndui_floor: float = 0.02,
    epsilon: float = 0.00001,
) -> ArrayLike:
    """Calculate Normalized Difference Urban Index (NDUI).

    The NDUI is the core index of the Black Marble algorithm, designed to
    identify and quantify urban areas by combining nighttime lights with
    vegetation indices. It leverages the fact that urban areas have high
    nighttime radiance and low vegetation density.

    Formula: NDUI = (NTL_norm - NDVI) / (NTL_norm + NDVI + epsilon)
    Where: NTL_norm = clamp(NTL / ceiling, 0, 1) after floor application

    NDUI values interpretation:
    - [0.7, 1.0]: Dense urban areas, city centers
    - [0.4, 0.7]: Suburban areas, developed land
    - [0.2, 0.4]: Sparse development, rural towns
    - [0.0, 0.2]: Non-urban (vegetation, water, bare land)

    The index is normalized to [0, 1] and incorporates thresholding to
    handle the wide dynamic range of nighttime lights data.

    Args:
        ntl: Nighttime light radiance values (nW/cm²/sr units).
        ndvi: NDVI values from calculate_ndvi(), range [-1, 1].
            Used to suppress vegetation areas.
        ndwi: Optional NDWI values for water masking.
            If provided, water pixels (NDWI > 0) are set to NDUI = 0.
        ntl_floor: Minimum NTL threshold below which NDUI = 0.
            Removes sensor noise and very dim light sources.
        ntl_ceiling: Maximum NTL value for normalization.
            Values above this are clamped to prevent saturation effects.
        ndui_floor: Minimum NDUI threshold below which values are set to 0.
            Removes spurious low-confidence urban detections.
        epsilon: Small value to prevent division by zero when NDVI = -1.
            Default 1e-5 handles edge cases in water/shadow areas.

    Returns:
        NDUI array with values in [0, 1] where:
        - 0 indicates non-urban areas
        - Values approaching 1 indicate dense urban development

    Raises:
        ValueError: If input arrays have incompatible shapes.

    Example:
        >>> # Calculate NDUI for urban mapping
        >>> ndui = calculate_ndui(
        >>>     ntl=viirs_radiance,
        >>>     ndvi=ndvi_values,
        >>>     ndwi=ndwi_values,
        >>>     ntl_floor=0.1,
        >>>     ntl_ceiling=10.0
        >>> )
        >>> # Create urban mask
        >>> urban_mask = ndui > 0.3

    Note:
        This is the signature index of the Black Marble algorithm.
        Parameter tuning significantly affects urban detection accuracy.
    """

    # Input validation
    if ntl.shape != ndvi.shape:
        raise ValueError(f"NTL and NDVI arrays must have same shape: {ntl.shape} vs {ndvi.shape}")

    if ndwi is not None and ndwi.shape != ntl.shape:
        raise ValueError(f"NDWI shape {ndwi.shape} must match NTL shape {ntl.shape}")

    if ntl_ceiling <= 0:
        raise ValueError(f"ntl_ceiling must be > 0, got {ntl_ceiling}")

    # Step 1: Scale NTL (no longer needed in BM v2)
    logger.debug(f"NDUI DEBUG: ntl_ceiling = {ntl_ceiling}, ntl_floor = {ntl_floor}")

    # Step 2: Apply floor/ceiling and normalize by ceiling (like bm.py)
    # First clip to ceiling
    ntl_clipped = np.where(ntl > ntl_ceiling, ntl_ceiling, ntl)
    # Then apply floor
    ntl_clipped = np.where(ntl_clipped < ntl_floor, 0.0, ntl_clipped)

    # DEBUG: Check clipping effect
    num_clipped = np.sum(ntl > ntl_ceiling)
    percent_clipped = num_clipped / ntl.size * 100
    logger.debug(f"NDUI DEBUG: Pixels clipped at ceiling: {num_clipped} ({percent_clipped:.1f}%)")

    # Normalize by dividing by ceiling (not range)
    # This ceiling-only normalization keeps the 0-1 scale invariant when ceiling
    # is tuned year-to-year, as physical zero is unambiguous unlike the floor
    ntl_normalized = ntl_clipped / ntl_ceiling

    # Step 2: Calculate NDUI using original Black Marble formula
    # Original formula: (ntl - ndvi) / (ntl + ndvi + epsilon)
    numerator = ntl_normalized - ndvi
    denominator = ntl_normalized + ndvi + epsilon
    valid_mask = np.isfinite(numerator) & np.isfinite(denominator) & (denominator != 0)
    ndui = np.full_like(denominator, np.nan, dtype=denominator.dtype)
    np.divide(numerator, denominator, out=ndui, where=valid_mask)

    # Step 3: Clamp to [-1, 1]
    ndui = np.where(ndui > 1.0, 1.0, ndui)
    ndui = np.where(ndui < -1.0, -1.0, ndui)

    # Step 4: Apply water masking if NDWI is provided
    if ndwi is not None:
        # Water pixels (NDWI >= 0) are set to -1 (like bm.py)
        water_mask = ndwi >= 0.0
        ndui = np.where(water_mask, -1.0, ndui)

    # Step 5: Transform to [0, 1] range using bm.py formula
    # Transform: abs((ndui + 1.0)) / 2.0
    ndui = np.abs(ndui + 1.0) / 2.0

    # Step 6: Apply floor threshold
    ndui = np.where(ndui < ndui_floor, 0.0, ndui)

    return ndui
