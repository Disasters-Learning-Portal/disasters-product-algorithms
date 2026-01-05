"""
Disasters Product Algorithms
============================

Unified package for processing satellite imagery from Landsat 8/9 and Sentinel-2
to generate various disaster response products.

Usage:
    Command line:
        process_landsat89 <input_dir> [options]
        process_sentinel2 <input_dir> [options]

    Or import functions in your code:
        from landsat import genTrueColor, genNdvi, etc.
        from sentinel import gen_true_color, gen_ndvi, etc.
        from shared_utils import convert_to_cog, etc.
"""

__version__ = "0.1.0"

# Make submodules available
from disasters_product_algorithms import landsat
from disasters_product_algorithms import sentinel
from disasters_product_algorithms import shared_utils

__all__ = ['landsat', 'sentinel', 'shared_utils']
