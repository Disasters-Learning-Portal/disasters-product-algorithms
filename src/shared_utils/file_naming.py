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


# The SHAPE of an activation event at the head of a stem: YYYYMM_Hazard_Location_.
# Anchored and followed by '_', so an 8-digit date run (20260812_SkySat_...) cannot
# match -- `\d{6}` would consume 202608 and then require '_' where '1' sits.
_EVENT_PREFIX_RE = re.compile(r'^\d{6}_[A-Za-z0-9]+_[A-Za-z0-9]+_')


def strip_event_prefix(name: str, event_name: Optional[str] = None) -> str:
    """
    Remove a leading activation-event prefix from a filename or stem.

    The inverse of prefix_event(), for pipelines that keep the activation in the
    GeoTIFF tags and the S3 prefix rather than in the filename. Sources often
    arrive already named for the event (vendor deliveries staged under it, or a
    re-run over output from an event-prefixing pipeline).

    Two passes, in this order:

    1. `event_name`, matched case-insensitively, WINS. It is the operator's
       declared truth, and it is the only way to strip an event whose location
       token itself contains underscores (`202508_Flood_New_Mexico`), which the
       generic shape below would only half-remove.
    2. Otherwise the generic `YYYYMM_Hazard_Location_` shape, so a *misnamed*
       delivery -- right shape, wrong event, wrong case -- is still cleaned.

    Returns `name` unchanged when neither matches. Only the basename is examined;
    any directory part is preserved.

        >>> strip_event_prefix("202607_Fire_OR_SkySat_TrueColor.tif")
        'SkySat_TrueColor.tif'
        >>> strip_event_prefix("202508_Flood_New_Mexico_NDVI.tif", "202508_Flood_New_Mexico")
        'NDVI.tif'
        >>> strip_event_prefix("SkySat_TrueColor.tif")
        'SkySat_TrueColor.tif'
    """
    directory, filename = os.path.split(name)

    stripped = None
    if event_name:
        head = f"{event_name}_"
        if filename.lower().startswith(head.lower()):
            stripped = filename[len(head):]
    if stripped is None:
        match = _EVENT_PREFIX_RE.match(filename)
        if match:
            stripped = filename[match.end():]

    if stripped is None:
        return name

    # A doubled separator in the source ('..._OR__NDVI.tif') would leave a leading
    # underscore behind.
    stripped = stripped.lstrip('_')

    # A name that is NOTHING but the event prefix strips to '' or to a bare
    # extension ('.tif'). Keep the original rather than emitting either.
    if not stripped or stripped.startswith('.'):
        return name
    return os.path.join(directory, stripped) if directory else stripped


def create_sar_output_filename(
    platform: str,
    product: str,
    acquired: datetime,
    filter_size: Optional[int] = None,
    ext: str = '.tif',
) -> str:
    """
    Build the output name for a calibrated SAR backscatter product.

        <platform>_<product>[_filtered<N>]_<YYYY-MM-DDTHH:MM:SSZ><ext>

        >>> create_sar_output_filename(
        ...     "Umbra-07", "sigma0", datetime(2026, 8, 5, 3, 54, 47), 5)
        'Umbra-07_sigma0_filtered5_2026-08-05T03:54:47Z.tif'

    Every part of this shape is load-bearing, and each sensor had previously
    hand-rolled the f-string and got a different part wrong. Umbra and Capella
    emitted `202608_Umbra-07_sigma02026-08-05T03:54:47Z_filtered5.tif`; iceye
    had the separator but still trailed the filter token.

    1. The **datetime is last**, so the stem ends in an ISO-Zulu stamp. That is
       the repo-wide canonical form for an individual scene with a time, and it
       is what makes _STAMPED_END_RE (and cog_utils._ISO_ZULU_END_RE) treat the
       name as a fixed point. With a trailing `_filtered5` the stem is NOT
       canonical, so create_output_filename / rename_with_event relocate the
       stamp on a downstream pass and rewrite the name — the old shape came back
       from create_output_filename as `..._sigma0_filtered5_<stamp>_hour.tif`.
    2. The product token is separated by `_`. Welded to the date (`sigma02026-`)
       it is unreadable, and no `_`-splitting consumer can recover either field.
    3. There is **no `<YYYYMM>_` head**. The three sensors used to lead with the
       acquisition year-month, which is fully redundant with the trailing stamp
       and, worse, reads as half an activation-event prefix — an operator seeing
       `202608_KyleWx_AL/202608_Umbra-07_...` reasonably concluded the pipeline
       had welded the event into the filename. The activation belongs in the
       GeoTIFF tags (`ACTIVATION_EVENT`, embedded by convert_to_cog from
       run.sh's --metadata-json) and in the S3 prefix, not in the name.
    """
    tokens = [platform, product]
    if filter_size is not None:
        tokens.append(f'filtered{filter_size}')
    tokens.append(acquired.strftime('%Y-%m-%dT%H:%M:%SZ'))
    return '_'.join(tokens) + ext


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


# A standalone YYYYMMDD run — the negative lookarounds keep it from biting a
# chunk out of a longer digit string.
_COMPACT_DATE_RE = re.compile(r'(?<!\d)(\d{8})(?!\d)')


def _compact_dates(stem: str) -> list:
    """Every standalone YYYYMMDD token in `stem` that is a real calendar date,
    in the order it appears. `20261332` is digits but not a date, so it is not
    one; a 6-digit path/row or capture-id can never be one."""
    found = []
    for m in _COMPACT_DATE_RE.finditer(stem):
        try:
            datetime.strptime(m.group(1), '%Y%m%d')
        except ValueError:
            continue
        found.append(m.group(1))
    return found


def create_nisar_filename(original_path: str, event_name: str) -> str:
    """
    Filename builder for NISAR interferometric PAIR products (GUNW and friends).

    Why this exists: an interferogram is derived from TWO acquisitions, so its
    name carries two dates —

        NISAR_D54_GUNW_20260617_20260629_unw_delon_deRamp_maskWater_cm.tif
                       ^ reference   ^ secondary

    — and create_output_filename relocates only the FIRST datetime it finds.
    Run against the above it promotes the *reference* date to the canonical
    trailing slot and strands the secondary date mid-name, still unhyphenated:

        ..._NISAR_D54_GUNW_20260629_unw_..._cm_2026-06-17_day.tif

    Both dates are meaningful (the pair IS the product), so both are kept, in
    source order, adjacent, immediately before the `_day` granularity suffix:

        <EVENT>_NISAR_D54_GUNW_unw_delon_deRamp_maskWater_cm_2026-06-17_2026-06-29_day.tif

    That keeps the repo-wide invariant that the name ends in a date + a
    granularity suffix, and anything reading the LAST date token gets the
    secondary (post-event) acquisition.

    Source order is preserved rather than sorted: NISAR names the reference
    first, and silently reordering would misreport the pair if a delivery ever
    did otherwise.

    Falls back to create_output_filename when the stem does not hold two dates,
    so this is safe to point a whole category at. Idempotent for the same
    reason create_output_filename is — an already-canonical stem is returned
    with only the event prefix applied.
    """
    filename = os.path.basename(original_path)
    stem, ext = os.path.splitext(filename)

    if _STAMPED_END_RE.search(stem):
        return f"{prefix_event(stem, event_name)}{ext or '.tif'}"

    dates = _compact_dates(stem)
    if len(dates) < 2:
        # One date (or none) -> ordinary single-acquisition convention.
        return create_output_filename(original_path, event_name)

    reference, secondary = dates[0], dates[1]

    stem_clean = stem
    for date in (reference, secondary):
        stem_clean = re.sub(r'_?' + re.escape(date), '', stem_clean, count=1)
    stem_clean = re.sub(r'_{2,}', '_', stem_clean).strip('_')

    def _hyphenate(d):
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"

    return (
        f"{prefix_event(stem_clean, event_name)}"
        f"_{_hyphenate(reference)}_{_hyphenate(secondary)}_day.tif"
    )
