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
    groups = []

    image_files = [
        x for x in tifs
        if (
            x.lower().endswith("_analytic.tif")
            or x.lower().endswith("_toa_0.tif")
        )
    ]

    for image in image_files:
        lower = image.lower()

        if lower.endswith("_analytic.tif"):
            base = image[:-len("_analytic.tif")]
            cloud = base + "_cloud.tif"
            visual = base + "_visual.tif"

        elif lower.endswith("_toa_0.tif"):
            base = image[:-len("_TOA_0.tif")]
            cloud = base + "_CLOUD_0.tif"
            visual = base + "_VISUAL_0.tif"

        group = [image]

        if cloud in tifs:
            group.append(cloud)

        if visual in tifs:
            group.append(visual)

        groups.append(group)

    return groups


def main():
    parser = argparse.ArgumentParser(description="Process Satellogic imagery")

    parser.add_argument(
        "--product",
        required=True,
        choices=["truecolor", "colorir", "ndvi", "ndwi", "evi"],
        help="Product to generate",
    )

    parser.add_argument("--date", required=True, help="Target datetime (YYYY-MM-DD HH:MM:SS)")
    parser.add_argument("--level", required=True, help="Processing level (e.g. L1D, L1B)")
    parser.add_argument("--output", default="/tmp/s3_temp")

    parser.add_argument("--use_mask", action="store_true", help="Apply cloud mask")

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

    parser.add_argument("-nodata", type=float, default=None)
    parser.add_argument("-compression", type=str, default="ZSTD")
    parser.add_argument("-compression_level", type=int, default=22)
    parser.add_argument(
        "-dst_crs",
        type=str,
        default="native",
        help=(
            "Target CRS for COG output. 'native' (default) preserves the "
            "source projection; pass 'EPSG:3857' for Web Mercator "
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

    dst_crs_value = None if args.dst_crs.lower() == "native" else args.dst_crs
    activation_metadata = load_metadata_json(args.metadata_json)

    os.makedirs(args.output, exist_ok=True)

    print("Retrieving Satellogic resources...")

    metadata, tifs = retrieve_satellogic_resources(args.date, args.level)

    scene_groups = group_satellogic_tifs(tifs)

    print(f"Generating {args.product}...")
    print(f"Found {len(scene_groups)} Satellogic scene/tile groups to process")

    outfiles = []

    for i, scene_tifs in enumerate(scene_groups, start=1):
        print(f"\nProcessing scene/tile {i}/{len(scene_groups)}")

        outfile = None

        if args.product == "truecolor":
            outfile = genTrueColor(
                scene_tifs,
                metadata,
                args.output,
                use_mask=False,
                visualize=args.visualize,
                gamma=args.gamma,
            )

        elif args.product == "colorir":
            outfile = gencolorIR(
                scene_tifs,
                metadata,
                args.output,
                use_mask=False,
                visualize=args.visualize,
                gamma=args.gamma,
            )

        elif args.product == "ndvi":
            outfile = genNDVI(
                scene_tifs,
                metadata,
                args.output,
                use_mask=args.use_mask,
            )

        elif args.product == "ndwi":
            outfile = genNDWI(
                scene_tifs,
                metadata,
                args.output,
                use_mask=args.use_mask,
            )

        elif args.product == "evi":
            outfile = genEVI(
                scene_tifs,
                metadata,
                args.output,
                use_mask=args.use_mask,
            )

        if outfile:
            print("\nConverting to COG...")

            cog_path = convert_to_cog(
                outfile,
                nodata=args.nodata,
                dst_crs=dst_crs_value,
                compression=args.compression,
                compression_level=args.compression_level,
                metadata=activation_metadata,
            )

            print(f"COG created: {cog_path}")
            outfiles.append(cog_path)

    print(f"\nFinished {args.product}. Created {len(outfiles)} COG(s).")


if __name__ == "__main__":
    main()