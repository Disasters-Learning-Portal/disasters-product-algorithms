"""Multi-scale urban field computation from road networks.

This module implements anisotropic urban density fields that capture
the spatial influence of road infrastructure at multiple scales.
"""

from typing import Literal

import numpy as np
from scipy.ndimage import gaussian_filter

from blackmarble.typing import ArrayLike

from ..logging_utils import get_logger


logger = get_logger(__name__)


def compute_urban_fields(
    road_fraction: ArrayLike,
    sigmas: list[float] | None = None,
    weights: list[float] | None = None,
) -> ArrayLike:
    """Compute anisotropic urban density fields.

    Creates a multi-scale representation of urban density by combining
    the direct road coverage with Gaussian-blurred versions at different
    scales. This captures both immediate road presence and the broader
    urban context.

    Args:
        road_fraction: Fractional road coverage (0.0-1.0) per pixel
        sigmas: Gaussian filter scales in pixels. Each sigma creates
            a density field at that scale. Default [2.0, 5.0, 10.0]
            corresponds to ~60m, ~150m, ~300m for 30m pixels.
        weights: Weights for combining [direct, scale1, scale2, ...].
            Must sum to 1.0 and have length = len(sigmas) + 1.

    Returns:
        Urban field combining multiple scales, values in [0.0, 1.0]

    Examples:
        >>> # Simple case with single scale
        >>> roads = np.array([[0, 1, 0], [0, 1, 0], [0, 1, 0]])
        >>> field = compute_urban_fields(roads, sigmas=[1.0], weights=[0.5, 0.5])
        >>> # Center column (roads) will have highest values
        >>> assert np.all(field[:, 1] > field[:, 0])
    """
    if sigmas is None:
        sigmas = [2.0, 5.0, 10.0]
    if weights is None:
        weights = [0.2, 0.3, 0.3, 0.2]
    # Validate inputs
    assert len(weights) == len(sigmas) + 1, (
        f"Need {len(sigmas) + 1} weights for {len(sigmas)} sigmas, got {len(weights)}"
    )
    assert abs(sum(weights) - 1.0) < 1e-6, f"Weights must sum to 1.0, got {sum(weights)}"

    # Direct coverage (unblurred)
    fields = [road_fraction.astype(np.float32)]

    # Multi-scale densities
    for sigma in sigmas:
        # Use reflect mode to handle edges properly
        density = gaussian_filter(road_fraction.astype(np.float32), sigma=sigma, mode="reflect")
        fields.append(density)

    # Weighted combination
    urban_field = np.zeros_like(road_fraction, dtype=np.float32)
    for field, weight in zip(fields, weights, strict=True):
        urban_field += weight * field

    # Ensure output is in valid range
    urban_field = np.clip(urban_field, 0.0, 1.0)

    return urban_field


def enhance_ntl_with_urban_field(
    ntl: ArrayLike,
    urban_field: ArrayLike,
    enhancement_mode: Literal["multiplicative"] = "multiplicative",
    multiplicative_factor: float = 0.3,
) -> ArrayLike:
    """Enhance NTL based on urban density field.

    Applies multiplicative enhancement to nighttime lights based on the urban
    density field. Areas with higher road density receive proportionally more
    enhancement, up to a maximum factor.

    The enhancement preserves the original dynamic range by rescaling after
    enhancement, preventing artificial brightening of the entire image.

    Args:
        ntl: Nighttime lights data (any units, e.g., DN values or nW/cm²/sr)
        urban_field: Urban density field from compute_urban_fields (0.0-1.0)
        enhancement_mode: Must be "multiplicative" (kept for compatibility)
        multiplicative_factor: Maximum enhancement factor minus 1.0
            (default: 0.3 means up to 1.3x enhancement)

    Returns:
        Enhanced NTL array with preserved dynamic range

    Examples:
        >>> # Enhance NTL with 30% maximum boost in urban areas
        >>> enhanced = enhance_ntl_with_urban_field(
        ...     ntl=viirs_data,
        ...     urban_field=urban_field,
        ...     multiplicative_factor=0.3
        ... )
    """
    if enhancement_mode != "multiplicative":
        raise ValueError(
            f"Only 'multiplicative' enhancement mode is supported, got: {enhancement_mode}"
        )

    # Multiplicative enhancement with dynamic range preservation
    enhancement_factor = 1.0 + multiplicative_factor * urban_field
    enhanced = ntl * enhancement_factor

    # Rescale to preserve dynamic range
    # This prevents the whole image from getting brighter
    max_factor = enhancement_factor.max()
    result = enhanced / max_factor

    # Log enhancement statistics
    enhanced_pixels = np.sum(enhancement_factor > 1.001)
    if enhanced_pixels > 0:
        logger.debug(
            "Urban field enhancement: %d pixels enhanced (%.1f%%), max factor: %.3f",
            enhanced_pixels,
            enhanced_pixels / enhancement_factor.size * 100,
            max_factor,
        )

    return result
