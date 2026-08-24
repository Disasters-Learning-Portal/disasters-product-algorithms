"""Tests for the Satellogic color-composite alpha band.

``genTrueColor`` / ``gencolorIR`` emit a 4-band RGBA whose alpha marks the
vendor fill (0-valued source samples, which ``load_reflectance_band`` turns
into NaN). Previously the composites declared ``nodata=0``, which also masked
legitimately-black imagery -- see disasters-portal, and the superseded
"composites -> 0" row in ticket #320.

``prepare_scene`` is monkeypatched so these run without S3: everything after
it -- band load, solar correction, normalize/gamma, mask derivation, writer --
is the real code path.
"""

import numpy as np
import pytest

pytest.importorskip("osgeo")
rasterio = pytest.importorskip("rasterio")

from osgeo import gdal, osr  # noqa: E402

# Matches build_output_name's analytic-tiled pattern.
IN_FILE = "20260627_140714_051_SN33_L1D_MS_19N_724_1158_analytic.tif"
SCALE = 1e-4


def _scene_ds(tmp_path, bands):
    """4-band uint16 GDAL dataset (blue, green, red, nir) on disk."""
    path = str(tmp_path / "scene.tif")
    rows, cols = bands[0].shape
    ds = gdal.GetDriverByName("GTiff").Create(path, cols, rows, 4, gdal.GDT_UInt16)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32617)
    ds.SetProjection(srs.ExportToWkt())
    ds.SetGeoTransform((500000.0, 10.0, 0.0, 3200000.0, 0.0, -10.0))
    for i, arr in enumerate(bands, start=1):
        ds.GetRasterBand(i).WriteArray(arr)
    ds.FlushCache()
    return ds


@pytest.fixture
def patched_scene(tmp_path, monkeypatch):
    """Return a factory: bands -> (prepare_scene patched, out_dir)."""
    from satellogic import satellogic_v2

    def _install(bands):
        ds = _scene_ds(tmp_path, bands)
        # L1D so maybe_correct skips solar correction (sunzen unused).
        monkeypatch.setattr(
            satellogic_v2,
            "prepare_scene",
            lambda paths, meta, use_mask=True: (ds, None, IN_FILE, "L1D", SCALE, None),
        )
        return str(tmp_path)

    return _install


def _bands(rows=32, cols=32, fill=5000):
    """Four constant-ish bands with per-band variation so percentiles differ."""
    rng = np.random.default_rng(1234)
    return [
        rng.integers(fill, fill + 2000, (rows, cols)).astype(np.uint16)
        for _ in range(4)
    ]


class TestCompositeAlphaBand:

    @pytest.mark.parametrize("visualize", [True, False])
    def test_zero_fill_border_becomes_transparent(self, patched_scene, visualize):
        """Vendor fill (0 in every band) -> alpha 0; imagery -> alpha 255."""
        from satellogic.satellogic_v2 import genTrueColor

        bands = _bands()
        for b in bands:
            b[:8, :] = 0  # fill border across all bands
        out_dir = patched_scene(bands)

        outfile = genTrueColor([], {}, out_dir, visualize=visualize, gamma=2.2)

        with rasterio.open(outfile) as src:
            assert src.count == 4
            alpha = src.read(4)
        assert (alpha[:8] == 0).all(), "fill border must be transparent"
        assert (alpha[8:] == 255).all(), "imagery must be opaque"

    def test_single_band_zero_is_treated_as_nodata(self, patched_scene):
        """A 0 in ANY band means the sample is unusable -> transparent.

        load_reflectance_band NaNs per-band zeros, so the composite mask is the
        intersection of the three finite masks.
        """
        from satellogic.satellogic_v2 import genTrueColor

        bands = _bands()
        bands[0][20:24, 10:14] = 0  # blue only
        out_dir = patched_scene(bands)

        outfile = genTrueColor([], {}, out_dir, visualize=True, gamma=2.2)

        with rasterio.open(outfile) as src:
            alpha = src.read(4)
        assert (alpha[20:24, 10:14] == 0).all()
        assert alpha.sum() < alpha.size * 255, "some pixels must be transparent"

    def test_degenerate_band_does_not_lose_the_nodata_border(self, patched_scene):
        """Regression: the mask must come from the SOURCE bands.

        normalize_band returns np.zeros_like(band) when a band has no finite
        samples or hi <= lo. Deriving the mask from the post-normalize stack
        would see all-finite zeros and mark the whole scene opaque, silently
        dropping the fill border.
        """
        from satellogic.satellogic_v2 import genTrueColor

        rows = cols = 32
        # Uniform band -> percentile hi == lo -> normalize_band's degenerate path.
        bands = [np.full((rows, cols), 5000, np.uint16) for _ in range(4)]
        for b in bands:
            b[:8, :] = 0
        out_dir = patched_scene(bands)

        outfile = genTrueColor([], {}, out_dir, visualize=True, gamma=2.2)

        with rasterio.open(outfile) as src:
            alpha = src.read(4)
        assert (alpha[:8] == 0).all(), (
            "degenerate normalize_band must not erase the nodata border"
        )
        assert (alpha[8:] == 255).all()

    def test_colorir_gets_the_same_alpha_treatment(self, patched_scene):
        from satellogic.satellogic_v2 import gencolorIR

        bands = _bands()
        for b in bands:
            b[:, :6] = 0  # fill along the left edge
        out_dir = patched_scene(bands)

        outfile = gencolorIR([], {}, out_dir, visualize=True, gamma=2.2)

        with rasterio.open(outfile) as src:
            assert src.count == 4
            alpha = src.read(4)
            assert src.nodata is None
        assert (alpha[:, :6] == 0).all()
        assert (alpha[:, 6:] == 255).all()

    def test_no_scalar_nodata_is_declared(self, patched_scene):
        """0 is a legitimate 8-bit sample; the alpha band carries validity."""
        from satellogic.satellogic_v2 import genTrueColor

        out_dir = patched_scene(_bands())
        outfile = genTrueColor([], {}, out_dir, visualize=True, gamma=2.2)

        with rasterio.open(outfile) as src:
            assert src.nodata is None
