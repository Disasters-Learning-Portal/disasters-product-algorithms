"""Tests for shared_utils.chunk_configs module."""

import pytest


class TestGetChunkConfig:
    """Tests for get_chunk_config(file_size_gb, memory_limit_mb)."""

    def test_small_file_whole_processing(self):
        from shared_utils.chunk_configs import get_chunk_config
        config = get_chunk_config(0.5)
        assert config['use_whole_file_processing'] is True
        assert config['aggressive_gc'] is False

    def test_medium_file_fixed_chunks(self):
        from shared_utils.chunk_configs import get_chunk_config
        config = get_chunk_config(2.0)
        # Medium files (1.5-3GB) use fixed chunks
        assert config['adaptive_chunks'] is False
        assert 'default_chunk_size' in config

    def test_large_file_config(self):
        from shared_utils.chunk_configs import get_chunk_config
        config = get_chunk_config(5.0)
        assert config['adaptive_chunks'] is False
        assert config['aggressive_gc'] is True
        assert config['default_chunk_size'] == 256

    def test_ultra_large_config(self):
        from shared_utils.chunk_configs import get_chunk_config
        config = get_chunk_config(10.0)
        assert config['adaptive_chunks'] is False
        assert config['aggressive_gc'] is True
        assert config['memory_limit_mb'] == 150

    def test_expected_keys_small(self):
        from shared_utils.chunk_configs import get_chunk_config
        config = get_chunk_config(0.5)
        assert 'memory_limit_mb' in config
        assert 'show_progress' in config
        assert 'max_retries' in config

    def test_expected_keys_large(self):
        from shared_utils.chunk_configs import get_chunk_config
        config = get_chunk_config(5.0)
        assert 'default_chunk_size' in config
        assert 'memory_limit_mb' in config
        assert 'min_chunk_size' in config
        assert 'max_chunk_size' in config

    def test_boundary_small_medium(self):
        from shared_utils.chunk_configs import get_chunk_config
        # 1.5 should still be small (< 1.5 threshold uses whole file)
        config = get_chunk_config(1.5)
        assert config.get('use_whole_file_processing', False) is True

    def test_boundary_medium_large(self):
        from shared_utils.chunk_configs import get_chunk_config
        # 3.1 should be large
        config = get_chunk_config(3.1)
        assert config['memory_limit_mb'] == 250
