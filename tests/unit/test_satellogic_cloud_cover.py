"""Cloud-cover reporting for Satellogic scenes.

Pins the behaviour that distinguishes "this scene is unusable" from "the
pipeline is broken". Both product families fail silently and differently on a
fully-clouded scene -- indices come out 100% nodata, composites come out as a
picture of cloud tops -- so the report has to fire for BOTH.
"""
import numpy as np
import pytest

gdal = pytest.importorskip("osgeo.gdal", reason="GDAL not available")
from osgeo import osr  # noqa: E402

from satellogic.satellogic_v2 import (  # noqa: E402
    CLOUD_CLEAR_CLASS,
    CLOUD_EFFECTIVELY_NONE,
    apply_mask,
    summarize_cloud_cover,
)


def _cloud_tif(path, arr):
    """Write a uint8 cloud-mask GeoTIFF with 0 declared as nodata."""
    h, w = arr.shape
    ds = gdal.GetDriverByName("GTiff").Create(str(path), w, h, 1, gdal.GDT_Byte)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32617)
    ds.SetProjection(srs.ExportToWkt())
    ds.SetGeoTransform((500000.0, 10.0, 0.0, 4000000.0, 0.0, -10.0))
    band = ds.GetRasterBand(1)
    band.WriteArray(arr.astype(np.uint8))
    band.SetNoDataValue(0)
    ds = None
    return str(path)


class TestSummarizeCloudCover:

    def test_clear_scene_reports_clear_and_stays_quiet(self, tmp_path, capsys):
        arr = np.full((200, 200), CLOUD_CLEAR_CLASS, np.uint8)
        arr[:, :100] = 0                      # half off-swath fill
        r = summarize_cloud_cover(_cloud_tif(tmp_path / "clear.tif", arr))
        out = capsys.readouterr().out

        assert r["clear_frac"] == pytest.approx(1.0)
        assert r["cloud_frac"] == pytest.approx(0.0)
        assert r["footprint_frac"] == pytest.approx(0.5, abs=0.01)
        assert "EFFECTIVELY CLOUD-COVERED" not in out

    def test_fully_clouded_scene_warns_loudly(self, tmp_path, capsys):
        arr = np.full((200, 200), 3, np.uint8)   # class 3 == cloud
        r = summarize_cloud_cover(_cloud_tif(tmp_path / "cloud.tif", arr))
        out = capsys.readouterr().out

        assert r["clear_frac"] == pytest.approx(0.0)
        assert "EFFECTIVELY CLOUD-COVERED" in out
        # must name BOTH failure modes -- the whole point of the report
        assert "ndvi" in out and "truecolor" in out

    def test_a_handful_of_stray_clear_pixels_still_warns(self, tmp_path, capsys):
        """Regression: the threshold must not be an `== 0.0` check.

        A real 100%-cloud L1D scene carried 63 class-1 pixels in a 1.3M-pixel
        sample (0.005% of the footprint). An exact-equality test missed it and
        silently downgraded the alarm to the mild "mostly nodata" branch.
        """
        arr = np.full((200, 200), 3, np.uint8)
        arr.flat[:40] = CLOUD_CLEAR_CLASS        # 40/40000 = 0.1% clear
        r = summarize_cloud_cover(_cloud_tif(tmp_path / "stray.tif", arr))
        out = capsys.readouterr().out

        assert 0.0 < r["clear_frac"] < CLOUD_EFFECTIVELY_NONE
        assert "EFFECTIVELY CLOUD-COVERED" in out

    def test_partly_clouded_scene_gets_the_mild_warning(self, tmp_path, capsys):
        arr = np.full((200, 200), 3, np.uint8)
        arr[:40] = CLOUD_CLEAR_CLASS             # 20% clear
        r = summarize_cloud_cover(_cloud_tif(tmp_path / "partly.tif", arr))
        out = capsys.readouterr().out

        assert r["clear_frac"] == pytest.approx(0.20, abs=0.01)
        assert "EFFECTIVELY CLOUD-COVERED" not in out
        assert "mostly nodata" in out

    def test_empty_footprint_does_not_divide_by_zero(self, tmp_path, capsys):
        r = summarize_cloud_cover(
            _cloud_tif(tmp_path / "empty.tif", np.zeros((50, 50), np.uint8))
        )
        assert r["footprint_frac"] == 0.0
        assert "no imagery" in capsys.readouterr().out

    def test_unreadable_file_returns_none_instead_of_raising(self, tmp_path, capsys):
        """A cosmetic report must never abort a real processing run."""
        assert summarize_cloud_cover(str(tmp_path / "nope.tif")) is None
        assert "could not summarize" in capsys.readouterr().out

    def test_percentages_are_of_footprint_not_whole_raster(self, tmp_path):
        """Off-swath fill is geometry, not a data-quality property."""
        arr = np.zeros((200, 200), np.uint8)
        arr[:20] = CLOUD_CLEAR_CLASS             # 10% of raster
        arr[20:40] = 3                           # 10% of raster
        r = summarize_cloud_cover(_cloud_tif(tmp_path / "fp.tif", arr))
        assert r["footprint_frac"] == pytest.approx(0.20, abs=0.01)
        assert r["clear_frac"] == pytest.approx(0.50, abs=0.01)


class TestReportMatchesMask:

    def test_reported_clear_fraction_equals_what_apply_mask_keeps(self, tmp_path):
        """The report would be a lie if it and apply_mask ever disagreed."""
        rng = np.random.default_rng(0)
        arr = rng.choice([0, 1, 2, 3], size=(200, 200), p=[.4, .3, .1, .2]).astype(np.uint8)
        r = summarize_cloud_cover(_cloud_tif(tmp_path / "mixed.tif", arr))

        data = np.ones((200, 200), np.float32)
        kept = np.isfinite(apply_mask([data], arr)[0]).sum()
        footprint = int((arr != 0).sum())

        assert kept / footprint == pytest.approx(r["clear_frac"], abs=1e-9)
