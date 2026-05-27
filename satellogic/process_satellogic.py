import argparse
import os

from satellogic.satellogic_v2 import (
    retrieve_satellogic_resources,
    genTrueColor,
    gencolorIR,
    genNDVI,
    genNDWI,
    genEVI
)

from shared_utils.cog_utils import convert_to_cog


def main():
    parser = argparse.ArgumentParser(description="Process Satellogic imagery")

    # Product selection
    parser.add_argument(
        "--product",
        required=True,
        choices=["truecolor", "colorir", "ndvi", "ndwi", "evi"],
        help="Product to generate"
    )

    # Input controls
    parser.add_argument("--date", required=True, help="Target datetime (YYYY-MM-DD HH:MM:SS)")
    parser.add_argument("--level", required=True, help="Processing level (e.g. L1D, L1B)")
    parser.add_argument("--output", default="./s3_temp")

    # Processing toggles
    parser.add_argument(
        "--use_mask",
        action="store_true",
        help="Apply cloud mask"
    )

    parser.add_argument(
        "--visualize",
        action="store_true",
        help="Apply normalization + gamma correction for RGB products only"
    )

    parser.add_argument(
        "--gamma",
        type=float,
        default=0.7,
        help="Gamma correction for RGB products (default 0.7)"
    )

    # COG options
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

    args = parser.parse_args()

    dst_crs_value = None if args.dst_crs.lower() == "native" else args.dst_crs

    print("Retrieving Satellogic resources...")

    metadata, tifs = retrieve_satellogic_resources(
        args.date,
        args.level
    )

    print(f"Generating {args.product}...")

    outfile = None

    # Product generation
    if args.product == "truecolor":
        outfile = genTrueColor(
            tifs,
            metadata,
            args.output,
            use_mask=args.use_mask,
            visualize=args.visualize,
            gamma=args.gamma
        )

    elif args.product == "colorir":
        outfile = gencolorIR(
            tifs,
            metadata,
            args.output,
            use_mask=args.use_mask,
            visualize=args.visualize,
            gamma=args.gamma
        )

    elif args.product == "ndvi":
        outfile = genNDVI(
            tifs,
            metadata,
            args.output,
            use_mask=args.use_mask
        )

    elif args.product == "ndwi":
        outfile = genNDWI(
            tifs,
            metadata,
            args.output,
            use_mask=args.use_mask
        )

    elif args.product == "evi":
        outfile = genEVI(
            tifs,
            metadata,
            args.output,
            use_mask=args.use_mask
        )

    # COG conversion
    if outfile:
        print("\nConverting to COG...")

        cog_path = convert_to_cog(
            outfile,
            nodata=args.nodata,
            dst_crs=dst_crs_value,
            compression=args.compression,
            compression_level=args.compression_level
        )

        print(f"COG created: {cog_path}")


if __name__ == "__main__":
    main()