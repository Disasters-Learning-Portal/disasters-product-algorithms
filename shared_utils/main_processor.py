"""
Main processor that combines all modules for complete file processing.
This is the primary entry point for COG conversion.
"""

import os
import gc
import tempfile
import uuid
import rasterio
import numpy as np
from datetime import datetime

# Try to import optimization methods
try:
    from rio_cogeo import cog_translate
    from rio_cogeo.profiles import cog_profiles as rio_cog_profiles
    HAS_RIO_COGEO = True
except ImportError:
    HAS_RIO_COGEO = False
    print("Warning: rio-cogeo not available, using fallback method")

# Try to import our optimized GDAL processor
try:
    from shared_utils.gdal_cog_processor import create_cog_gdal, process_file_optimized
    HAS_GDAL_PROCESSOR = True
except ImportError:
    HAS_GDAL_PROCESSOR = False
    print("Warning: GDAL COG processor not available")

# Import core modules
from shared_utils.s3_operations import (
    check_s3_file_exists,
    download_from_s3,
    upload_to_s3,
    setup_vsi_credentials,
    get_file_size_from_s3
)
from shared_utils.cog_validation import check_cog_with_warnings
from shared_utils.compression import set_nodata_value_src, get_predictor_for_dtype
from shared_utils.reprojection import calculate_transform_parameters, process_with_fixed_chunks

# Import utils
from shared_utils.memory_management import get_memory_usage, get_available_memory_mb
from shared_utils.error_handling import cleanup_temp_files, setup_temp_directory
from shared_utils.log_utils import print_status

# Import processors
from shared_utils.cog_creator import create_cog_with_overviews

# Import configs
from shared_utils.profiles import select_profile_by_size, get_compression_profile
from shared_utils.chunk_configs import get_chunk_config

# Import COG profiles - use the correct import path
try:
    from rasterio.cog import cog_profiles
except ImportError:
    # Fallback for older rasterio versions
    cog_profiles = {
        'zstd': {
            'driver': 'GTiff',
            'interleave': 'pixel',
            'tiled': True,
            'blockxsize': 512,
            'blockysize': 512,
            'compress': 'ZSTD',
            'photometric': 'MINISBLACK'
        },
        'lzw': {
            'driver': 'GTiff',
            'interleave': 'pixel',
            'tiled': True,
            'blockxsize': 512,
            'blockysize': 512,
            'compress': 'LZW'
        },
        'deflate': {
            'driver': 'GTiff',
            'interleave': 'pixel',
            'tiled': True,
            'blockxsize': 512,
            'blockysize': 512,
            'compress': 'DEFLATE'
        }
    }


def convert_to_cog(name, bucket, cog_filename, cog_data_bucket, cog_data_prefix,
                   s3_client, *, cog_profile=None, local_output_dir=None,
                   chunk_config=None, manual_nodata=None, overwrite=False,
                   skip_validation=False, target_crs='EPSG:3857',
                   resampling=None, clip_to_webmerc=None,
                   stream_from_s3=True, **_legacy_kwargs):
    """
    S3 orchestrator for COG conversion.

    Downloads `name` from `bucket`, runs the unified local warp+COG primitive
    (`shared_utils.cog_utils.convert_to_cog`), uploads the result to
    `s3://cog_data_bucket/cog_data_prefix/cog_filename`, and cleans up.

    Args:
        name: S3 key of source file (relative to `bucket`).
        bucket: Source S3 bucket.
        cog_filename: Output COG filename.
        cog_data_bucket: Destination S3 bucket.
        cog_data_prefix: Destination S3 prefix.
        s3_client: Boto3 S3 client.
        cog_profile: (deprecated, accepted for backwards compatibility — the
            engine selects its own COG profile internally).
        local_output_dir: If set, also save a copy of the output COG here.
        chunk_config: (deprecated, accepted for backwards compatibility —
            gdalwarp / rio cogeo handle their own chunking).
        manual_nodata: Manual no-data value (optional).
        overwrite: Whether to overwrite an existing S3 object (default: False).
        skip_validation: Whether to skip COG validation (default: False).
        target_crs: Target CRS string (default: 'EPSG:3857' — Web Mercator
            sidesteps the WGS 84 ensemble / lat-first axis bug that breaks
            rio_stac.get_dataset_geom in veda-data-airflow build_stac).
            Pass None or 'None'/'' to keep the source CRS.
        resampling: Warp resampling method ('near', 'bilinear', 'cubic',
            'average'). None defers to cog_utils auto-detect.
        clip_to_webmerc: Clip output extent to Web Mercator's valid domain.
            None defers to reprojection.needs_webmerc_clip() auto-detect
            (no-op for regional rasters, automatic for global / polar sources).
        stream_from_s3: When True (default) try `/vsis3/...` streaming so
            gdalwarp reads the source via byte-range requests instead of
            copying it to /tmp first. Falls back to download if the VSI probe
            fails (missing creds, network, etc.). Set False to force download
            when /tmp has enough space and you'd rather pay the latency
            up-front (recommended for ZSTD-22 heavy workloads).

    Returns:
        None (raises exception on error)
    """
    # Normalize target_crs: treat string 'None'/'none' and empty string as actual None.
    if isinstance(target_crs, str) and target_crs.strip().lower() in ('none', ''):
        target_crs = None

    start_time = datetime.now()
    s3_key = f"{cog_data_prefix}/{cog_filename}"
    temp_files = []

    # Local cog_utils import deferred — keeps this module importable even when
    # rasterio isn't available (e.g. environments that only need s3 helpers).
    from shared_utils.cog_utils import convert_to_cog as _convert_local_to_cog

    try:
        # 1. Check if destination object already exists.
        print(f"   [CHECK] s3://{cog_data_bucket}/{s3_key}")
        if check_s3_file_exists(s3_client, cog_data_bucket, s3_key):
            if overwrite:
                print(f"   [OVERWRITE] Exists but overwrite=True — reprocessing.")
            else:
                print(f"   [SKIP] Already exists, skipping: {cog_filename}")
                raise FileExistsError(f"File already exists: {cog_filename}")

        # 2. Resolve the source path — try VSI streaming first when allowed,
        # fall back to a local download. Streaming avoids the disk hit when
        # /tmp is tight or when we're processing many large rasters in a row.
        file_size_gb = get_file_size_from_s3(s3_client, bucket, name)
        print(f"   [INFO] Source size: {file_size_gb:.2f} GB")

        input_path = None
        if stream_from_s3 and setup_vsi_credentials(s3_client):
            vsi_path = f"/vsis3/{bucket}/{name}"
            print(f"   [STREAM] Attempting /vsis3 read: {vsi_path}")
            try:
                # Probe the file to confirm GDAL can open it via VSI. If this
                # raises (network blip, missing creds, etc.) we fall through
                # to the download path.
                import rasterio
                with rasterio.open(vsi_path) as _probe:
                    _ = _probe.profile
                input_path = vsi_path
                print(f"   [STREAM] ✅ Using /vsis3 streaming")
            except Exception as e:
                print(f"   [STREAM] ❌ Streaming probe failed ({e}); falling back to download")
                input_path = None

        if input_path is None:
            local_download_path = f"/tmp/data_download/{name}"
            os.makedirs(os.path.dirname(local_download_path), exist_ok=True)
            if os.path.exists(local_download_path):
                print(f"   [CACHE HIT] {local_download_path}")
            else:
                print(f"   [DOWNLOAD] From s3://{bucket}/{name}")
                if not download_from_s3(s3_client, bucket, name, local_download_path):
                    raise Exception(f"Failed to download s3://{bucket}/{name}")
                temp_files.append(local_download_path)
            input_path = local_download_path

        # 3. Delegate warp + COG to the unified local primitive.
        cog_output_path = f"/tmp/cog_{cog_filename}"
        temp_files.append(cog_output_path)
        initial_memory = get_memory_usage()
        print(f"   [MEMORY] Initial: {initial_memory:.1f} MB")
        print(f"   [COG] Delegating to cog_utils.convert_to_cog (dst_crs={target_crs}, "
              f"resampling={resampling or 'auto'}, clip_to_webmerc={clip_to_webmerc})")

        _convert_local_to_cog(
            input_tif=input_path,
            output_cog=cog_output_path,
            nodata=manual_nodata,
            dst_crs=target_crs,
            resampling_method=resampling,
            clip_to_webmerc=clip_to_webmerc,
            quiet=False,
        )

        # 4. Optional validation.
        if not skip_validation:
            is_valid = check_cog_with_warnings(cog_output_path)
            if not is_valid:
                print(f"   [WARNING] COG validation produced warnings — continuing.")

        # 5. Upload to S3.
        if not upload_to_s3(s3_client, cog_output_path, cog_data_bucket, s3_key):
            raise Exception("Failed to upload COG to S3")

        # 6. Optionally save a local copy.
        if local_output_dir:
            os.makedirs(local_output_dir, exist_ok=True)
            local_path = os.path.join(local_output_dir, cog_filename)
            import shutil
            shutil.copy2(cog_output_path, local_path)
            print(f"   [LOCAL] Saved to {local_path}")

        final_memory = get_memory_usage()
        total_time = (datetime.now() - start_time).total_seconds()
        print(f"   [MEMORY] Final: {final_memory:.1f} MB (Δ {final_memory - initial_memory:+.1f} MB)")
        print(f"   [TIME] Total: {total_time:.1f} s")

    except FileExistsError:
        # Re-raise without the generic error decoration; callers may special-case.
        raise
    except Exception as e:
        print(f"   [ERROR] {e}")
        raise
    finally:
        cleanup_temp_files(*temp_files)
        gc.collect()
