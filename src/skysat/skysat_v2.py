import os
import re
import numpy as np
from osgeo import gdal
from datetime import datetime
import json

from shared_utils.s3utils import *
from shared_utils.geotools import *

def retrieve_skysat_resources(date, level, bucket="csdap-planet-skysat-delivery", prefix="disasters"):
    files = retrieve_s3_file_list(bucket, prefix)

    filtered_files = [
        x for x in files
        if len(x.split("/")) > 1 and f"_{level}_" in x.split("/")[1]
    ]

    subdirs = sorted(set(x.split("/")[1] for x in filtered_files))

    dates = [
        datetime.strptime(f"{x.split('_')[0]}_{x.split('_')[1]}", "%Y%m%d_%H%M%S")
        for x in subdirs
    ]

    if isinstance(date, str):
        date = datetime.strptime(date, "%Y-%m-%d %H:%M:%S")

    closest_date = min(dates, key=lambda d: abs(d - date))
    date_prefix = closest_date.strftime("%Y%m%d_%H%M%S")

    selected = [x for x in subdirs if x.startswith(date_prefix)][0]

    selected_files = [
        x for x in filtered_files
        if len(x.split("/")) > 1 and x.split("/")[1] == selected
    ]

    metadata = [
        x for x in selected_files
        if not x.lower().endswith((".tif", ".tiff"))
    ]

    tifs = [
        x for x in selected_files
        if x.lower().endswith((".tif", ".tiff"))
    ]

    print(f"Selected Satellogic folder: {selected}")
    print(f"Metadata files found: {len(metadata)}")
    print(f"TIF files found: {len(tifs)}")
    for t in tifs:
        print("  ", t)

    return (
        [f"s3://{bucket}/{x}" for x in metadata],
        [f"s3://{bucket}/{x}" for x in tifs],
    )