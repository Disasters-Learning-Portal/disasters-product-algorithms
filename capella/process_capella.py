"""
process_capella.py

CLI processing for Capella SAR products
"""

import argparse
import os

from capella.capella_v2 import (
    retrieve_capella_resources,
    sigmaCalib,
    apply_filter
)

from shared_utils.cog_utils import convert_to_cog


def main():

    parser = argparse.ArgumentParser(
        description="Process Capella imagery"
    )

    parser.add_argument(
        "--product",
        required=True,
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
        required=True,
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

    args = parser.parse_args()

    dst_crs_value = None if args.dst_crs.lower() == "native" else args.dst_crs

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
            save_location=args.output
        )

        if args.apply_filter:

            raw_outfile = outfile

            outfile = apply_filter(
                outfile,
                size=args.filter_size
            )

            if os.path.exists(raw_outfile):
                os.remove(raw_outfile)

    # Convert to COG
    if outfile:

        print("\nConverting to COG...")

        cog_path = convert_to_cog(
            outfile,
            nodata=args.nodata,
            dst_crs=dst_crs_value,
            compression=args.compression,
            compression_level=args.compression_level
        )

        print(f"COG created: {cog_path}")


if __name__ == "__main__":
    main()