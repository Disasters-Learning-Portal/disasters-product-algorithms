"""
process_sentinel2.py

Command-line interface for processing Sentinel-2 imagery.

This script handles:
    - Sentinel-2 STAC searches
    - Algorithm selection
    - Index generation
    - Composite generation
    - Tile merging (optional)
    - COG creation
    - S3 upload

The actual Sentinel-2 processing functions are contained in
sentinel2.sentinel2_functions.
"""

import argparse
import os

from sentinel2.sentinel2_functions import (
    search_sentinel2,
    get_algorithm,
    generate_index,
    generate_composite,
    generate_water_extent,
    merge_products,
    _build_output_filename,
)

from shared_utils.cog_utils import convert_to_cog
from shared_utils.cog_metadata import load_metadata_json


# ---------------------------------------------------------------------
# Fixed Sentinel-2 processing parameters
# ---------------------------------------------------------------------

SENTINEL2_BUCKET = "nasa-disasters"
SENTINEL2_PREFIX = "drcs_activations_new"

COMPRESSION = "ZSTD"
COMPRESSION_LEVEL = 1

# Keep the native Sentinel-2 projection.
DST_CRS = None

# Default Sentinel-2 STAC collection.
SENTINEL2_COLLECTION_L1C = "sentinel-2-l1c"
SENTINEL2_COLLECTION_L2A = "sentinel-2-c1-l2a"

# Default STAC API.
SENTINEL2_STAC_API = (
    "https://earth-search.aws.element84.com/v1"
)

# No data setting
INDEX_NODATA = -9999
WATER_NODATA = 0
COMPOSITE_NODATA = False


# ---------------------------------------------------------------------
# Sentinel-2 product categories
# ---------------------------------------------------------------------

WATER_EXTENT_PRODUCTS = {
    "we",
}


INDEX_PRODUCTS = {
    "ndvi",
    "ndwi",
    "mndwi",
    "nbr",
    "evi",
}

COMPOSITE_PRODUCTS = {
    "true_color",
    "natural_color",
    "color_infrared",
    "swir",
}


def main():

    parser = argparse.ArgumentParser(
        description="Process Sentinel-2 imagery"
    )

    # -----------------------------------------------------------------
    # Product
    # -----------------------------------------------------------------

    parser.add_argument(
        "--product",
        required=True,
        choices=[
            "ndvi",
            "ndwi",
            "mndwi",
            "nbr",
            "evi",
            "we",
            "true_color",
            "natural_color",
            "color_infrared",
            "swir",
        ],
        help="Sentinel-2 product to generate",
    )

    # -----------------------------------------------------------------
    # Date / spatial search
    # -----------------------------------------------------------------

    parser.add_argument(
        "--start-date",
        required=True,
        help="Search start date (YYYY-MM-DD)",
    )

    parser.add_argument(
        "--end-date",
        required=True,
        help="Search end date (YYYY-MM-DD)",
    )

    parser.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        required=True,
        metavar=(
            "MIN_LON",
            "MIN_LAT",
            "MAX_LON",
            "MAX_LAT",
        ),
        help=(
            "Bounding box in the form: "
            "MIN_LON MIN_LAT MAX_LON MAX_LAT"
        ),
    )

    parser.add_argument(
        "--cloud-cover",
        type=float,
        default=50,
        help=(
            "Maximum allowed cloud cover percentage "
            "(default: 50)"
        ),
    )

    parser.add_argument(
        "--cloud-mask",
        action="store_true",
        help=(
            "Mask cloud, cloud-shadow, and thin-cirrus pixels using the "
            "Sentinel-2 L2A Scene Classification Layer (SCL). Only valid "
            "with --level 2; L1C has no SCL asset."
        ),
    )

    parser.add_argument(
        "--merge",
        action="store_true",
        help=(
            "Mosaic all matched Sentinel-2 tiles into a single output "
            "product instead of writing one file per tile."
        ),
    )

    # -----------------------------------------------------------------
    # STAC configuration
    # -----------------------------------------------------------------

    parser.add_argument(
        "--stac-api",
        default=SENTINEL2_STAC_API,
        help=(
            "STAC API endpoint "
            "(default: Earth Search)"
        ),
    )

    parser.add_argument(
        "--level",
        required=True,
        choices=["1", "2"],
        help=(
            "Sentinel-2 processing level: "
            "1 = Level-1C, 2 = Level-2A"
        ),
    )

    # -----------------------------------------------------------------
    # Algorithm configuration
    # -----------------------------------------------------------------

    parser.add_argument(
        "--algorithm-file",
        default="/home/jovyan/disasters-product-algorithms/src/sentinel2/algorithms-sentinel2.json",
        help=(
            "Path to Sentinel-2 algorithm JSON file "
            "(default: algorithms-sentinel2.json)"
        ),
    )

    # -----------------------------------------------------------------
    # Local output
    # -----------------------------------------------------------------

    parser.add_argument(
        "--output",
        default="/tmp/s3_temp",
        help=(
            "Local directory for intermediate GeoTIFFs "
            "and COGs (default: /tmp/s3_temp)"
        ),
    )

    # -----------------------------------------------------------------
    # Metadata
    # -----------------------------------------------------------------

    parser.add_argument(
        "--metadata-json",
        type=str,
        default=None,
        help=(
            "Path to a JSON file containing activation-event "
            "metadata to embed as GeoTIFF tags on the output COG."
        ),
    )

    args = parser.parse_args()

    if args.level == "1":
        collection_id = SENTINEL2_COLLECTION_L1C
    
    elif args.level == "2":
        collection_id = SENTINEL2_COLLECTION_L2A
    
    else:
        raise ValueError(
            f"Unsupported Sentinel-2 level: {args.level}"
        )

    if args.cloud_mask and args.level == "1":
        raise ValueError(
            "--cloud-mask requires Sentinel-2 L2A (--level 2); the Scene "
            "Classification Layer (SCL) needed for cloud masking does not "
            "exist for L1C products."
        )

    # -----------------------------------------------------------------
    # Determine product type
    # -----------------------------------------------------------------

    if args.product in WATER_EXTENT_PRODUCTS:
        algorithm_type = "water_extent"
        nodata = WATER_NODATA
    
    elif args.product in INDEX_PRODUCTS:
        algorithm_type = "index"
        nodata = INDEX_NODATA

    elif args.product in COMPOSITE_PRODUCTS:
        algorithm_type = "composite"
        nodata = COMPOSITE_NODATA

    else:
        raise ValueError(
            f"Unsupported Sentinel-2 product: {args.product}"
        )

    # -----------------------------------------------------------------
    # Load metadata
    # -----------------------------------------------------------------

    metadata = load_metadata_json(
        args.metadata_json
    )

    # -----------------------------------------------------------------
    # Print configuration
    # -----------------------------------------------------------------

    print()
    print("=" * 70)
    print("Sentinel-2 Processing")
    print("=" * 70)

    print(f"Product:        {args.product}")
    print(f"Product type:   {algorithm_type}")
    print(f"Start date:     {args.start_date}")
    print(f"End date:       {args.end_date}")
    print(f"Bounding box:   {args.bbox}")
    print(f"Cloud cover:    {args.cloud_cover}%")
    print(f"STAC API:       {args.stac_api}")
    print(f"Processing level: {args.level}")
    print(f"STAC collection:  {collection_id}")
    print(f"Algorithm file: {args.algorithm_file}")
    print(f"Output:         {args.output}")
    print(f"NoData:         {nodata}")
    print(f"S3 bucket:      {SENTINEL2_BUCKET}")
    print(f"S3 prefix:      {SENTINEL2_PREFIX}")
    print(f"Compression:    {COMPRESSION}")
    print(f"Compression lvl: {COMPRESSION_LEVEL}")
    print(f"Cloud mask:     {args.cloud_mask}")
    print(f"Merge tiles:    {args.merge}")
    print()

    # -----------------------------------------------------------------
    # Load algorithm
    # -----------------------------------------------------------------

    print("Loading Sentinel-2 algorithm...")

    algorithm = get_algorithm(
        algorithm_type=algorithm_type,
        algorithm_name=args.product,
        algorithm_file=args.algorithm_file,
    )

    print(
        f"Algorithm '{args.product}' loaded successfully."
    )

    # -----------------------------------------------------------------
    # Search Sentinel-2
    # -----------------------------------------------------------------

    print()
    print("Searching for Sentinel-2 imagery...")

    items = search_sentinel2(
        start_date=args.start_date,
        end_date=args.end_date,
        bbox=args.bbox,
        cloud_cover=args.cloud_cover,
        stac_api_url=args.stac_api,
        collection_id=collection_id,
    )

    if not items:
        raise FileNotFoundError(
            "No Sentinel-2 scenes found for the requested "
            "date range, bounding box, and cloud-cover threshold."
        )

    print(
        f"Found {len(items)} Sentinel-2 scene(s)."
    )

    # -----------------------------------------------------------------
    # Generate product GeoTIFF(s), one per scene
    # -----------------------------------------------------------------

    local_paths = []

    for i, item in enumerate(items, start=1):

        print()
        print("=" * 70)
        print(
            f"Generating scene {i}/{len(items)}: "
            f"{item.id}"
        )
        print("=" * 70)

        if algorithm_type == "water_extent":

            outfile = generate_water_extent(
                item=item,
                algorithm=algorithm,
                algorithm_name=args.product,
                output_dir=args.output,
                cloud_mask=args.cloud_mask,
            )

        elif algorithm_type == "index":

            outfile = generate_index(
                item=item,
                algorithm=algorithm,
                algorithm_name=args.product,
                output_dir=args.output,
                nodata=nodata,
                cloud_mask=args.cloud_mask,
            )


        else:

            outfile = generate_composite(
                item=item,
                algorithm=algorithm,
                algorithm_name=args.product,
                output_dir=args.output,
                cloud_mask=args.cloud_mask,
            )

        if not outfile:
            print(
                "No output was generated for this scene."
            )
            continue

        print(f"Product GeoTIFF created: {outfile}")

        local_paths.append((item, outfile))

    if not local_paths:
        raise RuntimeError(
            "No product GeoTIFFs were generated for any scene."
        )

    # -----------------------------------------------------------------
    # Merge, if requested
    # -----------------------------------------------------------------

    if args.merge:

        print()
        print("=" * 70)
        print(f"Merging {len(local_paths)} tile(s)")
        print("=" * 70)

        # Sort by item id for a deterministic merge order and a
        # deterministic choice of "first" item for the merged filename.
        items_sorted, paths_sorted = zip(
            *sorted(local_paths, key=lambda pair: pair[0].id)
        )

        merged_name = _build_output_filename(
            items_sorted[0],
            args.product,
            masked=args.cloud_mask,
            merged=True,
        )
        merged_path = os.path.join(args.output, merged_name)

        merge_products(list(paths_sorted), merged_path)

        print(f"Merged product created: {merged_path}")

        # Remove the pre-merge per-tile GeoTIFFs now that the mosaic
        # exists; only the merged file proceeds to COG conversion.
        for _, path in local_paths:
            if path != merged_path and os.path.exists(path):
                os.remove(path)

        files_to_cog = [merged_path]

    else:
        files_to_cog = [path for _, path in local_paths]

    # -----------------------------------------------------------------
    # Convert to COG(s)
    # -----------------------------------------------------------------

    cog_paths = []

    for outfile in files_to_cog:

        print("\nConverting to COG...")

        cog_path = convert_to_cog(
            outfile,
            nodata=nodata,
            dst_crs=DST_CRS,
            compression=COMPRESSION,
            compression_level=COMPRESSION_LEVEL,
            metadata=metadata,
        )

        if not cog_path:
            raise RuntimeError(
                f"COG conversion failed for {outfile}"
            )

        print(f"COG created: {cog_path}")

        cog_paths.append(cog_path)

        # ---------------------------------------------------------
        # Cleanup intermediate GeoTIFF
        # ---------------------------------------------------------

        if (
            os.path.exists(outfile)
            and outfile != cog_path
        ):
            os.remove(outfile)

            print(
                f"Removed source GeoTIFF: {outfile}"
            )

    # -----------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------

    print()
    print("=" * 70)
    print("Sentinel-2 processing complete")
    print("=" * 70)

    print(
        f"Created {len(cog_paths)} COG(s)."
    )

    for cog_path in cog_paths:
        print(f"  {cog_path}")

    print()


if __name__ == "__main__":
    main()