"""Tests for shared_utils.profiles module."""

import pytest


class TestGetCompressionProfile:
    """Tests for get_compression_profile(dtype, file_size_gb)."""

    def test_returns_dict(self):
        from shared_utils.profiles import get_compression_profile
        profile = get_compression_profile()
        assert isinstance(profile, dict)

    def test_has_expected_keys(self):
        from shared_utils.profiles import get_compression_profile
        profile = get_compression_profile()
        assert 'compress' in profile
        assert 'predictor' in profile
        assert 'tiled' in profile
        assert 'blockxsize' in profile
        assert 'blockysize' in profile

    def test_float_predictor(self):
        from shared_utils.profiles import get_compression_profile
        profile = get_compression_profile(dtype='float32')
        assert profile['predictor'] == 3

    def test_int_predictor(self):
        from shared_utils.profiles import get_compression_profile
        profile = get_compression_profile(dtype='uint8')
        assert profile['predictor'] == 2

    def test_large_file_small_blocks(self):
        from shared_utils.profiles import get_compression_profile
        profile = get_compression_profile(file_size_gb=15.0)
        assert profile['blockxsize'] == 256

    def test_large_file_bigtiff(self):
        from shared_utils.profiles import get_compression_profile
        profile = get_compression_profile(file_size_gb=5.0)
        assert profile['bigtiff'] == 'YES'


class TestSelectProfileBySize:
    """Tests for select_profile_by_size(file_size_gb)."""

    def test_small_file_standard(self):
        from shared_utils.profiles import select_profile_by_size
        profile = select_profile_by_size(0.5)
        # Standard profile has streaming enabled
        assert profile['use_streaming'] is True
        assert profile['adaptive_chunks'] is True

    def test_large_file_profile(self):
        from shared_utils.profiles import select_profile_by_size
        profile = select_profile_by_size(5.0)
        # Large file profile has aggressive GC
        assert profile['aggressive_gc'] is True
        assert profile['single_band_mode'] is True

    def test_ultra_large_profile(self):
        from shared_utils.profiles import select_profile_by_size
        profile = select_profile_by_size(10.0)
        # Ultra-large profile has max retries = 5
        assert profile['max_retries'] == 5
        assert profile['aggressive_gc'] is True
        assert profile['single_band_mode'] is True

    def test_boundary_standard(self):
        from shared_utils.profiles import select_profile_by_size
        # Exactly 3.0 should still be standard
        profile = select_profile_by_size(3.0)
        assert profile['use_streaming'] is True

    def test_boundary_large(self):
        from shared_utils.profiles import select_profile_by_size
        # Exactly 7.0 should still be large
        profile = select_profile_by_size(7.0)
        assert profile['aggressive_gc'] is True
        # But not ultra-large max_retries
        assert profile['max_retries'] == 3
