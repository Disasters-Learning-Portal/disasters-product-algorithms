"""Tests for shared_utils.summarize_raster."""

import math
import os

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")
from rasterio.transform import from_origin

# Import from the submodule directly to avoid triggering optional
# package-init imports (e.g. cog_metadata) that need the real GDAL C
# library, which the test conftest only stubs.
from shared_utils.geotiff_analyzer import summarize_raster

# 256x256 float32 window cropped from the GAIA low-durability-wood-framed-1
# monthly product over Atlanta, GA. EPSG:3857, nodata=-9999.0. Lives in the
# repo so this test runs without external downloads.
GAIA_ATLANTA = os.path.join(
    os.path.dirname(__file__), "..", "fixtures", "gaia_atlanta_sample.tif"
)


def test_summarize_raster_respects_nodata(tmp_path):
    path = tmp_path / "fixture.tif"
    arr = np.array([[1, 2, -9999], [3, 4, 5]], dtype="int16")
    with rasterio.open(
        str(path), "w",
        driver="GTiff", height=2, width=3, count=1,
        dtype="int16", nodata=-9999,
        transform=from_origin(0, 2, 1, 1), crs="EPSG:4326",
    ) as dst:
        dst.write(arr, 1)

    stats = summarize_raster(str(path))
    assert stats["nodata_count"] == 1
    assert stats["valid_count"] == 5
    assert stats["min"] == 1
    assert stats["max"] == 5
    assert stats["mean"] == pytest.approx((1 + 2 + 3 + 4 + 5) / 5)


def test_summarize_raster_uses_int16_fixture(int16_geotiff):
    stats = summarize_raster(int16_geotiff)
    # Fixture is random int16 in [-100, 100) with nodata=-9999 (none injected),
    # so all 64*64 pixels are valid.
    assert stats["valid_count"] == 64 * 64
    assert stats["nodata_count"] == 0
    assert -100 <= stats["min"] <= stats["max"] < 100


def test_summarize_raster_counts_float32_nodata(float32_geotiff):
    stats = summarize_raster(float32_geotiff)
    # Fixture sets a 5x5 block to -9999, so 25 pixels are nodata.
    assert stats["nodata_count"] == 25
    assert stats["valid_count"] == 64 * 64 - 25


def test_summarize_raster_override_nodata(tmp_path):
    path = tmp_path / "no_recorded_nodata.tif"
    arr = np.array([[0, 1, 2], [3, 4, 5]], dtype="int16")
    with rasterio.open(
        str(path), "w",
        driver="GTiff", height=2, width=3, count=1,
        dtype="int16",  # nodata intentionally not set
        transform=from_origin(0, 2, 1, 1), crs="EPSG:4326",
    ) as dst:
        dst.write(arr, 1)

    stats = summarize_raster(str(path), nodata=0)
    assert stats["nodata_count"] == 1
    assert stats["valid_count"] == 5
    assert stats["min"] == 1


def test_summarize_raster_on_real_gaia_atlanta():
    """Smoke test on a real GAIA crop committed to the repo.

    Synthetic fixtures verify arithmetic; this verifies the function
    behaves correctly on actual float32 EPSG:3857 GAIA data with the
    project's standard nodata=-9999.0 sentinel.
    """
    fixture = os.path.abspath(GAIA_ATLANTA)
    assert os.path.exists(fixture), f"fixture missing: {fixture}"

    stats = summarize_raster(fixture)

    # The crop is wholly within CONUS, so no pixels match the -9999 sentinel.
    assert stats["nodata_count"] == 0
    assert stats["valid_count"] == 256 * 256

    # Locked-in values from the cropped window (Atlanta, GA).
    # If the fixture is re-cropped these numbers must be updated together.
    assert stats["min"] == pytest.approx(-9189.3837890625, rel=1e-6)
    assert stats["max"] == pytest.approx(133991.734375, rel=1e-6)
    assert stats["mean"] == pytest.approx(12803.6240234375, rel=1e-6)


def test_summarize_raster_override_treats_real_data_as_nodata():
    """Override path on the real fixture: telling the function to treat 0.0
    as nodata should reduce valid_count below the full 65536."""
    stats_default = summarize_raster(os.path.abspath(GAIA_ATLANTA))
    stats_zero = summarize_raster(os.path.abspath(GAIA_ATLANTA), nodata=0.0)
    assert stats_zero["valid_count"] < stats_default["valid_count"]
    assert stats_zero["nodata_count"] > 0


def test_summarize_raster_all_nodata(tmp_path):
    path = tmp_path / "all_nodata.tif"
    arr = np.full((2, 3), -9999, dtype="int16")
    with rasterio.open(
        str(path), "w",
        driver="GTiff", height=2, width=3, count=1,
        dtype="int16", nodata=-9999,
        transform=from_origin(0, 2, 1, 1), crs="EPSG:4326",
    ) as dst:
        dst.write(arr, 1)

    stats = summarize_raster(str(path))
    assert stats["valid_count"] == 0
    assert stats["nodata_count"] == 6
    assert math.isnan(stats["min"])
    assert math.isnan(stats["mean"])
