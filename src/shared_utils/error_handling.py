"""
Error handling module - handles error recovery and cleanup.
Single responsibility: Error management and recovery strategies.
"""

import os
import tempfile
import shutil
import traceback


def handle_chunk_error(error, chunk_info, verbose=True):
    """
    Handle errors that occur during chunk processing.

    Args:
        error: The exception that occurred
        chunk_info: Dictionary with chunk information
        verbose: Print detailed error messages

    Returns:
        str: Error category (streaming, memory, unknown)
    """
    error_str = str(error).lower()

    # Categorize error
    if "chunk and warp" in error_str:
        category = "streaming"
        if verbose:
            print(f"   [ERROR] GDAL streaming error at chunk {chunk_info}")

    elif "memory" in error_str or isinstance(error, MemoryError):
        category = "memory"
        if verbose:
            print(f"   [ERROR] Memory error at chunk {chunk_info}")

    elif "curl" in error_str or "vsi" in error_str:
        category = "network"
        if verbose:
            print(f"   [ERROR] Network/S3 error at chunk {chunk_info}")

    else:
        category = "unknown"
        if verbose:
            print(f"   [ERROR] Unknown error at chunk {chunk_info}: {error}")

    # Print traceback in debug mode
    if verbose and category == "unknown":
        traceback.print_exc()

    return category


def retry_with_download(func, *args, **kwargs):
    """
    Retry a function with download mode if streaming fails.

    Args:
        func: Function to retry
        *args: Function arguments
        **kwargs: Function keyword arguments

    Returns:
        Function result or raises exception
    """
    try:
        # First attempt with streaming
        return func(*args, **kwargs)

    except Exception as e:
        if "STREAMING_CHUNK_ERROR" in str(e):
            print(f"   [RETRY] Streaming failed, retrying with download...")

            # Modify kwargs to disable streaming
            if 'chunk_config' in kwargs:
                kwargs['chunk_config'] = kwargs['chunk_config'].copy()
                kwargs['chunk_config']['use_streaming'] = False

            # Retry with download
            return func(*args, **kwargs)
        else:
            raise


def cleanup_temp_files(*file_paths):
    """
    Clean up temporary files.

    Args:
        *file_paths: Variable number of file paths to clean up

    Returns:
        int: Number of files cleaned up
    """
    cleaned = 0

    for file_path in file_paths:
        if file_path and os.path.exists(file_path):
            try:
                if os.path.isdir(file_path):
                    shutil.rmtree(file_path)
                else:
                    os.remove(file_path)
                cleaned += 1
                print(f"   [CLEANUP] Removed: {file_path}")
            except Exception as e:
                print(f"   [CLEANUP] Failed to remove {file_path}: {e}")

    return cleaned


def setup_temp_directory(preferred_dir=None):
    """
    Setup temporary directory for processing.

    Args:
        preferred_dir: Preferred directory path

    Returns:
        str: Path to temporary directory
    """
    if preferred_dir:
        os.makedirs(preferred_dir, exist_ok=True)
        tempfile.tempdir = preferred_dir
        return preferred_dir

    # Use /tmp as the base for all temporary files
    temp_dir = os.environ.get('COG_TEMP_DIR', '/tmp')
    os.makedirs(temp_dir, exist_ok=True)
    tempfile.tempdir = temp_dir
    print(f"   [TEMP] Using temp directory: {temp_dir}")
    return temp_dir


def create_error_report(errors_list):
    """
    Create a summary report of errors.

    Args:
        errors_list: List of error dictionaries

    Returns:
        dict: Error summary
    """
    report = {
        'total_errors': len(errors_list),
        'by_category': {},
        'files_affected': set()
    }

    for error in errors_list:
        category = error.get('category', 'unknown')
        if category not in report['by_category']:
            report['by_category'][category] = 0
        report['by_category'][category] += 1

        if 'file' in error:
            report['files_affected'].add(error['file'])

    report['files_affected'] = list(report['files_affected'])

    return report