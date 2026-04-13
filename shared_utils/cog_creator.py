"""
COG creator module - handles Cloud Optimized GeoTIFF creation.
Single responsibility: COG creation with overviews and optimization.
"""

import os
import rasterio
from rasterio.enums import Resampling
import numpy as np
import uuid
from shared_utils.reprojection import calculate_overview_factors


def create_cog_with_overviews(input_path, output_path, compression_config, verbose=True):
    """
    Create a COG with overviews from an input file.

    Args:
        input_path: Path to input file
        output_path: Path to output COG
        compression_config: Compression configuration
        verbose: Print progress messages

    Returns:
        bool: True if successful
    """
    try:
        with rasterio.open(input_path, 'r') as src:
            # Create profile for COG
            profile = src.profile.copy()
            profile.update(compression_config)

            if verbose:
                print(f"   [COG] Writing optimized COG...")

            # Write COG
            with rasterio.open(output_path, 'w', **profile) as dst:
                # Copy data band by band
                for band_idx in range(1, src.count + 1):
                    data = src.read(band_idx)
                    dst.write(data, band_idx)

                # Build overviews
                if verbose:
                    print(f"   [COG] Building overviews...")
                factors = [2, 4, 8, 16, 32]
                dst.build_overviews(factors, Resampling.average)

                # Update tags for COG
                dst.update_tags(ns='rio_overview', resampling='average')

        if verbose:
            print(f"   [COG] ✅ COG created successfully")

        return True

    except Exception as e:
        if verbose:
            print(f"   [COG] ❌ Failed to create COG: {e}")
        return False


def add_overviews_to_file(file_path, factors=[2, 4, 8, 16, 32], resampling=Resampling.average):
    """
    Add overviews to an existing GeoTIFF file.

    Args:
        file_path: Path to the file
        factors: Overview factors
        resampling: Resampling method

    Returns:
        bool: True if successful
    """
    try:
        with rasterio.open(file_path, 'r+') as dst:
            dst.build_overviews(factors, resampling)
            dst.update_tags(ns='rio_overview', resampling=resampling.name)
        return True
    except Exception as e:
        print(f"   [OVERVIEW] Failed to add overviews: {e}")
        return False


def optimize_cog_structure(src_path, dst_path, compression_config):
    """
    Optimize COG structure for cloud storage.

    Args:
        src_path: Source file path
        dst_path: Destination file path
        compression_config: Compression configuration

    Returns:
        bool: True if successful
    """
    try:
        with rasterio.open(src_path, 'r') as src:
            # Get optimal COG profile
            profile = src.profile.copy()

            # Update with COG-specific settings
            profile.update({
                'driver': 'GTiff',
                'tiled': True,
                'blockxsize': 512,
                'blockysize': 512,
                'interleave': 'pixel' if src.count > 1 else 'band'
            })

            # Add compression settings
            profile.update(compression_config)

            # Write optimized COG
            with rasterio.open(dst_path, 'w', **profile) as dst:
                # Copy all bands
                for band_idx in range(1, src.count + 1):
                    dst.write(src.read(band_idx), band_idx)

                # Add overviews
                factors = calculate_overview_factors(src.width, src.height)
                dst.build_overviews(factors, Resampling.average)

        return True

    except Exception as e:
        print(f"   [OPTIMIZE] Failed to optimize COG: {e}")
        return False


def write_cog_from_array(data, profile, output_path, overviews=True, verbose=True):
    """
    Write a COG directly from a numpy array.

    Args:
        data: Numpy array with data (bands, height, width) or (height, width)
        profile: Rasterio profile
        output_path: Output file path
        overviews: Add overviews
        verbose: Print progress

    Returns:
        bool: True if successful
    """
    try:
        # Ensure data is 3D
        if data.ndim == 2:
            data = np.expand_dims(data, axis=0)

        # Update profile for number of bands
        profile['count'] = data.shape[0]

        if verbose:
            print(f"   [COG] Writing COG from array...")

        with rasterio.open(output_path, 'w', **profile) as dst:
            # Write all bands
            for band_idx in range(data.shape[0]):
                dst.write(data[band_idx], band_idx + 1)

            # Add overviews if requested
            if overviews:
                if verbose:
                    print(f"   [COG] Building overviews...")
                factors = calculate_overview_factors(profile['width'], profile['height'])
                dst.build_overviews(factors, Resampling.average)
                dst.update_tags(ns='rio_overview', resampling='average')

        if verbose:
            print(f"   [COG] ✅ Written to {output_path}")

        return True

    except Exception as e:
        if verbose:
            print(f"   [COG] ❌ Failed to write COG: {e}")
        return False