"""Tests for shared_utils.gdal_cog_processor module (pure functions only)."""

import pytest


class TestGetResamplingForDtype:
    """Tests for get_resampling_for_dtype(dtype)."""

    def test_float32_bilinear(self):
        from shared_utils.gdal_cog_processor import get_resampling_for_dtype
        method, overview = get_resampling_for_dtype('float32')
        assert method == 'bilinear'
        assert overview == 'average'

    def test_uint8_nearest(self):
        from shared_utils.gdal_cog_processor import get_resampling_for_dtype
        method, overview = get_resampling_for_dtype('uint8')
        assert method == 'nearest'
        assert overview == 'mode'

    def test_int16_bilinear(self):
        from shared_utils.gdal_cog_processor import get_resampling_for_dtype
        method, overview = get_resampling_for_dtype('int16')
        assert method == 'bilinear'
        assert overview == 'average'

    def test_float64_bilinear(self):
        from shared_utils.gdal_cog_processor import get_resampling_for_dtype
        method, overview = get_resampling_for_dtype('float64')
        assert method == 'bilinear'
        assert overview == 'average'

    def test_unknown_type_defaults(self):
        from shared_utils.gdal_cog_processor import get_resampling_for_dtype
        method, overview = get_resampling_for_dtype('sometype')
        assert method == 'bilinear'
        assert overview == 'average'


class TestSetOptimalGdalEnv:
    """Tests for set_optimal_gdal_env()."""

    def test_returns_dict(self):
        from shared_utils.gdal_cog_processor import set_optimal_gdal_env
        env = set_optimal_gdal_env()
        assert isinstance(env, dict)

    def test_has_num_threads(self):
        from shared_utils.gdal_cog_processor import set_optimal_gdal_env
        env = set_optimal_gdal_env()
        assert env['GDAL_NUM_THREADS'] == 'ALL_CPUS'

    def test_has_cache_max(self):
        from shared_utils.gdal_cog_processor import set_optimal_gdal_env
        env = set_optimal_gdal_env()
        assert 'GDAL_CACHEMAX' in env

    def test_has_temp_dir(self):
        from shared_utils.gdal_cog_processor import set_optimal_gdal_env
        env = set_optimal_gdal_env()
        assert 'CPL_TMPDIR' in env

    def test_has_vsi_cache(self):
        from shared_utils.gdal_cog_processor import set_optimal_gdal_env
        env = set_optimal_gdal_env()
        assert env['VSI_CACHE'] == 'TRUE'
