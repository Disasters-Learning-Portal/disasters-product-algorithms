import boto3
from datetime import datetime
from typing import Union
import os
import shutil
from urllib.parse import urlparse
from rasterio.shutil import copy as rio_copy

def retrieve_s3_file_list(bucket : str, prefix : str) -> list[str]:
    session = boto3.Session(region_name="us-west-2")
    s3_client = session.client('s3')
    files = [x["Key"] for x in s3_client.list_objects_v2(Bucket = bucket, Prefix = f"{prefix}/")["Contents"] if x["Key"].split("/")[1] != ""]
    return files

def read_s3_file(s3filepath : str, file_format : str = "utf-8"):
    bucket = s3filepath.split("/")[2]
    key = "/".join(s3filepath.split("/")[3:])
    session = boto3.Session(region_name="us-west-2")
    s3_client = session.client('s3')
    file = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read().decode(file_format)
    return file

def download_s3_file(s3filepath : str, save_location : str = "./s3_temp") -> str:
    if save_location.endswith("/"):
        save_location = save_location[:-1]
    bucket = s3filepath.split("/")[2]
    key = "/".join(s3filepath.split("/")[3:])
    filename = s3filepath.split("/")[-1]
    session = boto3.Session(region_name="us-west-2")
    if not os.path.exists(save_location):
        os.mkdir(save_location)
    s3_client = session.client('s3')
    outpath = f"{save_location}/{filename}"
    s3_client.download_file(bucket, key, outpath)
    return outpath

def remove_s3_temp() -> None:
    shutil.rmtree("./s3_temp/")

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

def convert_to_cog(in_tif, out_cog):
    rio_copy(
        in_tif,
        out_cog,
        driver="COG",
        compress="deflate",
        blocksize=512,
        overview_resampling="nearest",
    )
    return out_cog

