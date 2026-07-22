"""
process_capella.py

CLI processing for Capella SAR products
"""

import argparse
import csv
import os
import sys

from botocore.exceptions import BotoCoreError, ClientError

from capella.capella_v2 import (
    retrieve_capella_resources,
    group_capella_scenes,
    report_capella_scenes,
    sigmaCalib
)

from shared_utils.cog_utils import convert_to_cog
from shared_utils.cog_metadata import load_metadata_json
from shared_utils.s3utils import explain_s3_read_failure


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

    if args.list_dates:
        try:
            scenes = report_capella_scenes(bucket=args.bucket, prefix=args.prefix)
        except (ClientError, BotoCoreError) as e:
            msg = explain_s3_read_failure(e, args.bucket, args.prefix)
            print(msg or f"Failed to list s3://{args.bucket}/{args.prefix}: {e}",
                  file=sys.stderr)
            sys.exit(2)
        print(
            f"{len(scenes)} available Capella scene(s) in "
            f"s3://{args.bucket}/{args.prefix} -- most recently added to S3 "
            f"first (top = closest to today). Copy a --date value to process:\n"
        )
        # Aligned table; scene folder LAST so the fixed-width columns stay
        # aligned regardless of the (long) Capella scene name.
        print(
            f"  {'--date':<16}{'acquired (UTC)':<22}"
            f"{'added to S3 (UTC)':<22}scene folder"
        )
        for s in scenes:
            print(
                f"  {s['date']:<16}"
                f"{s['acquired'].strftime('%Y-%m-%d %H:%M:%S'):<22}"
                f"{s['added_to_s3'].strftime('%Y-%m-%d %H:%M:%S'):<22}"
                f"{s['scene']}"
            )

        # Also drop a sortable CSV artifact so the report survives outside the
        # raw job log (on DPS it lands in output/ -> browsable via the Jobs
        # panel's "Open in File Browser", rendered as a grid by JupyterLab).
        os.makedirs(args.output, exist_ok=True)
        csv_path = os.path.join(args.output, "available_capella_dates.csv")
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
        if not scenes:
            print(
                f"\nNo scenes found at s3://{args.bucket}/{args.prefix} (read "
                "access OK). Double-check the sensor/prefix, or the vendor may "
                "not have delivered any scenes yet.",
                file=sys.stderr,
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

    # One group per GEO band = one genuine scene. Folders may hold several
    # processing levels of the same acquisition (GEO, SLC); only GEO is used,
    # so grouping by GEO both drops the unused levels and, in the rare case of
    # multiple same-timestamp acquisitions, processes every one.
    scenes = group_capella_scenes(tifs)

    if not scenes:
        raise FileNotFoundError(
            f"No Capella GEO band found for --date {args.date} "
            f"in s3://{args.bucket}/{args.prefix}"
        )

    print(f"Found {len(scenes)} Capella scene(s) for --date {args.date}")

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

        outfile = None
        source_tif = None

        if args.product == "sigma":

            outfile, source_tif = sigmaCalib(
                scene_tifs,
                save_location=scene_out,
                do_filt=args.apply_filter,
                filter_size=args.filter_size
            )

        if not outfile:
            continue

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

        # Delete the raw downloaded source raster now that a valid COG exists.
        # Gated on COG success (a failure/exception above skips cleanup so the
        # download is preserved for a retry).
        if cog_path and source_tif and os.path.exists(source_tif):
            os.remove(source_tif)
            print(f"Removed source raster: {source_tif}")

    print(f"\nCreated {len(cog_paths)} COG(s).")


if __name__ == "__main__":
    main()