#!/usr/bin/env python3
"""
Convert all .tif files to use nodata=0 regardless of data type.
"""

import subprocess
import rasterio
import numpy as np
from pathlib import Path
import tempfile
import shutil
import sys


def convert_nodata_to_zero(input_path: str):
    """
    Convert nodata value to 0 in a GeoTIFF file using GDAL COG driver.
    Handles all data types.

    Args:
        input_path: Path to the input GeoTIFF file
    """
    input_path = Path(input_path)
    print(f"\nProcessing: {input_path.name}")

    # Analyze the input file
    with rasterio.open(input_path) as src:
        current_nodata = src.nodata
        dtype = src.dtypes[0]

        print(f"  Current nodata: {current_nodata}")
        print(f"  Data type: {dtype}")

        # Target is always 0
        target_nodata = 0
        print(f"  Target nodata: 0")

        # Check if already set to 0
        if current_nodata == 0:
            print(f"  ✓ Already has nodata=0, skipping")
            return True

        # Check if we need to remap data values
        data = src.read(1)
        needs_remapping = False

        # Check for extreme values
        extreme_values = [
            -3.4028234663852886e+38,
            3.4028234663852886e+38,
            -3.40282346638529e+38,
            3.40282346638529e+38,
            3.3999999521443642e+38,
            -3.3999999521443642e+38,
            -9999,
            -9999.0
        ]

        for extreme_val in extreme_values:
            if dtype in ['float32', 'float64']:
                if np.any(np.isclose(data, extreme_val, rtol=1e-6)):
                    needs_remapping = True
                    print(f"  Detected value to remap: {extreme_val}")
                    break
            else:
                if np.any(data == extreme_val):
                    needs_remapping = True
                    print(f"  Detected value to remap: {extreme_val}")
                    break

    # Create temporary files
    temp_fd, temp_path = tempfile.mkstemp(suffix='.tif', dir=input_path.parent)
    final_fd, final_path = tempfile.mkstemp(suffix='.tif', dir=input_path.parent)

    try:
        # Close file descriptors
        import os
        os.close(temp_fd)
        os.close(final_fd)

        # Step 1: Remap values if needed
        if needs_remapping:
            print(f"  Step 1: Remapping values to {target_nodata}")
            with rasterio.open(input_path) as src:
                profile = src.profile.copy()
                profile['nodata'] = target_nodata

                # Read and remap data
                data = src.read()
                for extreme_val in extreme_values:
                    if dtype in ['float32', 'float64']:
                        mask = np.isclose(data, extreme_val, rtol=1e-6)
                    else:
                        mask = (data == extreme_val)

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

        # Step 2: Convert to proper COG
        print(f"  Step 2: Creating COG with nodata=0")

        cmd = [
            'rio', 'cogeo', 'create',
            input_for_cog,
            final_path,
            '--cog-profile', 'zstd',
            '--overview-level', '5',
            '--overview-resampling', 'nearest',
            '--blocksize', '512',
            '--nodata', '0'
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            print(f"  ✗ Error: {result.stderr}")
            return False

        # Step 3: Replace original file
        shutil.move(final_path, input_path)
        print(f"  ✓ Successfully updated to COG with nodata=0")

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
    """Process files matching the pattern."""

    # Get all new .tif files
    files = list(Path('.').glob('*.tif'))
    new_files = [f for f in files if any(x in f.name for x in
                ['202501', '202506', '202507',
                 '202410_Hurricane_Milton_ARIA',
                 '202409_Hurricane_Helene_ARIA'])]

    if not new_files:
        print("No new files found to process")
        return

    print(f"Found {len(new_files)} file(s) to process")
    print("="*70)

    success_count = 0
    skip_count = 0

    for file_path in sorted(new_files):
        try:
            # Check if already has nodata=0
            with rasterio.open(file_path) as src:
                if src.nodata == 0:
                    print(f"\n{file_path.name}")
                    print(f"  ✓ Already has nodata=0, skipping")
                    skip_count += 1
                    continue

            if convert_nodata_to_zero(str(file_path)):
                success_count += 1
        except Exception as e:
            print(f"  ✗ Error processing {file_path.name}: {e}")

    print("\n" + "="*70)
    print(f"✓ Successfully processed: {success_count} files")
    print(f"⏭️ Skipped (already correct): {skip_count} files")
    print(f"Total: {success_count + skip_count}/{len(new_files)} files")


if __name__ == '__main__':
    main()
