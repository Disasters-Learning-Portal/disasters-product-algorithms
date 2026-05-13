"""Tests for shared_utils.compression module."""

import pytest
import numpy as np
from unittest.mock import MagicMock


class TestValidateNodataForDtype:
    """Tests for validate_nodata_for_dtype(nodata_value, dtype)."""

    def test_valid_uint8(self):
        from shared_utils.compression import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(0, 'uint8')
        assert result['valid'] is True

    def test_invalid_uint8_negative(self):
        from shared_utils.compression import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(-1, 'uint8')
        assert result['valid'] is False

    def test_invalid_uint8_overflow(self):
        from shared_utils.compression import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(256, 'uint8')
        assert result['valid'] is False

    def test_valid_int16(self):
        from shared_utils.compression import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(-9999, 'int16')
        assert result['valid'] is True

    def test_valid_float32(self):
        from shared_utils.compression import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(-9999.0, 'float32')
        assert result['valid'] is True

    def test_nan_float32_valid(self):
        from shared_utils.compression import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(float('nan'), 'float32')
        assert result['valid'] is True

    def test_returns_dict_keys(self):
        from shared_utils.compression import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(0, 'uint8')
        assert 'valid' in result
        assert 'error' in result


class TestGetPredictorForDtype:
    """Tests for get_predictor_for_dtype(dtype)."""

    def test_float32_returns_3(self):
        from shared_utils.compression import get_predictor_for_dtype
        assert get_predictor_for_dtype('float32') == 3

    def test_float64_returns_3(self):
        from shared_utils.compression import get_predictor_for_dtype
        assert get_predictor_for_dtype('float64') == 3

    def test_uint8_returns_2(self):
        from shared_utils.compression import get_predictor_for_dtype
        assert get_predictor_for_dtype('uint8') == 2

    def test_int16_returns_2(self):
        from shared_utils.compression import get_predictor_for_dtype
        assert get_predictor_for_dtype('int16') == 2

    def test_unknown_returns_1(self):
        from shared_utils.compression import get_predictor_for_dtype
        assert get_predictor_for_dtype('complex64') == 1


class TestSetNodataValue:
    """Tests for set_nodata_value(ds) - takes dataset object."""

    def test_uint8_dataset(self):
        from shared_utils.compression import set_nodata_value
        ds = MagicMock()
        ds.dtype = 'uint8'
        assert set_nodata_value(ds) == 0

    def test_uint16_dataset(self):
        from shared_utils.compression import set_nodata_value
        ds = MagicMock()
        ds.dtype = 'uint16'
        assert set_nodata_value(ds) == 0

    def test_int8_dataset(self):
        from shared_utils.compression import set_nodata_value
        ds = MagicMock()
        ds.dtype = 'int8'
        assert set_nodata_value(ds) == -128

    def test_int16_dataset(self):
        from shared_utils.compression import set_nodata_value
        ds = MagicMock()
        ds.dtype = 'int16'
        assert set_nodata_value(ds) == -9999

    def test_float32_dataset(self):
        from shared_utils.compression import set_nodata_value
        ds = MagicMock()
        ds.dtype = 'float32'
        assert set_nodata_value(ds) == -9999


class TestSetNodataValueSrc:
    """Tests for set_nodata_value_src(src, manual_nodata=None)."""

    def test_auto_uint8(self):
        from shared_utils.compression import set_nodata_value_src
        src = MagicMock()
        src.dtypes = ['uint8']
        assert set_nodata_value_src(src) == 0

    def test_manual_nodata_valid(self):
        from shared_utils.compression import set_nodata_value_src
        src = MagicMock()
        src.dtypes = ['uint8']
        assert set_nodata_value_src(src, manual_nodata=255) == 255

    def test_manual_nodata_invalid_falls_back(self):
        from shared_utils.compression import set_nodata_value_src
        src = MagicMock()
        src.dtypes = ['uint8']
        # -1 is invalid for uint8, should fall back to auto (0)
        assert set_nodata_value_src(src, manual_nodata=-1) == 0


class TestGetCompressionConfig:
    """Tests for get_compression_config(file_size_gb, dtype)."""

    def test_default_config(self):
        from shared_utils.compression import get_compression_config
        config = get_compression_config()
        assert config['compress'] == 'zstd'
        assert config['tiled'] is True
        assert 'predictor' in config

    def test_large_file_bigtiff(self):
        from shared_utils.compression import get_compression_config
        config = get_compression_config(file_size_gb=5.0)
        assert config['bigtiff'] == 'YES'

    def test_small_file_bigtiff(self):
        from shared_utils.compression import get_compression_config
        config = get_compression_config(file_size_gb=1.0)
        assert config['bigtiff'] == 'IF_SAFER'

    def test_very_large_file_small_blocks(self):
        from shared_utils.compression import get_compression_config
        config = get_compression_config(file_size_gb=15.0)
        assert config['blockxsize'] == 256

    def test_float_predictor(self):
        from shared_utils.compression import get_compression_config
        config = get_compression_config(dtype='float32')
        assert config['predictor'] == 3

    def test_int_predictor(self):
        from shared_utils.compression import get_compression_config
        config = get_compression_config(dtype='uint8')
        assert config['predictor'] == 2


class TestRemapNodataValue:
    """Tests for remap_nodata_value(data, original, new, dtype)."""

    def test_integer_remap(self):
        from shared_utils.compression import remap_nodata_value
        data = np.array([1, 2, -9999, 4, -9999], dtype=np.int16)
        result = remap_nodata_value(data, -9999, 0, 'int16')
        expected = np.array([1, 2, 0, 4, 0], dtype=np.int16)
        np.testing.assert_array_equal(result, expected)

    def test_float_nan_remap(self):
        from shared_utils.compression import remap_nodata_value
        data = np.array([1.0, np.nan, 3.0, np.nan], dtype=np.float32)
        result = remap_nodata_value(data, float('nan'), -9999.0, 'float32')
        expected = np.array([1.0, -9999.0, 3.0, -9999.0], dtype=np.float32)
        np.testing.assert_array_equal(result, expected)

    def test_no_change_when_same(self):
        from shared_utils.compression import remap_nodata_value
        data = np.array([1, 2, 0, 4], dtype=np.int16)
        result = remap_nodata_value(data, 0, 0, 'int16')
        np.testing.assert_array_equal(result, data)

    def test_none_original_returns_unchanged(self):
        from shared_utils.compression import remap_nodata_value
        data = np.array([1, 2, 3], dtype=np.int16)
        result = remap_nodata_value(data, None, -9999, 'int16')
        np.testing.assert_array_equal(result, data)

    def test_does_not_modify_original(self):
        from shared_utils.compression import remap_nodata_value
        data = np.array([1, 2, -9999, 4], dtype=np.int16)
        original_copy = data.copy()
        remap_nodata_value(data, -9999, 0, 'int16')
        np.testing.assert_array_equal(data, original_copy)


class TestExportCogProfile:
    """Tests for export_cog_profile()."""

    def test_returns_dict(self):
        from shared_utils.compression import export_cog_profile
        profile = export_cog_profile()
        assert isinstance(profile, dict)

    def test_has_expected_keys(self):
        from shared_utils.compression import export_cog_profile
        profile = export_cog_profile()
        assert profile['driver'] == 'GTiff'
        assert profile['compress'] == 'ZSTD'
        assert profile['tiled'] is True
        assert 'predictor' in profile
        assert 'blockxsize' in profile
        assert 'blockysize' in profile
