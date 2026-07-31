"""Validation - ONLY WHAT'S USED."""

from typing import Any

import numpy as np

from blackmarble.typing import ArrayLike


def validate_for_cog(
    data: ArrayLike,
    transform: Any,
    crs: Any | None = None,
    check_tiling: bool = True,
    check_compression: bool = True,
    check_overviews: bool = True,
) -> dict[str, Any]:
    """Validate data is suitable for COG export.

    Args:
        data: Raster data array
        transform: Affine transform
        crs: Coordinate reference system
        check_tiling: Check if tiling is appropriate
        check_compression: Check compression settings
        check_overviews: Check if overviews are needed

    Returns:
        Validation results dictionary
    """
    errors: list[Any] = []
    warnings: list[Any] = []
    info: list[Any] = []

    # Check data shape
    if data.ndim not in (2, 3):
        errors.append(f"Data must be 2D or 3D, got {data.ndim}D")

    # Check for NaN/inf
    if np.any(np.isnan(data)):
        warnings.append("Data contains NaN values")
    if np.any(np.isinf(data)):
        errors.append("Data contains infinite values")

    # Check transform
    if transform is None:
        errors.append("Transform is required for georeferencing")

    # COG-specific checks
    if check_tiling:
        # Check if image is large enough to benefit from tiling
        shape = data.shape[-2:] if data.ndim == 3 else data.shape
        if shape[0] > 512 or shape[1] > 512:
            info.append("Image is large enough to benefit from tiling")

    if check_compression:
        info.append("Compression recommended for COG")

    if check_overviews:
        # Check if overviews would be beneficial
        shape = data.shape[-2:] if data.ndim == 3 else data.shape
        if shape[0] > 1024 or shape[1] > 1024:
            info.append("Overviews recommended for large image")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "info": info,
    }
