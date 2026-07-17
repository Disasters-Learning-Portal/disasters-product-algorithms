"""Integration tests for the Sentinel-2 merge path (s2_merge -> gen_merge).

Pins the two fixes ported from the Landsat merge path:

  - Fix 3 (gen_merge): a CRS-mismatched scene is reprojected to a TEMP file and
    swapped in, never reopened in 'w' mode on its own path. Reopening the source
    'w' truncated it mid-read and corrupted the merge. Verified by digesting the
    inputs before/after and confirming the temp dir is cleaned up.
  - Fix 4 (s2_merge): when mask=True, s2_merge returns the path of the *masked*
    product (under masked/), not the unmasked merge. Before the fix the caller
    COG'd/uploaded the unmasked merge and orphaned the masked copy.

Needs rasterio (Python). The mask path is fully rasterio-based (apply_cloud_mask
-> dump_geotiff both use rio.open), so no GDAL CLI is required.
"""

import hashlib
import os

import pytest

rasterio = pytest.importorskip("rasterio")
import numpy as np
from rasterio.crs import CRS
from rasterio.transform import from_bounds


def _make_3band(path, ox, oy, epsg=32610, size=16, span=160):
    data = np.random.randint(1, 255, (3, size, size), dtype=np.uint8)
    transform = from_bounds(ox, oy, ox + span, oy + span, size, size)
    with rasterio.open(
        path, "w", driver="GTiff", height=size, width=size, count=3,
        dtype="uint8", crs=CRS.from_epsg(epsg), transform=transform, nodata=0,
    ) as dst:
        dst.write(data)


def _make_1band(path, ox, oy, epsg=32610, size=16, span=160,
                dtype="float32", nodata=-9999.0, cloud=False):
    if cloud:
        data = np.zeros((1, size, size), dtype=dtype)
        data[0, 0, 0] = 1  # at least one masked pixel
    else:
        data = np.random.uniform(0.0, 1.0, (1, size, size)).astype(dtype)
    transform = from_bounds(ox, oy, ox + span, oy + span, size, size)
    with rasterio.open(
        path, "w", driver="GTiff", height=size, width=size, count=1,
        dtype=dtype, crs=CRS.from_epsg(epsg), transform=transform, nodata=nodata,
    ) as dst:
        dst.write(data)


def _digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# --------------------------------------------------------------------------- #
# Fix 3 — gen_merge must not mutate inputs; temp reprojection dir is cleaned
# --------------------------------------------------------------------------- #

def test_s2_gen_merge_multicrs_does_not_mutate_inputs(tmp_path):
    from sentinel2.sentinel2_functions import gen_merge
    import glob as _glob

    d = tmp_path / "mNDWI"
    d.mkdir()
    a = str(d / "S2B_MSIL2A_mNDWI_a.tif")
    b = str(d / "S2B_MSIL2A_mNDWI_b.tif")
    c = str(d / "S2B_MSIL2A_mNDWI_c.tif")
    _make_3band(a, 500000, 4000000, epsg=32610)
    _make_3band(b, 500160, 4000000, epsg=32610)
    _make_3band(c, 500000, 4000000, epsg=32611)  # different UTM zone -> must reproject
    before = {p: _digest(p) for p in (a, b, c)}

    out = str(d / "S2B_MSIL2A_mNDWI_merged_x.tif")
    gen_merge([a, b, c], out)

    # Every source scene is byte-identical after the merge (no in-place 'w').
    assert {p: _digest(p) for p in (a, b, c)} == before
    with rasterio.open(out) as src:
        assert src.count == 3          # merged output is a valid raster, not corrupt
    # The reprojection temp dir is cleaned up.
    assert not _glob.glob("/tmp/s2_merge_reproj_*")


def test_s2_gen_merge_singlecrs_valid_and_no_tmp(tmp_path):
    from sentinel2.sentinel2_functions import gen_merge
    import glob as _glob

    d = tmp_path / "trueColor"
    d.mkdir()
    a = str(d / "S2B_MSIL2A_trueColor_a.tif")
    b = str(d / "S2B_MSIL2A_trueColor_b.tif")
    _make_3band(a, 500000, 4000000)
    _make_3band(b, 500160, 4000000)
    before = {p: _digest(p) for p in (a, b)}

    out = str(d / "S2B_MSIL2A_trueColor_merged_x.tif")
    gen_merge([a, b], out)

    assert os.path.exists(out)
    assert {p: _digest(p) for p in (a, b)} == before
    assert not _glob.glob("/tmp/s2_merge_reproj_*")  # same-CRS path makes no temp dir


# --------------------------------------------------------------------------- #
# Fix 4 — s2_merge returns the masked product path when mask=True
# --------------------------------------------------------------------------- #

def test_s2_merge_mask_returns_existing_masked_path(tmp_path):
    from sentinel2.sentinel2_functions import s2_merge

    prod = tmp_path / "NDWI"
    prod.mkdir()
    cm = tmp_path / "cloudMask"
    cm.mkdir()
    # one 1-band index scene; merge of a single file preserves its geometry
    _make_1band(str(prod / "S2B_MSIL2A_NDWI_2026-06-06.tif"), 500000, 4000000)
    # a merged cloud mask with identical geometry (so resolutions match)
    _make_1band(str(cm / "S2B_MSIL2A_cloudMask_merged_2026-06-06.tif"),
                500000, 4000000, dtype="uint8", nodata=255, cloud=True)

    out = s2_merge(str(prod), mask=True)

    # Returns the masked path (not the unmasked merge), and it exists on disk.
    assert os.path.basename(out) == "S2B_MSIL2A_NDWI_merged_masked_2026-06-06.tif"
    assert os.path.basename(os.path.dirname(out)) == "masked"
    assert os.path.exists(out)


def test_s2_merge_no_mask_returns_merged(tmp_path):
    from sentinel2.sentinel2_functions import s2_merge

    prod = tmp_path / "NDWI"
    prod.mkdir()
    _make_1band(str(prod / "S2B_MSIL2A_NDWI_2026-06-06.tif"), 500000, 4000000)

    out = s2_merge(str(prod), mask=False)

    assert os.path.basename(out) == "S2B_MSIL2A_NDWI_merged_2026-06-06.tif"
    assert os.path.exists(out)
