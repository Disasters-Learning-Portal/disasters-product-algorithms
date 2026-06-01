"""
process_umbra.py

CLI processing for Umbra SAR products
"""

import argparse
import os
from umbra.umbra_v2 import (
    retrieve_umbra_resources,
    sigmaCalib,
    betaCalib,
    gammaCalib,
    rcsCalib,
    apply_filter
)

from shared_utils.cog_utils import convert_to_cog
from shared_utils.cog_metadata import load_metadata_json


def main():
    parser = argparse.ArgumentParser(description="Process Umbra imagery")

    parser.add_argument(
        "--product",
        required=True,
        choices=["sigma", "beta", "gamma", "rcs"],
        help="Calibration product to generate"
    )
    
    parser.add_argument(
        "--apply_filter",
        action="store_true",
        help="Apply filtering to the selected product"
    )

    parser.add_argument(
        "--filter_size",
        type=int,
        default=5,
        help="Lee filter window size (e.g. 3, 5, 7)"
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
    parser.add_argument('-nodata', type=float, default=None, help='No-data value for COG outputs (auto-detected if not specified).')
    parser.add_argument('-compression', type=str, default='ZSTD', help='Compression type for COG (default: ZSTD).')
    parser.add_argument('-compression_level', type=int, default=22, help='Compression level for COG (default: 22 for ZSTD).')
    parser.add_argument(
        '-dst_crs',
        type=str,
        default='native',
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

    dst_crs_value = None if args.dst_crs.lower() == 'native' else args.dst_crs
    metadata = load_metadata_json(args.metadata_json)

    print("Retrieving Umbra resources...")
    tifs = retrieve_umbra_resources(
        date=args.date,
        bucket=args.bucket,
        prefix=args.prefix
    )

    print(f"Generating {args.product}...")

    outfile = None

    if args.product == "sigma":
        outfile = sigmaCalib(tifs, args.output)
    
        if args.apply_filter:
            raw_outfile = outfile
            outfile = apply_filter(outfile, size=args.filter_size)
    
            # remove raw tif
            if os.path.exists(raw_outfile):
                os.remove(raw_outfile)
    
    elif args.product == "beta":
        outfile = betaCalib(tifs, args.output)
    
        if args.apply_filter:
            raw_outfile = outfile
            outfile = apply_filter(outfile, size=args.filter_size)
            
            if os.path.exists(raw_outfile):
                os.remove(raw_outfile)
    
    elif args.product == "gamma":
        outfile = gammaCalib(tifs, args.output)
    
        if args.apply_filter:
            raw_outfile = outfile
            outfile = apply_filter(outfile, size=args.filter_size)
            
            if os.path.exists(raw_outfile):
                os.remove(raw_outfile)
    
    elif args.product == "rcs":
        outfile = rcsCalib(tifs, args.output)
    
        if args.apply_filter:
            raw_outfile = outfile
            outfile = apply_filter(outfile, size=args.filter_size)
            
            if os.path.exists(raw_outfile):
                os.remove(raw_outfile)

    # COG Conversion Step
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