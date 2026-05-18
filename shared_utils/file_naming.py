"""
File naming module - handles filename creation and parsing.
Single responsibility: File naming conventions and date handling.

Pure Python (no GDAL/rasterio dependency) so it can be imported from any
notebook style — CLI-subprocess notebooks, Python-API notebooks, and class
wrappers like SimpleProcessor.
"""

import os
import re
from datetime import datetime
from typing import Dict, Optional, Tuple


# Ordered most-specific -> least-specific. First match wins.
# Each entry: (regex, granularity) where granularity ∈ {'hour', 'day'}.
DATETIME_PATTERNS = [
    (r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z?', 'hour'),  # 2025-01-11T19:46:16Z
    (r'\d{8}T\d{6}Z?',                          'hour'),  # 20250111T194616Z
    (r'\d{4}-\d{2}-\d{2}T\d{2}',                'hour'),  # 2025-01-11T19
    (r'\d{8}T\d{2}',                            'hour'),  # 20250111T19
    (r'\d{4}-\d{2}-\d{2}',                      'day'),   # 2025-01-11
    (r'\d{8}',                                  'day'),   # 20250111
]


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


# ---------------------------------------------------------------------------
# Unified categorization / filename API (new — used by notebooks/*.ipynb).
#
# The functions below replace the inline DATETIME_PATTERNS / CATEGORIES /
# create_output_filename logic that used to be duplicated across the local-
# file-processing templates and the SimpleProcessor wrapper. The legacy
# helpers above (extract_date_from_filename, create_cog_filename, etc.) are
# preserved for backwards compatibility with shared_utils_reference.ipynb and
# the unit tests.
# ---------------------------------------------------------------------------

def extract_datetime_from_filename(filename: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Find the first datetime-like substring in a filename.

    Returns:
        (matched_string, granularity) where granularity is 'hour' or 'day',
        or (None, None) if no DATETIME_PATTERNS entry matches.
    """
    for pattern, granularity in DATETIME_PATTERNS:
        m = re.search(pattern, filename)
        if m:
            return m.group(0), granularity
    return None, None


def categorize_file(filename: str, categories: Dict[str, str]) -> str:
    """
    Match `filename` against a `categories` dict (regex pattern -> S3 subdir).

    Returns the matching subdirectory string, or 'uncategorized' if no
    pattern matches. Case-insensitive.
    """
    for pattern, directory in categories.items():
        if re.search(pattern, filename, re.IGNORECASE):
            return directory
    return 'uncategorized'


def no_change(original_path: str, event_name: str) -> str:
    """
    Pass-through filename builder: prepend the event name, preserve stem + ext.

    Used for sub-products (e.g. AVIRIS) whose internal datetime ranges should
    not be rewritten.
    """
    filename = os.path.basename(original_path)
    stem, ext = os.path.splitext(filename)
    return f"{event_name}_{stem}{ext}"


def create_output_filename(
    original_path: str,
    event_name: str,
    categories: Optional[Dict[str, str]] = None,
    passthrough_categories: Tuple[str, ...] = ('AVIRIS',),
) -> str:
    """
    Build a standardized output filename for a disaster product.

    Behavior:
        - If `categories` is supplied AND the file's matched category starts
          with any entry in `passthrough_categories`, falls back to no_change.
        - Otherwise, extracts the first datetime substring (see
          DATETIME_PATTERNS), strips it from the stem, and rebuilds the name
          as `{event_name}_{stem_clean}_{datetime}_{granularity}.tif`.
        - If no datetime is found, returns `{event_name}_{stem}_day.tif`.
    """
    filename = os.path.basename(original_path)

    if categories is not None:
        category = categorize_file(filename, categories)
        for passthrough in passthrough_categories:
            if category.startswith(passthrough):
                return no_change(original_path, event_name)

    stem = os.path.splitext(filename)[0]
    matched, granularity = extract_datetime_from_filename(stem)
    if matched:
        stem_clean = re.sub(r'_?' + re.escape(matched), '', stem, count=1)
        stem_clean = stem_clean.strip('_')
        # Normalize raw YYYYMMDD -> YYYY-MM-DD so the embedded date matches
        # the legacy operator-facing convention (the old per-notebook helpers
        # ran `convert_date(...)` on the bare 8-digit string).
        embedded = matched
        if granularity == 'day' and len(matched) == 8 and matched.isdigit():
            embedded = f"{matched[0:4]}-{matched[4:6]}-{matched[6:8]}"
        return f"{event_name}_{stem_clean}_{embedded}_{granularity}.tif"
    return f"{event_name}_{stem}_day.tif"