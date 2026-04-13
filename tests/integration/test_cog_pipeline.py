"""Integration tests for COG conversion pipeline."""

import pytest
import shutil
import os

rasterio = pytest.importorskip("rasterio")

HAS_RIO = shutil.which("rio") is not None


@pytest.mark.skipif(not HAS_RIO, reason="rio CLI not available")
class TestCOGPipeline:

    def test_uint8_rgb_to_cog(self, uint8_geotiff):
        """Convert 3-band uint8 GeoTIFF to COG, verify valid output."""
        from shared_utils.cog_utils import convert_to_cog, validate_cog

        result = convert_to_cog(uint8_geotiff, quiet=True)
        assert os.path.exists(result)

        is_valid, details = validate_cog(result)
        assert is_valid

        with rasterio.open(result) as src:
            assert src.nodata == 0
            assert src.count == 3

    def test_float32_with_reprojection(self, float32_geotiff):
        """Convert float32 GeoTIFF with UTM->4326 reprojection."""
        from shared_utils.cog_utils import convert_to_cog

        result = convert_to_cog(float32_geotiff, dst_crs='EPSG:4326', quiet=True)
        assert os.path.exists(result)

        with rasterio.open(result) as src:
            assert 'EPSG:4326' in str(src.crs).upper() or src.crs.to_epsg() == 4326
            assert src.nodata == -9999.0

    def test_no_reprojection_when_already_4326(self, epsg4326_geotiff):
        """Skip reprojection when already in target CRS."""
        from shared_utils.cog_utils import convert_to_cog

        result = convert_to_cog(epsg4326_geotiff, dst_crs='EPSG:4326', quiet=True)
        assert os.path.exists(result)

        with rasterio.open(result) as src:
            assert src.crs.to_epsg() == 4326

    def test_categorical_uses_nearest(self, categorical_geotiff):
        """Verify categorical data uses nearest-neighbor resampling."""
        from shared_utils.cog_utils import determine_resampling_method

        method, overview_method = determine_resampling_method(categorical_geotiff)
        assert method == 'nearest'
        assert overview_method == 'mode'

    def test_convert_preserves_data(self, int16_geotiff):
        """Verify pixel values preserved after COG conversion (no reprojection)."""
        from shared_utils.cog_utils import convert_to_cog
        import numpy as np

        # Read original
        with rasterio.open(int16_geotiff) as src:
            original_data = src.read()

        result = convert_to_cog(int16_geotiff, dst_crs=None, quiet=True)

        with rasterio.open(result) as src:
            result_data = src.read()

        np.testing.assert_array_equal(original_data, result_data)

    def test_convert_with_custom_compression(self, uint8_geotiff):
        """Convert with DEFLATE compression."""
        from shared_utils.cog_utils import convert_to_cog

        result = convert_to_cog(
            uint8_geotiff,
            dst_crs=None,
            compression='DEFLATE',
            compression_level=9,
            quiet=True
        )
        assert os.path.exists(result)

    def test_output_path_specified(self, uint8_geotiff, tmp_path):
        """Convert to a specified output path."""
        from shared_utils.cog_utils import convert_to_cog

        output = str(tmp_path / "output_cog.tif")
        result = convert_to_cog(uint8_geotiff, output_cog=output, dst_crs=None, quiet=True)
        assert result == output
        assert os.path.exists(output)

    def test_nonexistent_input_raises(self):
        """Raise FileNotFoundError for missing input."""
        from shared_utils.cog_utils import convert_to_cog

        with pytest.raises(FileNotFoundError):
            convert_to_cog("/nonexistent/file.tif", quiet=True)
