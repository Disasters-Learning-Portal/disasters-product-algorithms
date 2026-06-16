"""
Chunk configuration module.
Single responsibility: Chunk processing configuration.
"""


def get_chunk_config(file_size_gb=0, memory_limit_mb=500):
    """
    Get chunk configuration based on file size.

    Args:
        file_size_gb: File size in GB
        memory_limit_mb: Memory limit in MB

    Returns:
        dict: Chunk configuration
    """
    if file_size_gb > 7:
        # Ultra-large files (>7GB)
        return get_fixed_chunk_config(chunk_size=256, memory_limit_mb=150)
    elif file_size_gb > 3:
        # Large files (3-7GB)
        return get_fixed_chunk_config(chunk_size=256, memory_limit_mb=250)
    elif file_size_gb > 1.5:
        # Medium files (1.5-3GB) - use fixed chunks
        return get_fixed_chunk_config(chunk_size=512, memory_limit_mb=300)
    else:
        # Small-medium files (<1.5GB) - process whole file without chunking
        return {
            'use_whole_file_processing': True,  # Process entire file at once
            'default_chunk_size': 256,  # Fallback if whole-file fails
            'memory_limit_mb': memory_limit_mb,
            'show_progress': True,
            'enable_memory_monitoring': False,  # Not needed for whole-file
            'use_streaming': False,
            'adaptive_chunks': False,
            'aggressive_gc': False,
            'single_band_mode': False,
            'cleanup_immediate': False,
            'max_retries': 3
        }


def get_adaptive_chunk_config(memory_limit_mb=500):
    """
    Get adaptive chunk configuration.

    Returns:
        dict: Adaptive chunk configuration
    """
    return {
        'default_chunk_size': 1024,
        'memory_limit_mb': memory_limit_mb,
        'show_progress': True,
        'enable_memory_monitoring': True,
        'use_streaming': True,
        'adaptive_chunks': True,
        'aggressive_gc': False,
        'single_band_mode': False,
        'cleanup_immediate': False,
        'max_retries': 3,
        'min_chunk_size': 128,
        'max_chunk_size': 2048
    }


def get_fixed_chunk_config(chunk_size=256, memory_limit_mb=250):
    """
    Get fixed chunk configuration (prevents striping).

    Args:
        chunk_size: Fixed chunk size
        memory_limit_mb: Memory limit

    Returns:
        dict: Fixed chunk configuration
    """
    return {
        'default_chunk_size': chunk_size,
        'memory_limit_mb': memory_limit_mb,
        'show_progress': True,
        'enable_memory_monitoring': True,
        'use_streaming': False,
        'adaptive_chunks': False,  # CRITICAL: Keep chunks fixed
        'aggressive_gc': True,
        'single_band_mode': True,
        'cleanup_immediate': True,
        'max_retries': 3,
        'min_chunk_size': chunk_size,
        'max_chunk_size': chunk_size  # Force same size
    }


def get_memory_safe_config():
    """
    Get memory-safe configuration for limited memory environments.

    Returns:
        dict: Memory-safe configuration
    """
    return {
        'default_chunk_size': 128,
        'memory_limit_mb': 100,
        'show_progress': True,
        'enable_memory_monitoring': True,
        'use_streaming': False,
        'adaptive_chunks': False,
        'aggressive_gc': True,
        'single_band_mode': True,
        'cleanup_immediate': True,
        'max_retries': 5,
        'min_chunk_size': 128,
        'max_chunk_size': 128
    }