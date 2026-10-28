"""
process_landsat_odr.py

Command-line interface for processing Landsat Collection 2 Level-2
imagery sourced from a STAC API (Earth Search, `landsat-c2-l2`).

This script handles:
    - Landsat STAC searches
    - Algorithm selection
    - Index generation
    - Composite generation
    - Tile merging (optional)
    - COG creation

It writes COGs to a local directory only. Publishing to S3 is the
caller's job (a DPS finalize step or an operator notebook) -- this CLI
never uploads.

The actual processing functions live in
landsat.landsat_odr_functions.

Landsat C2 L2 is surface reflectance only, so unlike the Sentinel-2 CLI
there is no --level option and --cloud-mask is always available (it uses
the QA_PIXEL band that ships with every scene).
"""

import argparse
import os
import shutil

from landsat.landsat_odr_functions import (
    apply_cloud_union,
    cloud_mask_sidecar_path,
    _build_output_filename,
    generate_composite,
    generate_index,
    get_algorithm,
    merge_products,
    search_landsat,
    finalize_composite_rgba,
    finalize_cloud_mask,
    CLOUD_GRAY,
    LANDSAT_COLLECTION,
    LANDSAT_STAC_API,
)

from shared_utils.cog_utils import convert_to_cog
from shared_utils.cog_metadata import load_metadata_json


# ---------------------------------------------------------------------
# Fixed Landsat processing parameters
# ---------------------------------------------------------------------

COMPRESSION = "ZSTD"

# Same trade-off as the Sentinel-2 CLI (throughput over ratio, since an
# activation fans out over many scenes). Set this to match the Landsat
# DPS run script if it differs.
COMPRESSION_LEVEL = 9

# Keep the native Landsat projection (UTM).
DST_CRS = None

# The algorithm catalog ships inside this package, so resolve it
# relative to this file.
DEFAULT_ALGORITHM_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "algorithms-landsat.json",
)

# No data setting
INDEX_NODATA = -9999
COMPOSITE_NODATA = False


# ---------------------------------------------------------------------
# Landsat product categories
# ---------------------------------------------------------------------

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
        description="Process Landsat imagery"
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
        help="Landsat product to generate",
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
            "Maximum allowed scene cloud cover percentage "
            "(default: 50)"
        ),
    )

    parser.add_argument(
        "--cloud-mask",
        action="store_true",
        help=(
            "Mask cloud, cloud-shadow, cirrus and dilated-cloud pixels "
            "using the Landsat QA_PIXEL band."
        ),
    )

    parser.add_argument(
        "--merge",
        action="store_true",
        help=(
            "Mosaic all matched Landsat scenes into a single output "
            "product instead of writing one file per scene."
        ),
    )

    # -----------------------------------------------------------------
    # STAC configuration
    # -----------------------------------------------------------------

    parser.add_argument(
        "--stac-api",
        default=LANDSAT_STAC_API,
        help=(
            "STAC API endpoint "
            "(default: Earth Search)"
        ),
    )

    # -----------------------------------------------------------------
    # Algorithm configuration
    # -----------------------------------------------------------------

    parser.add_argument(
        "--algorithm-file",
        default=DEFAULT_ALGORITHM_FILE,
        help=(
            "Path to Landsat algorithm JSON file "
            "(default: the algorithms-landsat.json shipped in this "
            "package)"
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

    collection_id = LANDSAT_COLLECTION

    # -----------------------------------------------------------------
    # Validate the bounding box up front.
    #
    # A transposed or degenerate bbox otherwise fails deep inside the
    # STAC search as an empty result, which reads as "no imagery for
    # this date" rather than "your bbox is wrong".
    # -----------------------------------------------------------------

    min_lon, min_lat, max_lon, max_lat = args.bbox

    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise ValueError(
            f"--bbox longitudes must be within [-180, 180]; got "
            f"{min_lon} and {max_lon}."
        )

    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise ValueError(
            f"--bbox latitudes must be within [-90, 90]; got "
            f"{min_lat} and {max_lat}."
        )

    if min_lon >= max_lon or min_lat >= max_lat:
        raise ValueError(
            f"--bbox must be MIN_LON MIN_LAT MAX_LON MAX_LAT with "
            f"min < max; got {args.bbox}. (A transposed bbox silently "
            f"returns zero scenes.)"
        )

    # -----------------------------------------------------------------
    # Determine product type
    # -----------------------------------------------------------------

    elif args.product in INDEX_PRODUCTS:
        algorithm_type = "index"
        nodata = INDEX_NODATA

    elif args.product in COMPOSITE_PRODUCTS:
        algorithm_type = "composite"
        nodata = COMPOSITE_NODATA

    else:
        raise ValueError(
            f"Unsupported Landsat product: {args.product}"
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
    print("Landsat Processing")
    print("=" * 70)

    print(f"Product:        {args.product}")
    print(f"Product type:   {algorithm_type}")
    print(f"Start date:     {args.start_date}")
    print(f"End date:       {args.end_date}")
    print(f"Bounding box:   {args.bbox}")
    print(f"Cloud cover:    {args.cloud_cover}%")
    print(f"STAC API:       {args.stac_api}")
    print(f"STAC collection:  {collection_id}")
    print(f"Algorithm file: {args.algorithm_file}")
    print(f"Output:         {args.output}")
    print(f"NoData:         {nodata}")
    print(f"Compression:    {COMPRESSION}")
    print(f"Compression lvl: {COMPRESSION_LEVEL}")
    print(f"Cloud mask:     {args.cloud_mask}")
    print(f"Merge scenes:   {args.merge}")

    print()

    # -----------------------------------------------------------------
    # Load algorithm
    # -----------------------------------------------------------------

    print("Loading Landsat algorithm...")

    algorithm = get_algorithm(
        algorithm_type=algorithm_type,
        algorithm_name=args.product,
        algorithm_file=args.algorithm_file,
    )

    print(
        f"Algorithm '{args.product}' loaded successfully."
    )

    # -----------------------------------------------------------------
    # Search Landsat
    # -----------------------------------------------------------------

    print()
    print("Searching for Landsat imagery...")

    items = search_landsat(
        start_date=args.start_date,
        end_date=args.end_date,
        bbox=args.bbox,
        cloud_cover=args.cloud_cover,
        stac_api_url=args.stac_api,
        collection_id=collection_id,
    )

    if not items:
        raise FileNotFoundError(
            "No Landsat scenes found for the requested "
            "date range, bounding box, and cloud-cover threshold."
        )

    print(
        f"Found {len(items)} Landsat scene(s)."
    )

    # -----------------------------------------------------------------
    # Generate product GeoTIFF(s), one per scene
    # -----------------------------------------------------------------

    # Each entry is (variant, item, outfile). `variant` is a filename
    # token distinguishing several parameterisations of the same product
    # on the same scene, or None.
    local_paths = []

    for i, item in enumerate(items, start=1):

        print()
        print("=" * 70)
        print(
            f"Generating scene {i}/{len(items)}: "
            f"{item.id}"
        )
        print("=" * 70)

        if algorithm_type == "index":

            produced = [
                (
                    None,
                    generate_index(
                        item=item,
                        algorithm=algorithm,
                        algorithm_name=args.product,
                        output_dir=args.output,
                        nodata=nodata,
                        cloud_mask=args.cloud_mask,
                    ),
                )
            ]

        else:

            produced = [
                (
                    None,
                    generate_composite(
                        item=item,
                        algorithm=algorithm,
                        algorithm_name=args.product,
                        output_dir=args.output,
                        cloud_mask=args.cloud_mask,
                    ),
                )
            ]

        for variant, outfile in produced:

            if not outfile:
                print(
                    "No output was generated for this scene."
                )
                continue

            print(f"Product GeoTIFF created: {outfile}")

            local_paths.append((variant, item, outfile))

    if not local_paths:
        raise RuntimeError(
            "No product GeoTIFFs were generated for any scene."
        )

    # -----------------------------------------------------------------
    # Merge, if requested
    # -----------------------------------------------------------------

    if args.merge:

        # Group by variant so each parameterisation mosaics separately.
        variants = {}

        for variant, item, path in local_paths:
            variants.setdefault(variant, []).append((item, path))

        files_to_cog = []

        for variant, group in variants.items():

            print()
            print("=" * 70)
            print(
                f"Merging {len(group)} scene(s)"
                + (f" [{variant}]" if variant else "")
            )
            print("=" * 70)

            # Sort by item id for a deterministic merge order and a
            # deterministic choice of "first" item for the merged
            # filename. Landsat ids start with SAT then PATHROW, so
            # "first" means lowest satellite id, then lowest path/row --
            # arbitrary with respect to image quality.
            items_sorted, paths_sorted = zip(
                *sorted(group, key=lambda pair: pair[0].id)
            )

            merged_name = _build_output_filename(
                items_sorted[0],
                args.product,
                masked=args.cloud_mask,
                merged=True,
                variant=variant,
            )
            merged_path = os.path.join(args.output, merged_name)

            # Keep every scene's cloud mask in the mosaic.
            #
            # Indices and composites store cloud as plain nodata, which
            # looks like off-swath nodata, so plain "first" would let
            # another scene's valid pixels fill the hole. For those, the
            # per-scene cloud masks are unioned after the merge instead.
            merge_products(
                list(paths_sorted),
                merged_path,
                priority_value=None,
            )

            if args.cloud_mask:
                apply_cloud_union(
                    merged_path,
                    [cloud_mask_sidecar_path(p) for p in paths_sorted],
                )

            print(f"Merged product created: {merged_path}")

            # Remove the pre-merge per-scene GeoTIFFs now that the
            # mosaic exists; only the merged file proceeds to COG
            # conversion.
            for path in paths_sorted:
                if path != merged_path and os.path.exists(path):
                    os.remove(path)

            files_to_cog.append(merged_path)

    else:
        files_to_cog = [path for _, _, path in local_paths]

    # ---------------------------------------------------------------------
    # Finalize cloud display and transparency before COG conversion
    # ---------------------------------------------------------------------
    
    if algorithm_type == "composite":
        for outfile in files_to_cog:
            mask_paths = None
    
            if args.cloud_mask:
                if args.merge:
                    # The merged cloud union was already applied above.
                    # Use the merged cloud-mask sidecars if available.
                    mask_paths = [
                        cloud_mask_sidecar_path(p)
                        for _, _, p in local_paths
                    ]
                else:
                    mask_paths = [cloud_mask_sidecar_path(outfile)]
    
            finalize_composite_rgba(
                outfile,
                mask_paths=mask_paths,
                gray=CLOUD_GRAY,
            )
    
    # Per-scene cloud-mask sidecars are no longer needed.
    shutil.rmtree(
        os.path.join(args.output, "_cloud_masks"),
        ignore_errors=True,
    )
    
    # ---------------------------------------------------------------------
    # Convert to COG(s)
    # ---------------------------------------------------------------------

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
    print("Landsat processing complete")
    print("=" * 70)

    print(
        f"Created {len(cog_paths)} COG(s)."
    )

    for cog_path in cog_paths:
        print(f"  {cog_path}")

    print()


if __name__ == "__main__":
    main()