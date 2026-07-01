import boto3
from datetime import datetime
from typing import Optional, Union
import os
import shutil
from urllib.parse import urlparse


def _read_session(region: str = "us-west-2"):
    """boto3 Session for reading source/vendor S3 buckets.

    If the ``READ_ROLE_ARN`` env var is set, assume that role first (optionally
    with ``READ_ROLE_EXTERNAL_ID``) and return a session with the temporary
    credentials -- this lets a DPS worker (or any caller) read a vendor bucket
    via an assumable role it has permission to assume. When ``READ_ROLE_ARN`` is
    unset, fall back to the default credential chain (the worker's ambient role
    or ``AWS_*`` env vars) -- i.e. behaviour is unchanged unless you opt in.
    """
    role_arn = os.environ.get("READ_ROLE_ARN")
    if role_arn:
        sts = boto3.client("sts")
        kwargs = {"RoleArn": role_arn, "RoleSessionName": "disasters-vendor-read"}
        ext = os.environ.get("READ_ROLE_EXTERNAL_ID")
        if ext:
            kwargs["ExternalId"] = ext
        creds = sts.assume_role(**kwargs)["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=region,
        )
    return boto3.Session(region_name=region)


def retrieve_s3_file_list(bucket : str, prefix : str) -> list[str]:
    session = _read_session()
    s3_client = session.client('s3')
    files = [x["Key"] for x in s3_client.list_objects_v2(Bucket = bucket, Prefix = f"{prefix}/")["Contents"] if x["Key"].split("/")[1] != ""]
    return files

def retrieve_s3_valid_dates(bucket: str, prefix: str, level: Optional[str] = None) -> list[datetime]:
    """
    Return a sorted list of available capture datetimes for a sensor's S3 layout.

    Vendor is inferred from the bucket name. Each vendor has its own
    subfolder convention, so the parsing differs per branch.

    Args:
        bucket: S3 bucket name. Must contain one of 'satellogic' / 'umbra' /
            'capella' (substring match).
        prefix: S3 prefix to scan (e.g. 'disasters').
        level: Satellogic processing level (e.g. 'L1D', 'L1B'). REQUIRED when
            the bucket is satellogic — the subfolder layout encodes the level
            in the name and we need it to filter. Ignored for umbra / capella
            (their subfolder layouts don't carry a processing-level
            distinction). Mirrors the --level arg of process_satellogic.

    Returns:
        Sorted list of datetime objects, one per available capture date.

    Raises:
        ValueError: bucket is satellogic but `level` is None.
        ValueError: bucket doesn't match any known vendor.
    """
    if "satellogic" in bucket and level is None:
        raise ValueError(
            "level is required for satellogic buckets — pass e.g. level='L1D' "
            "or level='L1B'. Mirrors the --level arg of process_satellogic. "
            "Other vendors (umbra, capella) ignore this parameter."
        )

    session = _read_session()
    s3_client = session.client('s3')
    files = [x["Key"] for x in s3_client.list_objects_v2(Bucket = bucket, Prefix = f"{prefix}/")["Contents"] if x["Key"].split("/")[1] != ""]

    if "satellogic" in bucket:
        filtered_files = [x for x in files if f"_{level}_" in x.split("/")[1]]
        subdirs = list(set([x.split("/")[1] for x in filtered_files]))
        dates = [datetime.strptime(f"{x.split('_')[0]}_{x.split('_')[1]}", "%Y%m%d_%H%M%S") for x in subdirs]
        dates.sort()
        return dates
    elif "umbra" in bucket:
        filtered_files = [x for x in files if len(x.split("/")) > 2]
        subdirs = list(set([x.split("/")[2] for x in filtered_files]))
        dates = [datetime.strptime(x.split('_')[0], "%Y-%m-%d-%H-%M-%S") for x in subdirs]
        dates.sort()
        return dates
    elif "capella" in bucket:
        filtered_files = [x for x in files if len(x.split("/")) > 2]
        subdirs = list(set([x.split("/")[1] for x in filtered_files]))
        dates = [datetime.strptime(x.split("_")[5], "%Y%m%d%H%M%S") for x in subdirs]
        dates.sort()
        return dates
    else:
        raise ValueError(f"The provided bucket '{bucket}' was not recognized as a valid vendor.")

def read_s3_file(s3filepath : str, file_format : str = "utf-8"):
    bucket = s3filepath.split("/")[2]
    key = "/".join(s3filepath.split("/")[3:])
    session = _read_session()
    s3_client = session.client('s3')
    file = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read().decode(file_format)
    return file

def local_tif_basename(s3path: str) -> str:
    """Local basename for an S3 path, with a ``.tif``-family extension forced
    to lowercase ``.tif``. Non-tif extensions are returned unchanged.

    CSDA vendor data ships uppercase ``.TIF``. The remote key must stay as-is
    for the fetch to resolve, but the local working copy is normalized here so
    every downstream ``.tif`` assumption (selectors, parsing, output naming)
    holds regardless of source case. Only the extension is lowercased — the
    rest of the name (e.g. ``_GEC``) is preserved.
    """
    name = s3path.split("/")[-1]
    stem, ext = os.path.splitext(name)
    return f"{stem}.tif" if ext.lower() == ".tif" else name


def download_s3_file(s3filepath : str, save_location : str = "/tmp/s3_temp") -> str:
    if save_location.endswith("/"):
        save_location = save_location[:-1]
    bucket = s3filepath.split("/")[2]
    key = "/".join(s3filepath.split("/")[3:])
    filename = local_tif_basename(s3filepath)
    session = _read_session()
    if not os.path.exists(save_location):
        os.mkdir(save_location)
    s3_client = session.client('s3')
    outpath = f"{save_location}/{filename}"
    s3_client.download_file(bucket, key, outpath)
    return outpath

def remove_s3_temp(save_location: str = "/tmp/s3_temp") -> None:
    shutil.rmtree(save_location)

def parse_s3_uri(uri):
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise ValueError(f"Invalid S3 URI: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")

def upload_file_to_s3(local_file, s3_uri):
    s3 = boto3.client("s3")
    out_bucket, out_key = parse_s3_uri(s3_uri)
    s3.upload_file(local_file, out_bucket, out_key)
    print(f"Uploaded: {s3_uri}")

def build_flat_s3_uri(spath, filename):
    out_bucket, out_prefix = parse_s3_uri(spath)
    out_prefix = out_prefix.rstrip("/")
    if out_prefix:
        return f"s3://{out_bucket}/{out_prefix}/{filename}"
    else:
        return f"s3://{out_bucket}/{filename}"
