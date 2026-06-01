"""
Tests for shared_utils.main_processor.convert_to_cog metadata forwarding.

main_processor is the S3 orchestrator: download → cog_utils → upload.
The new `metadata=` kwarg must reach cog_utils.convert_to_cog so that
ACTIVATION_EVENT / SOURCE / PROCESSOR tags get embedded in the output COG
before upload. These tests pin that contract end-to-end against a mocked
S3 (moto), reading the final uploaded object back and verifying tags
roundtripped.

We use `stream_from_s3=False` to force the download → /tmp path. The VSI
probe path doesn't play nicely with moto's mock environment (GDAL's curl
layer doesn't go through botocore), so the download path is the
deterministic one to test against.
"""

import os

import pytest

# Hard-skip the whole module if the geospatial / mock-S3 stack is missing.
rasterio = pytest.importorskip("rasterio")
gdal = pytest.importorskip("osgeo.gdal")
pytest.importorskip("rio_cogeo")
boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from moto import mock_aws


SRC_BUCKET = "src-bucket"
DST_BUCKET = "dst-bucket"
DST_PREFIX = "outputs"
SRC_KEY = "raw/input.tif"
COG_FILENAME = "output_cog.tif"

USER_METADATA = {
    "ACTIVATION_EVENT": "202501_Flood_TX",
    "SOURCE": "USGS",
    "PROCESSOR": "NASA Disasters COG Processor v0.0.0-test",
}


def _seed_source_s3(s3_client, local_src_path):
    """Create both buckets in the mocked S3 and upload the test input."""
    s3_client.create_bucket(Bucket=SRC_BUCKET)
    s3_client.create_bucket(Bucket=DST_BUCKET)
    s3_client.upload_file(local_src_path, SRC_BUCKET, SRC_KEY)


def _download_output_to(s3_client, tmp_path):
    """Download the produced COG from the mock bucket back to a local file."""
    local_out = tmp_path / "downloaded.tif"
    s3_client.download_file(
        DST_BUCKET,
        f"{DST_PREFIX}/{COG_FILENAME}",
        str(local_out),
    )
    return str(local_out)


class TestMainProcessorMetadataForwarding:
    """Pin that main_processor.convert_to_cog forwards `metadata=` to the
    local COG primitive — and that the no-metadata default still works."""

    @mock_aws
    def test_metadata_kwarg_forwards_to_cog(self, float32_geotiff, tmp_path):
        """With metadata=USER_METADATA, the uploaded COG must carry those tags."""
        from shared_utils.main_processor import convert_to_cog

        s3 = boto3.client("s3", region_name="us-east-1")
        _seed_source_s3(s3, float32_geotiff)

        convert_to_cog(
            name=SRC_KEY,
            bucket=SRC_BUCKET,
            cog_filename=COG_FILENAME,
            cog_data_bucket=DST_BUCKET,
            cog_data_prefix=DST_PREFIX,
            s3_client=s3,
            target_crs=None,  # skip warp for speed and determinism
            stream_from_s3=False,  # force download path, deterministic under moto
            skip_validation=True,  # don't depend on cog_validate in the orchestrator
            metadata=USER_METADATA,
        )

        local_out = _download_output_to(s3, tmp_path)
        ds = gdal.Open(local_out, gdal.GA_ReadOnly)
        actual = ds.GetMetadata()
        del ds

        for key, expected_value in USER_METADATA.items():
            assert actual.get(key) == expected_value, (
                f"Tag {key!r} did not reach the uploaded COG: "
                f"expected={expected_value!r}, got={actual.get(key)!r}. "
                f"All tags on output: {sorted(actual.keys())}"
            )

    @mock_aws
    def test_metadata_default_none_still_uploads_valid_cog(self, float32_geotiff, tmp_path):
        """Regression: omitting metadata must keep the orchestrator working
        and must NOT leak the USER_METADATA keys (e.g. via a stale module-
        level default) into the output."""
        from shared_utils.main_processor import convert_to_cog

        s3 = boto3.client("s3", region_name="us-east-1")
        _seed_source_s3(s3, float32_geotiff)

        convert_to_cog(
            name=SRC_KEY,
            bucket=SRC_BUCKET,
            cog_filename=COG_FILENAME,
            cog_data_bucket=DST_BUCKET,
            cog_data_prefix=DST_PREFIX,
            s3_client=s3,
            target_crs=None,
            stream_from_s3=False,
            skip_validation=True,
            # metadata kwarg omitted → defaults to None
        )

        # Object must exist on the mocked S3.
        head = s3.head_object(Bucket=DST_BUCKET, Key=f"{DST_PREFIX}/{COG_FILENAME}")
        assert head["ContentLength"] > 0

        local_out = _download_output_to(s3, tmp_path)
        ds = gdal.Open(local_out, gdal.GA_ReadOnly)
        actual = ds.GetMetadata()
        del ds

        leaked = [k for k in USER_METADATA if k in actual]
        assert leaked == [], (
            f"User metadata keys leaked into a metadata=None upload: {leaked}"
        )
