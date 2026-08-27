"""
Cloud Optimized GeoTIFF (COG) utilities for Landsat processing.

Based on implementation patterns from disasters-aws-conversion repository.
"""

import os
import shutil
import subprocess
import tempfile
import rasterio
import numpy as np
import re
from datetime import datetime
from typing import Dict, Optional, Union, Tuple


_INTEGER_DTYPE_DEFAULTS = {
    'uint8': 0,
    'uint16': 0,
    'uint32': 0,
    'uint64': 0,
    'int8': -128,
    'int16': -9999,
    'int32': -9999,
    'int64': -9999,
}

# 8-bit imagery band counts that carry NO alpha/mask band. A 1- or 3-band
# 8-bit file has no mask at all, and the dtype default (0 for uint8) would mask
# legitimately-black imagery, so it must not declare a numeric nodata -- see
# is_bare_8bit_imagery. A file that DOES carry alpha must not declare one
# either (the scalar shadows the alpha band), but that is a dtype-independent
# question answered by colour interpretation, not by band count -- see
# carries_alpha_band.
_BARE_8BIT_BAND_COUNTS = frozenset({1, 3})

_EIGHT_BIT_DTYPES = frozenset({'uint8', 'int8'})


def is_bare_8bit_imagery(dtype, band_count) -> bool:
    """True for an 8-bit raster with 1 or 3 bands (i.e. no alpha band).

    These must not auto-declare a nodata value. In an 8-bit product every
    in-range value is a legitimate sample -- dark water, shadow, burn scar and
    deep shade are all genuinely 0 -- so `set_nodata_value('uint8')`'s `0`
    masks real imagery rather than fill. Validity for 8-bit products belongs in
    an alpha band (making the file 2- or 4-band); a 1- or 3-band file has no
    such band, and is better off declaring nothing than declaring 0.

    Deliberately keyed on band count, not on colour interpretation: the
    colorinterp of a freshly written GTiff is not reliable until reopen, but
    band count always is.

    Scope note: this predicate answers "8-bit, no alpha" only. Whether a raster
    carries an alpha band is a separate, dtype-independent question — see
    `carries_alpha_band`.
    """
    return (
        str(dtype).lower() in _EIGHT_BIT_DTYPES
        and band_count in _BARE_8BIT_BAND_COUNTS
    )


def carries_alpha_band(colorinterp) -> bool:
    """True when the raster's LAST band is tagged as an alpha band.

    A raster that carries alpha must not declare a scalar nodata **at any bit
    depth**: the two are mutually exclusive ways to express validity, and the
    scalar wins. rasterio says so outright —

        NodataShadowWarning: The dataset's nodata attribute is shadowing the
        alpha band. All masks will be determined by the nodata attribute.

    — so the alpha band is silently ignored and every pixel that happens to
    equal the sentinel reads as fill, however legitimate it is.

    That shadowing has nothing to do with dtype, which is why keying the
    carve-out on "8-bit" alone (`is_bare_8bit_imagery`) is too narrow: a uint16
    RGBA — the shape Satellogic's source rasters arrive in — would otherwise
    auto-detect the uint16 default of 0 and shadow its own alpha band.

    Takes the colorinterp tuple rather than a dataset so it stays pure and
    directly testable. Read it from a dataset opened on a closed, on-disk file
    (`rasterio.open(path).colorinterp`), NOT from a freshly created dataset
    before close — that is the unreliability `is_bare_8bit_imagery` avoids.

    Checks the LAST band only, deliberately, rather than inferring alpha from a
    band count of 2 or 4. Band count is not evidence: a 4-band multispectral
    stack (e.g. Satellogic's own uint16 B/G/R/NIR TOA) has four bands and no
    alpha, and must keep its normal nodata behaviour. TIFF's ExtraSamples and
    every GDAL writer put alpha last, so the last band is where it lives.
    """
    if not colorinterp:
        return False
    from rasterio.enums import ColorInterp
    return colorinterp[-1] == ColorInterp.alpha


# Matches a trailing ISO 8601 Zulu datetime, e.g. "...2025-09-22T18:56:17Z".
# Its presence at the end of a filename is itself the "already renamed"
# marker for the new convention - no _day suffix needed alongside it.
_ISO_ZULU_END_RE = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')


def set_nodata_value(dtype: str, manual_nodata: Optional[Union[int, float]] = None) -> Union[int, float]:
    """
    Automatically select appropriate no-data value based on data type.

    Args:
        dtype: Rasterio/numpy data type string (e.g., 'uint8', 'int16', 'float32')
        manual_nodata: Optional manual no-data value. If provided and valid
            for the given dtype, returned as-is. If provided and invalid,
            ignored — falls through to auto-detect (legacy behavior).

    Returns:
        Appropriate no-data value for the data type.

    Raises:
        ValueError: dtype isn't a recognized integer or float family
            (e.g. 'complex64', 'bool', or a typo). Caller must pass a
            valid `manual_nodata` explicitly for those dtypes.
    """
    # Use manual no-data if provided and valid
    if manual_nodata is not None:
        validation = validate_nodata_for_dtype(manual_nodata, dtype)
        if validation['valid']:
            return manual_nodata

    dtype_str = str(dtype).lower()

    if dtype_str in _INTEGER_DTYPE_DEFAULTS:
        return _INTEGER_DTYPE_DEFAULTS[dtype_str]
    if 'float' in dtype_str:
        return -9999.0
    # complex64, complex128, bool, weird typos, ... — refuse to guess.
    raise ValueError(
        f"No default nodata for dtype {dtype!r}. Supported dtypes: "
        f"{sorted(_INTEGER_DTYPE_DEFAULTS)} + float* family. "
        f"Pass manual_nodata=<value> explicitly to override."
    )


def validate_nodata_for_dtype(nodata: Union[int, float], dtype: str) -> dict:
    """
    Validate that a no-data value is valid for the given data type.

    Args:
        nodata: No-data value to validate
        dtype: Rasterio/numpy data type string

    Returns:
        dict with keys: 'valid' (bool), 'error' (str or None)

    Based on disasters-aws-conversion/lib/core/compression.py:validate_nodata_for_dtype()
    """
    dtype_str = str(dtype).lower()

    try:
        nodata = float(nodata)
    except (TypeError, ValueError):
        return {'valid': False, 'error': f"Cannot convert {nodata} to numeric value"}

    if dtype_str == 'uint8':
        if not (0 <= nodata <= 255):
            return {'valid': False, 'error': f"Value {nodata} out of range for uint8 [0, 255]"}
    elif dtype_str == 'uint16':
        if not (0 <= nodata <= 65535):
            return {'valid': False, 'error': f"Value {nodata} out of range for uint16 [0, 65535]"}
    elif dtype_str == 'int8':
        if not (-128 <= nodata <= 127):
            return {'valid': False, 'error': f"Value {nodata} out of range for int8 [-128, 127]"}
    elif dtype_str == 'int16':
        if not (-32768 <= nodata <= 32767):
            return {'valid': False, 'error': f"Value {nodata} out of range for int16 [-32768, 32767]"}
    elif dtype_str == 'int32':
        if not (-2147483648 <= nodata <= 2147483647):
            return {'valid': False, 'error': f"Value {nodata} out of range for int32"}
    elif 'float' in dtype_str:
        # Float types can use any numeric value including NaN
        if not (isinstance(nodata, (int, float)) or np.isnan(nodata)):
            return {'valid': False, 'error': f"Value {nodata} must be numeric for float types"}
    else:
        # Strict for unknown dtypes — previously this branch silently returned
        # valid=True for typos like 'WeirdType', which let invalid nodata
        # values through unchecked.
        return {
            'valid': False,
            'error': (
                f"Unknown dtype {dtype!r} — supported: uint8/uint16/uint32/"
                f"uint64, int8/int16/int32/int64, float*. If this dtype is real, "
                f"add it to validate_nodata_for_dtype's ladder."
            ),
        }

    return {'valid': True, 'error': None}


def determine_resampling_method(src_path: str) -> Tuple[str, str]:
    """
    Auto-detect appropriate resampling method based on data characteristics.

    Args:
        src_path: Path to source raster file

    Returns:
        Tuple of (resampling_method, overview_resampling):
        - resampling_method: 'cubic', 'bilinear', or 'nearest'
        - overview_resampling: 'average' or 'mode'

    Logic:
        - 3-band data (RGB imagery) -> cubic / average
        - Single-band continuous data -> bilinear / average
        - Single-band categorical data -> nearest / mode
    """
    def _overview_for(method: str) -> str:
        if method == 'nearest':
            return 'mode'
        return 'average'

    try:
        with rasterio.open(src_path) as src:
            # 3-band imagery (RGB products)
            if src.count == 3:
                method = 'cubic'
                return method, _overview_for(method)

            # Single-band data - determine if categorical or continuous
            elif src.count == 1:
                dtype = str(src.dtypes[0]).lower()
                nodata = src.nodata
                filename = os.path.basename(src_path).lower()

                # Heuristics for categorical data
                # Check filename for categorical indicators
                categorical_keywords = ['mask', 'extent', 'classification', 'scl', 'qa']
                if any(keyword in filename for keyword in categorical_keywords):
                    return 'nearest', 'mode'

                # Check nodata value (999 and 255 common for categorical)
                if nodata in [999, 255] and dtype in ['uint8', 'uint16', 'int8']:
                    return 'nearest', 'mode'

                # Default to bilinear for continuous single-band data
                return 'bilinear', 'average'

            # Multi-band but not 3 (rare case)
            return 'bilinear', 'average'

    except Exception as e:
        print(f"  Warning: Could not determine resampling method: {e}")
        print("  Defaulting to bilinear resampling")
        return 'bilinear', 'average'


def get_compression_profile(
    compression: str = 'ZSTD',
    compression_level: int = 22,
    dtype: Optional[str] = None,
    file_size_gb: Optional[float] = None,
) -> dict:
    """
    Get compression profile for COG creation.

    Args:
        compression: Compression type (DEFLATE, LZW, ZSTD, etc.)
        compression_level: Compression level (default: 22 for ZSTD, 9 for others)
        dtype: Optional data type string. If it contains 'float', predictor 3
            (floating point) is used; if 'int'/'uint', predictor 2 (horizontal
            differencing). Default (None) uses predictor 2.
        file_size_gb: Optional file size in GB. Files >10 GB get 256x256 block
            size; files >3 GB get bigtiff='YES'.

    Returns:
        Dictionary of compression options for rio cogeo
    """
    valid_compressions = ['DEFLATE', 'LZW', 'ZSTD', 'JPEG', 'WEBP', 'NONE']

    if compression.upper() not in valid_compressions:
        print(f"Warning: Invalid compression '{compression}', using ZSTD")
        compression = 'ZSTD'

    # Determine predictor based on dtype
    if dtype is not None:
        dtype_lower = str(dtype).lower()
        if 'float' in dtype_lower:
            predictor = '3'  # Floating point predictor
        elif 'int' in dtype_lower or 'uint' in dtype_lower:
            predictor = '2'  # Horizontal differencing
        else:
            predictor = '2'
    else:
        predictor = '2'  # Default: horizontal differencing

    profile = {
        'compress': compression.upper(),
        'predictor': predictor,
        'level': compression_level,
    }

    # Adjust for large files
    if file_size_gb is not None:
        if file_size_gb > 10:
            profile['blockxsize'] = 256
            profile['blockysize'] = 256
        if file_size_gb > 3:
            profile['bigtiff'] = 'YES'

    return profile


# GDAL Env config shared by both convert_to_cog branches. The subprocess branch
# gets this for free from the `rio cogeo create` CLI; the in-process branch has
# to pass it to cog_translate(config=...) explicitly.
COG_GDAL_CONFIG = {
    'GDAL_NUM_THREADS': 'ALL_CPUS',
    'GDAL_TIFF_INTERNAL_MASK': True,
}


def build_creation_options(compression: str, compression_level: int) -> dict:
    """
    Single source of truth for the GDAL creation options used by BOTH
    ``convert_to_cog`` backends -- the subprocess ``rio cogeo create`` branch
    and the in-process ``rio_cogeo.cog_translate`` branch (``metadata=...``).

    The two branches used to build these independently, and the in-process one
    was missing both of the entries below. Keep them in one place so they
    cannot drift again (pinned by ``TestCreationOptionParity``).

    ``NUM_THREADS=ALL_CPUS``
        ``.clinerules.md`` rule #8 -- every raster hot path runs all cores.
        Without it GDAL compresses single-threaded, pinning one core per
        worker. Measured on a 1.2 Gpx SkySat scene: 55.6s -> 28.9s.

    ``BIGTIFF=IF_SAFER``
        Load-bearing, and the subtler of the two. GDAL's ``IF_NEEDED`` default
        sizes the classic-vs-BigTIFF decision on the BASE raster alone, but
        rio-cogeo writes an **uncompressed** scratch dataset and then adds
        overviews to it. A source that is comfortably under 4 GB uncompressed
        can therefore cross the 4 GB classic-TIFF offset ceiling *after* the
        format is already locked in. libtiff then falls into
        ``TIFFRewriteDirectory``, and ``GDALClose`` on the scratch file
        effectively never returns.

        Measured on ``SkySat_SR_TrueColor_2026-08-12T232033Z.tif``
        (39116x31550, 3 band uint8 = 3.70 GB raw, 4.38 GB once overviews are
        added): the finished COG was already on disk after ~12 min, then the
        process burned >15 further minutes at 100% CPU inside
        ``gdal_TIFFRewriteDirectorySec`` before being killed. With
        ``BIGTIFF=IF_SAFER`` the same file completes in 55.6s.

        Note this is invisible for small test rasters -- it only bites when
        raw size + overviews straddles 4 GB, which is why it shipped unnoticed.
    """
    opts = {
        'NUM_THREADS': 'ALL_CPUS',
        'BIGTIFF': 'IF_SAFER',
    }
    if compression.upper() == 'DEFLATE':
        opts['PREDICTOR'] = 2
        opts['ZLEVEL'] = compression_level
    elif compression.upper() == 'LZW':
        opts['PREDICTOR'] = 2
    elif compression.upper() == 'ZSTD':
        opts['PREDICTOR'] = 2
        opts['ZSTD_LEVEL'] = compression_level
    return opts


def _build_cog_translate_profile(compression: str, compression_level: int) -> dict:
    """
    Build a rio_cogeo profile dict that matches what the subprocess
    `rio cogeo create ... --co PREDICTOR=2 --co ZSTD_LEVEL=N` path produces.

    Used only when `convert_to_cog` is invoked with `metadata`, because
    embedding GeoTIFF tags requires the Python `cog_translate` API
    (the CLI has no flag for arbitrary metadata). Empirical: post-step
    `gdal.SetMetadata` on a finished COG breaks the COG layout in
    GDAL 3.10+.
    """
    from rio_cogeo.profiles import cog_profiles

    profile = dict(cog_profiles.get(compression.lower()))
    profile.update(build_creation_options(compression, compression_level))
    return profile


def convert_to_cog(
    input_tif: str,
    output_cog: Optional[str] = None,
    nodata: Optional[Union[int, float, bool]] = None,
    dst_crs: Optional[str] = 'EPSG:3857',
    resampling_method: Optional[str] = None,
    clip_to_webmerc: Optional[bool] = None,
    compression: str = 'ZSTD',
    compression_level: int = 22,
    overview_levels: int = 5,
    quiet: bool = False,
    backend: str = 'rio',
    metadata: Optional[Dict[str, str]] = None,
    strict_nodata: bool = True,
) -> str:
    """
    Convert a GeoTIFF to Cloud Optimized GeoTIFF (COG) format with optional reprojection.

    Args:
        input_tif: Path to input GeoTIFF file
        output_cog: Path to output COG file (if None, replaces input file)
        nodata: No-data value. `None` (default) auto-detects from the file's
            existing tag, else from the data type — **except for two carve-outs,
            both of which resolve to no nodata tag and strip an existing tag off
            the source**:

            1. The raster **carries an alpha band** (`carries_alpha_band`, i.e.
               its last band's colour interpretation is alpha) — at **any** bit
               depth, uint16 RGBA included. A scalar nodata declared alongside
               an alpha band shadows it: rasterio raises NodataShadowWarning
               ("All masks will be determined by the nodata attribute"),
               rio-cogeo warns "Nodata value will be prioritized", and the alpha
               band is silently ignored. Detection is by colour interpretation,
               never by band count — a 4-band multispectral stack has no alpha
               and keeps its normal nodata behaviour.
            2. The raster is **8-bit with 1 or 3 bands** and so carries no mask
               at all (`is_bare_8bit_imagery`). Every uint8 value is a
               legitimate sample — dark water, shadow, burn scar — so the old
               dtype default of 0 masked real black imagery.

            A number is used as-is, after `validate_nodata_for_dtype`, and still
            wins over both carve-outs. **`False` is an explicit opt-out**:
            declare no nodata at all, for an input that carries its own alpha or
            mask band. It remains the right call for a producer that knows it
            wrote alpha (e.g. `geotools.dump_geotiff_rgb(..., alpha=...)`); the
            carve-out above is the safety net for everything else.

            Net effect: validity is expressed exactly once per raster — by an
            alpha band where one exists, by a scalar sentinel where the dtype
            makes one meaningful (uint16 classified/quality rasters, float
            -9999), and by nothing at all for bare 8-bit imagery.
        dst_crs: Target CRS (default: 'EPSG:3857', None to preserve native CRS).
            Web Mercator avoids the WGS 84 ensemble / lat-first axis bug that
            breaks rio_stac.get_dataset_geom in veda-data-airflow build_stac.
        resampling_method: Warp resampling ('near', 'bilinear', 'cubic',
            'average'). If None, auto-detected via determine_resampling_method().
        clip_to_webmerc: Clip output extent to Web Mercator's valid domain
            (±20037508.34 m / ±85.05° latitude). True/False to force; None to
            auto-detect via reprojection.needs_webmerc_clip() — required for
            world-extent sources (e.g. global Mollweide) when dst_crs is
            EPSG:3857, no-op for regional rasters that already fit.
        compression: Compression type (default: ZSTD)
        compression_level: Compression level (default: 22 for ZSTD)
        overview_levels: Number of overview levels (default: 5, minimum)
        quiet: Suppress output messages
        backend: Backend to use for COG creation. 'rio' (default) uses rio-cogeo
            CLI, 'gdal' delegates to shared_utils.gdal_cog_processor.create_cog_gdal.
        metadata: Optional dict of activation-event tags to embed in the output
            COG (e.g. {'ACTIVATION_EVENT': '202501_Flood_CA', 'SOURCE': 'USGS',
            'PROCESSOR': 'NASA Disasters COG Processor v1.0.0'}). When provided,
            convert_to_cog routes through the in-process `rio_cogeo.cog_translate`
            (not the `rio cogeo create` subprocess) so the tags land at COG
            creation time. Auto-augments YEAR_MONTH/HAZARD/LOCATION/PROCESSING_DATE
            via shared_utils.cog_metadata.resolve_metadata. Not supported on the
            'gdal' backend yet.
        strict_nodata: When True (default), an out-of-range caller-supplied
            `nodata` raises ValueError up front instead of printing a warning
            and pretending to continue. Set False for the legacy warn-only
            behavior — though in practice rasterio itself rejects out-of-range
            nodata values at the gdalwarp / rio_cogeo step, so `strict_nodata=
            False` mostly just moves the crash site downstream. The kwarg
            exists as a forward-looking signal in case a future rasterio
            relaxes those checks. The default was flipped 2026-06-01 because
            the prior silent-fail mode produced a confusing warning then a
            cryptic rasterio crash several stack frames later.

    Returns:
        Path to created COG file
    """
    if metadata is not None and backend == 'gdal':
        raise NotImplementedError(
            "metadata embedding is not yet supported on backend='gdal'. "
            "Use backend='rio' (the default)."
        )

    # Normalize `dst_crs`: treat string 'None' / 'none' / '' as actual None.
    # Mirrors the same coercion in main_processor.convert_to_cog so notebooks
    # don't have to repeat the `_dst_crs = TARGET_CRS if (...) else None`
    # boilerplate at every call site.
    if isinstance(dst_crs, str) and dst_crs.strip().lower() in ('none', ''):
        dst_crs = None
    # GDAL virtual filesystem prefixes (/vsis3/, /vsicurl/, /vsigs/, ...) bypass
    # the existence check — rasterio + gdalwarp open them natively when the
    # caller is streaming a remote object instead of downloading first.
    is_vsi = isinstance(input_tif, str) and input_tif.startswith('/vsi')
    if not is_vsi and not os.path.exists(input_tif):
        raise FileNotFoundError(f"Input file not found: {input_tif}")

    # GDAL backend delegation
    if backend == 'gdal':
        from shared_utils.gdal_cog_processor import create_cog_gdal
        final_output = output_cog if output_cog is not None else input_tif
        success = create_cog_gdal(
            input_path=input_tif,
            output_path=final_output,
            nodata=nodata,
            compress=compression,
            compress_level=compression_level,
            reproject_to_4326=(dst_crs == 'EPSG:4326') if dst_crs else False,
            verbose=not quiet,
        )
        if not success:
            raise RuntimeError(f"GDAL backend failed to create COG for {input_tif}")
        return final_output

    # Determine output path
    if output_cog is None:
        # Replace input file with COG version
        output_cog = input_tif
        temp_output = os.path.join('/tmp', os.path.basename(input_tif) + '.cog.tmp.tif')
    else:
        temp_output = output_cog

    # Read input file metadata and check if reprojection is needed
    warped_file = None
    nodata_stripped_vrt = None
    strip_source_nodata = False
    input_for_cog = input_tif

    from shared_utils.reprojection import (
        needs_webmerc_clip, WEBMERC_EXTENT_M,
    )

    with rasterio.open(input_tif) as src:
        dtype = src.dtypes[0]
        existing_nodata = src.nodata
        src_crs = src.crs

        # Probe the PIXELS for FLT_MAX-class fill. `is_extreme_float_nodata`
        # below only inspects the nodata tag, which misses the inverse (and more
        # damaging) case: a sane-looking tag such as -9999 on a raster whose
        # fill pixels are actually FLT_MAX. Nothing masks those, so they survive
        # into the COG and render as real data. Bounded decimated read.
        from shared_utils.compression import detect_extreme_float_fill
        extreme_fill = detect_extreme_float_fill(src)

        # Set when the fill has to be rewritten rather than merely re-tagged.
        # Consumed at the warp step, which translates -srcnodata -> -dstnodata.
        remap_extreme_fill = None

        # Determine no-data value.
        #
        # `bool` is checked BEFORE the numeric branch and with isinstance, not
        # `is False`, for two reasons:
        #   - bool subclasses int, so a bare `True`/`False` would otherwise sail
        #     through validate_nodata_for_dtype as 1/0 and silently declare the
        #     wrong sentinel.
        #   - np.bool_ is NOT the `False` singleton (`np.False_ is False` is
        #     False), so an `is False` check would miss a numpy-derived flag.
        if isinstance(nodata, (bool, np.bool_)):
            if nodata:
                raise ValueError(
                    "nodata=True is not a no-data value. Use False to declare "
                    "no nodata (for inputs that carry their own alpha/mask "
                    "band), None to auto-detect from the file or dtype, or a "
                    "number for an explicit sentinel."
                )
            # Explicit opt-out: caller's file already carries an alpha/mask band
            # (e.g. RGB composites where 0 is a legitimate data value, not nodata).
            # Declaring a scalar nodata alongside an alpha band SHADOWS it
            # (rasterio NodataShadowWarning) — an RGB read then masks every
            # legitimately-black pixel. Leaving nodata unset lets consumers use
            # the mask the file already carries.
            nodata = None
            # Passing nodata=None downstream is NOT enough on its own: both
            # `rio cogeo create` and cog_translate fall back to the source's
            # own nodata tag when none is supplied, so an opt-out on a file
            # that already declares one would be silently ignored. Strip it
            # first (see the VRT step below).
            strip_source_nodata = existing_nodata is not None
            if not quiet:
                print("  No nodata value will be set; preserving existing alpha/mask band.")
                if strip_source_nodata:
                    print(f"  Dropping the source's existing nodata tag ({existing_nodata}).")
        elif nodata is None:
            from shared_utils.compression import is_extreme_float_nodata
            if carries_alpha_band(src.colorinterp):
                # An alpha band already expresses validity, and a scalar nodata
                # declared alongside it SHADOWS it -- rasterio raises
                # NodataShadowWarning and then determines every mask from the
                # nodata attribute, so the alpha band is silently ignored.
                # Dtype-independent, hence checked before the 8-bit rule: a
                # uint16 RGBA would otherwise auto-detect the uint16 default of
                # 0 and shadow its own alpha. An existing source tag is stripped
                # for the same reason it is under nodata=False.
                nodata = None
                strip_source_nodata = existing_nodata is not None
                if not quiet:
                    print(
                        f"  {src.count}-band {dtype} raster carries an alpha "
                        f"band: no nodata will be set (a scalar nodata would "
                        f"shadow the alpha band)."
                    )
                    if strip_source_nodata:
                        print(
                            f"  Dropping the source's existing nodata tag "
                            f"({existing_nodata})."
                        )
            elif is_bare_8bit_imagery(dtype, src.count):
                # 8-bit imagery never auto-declares a nodata value; 0 is a real
                # sample, not fill. Reached only when the file carries no alpha
                # band (the branch above), i.e. the 1- and 3-band shapes, which
                # have no mask at all. Strips a source tag that was written
                # under the old dtype-default behaviour. Explicit numeric
                # nodata= from the caller still wins (that lands in the else
                # branch below), as does nodata=False.
                nodata = None
                strip_source_nodata = existing_nodata is not None
                if not quiet:
                    print(
                        f"  8-bit {src.count}-band imagery: no nodata will be set "
                        f"(every uint8 value is a legitimate sample)."
                    )
                    if strip_source_nodata:
                        print(
                            f"  Dropping the source's existing nodata tag "
                            f"({existing_nodata})."
                        )
            elif existing_nodata is not None and is_extreme_float_nodata(existing_nodata):
                # Known FLT_MAX corruption pattern — remap before it
                # propagates to gdalwarp / veda-data-airflow.
                remapped = set_nodata_value(dtype)
                print(
                    f"  WARNING: source nodata={existing_nodata!r} matches "
                    f"FLT_MAX corruption pattern; remapping to dtype default "
                    f"({remapped}). See shared_utils.compression."
                    f"EXTREME_FLOAT_NODATA for the known-bad value set."
                )
                nodata = remapped
                # Re-tagging alone is not enough. The old behaviour set the tag
                # to -9999 and then handed gdalwarp `-srcnodata -9999`, which
                # matches nothing — the FLT_MAX pixels sailed through into the
                # output, masked by neither the old value nor the new one.
                # Translate the actual fill instead.
                remap_extreme_fill = (
                    extreme_fill if extreme_fill is not None
                    else float(existing_nodata)
                )
            elif existing_nodata is not None and extreme_fill is not None:
                # The tag is sane (e.g. -9999) but the PIXELS carry FLT_MAX.
                # Tag-level checks all pass, so this used to fall through to
                # "use the existing value" and emit a COG whose fill was
                # invisible to every consumer: rio-tiler masks on the declared
                # -9999, finds none, and renders FLT_MAX as data that clamps to
                # the top of any rescale.
                remapped = set_nodata_value(dtype)
                print(
                    f"  WARNING: source declares nodata={existing_nodata!r} but "
                    f"its pixels contain {extreme_fill!r} (FLT_MAX-class fill). "
                    f"Remapping that fill to {remapped} so the declared value "
                    f"and the data agree."
                )
                nodata = remapped
                remap_extreme_fill = extreme_fill
            elif existing_nodata is not None:
                nodata = existing_nodata
                if not quiet:
                    print(f"  Using existing no-data value: {nodata}")
            else:
                nodata = set_nodata_value(dtype)
                if not quiet:
                    print(f"  Auto-selected no-data value for {dtype}: {nodata}")
                if extreme_fill is not None:
                    # No tag at all, but the fill is still FLT_MAX. Declaring
                    # the dtype default without translating the pixels would
                    # produce the same silently-broken COG as the branch above.
                    print(
                        f"  WARNING: no nodata tag, and pixels contain "
                        f"{extreme_fill!r} (FLT_MAX-class fill). Remapping that "
                        f"fill to {nodata}."
                    )
                    remap_extreme_fill = extreme_fill
        else:
            # Validate user-provided no-data
            validation = validate_nodata_for_dtype(nodata, dtype)
            if not validation['valid']:
                if strict_nodata:
                    raise ValueError(
                        f"nodata={nodata} is invalid for dtype {dtype}: "
                        f"{validation['error']}. Pass a valid value, or set "
                        f"strict_nodata=False to suppress (legacy behavior)."
                    )
                print(f"  Warning: No-data value {nodata} may be invalid for {dtype}")

        # The branches above only set remap_extreme_fill while auto-detecting
        # (nodata=None). An explicitly-supplied nodata= skips all of them, and
        # the per-sensor CLIs pass one routinely (process_landsat89,
        # process_sentinel2, process_capella, process_satellogic all forward a
        # --nodata). A sane explicit value on a FLT_MAX-filled raster is exactly
        # the case this guard exists for, so catch it here rather than letting
        # the caller's argument wave the corrupt fill through.
        if (
            remap_extreme_fill is None
            and extreme_fill is not None
            and nodata is not None
            and not isinstance(nodata, (bool, np.bool_))
        ):
            print(
                f"  WARNING: caller supplied nodata={nodata!r}, but the pixels "
                f"contain {extreme_fill!r} (FLT_MAX-class fill). Remapping that "
                f"fill to {nodata!r} so the declared value and the data agree."
            )
            remap_extreme_fill = extreme_fill

        # One -srcnodata can only name one value, so a raster carrying more than
        # one extreme sentinel (e.g. +FLT_MAX and -FLT_MAX) keeps whichever is
        # not the dominant one. Say so loudly rather than emit a COG that looks
        # repaired but still has unmasked fill in it.
        if remap_extreme_fill is not None:
            from shared_utils.compression import list_extreme_float_fills
            all_fills = list_extreme_float_fills(src)
            if len(all_fills) > 1:
                leftover = [v for v in all_fills if v != remap_extreme_fill]
                print(
                    f"  WARNING: {len(all_fills)} distinct FLT_MAX-class fill "
                    f"values present {all_fills!r}. Only {remap_extreme_fill!r} "
                    f"will be remapped; {leftover!r} will REMAIN in the output "
                    f"and stay unmasked. Rewrite this raster's fill upstream."
                )

        # Check if reprojection is needed
        needs_reprojection = (dst_crs is not None and
                             src_crs is not None and
                             str(src_crs).upper() != dst_crs.upper())

        # A pending FLT_MAX fill remap also requires the warp pass, even when
        # the CRS already matches: gdalwarp's -srcnodata/-dstnodata is what
        # actually rewrites those pixels. gdal_translate -a_nodata only re-tags,
        # and the cog_translate path copies pixels verbatim, so without this a
        # same-CRS source would keep its corrupt fill.
        if remap_extreme_fill is not None and not needs_reprojection:
            needs_reprojection = True
            if not quiet:
                print(
                    "  Forcing a same-CRS warp pass to rewrite the FLT_MAX fill."
                )

        # Decide whether to clip output to Web Mercator's valid domain.
        # `clip_to_webmerc=None` (default) defers to auto-detect; pass True/False
        # to force-override.
        if clip_to_webmerc is None:
            clip_webmerc = needs_reprojection and needs_webmerc_clip(src, dst_crs)
        else:
            clip_webmerc = bool(clip_to_webmerc) and needs_reprojection

        if not quiet:
            print(f"  Data type: {dtype}")
            print(f"  No-data value: {nodata}")
            print(f"  Source CRS: {src_crs}")
            if dst_crs:
                print(f"  Target CRS: {dst_crs}")
                if needs_reprojection:
                    print(f"  Reprojection: Required")
                    if clip_webmerc:
                        print(f"  Web Mercator clip: enabled (source exceeds ±85° lat)")
                else:
                    print(f"  Reprojection: Not needed (already in target CRS)")
            print(f"  Compression: {compression} (level {compression_level})")
            print(f"  Overview levels: {overview_levels}")

    # Default overview resampling (may be overridden during reprojection)
    overview_resampling = 'average'

    # Step 0: honor an explicit nodata opt-out (`nodata=False`) on a source that
    # already declares one. A VRT is a lazy XML header — no pixel copy — so this
    # costs nothing but makes the opt-out actually stick: without it both
    # `rio cogeo create` and cog_translate re-read the source's nodata tag.
    if strip_source_nodata:
        nodata_stripped_vrt = os.path.join(
            tempfile.gettempdir(), os.path.basename(input_tif) + '.nonodata.tmp.vrt'
        )
        translate_cmd = [
            'gdal_translate', '-of', 'VRT', '-a_nodata', 'none',
            input_tif, nodata_stripped_vrt,
        ]
        try:
            subprocess.run(translate_cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"Failed to strip the source nodata tag for nodata=False: {e.stderr}"
            )
        # NOTE: only the *pixel source* is redirected. `input_tif` keeps pointing
        # at the real file because its BASENAME is load-bearing downstream —
        # resolve_metadata() parses the activation event out of it, and
        # determine_resampling_method() reads it.
        input_for_cog = nodata_stripped_vrt

    # Pixel source for the warp step (the nodata-stripped VRT when opting out).
    warp_source = input_for_cog

    # Step 1: Reproject if needed (warp to dst_crs)
    if needs_reprojection:
        warped_file = os.path.join('/tmp', os.path.basename(input_tif) + '.warped.tmp.tif')

        # Resolve resampling: explicit override > auto-detect from file content.
        if resampling_method is None:
            resampling_method, overview_resampling = determine_resampling_method(input_tif)
            if not quiet:
                print(f"  Resampling method: {resampling_method} (auto-detected)")
        else:
            # If caller specified resampling, also use it for overview building.
            overview_resampling = resampling_method
            if not quiet:
                print(f"  Resampling method: {resampling_method} (caller-supplied)")

        # When the warp was forced purely to rewrite the fill, dst_crs may be
        # None or identical to the source; keep the pixels where they are.
        warp_target_crs = dst_crs if dst_crs is not None else str(src_crs)

        if not quiet:
            print(f"  Warping to {warp_target_crs}...")

        # Build gdalwarp command (chosen over `rio warp` so we can use
        # NUM_THREADS=ALL_CPUS; rio warp's --threads only accepts integers).
        warp_cmd = [
            'gdalwarp',
            '-t_srs', warp_target_crs,
            '-r', resampling_method,
            '-multi',
            '-wo', 'NUM_THREADS=ALL_CPUS',
            '--config', 'GDAL_NUM_THREADS', 'ALL_CPUS',
            '-overwrite',
        ]

        # Clamp output extent to Web Mercator's valid domain when source
        # exceeds it (global Mollweide, polar stereographic, etc.).
        if clip_webmerc:
            warp_cmd.extend([
                '-te',
                f'-{WEBMERC_EXTENT_M}', f'-{WEBMERC_EXTENT_M}',
                f'{WEBMERC_EXTENT_M}', f'{WEBMERC_EXTENT_M}',
                '-te_srs', 'EPSG:3857',
            ])

        # Add nodata to warp command (gdalwarp uses -srcnodata/-dstnodata).
        # These are normally the same value — the warp is not meant to change
        # what counts as fill. The exception is a pending FLT_MAX remap, where
        # -srcnodata must name the value actually sitting in the pixels so
        # gdalwarp rewrites it to the safe -dstnodata on the way out. Using
        # `nodata` on both sides there would match nothing and silently keep
        # the corrupt fill.
        if nodata is not None:
            src_nodata_arg = (
                remap_extreme_fill if remap_extreme_fill is not None else nodata
            )
            warp_cmd.extend(['-srcnodata', repr(float(src_nodata_arg))])
            warp_cmd.extend(['-dstnodata', str(nodata)])

        warp_cmd.extend([warp_source, warped_file])

        try:
            result = subprocess.run(
                warp_cmd,
                capture_output=True,
                text=True,
                check=True
            )

            if not quiet and result.stdout:
                print(f"  {result.stdout.strip()}")

            # Use warped file as input for COG conversion
            input_for_cog = warped_file

        except subprocess.CalledProcessError as e:
            error_msg = f"Error warping to {dst_crs}: {e.stderr}"
            print(error_msg)
            raise RuntimeError(error_msg)

    # Step 2: Build rio cogeo create command (using warped file if reprojected)
    cmd = [
        'rio', 'cogeo', 'create',
        input_for_cog,  # Use warped file if reprojection occurred
        temp_output,
        '--cog-profile', compression.lower(),
        '--overview-level', str(overview_levels),
        '--overview-resampling', overview_resampling,
    ]

    # Add no-data value
    if nodata is not None:
        cmd.extend(['--nodata', str(nodata)])

    # Creation options come from the ONE builder both branches share, so the
    # subprocess path and the in-process cog_translate path cannot diverge
    # (see build_creation_options for why NUM_THREADS and BIGTIFF matter).
    for key, value in build_creation_options(compression, compression_level).items():
        cmd.extend(['--co', f'{key}={value}'])

    # Execute COG creation.
    #
    # Two paths:
    #   - `metadata is None` (default): subprocess `rio cogeo create`. Fast,
    #     unchanged from prior behavior.
    #   - `metadata is not None`: in-process `rio_cogeo.cogeo.cog_translate`
    #     with `additional_cog_metadata=...`. Required because:
    #       (a) `rio cogeo create` CLI has no flag for arbitrary tags, and
    #       (b) reopening a finished COG with `gdal.Open(GA_Update)` +
    #           `SetMetadata(...)` breaks the COG layout in GDAL 3.10+
    #           (`cog_validate` returns valid=False with IFD-offset errors).
    if not quiet:
        if metadata is not None:
            print(f"  Creating COG with embedded metadata: {os.path.basename(temp_output)}")
        else:
            print(f"  Creating COG: {os.path.basename(temp_output)}")

    try:
        if metadata is not None:
            from rio_cogeo.cogeo import cog_translate

            # Auto-augment with YEAR_MONTH/HAZARD/LOCATION/PROCESSING_DATE
            # if the filename matches the activation-event pattern.
            try:
                from shared_utils.cog_metadata import resolve_metadata
                full_metadata = resolve_metadata(
                    os.path.basename(input_tif),
                    mode='manual',
                    manual_metadata=metadata,
                )
            except ImportError:
                full_metadata = dict(metadata)

            if not quiet:
                print(f"  Embedded tags: {sorted(full_metadata.keys())}")

            profile = _build_cog_translate_profile(compression, compression_level)
            cog_translate(
                input_for_cog,
                temp_output,
                profile,
                nodata=nodata,
                overview_level=overview_levels,
                overview_resampling=overview_resampling,
                web_optimized=False,
                additional_cog_metadata=full_metadata,
                # The subprocess branch gets this from the `rio cogeo create`
                # CLI (its --threads defaults to ALL_CPUS). Without it here,
                # cog_translate runs under a bare rasterio.Env() and GDAL
                # compresses on a single thread.
                config=COG_GDAL_CONFIG,
                quiet=quiet,
            )
        else:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
            )
            if not quiet and result.stdout:
                print(f"  {result.stdout.strip()}")

        # If we created a temp file, replace the original.
        # Use shutil.move (not os.rename): temp_output lives in /tmp while
        # output_cog is usually on a different mount (e.g. the Hub's /home or a
        # shared volume). os.rename across filesystems raises
        # OSError(EXDEV, 'Invalid cross-device link'); shutil.move falls back to
        # copy + delete.
        if temp_output != output_cog:
            if os.path.exists(output_cog):
                os.remove(output_cog)
            shutil.move(temp_output, output_cog)

        # Clean up warped temp file if it was created
        if warped_file and os.path.exists(warped_file):
            os.remove(warped_file)
        if nodata_stripped_vrt and os.path.exists(nodata_stripped_vrt):
            os.remove(nodata_stripped_vrt)

        if not quiet:
            print(f"  ✓ COG created: {os.path.basename(output_cog)}")

        return output_cog

    except subprocess.CalledProcessError as e:
        error_msg = f"Error creating COG: {e.stderr}"
        print(error_msg)
        # Clean up temp files if they exist
        if os.path.exists(temp_output):
            os.remove(temp_output)
        if warped_file and os.path.exists(warped_file):
            os.remove(warped_file)
        if nodata_stripped_vrt and os.path.exists(nodata_stripped_vrt):
            os.remove(nodata_stripped_vrt)
        raise RuntimeError(error_msg)
    except Exception:
        if os.path.exists(temp_output):
            os.remove(temp_output)
        if warped_file and os.path.exists(warped_file):
            os.remove(warped_file)
        if nodata_stripped_vrt and os.path.exists(nodata_stripped_vrt):
            os.remove(nodata_stripped_vrt)
        raise


def validate_cog(cog_path: str) -> Tuple[bool, dict]:
    """
    Validate that a file is a valid Cloud Optimized GeoTIFF.

    Tries the rio_cogeo Python library first for richer detail, then falls
    back to the ``rio cogeo validate`` CLI subprocess.

    Args:
        cog_path: Path to COG file

    Returns:
        Tuple of (is_valid, details) where details is a dict with keys:
            'valid' (bool), 'errors' (list[str]), 'warnings' (list[str])
    """
    empty_details = {'valid': False, 'errors': [], 'warnings': []}

    if not os.path.exists(cog_path):
        empty_details['errors'].append(f"File not found: {cog_path}")
        return False, empty_details

    # Try the Python library first
    try:
        from rio_cogeo.cogeo import cog_validate
        is_valid, errors, warnings = cog_validate(cog_path, quiet=True)
        details = {
            'valid': is_valid,
            'errors': errors if errors else [],
            'warnings': warnings if warnings else [],
        }
        return is_valid, details
    except ImportError:
        pass
    except Exception:
        pass

    # Fallback to CLI subprocess
    try:
        result = subprocess.run(
            ['rio', 'cogeo', 'validate', cog_path],
            capture_output=True,
            text=True,
            check=False
        )

        is_valid = result.returncode == 0
        details = {
            'valid': is_valid,
            'errors': [] if is_valid else [result.stderr.strip() or "COG validation failed"],
            'warnings': [],
        }
        return is_valid, details

    except Exception as e:
        print(f"Error validating COG: {e}")
        empty_details['errors'].append(str(e))
        return False, empty_details


def _relocate_datetime(base_name: str, extension: str = '.tif') -> str:
    """Rebuild a product basename to the canonical date/time convention.

    Shared by rename_with_event() (the actual rename) and get_final_filename()
    (the skip-check predictor) so the two can never drift.

    Convention:
      - merged mosaics            -> {tokens}_{YYYY-MM-DD}_day         (date only)
      - individual WITH a time    -> {tokens}_{YYYY-MM-DD}T{HH:MM:SS}Z  (ends in Z)
      - individual WITHOUT a time -> {tokens}_{YYYY-MM-DD}_day         (date only)

    `{tokens}` is every non-date/-time token in its original order (product,
    tile / path-row, and merged / masked markers preserved verbatim). Callers
    are expected to have already short-circuited files that are done (ending in
    an ISO-Zulu datetime or `_day`).

    Raises:
        ValueError: no YYYYMMDD / YYYY-MM-DD date token found.
    """
    parts = base_name.split('_')
    is_merged = 'merged' in parts

    # 1) Adjacent acquisition date+time: ..._YYYYMMDD_HHMMSS_...
    date_index = date_str = time_str = None
    for i in range(len(parts) - 1):
        p, nxt = parts[i], parts[i + 1]
        if len(p) == 8 and p.isdigit() and len(nxt) == 6 and nxt.isdigit():
            try:
                datetime.strptime(p, '%Y%m%d')
                date_str, time_str, date_index = p, nxt, i
                break
            except ValueError:
                continue

    # 2) Lone compact date (no time): ..._YYYYMMDD_...
    if date_str is None:
        for i, p in enumerate(parts):
            if len(p) == 8 and p.isdigit():
                try:
                    datetime.strptime(p, '%Y%m%d')
                    date_str, date_index = p, i
                    break
                except ValueError:
                    continue

    # 3) Lone dashed date (e.g. a raw merged name ..._merged_YYYY-MM-DD): no time.
    if date_str is None:
        for i, p in enumerate(parts):
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', p):
                date_str, date_index = p, i
                break

    if date_str is None:
        raise ValueError(
            f"Could not find a date (YYYYMMDD or YYYY-MM-DD) in filename: {base_name}"
        )

    formatted_date = (
        date_str if '-' in date_str
        else f'{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}'
    )

    # Drop the date (and its adjacent time) token(s); keep the rest in order.
    drop = {date_index}
    if time_str is not None:
        drop.add(date_index + 1)
    prefix = '_'.join(p for i, p in enumerate(parts) if i not in drop)

    if is_merged or time_str is None:
        # Merged mosaics (times differ across scenes) + time-less products
        # -> day granularity.
        return f"{prefix}_{formatted_date}_day{extension}"
    # Individual scene with a real acquisition time -> full ISO 8601 Zulu.
    iso_time = f"{time_str[0:2]}:{time_str[2:4]}:{time_str[4:6]}"
    return f"{prefix}_{formatted_date}T{iso_time}Z{extension}"


def get_final_filename(original_path: str, event_name: Optional[str] = None, tif_only: bool = False) -> str:
    """
    Predict what the final filename will be after COG conversion and/or event renaming.

    This is used to check if a file already exists before processing.

    Args:
        original_path: Original TIF file path (before COG/rename)
        event_name: Event name for renaming (None if no renaming)
        tif_only: If True, COG conversion will be skipped

    Returns:
        Predicted final file path

    Examples:
        >>> get_final_filename("/path/LC08_trueColor_20250922_185617_046028.tif", None, False)
        "/path/LC08_trueColor_20250922_185617_046028.tif"  # COG converts in place

        >>> get_final_filename("/path/LC08_trueColor_20250922_185617_046028.tif", "202512_Flood_WA", False)
        "/path/LC08_trueColor_185617_046028_2025-09-22_day.tif"  # event name no longer prefixed
    """
    if event_name is None:
        # No renaming, COG converts in place or stays as TIF
        return original_path

    # If event name is provided, predict the rename via the shared transform so
    # this predictor and rename_with_event() can never disagree.
    directory = os.path.dirname(original_path)
    filename = os.path.basename(original_path)
    base_name, extension = os.path.splitext(filename)

    # Already renamed (ISO-Zulu datetime or _day) -> no change, same as the rename.
    if _ISO_ZULU_END_RE.search(base_name) or base_name.endswith('_day'):
        return original_path

    if len(base_name.split('_')) < 3:
        # Doesn't match the sensor_product_date... pattern -> leave as-is.
        return original_path

    try:
        new_filename = _relocate_datetime(base_name, extension)
    except ValueError:
        # No parseable date -> nothing to relocate, name stays as-is.
        return original_path

    return os.path.join(directory, new_filename)


def rename_with_event(file_path: str, event_name: str, quiet: bool = False) -> str:
    """
    Rename a file to relocate its date/time to the end.
 
    NOTE: the event name is intentionally NOT added to the filename. The
    `event_name` parameter is retained for backward compatibility and as the
    rename trigger (callers invoke this only when an event was supplied); only
    the date/time relocation to the canonical convention (below) is applied.
 
    The convention is date/time granularity, applied uniformly across sensors
    (see _relocate_datetime, which this shares with get_final_filename):

      * Individual scene WITH a time -> full ISO 8601 Zulu datetime, ending in
        "Z" (the completion marker; no _day). The date+time pair is relocated
        to the end and every other token (product, tile/path-row) is kept:
         Original: LC08_trueColor_20250922_185617_046028.tif
         New:      LC08_trueColor_046028_2025-09-22T18:56:17Z.tif
         Original: S2B_MSIL2A_colorInfrared_20251111_161419_T17RLN.tif
         New:      S2B_MSIL2A_colorInfrared_T17RLN_2025-11-11T16:14:19Z.tif

      * Merged mosaic -> day granularity (times differ across scenes), so the
        date only, with a _day suffix. merged/masked markers are preserved:
         Original: LC08_trueColor_20250922_merged.tif
         New:      LC08_trueColor_merged_2025-09-22_day.tif
         Original: LC08_trueColor_merged_masked_20250922_185617.tif
         New:      LC08_trueColor_merged_masked_2025-09-22_day.tif

      * Individual scene with NO time (e.g. waterExtent) -> _day (there is no
        HH:MM:SS to build a Z stamp):
         Original: LC08_waterExtent_NSTD_1_5_20250922.tif
         New:      LC08_waterExtent_NSTD_1_5_2025-09-22_day.tif
 
    Args:
        file_path: Path to the file to rename
        event_name: Retained for backward compatibility / rename trigger; not added to the name
        quiet: Suppress output messages
 
    Returns:
        New file path after renaming
 
    Raises:
        ValueError: If date cannot be extracted from filename
        FileNotFoundError: If file doesn't exist
    """
    import os
 
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")
 
    directory = os.path.dirname(file_path)
    filename = os.path.basename(file_path)
    name_parts = os.path.splitext(filename)
    base_name = name_parts[0]
    extension = name_parts[1]
 
    # New-convention files are done once they end in an
    # ISO Zulu datetime (Z is the completion marker, no _day needed).
    # Legacy-shape files are done once they end in _day.
    if _ISO_ZULU_END_RE.search(base_name) or base_name.endswith('_day'):
        if not quiet:
            print(f"  Already renamed, skipping: {filename}")
        return file_path
 
    if len(base_name.split('_')) < 3:
        raise ValueError(f"Filename doesn't match expected pattern: {filename}")

    # Canonical rename lives in one shared transform so this and
    # get_final_filename() (the skip-check predictor) can never drift:
    #   merged            -> ..._YYYY-MM-DD_day        (date only)
    #   individual +time  -> ..._YYYY-MM-DDTHH:MM:SSZ  (ends in Z)
    #   individual no-time -> ..._YYYY-MM-DD_day       (date only)
    new_filename = _relocate_datetime(base_name, extension)

    new_path = os.path.join(directory, new_filename)
 
    if not quiet:
        print(f"  Renaming: {filename}")
        print(f"        to: {new_filename}")
 
    try:
        os.rename(file_path, new_path)
        return new_path
    except Exception as e:
        raise RuntimeError(f"Failed to rename file: {e}")