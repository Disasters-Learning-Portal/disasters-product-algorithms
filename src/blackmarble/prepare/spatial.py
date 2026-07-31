"""Spatial data preparation and transformations.

This module handles reprojection, resampling, cropping, and alignment
of raster data to prepare it for analysis.
"""

from typing import Any, Literal

import numpy as np
from rasterio import MemoryFile
from rasterio.transform import Affine
from rasterio.warp import Resampling, calculate_default_transform, reproject
from rasterio.windows import from_bounds

from ..typing import ArrayLike, BBox


# Local type aliases for rasterio types
Transform = Any  # rasterio.transform.Affine
CRS = Any  # rasterio.crs.CRS


def reproject_image(
    data: ArrayLike,
    src_transform: Transform,
    src_crs: CRS,
    dst_crs: CRS,
    dst_shape: tuple[int, int] | None = None,
    dst_transform: Transform | None = None,
    resampling: Literal["nearest", "bilinear", "cubic", "average"] = "nearest",
    nodata: float | None = None,
    resolution: float | tuple[float, float] | None = None,
) -> tuple[ArrayLike, Transform, BBox]:
    """Reproject image data to target coordinate reference system.

    Args:
        data: Input image array (2D or 3D with bands as first dimension)
        src_crs: Source coordinate reference system (e.g., "EPSG:4326")
        dst_crs: Target coordinate reference system
        src_transform: Affine transform for source data
        dst_shape: Optional output shape (height, width)
        dst_transform: Optional destination transform
        resampling: Resampling method ('bilinear', 'cubic', 'nearest', 'average')
        nodata: Optional nodata value
        resolution: Optional target resolution in units of dst_crs (single value or (x_res, y_res))

    Returns:
        Tuple of (reprojected_data, new_transform, new_bounds)
    """
    # Map resampling methods
    resample_map = {
        "nearest": Resampling.nearest,
        "bilinear": Resampling.bilinear,
        "cubic": Resampling.cubic,
        "average": Resampling.average,
    }
    resample_alg = resample_map.get(resampling, Resampling.bilinear)

    # Get dimensions
    if data.ndim == 2:
        height, width = data.shape
        bands = 1
        data_3d = data[np.newaxis, ...]
    else:
        bands, height, width = data.shape
        data_3d = data

    # Calculate bounds from transform if not provided
    west = src_transform.c
    north = src_transform.f
    east = west + src_transform.a * width
    south = north + src_transform.e * height
    source_bounds = (west, south, east, north)

    # Convert CRS to string if needed
    source_crs = str(src_crs) if not isinstance(src_crs, str) else src_crs
    target_crs = str(dst_crs) if not isinstance(dst_crs, str) else dst_crs

    # Calculate transform for target CRS if not provided.
    # If a destination transform is explicitly supplied, require an explicit shape too.
    if dst_transform is not None and dst_shape is None:
        raise ValueError("dst_shape must be provided when dst_transform is provided")

    if dst_transform is None:
        if resolution is not None:
            dst_transform, dst_width, dst_height = calculate_default_transform(
                source_crs, target_crs, width, height, *source_bounds, resolution=resolution
            )
        else:
            dst_transform, dst_width, dst_height = calculate_default_transform(
                source_crs, target_crs, width, height, *source_bounds
            )
        if dst_shape is None:
            dst_shape = (int(dst_height), int(dst_width))

    # Create output array
    assert dst_shape is not None  # Type checker hint
    reprojected = np.empty((bands,) + dst_shape, dtype=data.dtype)

    # Reproject each band
    for i in range(bands):
        reproject(
            source=data_3d[i],
            destination=reprojected[i],
            src_transform=src_transform,
            src_crs=source_crs,
            dst_transform=dst_transform,
            dst_crs=target_crs,
            resampling=resample_alg,
            src_nodata=nodata,
            dst_nodata=nodata,
        )

    # Calculate new bounds
    west = dst_transform.c
    north = dst_transform.f
    east = west + dst_transform.a * dst_shape[1]
    south = north + dst_transform.e * dst_shape[0]
    new_bounds = (west, south, east, north)

    # Return 2D array if input was 2D
    if data.ndim == 2:
        reprojected = reprojected[0]

    return reprojected, dst_transform, new_bounds


def resample_image(
    data: ArrayLike,
    src_transform: Transform,
    scale_factor: float | None = None,
    target_resolution: float | tuple[float, float] | None = None,
    target_shape: tuple[int, int] | None = None,
    resampling: Literal["nearest", "bilinear", "cubic", "average"] = "bilinear",
    nodata: float | None = None,
) -> tuple[ArrayLike, Transform]:
    """Resample image data to target resolution.

    Args:
        data: Input image array (2D or 3D with bands as first dimension)
        src_transform: Source affine transform
        scale_factor: Scale factor for resolution (e.g., 0.5 for half resolution)
        target_resolution: Desired resolution in meters (single value or (x_res, y_res))
        target_shape: Target shape (height, width)
        resampling: Resampling method ('bilinear', 'cubic', 'nearest', 'average')
        nodata: Optional nodata value

    Returns:
        Tuple of (resampled_data, new_transform)
    """
    # Normalize resolutions to tuples
    source_resolution = (abs(src_transform.a), abs(src_transform.e))

    if target_resolution is not None and isinstance(target_resolution, int | float):
        target_resolution = (target_resolution, target_resolution)

    # Map resampling methods
    resample_map = {
        "nearest": Resampling.nearest,
        "bilinear": Resampling.bilinear,
        "cubic": Resampling.cubic,
        "average": Resampling.average,
    }
    resample_alg = resample_map.get(resampling, Resampling.bilinear)

    # Get dimensions
    if data.ndim == 2:
        height, width = data.shape
        bands = 1
        data_3d = data[np.newaxis, ...]
    else:
        bands, height, width = data.shape
        data_3d = data

    # Calculate new dimensions based on input parameters
    if scale_factor is not None:
        # Scale factor provided
        new_width = int(width * scale_factor)
        new_height = int(height * scale_factor)
        target_resolution = (
            source_resolution[0] / scale_factor,
            source_resolution[1] / scale_factor,
        )
    elif target_resolution is not None:
        # Target resolution provided
        scale_x = source_resolution[0] / target_resolution[0]
        scale_y = source_resolution[1] / target_resolution[1]
        new_width = int(width * scale_x)
        new_height = int(height * scale_y)
    elif target_shape is not None:
        # Target shape provided
        new_height, new_width = target_shape
        target_resolution = (
            source_resolution[0] * width / new_width,
            source_resolution[1] * height / new_height,
        )
    else:
        # No change needed
        return data, src_transform

    # Create new transform with target resolution
    new_transform = Affine(
        target_resolution[0] * (1 if src_transform.a > 0 else -1),
        src_transform.b,
        src_transform.c,
        src_transform.d,
        target_resolution[1] * (1 if src_transform.e > 0 else -1),
        src_transform.f,
    )

    # Create output array
    resampled = np.empty((bands, new_height, new_width), dtype=data.dtype)

    # Use MemoryFile for in-memory resampling
    profile = {
        "driver": "GTiff",
        "width": width,
        "height": height,
        "count": bands,
        "dtype": data.dtype,
        "transform": src_transform,
        "nodata": nodata,
    }

    with MemoryFile() as memfile, memfile.open(**profile, mode="w") as src:
        # Write all bands
        for i in range(bands):
            src.write(data_3d[i], i + 1)

        # Read resampled data
        out_shape = (bands, new_height, new_width)
        resampled = src.read(out_shape=out_shape, resampling=resample_alg)

    # Return 2D array if input was 2D
    if data.ndim == 2:
        resampled = resampled[0]

    return resampled, new_transform


def crop_to_bounds(
    data: ArrayLike,
    src_transform: Transform,
    bounds: BBox,
    buffer_distance: float | None = None,
    buffer_units: Literal["crs", "pixels"] = "crs",
) -> tuple[ArrayLike, Transform, BBox]:
    """Crop raster data to specified spatial bounds.

    Args:
        data: Input image array (2D or 3D with bands as first dimension)
        src_transform: Affine transform for the data
        bounds: Target bounds (min_x, min_y, max_x, max_y) in CRS units
        buffer_distance: Optional buffer distance to add around bounds
        buffer_units: Units for buffer ("crs" for coordinate units or "pixels")

    Returns:
        Tuple of (cropped_data, new_transform, actual_bounds)
    """
    try:
        # Apply buffer to bounds
        min_x, min_y, max_x, max_y = bounds
        if buffer_distance is not None and buffer_distance > 0:
            if buffer_units == "crs":
                min_x -= buffer_distance
                min_y -= buffer_distance
                max_x += buffer_distance
                max_y += buffer_distance
            else:  # pixels
                pixel_buffer_x = buffer_distance * abs(src_transform.a)
                pixel_buffer_y = buffer_distance * abs(src_transform.e)
                min_x -= pixel_buffer_x
                min_y -= pixel_buffer_y
                max_x += pixel_buffer_x
                max_y += pixel_buffer_y

        # Get window from bounds - rasterio handles fractional pixels
        window = from_bounds(min_x, min_y, max_x, max_y, src_transform)

        # Round to integer pixels to match GDAL's projWin behavior
        col_off = round(window.col_off)
        row_off = round(window.row_off)
        width = round(window.width)
        height = round(window.height)

        # Get data dimensions
        if data.ndim == 2:
            data_height, data_width = data.shape
        else:
            _, data_height, data_width = data.shape

        # Ensure we don't exceed data bounds
        col_off = max(0, col_off)
        row_off = max(0, row_off)
        col_end = min(data_width, col_off + width)
        row_end = min(data_height, row_off + height)

        # Crop the data
        if data.ndim == 2:
            cropped = data[row_off:row_end, col_off:col_end]
        else:
            cropped = data[:, row_off:row_end, col_off:col_end]

        # Calculate the new transform
        new_transform: Affine = src_transform * Affine.translation(col_off, row_off)

        # Calculate actual bounds of the cropped data
        actual_height, actual_width = cropped.shape[-2:]
        actual_bounds = (
            new_transform.c,  # west
            new_transform.f + new_transform.e * actual_height,  # south
            new_transform.c + new_transform.a * actual_width,  # east
            new_transform.f,  # north
        )

        return cropped, new_transform, actual_bounds

    except ImportError:
        # Fallback if rasterio not available
        raise ImportError("Rasterio is required for crop_to_bounds") from None


def align_rasters(
    rasters: list[ArrayLike],
    transforms: list[Transform],
    crs_list: list[str] | None = None,
    reference_idx: int = 0,
    resampling: Literal["nearest", "bilinear", "cubic"] = "nearest",
    nodata: float | None = None,
) -> list[tuple[ArrayLike, Transform]]:
    """Align multiple rasters to a common grid and extent.

    Args:
        rasters: List of raster arrays to align
        transforms: List of affine transforms for each raster
        crs_list: Optional list of CRS strings for each raster
        reference_idx: Index of reference raster to align others to
        resampling: Resampling method for alignment
        nodata: Optional nodata value

    Returns:
        List of tuples (aligned_data, transform) for each raster
    """
    if not rasters:
        raise ValueError("No rasters provided for alignment")

    if len(rasters) != len(transforms):
        raise ValueError("Number of rasters must match number of transforms")

    if crs_list and len(crs_list) != len(rasters):
        raise ValueError("Number of CRS strings must match number of rasters")

    # Use reference raster for alignment target
    ref_raster = rasters[reference_idx]
    ref_transform = transforms[reference_idx]
    ref_crs = crs_list[reference_idx] if crs_list else "EPSG:4326"

    # Get reference dimensions
    if ref_raster.ndim == 2:
        ref_height, ref_width = ref_raster.shape
    else:
        _, ref_height, ref_width = ref_raster.shape

    # Map resampling methods
    resample_map = {
        "nearest": Resampling.nearest,
        "bilinear": Resampling.bilinear,
        "cubic": Resampling.cubic,
        "average": Resampling.average,
    }
    resample_alg = resample_map.get(resampling, Resampling.nearest)

    # Align each raster
    results = []
    for i, (raster, transform) in enumerate(zip(rasters, transforms, strict=True)):
        if i == reference_idx:
            # Reference raster stays unchanged
            results.append((raster, transform))
        else:
            # Get source CRS
            src_crs = crs_list[i] if crs_list else ref_crs

            # Handle 2D or 3D data
            if raster.ndim == 2:
                bands = 1
                _, _ = raster.shape
                raster_3d = raster[np.newaxis, ...]
            else:
                bands, _, _ = raster.shape
                raster_3d = raster

            # Create aligned array
            aligned = np.empty((bands, ref_height, ref_width), dtype=raster.dtype)

            # Reproject each band to reference grid
            for b in range(bands):
                reproject(
                    source=raster_3d[b],
                    destination=aligned[b],
                    src_transform=transform,
                    src_crs=src_crs,
                    dst_transform=ref_transform,
                    dst_crs=ref_crs,
                    resampling=resample_alg,
                    src_nodata=nodata,
                    dst_nodata=nodata,
                )

            # Return 2D if input was 2D
            if raster.ndim == 2:
                aligned = aligned[0]

            results.append((aligned, ref_transform))

    return results


def convert_to_wgs84(
    data: ArrayLike,
    src_transform: Transform,
    src_crs: CRS,
    bounds: BBox,
    target_resolution: float | None = None,
) -> tuple[ArrayLike, Transform]:
    """Convert raster to WGS84 (EPSG:4326) projection.

    Args:
        data: Input raster array
        src_transform: Source affine transform
        src_crs: Source CRS
        bounds: Geographic bounds for output
        target_resolution: Optional target resolution in degrees

    Returns:
        Tuple of (wgs84_data, wgs84_transform)
    """
    # Use reproject_image with WGS84 as target
    wgs84_data, wgs84_transform, _ = reproject_image(
        data,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_crs="EPSG:4326",
        resampling="bilinear",
    )

    # If target resolution specified, resample
    if target_resolution is not None:
        wgs84_data, wgs84_transform = resample_image(
            wgs84_data,
            src_transform=wgs84_transform,
            target_resolution=target_resolution,
            resampling="bilinear",
        )

    return wgs84_data, wgs84_transform
