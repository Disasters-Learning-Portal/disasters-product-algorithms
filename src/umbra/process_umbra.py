"""
process_umbra.py

CLI processing for Umbra SAR products
"""

import argparse
import csv
import os
from umbra.umbra_v2 import (
    retrieve_umbra_resources,
    report_umbra_scenes,
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
        "--list_dates",
        action="store_true",
        help=(
            "Report the Umbra scenes available in the vendor bucket "
            "(--bucket/--prefix), newest first by S3 delivery time, then exit "
            "without processing. Use it to discover which --date values exist; "
            "each printed date can be passed straight back as --date. Ignores "
            "--date/--product."
        ),
    )

    parser.add_argument(
        "--product",
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

    if args.list_dates:
        scenes = report_umbra_scenes(bucket=args.bucket, prefix=args.prefix)
        print(
            f"{len(scenes)} available Umbra scene(s) in "
            f"s3://{args.bucket}/{args.prefix} -- most recently added to S3 "
            f"first (top = closest to today). Copy a --date value to process:\n"
        )
        # Aligned table; scene folder LAST so the fixed-width columns stay
        # aligned regardless of the (long) Umbra scene name.
        print(
            f"  {'--date':<22}{'acquired (UTC)':<22}"
            f"{'added to S3 (UTC)':<22}scene folder"
        )
        for s in scenes:
            print(
                f"  {s['date']:<22}"
                f"{s['acquired'].strftime('%Y-%m-%d %H:%M:%S'):<22}"
                f"{s['added_to_s3'].strftime('%Y-%m-%d %H:%M:%S'):<22}"
                f"{s['scene']}"
            )

        # Also drop a sortable CSV artifact so the report survives outside the
        # raw job log (on DPS it lands in output/ -> browsable via the Jobs
        # panel's "Open in File Browser", rendered as a grid by JupyterLab).
        os.makedirs(args.output, exist_ok=True)
        csv_path = os.path.join(args.output, "available_umbra_dates.csv")
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["date", "scene", "acquired_utc", "added_to_s3_utc"])
            for s in scenes:
                writer.writerow([
                    s["date"],
                    s["scene"],
                    s["acquired"].strftime("%Y-%m-%d %H:%M:%S"),
                    s["added_to_s3"].strftime("%Y-%m-%d %H:%M:%S"),
                ])
        print(f"\nWrote {len(scenes)} scene(s) to {csv_path}")
        return

    # --date / --product are optional above so --list_dates can run without
    # them; enforce them here for the normal processing path.
    missing = [n for n, v in (("--date", args.date), ("--product", args.product)) if not v]
    if missing:
        parser.error("the following arguments are required: " + ", ".join(missing))

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