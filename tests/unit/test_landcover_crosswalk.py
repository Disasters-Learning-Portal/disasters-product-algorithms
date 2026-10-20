"""Pins the ONE land-cover crosswalk shared by every water-extent pipeline.

The Sentinel-2 STAC path, the Sentinel-2 .SAFE path and Landsat each carried
their own CDL table. CDL 112 (Perennial Ice/Snow) was fixed in the STAC copy
(rule 54) and left mapped to PERMANENT WATER in the other two. The NIR water
threshold is mean + nstd*std of the permanent-water sample; snow is bright at
NIR (~0.6-0.9 vs ~0.01-0.05 for open water), so it inflates both terms and
dry land falls under the threshold as "flooded".

Codes verified against the USDA NASS CDL code list and the NLCD legend (see
shared_utils/landcover.py).
"""

import os

import numpy as np
import pytest

rio = pytest.importorskip("rasterio")
from rasterio.transform import from_origin  # noqa: E402

from shared_utils import landcover  # noqa: E402
from landsat import landsat89_functions as lf  # noqa: E402
from sentinel2 import sentinel2_functions as sf  # noqa: E402
from sentinel2 import sentinel2_odr_functions as odr  # noqa: E402

# Every code with a class name in the USDA NASS CDL code list
# (https://www.nass.usda.gov/Research_and_Science/Cropland/sarsfaqs2.php).
USDA_CDL_CODES = [
    0, 1, 2, 3, 4, 5, 6, 10, 11, 12, 13, 14, *range(21, 40), *range(41, 78),
    81, 82, 83, 87, 88, 92, 111, 112, 121, 122, 123, 124, 131, 141, 142, 143,
    152, 176, 190, 195, *range(204, 251), 254]
USDA_CDL_CODES.remove(73)  # not in the list

CRS = "EPSG:32615"            # Iowa: inside the CONUS box -> CDL path
ORIGIN = (400000, 4600000)


def _cdl_reclass_file(module, tmp_path, codes):
    raw = str(tmp_path / "CDL_2024.tif")
    arr = np.asarray(codes, dtype=np.uint8).reshape(1, -1)
    with rio.open(raw, "w", driver="GTiff", count=1, dtype="uint8", height=1,
                  width=arr.shape[1], crs=CRS, transform=from_origin(*ORIGIN, 30, 30)) as d:
        d.write(arr, 1)
    out = module.reclass_cdl(raw, np.where(np.zeros(arr.shape, bool)))
    return landcover.read_reclass(out)


@pytest.mark.parametrize("module", [sf, lf], ids=["safe", "landsat"])
class TestFileReclassMatchesSharedTable:

    def test_ice_snow_is_not_water(self, module, tmp_path):
        buckets, sample = _cdl_reclass_file(module, tmp_path, [112])
        assert buckets[0, 0] == 999          # pre-fix: 4 (permanent water)
        assert not sample[0, 0]

    def test_open_water_is_label_and_sample(self, module, tmp_path):
        buckets, sample = _cdl_reclass_file(module, tmp_path, [111])
        assert buckets[0, 0] == 4 and sample[0, 0]

    def test_aquaculture_is_label_not_sample(self, module, tmp_path):
        buckets, sample = _cdl_reclass_file(module, tmp_path, [92])
        assert buckets[0, 0] == 4 and not sample[0, 0]

    def test_every_usda_code_reclassifies(self, module, tmp_path):
        # The old np.vectorize(dict.get) table had no entry for 62-65/82/83/
        # 87 and raised TypeError on ANY unmapped code in the scene.
        buckets, _ = _cdl_reclass_file(module, tmp_path, USDA_CDL_CODES)
        assert buckets.shape[1] == len(USDA_CDL_CODES)

    def test_identical_to_stac_path(self, module, tmp_path):
        buckets, sample = _cdl_reclass_file(module, tmp_path, USDA_CDL_CODES)
        arr = np.asarray(USDA_CDL_CODES, np.uint16).reshape(1, -1)
        ref_b, ref_s = odr._reclass_cdl_array(arr, np.zeros(arr.shape, bool))
        np.testing.assert_array_equal(buckets, ref_b)
        np.testing.assert_array_equal(sample, ref_s)


def test_select_water_sample_empty_raises():
    none = np.zeros((4, 4), bool)
    with pytest.raises(RuntimeError, match="No valid permanent-water"):
        landcover.select_water_sample(none, none)


def test_select_water_sample_falls_back_below_minimum():
    label = np.ones((20, 20), bool)       # 400 px of bucket 4
    sample = np.zeros((20, 20), bool)
    sample[0, :5] = True                  # 5 px of strict endmember
    assert landcover.select_water_sample(label, sample) is label


# ---------------------------------------------------------------------------
# End-to-end through the real gen_water_extent, both pipelines.
#
# 30x30 scene, NIR reflectance by column block:
#   0-9   CDL 111 open water     0.02 (+/- small noise)
#   10-14 CDL 112 ice/snow       0.80
#   15-19 CDL 92  aquaculture    0.06
#   20-29 CDL 1   corn: rows 0-9 flooded (0.02), rows 10-29 dry (0.30)
# Truth: threshold ~0.02-0.03 from open water alone -> flooded corn is class 4,
# dry corn is NOT water. With snow in the sample (pre-fix) the threshold is
# ~0.55 and the dry corn is reported flooded.
# ---------------------------------------------------------------------------

def _scene_arrays():
    rng = np.random.default_rng(0)
    nir = np.empty((30, 30))
    nir[:, 0:10] = 0.02 + rng.normal(0, 0.003, (30, 10))
    nir[:, 10:15] = 0.80
    nir[:, 15:20] = 0.06
    nir[:, 20:30] = 0.30
    nir[0:10, 20:30] = 0.02
    cdl = np.ones((30, 30), np.uint8)
    cdl[:, 0:10], cdl[:, 10:15], cdl[:, 15:20] = 111, 112, 92
    return nir, cdl


def _write(path, arr, dtype, res):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with rio.open(path, "w", driver="GTiff", count=1, dtype=dtype, height=arr.shape[0],
                  width=arr.shape[1], crs=CRS, transform=from_origin(*ORIGIN, res, res)) as d:
        d.write(arr.astype(dtype), 1)


def _prepare_outputs(tmp_path, monkeypatch, module, cdl, res):
    out_dir = tmp_path / "out"
    we = out_dir / "waterExtent" / "we.tif"
    os.makedirs(we.parent)
    os.makedirs(out_dir / "CDL")
    # A stale pre-fix cache, with everything labelled permanent water. The
    # versioned suffix must make the pipeline ignore it.
    _write(out_dir / "CDL" / "CDL_2024_reclass.tif", np.full(cdl.shape, 4), "uint16", res)
    monkeypatch.setattr(module, "download_cdl",
                        lambda image, year, outname: _write(outname, cdl, "uint8", res))
    return str(we)


def _assert_classes(we):
    assert (we[3:7, 2:8] == 1).all()       # open water -> permanent water
    assert (we[3:27, 16:19] == 1).all()    # aquaculture is still labelled water
    assert not (we[:, 11:14] == 1).any()   # snow is NOT permanent water
    assert (we[3:7, 22:28] == 4).all()     # flooded corn detected
    assert (we[14:27, 22:28] == 0).all()   # dry corn not flooded (pre-fix: 4)


def test_safe_water_extent_excludes_snow(tmp_path, monkeypatch):
    nir, cdl = _scene_arrays()
    indir = tmp_path / "20250701"
    safe = indir / "S2C_MSIL2A_20250701T165121_N0511_R026_T15TVG_20250701T202705.SAFE"
    os.makedirs(safe)
    # Minimal PB 05.11 L2A metadata: B8 is band_id 7, offset -1000.
    (safe / "MTD_MSIL2A.xml").write_text(
        '<root><BOA_QUANTIFICATION_VALUE unit="none">10000</BOA_QUANTIFICATION_VALUE>'
        '<BOA_ADD_OFFSET band_id="7">-1000</BOA_ADD_OFFSET>'
        '<Spectral_Information bandId="7" physicalBand="B8"></Spectral_Information></root>')
    _write(safe / "GRANULE" / "L2A_T" / "IMG_DATA" / "R10m" / "T15TVG_B08_10m.jp2",
           np.round(nir * 10000 + 1000), "uint16", 10)
    we_path = _prepare_outputs(tmp_path, monkeypatch, sf, cdl, 10)
    cloud = tmp_path / "cloud.tif"
    _write(cloud, np.zeros((15, 15)), "uint16", 20)
    sf.gen_water_extent(str(indir), 1, we_path, str(cloud))
    with rio.open(we_path) as src:
        _assert_classes(src.read(1))


def test_landsat_water_extent_excludes_snow(tmp_path, monkeypatch):
    nir, cdl = _scene_arrays()
    indir = tmp_path / "20250701"
    scene = indir / "LC09_L2SP_026031_20250701_20250702_02_T1"
    os.makedirs(scene)
    (scene / "LC09_L2SP_026031_20250701_20250702_02_T1_MTL.xml").write_text(
        "<LANDSAT_METADATA_FILE><IMAGE_ATTRIBUTES><SUN_ELEVATION>60</SUN_ELEVATION>"
        "</IMAGE_ATTRIBUTES><LEVEL2_SURFACE_REFLECTANCE_PARAMETERS>"
        "<REFLECTANCE_MULT_BAND_5>2.75e-05</REFLECTANCE_MULT_BAND_5>"
        "<REFLECTANCE_ADD_BAND_5>-0.2</REFLECTANCE_ADD_BAND_5>"
        "</LEVEL2_SURFACE_REFLECTANCE_PARAMETERS></LANDSAT_METADATA_FILE>")
    _write(scene / "LC09_L2SP_026031_20250701_20250702_02_T1_SR_B5.TIF",
           np.round((nir + 0.2) / 2.75e-5), "uint16", 30)
    we_path = _prepare_outputs(tmp_path, monkeypatch, lf, cdl, 30)
    cloud = tmp_path / "cloud.tif"
    _write(cloud, np.zeros((30, 30)), "uint16", 30)
    lf.gen_water_extent(str(indir), 1, we_path, str(cloud), 30.0)
    with rio.open(we_path) as src:
        _assert_classes(src.read(1))
