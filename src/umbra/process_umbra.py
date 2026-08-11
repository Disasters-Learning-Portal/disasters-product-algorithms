"""
process_umbra.py

CLI processing for Umbra SAR products
"""

import argparse
import os

from umbra.umbra_v2 import (
    retrieve_umbra_resources,
    group_umbra_scenes,
    sigmaCalib,
    betaCalib,
    gammaCalib,
)

from shared_utils.cog_utils import convert_to_cog
from shared_utils.cog_metadata import load_metadata_json


def main():
    parser = argparse.ArgumentParser(description="Process Umbra imagery")

    parser.add_argument(
        "--product",
        choices=["sigma", "beta", "gamma"],
        required=True,
        help="Calibration product to generate"
    )

    parser.add_argument(
        "--filter_size",
        type=int,
        choices=[3, 5, 7],
        default=5,
        help=(
            "Lee speckle-filter window size. Filtering is always applied to "
            "the backscatter (there is no opt-out); only the kernel is tunable."
        ),
    )

    parser.add_argument(
        "--date",
        required=True,
        help="Target date (YYYY-MM-DD HH:MM:SS)"
    )

    parser.add_argument(
        "--prefix",
        default="disasters",
        help="S3 prefix"
    )

    parser.add_argument(
        "--bucket",
        default="csda-data-vendor-umbra",
        help="S3 bucket"
    )

    parser.add_argument(
        "--output",
        default="/tmp/s3_temp",
        help="Output directory"
    )

    # COG options
    parser.add_argument('-nodata', type=float, default=-9999.0, help='No-data value for COG outputs (default -9999.0). SAR backscatter is float32 dB where 0 dB is a legitimate value, so nodata must never be 0.')
    parser.add_argument('-compression', type=str, default='ZSTD', help='Compression type for COG (default: ZSTD).')
    parser.add_argument('-compression_level', type=int, default=22, help='Compression level for COG (default: 22 for ZSTD).')
    parser.add_argument(
        '-dst_crs',
        type=str,
        default='native',
        help=(
            "Target CRS for COG output. 'native' (default) preserves the "
            "source UTM projection; pass 'EPSG:3857' (Web Mercator) for "
            "optimal VEDA titiler-pgstac tiling (also required by "
            "veda-data-airflow build_stac)."
        ),
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

    dst_crs_value = None if args.dst_crs.lower() == 'native' else args.dst_crs
    metadata = load_metadata_json(args.metadata_json)

    print("Retrieving Umbra resources...")
    tifs = retrieve_umbra_resources(
        date=args.date,
        bucket=args.bucket,
        prefix=args.prefix
    )

    # One group per GEC band = one genuine scene. Pooled folders may hold other
    # bands/levels; only GEC is calibrated, so grouping by GEC drops the unused
    # ones and, when several scenes share a timestamp, processes every one.
    scenes = group_umbra_scenes(tifs)

    if not scenes:
        raise FileNotFoundError(
            f"No Umbra GEC band found for --date {args.date} "
            f"in s3://{args.bucket}/{args.prefix}"
        )

    print(f"Found {len(scenes)} Umbra scene(s) for --date {args.date}")

    cog_paths = []

    for i, scene_tifs in enumerate(scenes, start=1):

        print(f"\nProcessing scene {i}/{len(scenes)}: {scene_tifs[0]}")

        # Keep single-scene output flat (byte-identical to before). Isolate each
        # scene in its own subdir only when there are several, so identically
        # named (same-timestamp) COGs don't clobber each other locally or in S3.
        scene_out = (
            args.output if len(scenes) == 1
            else os.path.join(args.output, f"scene_{i}")
        )

        print(f"Generating {args.product}...")

        outfile = None

        # Lee filtering is baked into each calibration (always on); the kernel
        # comes straight from --filter_size.
        if args.product == "sigma":
            outfile = sigmaCalib(scene_tifs, scene_out, filter_size=args.filter_size)
        elif args.product == "beta":
            outfile = betaCalib(scene_tifs, scene_out, filter_size=args.filter_size)
        elif args.product == "gamma":
            outfile = gammaCalib(scene_tifs, scene_out, filter_size=args.filter_size)

        # COG Conversion Step
        if outfile:
            print("Converting to COG...")

            cog_path = convert_to_cog(
                outfile,
                nodata=args.nodata,
                dst_crs=dst_crs_value,
                compression=args.compression,
                compression_level=args.compression_level,
                metadata=metadata,
            )

            print(f"COG created: {cog_path}")
            cog_paths.append(cog_path)

    print(f"\nCreated {len(cog_paths)} COG(s).")


if __name__ == "__main__":
    main()