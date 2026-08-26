"""
S3 operations module - handles all AWS S3 interactions.
Single responsibility: S3 file operations and client management.
"""

import os
import boto3
from botocore.exceptions import ClientError, NoCredentialsError
import fsspec

# Try to import credentials for upload permissions
try:
    from aws_credentials import EXTERNAL_ID, UPLOAD_ROLE_ARN
    HAS_UPLOAD_CREDENTIALS = True
except ImportError:
    HAS_UPLOAD_CREDENTIALS = False
    EXTERNAL_ID = None
    UPLOAD_ROLE_ARN = None


def initialize_s3_client(bucket_name='nasa-disasters', verbose=True):
    """
    Initialize AWS S3 client with automatic credential detection.

    Tries two authentication methods in order:
    1. External ID + STS assume role (if aws_credentials.py exists) - for upload permissions
    2. Default credentials (environment/config) - for read-only access

    Args:
        bucket_name: Name of the S3 bucket
        verbose: Print status messages

    Returns:
        tuple: (s3_client, fs_read) or (None, None) if initialization fails
    """
    s3_client = None

    # Say so when the assume-role path is not even available. `aws_credentials`
    # is gitignored and absent from a fresh checkout / hub pod, so this branch
    # is the NORMAL case -- and it used to be entirely silent, which meant an
    # operator saw only the "default credentials" line below and had no way to
    # tell whether the role had been tried and failed or never attempted.
    if verbose and not (HAS_UPLOAD_CREDENTIALS and EXTERNAL_ID and UPLOAD_ROLE_ARN):
        print(
            "ℹ️ aws_credentials.py not importable -- skipping the STS assume-role "
            "and using ambient credentials. On the Disasters hub those are the "
            "disasters-prod role; elsewhere they may be read-only."
        )

    # Method 1: Try to use external ID for upload permissions
    if HAS_UPLOAD_CREDENTIALS and EXTERNAL_ID and UPLOAD_ROLE_ARN:
        try:
            if verbose:
                print("🔑 Attempting to authenticate with external ID for upload permissions...")

            sts = boto3.client('sts')

            # Assume role with the external ID
            response = sts.assume_role(
                RoleArn=UPLOAD_ROLE_ARN,
                RoleSessionName='disaster-upload-session',
                ExternalId=EXTERNAL_ID
            )

            # Create S3 client with temporary credentials
            creds = response['Credentials']
            s3_client = boto3.client(
                's3',
                aws_access_key_id=creds['AccessKeyId'],
                aws_secret_access_key=creds['SecretAccessKey'],
                aws_session_token=creds['SessionToken']
            )

            # Test access
            try:
                s3_client.head_bucket(Bucket=bucket_name)
                if verbose:
                    print(f"✅ S3 client initialized with UPLOAD permissions via external ID")
            except ClientError as e:
                error_code = int(e.response['Error']['Code'])
                if error_code == 403:
                    if verbose:
                        print(f"⚠️ S3 client initialized via external ID (limited bucket list access)")
                else:
                    raise

        except Exception as e:
            if verbose:
                print(f"⚠️ Failed to authenticate with external ID: {e}")
                print("   Falling back to default credentials...")
            s3_client = None

    # Method 2: Fall back to default credentials (read-only)
    if s3_client is None:
        try:
            # Try to create S3 client with default credentials
            s3_client = boto3.client('s3')

            # Test access
            try:
                s3_client.head_bucket(Bucket=bucket_name)
                if verbose:
                    print(f"✅ S3 client initialized with default credentials (read-only access)")
            except ClientError as e:
                error_code = int(e.response['Error']['Code'])
                if error_code == 403:
                    if verbose:
                        print(f"⚠️ S3 client initialized (limited bucket list access)")
                else:
                    raise

        except NoCredentialsError:
            if verbose:
                print("❌ No AWS credentials found")
                print("\nTo configure credentials:")
                print("  1. For upload permissions: Create aws_credentials.py with EXTERNAL_ID")
                print("  2. For read-only: AWS CLI: aws configure")
                print("  3. Environment variables: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY")
                print("  4. IAM role (if on EC2)")
            return None, None
        except Exception as e:
            if verbose:
                print(f"❌ Failed to initialize S3 client: {e}")
            return None, None

    # Create fsspec filesystem (works with both auth methods)
    try:
        fs_read = fsspec.filesystem('s3', anon=False)

        # NOT "Confirmed access to <bucket>" -- that is what this line used to
        # say, and it was false: constructing an fsspec filesystem never
        # touches the bucket. It read as a green light while uploads went on to
        # AccessDenied. Use can_write_to_bucket() for a real write check.
        if verbose:
            print(f"✅ S3 filesystem (fsspec) initialized")

        return s3_client, fs_read

    except Exception as e:
        if verbose:
            print(f"❌ Failed to initialize fsspec filesystem: {e}")
        return None, None


def can_write_to_bucket(s3_client, bucket, prefix='', verbose=True):
    """
    Actually verify write access by round-tripping a tiny probe object.

    ``head_bucket`` and fsspec construction both succeed for a read-only
    identity, so neither is evidence that an upload will work. Permissions on
    these buckets are commonly granted per PREFIX, so the probe is written
    under ``prefix`` -- writable at the bucket root does not imply writable at
    ``ProgramData/<Product>/``.

    Call this BEFORE a long conversion. The failure it catches otherwise
    surfaces only at the upload, i.e. after every file has been processed.

    Args:
        s3_client: Boto3 S3 client
        bucket: S3 bucket name
        prefix: Key prefix to probe under (e.g. 'ProgramData/Skysat')
        verbose: Print the outcome

    Returns:
        tuple: (ok, detail) -- ``detail`` is None on success, else the error string.
    """
    key = f"{prefix.strip('/')}/.write-probe" if prefix.strip('/') else '.write-probe'
    try:
        s3_client.put_object(Bucket=bucket, Key=key, Body=b'')
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        if verbose:
            print(f"❌ NOT writable: s3://{bucket}/{key}\n   {detail}")
        return False, detail

    try:
        s3_client.delete_object(Bucket=bucket, Key=key)
    except Exception:
        # Wrote but could not clean up -- write access is still proven.
        if verbose:
            print(f"⚠️ Wrote s3://{bucket}/{key} but could not delete the probe")

    if verbose:
        print(f"✅ Write access confirmed: s3://{bucket}/{prefix.strip('/')}/")
    return True, None


def check_s3_file_exists(s3_client, bucket, key):
    """
    Check if a file already exists in S3.

    Args:
        s3_client: Boto3 S3 client
        bucket: S3 bucket name
        key: S3 object key

    Returns:
        bool: True if file exists, False otherwise
    """
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == '404':
            return False
        raise


def check_s3_cog_status(s3_client, bucket, key, verbose=False):
    """
    Check if a file exists in S3 and whether it's already a valid COG.

    Args:
        s3_client: Boto3 S3 client
        bucket: S3 bucket name
        key: S3 object key
        verbose: Print status messages

    Returns:
        dict: Status information with keys:
            - exists (bool): True if file exists in S3
            - is_cog (bool): True if file is a valid COG (None if doesn't exist)
            - file_size_mb (float): File size in MB (0 if doesn't exist)
            - validation_details (dict): COG validation details
    """
    from shared_utils.cog_validation import is_s3_file_cog

    # First check if file exists
    exists = check_s3_file_exists(s3_client, bucket, key)

    if not exists:
        return {
            'exists': False,
            'is_cog': None,
            'file_size_mb': 0,
            'validation_details': {}
        }

    # File exists, check if it's a valid COG
    is_cog, validation_details = is_s3_file_cog(s3_client, bucket, key, verbose=verbose)

    return {
        'exists': True,
        'is_cog': is_cog,
        'file_size_mb': validation_details.get('file_size_mb', 0),
        'validation_details': validation_details
    }


def download_from_s3(s3_client, bucket, key, local_path, verbose=True):
    """
    Download a file from S3 to local storage.

    Args:
        s3_client: Boto3 S3 client
        bucket: S3 bucket name
        key: S3 object key
        local_path: Local file path to save to
        verbose: Print progress messages

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        if verbose:
            print(f"   [DOWNLOAD] Downloading from S3: s3://{bucket}/{key}")

        s3_client.download_file(bucket, key, local_path)

        if verbose:
            file_size_mb = os.path.getsize(local_path) / (1024 * 1024)
            print(f"   [DOWNLOAD] ✅ Downloaded {file_size_mb:.1f} MB to {local_path}")

        return True

    except Exception as e:
        if verbose:
            print(f"   [DOWNLOAD] ❌ Failed to download: {e}")
        return False


def upload_to_s3(s3_client, local_path, bucket, key, verbose=True):
    """
    Upload a file to S3.

    Args:
        s3_client: Boto3 S3 client
        local_path: Local file path to upload
        bucket: S3 bucket name
        key: S3 object key
        verbose: Print progress messages

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        if verbose:
            file_size_mb = os.path.getsize(local_path) / (1024 * 1024)
            print(f"   [UPLOAD] Uploading {file_size_mb:.1f} MB to s3://{bucket}/{key}")

        # Use multipart upload for large files
        file_size = os.path.getsize(local_path)
        if file_size > 100 * 1024 * 1024:  # 100MB
            from boto3.s3.transfer import TransferConfig
            config = TransferConfig(
                multipart_threshold=1024 * 25,  # 25MB
                max_concurrency=10,
                multipart_chunksize=1024 * 25,
                use_threads=True
            )
            s3_client.upload_file(
                Filename=local_path,
                Bucket=bucket,
                Key=key,
                Config=config
            )
        else:
            s3_client.upload_file(local_path, bucket, key)

        if verbose:
            print(f"   [UPLOAD] ✅ Uploaded to s3://{bucket}/{key}")

        return True

    except Exception as e:
        if verbose:
            print(f"   [UPLOAD] ❌ Failed to upload: {e}")
        return False


def setup_vsi_credentials(s3_client):
    """
    Setup GDAL VSI credentials for S3 streaming.

    Args:
        s3_client: Boto3 S3 client

    Returns:
        bool: True if successful
    """
    try:
        # Get credentials from the client
        credentials = None

        # Try to get from client's session
        if hasattr(s3_client, '_request_signer') and hasattr(s3_client._request_signer, '_credentials'):
            credentials = s3_client._request_signer._credentials

        # Get fresh credentials from boto3 session
        if not credentials:
            session = boto3.Session()
            credentials = session.get_credentials()

        # Set environment variables for GDAL
        if credentials:
            if hasattr(credentials, 'access_key'):
                os.environ['AWS_ACCESS_KEY_ID'] = credentials.access_key
            if hasattr(credentials, 'secret_key'):
                os.environ['AWS_SECRET_ACCESS_KEY'] = credentials.secret_key
            if hasattr(credentials, 'token') and credentials.token:
                os.environ['AWS_SESSION_TOKEN'] = credentials.token

        # Configure GDAL for S3
        os.environ['AWS_REGION'] = 'us-west-2'
        os.environ['AWS_REQUEST_PAYER'] = 'bucket-owner'
        os.environ['CPL_VSIL_CURL_ALLOWED_EXTENSIONS'] = '.tif,.tiff,.TIF,.TIFF'
        os.environ['VSI_CACHE'] = 'TRUE'
        os.environ['VSI_CACHE_SIZE'] = '1000000000'

        return True

    except Exception as e:
        print(f"   [WARNING] Could not setup VSI credentials: {e}")
        return False


def list_s3_files(s3_client, bucket, prefix, suffix='.tif'):
    """
    List all files in an S3 bucket with given prefix and suffix.

    Args:
        s3_client: Boto3 S3 client
        bucket: S3 bucket name
        prefix: S3 prefix to search
        suffix: File suffix to filter (matched case-insensitively, so
            suffix='.tif' returns both '.tif' and '.TIF' keys)

    Returns:
        list: List of S3 keys
    """
    try:
        keys = []
        paginator = s3_client.get_paginator('list_objects_v2')

        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            if 'Contents' in page:
                for obj in page['Contents']:
                    if obj['Key'].lower().endswith(suffix.lower()):
                        keys.append(obj['Key'])

        return keys

    except Exception as e:
        print(f"   [ERROR] Failed to list S3 files: {e}")
        return []


def get_file_size_from_s3(s3_client, bucket, key):
    """
    Get file size in GB from S3.

    Args:
        s3_client: Boto3 S3 client
        bucket: S3 bucket name
        key: S3 object key

    Returns:
        float: File size in GB
    """
    try:
        response = s3_client.head_object(Bucket=bucket, Key=key)
        size_gb = response['ContentLength'] / (1024**3)
        return size_gb
    except:
        return 0.0