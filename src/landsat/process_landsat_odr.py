"""
process_landsat_odr.py

Command-line interface for processing Landsat Collection 2 Level-2
imagery sourced from a STAC API (Earth Search, `landsat-c2-l2`).

This script handles:
    - Landsat STAC searches
    - Algorithm selection
    - Index generation
    - Composite generation
    - Scene merging, one mosaic per satellite pass (optional)
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
import tempfile

from landsat.landsat_odr_functions import (
    apply_cloud_union,
    cloud_mask_sidecar_path,
    _build_output_filename,
    generate_composite,
    generate_index,
    get_algorithm,
    group_by_acquisition,
    merge_products,
    search_landsat,
    finalize_composite_rgba,
    CLOUD_GRAY,
    DEFAULT_ALGORITHM_FILE,
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

# No data setting
INDEX_NODATA = -9999
COMPOSITE_NODATA = False

# Scratch directory a run builds its products in, inside --output.
WORK_DIR_PREFIX = "_work_"


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
            "Mosaic the scenes of each satellite pass into one product "
            "per pass, instead of writing one file per scene. A date "
            "range covering several passes gives several mosaics -- "
            "passes are never mixed."
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
            "Local directory the finished COGs are written to "
            "(default: /tmp/s3_temp). Intermediates are built in a "
            "scratch subdirectory and removed."
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

    if args.product in INDEX_PRODUCTS:
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

    # Two passes that would share one mosaic name: refuse before any work.
    if args.merge:
        _refuse_ambiguous_mosaics(items, args.product, args.cloud_mask)

    # -----------------------------------------------------------------
    # Generate, merge, convert
    # -----------------------------------------------------------------

    # Everything is built in a scratch directory inside --output, and the
    # COGs are moved out of it under their product names only once the
    # whole run has succeeded. A file in --output under a product name is
    # therefore always a finished, tagged COG -- after an exception, a
    # SIGTERM or an OOM kill alike. That matters because the caller (a
    # notebook upload cell, a DPS finalize step) publishes by filename
    # and cannot tell a half-made intermediate from a product. It also
    # means a failed re-run leaves an earlier run's COGs as they were.
    os.makedirs(args.output, exist_ok=True)
    work_dir = tempfile.mkdtemp(prefix=WORK_DIR_PREFIX, dir=args.output)

    try:
        cog_paths = _produce_cogs(
            args,
            items,
            algorithm,
            algorithm_type,
            nodata,
            metadata,
            work_dir,
        )

    finally:
        # Takes the per-scene cloud-mask sidecars with it (they live in
        # work_dir/_cloud_masks). A run that is killed outright leaves
        # this directory behind; it is safe to delete.
        shutil.rmtree(work_dir, ignore_errors=True)

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


def _refuse_ambiguous_mosaics(items, product, masked):
    """
    Stop if --merge would write two satellite passes to one file.

    A merged name holds the satellite, the WRS-2 path and the date. That
    is unique everywhere except near the poles, where one path number is
    flown twice on a UTC day, one orbit apart (seen on real searches over
    Svalbard and northern Greenland). The second mosaic would replace the
    first without a word, so the run is refused instead.
    """

    by_name = {}

    for scenes in group_by_acquisition(items):
        name = _build_output_filename(
            scenes[0], product, masked=masked, merged=True
        )
        by_name.setdefault(name, []).append(scenes)

    clashes = [
        f"  {name}: {len(passes)} passes, starting "
        + ", ".join(
            scenes[0].datetime.strftime("%H:%M:%SZ") for scenes in passes
        )
        for name, passes in sorted(by_name.items())
        if len(passes) > 1
    ]

    if clashes:
        raise ValueError(
            "--merge would write more than one satellite pass to the "
            "same file:\n" + "\n".join(clashes) + "\n"
            "A merged name holds the satellite, WRS-2 path and date, and "
            "near the poles one path is flown twice on a UTC day. Run "
            "without --merge, or narrow --bbox or the dates to one pass."
        )


def _produce_cogs(
    args,
    items,
    algorithm,
    algorithm_type,
    nodata,
    metadata,
    work_dir,
):
    """
    Generate one product GeoTIFF per scene, mosaic per pass if requested,
    convert the results to COGs, and move them into --output. Returns the
    COG paths.

    Everything is written in `work_dir` until the last COG is finished;
    see the note in main().
    """

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
                        output_dir=work_dir,
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
                        output_dir=work_dir,
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

        # One mosaic per (variant, pass). Grouping by PASS keeps every
        # date in the requested range as its own product: the merge is
        # first-wins, so mixing passes would stitch pre-event pixels into
        # a post-event mosaic wherever scenes overlap. Grouping by variant
        # keeps different parameterisations of one product apart.
        variants = {}

        for variant, item, path in local_paths:
            variants.setdefault(variant, []).append((item, path))

        files_to_cog = []

        for variant, group in variants.items():

            path_of = {item.id: path for item, path in group}

            for scenes in group_by_acquisition([item for item, _ in group]):

                paths = [path_of[scene.id] for scene in scenes]

                # scenes[0] is the earliest scene of the pass, so the
                # merged name carries the pass's own date and path.
                merged_name = _build_output_filename(
                    scenes[0],
                    args.product,
                    masked=args.cloud_mask,
                    merged=True,
                    variant=variant,
                )
                merged_path = os.path.join(work_dir, merged_name)

                print()
                print("=" * 70)
                print(
                    f"Merging {len(scenes)} scene(s) of one pass into "
                    f"{merged_name}"
                    + (f" [{variant}]" if variant else "")
                )
                print("=" * 70)

                # Within one pass, overlapping scenes are the same
                # observation, so first-wins loses nothing.
                merge_products(
                    paths,
                    merged_path,
                    priority_value=None,
                )

                sidecars = (
                    [cloud_mask_sidecar_path(p) for p in paths]
                    if args.cloud_mask
                    else None
                )

                # Keep every scene's cloud mask in the mosaic. An index
                # stores cloud as plain nodata, which looks like off-swath
                # nodata, so "first" lets a neighbouring scene's valid
                # pixels fill the hole; the per-scene masks are unioned
                # back in. A composite gets the same union painted gray.
                if algorithm_type == "index":
                    if sidecars:
                        apply_cloud_union(merged_path, sidecars)
                else:
                    finalize_composite_rgba(
                        merged_path,
                        mask_paths=sidecars,
                        gray=CLOUD_GRAY,
                    )

                print(f"Merged product created: {merged_path}")

                # Remove the pre-merge per-scene GeoTIFFs now that the
                # mosaic exists; only the merged file proceeds to COG
                # conversion.
                for path in paths:
                    if path != merged_path and os.path.exists(path):
                        os.remove(path)

                files_to_cog.append(merged_path)

    else:
        files_to_cog = [path for _, _, path in local_paths]

        # Gray clouds and a transparent no-data edge, per scene.
        if algorithm_type == "composite":
            for outfile in files_to_cog:
                finalize_composite_rgba(
                    outfile,
                    mask_paths=(
                        [cloud_mask_sidecar_path(outfile)]
                        if args.cloud_mask
                        else None
                    ),
                    gray=CLOUD_GRAY,
                )

    # ---------------------------------------------------------------------
    # Convert to COG(s)
    # ---------------------------------------------------------------------

    finished = []

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

        print(f"COG created: {os.path.basename(cog_path)}")

        finished.append(cog_path)

    # Every COG of this run exists: hand them over under their names.
    cog_paths = []

    for cog_path in finished:
        final_path = os.path.join(args.output, os.path.basename(cog_path))
        os.replace(cog_path, final_path)
        cog_paths.append(final_path)

    return cog_paths


if __name__ == "__main__":
    main()