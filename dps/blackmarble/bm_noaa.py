#!/usr/bin/env python
"""Run the UPSTREAM Black Marble CLI against NOAA-20 (VJ146A2) instead of Suomi-NPP.

WHY THIS EXISTS
---------------
Suomi-NPP data product delivery ceases 2026-11-01, taking VNP46A2 -- the VIIRS
nighttime-lights product the Black Marble pipeline runs on -- with it
(disasters-portal#365). The supplemental NOAA-20 / JPSS-1 product VJ146A2 is not
affected, and is a structural twin: same CMR version (2), same LAADS provider, same
h5v/h5h tiling, same HDF-EOS5 grid path, same Gap_Filled_DNB_BRDF-Corrected_NTL layer,
same -999.9 fill. Retargeting is therefore a product-name swap and nothing else.

Upstream (github.com/NASA-IMPACT/veda-black-marble, pip-installed by dps/environment.yml)
HARDCODES the product and exposes no way to change it:

    blackmarble/acquire/viirs.py:17   BM_SHORT_NAME = "VNP46A2"
    blackmarble/pipeline.py:476       "product": "VNP46A2"   (inside the COG metadata dict)

There is no --product flag, no PipelineConfig field, and no env var. So this module is the
entry point the NOAA-20 DPS job runs INSTEAD of the `blackmarble` console script: it sets
the product, then hands off to upstream's own typer app, unmodified. Every CLI option,
default, and behavior (including the hardcoded multiplicative NTL enhancement) is upstream's.

WHY A PATCH RATHER THAN A FORK
------------------------------
CLAUDE.md is explicit that Black Marble must NOT be re-vendored -- it used to live at
src/blackmarble/ as a copy of a personal fork and was removed precisely so Disasters is not
maintaining a private copy that drifts. This keeps the upstream SHA pin intact. When
NASA-IMPACT adds a real product option, delete this module and pass their flag.

`download_viirs` reads BM_SHORT_NAME as a MODULE GLOBAL at call time (it takes no product
argument), and pipeline.py reaches create_metadata through the `export` package object
(`from . import export` ... `export.create_metadata(...)`), so both patch points are
reachable from here. Both were confirmed by reading the pinned upstream source.

THE CONTRACT GUARD IS THE POINT
-------------------------------
If upstream renames BM_SHORT_NAME, moves the layer path, or restructures the module, the
naive version of this patch would silently do NOTHING -- the job would download Suomi-NPP
granules, write them under a NOAA-20 product name, and exit 0. That is the exact failure
this file is built to prevent: apply_noaa20_patch() validates upstream's shape first and
raises. `--self-check` runs that validation with no network and no credentials, and is
wired into dps/Dockerfile's build-time smoke gate, so the drift fails an IMAGE BUILD
rather than a live activation.
"""

import sys

# --- the product we retargeted to, and the exact upstream shape we verified against ----
# (short_name, version) is ONE fact, not two: VJ146A2's current collection is version "2",
# the same as VNP46A2's, verified live against CMR
# (collections.umm_json?short_name=VJ146A2 -> C3370789118-LAADS, Version 2). Setting the
# version explicitly rather than inheriting upstream's means a future upstream bump to a
# VNP46A2-only version can't silently make every NOAA-20 search return zero granules.
VIIRS_SHORT_NAME = 'VJ146A2'
VIIRS_VERSION = '2'
VIIRS_SENSOR = 'VIIRS (NOAA-20)'

# What upstream is expected to hold BEFORE we patch it.
UPSTREAM_SHORT_NAME = 'VNP46A2'
UPSTREAM_NTL_DATASET_PATH = (
    'HDFEOS/GRIDS/VIIRS_Grid_DNB_2d/Data_Fields/Gap_Filled_DNB_BRDF-Corrected_NTL'
)

# Marker attribute set on our create_metadata wrapper so applying the patch twice does not
# nest wrappers (run.sh calls main() once, but the test suite and --self-check do not).
_PATCH_MARKER = '_bm_noaa20_wrapped'


class UpstreamContractError(RuntimeError):
    """Upstream blackmarble no longer has the shape this patch depends on.

    Raised INSTEAD of silently failing to retarget -- a no-op patch would download
    Suomi-NPP data and publish it under a NOAA-20 product name.
    """


def _require(condition, message):
    if not condition:
        raise UpstreamContractError(
            f'{message}\n'
            f'  dps/blackmarble/bm_noaa.py patches the pinned upstream blackmarble '
            f'(dps/environment.yml) to download {VIIRS_SHORT_NAME} instead of '
            f'{UPSTREAM_SHORT_NAME}. Upstream changed shape, so the patch can no longer be '
            f'applied safely. Re-check github.com/NASA-IMPACT/veda-black-marble against this '
            f'module -- and if a real product option now exists upstream, delete this shim '
            f'and pass their flag.'
        )


def _wrap_create_metadata(bm_export):
    """Rewrite the VIIRS entry upstream stamps into the COG's `data_sources` tag.

    pipeline.py hardcodes `"product": "VNP46A2"` in a dict literal, so unlike BM_SHORT_NAME
    it cannot be reassigned -- the value is baked at the call site. Wrapping
    create_metadata (which pipeline.py reaches as `export.create_metadata`, an attribute
    lookup on the package object at call time) is the one place it can be corrected before
    it reaches rasterio's update_tags. Without this the NOAA-20 COG would carry a
    data_sources tag naming VNP46A2 -- misleading provenance on a product that is not it.
    """
    original = getattr(bm_export, 'create_metadata', None)
    _require(
        callable(original),
        'blackmarble.export.create_metadata is missing or not callable.',
    )
    if getattr(original, _PATCH_MARKER, False):
        return original  # already wrapped; do not nest

    def create_metadata(data_sources=None, *args, **kwargs):
        if isinstance(data_sources, dict) and isinstance(data_sources.get('viirs'), dict):
            # Copy rather than mutate: the caller keeps its own dict, and a shallow copy of
            # the one nested entry we touch is enough (we replace two scalar values).
            data_sources = dict(data_sources)
            viirs_entry = dict(data_sources['viirs'])
            viirs_entry['product'] = VIIRS_SHORT_NAME
            viirs_entry['sensor'] = VIIRS_SENSOR
            data_sources['viirs'] = viirs_entry
        return original(data_sources, *args, **kwargs)

    setattr(create_metadata, _PATCH_MARKER, True)
    create_metadata.__wrapped__ = original
    bm_export.create_metadata = create_metadata
    return create_metadata


def apply_noaa20_patch():
    """Point the pinned upstream pipeline at VJ146A2. Raises if upstream drifted.

    Returns the (short_name, version) pair now in effect, so callers can assert on it.
    """
    import blackmarble.acquire.viirs as viirs
    import blackmarble.export as bm_export

    current = getattr(viirs, 'BM_SHORT_NAME', None)
    _require(
        current is not None,
        'blackmarble.acquire.viirs.BM_SHORT_NAME no longer exists.',
    )
    _require(
        current in (UPSTREAM_SHORT_NAME, VIIRS_SHORT_NAME),
        f'blackmarble.acquire.viirs.BM_SHORT_NAME is {current!r}, expected '
        f'{UPSTREAM_SHORT_NAME!r} (unpatched) or {VIIRS_SHORT_NAME!r} (already patched).',
    )
    _require(
        hasattr(viirs, 'BM_VERSION'),
        'blackmarble.acquire.viirs.BM_VERSION no longer exists.',
    )
    # VJ146A2 shares VNP46A2's HDF-EOS5 layer path, which is the whole reason a product
    # swap is sufficient. If upstream reads a different layer, that assumption is void.
    _require(
        getattr(viirs, 'NTL_DATASET_PATH', None) == UPSTREAM_NTL_DATASET_PATH,
        f'blackmarble.acquire.viirs.NTL_DATASET_PATH is '
        f'{getattr(viirs, "NTL_DATASET_PATH", None)!r}, expected '
        f'{UPSTREAM_NTL_DATASET_PATH!r}.',
    )

    viirs.BM_SHORT_NAME = VIIRS_SHORT_NAME
    viirs.BM_VERSION = VIIRS_VERSION
    _wrap_create_metadata(bm_export)

    return viirs.BM_SHORT_NAME, viirs.BM_VERSION


def self_check():
    """Apply the patch and assert it took, offline. Exit code is the result.

    Run by dps/Dockerfile's build-time smoke gate so upstream drift fails the image build
    instead of a live DPS job. Needs no Earthdata token and makes no network call.
    """
    try:
        short_name, version = apply_noaa20_patch()
    except UpstreamContractError as exc:
        print(f'ERROR: Black Marble NOAA-20 patch cannot be applied:\n{exc}', file=sys.stderr)
        return 1
    except ImportError as exc:
        print(f'ERROR: upstream blackmarble is not installed: {exc}', file=sys.stderr)
        return 1

    import blackmarble.export as bm_export

    if short_name != VIIRS_SHORT_NAME or version != VIIRS_VERSION:
        print(
            f'ERROR: patch applied but product is {short_name} v{version}, '
            f'expected {VIIRS_SHORT_NAME} v{VIIRS_VERSION}',
            file=sys.stderr,
        )
        return 1
    if not getattr(bm_export.create_metadata, _PATCH_MARKER, False):
        print('ERROR: patch applied but create_metadata was not wrapped', file=sys.stderr)
        return 1

    print(f'OK: Black Marble retargeted to {short_name} (version {version}, {VIIRS_SENSOR})')
    return 0


def main(argv=None):
    """Patch, then run upstream's own CLI with the arguments it was given.

    `--self-check` is consumed here, before typer ever sees argv, so it cannot collide with
    an upstream option.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--self-check' in argv:
        return self_check()

    # The Landsat georeferencing fix is platform-independent -- it applies to Suomi-NPP and
    # NOAA-20 alike -- but NOAA-20 enters through THIS module rather than bm_georef.py, so
    # apply it here too. Both patches are idempotent, and they touch disjoint upstream
    # symbols (viirs.BM_SHORT_NAME / export.create_metadata vs prepare.landsat.reproject),
    # so the order does not matter.
    #
    # dps/ is not an installed package, so import bm_georef by its directory rather than by
    # package path. run.sh invokes this file as a script (which already puts its directory on
    # sys.path[0]), but the test suite loads it via importlib from an arbitrary cwd.
    import os

    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)
    from bm_georef import apply_georef_patch

    apply_georef_patch()
    apply_noaa20_patch()

    from blackmarble.cli import app

    # typer reads sys.argv itself; hand it exactly what we were given, minus nothing.
    sys.argv = [sys.argv[0]] + argv
    app()
    return 0


if __name__ == '__main__':
    sys.exit(main())
