"""Tests for shared_utils.geotools raster writers.

Focus: ``dump_geotiff_rgb``'s optional alpha band (added for the Satellogic
composite nodata fix). The 3-band path must stay byte-compatible because
Sentinel-2 calls this helper at four sites without an alpha.
"""

import numpy as np
import pytest

pytest.importorskip("osgeo")
rasterio = pytest.importorskip("rasterio")

from osgeo import gdal, osr  # noqa: E402
from rasterio.enums import ColorInterp  # noqa: E402


def _projref():
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32617)
    return srs.ExportToWkt()


GEO = (500000.0, 10.0, 0.0, 3200000.0, 0.0, -10.0)


@pytest.fixture
def rgb_bands():
    """Three 32x32 uint8 bands with a distinguishable constant per band."""
    return (
        np.full((32, 32), 10, np.uint8),
        np.full((32, 32), 20, np.uint8),
        np.full((32, 32), 30, np.uint8),
    )


class TestDumpGeotiffRgbNoAlpha:
    """alpha=None must reproduce the legacy 3-band behavior exactly."""

    def test_writes_three_bands(self, tmp_path, rgb_bands):
        from shared_utils.geotools import dump_geotiff_rgb

        out = str(tmp_path / "rgb.tif")
        dump_geotiff_rgb(out, *rgb_bands, _projref(), GEO)

        with rasterio.open(out) as src:
            assert src.count == 3
            assert ColorInterp.alpha not in src.colorinterp
            assert src.nodata is None
            np.testing.assert_array_equal(src.read(1), rgb_bands[0])
            np.testing.assert_array_equal(src.read(3), rgb_bands[2])

    def test_alpha_is_keyword_only_addition(self, rgb_bands):
        """Positional signature is unchanged -- Sentinel-2 passes 6 positionals."""
        import inspect
        from shared_utils.geotools import dump_geotiff_rgb

        params = list(inspect.signature(dump_geotiff_rgb).parameters)
        assert params[:6] == ["filename", "r", "g", "b", "projref", "in_geo"]
        assert params[6] == "alpha"
        assert inspect.signature(dump_geotiff_rgb).parameters["alpha"].default is None


class TestDumpGeotiffRgbWithAlpha:
    """alpha=<array> writes a 4th band tagged GCI_AlphaBand."""

    def test_writes_four_bands_with_alpha_colorinterp(self, tmp_path, rgb_bands):
        from shared_utils.geotools import dump_geotiff_rgb

        alpha = np.full((32, 32), 255, np.uint8)
        alpha[:8, :] = 0  # nodata fill along the top edge

        out = str(tmp_path / "rgba.tif")
        dump_geotiff_rgb(out, *rgb_bands, _projref(), GEO, alpha=alpha)

        with rasterio.open(out) as src:
            assert src.count == 4
            assert src.colorinterp[3] is ColorInterp.alpha
            # No scalar nodata: 0 is a legitimate 8-bit sample.
            assert src.nodata is None
            np.testing.assert_array_equal(src.read(4), alpha)

    def test_dataset_mask_follows_the_alpha_band(self, tmp_path, rgb_bands):
        """The point of the alpha band: GDAL's mask must honor it."""
        from shared_utils.geotools import dump_geotiff_rgb

        alpha = np.full((32, 32), 255, np.uint8)
        alpha[:8, :] = 0

        out = str(tmp_path / "rgba_mask.tif")
        dump_geotiff_rgb(out, *rgb_bands, _projref(), GEO, alpha=alpha)

        with rasterio.open(out) as src:
            mask = src.dataset_mask()
        assert (mask[:8] == 0).all(), "fill border should read as masked"
        assert (mask[8:] == 255).all(), "imagery should read as valid"

    def test_zero_valued_rgb_stays_valid_when_alpha_is_255(self, tmp_path):
        """The bug this replaces: nodata=0 masked legitimately-black pixels."""
        from shared_utils.geotools import dump_geotiff_rgb

        black = np.zeros((16, 16), np.uint8)
        alpha = np.full((16, 16), 255, np.uint8)

        out = str(tmp_path / "black.tif")
        dump_geotiff_rgb(out, black, black, black, _projref(), GEO, alpha=alpha)

        with rasterio.open(out) as src:
            assert (src.dataset_mask() == 255).all(), (
                "an all-black but valid scene must not be masked"
            )
