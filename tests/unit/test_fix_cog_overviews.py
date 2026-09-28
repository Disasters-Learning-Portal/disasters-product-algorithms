"""Tests for raster_tools.fix_cog_overviews.

The bug being pinned: AVERAGE-resampled overviews on a categorical raster
invent class codes that do not exist in the data, so a zoomed-out map view
renders phantom classes. On the live OPERA DSWx S1 WTR mosaic the native band
holds only {0, 1, 3, 251, 255} while its coarsest AVERAGE overview held 256
distinct codes, 1.184% of them class 2 == (1 + 3) / 2.

These tests use a small synthetic stand-in for that raster -- 2048x1024 so the
max()-based level rule differs from the min()-based one (2 levels vs 1) -- and
assert the three properties the tool exists to guarantee: the full-resolution
pixels survive untouched, the overviews stop inventing codes, and the output is
still COG-laid-out.
"""

import os
import shutil
import subprocess

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")
from rasterio.transform import from_origin

from raster_tools.fix_cog_overviews import build_command, fix_one, plan_fix

pytestmark = pytest.mark.gdal

# Two classes with a midpoint that is NOT one of them, so any averaging in the
# overviews shows up as a code that cannot occur in the source.
CLASS_LOW, CLASS_HIGH, PHANTOM = 1, 3, 2


@pytest.fixture
def categorical_cog(tmp_path):
    """A 2048x1024 uint8 COG carrying only {1, 3}, with AVERAGE overviews.

    Deliberately non-square and deliberately under-built (OVERVIEW_COUNT=1
    where the size calls for 2) so one fixture exercises both the wrong
    resampling and the wrong level count.
    """
    if shutil.which("gdal_translate") is None:
        pytest.skip("gdal_translate not on PATH")

    plain = tmp_path / "plain.tif"
    rng = np.random.default_rng(0)
    data = np.where(
        rng.random((1024, 2048)) < 0.5, CLASS_LOW, CLASS_HIGH
    ).astype("uint8")
    with rasterio.open(
        str(plain), "w",
        driver="GTiff", height=1024, width=2048, count=1,
        dtype="uint8", nodata=255,
        transform=from_origin(0, 1024, 1, 1), crs="EPSG:3857",
    ) as dst:
        dst.write(data, 1)

    broken = tmp_path / "broken.tif"
    subprocess.run(
        ["gdal_translate", "-of", "COG", "-q",
         "-co", "COMPRESS=ZSTD", "-co", "BLOCKSIZE=512",
         "-co", "OVERVIEW_RESAMPLING=AVERAGE", "-co", "OVERVIEW_COUNT=1",
         "-mo", "OVERVIEW_RESAMPLING=AVERAGE",
         str(plain), str(broken)],
        check=True,
    )
    return broken


def _codes(path, overview_index):
    """Distinct pixel values in one overview level."""
    with rasterio.open(path) as src:
        factor = src.overviews(1)[overview_index]
        arr = src.read(
            1, out_shape=(src.height // factor, src.width // factor)
        )
    return set(np.unique(arr).tolist())


def test_plan_uses_max_based_level_rule(categorical_cog):
    plan = plan_fix(str(categorical_cog))
    # ceil(log2(2048 / 512)) == 2 on the LONGER side. rio-cogeo's min()-based
    # default would stop at 1, which is the bug fixed upstream in #162.
    assert plan.target_levels == 2
    assert plan.current_levels == 1
    assert plan.current_resampling == "AVERAGE"
    assert plan.target_resampling == "mode"
    assert plan.needs_change


def test_fixture_really_has_phantom_codes(categorical_cog):
    """Guards the test itself: without this the fix proves nothing."""
    assert _codes(categorical_cog, 0) - {CLASS_LOW, CLASS_HIGH}


def test_fix_preserves_full_resolution_pixels(categorical_cog):
    with rasterio.open(categorical_cog) as src:
        before = src.read(1)
        before_hist = np.bincount(before.ravel(), minlength=256)

    assert fix_one(plan_fix(str(categorical_cog)), quiet=True)

    with rasterio.open(categorical_cog) as src:
        after = src.read(1)
        assert np.array_equal(before, after)
        assert np.array_equal(before_hist, np.bincount(after.ravel(), minlength=256))
        assert src.nodata == 255
        assert src.crs == rasterio.crs.CRS.from_epsg(3857)


def test_fix_removes_phantom_classes_and_relayouts(categorical_cog):
    assert fix_one(plan_fix(str(categorical_cog)), quiet=True)

    with rasterio.open(categorical_cog) as src:
        assert len(src.overviews(1)) == 2
        image_structure = src.tags(ns="IMAGE_STRUCTURE")
        # LAYOUT=COG is computed by GDAL from the file's actual IFD/data
        # ordering, so it is the check that gdaladdo's appended overviews
        # would fail.
        assert image_structure["LAYOUT"] == "COG"
        assert image_structure["OVERVIEW_RESAMPLING"] == "MODE"
        assert image_structure["COMPRESSION"] == "ZSTD"
        # The default-domain tag is copied forward by CreateCopy and would
        # otherwise still claim AVERAGE.
        assert src.tags()["OVERVIEW_RESAMPLING"] == "MODE"

    for level in range(2):
        assert _codes(categorical_cog, level) <= {CLASS_LOW, CLASS_HIGH}
        assert PHANTOM not in _codes(categorical_cog, level)


def test_fix_is_idempotent(categorical_cog):
    assert fix_one(plan_fix(str(categorical_cog)), quiet=True)
    size_after_first = os.path.getsize(categorical_cog)

    second = plan_fix(str(categorical_cog))
    assert not second.needs_change
    assert fix_one(second, quiet=True)
    assert os.path.getsize(categorical_cog) == size_after_first


def test_continuous_dtype_is_skipped_unless_asked(tmp_path):
    path = tmp_path / "continuous.tif"
    with rasterio.open(
        str(path), "w",
        driver="GTiff", height=8, width=8, count=1,
        dtype="float32", nodata=-9999.0,
        transform=from_origin(0, 8, 1, 1), crs="EPSG:4326",
    ) as dst:
        dst.write(np.ones((8, 8), dtype="float32"), 1)

    default = plan_fix(str(path))
    assert default.skip_reason is not None
    assert not default.needs_change

    explicit = plan_fix(str(path), resampling="average")
    assert explicit.skip_reason is None


def test_command_preserves_source_structure(categorical_cog):
    cmd = build_command(plan_fix(str(categorical_cog)), "/tmp/out.tif")
    # OVERVIEWS=IGNORE_EXISTING is the one option whose absence makes the
    # tool silently keep the AVERAGE overviews it was run to replace.
    assert "OVERVIEWS=IGNORE_EXISTING" in cmd
    assert "COMPRESS=ZSTD" in cmd
    assert "BLOCKSIZE=512" in cmd
    assert "OVERVIEW_RESAMPLING=MODE" in cmd
    assert "OVERVIEW_COUNT=2" in cmd
    # Never re-grid: the products are already on the WebMercatorQuad grid.
    assert not any(c.startswith("TILING_SCHEME") for c in cmd)


def test_failed_rewrite_leaves_input_untouched(categorical_cog):
    before = categorical_cog.read_bytes()
    plan = plan_fix(str(categorical_cog))
    plan.target_resampling = "not_a_resampling_method"

    assert not fix_one(plan, quiet=True)
    assert categorical_cog.read_bytes() == before
    leftovers = [p for p in os.listdir(categorical_cog.parent)
                 if p.startswith(".fix_ovr_")]
    assert leftovers == []
