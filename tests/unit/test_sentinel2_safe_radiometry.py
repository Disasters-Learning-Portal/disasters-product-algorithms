"""Pins the .SAFE pipeline's DN -> reflectance conversion (sentinel2_functions).

ESA's conversion is ``reflectance = (DN + offset) / quantification``. From
processing baseline 04.00 (2022-01-25), and on every reprocessed Collection-1
product, the offset is -1000 for every band (L1C RADIO_ADD_OFFSET, L2A
BOA_ADD_OFFSET); earlier baselines carry no offset. The pipeline used to
divide by 10000 and ignore the offset, adding +0.1 to every band -- a bias that
does not cancel in a normalized difference (healthy-corn NDVI 0.81 -> 0.53,
water NDWI 0.64 -> 0.14).

The metadata layout below is copied from real products (S2C T15TYH
2025-09-29, PB 05.11, L1C + L2A): band_id 0..12 maps to B1..B12/B8A through
Spectral_Information, and NODATA is DN 0.
"""

import os

import numpy as np
import pytest

rio = pytest.importorskip("rasterio")
from rasterio.transform import from_origin  # noqa: E402

from sentinel2 import sentinel2_functions as sf  # noqa: E402

PHYSICAL = ["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B9",
            "B10", "B11", "B12"]
CRS = "EPSG:32633"  # Europe: keeps the water-extent test on the WorldCover path


def _mtd(level, offset):
    quant_tag = "BOA_QUANTIFICATION_VALUE" if level == "MSIL2A" else "QUANTIFICATION_VALUE"
    spectral = "".join(
        f'<Spectral_Information bandId="{i}" physicalBand="{b}"></Spectral_Information>'
        for i, b in enumerate(PHYSICAL))
    offsets = ""
    if offset is not None:
        if level == "MSIL2A":
            offsets = "<BOA_ADD_OFFSET_VALUES_LIST>" + "".join(
                f'<BOA_ADD_OFFSET band_id="{i}">{offset}</BOA_ADD_OFFSET>'
                for i in range(13)) + "</BOA_ADD_OFFSET_VALUES_LIST>"
        else:
            offsets = "<Radiometric_Offset_List>" + "".join(
                f'<RADIO_ADD_OFFSET band_id="{i}">{offset}</RADIO_ADD_OFFSET>'
                for i in range(13)) + "</Radiometric_Offset_List>"
    return (
        '<?xml version="1.0" encoding="UTF-8"?><root>'
        f'<{quant_tag} unit="none">10000</{quant_tag}>{offsets}'
        f"<Spectral_Information_List>{spectral}</Spectral_Information_List>"
        "</root>")


def _write_band(path, dn, res):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # GDAL identifies the format by content, so a GTiff named .jp2 is read
    # exactly like the real JPEG2000 band would be.
    with rio.open(path, "w", driver="GTiff", count=1, dtype="uint16",
                  height=dn.shape[0], width=dn.shape[1], crs=CRS,
                  transform=from_origin(500000, 5000000, res, res)) as dst:
        dst.write(dn.astype(np.uint16), 1)


def make_safe(root, level, offset, bands, name=None):
    """Synthetic .SAFE; `bands` maps band name -> DN array (written at 10 m)."""
    name = name or f"S2C_{level}_20250929T165121_N0511_R026_T33UUP_20250929T202705.SAFE"
    safe = os.path.join(str(root), name)
    os.makedirs(safe, exist_ok=True)
    with open(os.path.join(safe, f"MTD_{level}.xml"), "w") as f:
        f.write(_mtd(level, offset))
    for band, dn in bands.items():
        if level == "MSIL2A":
            p = os.path.join(safe, "GRANULE", "L2A_T", "IMG_DATA", "R10m", f"T33UUP_{band}_10m.jp2")
        else:
            p = os.path.join(safe, "GRANULE", "L1C_T", "IMG_DATA", f"T33UUP_{band}.jp2")
        _write_band(p, dn, 10)
    return safe


# Surface truth for three pixels: corn, water, soil.
RED = np.array([0.04, 0.02, 0.15])
GREEN = np.array([0.06, 0.06, 0.12])
NIR = np.array([0.45, 0.01, 0.21])


def _dn(refl, offset):
    return np.round(refl * 10000 - offset).astype(np.uint16).reshape(1, -1)


class TestRadiometricScaling:

    @pytest.mark.parametrize("level", ["MSIL2A", "MSIL1C"])
    @pytest.mark.parametrize("band", ["B02", "B08", "B8A", "B11", "B12"])
    def test_pb04_offset_read_per_band(self, tmp_path, level, band):
        safe = make_safe(tmp_path, level, -1000, {})
        assert sf.get_radiometric_scaling(safe, level, band) == (-1000.0, 10000.0)

    @pytest.mark.parametrize("level", ["MSIL2A", "MSIL1C"])
    def test_pre_pb04_has_no_offset(self, tmp_path, level):
        safe = make_safe(tmp_path, level, None, {})
        assert sf.get_radiometric_scaling(safe, level, "B08") == (0.0, 10000.0)

    def test_offset_keyed_by_band_id_not_position(self, tmp_path):
        # Distinct offset for B8A (band_id 8) only: the parser must follow
        # Spectral_Information, not assume B08 and B8A share an index.
        safe = make_safe(tmp_path, "MSIL2A", -1000, {})
        mtd = os.path.join(safe, "MTD_MSIL2A.xml")
        txt = open(mtd).read().replace(
            '<BOA_ADD_OFFSET band_id="8">-1000', '<BOA_ADD_OFFSET band_id="8">-777')
        open(mtd, "w").write(txt)
        assert sf.get_radiometric_scaling(safe, "MSIL2A", "B8A")[0] == -777.0
        assert sf.get_radiometric_scaling(safe, "MSIL2A", "B08")[0] == -1000.0

    def test_missing_metadata_raises(self, tmp_path):
        safe = os.path.join(str(tmp_path), "X.SAFE")
        os.makedirs(safe)
        with pytest.raises(FileNotFoundError):
            sf.get_radiometric_scaling(safe, "MSIL2A", "B08")


class TestExtractedReflectance:

    @pytest.mark.parametrize("level", ["MSIL2A", "MSIL1C"])
    @pytest.mark.parametrize("offset", [-1000, None])
    def test_reflectance_recovered_and_fill_is_nan(self, tmp_path, level, offset):
        dn = _dn(NIR, offset or 0)
        dn = np.concatenate([dn, [[0]]], axis=1)  # trailing NODATA pixel
        safe = make_safe(tmp_path, level, offset, {"B08": dn})
        out = sf.extract_band_geotiffs("B08", safe, level, "10")
        with rio.open(out) as src:
            refl = src.read(1)
            assert np.isnan(src.nodata)
        np.testing.assert_allclose(refl[0, :3], NIR, atol=1e-6)
        assert np.isnan(refl[0, 3])

    def test_dark_water_keeps_negative_reflectance(self, tmp_path):
        # DN 950 under a -1000 offset is -0.005: real Sen2Cor noise around a
        # ~0 true value. It must stay a valid (finite) sample, not become fill.
        safe = make_safe(tmp_path, "MSIL2A", -1000, {"B08": np.array([[950]])})
        out = sf.extract_band_geotiffs("B08", safe, "MSIL2A", "10")
        with rio.open(out) as src:
            assert src.read(1)[0, 0] == pytest.approx(-0.005, abs=1e-7)


def _index(tmp_path, fn, bands, offset=-1000):
    safe = make_safe(tmp_path, "MSIL2A", offset,
                     {b: _dn(v, offset or 0) for b, v in bands.items()})
    out = os.path.join(str(tmp_path), "index.tif")
    fn(safe, out, "MSIL2A")
    with rio.open(out) as src:
        return src.read(1)[0]


class TestIndicesUseTrueReflectance:

    @pytest.mark.parametrize("offset", [-1000, None])
    def test_ndvi(self, tmp_path, offset):
        got = _index(tmp_path, sf.gen_ndvi, {"B04": RED, "B08": NIR}, offset)
        # pre-fix (offset ignored) on PB>=04: [0.5263, -0.0435, 0.1176]
        np.testing.assert_allclose(got, (NIR - RED) / (NIR + RED), atol=1e-4)

    @pytest.mark.parametrize("offset", [-1000, None])
    def test_ndwi(self, tmp_path, offset):
        got = _index(tmp_path, sf.gen_ndwi, {"B03": GREEN, "B08": NIR}, offset)
        # pre-fix water NDWI was 0.14 instead of 0.71
        np.testing.assert_allclose(got, (GREEN - NIR) / (GREEN + NIR), atol=1e-4)

    def test_clear_water_negative_nir_is_ndwi_one_not_nodata(self, tmp_path):
        got = _index(tmp_path, sf.gen_ndwi,
                     {"B03": np.array([0.05]), "B08": np.array([-0.005])})
        assert got[0] == pytest.approx(1.0)

    def test_fill_is_nodata(self, tmp_path):
        safe = make_safe(tmp_path, "MSIL2A", -1000,
                         {"B04": np.array([[0, 1400]]), "B08": np.array([[5500, 0]])})
        out = os.path.join(str(tmp_path), "ndvi.tif")
        sf.gen_ndvi(safe, out, "MSIL2A")
        with rio.open(out) as src:
            assert src.read(1).tolist() == [[-9999.0, -9999.0]]

    def test_both_bands_zero_is_nodata(self):
        out = sf.normalized_difference(np.array([0.0, -0.01]), np.array([0.0, -0.02]))
        assert out.tolist() == [-9999.0, -9999.0]


class TestTrueColorAlpha:

    def test_fill_transparent_dark_pixel_opaque(self, tmp_path):
        # pixel 0 fill, pixel 1 legitimately dark (<= 0 after the offset)
        dn = np.array([[0, 990]])
        safe = make_safe(tmp_path, "MSIL2A", -1000, {"B02": dn, "B03": dn, "B04": dn})
        files = [sf.extract_band_geotiffs(b, safe, "MSIL2A", "10") for b in ("B04", "B03", "B02")]
        r, g, b, alpha, _, _ = sf.gen_rgb(*files, rayleigh=False, enhance=2)
        assert alpha.tolist() == [[0, 255]]
        assert r.tolist() == [[0, 0]]


def test_water_extent_nodata_follows_nan_fill(tmp_path):
    """NaN band fill must survive gen_merge and land in water-extent class 0,
    while a negative-NIR water pixel stays valid data (classified water)."""
    indir = tmp_path / "20250929"
    # 4x4 at 10 m: column 0 is fill, column 1 is dark water (NIR < 0 after
    # the offset), the rest bright land.
    nir = np.full((4, 4), 0.30)
    nir[:, 1] = -0.004
    dn = _dn(nir.ravel(), -1000).reshape(4, 4)
    dn[:, 0] = 0
    make_safe(indir, "MSIL2A", -1000, {"B08": dn})

    out_dir = tmp_path / "out"
    we_dir = out_dir / "waterExtent"
    os.makedirs(we_dir)
    # reference: column 1 is permanent water (4), the rest crop (1)
    ref = np.ones((4, 4), dtype=np.int16)
    ref[:, 1] = 4
    os.makedirs(out_dir / "WorldCover")
    with rio.open(out_dir / "WorldCover" / "WorldCover_2021_reclass.tif", "w", driver="GTiff",
                  count=1, dtype="int16", height=4, width=4, crs=CRS,
                  transform=from_origin(500000, 5000000, 10, 10)) as dst:
        dst.write(ref, 1)
    cloud = tmp_path / "cloud.tif"  # 20 m, upsampled x2 inside gen_water_extent
    with rio.open(cloud, "w", driver="GTiff", count=1, dtype="uint16", height=2, width=2,
                  crs=CRS, transform=from_origin(500000, 5000000, 20, 20)) as dst:
        dst.write(np.zeros((2, 2), dtype=np.uint16), 1)

    out = str(we_dir / "we.tif")
    sf.gen_water_extent(str(indir), 1, out, str(cloud))
    with rio.open(out) as src:
        we = src.read(1)
    assert (we[:, 0] == 0).all()   # fill -> nodata class
    assert (we[:, 1] == 1).all()   # negative-NIR water -> permanent water
