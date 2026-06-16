"""
process_satellogic.py

Name: Ethan Kerr
Date: April 2026
"""

import argparse
from satellogic.satellogic_v2 import (
    retrieve_satellogic_resources,
    genTrueColor,
    genNDVI,
    genNDWI,
    gencolorIR,
)
from shared_utils.cog_utils import convert_to_cog


def main():
    parser = argparse.ArgumentParser(description="Process Satellogic imagery")

    parser.add_argument(
        "--product",
        required=True,
        choices=["truecolor", "ndvi", "ndwi", "colorir"],
        help="Product to generate"
    )

    parser.add_argument(
        "--date",
        required=True,
        help="Target date (YYYY-MM-DD HH:MM:SS)"
    )

    parser.add_argument(
        "--level",
        required=True,
        help="Processing level (e.g. L1)"
    )

    parser.add_argument(
        "--output",
        default="./s3_temp",
        help="Output directory"
    )

    parser.add_argument('-nodata', type=float, default=None, help='No-data value for COG outputs (auto-detected if not specified).')
    parser.add_argument('-compression', type=str, default='ZSTD', help='Compression type for COG (default: ZSTD).')
    parser.add_argument('-compression_level', type=int, default=22, help='Compression level for COG (default: 22 for ZSTD).')

    args = parser.parse_args()

    print("Retrieving Satellogic resources...")
    metadata, tifs = retrieve_satellogic_resources(args.date, args.level)

    print(f"Generating {args.product}...")

    outfile = None
    
    if args.product == "truecolor":
        outfile = genTrueColor(tifs, metadata, args.output)
    
    elif args.product == "ndvi":
        outfile = genNDVI(tifs, metadata, args.output)
    
    elif args.product == "ndwi":
        outfile = genNDWI(tifs, metadata, args.output)
    
    elif args.product == "colorir":
        outfile = gencolorIR(tifs, metadata, args.output)
    
    if outfile:
        print("\nConverting to COG...")
    
        cog_path = convert_to_cog(
            outfile,
            nodata=args.nodata,
            compression=args.compression,
            compression_level=args.compression_level
        )
    
        print(f"COG created: {cog_path}")


if __name__ == "__main__":
    main()