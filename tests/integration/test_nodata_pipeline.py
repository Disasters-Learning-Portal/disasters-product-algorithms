"""Integration tests for nodata handling in COG pipeline."""

import pytest
import shutil
import os
import numpy as np

rasterio = pytest.importorskip("rasterio")

HAS_RIO = shutil.which("rio") is not None


@pytest.mark.skipif(not HAS_RIO, reason="rio CLI not available")
class TestNodataPipeline:

    def test_nodata_auto_detection_uint8(self, uint8_geotiff):
        """Auto-detect nodata=0 for uint8."""
        from shared_utils.cog_utils import convert_to_cog

        result = convert_to_cog(uint8_geotiff, quiet=True)
        with rasterio.open(result) as src:
            assert src.nodata == 0

    def test_nodata_auto_detection_float(self, float32_geotiff):
        """Auto-detect nodata=-9999 for float32."""
        from shared_utils.cog_utils import convert_to_cog

        result = convert_to_cog(float32_geotiff, dst_crs=None, quiet=True)
        with rasterio.open(result) as src:
            assert src.nodata == -9999.0

    def test_custom_nodata_override(self, uint8_geotiff):
        """Override nodata with custom value."""
        from shared_utils.cog_utils import convert_to_cog

        result = convert_to_cog(uint8_geotiff, nodata=255, quiet=True)
        with rasterio.open(result) as src:
            assert src.nodata == 255

    def test_temp_files_use_tmp_dir(self, float32_geotiff):
        """Verify no temp files left behind in source directory."""
        from shared_utils.cog_utils import convert_to_cog

        source_dir = os.path.dirname(float32_geotiff)
        files_before = set(os.listdir(source_dir))

        convert_to_cog(float32_geotiff, dst_crs='EPSG:4326', quiet=True)

        files_after = set(os.listdir(source_dir))
        # No new temp files should remain in source directory
        new_files = files_after - files_before
        temp_files = [f for f in new_files if '.tmp.' in f or '.warped.' in f]
        assert len(temp_files) == 0, f"Temp files left behind: {temp_files}"

    def test_nodata_preserved_through_conversion(self, int16_geotiff):
        """Verify nodata value survives COG conversion without reprojection."""
        from shared_utils.cog_utils import convert_to_cog

        result = convert_to_cog(int16_geotiff, dst_crs=None, quiet=True)
        with rasterio.open(result) as src:
            assert src.nodata == -9999
