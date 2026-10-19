"""Pins two radiometry fixes.

1. Sentinel-2 ``gen_rgb``: reflectance in (0, 0.04) stretches negative, and a
   negative float assigned into a uint8 array wraps (0.01 -> 249), so dark
   targets such as water rendered near-white. The stretch is now clamped.
2. Landsat: the 1/cos(solar zenith) sun-angle correction belongs to Level-1
   TOA reflectance only. Collection 2 L2 surface reflectance is already
   corrected; dividing it again inflated it by ~1.3x at 40 deg SZA.
"""

import ast
import os

import numpy as np
import pytest

pytest.importorskip("osgeo")
from osgeo import gdal, osr  # noqa: E402

from landsat import landsat89_functions as lf  # noqa: E402
from sentinel2.sentinel2_functions import gen_rgb  # noqa: E402

GEO = (500000.0, 20.0, 0.0, 3200000.0, 0.0, -20.0)


def _write_float(path, arr):
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32617)
    ds = gdal.GetDriverByName("GTiff").Create(
        str(path), arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
    ds.SetProjection(srs.ExportToWkt())
    ds.SetGeoTransform(GEO)
    ds.GetRasterBand(1).WriteArray(arr)
    ds = None
    return str(path)


class TestSentinel2StretchNoWrap:
    # 0 = fill, then two dark-but-valid reflectances, then a mid value.
    REFL = np.array([[0.0, 0.01, 0.03, 0.5]], dtype=np.float32)

    def _run(self, tmp_path):
        # gen_rgb reads the band name from the second-to-last '_' token.
        files = [_write_float(tmp_path / f"T_{b}_20m.tif", self.REFL)
                 for b in ("B12", "B8A", "B04")]
        return gen_rgb(*files, rayleigh=False, enhance=2)

    def test_dark_reflectance_renders_black_not_white(self, tmp_path):
        r, g, b, alpha, _, _ = self._run(tmp_path)
        for band in (r, g, b):
            # pre-fix these were 249 and 254 (wrapped) -> 255 after Brightness x2
            assert band[0, 1] == 0
            assert band[0, 2] == 0

    def test_bright_reflectance_unchanged(self, tmp_path):
        r, _, _, _, _, _ = self._run(tmp_path)
        # (0.5-0.04)/0.96*255 = 122.18 -> uint8 122 -> Brightness x2 = 244
        assert r[0, 3] == 244

    def test_alpha_keeps_dark_pixels_valid(self, tmp_path):
        _, _, _, alpha, _, _ = self._run(tmp_path)
        assert alpha.tolist() == [[0, 255, 255, 255]]


def _write_mtl(indir, level):
    tag = "L2SP" if level == "2" else "L1TP"
    (indir / f"LC09_{tag}_015035_20260601_20260602_02_T1_MTL.xml").write_text(
        "<LANDSAT_METADATA_FILE><IMAGE_ATTRIBUTES>"
        "<SUN_ELEVATION>50.0</SUN_ELEVATION>"
        "</IMAGE_ATTRIBUTES></LANDSAT_METADATA_FILE>")


class TestLandsatSunAngle:
    REFL = np.array([0.1, 0.2, 0.3])

    def test_level_parsed_from_mtl_name(self, tmp_path):
        _write_mtl(tmp_path, "2")
        assert lf.getProcessingLevel(str(tmp_path)) == "2"

    @pytest.mark.parametrize("sza", [0.0, 40.0, 60.0])
    def test_l2_surface_reflectance_not_divided(self, tmp_path, sza):
        _write_mtl(tmp_path, "2")
        out = lf.normalizeSunAngle(self.REFL.copy(), str(tmp_path), sza)
        np.testing.assert_allclose(out, self.REFL)

    @pytest.mark.parametrize("sza", [0.0, 40.0, 60.0])
    def test_l1_toa_divided_by_cos_sza(self, tmp_path, sza):
        _write_mtl(tmp_path, "1")
        out = lf.normalizeSunAngle(self.REFL.copy(), str(tmp_path), sza)
        np.testing.assert_allclose(out, self.REFL / np.cos(np.radians(sza)))

    def test_no_bare_cos_division_outside_helper(self):
        # Every product must go through normalizeSunAngle; a reintroduced
        # inline `/np.cos(...)` would silently re-break L2.
        src = open(lf.__file__).read()
        tree = ast.parse(src)
        offenders = []
        for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
            if fn.name == "normalizeSunAngle":
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "cos"):
                    offenders.append(fn.name)
        assert offenders == []
