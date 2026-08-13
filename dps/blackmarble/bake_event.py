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

`preserve_compression=True`, `web_optimized=False` and `target_crs=None` are all
load-bearing: the in-memory/bytes path of `create_cog_with_metadata` defaults
`web_optimized=True`, which would silently reproject the product to EPSG:3857. We want
the raster untouched -- only the tags change.
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


def iter_tifs(out_home):
    """Every .tif under out_home, case-insensitively (CSDA-style .TIF never appears
    here, but the predicate matches the repo-wide convention)."""
    for root, _dirs, files in os.walk(out_home):
        for name in sorted(files):
            if name.lower().endswith('.tif'):
                yield os.path.join(root, name)


def bake(path, event, platform=DEFAULT_PLATFORM):
    """Re-create one COG with the activation-event tags embedded at creation."""
    try:
        tags = PLATFORMS[platform]
    except KeyError:
        raise ValueError(
            f'unknown platform {platform!r}; expected one of '
            f'{", ".join(sorted(PLATFORMS))}'
        ) from None

    metadata = resolve_metadata(
        os.path.basename(path),
        mode='manual',
        manual_metadata={
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
        target_crs=None,
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
