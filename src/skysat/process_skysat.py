"""
process_skysat.py

Process Planet SkySat imagery for disaster activations.
"""

import argparse
import os

from skysat.skysat_v2 import (
    LEVEL_TOKEN,
    retrieve_skysat_resources,
    calc_ndvi,
    calc_evi,
    calc_ndwi,
    produce_truecolor,
    produce_colorir,
)

from shared_utils.cog_utils import convert_to_cog
from shared_utils.cog_metadata import load_metadata_json


# COG settings
COMPRESSION = "ZSTD"
COMPRESSION_LEVEL = 9
# Native projection, no warp -- same as the other vendor sensors. Left unset,
# convert_to_cog would fall back to its library default of EPSG:3857.
DST_CRS = None
SOURCE = "Planet SkySat"


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
        default="/tmp/s3_temp",
        help="Output directory",
    )

    parser.add_argument(
        "--gamma",
        type=float,
        default=1.0,
        help="Gamma correction for color composites",
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

    if args.product in ["truecolor", "colorir"] and args.product_type is None:
        parser.error("--product-type is required for truecolor and colorir")

    # Set NoData based on product type
    if args.product in ["ndvi", "ndwi", "evi"]:
        nodata = -9999
    else:
        nodata = None

    # setdefault, never assignment: a caller-supplied SOURCE must survive. Only
    # touched when metadata was asked for -- creating a dict here would switch
    # convert_to_cog onto its in-process cog_translate backend for every run.
    activation_metadata = load_metadata_json(args.metadata_json)
    if activation_metadata is not None:
        activation_metadata.setdefault("SOURCE", SOURCE)
        # Indices always read the analytic asset; composites read --product-type.
        asset = "analytic" if args.product in ["ndvi", "ndwi", "evi"] else args.product_type
        activation_metadata.setdefault("PROCESSING_LEVEL", LEVEL_TOKEN[asset])

    os.makedirs(args.output, exist_ok=True)

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
                dst_crs=DST_CRS,
                compression=COMPRESSION,
                compression_level=COMPRESSION_LEVEL,
                metadata=activation_metadata,
            )

            print(f"COG created: {cog_path}")


if __name__ == "__main__":
    main()