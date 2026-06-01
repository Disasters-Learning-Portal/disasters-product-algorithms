"""
Tests for shared_utils.cog_processing.process_single_file metadata routing.

process_single_file has two backends, picked by whether `metadata` is set:
  * metadata=None: subprocess fast path (`rio cogeo create ...`). The rio
    CLI has no flag for arbitrary metadata, so this path must NOT embed
    any user tags.
  * metadata={...}: routes through shared_utils.cog_utils.convert_to_cog
    which uses rio_cogeo.cog_translate's `additional_cog_metadata=` to
    embed tags at creation time. This is the only place ACTIVATION_EVENT
    tags actually land in the output.

Both branches are exercised against a moto-mocked S3 (the function
downloads from S3, processes locally, uploads back).
"""

import os

import pytest

# Skip cleanly without the geospatial / mock-S3 stack.
rasterio = pytest.importorskip("rasterio")
gdal = pytest.importorskip("osgeo.gdal")
pytest.importorskip("rio_cogeo")
boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from moto import mock_aws


BUCKET = "cog-processing-bucket"
SRC_KEY = "raw/source.tif"
DST_KEY = "cogs/destination.tif"

USER_METADATA = {
    "ACTIVATION_EVENT": "202501_Flood_TX",
    "SOURCE": "USGS",
    "PROCESSOR": "NASA Disasters COG Processor v0.0.0-test",
}


def _seed_bucket(s3_client, local_src_path):
    """Single-bucket setup — process_single_file uses one bucket for src+dst."""
    s3_client.create_bucket(Bucket=BUCKET)
    s3_client.upload_file(local_src_path, BUCKET, SRC_KEY)


def _download_dest(s3_client, tmp_path):
    out = tmp_path / "dest.tif"
    s3_client.download_file(BUCKET, DST_KEY, str(out))
    return str(out)


class TestCogProcessingMetadataForwarding:
    """Pin that process_single_file:
      * metadata=None → subprocess fast path, NO user tags leak through
      * metadata={...} → routes through cog_utils, tags ARE embedded
    """

    @mock_aws
    def test_metadata_none_uses_fast_path_and_does_not_embed_user_tags(
        self, float32_geotiff, tmp_path
    ):
        """Regression: metadata=None must keep working AND must not somehow
        leak ACTIVATION_EVENT etc. into the output COG."""
        from shared_utils.cog_processing import process_single_file

        s3 = boto3.client("s3", region_name="us-east-1")
        _seed_bucket(s3, float32_geotiff)

        ok = process_single_file(
            s3_client=s3,
            bucket=BUCKET,
            source_key=SRC_KEY,
            dest_key=DST_KEY,
            verify=False,                  # don't depend on cog_validate
            check_source_is_cog=False,     # skip the is-already-cog shortcut
            verbose=False,
            metadata=None,
        )
        assert ok is True, "metadata=None fast path should return True on success"

        local_out = _download_dest(s3, tmp_path)
        ds = gdal.Open(local_out, gdal.GA_ReadOnly)
        actual = ds.GetMetadata()
        del ds

        leaked = [k for k in USER_METADATA if k in actual]
        assert leaked == [], (
            f"User metadata keys leaked into the metadata=None fast path: {leaked}"
        )

    @mock_aws
    def test_metadata_dict_routes_through_cog_utils_and_embeds_tags(
        self, float32_geotiff, tmp_path
    ):
        """When metadata={...} is passed, process_single_file must route
        through cog_utils.convert_to_cog so the tags actually get embedded
        by rio_cogeo.cog_translate."""
        from shared_utils.cog_processing import process_single_file

        s3 = boto3.client("s3", region_name="us-east-1")
        _seed_bucket(s3, float32_geotiff)

        ok = process_single_file(
            s3_client=s3,
            bucket=BUCKET,
            source_key=SRC_KEY,
            dest_key=DST_KEY,
            verify=False,
            check_source_is_cog=False,
            verbose=False,
            metadata=USER_METADATA,
        )
        assert ok is True, (
            "metadata={...} branch returned False — process_single_file failed "
            "to route through cog_utils.convert_to_cog"
        )

        local_out = _download_dest(s3, tmp_path)
        ds = gdal.Open(local_out, gdal.GA_ReadOnly)
        actual = ds.GetMetadata()
        del ds

        for key, expected_value in USER_METADATA.items():
            assert actual.get(key) == expected_value, (
                f"Tag {key!r} did not roundtrip through process_single_file: "
                f"expected={expected_value!r}, got={actual.get(key)!r}. "
                f"All tags on output: {sorted(actual.keys())}"
            )
