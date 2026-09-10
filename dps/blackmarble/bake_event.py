#!/usr/bin/env python
"""Bake the activation event into every Black Marble output COG.

WHY THIS EXISTS
---------------
Black Marble is the one DPS job that does not go through
`shared_utils.convert_to_cog`, so it never sees the `--metadata-json` path every
`process_*` sensor uses to embed activation-event tags at COG creation. Upstream writes
its own COG, and its `blackmarble.export.create_metadata()` has no notion of a Disasters
activation event -- it covers data sources, processing steps, bbox, CRS, resolution and
road types only. The result is a product whose event lives solely in the S3 key.

This script closes that gap: it re-creates each output COG with
ACTIVATION_EVENT (plus the YEAR_MONTH / HAZARD / LOCATION split that
`resolve_metadata` derives from it, PROCESSOR, and SOURCE) applied.

WHY IT RE-CREATES RATHER THAN EDITS
-----------------------------------
Baking happens AT COG CREATION, never as a post-step edit. GDAL 3.10+ refuses a
`gdal.Open(path, GA_Update)` + `SetMetadata()` on a COG ("Updating it will generally
result in losing part of the optimizations"), and forcing it with
`IGNORE_COG_LAYOUT_BREAK=YES` yields a file that fails `cog_validate` -- main IFD offset
bloats, overview-IFD ordering inverts. See CLAUDE.md "Critical Constraints"; commit
de80b1a has the empirical validation. `create_cog_with_metadata` therefore rewrites the
COG with the tags supplied at creation time.

Because the COG is re-created here anyway, this is also where the product is put into a
NAMED projection -- see below. `preserve_compression=True` keeps upstream's DEFLATE.

WHY IT REPROJECTS TO EPSG:3857
------------------------------
Upstream writes every product into a per-bbox ad-hoc Albers Equal Area built by
`blackmarble.crs.make_local_albers()` -- a DIFFERENT CRS for every bounding box, with no
EPSG authority code at all (`crs.to_epsg()` returns None; the WKT literally says
`PROJCS["unknown", GEOGCS["unknown", ...]]`). Upstream exposes no way to change it: no CLI
flag, no `PipelineConfig` field, no env var.

An authority-less CRS is what breaks `veda-data-airflow`'s `build_stac`
(`rio_stac.get_dataset_geom`), so it has to be resolved before VEDA ingest. EPSG:3857 is
this repo's answer everywhere else -- it is `cog_utils.convert_to_cog`'s default for exactly
this reason, and it is the optimal projection for titiler-pgstac's WebMercatorQuad tiling.

`web_optimized=False` is STILL load-bearing, and for a subtler reason now that `target_crs`
is set: rio-cogeo's `web_optimized=True` ALSO forces EPSG:3857, but it does so by snapping
the output onto the WebMercatorQuad grid, which changes the resolution and extent. We want
the explicit `target_crs` warp -- the same choice `cog_utils.convert_to_cog` makes by
hardcoding `web_optimized=False` and warping itself.

The `crs` GeoTIFF tag is overwritten too. rio-cogeo copies the source's tags and then
applies `additional_cog_metadata` on top, so without that the product would advertise
upstream's stale `crs=+proj=aea +lon_0=...` string while the raster is actually Web Mercator.

NOTE the separate defect this does NOT fix: upstream misplaces the Landsat mosaic by
~3.6 km to the north-west. That is corrected at the source by dps/blackmarble/bm_georef.py,
before the COG is ever written. Reprojecting a misregistered raster would only have
relocated the error.
"""

import argparse
import os
import sys

from shared_utils import PROCESSOR_STRING
from shared_utils.cog_metadata import create_cog_with_metadata, resolve_metadata

# Mirrors the per-sensor SOURCE constants (cf. capella_v2.SOURCE). Describes what the
# product is made of, not who ran it.
#
# Black Marble runs on one of two VIIRS nighttime-lights products, so SOURCE is per
# platform. Keep these in step with dps/blackmarble/platform.sh's bm_platform_source --
# that bash table is what run.sh prints, this dict is what actually reaches the COG.
# VIIRS_PRODUCT is baked as its own tag so downstream consumers can read the product code
# directly instead of parsing it out of the prose SOURCE string (or out of upstream's
# stringified `data_sources` blob).
# SOURCE carries the PLATFORM TOKEN (`snpp` / `noaa20`) -- the same string as BM_PLATFORM
# and as the `hdnightlightsnoaa20` product folder -- so one grep for `noaa20` finds the
# algorithm, the S3 prefix, the filename and the tag. VIIRS_PRODUCT and VIIRS_PLATFORM
# carry the product code and the satellite's proper name as their own tags, so a consumer
# never has to parse either out of the prose SOURCE string (or out of upstream's
# stringified `data_sources` blob).
PLATFORMS = {
    'snpp': {
        'SOURCE': 'NASA Black Marble VNP46A2 (VIIRS/snpp) + Landsat + OSM',
        'VIIRS_PRODUCT': 'VNP46A2',
        'VIIRS_PLATFORM': 'Suomi-NPP',
    },
    'noaa20': {
        'SOURCE': 'NASA Black Marble VJ146A2 (VIIRS/noaa20) + Landsat + OSM',
        'VIIRS_PRODUCT': 'VJ146A2',
        'VIIRS_PLATFORM': 'NOAA-20',
    },
}

DEFAULT_PLATFORM = 'snpp'

# The projection every Black Marble product is put into on the way out. Hardcoded rather
# than exposed as a job input, matching Satellogic (PR #45) and Capella (PR #76) -- and
# keeping this a CODE-only change, so deploying it needs no algorithm re-registration.
BM_DST_CRS = 'EPSG:3857'


def iter_tifs(out_home):
    """Every .tif under out_home, case-insensitively (CSDA-style .TIF never appears
    here, but the predicate matches the repo-wide convention)."""
    for root, _dirs, files in os.walk(out_home):
        for name in sorted(files):
            if name.lower().endswith('.tif'):
                yield os.path.join(root, name)


def _carry_forward_tags(path, dst_resolution):
    """Upstream's own GeoTIFF tags, adjusted for the reprojection we are about to do.

    These have to be re-supplied EXPLICITLY. When `target_crs` is set,
    `create_cog_with_metadata` wraps the source in a `WarpedVRT` -- and a WarpedVRT carries
    none of the source dataset's tags, so `cog_translate` has nothing to copy and every one
    of upstream's tags is dropped. That would silently discard the product's whole
    provenance record: `data_sources` (every VIIRS + Landsat granule the product was built
    from), `bbox`, `producer`, `processing_steps`, `indices`, `creation_date`.

    Two are rewritten rather than copied, because the warp invalidates them:
      crs        -- upstream wrote the local-Albers proj4 string; the raster is Web Mercator
                    once we are done, so copying it verbatim would make the file describe a
                    projection it is not in.
      resolution -- upstream wrote 30.0 (metres, in Albers). Web Mercator metres are not
                    ground metres: at this latitude the scale factor is 1/cos(lat), so the
                    output pixel is ~38 m. Recomputed from the actual destination transform.

    The projection we warped AWAY from is preserved as SOURCE_CRS. It is not recoverable
    from anything else in the file once the raster is in Web Mercator -- it is a per-bbox
    ad-hoc Albers with no EPSG code, so "the CRS upstream used" is genuinely unique to this
    product and this bounding box. Keeping it means the native-grid product can be
    reconstructed, and that the resampling step is auditable rather than invisible.
    """
    import rasterio

    with rasterio.open(path) as src:
        upstream = dict(src.tags())
        native_crs = src.crs

    if native_crs is not None:
        # WKT, not proj4: the proj4 form is lossy, and upstream's own lowercase `crs` tag
        # already carries a proj4 string for anyone who wants the short version.
        upstream['SOURCE_CRS'] = native_crs.to_wkt()
        upstream['SOURCE_CRS_PROJ4'] = native_crs.to_proj4()
    upstream['crs'] = BM_DST_CRS
    if dst_resolution is not None:
        upstream['resolution'] = f'{dst_resolution:g}'
    else:
        upstream.pop('resolution', None)
    return upstream


def _destination_resolution(path):
    """Pixel size the EPSG:3857 output will have, or None if it cannot be determined.

    Mirrors what `WarpedVRT(src, crs=...)` computes internally, so the `resolution` tag
    describes the file we actually write rather than the grid upstream worked in.
    Pinned by test_bake_resolution_tag_matches_the_raster.
    """
    import rasterio
    from rasterio.warp import calculate_default_transform

    try:
        with rasterio.open(path) as src:
            transform, _width, _height = calculate_default_transform(
                src.crs, BM_DST_CRS, src.width, src.height, *src.bounds
            )
        return abs(transform.a)
    except Exception:
        return None


def bake(path, event, platform=DEFAULT_PLATFORM):
    """Re-create one COG with the activation-event tags embedded at creation."""
    try:
        tags = PLATFORMS[platform]
    except KeyError:
        raise ValueError(
            f'unknown platform {platform!r}; expected one of '
            f'{", ".join(sorted(PLATFORMS))}'
        ) from None

    upstream_tags = _carry_forward_tags(path, _destination_resolution(path))

    metadata = resolve_metadata(
        os.path.basename(path),
        mode='manual',
        manual_metadata={
            # Upstream's provenance first, so our keys win on any collision.
            **upstream_tags,
            'ACTIVATION_EVENT': event,
            'PROCESSOR': PROCESSOR_STRING,
            **tags,
        },
    )
    tmp_path = path + '.baking.tif'
    create_cog_with_metadata(
        path,
        metadata,
        output_path=tmp_path,
        preserve_compression=True,
        target_crs=BM_DST_CRS,
        web_optimized=False,
        quiet=True,
    )
    os.replace(tmp_path, path)
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-home', required=True,
                        help='Directory holding the produced COGs (searched recursively).')
    parser.add_argument('--event', required=True,
                        help='Activation event, e.g. 202511_Flood_TX.')
    parser.add_argument('--platform', default=DEFAULT_PLATFORM,
                        choices=sorted(PLATFORMS),
                        help='VIIRS platform the product was built from: snpp (VNP46A2, '
                             'default) or noaa20 (VJ146A2). Selects the SOURCE / '
                             'VIIRS_PRODUCT tags.')
    args = parser.parse_args()

    tifs = list(iter_tifs(args.out_home))
    if not tifs:
        # run.sh already fails on "no COG produced"; reaching here means the layout
        # changed underneath us, which is worth failing loudly rather than skipping.
        print(f'ERROR: no .tif found under {args.out_home} to bake', file=sys.stderr)
        return 1

    for path in tifs:
        metadata = bake(path, args.event, args.platform)
        print(f'  baked ACTIVATION_EVENT={metadata["ACTIVATION_EVENT"]} '
              f'({metadata["VIIRS_PRODUCT"]}) into '
              f'{os.path.relpath(path, args.out_home)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
