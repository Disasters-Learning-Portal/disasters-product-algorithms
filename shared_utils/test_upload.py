"""
Test S3 upload permissions by writing and cleaning up a small test object.
"""

import tempfile
import os

from shared_utils.s3_operations import initialize_s3_client, upload_to_s3


def test_s3_upload(bucket='nasa-disasters', prefix='_test_uploads'):
    """
    Verify that the current credentials can upload to the S3 bucket.

    Creates a tiny test file, uploads it, confirms it exists, then deletes it.

    Args:
        bucket: S3 bucket to test against
        prefix: S3 prefix for the test object (will be cleaned up)

    Returns:
        bool: True if upload succeeded, False otherwise
    """
    s3_client, _ = initialize_s3_client(bucket_name=bucket, verbose=False)
    test_key = f"{prefix}/upload_permission_test.txt"

    tmp_path = None
    try:
        # Create a small temp file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("upload permission test")
            tmp_path = f.name

        # Attempt upload
        success = upload_to_s3(s3_client, tmp_path, bucket, test_key, verbose=False)
        if not success:
            print(f"[TEST] Upload failed for s3://{bucket}/{test_key}")
            return False

        # Verify it exists
        s3_client.head_object(Bucket=bucket, Key=test_key)
        print(f"[TEST] Upload test passed — credentials can write to s3://{bucket}/")

        # Clean up the test object
        s3_client.delete_object(Bucket=bucket, Key=test_key)
        return True

    except Exception as e:
        print(f"[TEST] Upload test failed: {e}")
        return False

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
