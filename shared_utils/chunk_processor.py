"""
Chunk processor module - handles chunked data processing.
Single responsibility: Chunk-based processing logic.
"""

import numpy as np
from rasterio.windows import Window
import gc


def process_single_chunk(data_source, chunk_window, processing_func, **kwargs):
    """
    Process a single chunk of data.

    Args:
        data_source: Source data (rasterio band or array)
        chunk_window: Window defining the chunk
        processing_func: Function to apply to chunk
        **kwargs: Additional arguments for processing function

    Returns:
        Processed chunk data
    """
    try:
        # Read chunk data
        if hasattr(data_source, 'read'):
            # Rasterio dataset
            chunk_data = data_source.read(window=chunk_window)
        else:
            # Numpy array
            chunk_data = data_source[
                chunk_window.row_off:chunk_window.row_off + chunk_window.height,
                chunk_window.col_off:chunk_window.col_off + chunk_window.width
            ]

        # Apply processing function
        if processing_func:
            chunk_data = processing_func(chunk_data, **kwargs)

        return chunk_data

    except Exception as e:
        print(f"   [CHUNK] Error processing chunk at {chunk_window}: {e}")
        return None


def process_band_with_chunks(src_band, dst_band, chunk_size, processing_func=None, verbose=True):
    """
    Process an entire band using chunks.

    Args:
        src_band: Source band
        dst_band: Destination band
        chunk_size: Size of chunks
        processing_func: Optional processing function
        verbose: Print progress

    Returns:
        bool: True if successful
    """
    try:
        width = src_band.width if hasattr(src_band, 'width') else src_band.shape[1]
        height = src_band.height if hasattr(src_band, 'height') else src_band.shape[0]

        total_chunks = 0
        processed_chunks = 0

        for y in range(0, height, chunk_size):
            for x in range(0, width, chunk_size):
                # Define chunk window
                win_width = min(chunk_size, width - x)
                win_height = min(chunk_size, height - y)
                window = Window(x, y, win_width, win_height)

                total_chunks += 1

                # Process chunk
                chunk_data = process_single_chunk(
                    src_band, window, processing_func
                )

                if chunk_data is not None:
                    # Write to destination
                    if hasattr(dst_band, 'write'):
                        dst_band.write(chunk_data, window=window)
                    else:
                        dst_band[y:y+win_height, x:x+win_width] = chunk_data

                    processed_chunks += 1

                # Memory management
                del chunk_data
                gc.collect()

        if verbose:
            print(f"   [CHUNKS] Processed {processed_chunks}/{total_chunks} chunks successfully")

        return processed_chunks == total_chunks

    except Exception as e:
        print(f"   [CHUNKS] Error in band processing: {e}")
        return False


def maintain_chunk_alignment(x, y, chunk_size, grid_origin=(0, 0)):
    """
    Maintain consistent chunk grid alignment.

    Args:
        x: X coordinate
        y: Y coordinate
        chunk_size: Chunk size
        grid_origin: Grid origin point

    Returns:
        tuple: Aligned (x, y) coordinates
    """
    # Align to chunk grid
    aligned_x = ((x - grid_origin[0]) // chunk_size) * chunk_size + grid_origin[0]
    aligned_y = ((y - grid_origin[1]) // chunk_size) * chunk_size + grid_origin[1]

    return aligned_x, aligned_y


def calculate_chunk_grid(width, height, chunk_size):
    """
    Calculate the chunk grid for an image.

    Args:
        width: Image width
        height: Image height
        chunk_size: Chunk size

    Returns:
        dict: Grid information
    """
    grid = {
        'chunk_size': chunk_size,
        'chunks_x': (width + chunk_size - 1) // chunk_size,
        'chunks_y': (height + chunk_size - 1) // chunk_size,
        'total_chunks': 0,
        'chunks': []
    }

    grid['total_chunks'] = grid['chunks_x'] * grid['chunks_y']

    # Generate chunk list
    for y_idx in range(grid['chunks_y']):
        for x_idx in range(grid['chunks_x']):
            x = x_idx * chunk_size
            y = y_idx * chunk_size
            w = min(chunk_size, width - x)
            h = min(chunk_size, height - y)

            grid['chunks'].append({
                'index': (x_idx, y_idx),
                'window': Window(x, y, w, h),
                'coords': (x, y, w, h)
            })

    return grid