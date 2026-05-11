import boto3
from datetime import datetime
from typing import Union
import os
import shutil

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

def download_s3_file(s3filepath : str, save_location : str = "/tmp/s3_temp") -> str:
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

def remove_s3_temp(save_location: str = "/tmp/s3_temp") -> None:
    shutil.rmtree(save_location)