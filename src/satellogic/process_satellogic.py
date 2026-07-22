import argparse
import csv
import os
import sys

from botocore.exceptions import BotoCoreError, ClientError

from satellogic.satellogic_v2 import (
    retrieve_satellogic_resources,
    report_satellogic_scenes,
    genTrueColor,
    gencolorIR,
    genNDVI,
    genNDWI,
    genEVI,
)

from shared_utils.cog_utils import convert_to_cog
from shared_utils.cog_metadata import load_metadata_json
from shared_utils.s3utils import explain_s3_read_failure


def group_satellogic_tifs(tifs):
    """Group each image tif with its cloud/visual companions.

    Companion matching is case-insensitive — CSDA vendor data ships mixed- and
    upper-case ``.TIF`` (see CLAUDE.md "Critical Constraints" / .clinerules
    rule 20) — but the returned keys preserve the real S3-object case so
    downstream fetches don't 404.
    """
    groups = []

    image_files = [
        x for x in tifs
        if (
            x.lower().endswith("_analytic.tif")
            or x.lower().endswith("_toa_0.tif")
        )
    ]

    def _find(expected_lower):
        """Return the real-case key whose lowercased name matches, else None."""
        return next((t for t in tifs if t.lower() == expected_lower), None)

    for image in image_files:
        lower = image.lower()

        if lower.endswith("_analytic.tif"):
            base = lower[:-len("_analytic.tif")]
            cloud = _find(base + "_cloud.tif")
            visual = _find(base + "_visual.tif")

        elif lower.endswith("_toa_0.tif"):
            base = lower[:-len("_toa_0.tif")]
            cloud = _find(base + "_cloud_0.tif")
            visual = _find(base + "_visual_0.tif")

        group = [image]

        if cloud:
            group.append(cloud)

        if visual:
            group.append(visual)

        groups.append(group)

    return groups


def main():
    parser = argparse.ArgumentParser(description="Process Satellogic imagery")

    parser.add_argument(
        "--list_dates",
        action="store_true",
        help=(
            "Report the Satellogic scenes available in the vendor bucket for the "
            "given --level, newest first by S3 delivery time, then exit without "
            "processing. Use it to discover which --date values exist; each "
            "printed date can be passed straight back as --date. Ignores "
            "--date/--product (but --level is still required)."
        ),
    )

    parser.add_argument(
        "--product",
        choices=["truecolor", "colorir", "ndvi", "ndwi", "evi"],
        help="Product to generate",
    )

    parser.add_argument("--date", help="Target datetime (YYYY-MM-DD HH:MM:SS) or (YYYY-MM-DD HH:MM:SS,YYYY-MM-DD HH:MM:SS,etc) for multiple")
    parser.add_argument("--level", required=True, help="Processing level (e.g. L1D, L1B)")
    parser.add_argument("--output", default="/tmp/s3_temp")

    parser.add_argument(
        "--visualize",
        action="store_true",
        help="Apply normalization + gamma correction for RGB products only",
    )

    parser.add_argument(
        "--gamma",
        type=float,
        default=0.7,
        help="Gamma correction for RGB products (default 0.7)",
    )

    parser.add_argument(
        "--filter_size",
        type=int,
        choices=[3, 5, 7],
        default=5,
        help="Lee-filter window applied to indices (NDVI/NDWI/EVI). Odd only: 3, 5, or 7.",
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
        # Bucket/prefix are hardcoded in report_satellogic_scenes; mirror them
        # here only so a read failure reports the right location to the operator.
        sat_bucket, sat_prefix = "csda-data-vendor-satellogic", "disasters"
        try:
            scenes = report_satellogic_scenes(args.level)
        except (ClientError, BotoCoreError) as e:
            msg = explain_s3_read_failure(e, sat_bucket, sat_prefix)
            print(msg or f"Failed to list s3://{sat_bucket}/{sat_prefix}: {e}",
                  file=sys.stderr)
            sys.exit(2)
        print(
            f"{len(scenes)} available Satellogic {args.level} scene(s) in "
            f"s3://csda-data-vendor-satellogic/disasters -- most recently added "
            f"to S3 first (top = closest to today). Copy a --date value to "
            f"process:\n"
        )
        # Aligned table; scene folder LAST so the fixed-width columns stay
        # aligned regardless of the (long) Satellogic scene name.
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
        csv_path = os.path.join(args.output, "available_satellogic_dates.csv")
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
                f"\nNo {args.level} scenes found at "
                f"s3://{sat_bucket}/{sat_prefix} (read access OK). Check the "
                "level, or the vendor may not have delivered any scenes at this "
                "level yet.",
                file=sys.stderr,
            )
        return

    # --date / --product are optional above so --list_dates can run without
    # them; enforce them here for the normal processing path.
    missing = [n for n, v in (("--date", args.date), ("--product", args.product)) if not v]
    if missing:
        parser.error("the following arguments are required: " + ", ".join(missing))

    activation_metadata = load_metadata_json(args.metadata_json)

    os.makedirs(args.output, exist_ok=True)

    for datestring in args.date.split(","):
    
        print(f"Retrieving Satellogic resources for {datestring}...")
    
        metadata, tifs = retrieve_satellogic_resources(datestring, args.level)
    
        scene_groups = group_satellogic_tifs(tifs)
    
        print(f"Generating {args.product}...")
        print(f"Found {len(scene_groups)} Satellogic scene/tile groups to process")
    
        outfiles = []
    
        for i, scene_tifs in enumerate(scene_groups, start=1):
            print(f"\nProcessing scene/tile {i}/{len(scene_groups)}")
    
            outfile = None
    
            if args.product == "truecolor":
                nodata_setting = 0
                outfile = genTrueColor(
                    scene_tifs,
                    metadata,
                    args.output,
                    visualize=args.visualize,
                    gamma=args.gamma,
                )
    
            elif args.product == "colorir":
                nodata_setting = 0
                outfile = gencolorIR(
                    scene_tifs,
                    metadata,
                    args.output,
                    visualize=args.visualize,
                    gamma=args.gamma,
                )
    
            elif args.product == "ndvi":
                nodata_setting = -9999
                outfile = genNDVI(
                    scene_tifs,
                    metadata,
                    args.output,
                    filter_size=args.filter_size,
                )

            elif args.product == "ndwi":
                nodata_setting = -9999
                outfile = genNDWI(
                    scene_tifs,
                    metadata,
                    args.output,
                    filter_size=args.filter_size,
                )

            elif args.product == "evi":
                nodata_setting = -9999
                outfile = genEVI(
                    scene_tifs,
                    metadata,
                    args.output,
                    filter_size=args.filter_size,
                )
    
            if outfile:
                print("\nConverting to COG...")
    
                cog_path = convert_to_cog(
                    outfile,
                    nodata=nodata_setting,
                    dst_crs=None,
                    compression="ZSTD",
                    compression_level=22,
                    metadata=activation_metadata,
                )
    
                print(f"COG created: {cog_path}")
                outfiles.append(cog_path)
    
        print(f"\nFinished {args.product}. Created {len(outfiles)} COG(s).")


if __name__ == "__main__":
    main()