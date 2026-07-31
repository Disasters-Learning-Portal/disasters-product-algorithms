"""Type aliases for the Black Marble project."""

from typing import Any

import numpy as np
import numpy.typing as npt


# Generic array type for pixel data
ArrayLike = npt.NDArray[Any]

# Floating point arrays (most common in scientific computing)
FloatArray = npt.NDArray[np.floating[Any]]

# Integer arrays
IntArray = npt.NDArray[np.integer[Any]]

# Unsigned 8-bit arrays (for RGB images)
UInt8Array = npt.NDArray[np.uint8]

# Boolean arrays (for masks)
BoolArray = npt.NDArray[np.bool_]

# Bounding box type: (min_lon, min_lat, max_lon, max_lat)
BBox = tuple[float, float, float, float]

# CRS type that can be either pyproj or rasterio CRS
# This avoids type confusion between the two libraries
CRSType = Any  # Can be pyproj.CRS or rasterio.crs.CRS
