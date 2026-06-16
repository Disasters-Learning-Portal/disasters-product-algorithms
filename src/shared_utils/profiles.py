"""
Profile configurations for COG processing.
Single responsibility: Configuration profiles management.
"""


def get_compression_profile(dtype='float32', file_size_gb=0):
    """
    Get compression profile based on data type and file size.

    Args:
        dtype: Data type
        file_size_gb: File size in GB

    Returns:
        dict: Compression profile
    """
    # Base profile
    profile = {
        'compress': 'zstd',
        'zstd_level': 22,
        'tiled': True,
        'num_threads': 'ALL_CPUS'
    }

    # Set predictor based on dtype
    if dtype in ['float32', 'float64']:
        profile['predictor'] = 3
    elif dtype in ['uint8', 'uint16', 'int16', 'int32']:
        profile['predictor'] = 2
    else:
        profile['predictor'] = 1

    # Adjust block size for large files
    if file_size_gb > 10:
        profile['blockxsize'] = 256
        profile['blockysize'] = 256
    else:
        profile['blockxsize'] = 512
        profile['blockysize'] = 512

    # Set bigtiff
    profile['bigtiff'] = 'YES' if file_size_gb > 3 else 'IF_SAFER'

    return profile


def get_standard_profile():
    """
    Get standard processing profile for files < 3GB.

    Returns:
        dict: Standard profile
    """
    return {
        'chunk_size': 1024,
        'memory_limit_mb': 500,
        'show_progress': True,
        'enable_memory_monitoring': True,
        'use_streaming': True,
        'adaptive_chunks': True,
        'aggressive_gc': False,
        'single_band_mode': False,
        'cleanup_immediate': False,
        'max_retries': 3
    }


def get_large_file_profile():
    """
    Get profile for large files (3-7 GB).

    Returns:
        dict: Large file profile
    """
    return {
        'chunk_size': 256,
        'memory_limit_mb': 250,
        'show_progress': True,
        'enable_memory_monitoring': True,
        'use_streaming': False,
        'adaptive_chunks': False,  # Use fixed chunks
        'aggressive_gc': True,
        'single_band_mode': True,
        'cleanup_immediate': True,
        'max_retries': 3,
        'min_chunk_size': 256,
        'max_chunk_size': 256
    }


def get_ultra_large_profile():
    """
    Get profile for ultra-large files (> 7GB).

    Returns:
        dict: Ultra-large file profile
    """
    return {
        'chunk_size': 256,  # Small fixed chunks
        'memory_limit_mb': 150,
        'show_progress': True,
        'enable_memory_monitoring': True,
        'use_streaming': False,
        'adaptive_chunks': False,  # Always use fixed chunks
        'aggressive_gc': True,
        'single_band_mode': True,
        'cleanup_immediate': True,
        'max_retries': 5,
        'min_chunk_size': 256,
        'max_chunk_size': 256
    }


def select_profile_by_size(file_size_gb):
    """
    Select appropriate profile based on file size.

    Args:
        file_size_gb: File size in GB

    Returns:
        dict: Selected profile
    """
    if file_size_gb > 7:
        return get_ultra_large_profile()
    elif file_size_gb > 3:
        return get_large_file_profile()
    else:
        return get_standard_profile()