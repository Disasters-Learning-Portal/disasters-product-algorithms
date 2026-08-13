#!/usr/bin/env python
"""Correct the Landsat mosaic georeferencing in the UPSTREAM Black Marble pipeline.

WHAT IS WRONG
-------------
Every Black Marble product this repo has ever published is misregistered by roughly
3.6 km to the north-west. Measured on the two products in disasters-portal#365
(San Francisco, 2023-06-15, bbox -122.55,37.69,-122.32,37.81), by phase-correlating the
delivered COG against the OSM road network:

    Suomi-NPP  dy = -117 px (-3.51 km)   dx =  -93 px (-2.79 km)
    NOAA-20    dy = -116 px (-3.48 km)   dx =  -91 px (-2.73 km)

The raster FRAME is correct -- the COG's transform is exactly what
`blackmarble.crs.create_processing_grid(bbox, 30)` returns and its WGS84 bounds match the
requested bbox to 5 decimal places. It is the PIXEL CONTENT that sits in the wrong place,
which is why the error survives every downstream check: gdalinfo looks right, cog_validate
passes, and the layer lands over the correct city in a viewer. It is only wrong by a couple
of kilometres, which reads as "the coastline looks a bit off" rather than as a failure.

WHERE IT COMES FROM
-------------------
`blackmarble/prepare/landsat.py`, in the multi-tile band mosaic path (the `reproject` call
at upstream line ~509 inside `process_landsat_date`):

    dst_window_int = round_window_offsets_correctly(dst_window, band_profile["transform"])
    dst_height = int(dst_window_int.height)
    dst_width  = int(dst_window_int.width)
    tile_data_reprojected = np.zeros((dst_height, dst_width), dtype=band_data.dtype)

    reproject(
        source=band_tile_data,
        destination=tile_data_reprojected,        # <- sized to the WINDOW
        src_transform=window_transform,
        src_crs=src.crs,
        dst_transform=band_profile["transform"],  # <- but the FULL-MOSAIC transform
        dst_crs=band_profile["crs"],
        ...
    )
    # ... and then pasted into the mosaic at dst_window_int's offsets, via
    #     calculate_window_offsets(dst_row_off, dst_col_off, ...)

`dst_transform` tells `reproject` where destination pixel (0, 0) IS. Passing the mosaic's
transform makes it render tile content at MOSAIC coordinates into an array whose (0, 0) is
actually the window's top-left corner. The paste step then offsets that array by the window
origin a second time, so the window offset is applied twice.

`MARGIN_PIXELS = 120` (expanding the read window by ~3.6 km on every side "to avoid gaps at
UTM zone boundaries") is what makes the window origin large and negative -- for the SF bbox,
`dst_window_int` is `row_off=-124, col_off=-120`. That is the 3.6 km.

The QA/cloud-mask path 250 lines above, in the same file, does it correctly:

    dst_transform=rasterio.windows.transform(dst_window, qa_profile["transform"])

so the cloud mask and the band data it masks are placed on DIFFERENT grids. Only the band
path is wrong.

THE PATCH
---------
Give the band-path `reproject` the window's transform instead of the mosaic's, matching what
the QA path already does. We use the ROUNDED window (`dst_window_int`), not the float
`dst_window` upstream's QA path uses, because the rounded one is what sizes the destination
array AND what the paste offsets use -- deriving the transform from the float window leaves
a residual sub-pixel shear between the three.

Verified against a synthetic UTM-10N scene with markers at known coordinates (this is what
`self_check()` runs, and what tests/unit/test_blackmarble_landsat_georef.py pins):

    MARGIN=120, as shipped : FerryBldg=LOST  OceanBeach=LOST  TwinPeaks=-3.59E/+3.69N km
    MARGIN=120, patched    : every marker within 30 m
    MARGIN=0,   as shipped : every marker within 80 m   (why "just set MARGIN_PIXELS=0"
                                                         also works -- see below)

WHY NOT JUST SET MARGIN_PIXELS = 0
----------------------------------
It is a one-liner and it does remove the 3.6 km error, because a zero-width margin leaves
the window origin at ~(0, 0) so applying it twice costs nothing. But it works by disabling
the margin, which exists to stop gaps appearing where an AOI spans a UTM zone boundary --
trading a georeferencing bug for a seam bug on wide AOIs. Patching the transform keeps the
margin doing its job. `MARGIN_PIXELS = 0` stays documented here as the emergency fallback if
this patch ever has to be backed out in a hurry.

WHY A PATCH RATHER THAN A FORK
------------------------------
Same reason as bm_noaa.py: CLAUDE.md is explicit that Black Marble must NOT be re-vendored.
It used to live at src/blackmarble/ as a copy of a personal fork and was removed precisely so
Disasters is not maintaining a private copy that drifts from upstream. This keeps the SHA pin
in dps/environment.yml intact. This belongs upstream at NASA-IMPACT/veda-black-marble --
delete this module the moment a fixed release is pinned.

THE CONTRACT GUARD IS THE POINT
-------------------------------
A stale version of this patch fails SILENTLY: it would simply not fire, and the job would
publish a misregistered product and exit 0 -- exactly the defect we are fixing, with a patch
file sitting next to it claiming otherwise. So `apply_georef_patch()` validates upstream's
shape first and raises, and `--self-check` re-runs that validation plus an end-to-end
synthetic placement assertion with no network and no credentials, so drift fails an image
build rather than a live activation.
"""

import inspect
import sys

# Placement tolerance for the synthetic self-check, in metres. The patched path lands
# markers within ~30 m (one 30 m pixel) -- nearest-neighbour resampling of a 5x5 marker
# through two reprojections cannot do better. 200 m gives headroom for PROJ version
# differences while still being ~18x smaller than the 3.6 km defect.
SELF_CHECK_TOLERANCE_M = 200.0

# The exact upstream shape this patch was written against. Each of these is asserted before
# patching; any of them going missing means upstream restructured and this module must be
# re-derived (or deleted, if they fixed it).
UPSTREAM_MARGIN_PIXELS = 120
BUGGY_CALL = 'dst_transform=band_profile["transform"]'
CORRECT_QA_CALL = "dst_transform=rasterio.windows.transform("

_PATCH_MARKER = "_disasters_georef_patched"


def _require(condition, message):
    """Raise on upstream drift. A patch that silently no-ops is worse than no patch."""
    if not condition:
        raise RuntimeError(
            f"blackmarble upstream contract check failed: {message}\n"
            f"dps/blackmarble/bm_georef.py was written against a specific upstream shape "
            f"(see the module docstring). Re-derive the patch against the SHA pinned in "
            f"dps/environment.yml, or delete this module if upstream fixed the bug."
        )


def _validate_upstream(landsat):
    """Assert the pinned upstream still has the defect this module patches."""
    _require(
        hasattr(landsat, "MARGIN_PIXELS"),
        "blackmarble.prepare.landsat.MARGIN_PIXELS is gone",
    )
    _require(
        landsat.MARGIN_PIXELS == UPSTREAM_MARGIN_PIXELS,
        f"MARGIN_PIXELS is {landsat.MARGIN_PIXELS}, expected {UPSTREAM_MARGIN_PIXELS}. "
        f"The offset this patch corrects is exactly this value, so a change here means "
        f"the magnitude of the defect changed",
    )
    for name in ("reproject", "round_window_offsets_correctly", "calculate_window_offsets"):
        _require(
            hasattr(landsat, name),
            f"blackmarble.prepare.landsat.{name} is gone -- the patch points moved",
        )

    src = inspect.getsource(landsat.process_landsat_date)
    _require(
        BUGGY_CALL in src,
        f"the buggy call {BUGGY_CALL!r} is NO LONGER in "
        f"blackmarble.prepare.landsat.process_landsat_date. Upstream most likely FIXED "
        f"this -- verify, then delete this module and drop it from run.sh",
    )
    _require(
        CORRECT_QA_CALL in src,
        f"the QA path's correct idiom {CORRECT_QA_CALL!r} is gone, so this file no longer "
        f"describes upstream's structure",
    )


def apply_georef_patch():
    """Route the band-path reproject through the window's transform. Idempotent.

    Two cooperating wrappers, because `reproject` alone cannot know which window it is
    filling:

      * `round_window_offsets_correctly` is called immediately before the reproject, with
        the very same transform object that gets passed as `dst_transform`. The wrapper
        stashes (window, transform) on the way out.
      * `reproject` consumes that stash and, ONLY when the call has the exact buggy
        signature, substitutes the window's transform.

    The stash is consumed on first use so a stale entry can never leak into an unrelated
    call -- upstream's single-file branches also pass `band_profile["transform"]`, but with
    a full-mosaic-sized destination, and must not be touched.
    """
    from blackmarble.prepare import landsat

    if getattr(landsat, _PATCH_MARKER, False):
        return landsat

    _validate_upstream(landsat)

    import rasterio.windows

    orig_round = landsat.round_window_offsets_correctly
    orig_reproject = landsat.reproject
    stash = {}

    def round_window_offsets_correctly(window, transform):
        rounded = orig_round(window, transform)
        stash["window"] = rounded
        stash["transform"] = transform
        return rounded

    def reproject(*args, **kwargs):
        window = stash.pop("window", None)
        transform = stash.pop("transform", None)
        destination = kwargs.get("destination")
        dst_transform = kwargs.get("dst_transform")

        # Fire ONLY on the buggy shape: the destination array is exactly the size of the
        # window we just rounded, yet the transform handed in is the mosaic's own object.
        # `is` (not ==) on the transform, because the correct QA-path call builds a NEW
        # Affine from the same mosaic transform -- an equality test would match it.
        if (
            window is not None
            and destination is not None
            and dst_transform is not None
            and dst_transform is transform
            and getattr(destination, "shape", None)
            == (int(window.height), int(window.width))
        ):
            kwargs["dst_transform"] = rasterio.windows.transform(window, transform)

        return orig_reproject(*args, **kwargs)

    landsat.round_window_offsets_correctly = round_window_offsets_correctly
    landsat.reproject = reproject
    setattr(landsat, _PATCH_MARKER, True)
    return landsat


def _synthetic_placement_error():
    """Push markers at known coordinates through the patched path; return metres of error.

    This replays upstream's own window arithmetic (same helpers, same MARGIN_PIXELS, same
    paste) against a synthetic UTM-10N scene, so it exercises the patch end to end without
    Earthdata credentials, AWS credentials, or a network.
    """
    import numpy as np
    import rasterio.windows as rw
    from pyproj import Transformer
    from rasterio.transform import from_bounds
    from rasterio.warp import Resampling, transform_bounds

    from blackmarble.crs import create_processing_grid
    from blackmarble.prepare import landsat

    bbox = (-122.55, 37.69, -122.32, 37.81)
    marks = {
        "FerryBldg": (-122.3937, 37.7955),
        "OceanBeach": (-122.5107, 37.7600),
        "TwinPeaks": (-122.4477, 37.7544),
    }
    utm = "EPSG:32610"
    dst_crs, mosaic_transform, (height, width) = create_processing_grid(bbox, 30.0)
    fwd = Transformer.from_crs("EPSG:4326", utm, always_xy=True)
    back = Transformer.from_crs(dst_crs, "EPSG:4326", always_xy=True)

    # A synthetic Landsat scene: UTM 10N, 30 m, one labelled 5x5 blob per landmark.
    sx0, sy1 = fwd.transform(-122.80, 38.00)
    sx1, sy0 = fwd.transform(-122.10, 37.50)
    sw, sh = int((sx1 - sx0) / 30), int((sy1 - sy0) / 30)
    src_transform = from_bounds(sx0, sy0, sx1, sy1, sw, sh)
    scene = np.zeros((sh, sw), dtype="float32")
    labels = {}
    for i, (name, (lon, lat)) in enumerate(marks.items(), start=1):
        mx, my = fwd.transform(lon, lat)
        col, row = (~src_transform) * (mx, my)
        scene[int(row) - 2 : int(row) + 3, int(col) - 2 : int(col) + 3] = i * 100.0
        labels[float(i * 100)] = name

    # --- upstream's window math, verbatim ---
    minx, miny = fwd.transform(bbox[0], bbox[1])
    maxx, maxy = fwd.transform(bbox[2], bbox[3])
    window = rw.from_bounds(minx, miny, maxx, maxy, transform=src_transform)
    margin = landsat.MARGIN_PIXELS
    expanded = rw.Window(
        window.col_off - margin,
        window.row_off - margin,
        window.width + 2 * margin,
        window.height + 2 * margin,
    )
    win_data = np.zeros(
        (int(np.ceil(expanded.height)), int(np.ceil(expanded.width))), dtype="float32"
    )
    r_off, c_off = int(expanded.row_off), int(expanded.col_off)
    win_data[:] = scene[
        r_off : r_off + win_data.shape[0], c_off : c_off + win_data.shape[1]
    ]

    dst_bounds = transform_bounds(utm, dst_crs, *rw.bounds(expanded, src_transform))
    dst_window = rw.from_bounds(*dst_bounds, transform=mosaic_transform)
    dst_window_int = landsat.round_window_offsets_correctly(dst_window, mosaic_transform)
    dh, dw = int(dst_window_int.height), int(dst_window_int.width)
    tile = np.zeros((dh, dw), dtype="float32")

    landsat.reproject(
        source=win_data,
        destination=tile,
        src_transform=rw.transform(expanded, src_transform),
        src_crs=utm,
        dst_transform=mosaic_transform,
        dst_crs=dst_crs,
        resampling=Resampling.nearest,
    )

    mosaic = np.zeros((height, width), dtype="float32")
    off = landsat.calculate_window_offsets(
        int(dst_window_int.row_off), int(dst_window_int.col_off), dh, dw, mosaic.shape
    )
    dst_slice = mosaic[
        off.mosaic_row_start : off.mosaic_row_end,
        off.mosaic_col_start : off.mosaic_col_end,
    ]
    tile_slice = tile[
        off.tile_row_start : off.tile_row_start
        + (off.mosaic_row_end - off.mosaic_row_start),
        off.tile_col_start : off.tile_col_start
        + (off.mosaic_col_end - off.mosaic_col_start),
    ]
    if tile_slice.shape == dst_slice.shape:
        dst_slice[:] = tile_slice

    errors = {}
    for value, name in labels.items():
        hit = np.abs(mosaic - value) < 1e-6
        if not hit.any():
            errors[name] = float("inf")
            continue
        row, col = np.argwhere(hit).mean(axis=0)
        x, y = mosaic_transform * (col + 0.5, row + 0.5)
        lon, lat = back.transform(x, y)
        true_lon, true_lat = marks[name]
        dx = (lon - true_lon) * 111320.0 * np.cos(np.radians(true_lat))
        dy = (lat - true_lat) * 110900.0
        errors[name] = float(np.hypot(dx, dy))
    return errors


def self_check():
    """Validate upstream's shape, then prove the patch actually places pixels correctly.

    No network, no credentials -- safe to wire into dps/Dockerfile's build-time smoke gate
    alongside bm_noaa.py --self-check, so upstream drift fails an image build rather than a
    live activation.
    """
    from blackmarble.prepare import landsat

    _validate_upstream(landsat)
    print(f"  upstream shape OK (MARGIN_PIXELS={landsat.MARGIN_PIXELS}, buggy call present)")

    apply_georef_patch()
    errors = _synthetic_placement_error()
    for name, err in sorted(errors.items()):
        print(f"  {name:12s} placement error {err:8.1f} m")
    worst = max(errors.values())
    if worst > SELF_CHECK_TOLERANCE_M:
        raise RuntimeError(
            f"georef patch did not take effect: worst marker placement error {worst:.1f} m "
            f"exceeds {SELF_CHECK_TOLERANCE_M} m. Unpatched upstream puts these markers "
            f"~3600 m out (or off the grid entirely)."
        )
    print(f"  patched placement OK (worst {worst:.1f} m < {SELF_CHECK_TOLERANCE_M} m)")
    return 0


def main(argv=None):
    """Apply the georeferencing patch, then hand off to upstream's own typer app.

    `--self-check` is consumed here, before typer ever sees argv, so it cannot collide with
    an upstream option of the same name.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--self-check" in argv:
        return self_check()

    apply_georef_patch()
    from blackmarble.cli import app

    sys.argv = [sys.argv[0]] + argv
    app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
