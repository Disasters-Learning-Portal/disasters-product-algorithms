"""Tests for shared_utils.cog_validation module."""

import pytest
import numpy as np


class TestCheckAndFixNanValues:
    """Tests for check_and_fix_nan_values(data, nodata, dtype, band_idx, verbose)."""

    def test_nan_replacement(self):
        from shared_utils.cog_validation import check_and_fix_nan_values
        data = np.array([1.0, np.nan, 3.0, np.nan, 5.0], dtype=np.float32)
        fixed, had_nan = check_and_fix_nan_values(data, -9999.0, np.float32)
        assert had_nan is True
        assert not np.any(np.isnan(fixed))
        assert fixed[1] == -9999.0
        assert fixed[3] == -9999.0

    def test_inf_replacement(self):
        from shared_utils.cog_validation import check_and_fix_nan_values
        data = np.array([1.0, np.inf, 3.0, -np.inf], dtype=np.float32)
        fixed, had_nan = check_and_fix_nan_values(data, -9999.0, np.float32)
        assert had_nan is True
        assert not np.any(np.isinf(fixed))
        assert fixed[1] == -9999.0
        assert fixed[3] == -9999.0

    def test_clean_data_passthrough(self):
        from shared_utils.cog_validation import check_and_fix_nan_values
        data = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        fixed, had_nan = check_and_fix_nan_values(data, -9999.0, np.float32)
        assert had_nan is False
        np.testing.assert_array_equal(fixed, data)

    def test_integer_data_no_nan_check(self):
        from shared_utils.cog_validation import check_and_fix_nan_values
        data = np.array([1, 2, 3], dtype=np.int16)
        fixed, had_nan = check_and_fix_nan_values(data, -9999, np.int16)
        assert had_nan is False
        np.testing.assert_array_equal(fixed, data)

    def test_mixed_nan_and_inf(self):
        from shared_utils.cog_validation import check_and_fix_nan_values
        data = np.array([np.nan, np.inf, 3.0, -np.inf, np.nan], dtype=np.float64)
        fixed, had_nan = check_and_fix_nan_values(data, -9999.0, np.float64)
        assert had_nan is True
        assert not np.any(np.isnan(fixed))
        assert not np.any(np.isinf(fixed))


class TestValidateDataIntegrity:
    """Tests for validate_data_integrity(data, ...)."""

    def test_returns_dict_with_expected_keys(self):
        from shared_utils.cog_validation import validate_data_integrity
        data = np.random.rand(10, 10).astype(np.float32)
        result = validate_data_integrity(data, verbose=False)
        assert 'valid' in result
        assert 'issues' in result
        assert 'stats' in result

    def test_stats_contain_expected_fields(self):
        from shared_utils.cog_validation import validate_data_integrity
        data = np.random.rand(10, 10).astype(np.float32)
        result = validate_data_integrity(data, verbose=False)
        stats = result['stats']
        assert 'shape' in stats
        assert 'dtype' in stats
        assert 'min' in stats
        assert 'max' in stats
        assert 'mean' in stats
        assert 'has_nan' in stats
        assert 'has_inf' in stats

    def test_clean_data_is_valid(self):
        from shared_utils.cog_validation import validate_data_integrity
        data = np.random.rand(10, 10).astype(np.float32)
        result = validate_data_integrity(data, verbose=False)
        assert result['valid'] is True

    def test_nan_data_flagged(self):
        from shared_utils.cog_validation import validate_data_integrity
        data = np.array([1.0, np.nan, 3.0], dtype=np.float32)
        result = validate_data_integrity(data, verbose=False)
        assert result['stats']['has_nan'] is True
        assert any('NaN' in issue for issue in result['issues'])

    def test_inf_data_flagged(self):
        from shared_utils.cog_validation import validate_data_integrity
        data = np.array([1.0, np.inf, 3.0], dtype=np.float32)
        result = validate_data_integrity(data, verbose=False)
        assert result['stats']['has_inf'] is True

    def test_shape_mismatch_detected(self):
        from shared_utils.cog_validation import validate_data_integrity
        data = np.random.rand(10, 10).astype(np.float32)
        result = validate_data_integrity(data, expected_shape=(5, 5), verbose=False)
        assert result['valid'] is False
        assert any('Shape' in issue or 'shape' in issue.lower() for issue in result['issues'])

    def test_constant_data_flagged(self):
        from shared_utils.cog_validation import validate_data_integrity
        data = np.full((10, 10), 42.0, dtype=np.float32)
        result = validate_data_integrity(data, verbose=False)
        assert any('identical' in issue.lower() for issue in result['issues'])
