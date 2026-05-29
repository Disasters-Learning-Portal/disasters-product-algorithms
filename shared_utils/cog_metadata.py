"""
COG Metadata Module
===================

Create Cloud Optimized GeoTIFFs with embedded metadata tags.
Supports both in-memory (bytes via GDAL vsimem) and on-disk (file path) workflows.

Single responsibility: COG creation with custom metadata injection.
"""

import os
import re
import tempfile
from typing import Dict, List, Optional, Tuple, Union, Any
from datetime import datetime

from osgeo import gdal
import rasterio
from rasterio.io import MemoryFile
from rio_cogeo.cogeo import cog_validate, cog_translate

from shared_utils.version import PROCESSOR_STRING

# Enable GDAL exceptions
gdal.UseExceptions()

# Default filename pattern: YYYYMM_HAZARD_LOCATION_*.tif
DEFAULT_ACTIVATION_PATTERN = r'^(\d{6})_([A-Za-z0-9]+)_([A-Za-z0-9]+)_(.*)\.tif$'


def parse_activation_event(event_name: str) -> Dict[str, str]:
    """
    Parse YEAR_MONTH, HAZARD, LOCATION from an activation event name.

    Args:
        event_name: Event string like '202501_Flood_CA'.

    Returns:
        Dict with keys YEAR_MONTH, HAZARD, LOCATION (whichever are present).
    """
    parts = event_name.split('_', 2)
    result = {}
    if len(parts) >= 1:
        result['YEAR_MONTH'] = parts[0]
    if len(parts) >= 2:
        result['HAZARD'] = parts[1]
    if len(parts) >= 3:
        result['LOCATION'] = parts[2]
    return result


def detect_activation_event(
    filename: str,
    pattern: str = DEFAULT_ACTIVATION_PATTERN,
) -> Optional[Dict[str, str]]:
    """
    Auto-detect activation event metadata from a filename.

    Args:
        filename: Basename of the file (e.g. '202501_Flood_CA_trueColor.tif').
        pattern: Regex with groups (year_month, hazard, location, remainder).

    Returns:
        Metadata dict or None if the pattern doesn't match.
    """
    match = re.match(pattern, filename)
    if not match:
        return None
    year_month, hazard, location, _ = match.groups()
    return {
        'ACTIVATION_EVENT': f"{year_month}_{hazard}_{location}",
        'YEAR_MONTH': year_month,
        'HAZARD': hazard,
        'LOCATION': location,
        'PROCESSOR': PROCESSOR_STRING,
    }


def resolve_metadata(
    filename: str,
    mode: str = 'manual',
    manual_metadata: Optional[Dict[str, str]] = None,
    pattern: str = DEFAULT_ACTIVATION_PATTERN,
) -> Dict[str, str]:
    """
    Build the full metadata dict for a file.

    In 'manual' mode, starts from manual_metadata and auto-parses
    YEAR_MONTH/HAZARD/LOCATION from ACTIVATION_EVENT if not already set.

    In 'auto' mode, tries to detect from the filename first, falls back
    to manual_metadata.

    Args:
        filename: Basename of the file.
        mode: 'manual' or 'auto'.
        manual_metadata: Base metadata dict (used in manual mode or as fallback).
        pattern: Regex pattern for auto-detection.

    Returns:
        Complete metadata dict.
    """
    manual_metadata = manual_metadata or {}

    if mode == 'auto':
        detected = detect_activation_event(filename, pattern)
        if detected:
            return detected
        print(f"  WARNING: No pattern match for {filename}, using manual fallback")

    # Manual mode (or auto fallback)
    meta = dict(manual_metadata)
    if 'ACTIVATION_EVENT' in meta:
        parsed = parse_activation_event(meta['ACTIVATION_EVENT'])
        for key, val in parsed.items():
            if key not in meta:
                meta[key] = val
    return meta


def read_compression_settings(input_data: Union[bytes, str]) -> Dict[str, Any]:
    """
    Read compression settings from a GeoTIFF file.

    Args:
        input_data: GeoTIFF bytes or file path string.

    Returns:
        Dict with keys: 'compress', 'predictor', 'level'.
        Values may be None if not detected.
    """
    if isinstance(input_data, bytes):
        with MemoryFile(input_data) as memfile:
            with memfile.open() as src:
                profile = src.profile.copy()
    else:
        with rasterio.open(input_data) as src:
            profile = src.profile.copy()

    compress = profile.get('compress', 'ZSTD')
    predictor = profile.get('predictor', None)

    # Extract compression level
    level = None
    if 'zstd_level' in profile:
        level = profile['zstd_level']
    elif 'zlevel' in profile:
        level = profile['zlevel']

    return {
        'compress': compress,
        'predictor': predictor,
        'level': level,
    }


def create_cog_with_metadata(
    input_data: Union[bytes, str],
    metadata: Dict[str, str],
    output_path: Optional[str] = None,
    preserve_compression: bool = True,
    compression_override: Optional[Dict[str, Any]] = None,
    target_crs: Optional[str] = None,
    blockxsize: int = 512,
    blockysize: int = 512,
    overview_level: int = 4,
    overview_resampling: str = 'average',
    web_optimized: bool = True,
    add_mask: bool = False,
    quiet: bool = False,
) -> Union[bytes, str]:
    """
    Create a Cloud Optimized GeoTIFF with custom metadata tags.

    Works in-memory (bytes in, bytes out) or on-disk (path in, path out).
    Preserves original compression settings from the source file by default.

    Args:
        input_data: Raw GeoTIFF bytes or a file path string.
        metadata: Dictionary of metadata tags to embed (arbitrary key-value pairs).
        output_path: If provided, write the COG to this path and return it.
                     If None and input_data is bytes, return output bytes.
                     If None and input_data is a path, write to a temp file and return path.
        preserve_compression: Read compression settings from source and reuse (default True).
        compression_override: Dict to override compression settings.
                              Keys: 'compress', 'predictor', 'level' (any subset).
        target_crs: Target CRS string (e.g. 'EPSG:4326') for reprojection.
                    None = keep the original CRS. String 'None'/'none'/'' treated as None.
        blockxsize: Tile width (default 512).
        blockysize: Tile height (default 512).
        overview_level: Number of overview levels (default 4).
        overview_resampling: Resampling method for overviews (default 'average').
        web_optimized: Create web-optimized COG layout (default True).
        add_mask: Add mask band (default False).
        quiet: Suppress progress output (default False).

    Returns:
        If input was bytes and no output_path: returns COG as bytes.
        If output_path was provided or input was a file path: returns output file path.

    Raises:
        Exception: If COG creation fails.
    """
    # Normalize target_crs
    if isinstance(target_crs, str) and target_crs.strip().lower() in ('none', ''):
        target_crs = None

    is_bytes = isinstance(input_data, bytes)

    if is_bytes:
        return _create_cog_in_memory(
            file_bytes=input_data,
            metadata=metadata,
            output_path=output_path,
            preserve_compression=preserve_compression,
            compression_override=compression_override,
            target_crs=target_crs,
            blockxsize=blockxsize,
            blockysize=blockysize,
            overview_level=overview_level,
            overview_resampling=overview_resampling,
            web_optimized=web_optimized,
            add_mask=add_mask,
            quiet=quiet,
        )
    else:
        return _create_cog_on_disk(
            input_path=input_data,
            metadata=metadata,
            output_path=output_path,
            preserve_compression=preserve_compression,
            compression_override=compression_override,
            target_crs=target_crs,
            blockxsize=blockxsize,
            blockysize=blockysize,
            overview_level=overview_level,
            overview_resampling=overview_resampling,
            web_optimized=web_optimized,
            add_mask=add_mask,
            quiet=quiet,
        )


def validate_cog_in_memory(file_bytes: bytes, filename: str = "temp.tif") -> Tuple[bool, dict]:
    """
    Validate COG structure from in-memory bytes using GDAL vsimem.

    Args:
        file_bytes: GeoTIFF file content as bytes.
        filename: Display name for reporting.

    Returns:
        Tuple of (is_valid, info_dict) where info_dict contains:
            is_cog, errors, warnings, width, height, bands,
            compression, blocksize, overviews.
    """
    vsimem_path = f'/vsimem/validate_{filename}'

    try:
        gdal.FileFromMemBuffer(vsimem_path, file_bytes)

        is_valid, errors, warnings = cog_validate(vsimem_path)

        ds = gdal.Open(vsimem_path)
        if ds:
            info = {
                'is_cog': is_valid,
                'errors': errors,
                'warnings': warnings,
                'width': ds.RasterXSize,
                'height': ds.RasterYSize,
                'bands': ds.RasterCount,
                'compression': ds.GetMetadataItem('COMPRESSION', 'IMAGE_STRUCTURE') or 'None',
                'blocksize': (
                    ds.GetRasterBand(1).GetBlockSize()
                    if ds.RasterCount > 0
                    else (None, None)
                ),
                'overviews': (
                    ds.GetRasterBand(1).GetOverviewCount()
                    if ds.RasterCount > 0
                    else 0
                ),
            }
            ds = None
        else:
            info = {'is_cog': False, 'errors': ['Could not open file'], 'warnings': []}

        return is_valid, info

    finally:
        gdal.Unlink(vsimem_path)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_cog_profile(
    source_settings: Dict[str, Any],
    preserve_compression: bool,
    compression_override: Optional[Dict[str, Any]],
    blockxsize: int,
    blockysize: int,
) -> dict:
    """Build the COG creation profile dict from source settings and overrides."""
    if preserve_compression:
        compress = source_settings.get('compress', 'ZSTD')
        predictor = source_settings.get('predictor', None)
        level = source_settings.get('level', None)
    else:
        compress = 'ZSTD'
        predictor = None
        level = None

    # Apply overrides
    if compression_override:
        compress = compression_override.get('compress', compress)
        predictor = compression_override.get('predictor', predictor)
        level = compression_override.get('level', level)

    profile = {
        'driver': 'GTiff',
        'interleave': 'pixel',
        'tiled': True,
        'blockxsize': blockxsize,
        'blockysize': blockysize,
        'compress': compress,
    }

    # Add compression level
    if level is not None:
        if str(compress).upper() == 'ZSTD':
            profile['zstd_level'] = level
        elif str(compress).upper() in ('DEFLATE', 'LZW'):
            profile['zlevel'] = level

    # Add predictor
    if predictor is not None:
        profile['predictor'] = predictor

    return profile


def _create_cog_in_memory(
    file_bytes: bytes,
    metadata: Dict[str, str],
    output_path: Optional[str],
    preserve_compression: bool,
    compression_override: Optional[Dict[str, Any]],
    target_crs: Optional[str],
    blockxsize: int,
    blockysize: int,
    overview_level: int,
    overview_resampling: str,
    web_optimized: bool,
    add_mask: bool,
    quiet: bool,
) -> Union[bytes, str]:
    """In-memory COG creation via GDAL vsimem."""
    input_vsi = '/vsimem/cog_meta_input.tif'
    output_vsi = '/vsimem/cog_meta_output.tif'

    try:
        gdal.FileFromMemBuffer(input_vsi, file_bytes)

        # Read source compression settings
        source_settings = read_compression_settings(file_bytes)
        if not quiet:
            print(f"    Source compression: {source_settings['compress']}, "
                  f"predictor={source_settings['predictor']}, level={source_settings['level']}")

        cog_profile = _build_cog_profile(
            source_settings, preserve_compression, compression_override,
            blockxsize, blockysize,
        )

        if not quiet:
            print(f"    Output compression: {cog_profile.get('compress')}")
            if target_crs:
                print(f"    Reprojecting to: {target_crs}")
            else:
                print("    Keeping original CRS")
            print("    Creating COG with metadata...")

        # Auto-add PROCESSING_DATE if not provided
        tags = dict(metadata)
        if 'PROCESSING_DATE' not in tags:
            tags['PROCESSING_DATE'] = datetime.utcnow().isoformat()

        # If reprojection needed, wrap source in a WarpedVRT
        source_for_translate = input_vsi
        vrt_handle = None

        if target_crs is not None:
            from rasterio.vrt import WarpedVRT
            from rasterio.enums import Resampling

            src_ds = rasterio.open(input_vsi)
            src_crs = str(src_ds.crs).upper() if src_ds.crs else None

            if src_crs and src_crs != target_crs.upper():
                vrt_handle = WarpedVRT(
                    src_ds, crs=target_crs,
                    resampling=Resampling.bilinear,
                    nodata=src_ds.nodata,
                )
                source_for_translate = vrt_handle
            else:
                src_ds.close()
                if not quiet:
                    print(f"    Already in {target_crs}, skipping reprojection")

        cog_translate(
            source=source_for_translate,
            dst_path=output_vsi,
            dst_kwargs=cog_profile,
            add_mask=add_mask,
            overview_level=overview_level,
            overview_resampling=overview_resampling,
            web_optimized=web_optimized,
            additional_cog_metadata=tags,
            quiet=quiet,
        )

        # Clean up VRT handle
        if vrt_handle is not None:
            vrt_handle.close()
            src_ds.close()

        if not quiet:
            print("    COG creation complete")

        # If caller wants a file on disk, copy vsimem → disk
        if output_path:
            _vsimem_to_file(output_vsi, output_path)
            if not quiet:
                print(f"    Written to {output_path}")
            return output_path

        # Otherwise return bytes
        output_bytes = _read_vsimem(output_vsi)
        if not quiet:
            print(f"    Output size: {len(output_bytes):,} bytes")
        return output_bytes

    finally:
        for p in (input_vsi, output_vsi):
            try:
                gdal.Unlink(p)
            except Exception:
                pass


def _create_cog_on_disk(
    input_path: str,
    metadata: Dict[str, str],
    output_path: Optional[str],
    preserve_compression: bool,
    compression_override: Optional[Dict[str, Any]],
    target_crs: Optional[str],
    blockxsize: int,
    blockysize: int,
    overview_level: int,
    overview_resampling: str,
    web_optimized: bool,
    add_mask: bool,
    quiet: bool,
) -> str:
    """On-disk COG creation via temp files."""
    temp_output = None

    try:
        # Read source compression settings
        source_settings = read_compression_settings(input_path)
        if not quiet:
            print(f"    Source compression: {source_settings['compress']}, "
                  f"predictor={source_settings['predictor']}, level={source_settings['level']}")

        cog_profile = _build_cog_profile(
            source_settings, preserve_compression, compression_override,
            blockxsize, blockysize,
        )

        if not quiet:
            print(f"    Output compression: {cog_profile.get('compress')}")
            if target_crs:
                print(f"    Reprojecting to: {target_crs}")
            else:
                print("    Keeping original CRS")
            print("    Creating COG with metadata...")

        # Auto-add PROCESSING_DATE if not provided
        tags = dict(metadata)
        if 'PROCESSING_DATE' not in tags:
            tags['PROCESSING_DATE'] = datetime.utcnow().isoformat()

        # Determine output path
        if output_path is None:
            fd, temp_output = tempfile.mkstemp(suffix='.tif', dir='/tmp')
            os.close(fd)
            dst = temp_output
        else:
            dst = output_path

        # If reprojection needed, wrap source in a WarpedVRT
        source_for_translate = input_path
        vrt_handle = None
        src_ds = None

        if target_crs is not None:
            from rasterio.vrt import WarpedVRT
            from rasterio.enums import Resampling

            src_ds = rasterio.open(input_path)
            src_crs = str(src_ds.crs).upper() if src_ds.crs else None

            if src_crs and src_crs != target_crs.upper():
                vrt_handle = WarpedVRT(
                    src_ds, crs=target_crs,
                    resampling=Resampling.bilinear,
                    nodata=src_ds.nodata,
                )
                source_for_translate = vrt_handle
            else:
                src_ds.close()
                src_ds = None
                if not quiet:
                    print(f"    Already in {target_crs}, skipping reprojection")

        cog_translate(
            source=source_for_translate,
            dst_path=dst,
            dst_kwargs=cog_profile,
            add_mask=add_mask,
            overview_level=overview_level,
            overview_resampling=overview_resampling,
            web_optimized=web_optimized,
            additional_cog_metadata=tags,
            quiet=quiet,
        )

        # Clean up VRT handle
        if vrt_handle is not None:
            vrt_handle.close()
        if src_ds is not None:
            src_ds.close()

        if not quiet:
            size_mb = os.path.getsize(dst) / (1024 * 1024)
            print(f"    COG creation complete ({size_mb:.1f} MB)")
            print(f"    Written to {dst}")

        return dst

    except Exception:
        # Clean up temp file on error (but not user-specified output_path)
        if temp_output and os.path.exists(temp_output):
            os.unlink(temp_output)
        raise


def _read_vsimem(vsimem_path: str) -> bytes:
    """Read bytes from a GDAL vsimem path."""
    vsi_file = gdal.VSIFOpenL(vsimem_path, 'rb')
    if not vsi_file:
        raise Exception(f"Could not read {vsimem_path}")

    gdal.VSIFSeekL(vsi_file, 0, 2)
    file_size = gdal.VSIFTellL(vsi_file)
    gdal.VSIFSeekL(vsi_file, 0, 0)

    data = gdal.VSIFReadL(1, file_size, vsi_file)
    gdal.VSIFCloseL(vsi_file)
    return data


def _vsimem_to_file(vsimem_path: str, file_path: str) -> None:
    """Copy a GDAL vsimem file to disk."""
    data = _read_vsimem(vsimem_path)
    with open(file_path, 'wb') as f:
        f.write(data)
