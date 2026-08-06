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
from shared_utils.plotting import save_cog_png


def main():
    CAPELLA_BUCKET = "csdap-capellaspace-delivery"
    CAPELLA_PREFIX = "disasters"
    SOURCE = "CSDA"
    
    NODATA = -9999
    COMPRESSION = "ZSTD"
    COMPRESSION_LEVEL = 22
    DST_CRS = None        # native projection

    
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
        help="Target date (YYYYMMDDHHMMSS)"
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

    if args.list_dates:
        try:
            scenes = report_capella_scenes(
                bucket=CAPELLA_BUCKET,
                prefix=CAPELLA_PREFIX
            )
        except (ClientError, BotoCoreError) as e:
            msg = explain_s3_read_failure(e, CAPELLA_BUCKET, CAPELLA_PREFIX)
            print(msg or f"Failed to list s3://{CAPELLA_BUCKET}/{CAPELLA_PREFIX}: {e}",
                  file=sys.stderr)
            sys.exit(2)
        print(
            f"{len(scenes)} available Capella scene(s) in "
            f"s3://{CAPELLA_BUCKET}/{CAPELLA_PREFIX} -- most recently added to S3 "
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
    missing = [n for n, v in (("--date", args.date),) if not v]
    if missing:
        parser.error("the following arguments are required: " + ", ".join(missing))
        
    metadata = load_metadata_json(args.metadata_json)

    if metadata is None:
        metadata = {}
    
    metadata["SOURCE"] = SOURCE

    print("Retrieving Capella resources...")

    tifs = retrieve_capella_resources(
        date=args.date,
        bucket=CAPELLA_BUCKET,
        prefix=CAPELLA_PREFIX
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

        outfile, source_tif = sigmaCalib(
            scene_tifs,
            save_location=scene_out,
            filter_size=args.filter_size
        )

        if not outfile:
            continue

        print("Converting to COG...")

        cog_path = convert_to_cog(
            outfile,
            nodata=NODATA,
            dst_crs=DST_CRS,
            compression=COMPRESSION,
            compression_level=COMPRESSION_LEVEL,
            metadata=metadata,
        )

        print(f"COG created: {cog_path}")
        cog_paths.append(cog_path)

        png_path = os.path.splitext(cog_path)[0] + ".png"

        save_cog_png(
            src=cog_path,
            out_path=png_path,
        )
        
        print(f"PNG created: {png_path}")

        # Delete the raw downloaded source raster now that a valid COG exists.
        # Gated on COG success (a failure/exception above skips cleanup so the
        # download is preserved for a retry).
        if cog_path and source_tif and os.path.exists(source_tif):
            os.remove(source_tif)
            print(f"Removed source raster: {source_tif}")

    print(f"\nCreated {len(cog_paths)} COG(s).")


if __name__ == "__main__":
    main()