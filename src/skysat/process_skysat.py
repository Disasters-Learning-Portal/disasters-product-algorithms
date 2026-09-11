"""
process_skysat.py

Process Planet SkySat imagery for disaster activations.
"""

import argparse

from skysat.skysat_v2 import (
    retrieve_skysat_resources,
    calc_ndvi,
    calc_evi,
    calc_ndwi,
    produce_truecolor,
    produce_colorir,
)

from shared_utils.cog_utils import convert_to_cog


# COG settings
COMPRESSION = "ZSTD"
COMPRESSION_LEVEL = 9


def main():
    parser = argparse.ArgumentParser(
        description="Process SkySat imagery"
    )

    parser.add_argument(
        "--product",
        required=True,
        choices=[
            "truecolor",
            "colorir",
            "ndvi",
            "ndwi",
            "evi",
        ],
        help="Product to generate",
    )

    parser.add_argument(
        "--product-type",
        required=False,
        choices=["visual", "analytic", "basic_analytic"],
        help="SkySat image type: visual, analytic, or basic_analytic",
    )

    parser.add_argument(
        "--date",
        required=True,
        help="Target date (YYYY-MM-DD HH:MM:SS)",
    )

    parser.add_argument(
        "--output",
        default="./s3_temp",
        help="Output directory",
    )

    parser.add_argument(
        "--gamma",
        type=float,
        default=1.0,
        help="Gamma correction for color composites",
    )

    args = parser.parse_args()

    if args.product in ["truecolor", "colorir"] and args.product_type is None:
        parser.error("--product-type is required for truecolor and colorir")

    # Set NoData based on product type
    if args.product in ["ndvi", "ndwi", "evi"]:
        nodata = -9999
    else:
        nodata = None

    print("Retrieving SkySat resources...")

    tifs = retrieve_skysat_resources(
        args.date
    )

    print(f"Generating {args.product}...")

    output_files = []

    if args.product == "truecolor":
        output_files = produce_truecolor(
            tifs,
            args.product_type,
            args.output,
            gamma=args.gamma,
        )

    elif args.product == "colorir":
        output_files = produce_colorir(
            tifs,
            args.product_type,
            args.output,
            gamma=args.gamma,
        )

    elif args.product == "ndvi":
        output_files = calc_ndvi(
            tifs,
            args.output,
        )

    elif args.product == "ndwi":
        output_files = calc_ndwi(
            tifs,
            args.output,
        )

    elif args.product == "evi":
        output_files = calc_evi(
            tifs,
            args.output,
        )

    # Make sure a single output is also handled correctly.
    if output_files:
        if isinstance(output_files, str):
            output_files = [output_files]

        print("\nConverting outputs to COG...")

        for outfile in output_files:
            cog_path = convert_to_cog(
                outfile,
                nodata=nodata,
                compression=COMPRESSION,
                compression_level=COMPRESSION_LEVEL,
            )

            print(f"COG created: {cog_path}")


if __name__ == "__main__":
    main()