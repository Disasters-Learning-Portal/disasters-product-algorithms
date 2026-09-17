"""
process_iceye.py

CLI processing for ICEYE SAR products
"""

import argparse
import os

from iceye.iceye_v2 import (
    ICEYE_NODATA,
    retrieve_iceye_resources,
    sigmaCalib
)

from shared_utils.cog_utils import convert_to_cog
from shared_utils.cog_metadata import load_metadata_json
from shared_utils.plotting import save_cog_png


# Fixed processing parameters -- same treatment as Capella (PR #76): one
# vendor bucket, one calibration product, one COG encoding. Changing one is a
# code change with a review, not a per-run argument.
ICEYE_BUCKET = "csdap-iceye-delivery"
ICEYE_PREFIX = "disasters"
SOURCE = "ICEYE"
COMPRESSION = "ZSTD"
COMPRESSION_LEVEL = 9
DST_CRS = None  # native projection; no warp


def main():

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
            "applied to the raw GRD digital numbers before squaring "
            "and calibration; only the kernel is tunable."
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

    parser.add_argument(
        "--metadata-json",
        type=str,
        default=None,
        help=(
            "Path to a JSON file containing activation-event metadata to "
            "embed as GeoTIFF tags on the output COG (e.g. ACTIVATION_EVENT, "
            "SOURCE, PROCESSOR). The notebooks write ACTIVATION_METADATA to "
            "a temp JSON file and pass it here."
        ),
    )

    args = parser.parse_args()

    metadata = load_metadata_json(args.metadata_json)
    # Fill in the vendor as the default provenance, but never clobber a
    # SOURCE the operator supplied via --metadata-json.
    metadata.setdefault("SOURCE", SOURCE)

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

    # Speckle filtering is always on; --filter_size only tunes the kernel.
    outfile = sigmaCalib(
        s3_image_paths=image_paths,
        s3_metadata_paths=metadata_paths,
        save_location=args.output,
        filter_size=args.filter_size
    )

    print("\nConverting to COG...")

    cog_path = convert_to_cog(
        outfile,
        nodata=ICEYE_NODATA,
        dst_crs=DST_CRS,
        compression=COMPRESSION,
        compression_level=COMPRESSION_LEVEL,
        metadata=metadata,
    )

    print(f"COG created: {cog_path}")

    # PNG quicklook next to the COG.
    png_path = os.path.splitext(cog_path)[0] + ".png"

    save_cog_png(
        src=cog_path,
        out_path=png_path,
    )

    print(f"PNG created: {png_path}")

    print("\nCreated 1 COG.")


if __name__ == "__main__":
    main()
