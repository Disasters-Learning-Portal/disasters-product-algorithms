"""Contrast enhancement - ONLY WHAT'S USED."""

from typing import Literal

import numpy as np

from blackmarble.typing import ArrayLike


def enhance_contrast(
    data: ArrayLike,
    method: Literal["percentile", "minmax"] = "percentile",
    percentiles: tuple[float, float] = (2, 98),
    output_range: tuple[float, float] = (0.0, 1.0),
) -> ArrayLike:
    """Enhance image contrast."""

    out_min, out_max = output_range

    if method == "percentile":
        vmin, vmax = np.percentile(data[~np.isnan(data)], percentiles)
        enhanced = np.clip((data - vmin) / (vmax - vmin), 0, 1)
    else:  # minmax
        vmin, vmax = np.nanmin(data), np.nanmax(data)
        enhanced = (data - vmin) / (vmax - vmin) if vmax > vmin else data

    # Scale to output range
    enhanced = enhanced * (out_max - out_min) + out_min

    return enhanced
