import argparse
import os

from satellogic.satellogic_v2 import (
    retrieve_satellogic_resources,
    genTrueColor,
    gencolorIR,
    genNDVI,
    genNDWI,
    genEVI,
)

from shared_utils.cog_utils import convert_to_cog
from shared_utils.cog_metadata import load_metadata_json


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
        "--product",
        choices=["truecolor", "colorir", "ndvi", "ndwi", "evi"],
        required=True,
        help="Product to generate",
    )

    parser.add_argument("--date", required=True, help="Target datetime (YYYY-MM-DD HH:MM:SS) or (YYYY-MM-DD HH:MM:SS,YYYY-MM-DD HH:MM:SS,etc) for multiple")
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