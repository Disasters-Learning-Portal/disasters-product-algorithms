"""
process_capella.py

CLI processing for Capella SAR products
"""

import argparse
import os

from capella.capella_v2 import (
    retrieve_capella_resources,
    report_capella_scenes,
    sigmaCalib
)

from shared_utils.cog_utils import convert_to_cog
from shared_utils.cog_metadata import load_metadata_json


def main():

    parser = argparse.ArgumentParser(
        description="Process Capella imagery"
    )

    parser.add_argument(
        "--list_dates",
        action="store_true",
        help=(
            "Report the Capella scenes available in the vendor bucket "
            "(--bucket/--prefix), newest first by S3 delivery time, then exit "
            "without processing. Use it to discover which --date values exist; "
            "each printed date can be passed straight back as --date. Ignores "
            "--date/--product."
        ),
    )

    parser.add_argument(
        "--product",
        choices=["sigma"],
        help="Calibration product to generate"
    )

    parser.add_argument(
        "--apply_filter",
        action="store_true",
        help="Apply Lee filtering"
    )

    parser.add_argument(
        "--filter_size",
        type=int,
        default=5,
        help="Lee filter window size"
    )

    parser.add_argument(
        "--date",
        help="Target date (YYYYMMDDHHMMSS)"
    )

    parser.add_argument(
        "--prefix",
        default="disasters",
        help="S3 prefix"
    )

    parser.add_argument(
        "--bucket",
        default="csdap-capellaspace-delivery",
        help="S3 bucket"
    )

    parser.add_argument(
        "--output",
        default="/tmp/s3_temp",
        help="Output directory"
    )

    # COG options
    parser.add_argument(
        "-nodata",
        type=float,
        default=None,
        help="No-data value for COG outputs"
    )

    parser.add_argument(
        "-compression",
        type=str,
        default="ZSTD",
        help="Compression type for COG"
    )

    parser.add_argument(
        "-compression_level",
        type=int,
        default=22,
        help="Compression level for COG"
    )

    parser.add_argument(
        "-dst_crs",
        type=str,
        default="native",
        help=(
            "Target CRS for COG output. 'native' (default) preserves the "
            "source UTM projection; pass 'EPSG:3857' for Web Mercator "
            "(required by veda-data-airflow build_stac)."
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

    if args.list_dates:
        scenes = report_capella_scenes(bucket=args.bucket, prefix=args.prefix)
        print(
            f"{len(scenes)} available Capella scene(s) in "
            f"s3://{args.bucket}/{args.prefix} -- most recently added to S3 "
            f"first (top = closest to today). Pass a --date value to process:\n"
        )
        print(f"  {'--date':<16}{'acquired (UTC)':<22}added to S3 (UTC)")
        for s in scenes:
            print(
                f"  {s['date']:<16}"
                f"{s['acquired'].strftime('%Y-%m-%d %H:%M:%S'):<22}"
                f"{s['added_to_s3'].strftime('%Y-%m-%d %H:%M:%S')}"
            )
        return

    # --date / --product are optional above so --list_dates can run without
    # them; enforce them here for the normal processing path.
    missing = [n for n, v in (("--date", args.date), ("--product", args.product)) if not v]
    if missing:
        parser.error("the following arguments are required: " + ", ".join(missing))

    dst_crs_value = None if args.dst_crs.lower() == "native" else args.dst_crs
    metadata = load_metadata_json(args.metadata_json)

    print("Retrieving Capella resources...")

    tifs = retrieve_capella_resources(
        date=args.date,
        bucket=args.bucket,
        prefix=args.prefix
    )

    outfile = None

    if args.product == "sigma":

        outfile = sigmaCalib(
            tifs,
            save_location=args.output,
            do_filt=args.apply_filter,
            filter_size=args.filter_size
        )

    # Convert to COG
    if outfile:

        print("\nConverting to COG...")

        cog_path = convert_to_cog(
            outfile,
            nodata=args.nodata,
            dst_crs=dst_crs_value,
            compression=args.compression,
            compression_level=args.compression_level,
            metadata=metadata,
        )

        print(f"COG created: {cog_path}")


if __name__ == "__main__":
    main()