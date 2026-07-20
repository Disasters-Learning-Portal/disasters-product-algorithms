"""
Tests for ``shared_utils.s3utils.explain_s3_read_failure`` -- the pure
message-mapper the ``--list_dates`` discovery path uses to turn a vendor-bucket
read failure into ONE operator-facing line instead of a raw boto3 traceback.

Pins:
1. Missing credentials -> a message mentioning "credentials".
2. Access denied (both the string code and the bare "403") -> "access denied".
3. No-such-bucket (both "NoSuchBucket" and "404") -> "does not exist".
4. An unrecognized ClientError code, and a non-boto exception, -> ``None`` (so
   the caller falls back to the raw string).
"""

import pytest

pytest.importorskip("botocore")

from botocore.exceptions import ClientError, NoCredentialsError

# conftest stubs osgeo so importing shared_utils.__init__ (geotools) is safe.
from shared_utils.s3utils import explain_s3_read_failure

BUCKET, PREFIX = "csda-data-vendor-umbra", "disasters"


def _client_error(code, operation="ListObjectsV2"):
    return ClientError(
        {"Error": {"Code": code, "Message": "boom"}}, operation
    )


def test_no_credentials_mentions_credentials():
    msg = explain_s3_read_failure(NoCredentialsError(), BUCKET, PREFIX)
    assert msg is not None
    assert "credentials" in msg.lower()
    # location is echoed so the operator sees which bucket failed
    assert BUCKET in msg


@pytest.mark.parametrize("code", ["AccessDenied", "AccessDeniedException", "403"])
def test_access_denied_variants(code):
    msg = explain_s3_read_failure(_client_error(code), BUCKET, PREFIX)
    assert msg is not None
    assert "access denied" in msg.lower()
    assert BUCKET in msg


def test_access_denied_on_assume_role_names_the_role(monkeypatch):
    # An AccessDenied whose operation is AssumeRole means READ_ROLE_ARN can't be
    # assumed (trust policy), NOT that s3:ListBucket is missing. The message must
    # say so and echo the role, so an operator fixes the right thing.
    monkeypatch.setenv("READ_ROLE_ARN", "arn:aws:iam::515966502221:role/disasters-prod")
    err = _client_error("AccessDenied", operation="AssumeRole")
    msg = explain_s3_read_failure(err, BUCKET, PREFIX)
    assert msg is not None
    assert "assumerole" in msg.lower()
    assert "disasters-prod" in msg          # the role is named
    assert "s3:listbucket" not in msg.lower()  # not the misleading old text


@pytest.mark.parametrize("code", ["NoSuchBucket", "404"])
def test_no_such_bucket_variants(code):
    msg = explain_s3_read_failure(_client_error(code), BUCKET, PREFIX)
    assert msg is not None
    assert "does not exist" in msg.lower()


def test_unrecognized_client_error_returns_none():
    # e.g. throttling -- not an access/credential problem, so no friendly mapping
    assert explain_s3_read_failure(_client_error("SlowDown"), BUCKET, PREFIX) is None


def test_non_boto_exception_returns_none():
    assert explain_s3_read_failure(ValueError("nope"), BUCKET, PREFIX) is None
