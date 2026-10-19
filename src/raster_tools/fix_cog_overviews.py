#!/usr/bin/env python
"""fix_cog_overviews.py -- rebuild an existing COG's overviews with a different
resampling, leaving the full-resolution pixels untouched.

Why this exists
---------------
Categorical rasters in this program were converted with AVERAGE-resampled
overviews. Averaging class codes INVENTS codes that do not exist in the data,
so every zoomed-out map view renders phantom classes. Measured on a live
product, ``OPERA_DSWx_S1_WTR_mosaic_2024-10-11_day.tif`` (16128x21504, Byte):

    native class codes (exact, block-by-block)  : 0, 1, 3, 251, 255
    coarsest AVERAGE overview (1008x1344)       : 256 distinct codes
    class 2, which does not exist in the data   : 1.184% of that overview

2 == (1 + 3) / 2. titiler renders the overview, not the full-resolution band,
so the phantom class is what the portal actually shows. ``mode`` (the most
frequent value in the window) is the only decimation that cannot invent a
code -- see ``docs/RESAMPLING_GUIDE.md``, which already prescribes
``nearest`` + ``mode`` for uint8 categorical data. This tool is the repair
path for the rasters that were written before that was applied.

THE TRADEOFF -- read this before believing the name
---------------------------------------------------
"Rebuild only the overviews" and "still a valid COG" cannot BOTH be satisfied
by an in-place edit. Measured against GDAL 3.12.3:

1. ``gdaladdo -r mode <cog.tif>`` is refused outright::

       ERROR 1: ... has C(loud) O(ptimized) G(eoTIFF) layout. Updating it will
       generally result in losing part of the optimizations ... open the file
       with the IGNORE_COG_LAYOUT_BREAK open option set to YES.

2. With ``-oo IGNORE_COG_LAYOUT_BREAK=YES`` it succeeds and the overviews are
   correct, but the result is no longer a COG. ``LAYOUT=COG`` disappears from
   the Image Structure Metadata and ``rio cogeo validate`` fails with::

       - This file used to have optimizations in its layout, but those have
         been, at least partly, invalidated by later changes
       - The offset of the first block of overview of index 4 should be after
         the one of the overview of index 5
       - The offset of the first block of overview of index 3 should be after
         the one of the overview of index 4

   gdaladdo appends the new overview IFDs and their data at the end of the
   file; COG requires the IFDs up front and the image data ordered coarsest
   overview first. It also leaves the ``OVERVIEW_RESAMPLING`` tag saying
   whatever it said before, so the file then lies about how it was built.

So this tool REWRITES the file through the GDAL COG driver
(``gdal_translate -of COG``), which relays out the whole file. The
consequence, stated plainly rather than papered over:

    * the full-resolution PIXELS are preserved exactly -- verified
      block-by-block with ``numpy.array_equal`` over every block, and by an
      exact ``np.bincount`` histogram before and after;
    * the full-resolution TILES are re-compressed, so the file is not a byte
      copy. On the WTR sample the main IFD's TileByteCounts summed to
      2,320,983 bytes before and 1,716,178 after (same 1344 tiles).

There is no GDAL path that copies compressed tiles verbatim while relaying
out the file, so "overview-only" is true of the DATA, not of the bytes.

Two creation options are load-bearing and easy to get wrong
-----------------------------------------------------------
``OVERVIEWS=IGNORE_EXISTING``
    The COG driver's default is ``AUTO``, which REUSES the source's existing
    overviews. Measured on a 9984x9984 sample: with the default the output
    kept the 4 AVERAGE overviews it came in with and ``OVERVIEW_RESAMPLING``
    was never written; with ``IGNORE_EXISTING`` it recomputed 5 MODE levels.
    Without this flag the tool silently does nothing.

``-mo OVERVIEW_RESAMPLING=`` / ``-mo OVR_RESAMPLING_ALG=``
    GDAL records the real algorithm in the IMAGE_STRUCTURE domain, but our
    older products carry a *default-domain* ``OVERVIEW_RESAMPLING=AVERAGE``
    (and rio-cogeo's ``OVR_RESAMPLING_ALG``) that ``CreateCopy`` faithfully
    copies forward. Left alone those tags outlive the thing they describe and
    the fixed file claims AVERAGE. They are set at creation because GDAL 3.10+
    refuses an in-place metadata update on a COG (``.clinerules.md`` rule 13).

What is preserved, and the one thing that cannot be
---------------------------------------------------
Compression, predictor, interleave, block size, CRS, geotransform, nodata and
all GeoTIFF tags are read off the source and passed back to the driver. The
compression LEVEL is not recoverable -- TIFF does not record it -- so it is
left to the GDAL default unless ``--level`` is given.

Usage
-----
    fix_cog_overviews PATH [PATH ...] [--dry-run]
    fix_cog_overviews PATH -r nearest --level 22
    fix_cog_overviews PATH -o fixed.tif

Overview LEVEL COUNT comes from ``shared_utils.cog_utils.resolve_overview_count``
(``ceil(log2(max(width, height) / 512))``), not from a hardcoded number and not
from rio-cogeo's ``min()``-based default, which under-builds non-square
rasters. Verified to agree with the COG driver's own derivation: on the
16128x21504 WTR sample both produce 6 levels, where the ``min()`` rule gives 5.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import List, Optional

import numpy
import rasterio

from shared_utils.cog_utils import BIGTIFF_FORCE_GB, resolve_overview_count

# The point of the tool: the only decimation that cannot invent a class code.
DEFAULT_RESAMPLING = 'mode'

# Dtypes for which `mode` is the WRONG default -- a float raster's overviews
# are supposed to be averaged (docs/RESAMPLING_GUIDE.md), so applying the
# default to one would be the same class of mistake in reverse. Such a file is
# skipped unless the caller names a resampling explicitly.
_CONTINUOUS_DTYPE_MARKERS = ('float', 'complex')


@dataclass
class OverviewPlan:
    """What ``fix_one`` would do to one file, and why."""

    path: str
    width: int
    height: int
    band_count: int
    dtype: str
    blocksize: int
    compression: Optional[str]
    predictor: Optional[str]
    interleave: Optional[str]
    current_levels: int
    target_levels: int
    current_resampling: Optional[str]
    target_resampling: str
    level: Optional[int]
    skip_reason: Optional[str] = None

    @property
    def raw_size_gb(self) -> float:
        """Uncompressed size of the full-resolution bands, in GB."""
        itemsize = numpy.dtype(self.dtype).itemsize
        return self.width * self.height * self.band_count * itemsize / 1e9

    @property
    def needs_change(self) -> bool:
        """True when the file's overviews do not already match the target.

        An unknown current resampling counts as "needs change": nothing in the
        file proves the overviews are right, and rebuilding is cheap relative
        to shipping phantom classes.
        """
        if self.skip_reason:
            return False
        if self.current_levels != self.target_levels:
            return True
        return self.current_resampling != self.target_resampling.upper()


def _current_resampling(image_structure: dict, tags: dict) -> Optional[str]:
    """Best available record of how the existing overviews were built.

    IMAGE_STRUCTURE wins: the COG driver writes it at creation from the
    creation option actually used. The default-domain tags are consulted only
    as a fallback because they are copied forward by ``CreateCopy`` and go
    stale (our WTR sample carries ``OVERVIEW_RESAMPLING=AVERAGE`` next to
    ``OVR_RESAMPLING_ALG=NEAREST`` -- they cannot both be true).
    """
    for value in (
        image_structure.get('OVERVIEW_RESAMPLING'),
        tags.get('OVERVIEW_RESAMPLING'),
        tags.get('OVR_RESAMPLING_ALG'),
    ):
        if value:
            return str(value).upper()
    return None


def plan_fix(
    path: str,
    resampling: Optional[str] = None,
    overview_count: Optional[int] = None,
    level: Optional[int] = None,
) -> OverviewPlan:
    """Inspect one raster and decide what its overviews should become.

    Args:
        path: Raster to inspect. Reads the header only.
        resampling: GDAL overview resampling name. ``None`` means the
            ``mode`` default, which is NOT applied to continuous data.
        overview_count: Force a level count instead of deriving it.
        level: Compression level to write with (see module docstring).

    Returns:
        OverviewPlan. ``needs_change`` is False when the file already matches,
        and ``skip_reason`` is set when the tool declines to guess.
    """
    with rasterio.open(path) as src:
        image_structure = src.tags(ns='IMAGE_STRUCTURE')
        tags = src.tags()
        blocksize = src.block_shapes[0][0]
        dtype = src.dtypes[0]
        width, height, count = src.width, src.height, src.count
        current_levels = len(src.overviews(1))

    explicit = resampling is not None
    target = (resampling or DEFAULT_RESAMPLING)

    skip_reason = None
    if not explicit and any(m in dtype for m in _CONTINUOUS_DTYPE_MARKERS):
        skip_reason = (
            f'{dtype} is continuous; `{DEFAULT_RESAMPLING}` is a categorical '
            f'default. Pass --resampling explicitly to act on this file.'
        )

    return OverviewPlan(
        path=path,
        width=width,
        height=height,
        band_count=count,
        dtype=dtype,
        blocksize=blocksize,
        compression=image_structure.get('COMPRESSION'),
        predictor=image_structure.get('PREDICTOR'),
        interleave=image_structure.get('INTERLEAVE'),
        current_levels=current_levels,
        target_levels=resolve_overview_count(width, height, overview_count),
        current_resampling=_current_resampling(image_structure, tags),
        target_resampling=target,
        level=level,
        skip_reason=skip_reason,
    )


def build_command(plan: OverviewPlan, output_path: str) -> List[str]:
    """The ``gdal_translate -of COG`` invocation that realises ``plan``.

    Every option here either preserves something read off the source or is
    justified in the module docstring. Notably absent: ``TILING_SCHEME`` --
    our products are already on the WebMercatorQuad grid and passing it would
    re-grid them, which is a pixel change, not an overview change.
    """
    cmd = [
        'gdal_translate',
        '-of', 'COG',
        '-co', 'NUM_THREADS=ALL_CPUS',
        # Same rule as cog_utils.build_creation_options: IF_SAFER is a
        # heuristic, so force BigTIFF above the shared raw-size threshold.
        '-co', ('BIGTIFF=YES' if plan.raw_size_gb > BIGTIFF_FORCE_GB
                else 'BIGTIFF=IF_SAFER'),
        '-co', f'BLOCKSIZE={plan.blocksize}',
        # Load-bearing: the AUTO default reuses the overviews we are here to
        # replace.
        '-co', 'OVERVIEWS=IGNORE_EXISTING',
        '-co', f'OVERVIEW_RESAMPLING={plan.target_resampling.upper()}',
        '-co', f'OVERVIEW_COUNT={plan.target_levels}',
        # Stop the copied-forward tags describing overviews that no longer
        # exist. Set at creation -- GDAL 3.10+ refuses an in-place COG update.
        '-mo', f'OVERVIEW_RESAMPLING={plan.target_resampling.upper()}',
        '-mo', f'OVR_RESAMPLING_ALG={plan.target_resampling.upper()}',
    ]
    if plan.compression:
        cmd.extend(['-co', f'COMPRESS={plan.compression}'])
    if plan.level is not None:
        cmd.extend(['-co', f'LEVEL={plan.level}'])
    if plan.predictor:
        cmd.extend(['-co', f'PREDICTOR={plan.predictor}'])
    if plan.interleave:
        cmd.extend(['-co', f'INTERLEAVE={plan.interleave}'])
    cmd.extend([plan.path, output_path])
    return cmd


def fix_one(
    plan: OverviewPlan,
    output_path: Optional[str] = None,
    dry_run: bool = False,
    quiet: bool = False,
) -> bool:
    """Rebuild one file's overviews. Never destroys the input on failure.

    The COG driver writes to a temporary file alongside the destination and
    the result is moved into place with ``os.replace`` (atomic within a
    filesystem). A failed or interrupted run leaves the input exactly as it
    was.

    Args:
        plan: From ``plan_fix``.
        output_path: Write here instead of replacing ``plan.path``.
        dry_run: Report only; touch nothing.
        quiet: Suppress the per-file report.

    Returns:
        True on success or a legitimate skip, False if the rewrite failed.
    """
    destination = output_path or plan.path
    if not quiet:
        _report(plan, destination)

    if plan.skip_reason or (plan.needs_change is False and output_path is None):
        return True
    if dry_run:
        return True

    parent = os.path.dirname(os.path.abspath(destination)) or '.'
    handle, temp_path = tempfile.mkstemp(suffix='.tif', prefix='.fix_ovr_', dir=parent)
    os.close(handle)
    try:
        started = time.time()
        try:
            result = subprocess.run(
                build_command(plan, temp_path),
                capture_output=True, text=True,
            )
        except FileNotFoundError:
            print('   FAILED: gdal_translate not on PATH')
            return False
        if result.returncode != 0:
            print(f'   FAILED: {result.stderr.strip() or result.stdout.strip()}')
            return False
        before = os.path.getsize(plan.path)
        after = os.path.getsize(temp_path)
        os.replace(temp_path, destination)
        temp_path = None
        if not quiet:
            print(f'   rebuilt in {time.time() - started:.1f}s '
                  f'({before:,} -> {after:,} bytes)')
        return True
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def _report(plan: OverviewPlan, destination: str) -> None:
    """Print the per-file before/after summary."""
    print(os.path.basename(plan.path))
    print(f'   {plan.width} x {plan.height}, {plan.band_count} band {plan.dtype}, '
          f'{plan.compression or "uncompressed"}, {plan.blocksize} px blocks')
    print(f'   overviews  {plan.current_levels} -> {plan.target_levels} levels')
    print(f'   resampling {plan.current_resampling or "unrecorded"} -> '
          f'{plan.target_resampling.upper()}')
    if plan.skip_reason:
        print(f'   SKIP: {plan.skip_reason}')
    elif plan.needs_change:
        print(f'   ACTION: rebuild -> {destination}')
    elif destination != plan.path:
        print(f'   ACTION: already correct, copying -> {destination}')
    else:
        print('   ACTION: none, already correct')


def main():
    parser = argparse.ArgumentParser(
        description='Rebuild a COG\'s overviews with a different resampling, '
                    'preserving the full-resolution pixels.',
        usage='fix_cog_overviews PATH [PATH ...] [-r RESAMPLING] [-o OUTPUT] '
              '[--overview-count N] [--level N] [-n]',
    )
    parser.add_argument('paths', nargs='+', help='COG(s) to fix, in place by default.')
    parser.add_argument('-r', '--resampling', default=None,
                        help=f'GDAL overview resampling (default: '
                             f'{DEFAULT_RESAMPLING}, applied only to '
                             f'non-continuous dtypes).')
    parser.add_argument('-o', '--output', default=None,
                        help='Write here instead of replacing the input. '
                             'Only valid with a single input path.')
    parser.add_argument('--overview-count', type=int, default=None,
                        help='Force a level count (default: derived from the '
                             'raster size).')
    parser.add_argument('--level', type=int, default=None,
                        help='Compression level. The source\'s level is not '
                             'recorded in the file, so it cannot be '
                             'preserved; omitted means the GDAL default.')
    parser.add_argument('-n', '--dry-run', action='store_true',
                        help='Report what would change and write nothing.')
    args = parser.parse_args()

    if args.output and len(args.paths) > 1:
        parser.error('--output takes a single input path.')

    failures = 0
    for path in args.paths:
        plan = plan_fix(path, args.resampling, args.overview_count, args.level)
        if not fix_one(plan, args.output, args.dry_run):
            failures += 1
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
