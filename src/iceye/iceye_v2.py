import os
import re
import numpy as np
from osgeo import gdal
from datetime import datetime
import json
from typing import Literal, Union
import rasterio
from glob import glob

from shared_utils.s3utils import *
from shared_utils.geotools import *

def retrieve_iceye_resources(date: Union[str, datetime], bucket="csdap-iceye-delivery", prefix="disasters"):
    files = retrieve_s3_file_list(bucket, prefix)

    filtered_files = [x for x in files if len(x.split("/")) > 2]

    datestrings = set([x.split("/")[-1] for x in filtered_files if (x.split("/")[-1].endswith(".tif") and ("GRD" in x.split("/")[-1]))])
    dates = [datetime.strptime(x.split("_")[-1].split(".")[0], "%Y%m%dT%H%M%S") for x in datestrings]

    if isinstance(date, str):
        date = datetime.strptime(date, "%Y-%m-%d %H:%M:%S")

    closest_date = min(dates, key=lambda d: abs(d - date))

    selected_files = [x for x in filtered_files if x.split(".")[0].endswith(closest_date.strftime("%Y%m%dT%H%M%S"))]
    # Much more rudimentary file selection method since the prefix depth seems to vary for Iceye, but the filenames do not
    # This can be updated in the future if things break, but for now it's capturing all the data that's in there fine

    tifs = [
        x for x in selected_files
        if x.lower().endswith((".tif", ".tiff"))
    ]

    metadata = [
        x for x in selected_files
        if x.lower().endswith((".xml"))
    ]

    print(f"TIF files found: {len(tifs)}")
    for t in tifs:
        print("  ", t)

    print(f"Metadata files found: {len(metadata)}")
    for m in metadata:
        print("  ", m)

    return [f"s3://{bucket}/{x}" for x in metadata], [f"s3://{bucket}/{x}" for x in tifs]