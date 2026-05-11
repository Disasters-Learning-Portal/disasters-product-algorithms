"""
Memory management module - handles memory monitoring and optimization.
Single responsibility: Memory usage tracking and chunk size optimization.
"""

import psutil
import gc


def get_memory_usage():
    """
    Get current memory usage in MB.

    Returns:
        float: Current memory usage in MB
    """
    process = psutil.Process()
    return process.memory_info().rss / (1024 * 1024)


def get_available_memory_mb():
    """
    Get available system memory in MB.

    Returns:
        float: Available memory in MB
    """
    return psutil.virtual_memory().available / (1024 * 1024)


def calculate_optimal_chunk_size(width, height, bands, dtype_size, target_memory_mb=500):
    """
    Calculate optimal chunk size based on available memory.

    Args:
        width: Image width
        height: Image height
        bands: Number of bands
        dtype_size: Size of data type in bytes
        target_memory_mb: Target memory usage in MB

    Returns:
        int: Optimal chunk size
    """
    # Calculate bytes per pixel
    bytes_per_pixel = bands * dtype_size

    # Calculate target bytes
    target_bytes = target_memory_mb * 1024 * 1024

    # Calculate chunk size that fits in memory
    pixels_in_chunk = target_bytes / bytes_per_pixel
    chunk_size = int((pixels_in_chunk ** 0.5))

    # Ensure reasonable bounds
    chunk_size = max(128, min(4096, chunk_size))

    # Round to multiple of 128 for better alignment
    chunk_size = (chunk_size // 128) * 128

    return chunk_size


def estimate_chunk_memory(chunk_size, bands, dtype_size):
    """
    Estimate memory usage for a chunk.

    Args:
        chunk_size: Size of chunk in pixels (assumes square)
        bands: Number of bands
        dtype_size: Size of data type in bytes

    Returns:
        float: Estimated memory usage in MB
    """
    bytes_needed = chunk_size * chunk_size * bands * dtype_size
    return bytes_needed / (1024 * 1024)


def format_bytes(bytes_value):
    """
    Format bytes to human readable string.

    Args:
        bytes_value: Number of bytes

    Returns:
        str: Formatted string
    """
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_value < 1024.0:
            return f"{bytes_value:.2f} {unit}"
        bytes_value /= 1024.0
    return f"{bytes_value:.2f} PB"


def monitor_memory(threshold_mb=1000, force_gc=True):
    """
    Monitor memory usage and trigger garbage collection if needed.

    Args:
        threshold_mb: Memory threshold in MB
        force_gc: Force garbage collection if over threshold

    Returns:
        dict: Memory statistics
    """
    current_mb = get_memory_usage()
    available_mb = get_available_memory_mb()

    stats = {
        'current_mb': current_mb,
        'available_mb': available_mb,
        'over_threshold': current_mb > threshold_mb
    }

    if stats['over_threshold'] and force_gc:
        print(f"   [MEMORY] High usage detected ({current_mb:.1f} MB), forcing garbage collection...")
        gc.collect()
        stats['after_gc_mb'] = get_memory_usage()
        stats['freed_mb'] = current_mb - stats['after_gc_mb']
        print(f"   [MEMORY] Freed {stats['freed_mb']:.1f} MB")

    return stats


def get_dtype_size(dtype_str):
    """
    Get size of data type in bytes.

    Args:
        dtype_str: String representation of dtype

    Returns:
        int: Size in bytes
    """
    dtype_sizes = {
        'uint8': 1,
        'int8': 1,
        'uint16': 2,
        'int16': 2,
        'uint32': 4,
        'int32': 4,
        'float32': 4,
        'float64': 8
    }
    return dtype_sizes.get(str(dtype_str), 4)