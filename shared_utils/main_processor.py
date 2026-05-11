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
                   s3_client, cog_profile=None, local_output_dir=None,
                   chunk_config=None, manual_nodata=None, overwrite=False,
                   skip_validation=False, target_crs='EPSG:4326'):
    """
    Main function to convert a file to Cloud Optimized GeoTIFF.

    Args:
        name: S3 key of source file
        bucket: Source S3 bucket
        cog_filename: Output COG filename
        cog_data_bucket: Destination S3 bucket
        cog_data_prefix: Destination S3 prefix
        s3_client: Boto3 S3 client
        cog_profile: COG profile (optional)
        local_output_dir: Local output directory (optional)
        chunk_config: Chunk configuration (optional)
        manual_nodata: Manual no-data value (optional)
        overwrite: Whether to overwrite existing files (default: False)
        skip_validation: Whether to skip COG validation (default: False)
        target_crs: Target CRS string (e.g. 'EPSG:4326') or None to keep
            the original CRS without reprojection. Default: 'EPSG:4326'.

    Returns:
        None (raises exception on error)
    """
    # Normalize target_crs: treat string 'None'/'none' and empty string as actual None
    if isinstance(target_crs, str) and target_crs.strip().lower() in ('none', ''):
        target_crs = None

    # Initialize
    start_time = datetime.now()
    s3_key = f"{cog_data_prefix}/{cog_filename}"
    reproject_filename = f"/tmp/reproj/{cog_filename}"
    temp_files = []

    try:
        # Step 1: Check if file already exists in S3
        print(f"   [CHECK] Checking if file already exists in S3: s3://{cog_data_bucket}/{s3_key}")
        if check_s3_file_exists(s3_client, cog_data_bucket, s3_key):
            if overwrite:
                print(f"   [OVERWRITE] File exists but overwrite=True, reprocessing: {cog_filename}")
            else:
                print(f"   [SKIP] File already exists in S3, skipping processing: {cog_filename}")
                raise FileExistsError(f"File already exists: {cog_filename}")

        # Step 2: Get file size and select appropriate configuration
        file_size_gb = get_file_size_from_s3(s3_client, bucket, name)
        print(f"   [INFO] File size: {file_size_gb:.1f} GB")

        # Auto-select configuration if not provided
        if chunk_config is None:
            chunk_config = get_chunk_config(file_size_gb)
            print(f"   [CONFIG] Using {'fixed' if not chunk_config['adaptive_chunks'] else 'adaptive'} chunks")

        # Step 3: Setup directories
        os.makedirs("/tmp/reproj", exist_ok=True)
        setup_temp_directory()

        # Step 4: Memory monitoring
        initial_memory = get_memory_usage()
        available_memory = get_available_memory_mb()
        print(f"   [MEMORY] Initial: {initial_memory:.1f} MB, Available: {available_memory:.1f} MB")

        # Step 5: Determine input path (streaming vs download)
        input_path = None

        # Try streaming first if configured
        if chunk_config.get('use_streaming', True) and setup_vsi_credentials(s3_client):
            input_path = f"/vsis3/{bucket}/{name}"
            print(f"   [STREAM] Attempting to stream from S3: {input_path}")

            # Test if streaming works
            try:
                with rasterio.open(input_path) as test_src:
                    _ = test_src.profile
                print(f"   [STREAM] ✅ Successfully opened file via streaming")
            except Exception as e:
                print(f"   [STREAM] ❌ Streaming failed: {e}")
                input_path = None

        # Fallback to download
        if input_path is None:
            local_download_path = f"/tmp/data_download/{name}"
            os.makedirs(os.path.dirname(local_download_path), exist_ok=True)

            if os.path.exists(local_download_path):
                print(f"   [CACHE HIT] Using cached file: {local_download_path}")
                input_path = local_download_path
            else:
                print(f"   [DOWNLOAD] Downloading from S3...")
                if download_from_s3(s3_client, bucket, name, local_download_path):
                    input_path = local_download_path
                    temp_files.append(local_download_path)
                else:
                    raise Exception("Failed to download file from S3")

        # Step 6: Use best available optimization method
        # Priority: 1) GDAL COG driver, 2) rio-cogeo, 3) fallback

        if HAS_GDAL_PROCESSOR and file_size_gb < 10.0:  # Use GDAL for files under 10GB
            print(f"   [OPTIMIZED] Using GDAL COG driver for maximum performance")
            cog_output_path = f"/tmp/cog_{cog_filename}"

            # Ensure output directory exists
            output_dir = os.path.dirname(cog_output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)

            temp_files.append(cog_output_path)

            # Get nodata value
            with rasterio.open(input_path) as src:
                if manual_nodata is not None:
                    src_nodata = manual_nodata
                    print(f"   [NODATA] Using manual no-data value: {manual_nodata}")
                elif src.nodata is not None:
                    src_nodata = src.nodata
                    print(f"   [NODATA] Using existing no-data value: {src.nodata}")
                else:
                    src_nodata = set_nodata_value_src(src, manual_nodata)

            # Use GDAL COG processor
            success = process_file_optimized(
                input_path,
                cog_output_path,
                nodata=src_nodata,
                file_size_gb=file_size_gb,
                reproject=target_crs is not None,
                target_crs=target_crs,
                verbose=True
            )

            if success:
                print(f"   [GDAL-COG] ✅ COG created successfully")

                # Upload to S3
                if upload_to_s3(s3_client, cog_output_path, cog_data_bucket, s3_key):
                    # Save locally if requested
                    if local_output_dir:
                        os.makedirs(local_output_dir, exist_ok=True)
                        local_path = os.path.join(local_output_dir, cog_filename)
                        import shutil
                        shutil.copy2(cog_output_path, local_path)
                        print(f"   [LOCAL] Saved to {local_path}")
                else:
                    raise Exception("Failed to upload COG to S3")

                # Report performance
                final_memory = get_memory_usage()
                print(f"   [MEMORY] Final: {final_memory:.1f} MB (Change: {final_memory - initial_memory:+.1f} MB)")
                total_time = (datetime.now() - start_time).total_seconds()
                print(f"   [TIME] Total processing time: {total_time:.1f} seconds")

                # Clean up and return early
                cleanup_temp_files(*temp_files)
                gc.collect()
                return

            else:
                print(f"   [GDAL-COG] Failed, trying rio-cogeo fallback...")

        # Fallback to rio-cogeo if GDAL processor failed or not available
        if HAS_RIO_COGEO and file_size_gb < 5.0:  # Use rio-cogeo for files under 5GB
            print(f"   [OPTIMIZED] Using rio-cogeo for single-pass COG creation")
            cog_output_path = f"/tmp/cog_{cog_filename}"

            # Ensure output directory exists
            output_dir = os.path.dirname(cog_output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)

            temp_files.append(cog_output_path)

            # Get nodata values and check if remapping is needed
            original_nodata = None
            needs_remapping = False

            with rasterio.open(input_path) as src:
                original_nodata = src.nodata

                if manual_nodata is not None:
                    src_nodata = manual_nodata
                    print(f"   [NODATA] Using manual no-data value: {manual_nodata}")
                    if original_nodata is not None and original_nodata != manual_nodata:
                        needs_remapping = True
                        print(f"   [NODATA] Will remap nodata: {original_nodata} → {manual_nodata}")
                elif src.nodata is not None:
                    src_nodata = src.nodata
                    print(f"   [NODATA] Using existing no-data value: {src.nodata}")
                else:
                    src_nodata = set_nodata_value_src(src, manual_nodata)

                # Get predictor for data type
                predictor = get_predictor_for_dtype(src.dtypes[0])

            # Prepare COG profile
            output_profile = rio_cog_profiles.get("zstd")
            output_profile.update({
                "ZSTD_LEVEL": 22,  # Maximum compression
                "PREDICTOR": predictor,
                "BLOCKSIZE": 512
            })

            # Additional GDAL options for performance
            config = {
                "GDAL_NUM_THREADS": "ALL_CPUS",
                "GDAL_TIFF_INTERNAL_MASK": "YES",
                "GDAL_TIFF_OVR_BLOCKSIZE": "512"
            }

            # If remapping is needed, process through temporary file with pixel remapping
            if needs_remapping:
                from shared_utils.compression import remap_nodata_value
                from rasterio.enums import Resampling

                print(f"   [COG] Creating COG with nodata remapping" +
                      (f" and reprojection to {target_crs}..." if target_crs else " (keeping original CRS)..."))
                temp_remapped = f"/tmp/temp_remapped_{uuid.uuid4().hex}.tif"
                temp_files.append(temp_remapped)

                with rasterio.open(input_path) as src:
                    from rasterio.warp import calculate_default_transform, reproject

                    if target_crs is not None:
                        dst_crs = target_crs
                        transform, width, height = calculate_default_transform(
                            src.crs, dst_crs, src.width, src.height, *src.bounds
                        )
                    else:
                        dst_crs = src.crs
                        transform = src.transform
                        width = src.width
                        height = src.height

                    # Setup output profile
                    kwargs = src.meta.copy()
                    kwargs.update({
                        'crs': dst_crs,
                        'transform': transform,
                        'width': width,
                        'height': height,
                        'nodata': src_nodata  # Use new nodata in output
                    })

                    # Write reprojected data with remapped nodata
                    with rasterio.open(temp_remapped, 'w', **kwargs) as dst:
                        for band_idx in range(1, src.count + 1):
                            reproject(
                                source=rasterio.band(src, band_idx),
                                destination=rasterio.band(dst, band_idx),
                                src_transform=src.transform,
                                src_crs=src.crs,
                                src_nodata=original_nodata,
                                dst_transform=transform,
                                dst_crs=dst_crs,
                                dst_nodata=src_nodata,
                                resampling=Resampling.bilinear
                            )

                # Now create COG from the remapped file
                with rasterio.open(temp_remapped) as vrt:
                    cog_translate(
                        vrt,
                        cog_output_path,
                        output_profile,
                        nodata=src_nodata,
                        overview_level=5,
                        overview_resampling="average",
                        config=config,
                        quiet=False,
                        in_memory=False,
                        use_cog_driver=False
                    )
            else:
                from rasterio.vrt import WarpedVRT
                from rasterio.enums import Resampling

                with rasterio.open(input_path) as src:
                    if target_crs is not None:
                        # Reproject to target CRS via WarpedVRT
                        print(f"   [COG] Creating COG with reprojection to {target_crs} in single pass...")
                        with WarpedVRT(src, crs=target_crs,
                                      resampling=Resampling.bilinear,
                                      nodata=src_nodata) as vrt:
                            cog_translate(
                                vrt,
                                cog_output_path,
                                output_profile,
                                nodata=src_nodata,
                                overview_level=5,
                                overview_resampling="average",
                                config=config,
                                quiet=False,
                                in_memory=False,
                                use_cog_driver=False
                            )
                    else:
                        # Keep original CRS — translate directly without WarpedVRT
                        print(f"   [COG] Creating COG with original CRS ({src.crs}) in single pass...")
                        cog_translate(
                            src,
                            cog_output_path,
                            output_profile,
                            nodata=src_nodata,
                            overview_level=5,
                            overview_resampling="average",
                            config=config,
                            quiet=False,
                            in_memory=False,
                            use_cog_driver=False
                        )

            print(f"   [COG] ✅ COG created successfully")

            # Optional validation
            if not skip_validation:
                is_valid = check_cog_with_warnings(cog_output_path)
                if not is_valid:
                    print(f"   [WARNING] COG validation had warnings but continuing...")

            # Upload to S3
            if upload_to_s3(s3_client, cog_output_path, cog_data_bucket, s3_key):
                # Save locally if requested
                if local_output_dir:
                    os.makedirs(local_output_dir, exist_ok=True)
                    local_path = os.path.join(local_output_dir, cog_filename)
                    import shutil
                    shutil.copy2(cog_output_path, local_path)
                    print(f"   [LOCAL] Saved to {local_path}")
            else:
                raise Exception("Failed to upload COG to S3")

            # Report performance
            final_memory = get_memory_usage()
            print(f"   [MEMORY] Final: {final_memory:.1f} MB (Change: {final_memory - initial_memory:+.1f} MB)")
            total_time = (datetime.now() - start_time).total_seconds()
            print(f"   [TIME] Total processing time: {total_time:.1f} seconds")

            # Clean up and return early - we're done!
            cleanup_temp_files(*temp_files)
            gc.collect()
            return

        # Step 7: Fall back to original processing for large files or if rio-cogeo unavailable
        print(f"   [FALLBACK] Using original processing method")
        with rasterio.open(input_path) as src:
            # Check if we should use whole-file processing for small files
            use_whole_file = chunk_config.get('use_whole_file_processing', False)

            if use_whole_file and file_size_gb < 1.5:
                print(f"   [WHOLE-FILE] Small/medium file detected ({file_size_gb:.2f} GB), processing without chunks")
            else:
                # Get chunk size based on configuration
                chunk_size = chunk_config.get('default_chunk_size', 512)
                if not chunk_config.get('adaptive_chunks', True):
                    print(f"   [CHUNKS] Using FIXED chunk size: {chunk_size}x{chunk_size}")
                else:
                    print(f"   [CHUNKS] Using adaptive chunk size starting at: {chunk_size}x{chunk_size}")

            # Calculate reprojection parameters
            if target_crs is not None:
                dst_crs = target_crs
                if use_whole_file and file_size_gb < 1.5:
                    print(f"   [REPROJECT] Converting to {dst_crs} using whole-file processing...")
                else:
                    print(f"   [REPROJECT] Converting to {dst_crs} using fixed-grid chunked processing...")
                transform, width, height = calculate_transform_parameters(src, dst_crs)
            else:
                dst_crs = src.crs
                transform = src.transform
                width = src.width
                height = src.height
                print(f"   [CRS] Keeping original CRS: {dst_crs}")

            # Get or set nodata value and check if remapping is needed
            original_nodata = src.nodata
            if manual_nodata is not None:
                # Use manual no-data if provided
                src_nodata = manual_nodata
                print(f"   [NODATA] Using manual no-data value: {manual_nodata}")
                if original_nodata is not None and original_nodata != manual_nodata:
                    print(f"   [NODATA] Will remap nodata: {original_nodata} → {manual_nodata}")
            elif src.nodata is not None:
                src_nodata = src.nodata
                print(f"   [NODATA] Using existing no-data value: {src.nodata}")
            else:
                src_nodata = set_nodata_value_src(src, manual_nodata)

            # Get appropriate predictor
            predictor = get_predictor_for_dtype(src.dtypes[0])

            # Prepare output profile using rasterio's COG profile
            # Start with a COG profile that ensures proper structure
            cog_profile = cog_profiles.get('zstd')
            kwargs = src.meta.copy()
            kwargs.update(cog_profile)
            kwargs.update({
                'crs': dst_crs,
                'transform': transform,
                'width': width,
                'height': height,
                'nodata': src_nodata,
                'compress': 'ZSTD',
                'zstd_level': 22,  # Maximum compression as requested
                'predictor': predictor,
                'blockxsize': 512,
                'blockysize': 512,
                'tiled': True,
                'interleave': 'pixel' if src.count > 1 else 'band',
                'BIGTIFF': 'IF_SAFER'
            })

            # Process based on file size
            with rasterio.open(reproject_filename, 'w', **kwargs) as dst:
                if use_whole_file and file_size_gb < 1.5:
                    # Import the whole-file processing function
                    from shared_utils.reprojection import process_whole_file
                    process_whole_file(
                        src, dst, src.crs, dst_crs, transform,
                        width, height, original_nodata, src_nodata
                    )
                else:
                    # Use chunked processing for larger files
                    chunk_size = chunk_config.get('default_chunk_size', 512)
                    process_with_fixed_chunks(
                        src, dst, src.crs, dst_crs, transform,
                        width, height, chunk_size, original_nodata,
                        chunk_config, initial_memory, src_nodata
                    )

        # Add overviews to make it a valid COG
        from shared_utils.reprojection import add_cog_overviews
        add_cog_overviews(reproject_filename)

        temp_files.append(reproject_filename)

        # Step 7: Validate the COG (it already has overviews from reprojection)
        is_valid_cog = check_cog_with_warnings(reproject_filename)

        if is_valid_cog:
            print(f"   [COG] ✅ File is a valid COG with overviews")
            # Upload directly to S3
            if upload_to_s3(s3_client, reproject_filename, cog_data_bucket, s3_key):
                # Save locally if requested
                if local_output_dir:
                    os.makedirs(local_output_dir, exist_ok=True)
                    local_path = os.path.join(local_output_dir, cog_filename)
                    import shutil
                    shutil.copy2(reproject_filename, local_path)
                    print(f"   [LOCAL] Saved to {local_path}")
            else:
                raise Exception("Failed to upload COG to S3")
        else:
            # Fallback: Create COG with overviews if validation failed
            print(f"   [COG] File needs COG optimization...")
            file_size_mb = os.path.getsize(reproject_filename) / (1024 * 1024)
            print(f"   [COG] Processing {file_size_mb:.1f} MB file...")

            # Get compression configuration
            compression_config = get_compression_profile(
                dtype=str(src.dtypes[0]),
                file_size_gb=file_size_mb / 1024
            )

            # Create temporary COG with overviews
            temp_cog = f"/tmp/cog_{datetime.now().strftime('%Y%m%d_%H%M%S')}.tif"
            temp_files.append(temp_cog)

            if create_cog_with_overviews(reproject_filename, temp_cog, compression_config):
                # Upload to S3
                if upload_to_s3(s3_client, temp_cog, cog_data_bucket, s3_key):
                    # Save locally if requested
                    if local_output_dir:
                        os.makedirs(local_output_dir, exist_ok=True)
                        local_path = os.path.join(local_output_dir, cog_filename)
                        import shutil
                        shutil.copy2(temp_cog, local_path)
                        print(f"   [LOCAL] Saved to {local_path}")
                else:
                    raise Exception("Failed to upload COG to S3")
            else:
                raise Exception("Failed to create COG")

        # Step 9: Report memory usage
        final_memory = get_memory_usage()
        print(f"   [MEMORY] Final: {final_memory:.1f} MB (Change: {final_memory - initial_memory:+.1f} MB)")

        # Step 10: Report total time
        total_time = (datetime.now() - start_time).total_seconds()
        print(f"   [TIME] Total processing time: {total_time:.1f} seconds")

    except Exception as e:
        print(f"   [ERROR] {str(e)}")

        # Check for specific errors and retry
        error_msg = str(e).lower()
        if ("streaming_chunk_error" in error_msg or
            "chunk and warp" in error_msg) and chunk_config.get('use_streaming', True):
            print(f"   [RETRY] Streaming error detected, retrying with download...")

            # Retry with download
            new_config = chunk_config.copy()
            new_config['use_streaming'] = False
            return convert_to_cog(
                name, bucket, cog_filename, cog_data_bucket, cog_data_prefix,
                s3_client, cog_profile, local_output_dir, new_config
            )

        raise

    finally:
        # Cleanup temporary files
        cleanup_temp_files(*temp_files)
        gc.collect()