"""Tests for shared_utils.memory_management module."""

import pytest

psutil = pytest.importorskip("psutil")


class TestFormatBytes:
    """Tests for format_bytes(bytes_value)."""

    def test_megabytes(self):
        from shared_utils.memory_management import format_bytes
        assert format_bytes(1048576) == "1.00 MB"

    def test_gigabytes(self):
        from shared_utils.memory_management import format_bytes
        assert format_bytes(1073741824) == "1.00 GB"

    def test_kilobytes(self):
        from shared_utils.memory_management import format_bytes
        assert format_bytes(1024) == "1.00 KB"

    def test_bytes(self):
        from shared_utils.memory_management import format_bytes
        assert format_bytes(500) == "500.00 B"

    def test_terabytes(self):
        from shared_utils.memory_management import format_bytes
        result = format_bytes(1024 ** 4)
        assert "1.00 TB" == result


class TestGetDtypeSize:
    """Tests for get_dtype_size(dtype_str)."""

    def test_float32(self):
        from shared_utils.memory_management import get_dtype_size
        assert get_dtype_size('float32') == 4

    def test_uint8(self):
        from shared_utils.memory_management import get_dtype_size
        assert get_dtype_size('uint8') == 1

    def test_float64(self):
        from shared_utils.memory_management import get_dtype_size
        assert get_dtype_size('float64') == 8

    def test_int16(self):
        from shared_utils.memory_management import get_dtype_size
        assert get_dtype_size('int16') == 2

    def test_unknown_defaults_to_4(self):
        from shared_utils.memory_management import get_dtype_size
        assert get_dtype_size('complex128') == 4


class TestGetMemoryUsage:
    """Tests for get_memory_usage()."""

    def test_returns_positive_float(self):
        from shared_utils.memory_management import get_memory_usage
        usage = get_memory_usage()
        assert isinstance(usage, float)
        assert usage > 0


class TestEstimateChunkMemory:
    """Tests for estimate_chunk_memory(chunk_size, bands, dtype_size)."""

    def test_returns_positive_float(self):
        from shared_utils.memory_management import estimate_chunk_memory
        result = estimate_chunk_memory(256, 3, 4)
        assert isinstance(result, float)
        assert result > 0

    def test_known_value(self):
        from shared_utils.memory_management import estimate_chunk_memory
        # 256 * 256 * 3 * 4 = 786432 bytes = 0.75 MB
        result = estimate_chunk_memory(256, 3, 4)
        assert abs(result - 0.75) < 0.01

    def test_single_band_uint8(self):
        from shared_utils.memory_management import estimate_chunk_memory
        # 512 * 512 * 1 * 1 = 262144 bytes = 0.25 MB
        result = estimate_chunk_memory(512, 1, 1)
        assert abs(result - 0.25) < 0.01


class TestCalculateOptimalChunkSize:
    """Tests for calculate_optimal_chunk_size(width, height, bands, dtype_size, target_memory_mb)."""

    def test_returns_int(self):
        from shared_utils.memory_management import calculate_optimal_chunk_size
        result = calculate_optimal_chunk_size(1000, 1000, 3, 4)
        assert isinstance(result, int)

    def test_within_bounds(self):
        from shared_utils.memory_management import calculate_optimal_chunk_size
        result = calculate_optimal_chunk_size(1000, 1000, 3, 4)
        assert 128 <= result <= 4096

    def test_multiple_of_128(self):
        from shared_utils.memory_management import calculate_optimal_chunk_size
        result = calculate_optimal_chunk_size(1000, 1000, 3, 4)
        assert result % 128 == 0

    def test_smaller_target_gives_smaller_chunks(self):
        from shared_utils.memory_management import calculate_optimal_chunk_size
        small = calculate_optimal_chunk_size(10000, 10000, 3, 4, target_memory_mb=100)
        large = calculate_optimal_chunk_size(10000, 10000, 3, 4, target_memory_mb=2000)
        assert small <= large
