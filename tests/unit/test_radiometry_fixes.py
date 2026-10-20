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
    # NaN = fill (extract_band_geotiffs' nodata), then two dark-but-valid
    # reflectances, then a mid value.
    REFL = np.array([[np.nan, 0.01, 0.03, 0.5]], dtype=np.float32)

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


# ---------------------------------------------------------------------------
# End-to-end: run the REAL product functions on a synthetic Collection 2 scene
# with known surface reflectance, and check the science properties directly.
# ---------------------------------------------------------------------------

# Representative SR per band for four cover types: vegetation, soil, water, burn.
SR = {
    "2": [0.03, 0.10, 0.04, 0.05], "3": [0.06, 0.13, 0.05, 0.06],
    "4": [0.04, 0.17, 0.03, 0.08], "5": [0.40, 0.25, 0.02, 0.12],
    "6": [0.20, 0.30, 0.01, 0.25], "7": [0.09, 0.26, 0.005, 0.22],
}
# Collection 2 rescaling: L2 SR is fixed by USGS; L1 values are typical OLI.
SCALE = {2: (2.75e-5, -0.2), 1: (2.0e-5, -0.1)}
QA_CLEAR = 21824  # L8/9 QA_PIXEL: clear, low-confidence cloud/cirrus/snow


def _scene(root, level, sun_elevation):
    """Write B2..B7 + QA_PIXEL GeoTIFFs and an MTL for one 4-pixel scene."""
    d = root / f"L{level}_{sun_elevation:g}"
    d.mkdir()
    tag = "L2SP" if level == 2 else "L1TP"
    grp = ("LEVEL2_SURFACE_REFLECTANCE_PARAMETERS" if level == 2
           else "LEVEL1_RADIOMETRIC_RESCALING")
    mult, add = SCALE[level]
    params = "".join(
        f"<REFLECTANCE_MULT_BAND_{b}>{mult}</REFLECTANCE_MULT_BAND_{b}>"
        f"<REFLECTANCE_ADD_BAND_{b}>{add}</REFLECTANCE_ADD_BAND_{b}>" for b in SR)
    (d / f"LC09_{tag}_015035_20260601_20260602_02_T1_MTL.xml").write_text(
        f"<LANDSAT_METADATA_FILE><IMAGE_ATTRIBUTES><SUN_ELEVATION>{sun_elevation}"
        f"</SUN_ELEVATION></IMAGE_ATTRIBUTES><{grp}>{params}</{grp}>"
        f"</LANDSAT_METADATA_FILE>")

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32617)

    def write(name, arr):
        path = str(d / name)
        ds = gdal.GetDriverByName("GTiff").Create(path, arr.size, 1, 1, gdal.GDT_UInt16)
        ds.SetProjection(srs.ExportToWkt())
        ds.SetGeoTransform((500000.0, 30.0, 0.0, 3200000.0, 0.0, -30.0))
        ds.GetRasterBand(1).WriteArray(arr.reshape(1, -1))
        ds = None
        return [path]

    files = {b: write(f"X_B{b}.TIF",
                      np.round((np.array(v) - add) / mult).astype(np.uint16))
             for b, v in SR.items()}
    files["qa"] = write("X_QA_PIXEL.TIF", np.full(4, QA_CLEAR, np.uint16))
    return d, files, 90.0 - sun_elevation


def _read(path, band=1):
    ds = gdal.Open(path)
    arr = ds.GetRasterBand(band).ReadAsArray()[0].astype(float)
    ds = None
    return arr


def _index(tmp_path, name, level, sun_elevation):
    d, f, sza = _scene(tmp_path, level, sun_elevation)
    out = str(d / f"{name}.tif")
    fn, bands = {
        "ndvi": (lf.genNdvi, ("5", "4")),
        "ndwi": (lf.genNdwi, ("3", "5")),
        "mndwi": (lf.genmNdwi, ("3", "6")),
        "nbr": (lf.genNbr, ("5", "7")),
        "evi": (lf.genEvi, ("5", "4", "2")),
    }[name]
    fn(*(f[b] for b in bands), f["qa"], sza, out)
    return _read(out)


def _natural_color(tmp_path, level, sun_elevation):
    d, f, sza = _scene(tmp_path, level, sun_elevation)
    out = str(d / "nat.tif")
    lf.genNaturalColor(f["6"], f["5"], f["4"], f["qa"], sza, out)
    return np.stack([_read(out, b) for b in (1, 2, 3)])


class TestLandsatProductsEndToEnd:

    def test_l2_evi_matches_usgs_definition(self, tmp_path):
        # USGS Landsat 8-9 EVI on Level-2 SR:
        #   2.5 * (B5 - B4) / (B5 + 6*B4 - 7.5*B2 + 1)
        # Low sun (SZA 60 deg) is where the old 1/cos(SZA) inflation hurt
        # most: it was off by 0.35 here.
        b2, b4, b5 = (np.array(SR[b]) for b in ("2", "4", "5"))
        expected = np.clip(2.5 * (b5 - b4) / (b5 + 6 * b4 - 7.5 * b2 + 1), -1, 1)
        got = _index(tmp_path, "evi", level=2, sun_elevation=30.0)
        np.testing.assert_allclose(got, expected, atol=1e-3)

    @pytest.mark.parametrize("name", ["ndvi", "ndwi", "mndwi", "nbr"])
    def test_l2_normalized_differences_match_definition(self, tmp_path, name):
        a, b = {"ndvi": ("5", "4"), "ndwi": ("3", "5"),
                "mndwi": ("3", "6"), "nbr": ("5", "7")}[name]
        a, b = np.array(SR[a]), np.array(SR[b])
        got = _index(tmp_path, name, level=2, sun_elevation=30.0)
        np.testing.assert_allclose(got, (a - b) / (a + b), atol=1e-3)

    def test_l2_composite_does_not_depend_on_sun_elevation(self, tmp_path):
        # SR is already illumination-normalized, so the same SR must render
        # the same pixels whatever the scene's sun elevation.
        high = _natural_color(tmp_path, level=2, sun_elevation=70.0)
        low = _natural_color(tmp_path, level=2, sun_elevation=30.0)
        np.testing.assert_array_equal(high, low)

    def test_l1_composite_still_sun_angle_corrected(self, tmp_path):
        # Same L1 DNs under a lower sun mean more surface reflectance once
        # corrected, so every valid channel must get brighter, never darker.
        high = _natural_color(tmp_path, level=1, sun_elevation=70.0)
        low = _natural_color(tmp_path, level=1, sun_elevation=30.0)
        assert (low >= high).all()
        assert (low > high).any()


class TestSentinel2StretchProperties:

    def test_stretch_is_monotonic(self, tmp_path):
        # A contrast stretch must preserve brightness ORDER: a darker input
        # can never render brighter than a lighter one. The uint8 wrap broke
        # exactly this (0.01 -> 249 > 0.5 -> 122).
        ramp = np.linspace(0.001, 1.0, 400, dtype=np.float32).reshape(1, -1)
        files = [_write_float(tmp_path / f"T_{b}_20m.tif", ramp)
                 for b in ("B12", "B8A", "B04")]
        r, g, b, _, _, _ = gen_rgb(*files, rayleigh=False, enhance=2)
        for band in (r, g, b):
            assert (np.diff(band[0].astype(int)) >= 0).all()
