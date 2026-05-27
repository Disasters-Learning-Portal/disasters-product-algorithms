#!/usr/bin/env python
"""summarize.py — print min/max/mean/nodata-count for a GeoTIFF."""

import argparse
import json
import sys

from shared_utils import summarize_raster


def main():
    parser = argparse.ArgumentParser(
        description="Print summary statistics for a GeoTIFF band.",
        usage="summarize_raster INPUT [-b BAND] [-n NODATA] [--json]",
    )
    parser.add_argument("input", help="Path to the input GeoTIFF.")
    parser.add_argument("-b", "--band", type=int, default=1,
                        help="1-indexed band number (default: 1).")
    parser.add_argument("-n", "--nodata", type=float, default=None,
                        help="Override nodata value (default: from file).")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON instead of human-readable text.")
    args = parser.parse_args()

    stats = summarize_raster(args.input, band=args.band, nodata=args.nodata)

    if args.json:
        json.dump(stats, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        for k, v in stats.items():
            print(f"{k:>14}: {v}")


if __name__ == "__main__":
    main()
