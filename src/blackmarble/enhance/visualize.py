"""Visualization functions."""

import matplotlib.pyplot as plt
import numpy as np

from ..typing import FloatArray, UInt8Array


def apply_inferno_colormap(
    data: FloatArray, scale_range: tuple[float, float] = (0.0, 1.0)
) -> UInt8Array:
    """Apply inferno colormap to single-band data."""
    # Normalize to scale range
    vmin, vmax = scale_range
    normalized = np.clip((data - vmin) / (vmax - vmin), 0, 1)

    # Apply inferno colormap
    cmap = plt.get_cmap("inferno")  # type: ignore
    colored = cmap(normalized)  # type: ignore[no-untyped-call]

    # Return RGB channels (drop alpha)
    # matplotlib returns (height, width, 4) with RGBA
    # We need (3, height, width) for rasterio RGB
    rgb = colored[..., :3]  # Drop alpha channel, shape is (height, width, 3)

    # Convert to uint8 (0-255) for proper RGB imagery
    rgb_uint8 = (rgb * 255).astype(np.uint8)

    # Transpose to (bands, height, width) for rasterio
    return np.transpose(rgb_uint8, (2, 0, 1))


def create_false_color_composite(
    red_channel: FloatArray,
    green_channel: FloatArray,
    blue_channel: FloatArray,
    contrast_enhance: bool = True,
    gamma: float | None = None,
) -> FloatArray:
    """Create false color composite from three bands.

    Args:
        red_channel: Data for red channel
        green_channel: Data for green channel
        blue_channel: Data for blue channel
        contrast_enhance: Whether to apply contrast enhancement
        gamma: Optional gamma correction value

    Returns:
        RGB composite array
    """
    # Stack channels
    composite = np.stack([red_channel, green_channel, blue_channel], axis=0)

    if contrast_enhance:
        # Simple percentile stretch per band
        for i in range(3):
            band = composite[i]
            valid_data = band[~np.isnan(band)]
            if len(valid_data) > 0:
                percentiles = np.percentile(valid_data, [2, 98])
                vmin_float: float = float(percentiles[0])  # type: ignore[arg-type]
                vmax_float: float = float(percentiles[1])  # type: ignore[arg-type]
                if vmax_float > vmin_float:
                    composite[i] = np.clip((band - vmin_float) / (vmax_float - vmin_float), 0, 1)

    # Apply gamma correction if specified
    if gamma is not None and gamma != 1.0:
        composite = np.power(composite, 1.0 / gamma)

    return composite
