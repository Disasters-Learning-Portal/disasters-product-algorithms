"""Cloud-Optimized GeoTIFF (COG) generation - SIMPLIFIED."""

import logging
import os
import tempfile
from typing import Any, Literal

import boto3
import numpy as np
import rasterio
from boto3.s3.transfer import TransferConfig
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from rasterio.shutil import copy

from blackmarble.typing import ArrayLike


logger = logging.getLogger(__name__)

# Type aliases
Transform = Any  # rasterio.transform.Affine
CRS = Any  # rasterio.crs.CRS


def create_cog_local(
    data: ArrayLike | str,
    output_path: str,
    transform: Transform,
    crs: CRS,
    metadata: dict[str, Any] | None = None,
    compress: Literal["deflate", "lzw", "jpeg", "webp", "zstd"] = "deflate",
    tiled: bool = True,
    blocksize: int = 512,
    overviews: list[int] | None = None,
    nodata: float | None = None,
) -> str:
    """Create a Cloud-Optimized GeoTIFF from data."""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    if compress in ["jpeg", "webp"]:
        raise ValueError(f"Lossy compression '{compress}' not suitable for scientific data")

    if overviews is None:
        overviews = [2, 4, 8, 16, 32]

    if isinstance(data, str):
        # Convert existing file to COG
        with rasterio.open(data) as src:
            profile = src.profile.copy()
            profile.update(
                {
                    "driver": "GTiff",
                    "tiled": tiled,
                    "blockxsize": blocksize,
                    "blockysize": blocksize,
                    "compress": compress,
                    "BIGTIFF": "IF_SAFER",
                }
            )

            with MemoryFile() as memfile:
                with memfile.open(**profile) as dst:  # type: ignore[attr-defined]
                    for i in range(1, src.count + 1):
                        dst.write(src.read(i), i)  # type: ignore[attr-defined]

                    if metadata:
                        dst.update_tags(**metadata)  # type: ignore[attr-defined]

                    resampling = (
                        Resampling.nearest
                        if np.issubdtype(src.dtypes[0], np.integer)
                        else Resampling.bilinear
                    )
                    dst.build_overviews(overviews, resampling)  # type: ignore[attr-defined]

                copy(memfile, output_path, copy_src_overviews=True, **profile)
    else:
        # Create COG from array
        data = np.asarray(data)

        if data.ndim == 2:
            count, height, width = 1, data.shape[0], data.shape[1]
        elif data.ndim == 3:
            count, height, width = data.shape
        else:
            raise ValueError(f"Data must be 2D or 3D, got shape {data.shape}")

        profile = {
            "driver": "GTiff",
            "dtype": data.dtype,
            "count": count,
            "height": height,
            "width": width,
            "crs": crs,
            "transform": transform,
            "tiled": tiled,
            "blockxsize": blocksize,
            "blockysize": blocksize,
            "compress": compress,
            "nodata": nodata,
            "BIGTIFF": "IF_SAFER",
        }

        if compress in ["deflate", "lzw"] and np.issubdtype(data.dtype, np.floating):
            profile["predictor"] = 3

        with rasterio.open(output_path, "w", **profile) as dst:  # type: ignore[attr-defined]
            if data.ndim == 2:
                dst.write(data, 1)  # type: ignore[attr-defined]
            else:
                for i in range(count):
                    dst.write(data[i], i + 1)  # type: ignore[attr-defined]

            if metadata:
                dst.update_tags(**metadata)  # type: ignore[attr-defined]

            resampling = (
                Resampling.nearest if np.issubdtype(data.dtype, np.integer) else Resampling.bilinear
            )
            dst.build_overviews(overviews, resampling)  # type: ignore[attr-defined]

    return output_path


def parse_s3_url(s3_url: str) -> tuple[str, str]:
    """Parse S3 URL into bucket and key.

    Args:
        s3_url: S3 URL in format s3://bucket/path/to/file

    Returns:
        Tuple of (bucket, key)

    Raises:
        ValueError: If URL is not a valid S3 URL
    """
    if not s3_url.startswith("s3://"):
        raise ValueError(f"Invalid S3 URL: {s3_url}")

    path = s3_url[5:]  # Remove 's3://'
    parts = path.split("/", 1)

    if len(parts) != 2:
        raise ValueError(f"Invalid S3 URL format: {s3_url}")

    return parts[0], parts[1]


def upload_to_s3(local_path: str, bucket: str, key: str) -> None:
    """Upload file to S3 with progress reporting for large files.

    Args:
        local_path: Path to local file to upload
        bucket: S3 bucket name
        key: S3 key (path within bucket)

    Raises:
        Exception: If upload fails (with helpful error messages)
    """
    try:
        s3_client = boto3.client("s3")

        # Get file size for progress reporting
        file_size = os.path.getsize(local_path)
        file_size_mb = file_size / (1024 * 1024)

        logger.info(f"Uploading {file_size_mb:.1f} MB to s3://{bucket}/{key}")

        # Use multipart upload for large files (>100MB)
        if file_size > 100 * 1024 * 1024:  # 100MB
            logger.info("Using multipart upload for large file")
            # Configure multipart upload

            config = TransferConfig(
                multipart_threshold=1024 * 25,  # 25MB
                max_concurrency=10,
                multipart_chunksize=1024 * 25,
                use_threads=True,
            )
            s3_client.upload_file(local_path, bucket, key, Config=config)
        else:
            s3_client.upload_file(local_path, bucket, key)

        logger.info(f"Successfully uploaded to s3://{bucket}/{key}")

    except Exception as e:
        error_msg = f"Failed to upload {local_path} to {bucket}/{key}: {str(e)}"
        logger.error(error_msg)

        # Add helpful hints for common errors
        if "NoCredentialsError" in str(type(e)) or "AccessDenied" in str(e):
            logger.error(
                "Hint: Configure AWS credentials via one of these methods:\n"
                "  - Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY environment variables\n"
                "  - Run 'aws configure' to create ~/.aws/credentials\n"
                "  - Use IAM role (on EC2 instances)\n"
                "  - Set AWS_PROFILE environment variable"
            )
        elif "NoSuchBucket" in str(e):
            logger.error(
                f"Hint: The bucket '{bucket}' does not exist or you don't have access to it."
            )

        # Re-raise original exception to preserve type and traceback
        raise


def create_cog_s3(local_path: str, s3_url: str) -> str:
    """Upload an existing COG file to S3.

    Args:
        local_path: Path to the COG file to upload
        s3_url: S3 URL where the file should be uploaded

    Returns:
        The S3 URL where the file was uploaded
    """
    bucket, key = parse_s3_url(s3_url)
    upload_to_s3(local_path, bucket, key)
    return s3_url


def create_cog(
    data: ArrayLike | str,
    output_path: str,
    transform: Transform,
    crs: CRS,
    metadata: dict[str, Any] | None = None,
    compress: Literal["deflate", "lzw", "jpeg", "webp", "zstd"] = "deflate",
    tiled: bool = True,
    blocksize: int = 512,
    overviews: list[int] | None = None,
    nodata: float | None = None,
) -> str:
    """Create a Cloud-Optimized GeoTIFF, supporting both local and S3 paths.

    This is the main entry point that dispatches to either local or S3 creation
    based on the output path format.

    Args:
        data: Array data or path to existing raster
        output_path: Where to save the COG (local path or s3:// URL)
        transform: Affine transform for the data
        crs: Coordinate reference system
        metadata: Optional metadata to include
        compress: Compression method
        tiled: Whether to tile the output
        blocksize: Size of tiles
        overviews: Overview levels to build
        nodata: NoData value

    Returns:
        Path where the COG was saved
    """
    if output_path.startswith("s3://"):
        # Create locally first, then upload to S3
        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
            temp_path = tmp.name

        try:
            # Create COG locally
            create_cog_local(
                data=data,
                output_path=temp_path,
                transform=transform,
                crs=crs,
                metadata=metadata,
                compress=compress,
                tiled=tiled,
                blocksize=blocksize,
                overviews=overviews,
                nodata=nodata,
            )

            # Upload to S3
            return create_cog_s3(temp_path, output_path)
        finally:
            # Clean up temporary file
            if os.path.exists(temp_path):
                os.unlink(temp_path)
    else:
        # Direct local creation
        return create_cog_local(
            data=data,
            output_path=output_path,
            transform=transform,
            crs=crs,
            metadata=metadata,
            compress=compress,
            tiled=tiled,
            blocksize=blocksize,
            overviews=overviews,
            nodata=nodata,
        )
