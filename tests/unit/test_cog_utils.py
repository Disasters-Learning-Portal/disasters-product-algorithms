"""Tests for shared_utils.cog_utils module."""

import pytest
import os
import numpy as np

rasterio = pytest.importorskip("rasterio")


class TestSetNodataValue:
    """Tests for set_nodata_value(dtype, manual_nodata=None)."""

    def test_uint8_returns_0(self):
        from shared_utils.cog_utils import set_nodata_value
        assert set_nodata_value('uint8') == 0

    def test_uint16_returns_0(self):
        from shared_utils.cog_utils import set_nodata_value
        assert set_nodata_value('uint16') == 0

    def test_int8_returns_neg128(self):
        from shared_utils.cog_utils import set_nodata_value
        assert set_nodata_value('int8') == -128

    def test_int16_returns_neg9999(self):
        from shared_utils.cog_utils import set_nodata_value
        assert set_nodata_value('int16') == -9999

    def test_int32_returns_neg9999(self):
        from shared_utils.cog_utils import set_nodata_value
        assert set_nodata_value('int32') == -9999

    def test_float32_returns_neg9999_float(self):
        from shared_utils.cog_utils import set_nodata_value
        assert set_nodata_value('float32') == -9999.0

    def test_float64_returns_neg9999_float(self):
        from shared_utils.cog_utils import set_nodata_value
        assert set_nodata_value('float64') == -9999.0

    def test_unknown_type_fallback(self):
        from shared_utils.cog_utils import set_nodata_value
        assert set_nodata_value('complex64') == -9999.0

    def test_valid_manual_nodata(self):
        from shared_utils.cog_utils import set_nodata_value
        assert set_nodata_value('uint8', manual_nodata=255) == 255

    def test_invalid_manual_nodata_falls_back(self):
        from shared_utils.cog_utils import set_nodata_value
        # -1 is out of range for uint8, should fall back to default (0)
        assert set_nodata_value('uint8', manual_nodata=-1) == 0


class TestValidateNodataForDtype:
    """Tests for validate_nodata_for_dtype(nodata, dtype)."""

    def test_valid_uint8_zero(self):
        from shared_utils.cog_utils import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(0, 'uint8')
        assert result['valid'] is True
        assert result['error'] is None

    def test_negative_uint8_invalid(self):
        from shared_utils.cog_utils import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(-1, 'uint8')
        assert result['valid'] is False
        assert result['error'] is not None

    def test_overflow_uint8_invalid(self):
        from shared_utils.cog_utils import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(256, 'uint8')
        assert result['valid'] is False

    def test_valid_int16(self):
        from shared_utils.cog_utils import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(-9999, 'int16')
        assert result['valid'] is True

    def test_valid_float32(self):
        from shared_utils.cog_utils import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(-9999.0, 'float32')
        assert result['valid'] is True

    def test_none_nodata_invalid(self):
        from shared_utils.cog_utils import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(None, 'uint8')
        assert result['valid'] is False

    def test_nan_float32_valid(self):
        from shared_utils.cog_utils import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(float('nan'), 'float32')
        assert result['valid'] is True

    def test_returns_dict_with_expected_keys(self):
        from shared_utils.cog_utils import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(0, 'uint8')
        assert 'valid' in result
        assert 'error' in result


class TestDetermineResamplingMethod:
    """Tests for determine_resampling_method(src_path)."""

    def test_rgb_3band_returns_cubic(self, uint8_geotiff):
        from shared_utils.cog_utils import determine_resampling_method
        method, overview = determine_resampling_method(uint8_geotiff)
        assert method == 'cubic'
        assert overview == 'average'

    def test_float_1band_returns_bilinear(self, float32_geotiff):
        from shared_utils.cog_utils import determine_resampling_method
        method, overview = determine_resampling_method(float32_geotiff)
        assert method == 'bilinear'
        assert overview == 'average'

    def test_mask_file_returns_nearest(self, categorical_geotiff):
        from shared_utils.cog_utils import determine_resampling_method
        method, overview = determine_resampling_method(categorical_geotiff)
        assert method == 'nearest'
        assert overview == 'mode'


class TestGetCompressionProfile:
    """Tests for get_compression_profile(...)."""

    def test_default_profile(self):
        from shared_utils.cog_utils import get_compression_profile
        profile = get_compression_profile()
        assert profile['compress'] == 'ZSTD'
        assert profile['predictor'] == '2'
        assert profile['level'] == 22

    def test_float32_predictor(self):
        from shared_utils.cog_utils import get_compression_profile
        profile = get_compression_profile(dtype='float32')
        assert profile['predictor'] == '3'

    def test_uint8_predictor(self):
        from shared_utils.cog_utils import get_compression_profile
        profile = get_compression_profile(dtype='uint8')
        assert profile['predictor'] == '2'

    def test_large_file_bigtiff(self):
        from shared_utils.cog_utils import get_compression_profile
        profile = get_compression_profile(file_size_gb=5.0)
        assert profile['bigtiff'] == 'YES'

    def test_very_large_file_small_blocks(self):
        from shared_utils.cog_utils import get_compression_profile
        profile = get_compression_profile(file_size_gb=15.0)
        assert profile['blockxsize'] == 256

    def test_invalid_compression_falls_back(self):
        from shared_utils.cog_utils import get_compression_profile
        profile = get_compression_profile(compression='INVALID')
        assert profile['compress'] == 'ZSTD'


class TestValidateCog:
    """Tests for validate_cog(cog_path)."""

    def test_returns_tuple(self, uint8_geotiff):
        from shared_utils.cog_utils import validate_cog
        result = validate_cog(uint8_geotiff)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_result_dict_has_expected_keys(self, uint8_geotiff):
        from shared_utils.cog_utils import validate_cog
        _, details = validate_cog(uint8_geotiff)
        assert 'valid' in details
        assert 'errors' in details
        assert 'warnings' in details

    def test_nonexistent_file(self):
        from shared_utils.cog_utils import validate_cog
        is_valid, details = validate_cog('/nonexistent/path/file.tif')
        assert is_valid is False


class TestGetFinalFilename:
    """Tests for get_final_filename(original_path, event_name, tif_only)."""

    def test_no_event_name_returns_original(self):
        from shared_utils.cog_utils import get_final_filename
        path = "/path/LC08_trueColor_20250922_185617_046028.tif"
        assert get_final_filename(path, None) == path

    def test_landsat_with_event_name(self):
        from shared_utils.cog_utils import get_final_filename
        path = "/path/LC08_trueColor_20250922_185617_046028.tif"
        result = get_final_filename(path, "202512_Flood_WA")
        assert "202512_Flood_WA" in result
        assert "2025-09-22" in result
        assert result.endswith(".tif")

    def test_sentinel2_with_event_name(self):
        from shared_utils.cog_utils import get_final_filename
        path = "/path/S2B_MSIL2A_colorInfrared_20251111_161419_T17RLN.tif"
        result = get_final_filename(path, "202511_Fire_CA")
        assert "202511_Fire_CA" in result
        assert "2025-11-11" in result

    def test_merged_file_with_event_name(self):
        from shared_utils.cog_utils import get_final_filename
        path = "/path/LC08_trueColor_20250922_merged.tif"
        result = get_final_filename(path, "202512_Flood_WA")
        assert "merged" in result
        assert "2025-09-22" in result

    def test_no_date_returns_original(self):
        from shared_utils.cog_utils import get_final_filename
        path = "/path/some_file_no_date.tif"
        result = get_final_filename(path, "SomeEvent")
        assert result == path


class TestRenameWithEvent:
    """Tests for rename_with_event(file_path, event_name, quiet)."""

    def test_landsat_rename(self, tmp_path):
        from shared_utils.cog_utils import rename_with_event
        # Create a file with Landsat naming pattern
        src = tmp_path / "LC08_trueColor_20250922_185617_046028.tif"
        src.write_bytes(b"dummy")
        result = rename_with_event(str(src), "202512_Flood_WA", quiet=True)
        assert os.path.exists(result)
        assert "202512_Flood_WA" in os.path.basename(result)
        assert "2025-09-22" in os.path.basename(result)

    def test_sentinel2_rename(self, tmp_path):
        from shared_utils.cog_utils import rename_with_event
        src = tmp_path / "S2B_MSIL2A_colorInfrared_20251111_161419_T17RLN.tif"
        src.write_bytes(b"dummy")
        result = rename_with_event(str(src), "202511_Fire_CA", quiet=True)
        assert os.path.exists(result)
        assert "202511_Fire_CA" in os.path.basename(result)
        assert "2025-11-11" in os.path.basename(result)

    def test_invalid_filename_raises_valueerror(self, tmp_path):
        from shared_utils.cog_utils import rename_with_event
        src = tmp_path / "ab.tif"
        src.write_bytes(b"dummy")
        with pytest.raises(ValueError):
            rename_with_event(str(src), "Event", quiet=True)

    def test_missing_file_raises_filenotfounderror(self):
        from shared_utils.cog_utils import rename_with_event
        with pytest.raises(FileNotFoundError):
            rename_with_event("/nonexistent/file.tif", "Event", quiet=True)
