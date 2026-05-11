"""
File naming module - handles filename creation and parsing.
Single responsibility: File naming conventions and date handling.
"""

import os
import re
from datetime import datetime


def convert_date(date_str):
    """
    Convert date string from YYYYMMDD to YYYY-MM-DD format.

    Args:
        date_str: String like '20250731'

    Returns:
        String like '2025-07-31'
    """
    if len(date_str) != 8:
        return date_str

    year = date_str[0:4]
    month = date_str[4:6]
    day = date_str[6:8]

    return f"{year}-{month}-{day}"


def extract_date_from_filename(filename):
    """
    Extract date from filename.

    Args:
        filename: Filename containing date

    Returns:
        str: Date in YYYY-MM-DD format or None
    """
    # Find 8-digit date pattern
    dates = re.findall(r'\d{8}', filename)

    if dates:
        return convert_date(dates[0])

    return None


def parse_filename_components(filepath):
    """
    Parse filename into components.

    Args:
        filepath: Full file path

    Returns:
        dict: Dictionary with filename components
    """
    directory, filename = os.path.split(filepath)
    stem, ext = os.path.splitext(filename)

    # Extract common components
    components = {
        'directory': directory,
        'filename': filename,
        'stem': stem,
        'extension': ext
    }

    # Extract date if present
    date_str = extract_date_from_filename(stem)
    if date_str:
        components['date'] = date_str

    # Extract satellite info (S1, S2, etc.)
    satellite_match = re.search(r'S[12][ABC]?', stem)
    if satellite_match:
        components['satellite'] = satellite_match.group()

    # Extract product type (any uppercase word or camelCase pattern)
    # This will match patterns like NDVI, MNDWI, RGB, SAR, DEM, etc.
    product_patterns = [
        r'([A-Z]{2,})',           # Uppercase acronyms (NDVI, RGB, SAR, etc.)
        r'([a-z]+[A-Z][a-zA-Z]+)' # camelCase patterns (trueColor, etc.)
    ]

    for pattern in product_patterns:
        match = re.search(pattern, stem)
        if match:
            components['product'] = match.group(1)
            break

    # Extract location codes (3-letter codes)
    location_match = re.search(r'\b[A-Z]{3}\b', stem)
    if location_match:
        components['location'] = location_match.group()

    return components


def create_cog_filename(original_path, event_name, custom_suffix='day'):
    """
    Create standardized COG filename.

    Args:
        original_path: Original file path
        event_name: Event name for prefix
        custom_suffix: Optional suffix (default: 'day')

    Returns:
        str: Standardized COG filename
    """
    # Parse components
    components = parse_filename_components(original_path)

    # Extract parts
    stem = components['stem']
    date = components.get('date', '')

    # Find all dates and remove them from stem
    stem_clean = re.sub(r'_?\d{8}', '', stem)

    # Build new filename
    if date:
        cog_filename = f"{event_name}_{stem_clean}_{date}_{custom_suffix}.tif"
    else:
        cog_filename = f"{event_name}_{stem_clean}_{custom_suffix}.tif"

    # Clean up double underscores
    cog_filename = re.sub(r'_+', '_', cog_filename)

    return cog_filename


def create_output_path(base_dir, target_dir, filename):
    """
    Create full output path.

    Args:
        base_dir: Base directory (e.g., 'drcs_activations_new')
        target_dir: Target subdirectory (e.g., 'Sentinel-2/NDVI')
        filename: Output filename

    Returns:
        str: Full output path
    """
    return os.path.join(base_dir, target_dir, filename)