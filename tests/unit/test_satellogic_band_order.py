"""Tests for per-scene Satellogic band-order resolution.

The vendor ships the OPPOSITE RGB order at the two processing levels -- L1D is
band1=blue/band2=green/band3=red, L1B is band1=red/band2=green/band3=blue --
and ``satellogic_v2`` used to hardcode the L1D layout for both. That transposed
red and blue on every L1B composite and made ndvi/evi read blue where they
meant red. See issue #118 and .clinerules rule 50.

The defect is invisible in the output (``normalize_band`` stretches each band
independently, so a transposed composite still looks plausible), so these tests
pin the resolution rather than any downstream appearance.
"""

import numpy as np
import pytest

pytest.importorskip("osgeo")

from osgeo import gdal, osr  # noqa: E402

# Matches build_output_name's analytic-tiled pattern.
IN_FILE = "20260627_140714_051_SN33_L1D_MS_19N_724_1158_analytic.tif"
SCALE = 1e-4

CI = {
    "red": gdal.GCI_RedBand,
    "green": gdal.GCI_GreenBand,
    "blue": gdal.GCI_BlueBand,
    "gray": gdal.GCI_GrayIndex,
    "undefined": gdal.GCI_Undefined,
}

L1D_ORDER = {"blue": 1, "green": 2, "red": 3, "nir": 4}
L1B_ORDER = {"red": 1, "green": 2, "blue": 3, "nir": 4}


def _scene(tmp_path, colorinterp=None, bands=None, name="scene.tif"):
    """4-band uint16 scene on disk, with optional per-band ColorInterp.

    ``colorinterp`` is a list of CI keys, one per band. Omitted entirely, the
    file carries whatever GDAL defaults to -- which is Gray on band 1 and
    Undefined on the rest, i.e. the "declares nothing" case.
    """
    if bands is None:
        rng = np.random.default_rng(1234)
        bands = [
            rng.integers(1000 * i, 1000 * i + 2000, (16, 16)).astype(np.uint16)
            for i in range(1, 5)
        ]

    path = str(tmp_path / name)
    rows, cols = bands[0].shape
    ds = gdal.GetDriverByName("GTiff").Create(path, cols, rows, len(bands), gdal.GDT_UInt16)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32617)
    ds.SetProjection(srs.ExportToWkt())
    ds.SetGeoTransform((500000.0, 10.0, 0.0, 3200000.0, 0.0, -10.0))

    for i, arr in enumerate(bands, start=1):
        ds.GetRasterBand(i).WriteArray(arr)
        if colorinterp:
            ds.GetRasterBand(i).SetColorInterpretation(CI[colorinterp[i - 1]])

    ds.FlushCache()
    return ds


class TestBandOrderFromColorInterp:
    """The file's own declaration, when it makes one."""

    def test_l1d_declaration_is_read(self, tmp_path):
        from satellogic.satellogic_v2 import band_order_from_colorinterp

        ds = _scene(tmp_path, ["blue", "green", "red", "undefined"])
        assert band_order_from_colorinterp(ds) == L1D_ORDER

    def test_l1b_declaration_is_read(self, tmp_path):
        from satellogic.satellogic_v2 import band_order_from_colorinterp

        ds = _scene(tmp_path, ["red", "green", "blue", "undefined"])
        assert band_order_from_colorinterp(ds) == L1B_ORDER

    def test_nir_is_inferred_as_the_leftover_band(self, tmp_path):
        """The vendor never declares NIR even when it declares R/G/B."""
        from satellogic.satellogic_v2 import band_order_from_colorinterp

        # RGB on bands 2/3/4 -> nir must come out as the unclaimed band 1.
        ds = _scene(tmp_path, ["undefined", "red", "green", "blue"])
        assert band_order_from_colorinterp(ds) == {
            "red": 2, "green": 3, "blue": 4, "nir": 1,
        }

    def test_undeclared_file_returns_none(self, tmp_path):
        """GDAL's default (Gray, Undefined, ...) is not a vendor statement.

        This is what the committed 100x100 L1D crop looks like: its ColorInterp
        was stripped by ``create_segment_file``, which rebuilds the crop from
        rasterio's ``meta`` (which carries no colorinterp). Not hypothetical.
        """
        from satellogic.satellogic_v2 import band_order_from_colorinterp

        ds = _scene(tmp_path)
        assert [
            gdal.GetColorInterpretationName(ds.GetRasterBand(i).GetColorInterpretation())
            for i in range(1, 5)
        ] == ["Gray", "Undefined", "Undefined", "Undefined"]
        assert band_order_from_colorinterp(ds) is None

    def test_band_one_gray_is_not_a_declaration(self, tmp_path):
        """Gray on band 1 is a driver default and must read as unset."""
        from satellogic.satellogic_v2 import band_order_from_colorinterp

        ds = _scene(tmp_path, ["gray", "green", "blue", "undefined"])
        assert band_order_from_colorinterp(ds) is None

    def test_partial_declaration_returns_none(self, tmp_path):
        """Two of three is a default, not a statement -- do not half-trust it."""
        from satellogic.satellogic_v2 import band_order_from_colorinterp

        ds = _scene(tmp_path, ["red", "green", "undefined", "undefined"])
        assert band_order_from_colorinterp(ds) is None

    def test_duplicate_role_returns_none(self, tmp_path):
        """Two bands claiming Red is incoherent -- fall back rather than pick."""
        from satellogic.satellogic_v2 import band_order_from_colorinterp

        ds = _scene(tmp_path, ["red", "red", "green", "blue"])
        assert band_order_from_colorinterp(ds) is None

    def test_no_unique_leftover_returns_none(self, tmp_path):
        """A 3-band RGB file has no band left to be NIR."""
        from satellogic.satellogic_v2 import band_order_from_colorinterp

        rng = np.random.default_rng(7)
        bands = [rng.integers(0, 2000, (16, 16)).astype(np.uint16) for _ in range(3)]
        ds = _scene(tmp_path, ["red", "green", "blue"], bands=bands)
        assert band_order_from_colorinterp(ds) is None


class TestResolveBandIndices:
    """ColorInterp first, level layout as the fallback."""

    @pytest.mark.parametrize("level,expected", [("L1D", L1D_ORDER), ("L1B", L1B_ORDER)])
    def test_falls_back_to_the_level_layout(self, tmp_path, level, expected):
        from satellogic.satellogic_v2 import resolve_band_indices

        assert resolve_band_indices(_scene(tmp_path), level) == expected

    def test_colorinterp_wins_over_the_level_layout(self, tmp_path):
        """A file that declares its own order self-corrects a mislabelled level."""
        from satellogic.satellogic_v2 import resolve_band_indices

        ds = _scene(tmp_path, ["red", "green", "blue", "undefined"])
        assert resolve_band_indices(ds, "L1D") == L1B_ORDER

    def test_unknown_level_warns_and_assumes_l1d(self, tmp_path, capsys):
        from satellogic.satellogic_v2 import resolve_band_indices

        assert resolve_band_indices(_scene(tmp_path), "UNKNOWN") == L1D_ORDER
        assert "WARNING" in capsys.readouterr().out

    @pytest.mark.parametrize("level", ["L1D", "L1B", "UNKNOWN"])
    def test_never_silent(self, tmp_path, level, capsys):
        """A silently-wrong band order is the failure this function ends."""
        from satellogic.satellogic_v2 import resolve_band_indices

        resolve_band_indices(_scene(tmp_path), level)
        assert "Band order" in capsys.readouterr().out


class TestProductsUseTheResolvedOrder:
    """End-to-end through the real band math, S3 stubbed at prepare_scene."""

    @pytest.fixture
    def patched(self, tmp_path, monkeypatch):
        def _install(level, colorinterp=None, bands=None):
            from satellogic import satellogic_v2

            ds = _scene(tmp_path, colorinterp, bands)
            monkeypatch.setattr(
                satellogic_v2,
                "prepare_scene",
                lambda paths, meta, use_mask=True: (
                    ds, None, IN_FILE, level, SCALE, None
                ),
            )
            return ds, str(tmp_path)

        return _install

    # Flat, mutually distinct bands so a channel is traceable to its source band.
    RAW = (2000, 4000, 6000, 8000)

    @classmethod
    def _distinct_bands(cls):
        return [np.full((16, 16), v, np.uint16) for v in cls.RAW]

    @staticmethod
    def _dn(raw):
        """The 8-bit value the writer produces for a raw sample.

        Replays ``load_reflectance_band``'s arithmetic instead of hand-computing
        it, because hand-computing is a trap three times over and none of the
        traps is what this test is about (which BAND feeds which CHANNEL):

        * the cast is ``.astype(np.uint8)``, which TRUNCATES, not rounds;
        * float32 lands just UNDER the exact value, so truncation drops a whole
          count -- 2000 * 1e-4 * 255 is 51 exactly but 50.999996 in float32;
        * the multiply must be IN-PLACE. Under numpy 1.x value-based casting,
          ``np.float32(2000) * 1e-4`` promotes to float64 and yields exactly
          0.2 (-> 51), while the loader's ``arr *= scale_factor`` stays float32
          (-> 0.19999998807907104 -> 50). In-place is float32 on every version.
        """
        arr = np.full((1,), raw, np.float32)
        arr *= SCALE
        return int((np.clip(arr, 0, 1) * 255).astype(np.uint8)[0])

    def test_truecolor_reads_band1_as_red_on_l1b(self, patched):
        """The defect in one assertion: on L1B, band 1 IS red.

        Flat bands make normalize_band degenerate (hi == lo -> zeros), so this
        reads the pre-stretch path: visualize=False writes clipped reflectance
        straight through.
        """
        from satellogic.satellogic_v2 import genTrueColor

        ds, out = patched("L1B", bands=self._distinct_bands())
        path = genTrueColor([IN_FILE], [], out, visualize=False)

        written = gdal.Open(path)
        assert int(written.GetRasterBand(1).ReadAsArray()[0, 0]) == self._dn(2000)  # band 1
        assert int(written.GetRasterBand(3).ReadAsArray()[0, 0]) == self._dn(6000)  # band 3

    def test_truecolor_reads_band3_as_red_on_l1d(self, patched):
        """The other level, unchanged -- this is the regression that matters."""
        from satellogic.satellogic_v2 import genTrueColor

        ds, out = patched("L1D", bands=self._distinct_bands())
        path = genTrueColor([IN_FILE], [], out, visualize=False)

        written = gdal.Open(path)
        assert int(written.GetRasterBand(1).ReadAsArray()[0, 0]) == self._dn(6000)  # band 3
        assert int(written.GetRasterBand(3).ReadAsArray()[0, 0]) == self._dn(2000)  # band 1

    def test_ndvi_moves_between_levels(self, patched):
        """ndvi = (nir - red)/(nir + red); red is band 3 on L1D, band 1 on L1B."""
        from satellogic.satellogic_v2 import genNDVI

        vals = {}
        for level in ("L1D", "L1B"):
            _, out = patched(level, bands=self._distinct_bands())
            path = genNDVI([IN_FILE], [], out, filter_size=3)
            vals[level] = float(gdal.Open(path).ReadAsArray()[8, 8])

        # nir = band4 = 0.8 either way. red = band3 = 0.6 (L1D) / band1 = 0.2 (L1B).
        assert vals["L1D"] == pytest.approx((0.8 - 0.6) / (0.8 + 0.6), abs=1e-4)
        assert vals["L1B"] == pytest.approx((0.8 - 0.2) / (0.8 + 0.2), abs=1e-4)

    def test_ndwi_is_identical_across_levels(self, patched):
        """Bands 2 and 4 agree under both layouts, so ndwi never had the bug.

        Pinned so it stays true -- ndwi is the control that shows the fix moves
        only what the transposition actually touched.
        """
        from satellogic.satellogic_v2 import genNDWI

        arrays = {}
        for level in ("L1D", "L1B"):
            _, out = patched(level)
            path = genNDWI([IN_FILE], [], out, filter_size=3)
            arrays[level] = gdal.Open(path).ReadAsArray().copy()

        assert np.array_equal(arrays["L1D"], arrays["L1B"])
