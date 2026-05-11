"""
Sentinel-2 Processing Module
============================

Process Sentinel-2 L1C/L2A data to generate various satellite imagery products
including true color, natural color, SWIR, color infrared, NDVI, NDWI, MNDWI, NBR, and water extent.
"""

from sentinel2.sentinel2_functions import *

__all__ = [
    'gen_true_color',
    'gen_natural_color',
    'gen_swir',
    'gen_color_infrared',
    'gen_ndvi',
    'gen_ndwi',
    'gen_mndwi',
    'gen_nbr',
    'gen_cloudMask',
    'gen_water_extent',
    'gen_merge',
    's2_merge',
]
