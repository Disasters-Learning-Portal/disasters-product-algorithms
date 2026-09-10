"""Unit tests for the ICEYE GRD pipeline (PR #148).

Pins the pure-Python parts that need no vendor S3:

1. ``parse_corner_coordinate`` accepts both the bracketed/comma form the ICEYE
   metadata reference documents (``[16878,1,35.17738,-118.11233]``) and the
   whitespace form, and returns ``(col, row, lat, lon)``.
2. ``georeference_from_iceye_xml`` treats the corner coordinates as pixel
   CENTRES: the derived geotransform maps pixel centre (0.5, 0.5) back onto
   ``coord_first_near`` and the far/last corners onto theirs.
3. ``sigmaCalib`` writes ``ICEYE_NODATA`` into the zero-fill border (no
   ``-inf`` from ``log10(0)``), names the output through the shared SAR
   builder, and returns a single path.
"""

import os
import uuid

import numpy as np
import pytest

pytest.importorskip("osgeo.gdal")
pytest.importorskip("scipy")

from osgeo import gdal, osr

from iceye import iceye_v2
from iceye.iceye_v2 import (
    ICEYE_NODATA,
    georeference_from_iceye_xml,
    parse_corner_coordinate,
    parse_grd_metadata,
    sigmaCalib,
)


# ---------------------------------------------------------------- parsing

@pytest.mark.parametrize("value", [
    "[16878,1,35.17738,-118.11233]",
    "[16878, 1, 35.17738, -118.11233]",
    "16878 1 35.17738 -118.11233",
])
def test_parse_corner_coordinate_accepts_documented_and_whitespace_forms(value):
    assert parse_corner_coordinate(value) == (16878, 1, 35.17738, -118.11233)


def test_parse_corner_coordinate_rejects_wrong_arity():
    with pytest.raises(ValueError, match="Unexpected ICEYE coordinate format"):
        parse_corner_coordinate("1 2 3")


def _write_xml(path, cols, rows, corner, calib="1.23e-05"):
    with open(path, "w") as f:
        f.write(f"""<?xml version="1.0"?>
<GRD>
  <calibration_factor>{calib}</calibration_factor>
  <geo_ref_system>WGS84</geo_ref_system>
  <number_of_azimuth_samples>{rows}</number_of_azimuth_samples>
  <number_of_range_samples>{cols}</number_of_range_samples>
  <coord_first_near>{corner(0, 0)}</coord_first_near>
  <coord_first_far>{corner(0, cols - 1)}</coord_first_far>
  <coord_last_near>{corner(rows - 1, 0)}</coord_last_near>
  <coord_last_far>{corner(rows - 1, cols - 1)}</coord_last_far>
</GRD>
""")


def test_parse_grd_metadata_requires_calibration_factor(tmp_path):
    xml = tmp_path / "x.xml"
    xml.write_text("<GRD><geo_ref_system>WGS84</geo_ref_system></GRD>")
    with pytest.raises(ValueError, match="calibration_factor"):
        parse_grd_metadata(str(xml))


# ------------------------------------------------------------ georef math

def _skewed_grid(lat0=34.0, lon0=-86.0, dlat=-0.0001, dlon=0.0001, rot=0.00002):
    def corner(r, c):
        lat = lat0 + r * dlat + c * rot
        lon = lon0 + c * dlon + r * rot
        return f"[{c},{r},{lat:.10f},{lon:.10f}]"
    return corner


def test_georeference_treats_corners_as_pixel_centres(tmp_path):
    cols, rows = 200, 100
    corner = _skewed_grid()
    xml = tmp_path / "x.xml"
    _write_xml(xml, cols, rows, corner)
    md = parse_grd_metadata(str(xml))

    projref, gt = georeference_from_iceye_xml(md, cols, rows)

    srs = osr.SpatialReference()
    srs.ImportFromWkt(projref)
    assert srs.GetAuthorityCode(None) == "4326"

    def fwd(c, r):
        return gt[0] + c * gt[1] + r * gt[2], gt[3] + c * gt[4] + r * gt[5]

    # Every XML corner is recovered at its pixel CENTRE (col+0.5, row+0.5).
    for (r, c) in [(0, 0), (0, cols - 1), (rows - 1, 0), (rows - 1, cols - 1)]:
        _, _, lat, lon = parse_corner_coordinate(corner(r, c))
        x, y = fwd(c + 0.5, r + 0.5)
        assert x == pytest.approx(lon, abs=1e-9), f"lon at r={r} c={c}"
        assert y == pytest.approx(lat, abs=1e-9), f"lat at r={r} c={c}"


def test_georeference_reports_missing_corners(tmp_path):
    xml = tmp_path / "x.xml"
    xml.write_text("<GRD><calibration_factor>1</calibration_factor>"
                   "<geo_ref_system>WGS84</geo_ref_system></GRD>")
    md = parse_grd_metadata(str(xml))
    with pytest.raises(ValueError, match="coord_first_near"):
        georeference_from_iceye_xml(md, 10, 10)


# ----------------------------------------------------------- sigmaCalib

CACHE = "/tmp/s3_temp"  # hard-coded download cache inside iceye_v2


@pytest.fixture
def staged_grd(monkeypatch):
    """A synthetic un-georeferenced GRD + XML staged in the download cache
    sigmaCalib probes, under a unique stem, so no S3 call is made. Only the
    files this fixture wrote are removed afterwards."""
    monkeypatch.setattr(iceye_v2, "download_s3_file",
                        lambda *a, **k: pytest.fail("download_s3_file must not be called"))

    os.makedirs(CACHE, exist_ok=True)
    stem = f"ICEYE_X48_GRD_SLH_{uuid.uuid4().hex[:7]}_20260513T154816"
    cols, rows = 64, 48
    rng = np.random.default_rng(0)
    dn = rng.integers(50, 4000, size=(rows, cols), dtype=np.uint16)
    dn[:6, :] = 0   # zero-fill border rows
    dn[:, :9] = 0   # zero-fill border cols

    tif = f"{CACHE}/{stem}.tif"
    xml = f"{CACHE}/{stem}.xml"
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(tif, cols, rows, 1, gdal.GDT_UInt16)
    ds.GetRasterBand(1).WriteArray(dn)
    ds = None
    _write_xml(xml, cols, rows, _skewed_grid())

    try:
        yield {
            "tifs": [f"s3://csdap-iceye-delivery/disasters/x/{stem}.tif"],
            "xmls": [f"s3://csdap-iceye-delivery/disasters/x/{stem}.xml"],
            "dn": dn,
        }
    finally:
        for p in (tif, xml):
            if os.path.exists(p):
                os.remove(p)


def test_nodata_sentinel_is_not_zero():
    assert ICEYE_NODATA == -9999.0


def test_sigma_calib_border_is_declared_nodata_and_name_is_canonical(staged_grd, tmp_path):
    out = sigmaCalib(staged_grd["tifs"], staged_grd["xmls"],
                     save_location=str(tmp_path / "out"), filter_size=3)

    assert isinstance(out, str)
    assert os.path.basename(out) == "ICEYE-X48_sigma0-dB_filtered3_2026-05-13T15:48:16Z.tif"

    ds = gdal.Open(out)
    arr = ds.GetRasterBand(1).ReadAsArray()
    gt = ds.GetGeoTransform()
    proj = ds.GetProjection()
    ds = None

    border = staged_grd["dn"] == 0
    assert np.isfinite(arr).all(), "log10(0) leaked -inf into the product"
    assert (arr[border] == ICEYE_NODATA).all(), "zero-fill border is not the declared nodata"
    assert (arr[~border] != ICEYE_NODATA).all(), "valid pixels were overwritten with nodata"
    assert arr[~border].min() > -100 and arr[~border].max() < 100, "dB range is implausible"

    # Un-georeferenced input -> XML fallback engaged.
    assert gt != (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    assert "4326" in proj
