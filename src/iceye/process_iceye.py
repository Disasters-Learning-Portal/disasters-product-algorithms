"""
process_iceye.py

CLI processing for ICEYE SAR products
"""

import argparse
import os

from iceye.iceye_v2 import (
    retrieve_iceye_resources,
    sigmaCalib
)

from shared_utils.cog_utils import convert_to_cog
from shared_utils.plotting import save_cog_png


def main():

    ICEYE_BUCKET = "csdap-iceye-delivery"
    ICEYE_PREFIX = "disasters"
    SOURCE = "ICEYE"

    NODATA = -9999
    COMPRESSION = "ZSTD"
    COMPRESSION_LEVEL = 9

    parser = argparse.ArgumentParser(
        description="Process ICEYE imagery"
    )

    parser.add_argument(
        "--filter_size",
        type=int,
        choices=[3, 5, 7],
        default=5,
        help=(
            "Lee speckle-filter window size. Filtering is always "
            "applied to the calibrated backscatter; only the kernel "
            "is tunable."
        ),
    )

    parser.add_argument(
        "--date",
        required=True,
        help="Target date (YYYY-MM-DD HH:MM:SS)"
    )

    parser.add_argument(
        "--output",
        default="/tmp/s3_temp",
        help="Output directory"
    )

    args = parser.parse_args()

    os.makedirs(args.output, exist_ok=True)

    print("Retrieving ICEYE resources...")

    metadata_paths, image_paths = retrieve_iceye_resources(
        date=args.date,
        bucket=ICEYE_BUCKET,
        prefix=ICEYE_PREFIX
    )

    print(
        f"Found {len(image_paths)} ICEYE image file(s) "
        f"and {len(metadata_paths)} metadata file(s)"
    )

    print("\nGenerating ICEYE Sigma0 products...")

    output_files = sigmaCalib(
        s3_image_paths=image_paths,
        s3_metadata_paths=metadata_paths,
        save_location=args.output,
        filter_size=args.filter_size
    )
    
    # sigmaCalib may return a single path or a list of paths
    if isinstance(output_files, (str, os.PathLike)):
        output_files = [output_files]
    
    cog_paths = []
    
    for outfile in output_files:

        if not outfile:
            continue

        print("\nConverting to COG...")

        cog_path = convert_to_cog(
            outfile,
            nodata=NODATA,
            dst_crs=None,
            compression=COMPRESSION,
            compression_level=COMPRESSION_LEVEL
        )

        print(f"COG created: {cog_path}")

        cog_paths.append(cog_path)

        # Create PNG preview
        png_path = os.path.splitext(cog_path)[0] + ".png"

        save_cog_png(
            src=cog_path,
            out_path=png_path,
        )

        print(f"PNG created: {png_path}")

    print(f"\nCreated {len(cog_paths)} COG(s).")


if __name__ == "__main__":
    main()