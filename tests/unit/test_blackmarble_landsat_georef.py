"""Assertions for dps/blackmarble/bm_georef.py -- the Landsat mosaic georeferencing patch.

WHAT THIS PINS
--------------
Upstream `blackmarble` renders Landsat band tiles at MOSAIC coordinates into a
WINDOW-sized array, then pastes that array into the mosaic at the window offset -- applying
the offset twice. With upstream's `MARGIN_PIXELS = 120` that offset is ~120 px at 30 m, so
every published product sits ~3.6 km north-west of truth.

This is the nastiest class of defect to catch downstream, which is why it needs its own
tests: the COG's transform is CORRECT (it is exactly `create_processing_grid(bbox, 30)`),
its bounds match the requested bbox to 5 dp, `cog_validate` passes, and in a viewer the
layer lands over the right city. Only the pixel CONTENT is displaced. Nothing in the
existing suite -- or in a green DPS job -- would notice.

So the assertions here are about PIXEL PLACEMENT, not about metadata:

  1. the upstream contract the patch is written against still holds (if it stops holding,
     the patch would silently no-op and we would publish misregistered products again),
  2. the patch actually moves pixels to the right place, and
  3. the patch does NOT fire on the call shapes it must leave alone.

Both bm_georef.py and bm_noaa.py live under dps/, which is not an installed package, so
they are loaded by path the way a DPS worker runs them.
"""
import importlib.util
import os
import sys

import numpy as np
import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BLACKMARBLE_DIR = os.path.join(REPO_ROOT, "dps", "blackmarble")
BM_GEOREF_PY = os.path.join(BLACKMARBLE_DIR, "bm_georef.py")
FIXTURE = os.path.join(
    REPO_ROOT, "tests", "fixtures", "blackmarble_sf_misregistered_crop.tif"
)

# The whole point of the patch is a defect measured in kilometres, so the tolerance can be
# generous and still be decisive: unpatched puts markers ~3600 m out (or off the grid).
TOLERANCE_M = 200.0

pytest.importorskip("blackmarble", reason="upstream blackmarble not installed in this env")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bm_georef():
    """Load bm_georef.py fresh, and undo its monkeypatch afterwards.

    The patch mutates the process-global `blackmarble.prepare.landsat` module, so without
    this teardown one test's patch would leak into the next -- and in particular into
    `test_unpatched_upstream_misplaces_pixels`, which must observe the ORIGINAL behaviour.
    """
    from blackmarble.prepare import landsat

    saved = (landsat.reproject, landsat.round_window_offsets_correctly)
    module = _load(BM_GEOREF_PY, "bm_georef")
    try:
        yield module
    finally:
        landsat.reproject, landsat.round_window_offsets_correctly = saved
        if hasattr(landsat, "_disasters_georef_patched"):
            delattr(landsat, "_disasters_georef_patched")
        sys.modules.pop("bm_georef", None)


# --------------------------------------------------------------------------------------
# 1. the upstream contract
# --------------------------------------------------------------------------------------

def test_upstream_still_has_the_defect(bm_georef):
    """If this fails, upstream probably FIXED the bug -- delete the patch, do not 'fix' me.

    A patch for a bug that is no longer there is worse than no patch: it is a wrapper that
    silently does nothing while documentation claims otherwise.
    """
    from blackmarble.prepare import landsat

    bm_georef._validate_upstream(landsat)


def test_margin_pixels_is_what_sets_the_error_magnitude(bm_georef):
    """MARGIN_PIXELS is the offset that gets applied twice; 120 px at 30 m = 3.6 km."""
    from blackmarble.prepare import landsat

    assert landsat.MARGIN_PIXELS == bm_georef.UPSTREAM_MARGIN_PIXELS == 120


def test_validate_upstream_raises_when_the_buggy_call_disappears(bm_georef, monkeypatch):
    """The guard has to actually fire -- prove it, rather than trusting it."""
    from blackmarble.prepare import landsat

    monkeypatch.setattr(landsat, "MARGIN_PIXELS", 999)
    with pytest.raises(RuntimeError, match="MARGIN_PIXELS is 999"):
        bm_georef._validate_upstream(landsat)


# --------------------------------------------------------------------------------------
# 2. the patch places pixels correctly
# --------------------------------------------------------------------------------------

def test_unpatched_upstream_misplaces_pixels():
    """Baseline: without the patch the markers are ~3.6 km out, or gone entirely.

    Loaded WITHOUT the bm_georef fixture so no patch is applied. This is the test that
    would have caught the defect in the first place.
    """
    module = _load(BM_GEOREF_PY, "bm_georef_unpatched")
    try:
        errors = module._synthetic_placement_error()
    finally:
        sys.modules.pop("bm_georef_unpatched", None)

    assert max(errors.values()) > 1000.0, (
        f"expected the unpatched pipeline to misplace markers by kilometres, got {errors}. "
        f"If every marker is now correct, upstream fixed the bug -- drop dps/blackmarble/"
        f"bm_georef.py and this test."
    )


def test_patch_places_markers_within_tolerance(bm_georef):
    """The core assertion: after patching, known coordinates land where they belong."""
    bm_georef.apply_georef_patch()
    errors = bm_georef._synthetic_placement_error()

    assert set(errors) == {"FerryBldg", "OceanBeach", "TwinPeaks"}
    for name, err in errors.items():
        assert err < TOLERANCE_M, f"{name} landed {err:.1f} m from truth (limit {TOLERANCE_M} m)"


def test_self_check_passes(bm_georef, capsys):
    """`--self-check` is wired into dps/Dockerfile, so a regression must break the build."""
    assert bm_georef.self_check() == 0
    assert "patched placement OK" in capsys.readouterr().out


def test_patch_is_idempotent(bm_georef):
    """run.sh calls main() once, but bm_noaa.py also applies it -- double-patching a
    wrapper around a wrapper would nest, and each layer would consume the stash."""
    from blackmarble.prepare import landsat

    bm_georef.apply_georef_patch()
    once = landsat.reproject
    bm_georef.apply_georef_patch()
    assert landsat.reproject is once

    errors = bm_georef._synthetic_placement_error()
    assert max(errors.values()) < TOLERANCE_M


# --------------------------------------------------------------------------------------
# 3. the patch leaves other call shapes alone
# --------------------------------------------------------------------------------------

def _reproject_pair(landsat, dst_transform, dst_shape, mosaic_transform):
    """Run the same reproject through the PATCHED wrapper and through raw rasterio.

    Identical outputs mean the wrapper passed the call through untouched.
    """
    import rasterio.warp
    from rasterio.transform import from_bounds

    src = np.zeros((60, 60), dtype="float32")
    src[20:40, 20:40] = 1.0
    src_transform = from_bounds(-3000, -3000, 3000, 3000, 60, 60)
    crs = "EPSG:3857"

    kwargs = dict(
        src_transform=src_transform,
        src_crs=crs,
        dst_transform=dst_transform,
        dst_crs=crs,
        resampling=rasterio.warp.Resampling.nearest,
    )
    through_patch = np.zeros(dst_shape, dtype="float32")
    landsat.reproject(source=src, destination=through_patch, **kwargs)

    raw = np.zeros(dst_shape, dtype="float32")
    rasterio.warp.reproject(source=src, destination=raw, **kwargs)
    return through_patch, raw


def test_patch_does_not_touch_reprojects_with_no_pending_window(bm_georef):
    """Upstream's single-file branches pass the mosaic transform with a mosaic-sized
    destination and no preceding window rounding. Rewriting those would BREAK correct code.

    The stash is consumed on first use, so nothing can leak into an unrelated later call.
    """
    from blackmarble.prepare import landsat
    from rasterio.transform import from_bounds

    bm_georef.apply_georef_patch()
    mosaic_transform = from_bounds(-3000, -3000, 3000, 3000, 60, 60)

    patched, raw = _reproject_pair(landsat, mosaic_transform, (60, 60), mosaic_transform)
    np.testing.assert_array_equal(patched, raw)
    assert patched.any(), "the fixture should actually move some pixels"


def test_patch_does_not_fire_when_the_destination_is_not_window_sized(bm_georef):
    """A stashed window only licenses a rewrite if the destination array is EXACTLY that
    window's size -- the signature of the buggy call. Anything else passes through."""
    from blackmarble.prepare import landsat
    import rasterio.windows as rw
    from rasterio.transform import from_bounds

    bm_georef.apply_georef_patch()
    mosaic_transform = from_bounds(-3000, -3000, 3000, 3000, 60, 60)

    # Stash a window whose size deliberately disagrees with the destination below.
    landsat.round_window_offsets_correctly(
        rw.Window(col_off=-5, row_off=-5, width=30, height=30), mosaic_transform
    )
    patched, raw = _reproject_pair(landsat, mosaic_transform, (60, 60), mosaic_transform)
    np.testing.assert_array_equal(patched, raw)


def test_patch_fires_on_the_buggy_signature(bm_georef):
    """...and conversely, the exact buggy shape DOES get rewritten."""
    from blackmarble.prepare import landsat
    import rasterio.windows as rw
    from rasterio.transform import from_bounds

    bm_georef.apply_georef_patch()
    mosaic_transform = from_bounds(-3000, -3000, 3000, 3000, 60, 60)
    window = rw.Window(col_off=-10, row_off=-10, width=40, height=40)
    landsat.round_window_offsets_correctly(window, mosaic_transform)

    patched, raw = _reproject_pair(landsat, mosaic_transform, (40, 40), mosaic_transform)
    assert not np.array_equal(patched, raw), (
        "the buggy signature (window-sized destination + the mosaic transform object) "
        "must be rewritten to the window's own transform"
    )


# --------------------------------------------------------------------------------------
# 4. the committed reference product
# --------------------------------------------------------------------------------------

def test_fixture_documents_the_shipped_defect():
    """A crop of a real DPS-published product, kept so the defect stays demonstrable.

    Deliberately asserts only on things that make it a USEFUL reference, not on the
    misregistration itself -- the crop is frozen evidence, and a test that asserted "these
    pixels are wrong" would have to be deleted the day we regenerate it.
    """
    rasterio = pytest.importorskip("rasterio")

    assert os.path.exists(FIXTURE), "the reference crop is missing"
    assert os.path.getsize(FIXTURE) < 500_000, "fixtures must stay small (<500 KB)"

    with rasterio.open(FIXTURE) as src:
        # The unnamed per-bbox local Albers upstream builds -- no EPSG authority code.
        assert src.crs.to_epsg() is None
        assert src.crs.to_dict()["proj"] == "aea"
        assert src.count == 3 and src.dtypes[0] == "uint8"
        tags = src.tags()
        assert tags["ACTIVATION_EVENT"] == "202601_KyleWx_US"
        assert "georeferencing defect" in tags["FIXTURE_NOTE"]
