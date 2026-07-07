"""
Tests for shared_utils.s3utils.

Specifically pins the contract of `retrieve_s3_valid_dates` — the
"primitive date-finder function" added via PR #21 (commit fe02808). Without
these tests the satellogic branch's `level` parameter requirement could
silently regress (the previous version had an undefined `level` reference
that crashed at runtime). The fix also gave satellogic a required `level`
kwarg, so the contract is worth pinning explicitly.
"""

from datetime import datetime

import pytest

# Skip cleanly on dev machines without the geospatial / S3-mock stack.
boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")
from moto import mock_aws


SATELLOGIC_BUCKET = "csda-data-vendor-satellogic"
UMBRA_BUCKET = "csda-data-vendor-umbra"
CAPELLA_BUCKET = "csdap-capellaspace-delivery"
UNKNOWN_BUCKET = "some-other-vendor"
PREFIX = "disasters"


def _put(client, bucket, key):
    """Tiny helper — put an empty object at the given key."""
    client.put_object(Bucket=bucket, Key=key, Body=b"")


@mock_aws
class TestRetrieveS3ValidDates:
    """Covers all 4 branches of retrieve_s3_valid_dates."""

    def _setup_satellogic(self, level: str):
        """Build a satellogic subfolder layout. Two captures with the given
        level + one with a different level (which should be filtered out)."""
        client = boto3.client("s3", region_name="us-west-2")
        client.create_bucket(
            Bucket=SATELLOGIC_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )
        # subfolder convention: <YYYYMMDD>_<HHMMSS>_<sceneid>_<LEVEL>_<...>/file.tif
        for subfolder in [
            f"20240101_120000_sceneA_{level}_tail/file.tif",
            f"20240202_130000_sceneB_{level}_tail/file.tif",
            "20240303_140000_sceneC_OTHERLEVEL_tail/file.tif",  # should be excluded
        ]:
            _put(client, SATELLOGIC_BUCKET, f"{PREFIX}/{subfolder}")
        return client

    def _setup_umbra(self):
        client = boto3.client("s3", region_name="us-west-2")
        client.create_bucket(
            Bucket=UMBRA_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )
        # subfolder convention: disasters/<eventdir>/<YYYY-MM-DD-HH-MM-SS>_tail/file.tif
        for path in [
            "ev1/2024-01-01-12-00-00_tail/file.tif",
            "ev1/2024-02-02-13-00-00_tail/file.tif",
        ]:
            _put(client, UMBRA_BUCKET, f"{PREFIX}/{path}")
        return client

    def _setup_capella(self):
        client = boto3.client("s3", region_name="us-west-2")
        client.create_bucket(
            Bucket=CAPELLA_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )
        # subfolder convention: disasters/<a_b_c_d_e_YYYYMMDDHHMMSS_...>/<subdir>/file.tif
        # The parser does x.split("_")[5] on the subdir name.
        for path in [
            "a_b_c_d_e_20240101120000_tail/sub/file.tif",
            "a_b_c_d_e_20240202130000_tail/sub/file.tif",
        ]:
            _put(client, CAPELLA_BUCKET, f"{PREFIX}/{path}")
        return client

    # -------- satellogic --------

    def test_satellogic_returns_sorted_dates_filtered_by_level(self):
        from shared_utils.s3utils import retrieve_s3_valid_dates

        self._setup_satellogic(level="L1D")
        result = retrieve_s3_valid_dates(SATELLOGIC_BUCKET, PREFIX, level="L1D")

        assert result == [
            datetime(2024, 1, 1, 12, 0, 0),
            datetime(2024, 2, 2, 13, 0, 0),
        ], f"got: {result}"

    def test_satellogic_without_level_raises_ValueError(self):
        from shared_utils.s3utils import retrieve_s3_valid_dates

        self._setup_satellogic(level="L1D")
        with pytest.raises(ValueError, match="level is required for satellogic"):
            retrieve_s3_valid_dates(SATELLOGIC_BUCKET, PREFIX)

    def test_satellogic_with_wrong_level_returns_empty(self):
        """Filtering by a level that no subfolder uses yields an empty list,
        not an error — the function trusts the caller's level choice."""
        from shared_utils.s3utils import retrieve_s3_valid_dates

        self._setup_satellogic(level="L1D")
        result = retrieve_s3_valid_dates(SATELLOGIC_BUCKET, PREFIX, level="L1B")
        assert result == []

    # -------- umbra --------

    def test_umbra_returns_sorted_dates_no_level_required(self):
        from shared_utils.s3utils import retrieve_s3_valid_dates

        self._setup_umbra()
        result = retrieve_s3_valid_dates(UMBRA_BUCKET, PREFIX)
        assert result == [
            datetime(2024, 1, 1, 12, 0, 0),
            datetime(2024, 2, 2, 13, 0, 0),
        ]

    def test_umbra_ignores_level_kwarg(self):
        """level is satellogic-only; umbra silently ignores it."""
        from shared_utils.s3utils import retrieve_s3_valid_dates

        self._setup_umbra()
        result = retrieve_s3_valid_dates(UMBRA_BUCKET, PREFIX, level="L1D")
        # No raise, no filter — same result as without level.
        assert result == [
            datetime(2024, 1, 1, 12, 0, 0),
            datetime(2024, 2, 2, 13, 0, 0),
        ]

    # -------- capella --------

    def test_capella_returns_sorted_dates_no_level_required(self):
        from shared_utils.s3utils import retrieve_s3_valid_dates

        self._setup_capella()
        result = retrieve_s3_valid_dates(CAPELLA_BUCKET, PREFIX)
        assert result == [
            datetime(2024, 1, 1, 12, 0, 0),
            datetime(2024, 2, 2, 13, 0, 0),
        ]

    # -------- unknown vendor --------

    def test_unknown_bucket_raises_ValueError(self):
        from shared_utils.s3utils import retrieve_s3_valid_dates

        client = boto3.client("s3", region_name="us-west-2")
        client.create_bucket(
            Bucket=UNKNOWN_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )
        _put(client, UNKNOWN_BUCKET, f"{PREFIX}/anything/file.tif")
        with pytest.raises(ValueError, match="was not recognized as a valid vendor"):
            retrieve_s3_valid_dates(UNKNOWN_BUCKET, PREFIX)


# ---------------------------------------------------------------------------
# Case-insensitive .tif / .TIF handling.
#
# CSDA vendor data ships uppercase `.TIF`. Detection must match both cases, and
# the local working copy is normalized to lowercase `.tif` — while the remote
# S3 key stays as-is so the fetch still resolves.
# ---------------------------------------------------------------------------

GENERIC_BUCKET = "nasa-disasters-test"


class TestLocalTifBasename:
    """Pure-unit: local file naming forces a .tif-family extension to lowercase
    '.tif' while preserving the rest of the name."""

    @pytest.mark.parametrize(
        "s3path, expected",
        [
            ("s3://b/p/CAPELLA_C18_GEO_GEC.TIF", "CAPELLA_C18_GEO_GEC.tif"),  # upper -> lower
            ("s3://b/p/scene_GEC.tif", "scene_GEC.tif"),  # already lowercase, unchanged
            ("s3://b/p/scene_GEC.Tif", "scene_GEC.tif"),  # mixed case
            ("bare_file.TIF", "bare_file.tif"),           # no slashes
            ("s3://b/p/metadata.json", "metadata.json"),  # non-tif extension untouched
            ("s3://b/p/angles.geojson", "angles.geojson"),  # non-tif extension untouched
        ],
    )
    def test_extension_normalized(self, s3path, expected):
        from shared_utils.s3utils import local_tif_basename

        assert local_tif_basename(s3path) == expected

    def test_preserves_non_extension_case(self):
        """Only the extension is lowercased — tokens like _GEC keep their case."""
        from shared_utils.s3utils import local_tif_basename

        assert local_tif_basename("s3://b/p/X_GEC.TIF") == "X_GEC.tif"


@mock_aws
class TestListS3FilesCaseInsensitive:
    """list_s3_files must return both '.tif' and '.TIF' keys (CSDA ships .TIF)."""

    def _bucket(self):
        client = boto3.client("s3", region_name="us-west-2")
        client.create_bucket(
            Bucket=GENERIC_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )
        return client

    def test_matches_both_cases(self):
        from shared_utils.s3_operations import list_s3_files

        client = self._bucket()
        _put(client, GENERIC_BUCKET, f"{PREFIX}/lower.tif")
        _put(client, GENERIC_BUCKET, f"{PREFIX}/UPPER.TIF")
        _put(client, GENERIC_BUCKET, f"{PREFIX}/notes.txt")  # must be excluded

        keys = list_s3_files(client, GENERIC_BUCKET, PREFIX)  # default suffix='.tif'

        assert sorted(keys) == [f"{PREFIX}/UPPER.TIF", f"{PREFIX}/lower.tif"]
        # The real (uppercase) key is preserved — NOT lowercased — so a later
        # download resolves the actual object.
        assert f"{PREFIX}/UPPER.TIF" in keys


@mock_aws
class TestDownloadS3FileNormalizesExtension:
    """download_s3_file fetches the real .TIF key but writes a local .tif."""

    def test_uppercase_source_downloads_to_lowercase_tif(self, tmp_path):
        import os

        from shared_utils.s3utils import download_s3_file

        client = boto3.client("s3", region_name="us-west-2")
        client.create_bucket(
            Bucket=GENERIC_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )
        key = f"{PREFIX}/scene_GEC.TIF"
        _put(client, GENERIC_BUCKET, key)

        outpath = download_s3_file(
            f"s3://{GENERIC_BUCKET}/{key}", save_location=str(tmp_path)
        )

        # Local file normalized to .tif ...
        assert outpath.endswith("scene_GEC.tif")
        # ... and it exists, proving the real .TIF key (not a lowercased one)
        # was the object actually fetched.
        assert os.path.exists(outpath)
