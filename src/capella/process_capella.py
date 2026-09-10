"""
process_capella.py

CLI processing for Capella SAR products
"""

import argparse
import os

from capella.capella_v2 import (
    CAPELLA_NODATA,
    retrieve_capella_resources,
    group_capella_scenes,
    sigmaCalib
)

from shared_utils.cog_utils import convert_to_cog
from shared_utils.cog_metadata import load_metadata_json


# Fixed processing parameters. Capella has exactly one calibration product and
# one vendor bucket, and every activation wants the same COG encoding, so these
# are constants rather than CLI flags -- the same treatment Satellogic got in
# PR #45 and Umbra's filter in PR #44. Changing one is a code change with a
# review, not a per-run argument. See .clinerules.md rule 37.
CAPELLA_BUCKET = "csdap-capellaspace-delivery"
CAPELLA_PREFIX = "disasters"
SOURCE = "CSDA"
COMPRESSION = "ZSTD"
COMPRESSION_LEVEL = 22
DST_CRS = None  # native projection; no warp


def main():

    parser = argparse.ArgumentParser(
        description="Process Capella imagery"
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
        "--output",
        default="/tmp/s3_temp",
        help="Output directory"
    )

    # COG options
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

    metadata = load_metadata_json(args.metadata_json)
    # Fill in the vendor as the default provenance, but never clobber a
    # SOURCE the operator supplied via --metadata-json (DPS passes
    # source_label through, and it is a required job input).
    metadata.setdefault("SOURCE", SOURCE)

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
            f"in s3://{CAPELLA_BUCKET}/{CAPELLA_PREFIX}"
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

        # Speckle filtering is always on; --filter_size only tunes the kernel.
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
            nodata=CAPELLA_NODATA,
            dst_crs=DST_CRS,
            compression=COMPRESSION,
            compression_level=COMPRESSION_LEVEL,
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