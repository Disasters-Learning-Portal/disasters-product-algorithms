"""Functions for downloading VIIRS nighttime lights data from NASA LAADS."""

import logging
import warnings
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import earthaccess
import rasterio
from rasterio.errors import NotGeoreferencedWarning


logger = logging.getLogger(__name__)


BM_SHORT_NAME = "VNP46A2"
BM_VERSION = "2"

NTL_DATASET_PATH = "HDFEOS/GRIDS/VIIRS_Grid_DNB_2d/Data_Fields/Gap_Filled_DNB_BRDF-Corrected_NTL"


def convert_to_tiff(
    input_h5: str | Path, output_path: str | Path, dataset_path: str = NTL_DATASET_PATH
) -> Path:

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        subdatasets = rasterio.open(input_h5).subdatasets
    dnb_sds = [sds for sds in subdatasets if dataset_path in sds][0]

    with rasterio.open(dnb_sds, "r") as src:
        profile = src.profile
        data = src.read(1)

    dst_profile = deepcopy(profile)
    dst_profile.update(
        driver="GTiff", predictor=3, compress="deflate", blockxsize=256, blockysize=256
    )
    with rasterio.open(output_path, mode="w", **dst_profile) as dst:
        dst.write(data, 1)

    return Path(output_path)


def download_viirs(
    dt: datetime, bbox: tuple[float, float, float, float], output_dir: str | Path
) -> dict[str, list[Path]]:
    """Search and download VIIRS VNP46A2 files for a given date and bounding box."""

    logger.info("Logging in to Earthdata...")
    earthaccess.login()

    logger.info("Searching for VIIRS data...")
    results = earthaccess.search_data(
        short_name=BM_SHORT_NAME,
        version=BM_VERSION,
        temporal=(dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%dT23:59:59"), True),
        bounding_box=bbox,
    )

    logger.info("Downloading VIIRS data...")
    filelist = earthaccess.download(results, local_path=output_dir, show_progress=False)

    tiff_filelist = [convert_to_tiff(f, f.with_suffix(".tif")) for f in filelist]

    return {"gap_filled_ntl": tiff_filelist}
