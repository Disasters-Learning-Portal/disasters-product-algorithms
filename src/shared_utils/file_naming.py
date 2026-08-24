"""
File naming module - handles filename creation and parsing.
Single responsibility: File naming conventions and date handling.

Pure Python (no GDAL/rasterio dependency) so it can be imported from any
notebook style — CLI-subprocess notebooks, Python-API notebooks, and class
wrappers like SimpleProcessor.
"""

import os
import re
from typing import Dict, Optional, Tuple


# Ordered most-specific -> least-specific. First match wins.
# Each entry: (regex, granularity) where granularity ∈ {'hour', 'day'}.
#
# The mixed form (hyphenated date + compact time) is load-bearing: vendor
# deliveries ship it, and without its own entry the `YYYY-MM-DDTHH` pattern
# below matches a PREFIX of it, splitting the stamp mid-token (2026-08-12T12
# matched, "3721Z" left welded to the product name).
DATETIME_PATTERNS = [
    (r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z?', 'hour'),  # 2025-01-11T19:46:16Z
    (r'\d{4}-\d{2}-\d{2}T\d{6}Z?',              'hour'),  # 2025-01-11T194616Z
    (r'\d{8}T\d{6}Z?',                          'hour'),  # 20250111T194616Z
    (r'\d{4}-\d{2}-\d{2}T\d{2}',                'hour'),  # 2025-01-11T19
    (r'\d{8}T\d{2}',                            'hour'),  # 20250111T19
    (r'\d{4}-\d{2}-\d{2}',                      'day'),   # 2025-01-11
    (r'\d{8}',                                  'day'),   # 20250111
]

# A stem is already in canonical form once it ENDS in either an ISO 8601 Zulu
# datetime (Z is the completion marker — see cog_utils._ISO_ZULU_END_RE, which
# this mirrors, plus the compact-time variant vendors ship) or a _day / _hour
# granularity suffix. Such a name is a fixed point: re-running must not append
# a second suffix or re-strip its stamp.
_STAMPED_END_RE = re.compile(
    r'(?:(?:\d{4}-\d{2}-\d{2}|\d{8})T(?:\d{2}:\d{2}:\d{2}|\d{6})Z|_day|_hour)$'
)


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


def prefix_event(stem: str, event_name: str) -> str:
    """
    Prepend `event_name` to `stem`, unless it is already the leading token(s).

    Idempotent: sources that arrive already named for the activation (vendor
    deliveries staged under the event, or a re-run over this pipeline's own
    output) must not accumulate a second copy of the prefix.

        >>> prefix_event("SkySat_TrueColor", "202607_Fire_OR")
        '202607_Fire_OR_SkySat_TrueColor'
        >>> prefix_event("202607_Fire_OR_SkySat_TrueColor", "202607_Fire_OR")
        '202607_Fire_OR_SkySat_TrueColor'
    """
    if not event_name:
        return stem
    if stem == event_name or stem.startswith(f"{event_name}_"):
        return stem
    return f"{event_name}_{stem}"


def no_change(original_path: str, event_name: str) -> str:
    """
    Pass-through filename builder: prepend the event name, preserve stem + ext.

    Used for sub-products (e.g. AVIRIS) whose internal datetime ranges should
    not be rewritten. The prefix is applied via prefix_event, so it is not
    doubled on a stem that already carries it.
    """
    filename = os.path.basename(original_path)
    stem, ext = os.path.splitext(filename)
    return f"{prefix_event(stem, event_name)}{ext}"


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
        - If the stem is ALREADY in canonical form (ends in an ISO-Zulu
          datetime, or in _day / _hour), it is kept verbatim — only the event
          prefix is applied. This makes the function a fixed point.
        - Otherwise, extracts the first datetime substring (see
          DATETIME_PATTERNS), strips it from the stem, and rebuilds the name
          as `{event_name}_{stem_clean}_{datetime}_{granularity}.tif`. Bare
          YYYYMMDD dates are hyphenated to YYYY-MM-DD so the output matches
          the legacy operator-facing convention.
        - If no datetime is found, returns `{event_name}_{stem}_day.tif`.

    The event prefix goes through prefix_event() in every branch, so a source
    that already carries it is not prefixed twice.
    """
    filename = os.path.basename(original_path)

    if categories is not None:
        category = categorize_file(filename, categories)
        for passthrough in passthrough_categories:
            if category.startswith(passthrough):
                return no_change(original_path, event_name)

    stem = os.path.splitext(filename)[0]

    # Already canonical -> nothing to relocate. Without this guard a stem
    # ending in a full ISO-Zulu stamp picked up a bogus second suffix
    # (..._2026-08-12T123721Z.tif -> ..._2026-08-12T123721Z_day.tif), and a
    # re-run over this pipeline's own output produced ..._day_day.tif.
    if _STAMPED_END_RE.search(stem):
        return f"{prefix_event(stem, event_name)}.tif"

    matched, granularity = extract_datetime_from_filename(stem)
    if matched:
        stem_clean = re.sub(r'_?' + re.escape(matched), '', stem, count=1)
        stem_clean = stem_clean.strip('_')
        embedded = matched
        if granularity == 'day' and len(matched) == 8 and matched.isdigit():
            embedded = f"{matched[0:4]}-{matched[4:6]}-{matched[6:8]}"
        return f"{prefix_event(stem_clean, event_name)}_{embedded}_{granularity}.tif"
    return f"{prefix_event(stem, event_name)}_day.tif"
