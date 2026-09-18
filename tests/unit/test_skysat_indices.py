"""Tests for the SkySat index/composite path (``skysat.skysat_v2``).

SkySat indices come from Planet's 4-band ``_analytic`` asset (B,G,R,NIR):
reflectance is DN x the per-band ``reflectance_coefficients`` carried in the
GeoTIFF's own ``ImageDescription`` JSON, and clouds are masked from the
companion ``_udm2.tif`` (band 1 == 1 is clear).

The processor sat on an unmerged branch with the masked path broken
(``UDM_CLEAR_BAND`` was never defined, so any collect that HAD a UDM2 raised
``NameError``), a literal ``ssc1`` file selector that matched no other
satellite, and DN-0 fill that EVI turned into a plausible 0.0. These pin each.
"""

import json
import os

import numpy as np
import pytest

pytest.importorskip("osgeo")

from osgeo import gdal, osr  # noqa: E402

STEM = "20260420_213658_ssc2_u0002"
ANALYTIC = f"s3://bucket/disasters/event/collect/{STEM}_analytic.tif"
UDM2 = f"s3://bucket/disasters/event/collect/{STEM}_udm2.tif"

# blue, green, red, nir
COEFFS = [1e-4, 2e-4, 1e-4, 2e-4]
DN = {"blue": 1000, "green": 1500, "red": 2000, "nir": 3000}
REFL = {"blue": 0.1, "green": 0.3, "red": 0.2, "nir": 0.6}

CI = {
    "red": gdal.GCI_RedBand,
    "green": gdal.GCI_GreenBand,
    "blue": gdal.GCI_BlueBand,
    "undefined": gdal.GCI_Undefined,
}


def _write(path, bands, dtype=gdal.GDT_UInt16, colorinterp=None, description=None):
    rows, cols = bands[0].shape
    ds = gdal.GetDriverByName("GTiff").Create(str(path), cols, rows, len(bands), dtype)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32617)
    ds.SetProjection(srs.ExportToWkt())
    ds.SetGeoTransform((500000.0, 1.0, 0.0, 3200000.0, 0.0, -1.0))
    for i, arr in enumerate(bands, start=1):
        ds.GetRasterBand(i).WriteArray(arr)
        if colorinterp:
            ds.GetRasterBand(i).SetColorInterpretation(CI[colorinterp[i - 1]])
    if description is not None:
        ds.SetMetadataItem("TIFFTAG_IMAGEDESCRIPTION", description)
    ds.FlushCache()
    ds = None
    return str(path)


def _analytic(tmp_path, order=("blue", "green", "red", "nir"), colorinterp=None,
              coeffs=None, fill_corner=False):
    """4-band analytic scene; ``order`` is the role held by each band, 1..4."""
    bands = [np.full((8, 8), DN[role], dtype=np.uint16) for role in order]
    if fill_corner:
        for b in bands:
            b[0, 0] = 0
    if coeffs is None:
        coeffs = [COEFFS[("blue", "green", "red", "nir").index(r)] for r in order]
    desc = json.dumps({"properties": {"reflectance_coefficients": coeffs}})
    return _write(tmp_path / f"{STEM}_analytic.tif", bands,
                  colorinterp=colorinterp, description=desc)


def _udm2(tmp_path, clear):
    return _write(tmp_path / f"{STEM}_udm2.tif", [clear.astype(np.uint8)],
                  dtype=gdal.GDT_Byte)


@pytest.fixture
def local_s3(tmp_path, monkeypatch):
    """Resolve the fake s3:// paths to same-named files in ``tmp_path``."""
    from skysat import skysat_v2

    monkeypatch.setattr(
        skysat_v2, "fetch_local",
        lambda s3_path, cache_dir=None: str(tmp_path / os.path.basename(s3_path)),
    )
    return tmp_path


def _read(path):
    ds = gdal.Open(path)
    return ds.GetRasterBand(1).ReadAsArray()


class TestIndices:
    def test_ndvi_and_ndwi_values(self, local_s3):
        from skysat.skysat_v2 import calc_ndvi, calc_ndwi

        _analytic(local_s3)
        out = str(local_s3 / "out")

        ndvi = _read(calc_ndvi([ANALYTIC], out)[0])
        ndwi = _read(calc_ndwi([ANALYTIC], out)[0])

        assert np.allclose(ndvi, (REFL["nir"] - REFL["red"]) / (REFL["nir"] + REFL["red"]))
        # McFeeters: green - NIR, negative over land.
        assert np.allclose(ndwi, (REFL["green"] - REFL["nir"]) / (REFL["green"] + REFL["nir"]))

    def test_udm2_present_run_completes_and_masks(self, local_s3):
        """The masked path raised NameError on the original branch."""
        from skysat.skysat_v2 import NODATA_FLOAT, calc_ndvi

        _analytic(local_s3)
        clear = np.ones((8, 8))
        clear[:, :4] = 0
        _udm2(local_s3, clear)

        ndvi = _read(calc_ndvi([ANALYTIC, UDM2], str(local_s3 / "out"))[0])

        assert (ndvi[:, :4] == NODATA_FLOAT).all()
        assert np.allclose(ndvi[:, 4:], 0.5)

    def test_missing_udm2_runs_unmasked(self, local_s3, capsys):
        from skysat.skysat_v2 import NODATA_FLOAT, calc_ndvi

        _analytic(local_s3)
        ndvi = _read(calc_ndvi([ANALYTIC], str(local_s3 / "out"))[0])

        assert "UDM2 file not found" in capsys.readouterr().out
        assert not (ndvi == NODATA_FLOAT).any()

    def test_evi_fill_is_nodata_not_zero(self, local_s3):
        """EVI's ``+ 1`` denominator turns unmasked DN-0 fill into a finite 0.0."""
        from skysat.skysat_v2 import NODATA_FLOAT, calc_evi

        _analytic(local_s3, fill_corner=True)
        evi = _read(calc_evi([ANALYTIC], str(local_s3 / "out"))[0])

        assert evi[0, 0] == NODATA_FLOAT
        expected = 2.5 * (0.6 - 0.2) / (0.6 + 6 * 0.2 - 7.5 * 0.1 + 1.0)
        assert np.allclose(evi[1:, 1:], expected)


class TestBandOrder:
    def test_declared_colorinterp_wins_over_the_layout(self, local_s3, capsys):
        """R,G,B declared on bands 1-3: red is band 1, and so is its coefficient."""
        from skysat.skysat_v2 import calc_ndvi

        _analytic(local_s3, order=("red", "green", "blue", "nir"),
                  colorinterp=["red", "green", "blue", "undefined"])
        ndvi = _read(calc_ndvi([ANALYTIC], str(local_s3 / "out"))[0])

        assert "from the file's own ColorInterp" in capsys.readouterr().out
        assert np.allclose(ndvi, 0.5)

    def test_undeclared_falls_back_to_planet_layout(self, tmp_path, capsys):
        from skysat.skysat_v2 import BAND_ORDER, resolve_band_indices

        ds = gdal.Open(_analytic(tmp_path))
        assert resolve_band_indices(ds, "analytic") == BAND_ORDER["analytic"]
        assert "Planet analytic layout" in capsys.readouterr().out

    def test_visual_layout_is_rgb_not_bgr(self):
        from skysat.skysat_v2 import BAND_ORDER

        assert BAND_ORDER["visual"] == {"red": 1, "green": 2, "blue": 3}

    def test_three_band_declared_file_has_no_nir(self, tmp_path):
        from skysat.skysat_v2 import band_order_from_colorinterp

        path = _write(tmp_path / "v.tif", [np.ones((4, 4), np.uint8)] * 3,
                      dtype=gdal.GDT_Byte, colorinterp=["red", "green", "blue"])
        assert band_order_from_colorinterp(gdal.Open(path)) == {
            "red": 1, "green": 2, "blue": 3,
        }

    def test_index_from_a_file_without_nir_raises(self, tmp_path):
        from skysat.skysat_v2 import load_reflectance

        path = _write(tmp_path / "v.tif", [np.ones((4, 4), np.uint8)] * 3,
                      dtype=gdal.GDT_Byte, colorinterp=["red", "green", "blue"])
        with pytest.raises(ValueError, match="no NIR band"):
            load_reflectance(gdal.Open(path), ("nir", "red"), "visual")

    def test_colorir_refuses_visual(self):
        from skysat.skysat_v2 import produce_colorir

        with pytest.raises(ValueError, match="does not provide the NIR band"):
            produce_colorir([ANALYTIC], "visual")


class TestFileSelection:
    def test_any_satellite_number_is_selected(self):
        """The selector was a literal ``ssc1``; the constellation is ssc1..ssc21."""
        from skysat.skysat_v2 import get_skysat_product_files

        paths = [
            "s3://b/d/20260420_213658_ssc2_u0001_analytic.tif",
            "s3://b/d/20260420_213658_ssc14_u0002_analytic.TIF",
            "s3://b/d/20260420_213658_ssc2_u0001_udm2.tif",
            "s3://b/d/20260420_213658_ssc2d1_0001_basic_analytic.tif",
        ]
        assert get_skysat_product_files(paths, "analytic") == sorted(paths[:2])
        assert get_skysat_product_files(paths, "basic_analytic") == [paths[3]]


class TestOutputName:
    def test_shape_and_product_directory(self, tmp_path):
        from shared_utils.product_paths import product_dir
        from skysat.skysat_v2 import build_output_name

        out = build_output_name(ANALYTIC, str(tmp_path), "NDVI")

        assert os.path.dirname(out) == str(tmp_path / product_dir("skysat", "NDVI"))
        assert os.path.basename(out) == "SkySat_TOA_NDVI_ssc2_u0002_2026-04-20T21:36:58Z.tif"

    def test_scenes_of_one_collect_do_not_collide(self, tmp_path):
        from skysat.skysat_v2 import build_output_name

        a = build_output_name("20260420_213658_ssc2_u0001_analytic.tif", str(tmp_path), "NDWI")
        b = build_output_name("20260420_213658_ssc2_u0002_analytic.tif", str(tmp_path), "NDWI")
        assert a != b

    def test_name_is_a_fixed_point_of_the_shared_builder(self, tmp_path):
        from shared_utils.file_naming import create_output_filename
        from skysat.skysat_v2 import build_output_name

        name = os.path.basename(build_output_name(ANALYTIC, str(tmp_path), "NDVI"))
        assert create_output_filename(name, "") == name

    def test_visual_asset_is_not_labelled_toa(self, tmp_path):
        from skysat.skysat_v2 import build_output_name

        out = build_output_name(f"{STEM}_visual.tif", str(tmp_path), "TrueColor")
        assert os.path.basename(out).startswith("SkySat_Visual_TrueColor_")

    def test_unrecognized_name_raises(self, tmp_path):
        from skysat.skysat_v2 import build_output_name

        with pytest.raises(ValueError, match="Unrecognized SkySat asset name"):
            build_output_name("SkySat_SR_TrueColor_2026-08-12T232033Z.tif", str(tmp_path), "NDVI")


class TestCliCogContract:
    """``process_skysat`` -> ``convert_to_cog``: the branch left dst_crs unset
    (library default = an EPSG:3857 warp) and had no way to embed activation tags.
    """

    def _run(self, monkeypatch, tmp_path, argv):
        import runpy
        import sys

        import shared_utils.cog_utils as cog_utils
        from skysat import skysat_v2

        calls = []
        monkeypatch.setattr(cog_utils, "convert_to_cog",
                            lambda path, **kw: calls.append(kw) or path)
        monkeypatch.setattr(skysat_v2, "retrieve_skysat_resources", lambda date, **kw: [ANALYTIC])
        monkeypatch.setattr(skysat_v2, "calc_ndwi", lambda tifs, out: ["ndwi.tif"])
        monkeypatch.setattr(sys, "argv", ["process_skysat", "--date", "2026-04-20 21:36:58",
                                          "--output", str(tmp_path / "out"), *argv])

        script = os.path.join(os.path.dirname(skysat_v2.__file__), "process_skysat.py")
        runpy.run_path(script, run_name="__main__")
        return calls

    def test_index_is_native_crs_with_tags(self, monkeypatch, tmp_path):
        meta = tmp_path / "meta.json"
        meta.write_text(json.dumps({"ACTIVATION_EVENT": "202604_Typhoon_Guam", "SOURCE": "CSDA"}))

        (kw,) = self._run(monkeypatch, tmp_path,
                          ["--product", "ndwi", "--metadata-json", str(meta)])

        assert kw["nodata"] == -9999
        assert kw["dst_crs"] is None
        # A caller-supplied SOURCE survives (setdefault, not assignment).
        assert kw["metadata"]["SOURCE"] == "CSDA"
        assert kw["metadata"]["PROCESSING_LEVEL"] == "TOA"

    def test_no_metadata_json_keeps_the_subprocess_backend(self, monkeypatch, tmp_path):
        """A dict here would silently switch convert_to_cog onto cog_translate."""
        (kw,) = self._run(monkeypatch, tmp_path, ["--product", "ndwi"])
        assert kw["metadata"] is None


def test_valid_dates_recognizes_the_skysat_bucket(monkeypatch):
    """``retrieve_s3_valid_dates`` raised ValueError for any skysat bucket."""
    from datetime import datetime

    from shared_utils import s3utils

    monkeypatch.setattr(s3utils, "retrieve_s3_file_list", lambda bucket, prefix: [
        "disasters/event/collect_a/20260420_213658_ssc2_u0001_analytic.tif",
        "disasters/event/collect_a/20260420_213658_ssc2_u0001_udm2.tif",
        "disasters/event/collect_b/20260323_173545_ssc14_u0001_visual.tif",
        "disasters/readme.txt",
    ])

    assert s3utils.retrieve_s3_valid_dates("csdap-planet-skysat-delivery", "disasters") == [
        datetime(2026, 3, 23, 17, 35, 45),
        datetime(2026, 4, 20, 21, 36, 58),
    ]
