#!/usr/bin/env python3
"""
Update nodata values in GeoTIFF files while maintaining proper COG structure.
Uses GDAL's native COG driver to ensure proper IFD ordering and COG compliance.
"""

import subprocess
import rasterio
import numpy as np
from pathlib import Path
import tempfile
import shutil
import sys


def update_nodata_cog(input_path: str):
    """
    Update nodata value in a GeoTIFF file using GDAL COG driver.
    Auto-determines nodata based on data type:
    - uint8 with 0-255 range: nodata = 0
    - float32/float64: nodata = -9999

    Args:
        input_path: Path to the input GeoTIFF file
    """
    input_path = Path(input_path)
    print(f"\nProcessing: {input_path.name}")

    # Analyze the input file
    with rasterio.open(input_path) as src:
        current_nodata = src.nodata
        dtype = src.dtypes[0]
        data = src.read(1)

        print(f"  Current nodata: {current_nodata}")
        print(f"  Data type: {dtype}")

        # Auto-determine target nodata value
        if dtype in ['uint8', 'byte']:
            target_nodata = 0
            print(f"  Target nodata: 0 (uint8)")
        elif dtype in ['float32', 'float64']:
            target_nodata = -9999
            print(f"  Target nodata: -9999 (float)")
        else:
            target_nodata = -9999
            print(f"  Target nodata: -9999 (default)")

        # Check if we need to remap data values
        needs_remapping = False
        extreme_values = [
            -3.4028234663852886e+38,
            3.4028234663852886e+38,
            -3.40282346638529e+38,
            3.40282346638529e+38,
            3.3999999521443642e+38,
            -3.3999999521443642e+38
        ]

        for extreme_val in extreme_values:
            if np.any(np.isclose(data, extreme_val, rtol=1e-6)):
                needs_remapping = True
                print(f"  Detected extreme value: {extreme_val}")
                break

    # Create temporary file for intermediate processing
    temp_fd, temp_path = tempfile.mkstemp(suffix='.tif', dir=input_path.parent)
    final_fd, final_path = tempfile.mkstemp(suffix='.tif', dir=input_path.parent)

    try:
        # Close file descriptors
        import os
        os.close(temp_fd)
        os.close(final_fd)

        # Step 1: If we need to remap extreme values, do it with rasterio first
        if needs_remapping:
            print(f"  Step 1: Remapping extreme values to {target_nodata}")
            with rasterio.open(input_path) as src:
                profile = src.profile.copy()
                profile['nodata'] = target_nodata

                # Read and remap data
                data = src.read()
                for extreme_val in extreme_values:
                    mask = np.isclose(data, extreme_val, rtol=1e-6)
                    if np.any(mask):
                        num_pixels = np.sum(mask)
                        data[mask] = target_nodata
                        print(f"    Remapped {num_pixels} pixels from {extreme_val}")

                # Write to temp file
                with rasterio.open(temp_path, 'w', **profile) as dst:
                    dst.write(data)
                    # Copy tags
                    dst.update_tags(**src.tags())

            input_for_cog = temp_path
        else:
            input_for_cog = str(input_path)

        # Step 2: Convert to proper COG using rio cogeo
        print(f"  Step 2: Creating COG with proper structure")

        cmd = [
            'rio', 'cogeo', 'create',
            input_for_cog,
            final_path,
            '--cog-profile', 'zstd',
            '--overview-level', '22',
            '--overview-resampling', 'nearest',
            '--blocksize', '512',
            '--nodata', str(target_nodata)
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            print(f"  ✗ Error: {result.stderr}")
            return False

        # Step 3: Replace original file
        shutil.move(final_path, input_path)
        print(f"  ✓ Successfully updated to COG with nodata={target_nodata}")

        # Verify
        with rasterio.open(input_path) as src:
            print(f"  ✓ Verified: nodata={src.nodata}, dtype={src.dtypes[0]}, overviews={len(src.overviews(1))} levels")

        return True

    finally:
        # Cleanup temp files
        for tmp in [temp_path, final_path]:
            if Path(tmp).exists():
                try:
                    Path(tmp).unlink()
                except:
                    pass


def main():
    """Process all matching TIF files."""
    if len(sys.argv) > 1:
        pattern = sys.argv[1]
    else:
        pattern = '*.tif'

    files = list(Path('.').glob(pattern))

    if not files:
        print(f"No files found matching pattern: {pattern}")
        return

    print(f"Found {len(files)} file(s) matching '{pattern}'")

    success_count = 0
    for file_path in files:
        try:
            if update_nodata_cog(str(file_path)):
                success_count += 1
        except Exception as e:
            print(f"  ✗ Error processing {file_path.name}: {e}")

    print(f"\n✓ Successfully processed {success_count}/{len(files)} files")


if __name__ == '__main__':
    main()
