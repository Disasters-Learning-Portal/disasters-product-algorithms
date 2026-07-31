#!/usr/bin/env python
"""Vendor scene-date discovery for the MAAP ``list-dates`` DPS algorithm.

Lists the scenes available in a fetch/vendor sensor's CSDA S3 bucket, newest
first by S3 delivery time, prints an aligned table to stdout, and writes a
sortable ``available_<sensor>_dates.csv`` into the output dir.

This is the single home for the discovery report. The per-sensor
``process_<sensor>`` CLIs no longer carry a ``--list_dates`` flag -- the
``list-dates`` algorithm's ``run.sh`` invokes this module directly. It reuses
each sensor's ``report_<sensor>_scenes()`` helper (in ``<sensor>_v2``), which
does the S3 grouping/sorting; this module owns only the CLI + presentation.

The sensor's geospatial module is imported lazily (only the chosen sensor's
stack loads), so ``--sensor umbra`` never pays to import capella/satellogic.
"""
import argparse
import csv
import os
import sys

from botocore.exceptions import BotoCoreError, ClientError

from shared_utils.s3utils import explain_s3_read_failure


def _write_report(scenes, *, label, location, date_width, csv_path):
    """Print the aligned scene table to stdout and write the CSV artifact."""
    print(
        f"{len(scenes)} available {label} scene(s) in {location} -- most "
        f"recently added to S3 first (top = closest to today). Copy a --date "
        f"value to process:\n"
    )
    # Aligned table; scene folder LAST so the fixed-width columns stay aligned
    # regardless of the (long) vendor scene name.
    print(
        f"  {'--date':<{date_width}}{'acquired (UTC)':<22}"
        f"{'added to S3 (UTC)':<22}scene folder"
    )
    for s in scenes:
        print(
            f"  {s['date']:<{date_width}}"
            f"{s['acquired'].strftime('%Y-%m-%d %H:%M:%S'):<22}"
            f"{s['added_to_s3'].strftime('%Y-%m-%d %H:%M:%S'):<22}"
            f"{s['scene']}"
        )

    # A sortable CSV artifact so the report survives outside the raw job log (on
    # DPS it lands in output/ -> browsable via the Jobs panel's "Open in File
    # Browser", rendered as a grid by JupyterLab).
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


def main():
    parser = argparse.ArgumentParser(
        description=(
            "List available vendor scene dates for a sensor (the list-dates DPS "
            "discovery tool). Prints an aligned table + writes a CSV, then exits."
        )
    )
    parser.add_argument(
        "--sensor", required=True, choices=["capella", "umbra", "satellogic"],
        help="Which vendor bucket to list scene dates for.",
    )
    parser.add_argument(
        "--level",
        help="Satellogic processing level (L1D/L1B); required when --sensor satellogic.",
    )
    parser.add_argument(
        "--output", default="/tmp/s3_temp",
        help="Directory for the available_<sensor>_dates.csv artifact.",
    )
    args = parser.parse_args()

    # Per-sensor: vendor location + display label + --date column width + the
    # report callable (lazy-imported). Buckets/prefixes mirror each
    # report_<sensor>_scenes() default so the read-failure message points at the
    # right location.
    date_width = 16 if args.sensor == "capella" else 22
    if args.sensor == "capella":
        from capella import capella_v2
        bucket, prefix, label = "csdap-capellaspace-delivery", "disasters", "Capella"
        fetch = lambda: capella_v2.report_capella_scenes(bucket=bucket, prefix=prefix)
    elif args.sensor == "umbra":
        from umbra import umbra_v2
        bucket, prefix, label = "csda-data-vendor-umbra", "disasters", "Umbra"
        fetch = lambda: umbra_v2.report_umbra_scenes(bucket=bucket, prefix=prefix)
    else:  # satellogic -- discovery is LEVEL-SCOPED
        if not args.level:
            parser.error("--level is required when --sensor satellogic")
        from satellogic import satellogic_v2
        bucket, prefix = "csda-data-vendor-satellogic", "disasters"
        label = f"Satellogic {args.level}"
        fetch = lambda: satellogic_v2.report_satellogic_scenes(args.level)

    location = f"s3://{bucket}/{prefix}"
    os.makedirs(args.output, exist_ok=True)
    csv_path = os.path.join(args.output, f"available_{args.sensor}_dates.csv")

    try:
        scenes = fetch()
    except (ClientError, BotoCoreError) as e:
        msg = explain_s3_read_failure(e, bucket, prefix)
        print(msg or f"Failed to list {location}: {e}", file=sys.stderr)
        sys.exit(2)

    _write_report(scenes, label=label, location=location,
                  date_width=date_width, csv_path=csv_path)

    if not scenes:
        print(
            f"\nNo scenes found at {location} (read access OK). Double-check the "
            "sensor/level, or the vendor may not have delivered any scenes yet.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
