"""Regression tests for the Landsat -merge post-step directory handling (D1).

The merge phase, after building each merged mosaic, also event-renames the
individual scenes left in that same directory. That logic was copy-pasted into
two loops (cloud-mask dirs and product dirs); the cloud-mask copy referenced the
stale per-scene loop variable `prod_dir` instead of the directory it had just
merged, so per-scene cloud-mask files were never renamed and an unrelated
product directory was touched instead.

The fix extracts one shared helper, `rename_individual_scene_files(directory,
event)`. These tests pin its behavior: it renames the scene files in the GIVEN
directory, skips merged outputs, leaves other directories alone, and is safe to
re-run.
"""

import os

import pytest

rasterio = pytest.importorskip("rasterio")
import numpy as np
from rasterio.crs import CRS
from rasterio.transform import from_bounds


def _touch_scene(path):
    """A minimal valid GeoTIFF (rename_with_event only needs the file to exist)."""
    with rasterio.open(
        path, "w", driver="GTiff", height=4, width=4, count=1, dtype="uint8",
        crs=CRS.from_epsg(32610), transform=from_bounds(0, 0, 40, 40, 4, 4), nodata=0,
    ) as dst:
        dst.write(np.ones((1, 4, 4), dtype="uint8"))


def test_renames_only_the_given_directory(tmp_path):
    from landsat.landsat89_functions import rename_individual_scene_files

    cm = tmp_path / "cloudMask"
    other = tmp_path / "trueColor"
    cm.mkdir()
    other.mkdir()
    _touch_scene(str(cm / "LC08_cloudMask_20250922_185617_046028.tif"))
    _touch_scene(str(cm / "LC08_cloudMask_20250922_190001_046029.tif"))
    _touch_scene(str(cm / "LC08_cloudMask_20250922_merged.tif"))  # merged: skip
    _touch_scene(str(other / "LC08_trueColor_20250922_185617_046028.tif"))
    other_before = set(os.listdir(other))

    rename_individual_scene_files(str(cm), "202509_Flood_X", quiet=True)

    cm_after = set(os.listdir(cm))
    # Both per-scene cloud masks were renamed: date+time relocated to the end as
    # an ISO 8601 Zulu stamp (individual scenes carry a time -> Z, not _day).
    assert "LC08_cloudMask_046028_2025-09-22T18:56:17Z.tif" in cm_after
    assert "LC08_cloudMask_046029_2025-09-22T19:00:01Z.tif" in cm_after
    # The merged output is untouched.
    assert "LC08_cloudMask_20250922_merged.tif" in cm_after
    # D1: the unrelated product directory must not be touched.
    assert set(os.listdir(other)) == other_before


def test_rerun_is_safe(tmp_path):
    """Already-renamed files can't be re-parsed for a date; warn + skip, no raise."""
    from landsat.landsat89_functions import rename_individual_scene_files

    d = tmp_path / "trueColor"
    d.mkdir()
    _touch_scene(str(d / "LC08_trueColor_20250922_185617_046028.tif"))

    rename_individual_scene_files(str(d), "EVT", quiet=True)
    renamed = set(os.listdir(d))
    # Second pass must not raise and must not double-rename.
    rename_individual_scene_files(str(d), "EVT", quiet=True)
    assert set(os.listdir(d)) == renamed
