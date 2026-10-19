"""ICEYE georeferencing, memory and download hygiene.

1. SM/ScanSAR GRDs carry no geotransform, only a dense EPSG:4326 GCP grid.
   ``sigmaCalib`` must keep those GCPs and warp north-up with a thin-plate
   spline -- the old 4-corner XML affine was measured 9-18 m off on flat SM
   and 118-406 m off over terrain on ScanSAR. A synthetic GRD whose GCPs come
   from a known NON-affine mapping pins the warped output to the truth, and
   shows the affine would have been worse.
2. Branch order: embedded geotransform > GCPs > XML affine (with a WARNING).
   A rotated geotransform is warped north-up in its own CRS.
3. ``lee_filter`` and the calibration run in float32 (an SLF scene is ~600M
   px) and match a float64 reference.
4. The raw GRD + XML go to ``download_dir``, never ``save_location`` (the
   notebook OUTPUT_DIR, whose every .tif is published); the CLI uses a
   per-run ``download_scratch`` (.clinerules.md rule 61).
5. Vendor listing is matched case-insensitively and ignores the
   ``.sha256``/``.sig`` siblings.
6. End to end: sigmaCalib -> convert_to_cog gives a valid north-up EPSG:4326
   COG with the activation tag and -9999 nodata in the border.
"""

import os
import sys

import numpy as np
import pytest

pytest.importorskip("osgeo.gdal")
pytest.importorskip("scipy")

from osgeo import gdal, osr
from scipy.ndimage import uniform_filter

from iceye import iceye_v2
from iceye.iceye_v2 import (
    ICEYE_NODATA,
    georeference_from_iceye_xml,
    lee_filter,
    parse_grd_metadata,
    resolve_georeference,
    retrieve_iceye_resources,
    sigmaCalib,
    write_georeferenced,
)


COLS, ROWS = 200, 300


def truth(x, y):
    """A smooth, deliberately NON-affine pixel -> (lon, lat) mapping
    (quadratic + cross terms, a few pixels of curvature across the scene),
    standing in for ground-range geometry over terrain. ``x``/``y`` are GDAL
    pixel coordinates (origin at the outer corner of the first pixel)."""
    lon = -87.3 + 1e-4 * x - 2e-5 * y + 5e-8 * (x - 100.0) ** 2 + 2e-9 * x * y
    lat = 30.0 + 1e-5 * x - 1e-4 * y + 3e-8 * (y - 150.0) ** 2 - 1e-9 * x * y
    return lon, lat


def invert_truth(lon, lat):
    """Newton solve truth(x, y) == (lon, lat)."""
    x, y = COLS / 2.0, ROWS / 2.0
    for _ in range(30):
        f0 = np.subtract(truth(x, y), (lon, lat))
        h = 1e-3
        jx = (np.subtract(truth(x + h, y), truth(x, y))) / h
        jy = (np.subtract(truth(x, y + h), truth(x, y))) / h
        dx, dy = np.linalg.solve(np.column_stack([jx, jy]), -f0)
        x, y = x + dx, y + dy
        if abs(dx) + abs(dy) < 1e-9:
            break
    return x, y


def wgs84():
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    return srs.ExportToWkt()


def gcp_grid(nx=9, ny=13):
    # Like the vendor grid: pixel/line run (0, 0)..(cols-1, rows-1), with Z,
    # and each index names a pixel CENTRE (GDAL coordinate index + 0.5).
    return [
        gdal.GCP(*truth(p + 0.5, l + 0.5), 12.0, float(p), float(l))
        for l in np.linspace(0, ROWS - 1, ny)
        for p in np.linspace(0, COLS - 1, nx)
    ]


def xml_corners():
    """XML corners: 1-based pixel-centre indices; XML (i, j) = GDAL (i-.5, j-.5)."""
    md = {}
    for key, (c, r) in {
        "coord_first_near": (1, 1), "coord_first_far": (COLS, 1),
        "coord_last_near": (1, ROWS), "coord_last_far": (COLS, ROWS),
    }.items():
        lon, lat = truth(c - 0.5, r - 0.5)
        md[key] = f"{c} {r} {lat:.12f} {lon:.12f}"
    return md


def sample_points(path):
    """Yield (lon, lat, value) at a strided set of valid output pixel centres."""
    ds = gdal.Open(path)
    gt = ds.GetGeoTransform()
    arr = ds.GetRasterBand(1).ReadAsArray()
    ds = None
    assert gt[2] == 0 and gt[4] == 0, "output is not north-up"
    for r in range(3, arr.shape[0] - 3, 9):
        for c in range(3, arr.shape[1] - 3, 9):
            v = arr[r, c]
            if v == ICEYE_NODATA:
                continue
            yield gt[0] + (c + 0.5) * gt[1], gt[3] + (r + 0.5) * gt[5], float(v)


# ------------------------------------------------------- GCP accuracy

def test_gcp_warp_lands_on_truth_and_beats_the_affine(tmp_path):
    # Through resolve_georeference, so the pixel-centre half-pixel shift is
    # part of what is measured: without it every pixel lands ~0.7 px off.
    georef = resolve_georeference(_grd(tmp_path / "src.tif", gcps=gcp_grid()), xml_corners())
    assert georef["kind"] == "gcps"

    # Encode each source pixel's own centre coordinate as the value: after a
    # bilinear warp, the value at an output location IS the source x (or y)
    # the warp sampled there.
    cc, rr = np.meshgrid(np.arange(COLS) + 0.5, np.arange(ROWS) + 0.5)
    out_x = write_georeferenced(str(tmp_path / "x.tif"), cc.astype(np.float32), georef)
    out_y = write_georeferenced(str(tmp_path / "y.tif"), rr.astype(np.float32), georef)

    ds = gdal.Open(out_x)
    srs = osr.SpatialReference(wkt=ds.GetProjection())
    ds = None
    assert srs.GetAuthorityCode(None) == "4326"

    _, aff_gt = georeference_from_iceye_xml(xml_corners(), COLS, ROWS)
    aff_inv = gdal.InvGeoTransform(aff_gt)

    new_err, aff_err = [], []
    for (lon, lat, vx), (_, _, vy) in zip(sample_points(out_x), sample_points(out_y)):
        x, y = invert_truth(lon, lat)
        # Interior only: stay a few pixels clear of the swath edge.
        if not (3 < x < COLS - 3 and 3 < y < ROWS - 3):
            continue
        new_err.append(np.hypot(vx - x, vy - y))
        ax, ay = gdal.ApplyGeoTransform(aff_inv, lon, lat)
        aff_err.append(np.hypot(ax - x, ay - y))

    assert len(new_err) > 200
    new_err, aff_err = np.array(new_err), np.array(aff_err)
    print(f"\nTPS warp error (px): max {new_err.max():.3f} mean {new_err.mean():.3f}")
    print(f"XML affine error (px): max {aff_err.max():.3f} mean {aff_err.mean():.3f}")

    assert new_err.max() < 0.25, "GCP warp misplaces interior pixels"
    assert aff_err.max() > 2.0, "synthetic mapping is not non-affine enough to test"
    assert aff_err.mean() > 10 * new_err.mean()


# ------------------------------------------------------- branch selection

def _grd(path, gt=None, proj=None, gcps=None, dtype=gdal.GDT_UInt16, arr=None):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), COLS, ROWS, 1, dtype)
    if gt is not None:
        ds.SetGeoTransform(gt)
        ds.SetProjection(proj)
    if gcps is not None:
        ds.SetGCPs(gcps, wgs84())
    if arr is not None:
        ds.GetRasterBand(1).WriteArray(arr)
    ds = None
    return gdal.Open(str(path))


def test_branch_geotransform_wins(tmp_path):
    gt = (-87.3, 1e-4, 0.0, 30.0, 0.0, -1e-4)
    g = resolve_georeference(_grd(tmp_path / "a.tif", gt, wgs84()), xml_corners())
    assert g["kind"] == "geotransform" and g["geotransform"] == pytest.approx(gt)


def test_branch_gcps_when_no_geotransform(tmp_path):
    g = resolve_georeference(_grd(tmp_path / "b.tif", gcps=gcp_grid()), xml_corners())
    assert g["kind"] == "gcps" and len(g["gcps"]) == 9 * 13


@pytest.mark.parametrize("area_or_point, written", [("Area", 0.0), ("Point", -0.5)])
def test_gcp_half_pixel_shift_is_applied_exactly_once(tmp_path, area_or_point, written):
    # Every case stores a raw tiepoint of 0 -- the vendor's "centre of the
    # first pixel" -- which must resolve to GDAL pixel 0.5. Under Area we add
    # the half pixel; under PixelIsPoint GDAL's reader already has, so we must
    # not. (GDAL's writer also adds half a pixel under Point, hence -0.5.)
    path = str(tmp_path / f"{area_or_point}.tif")
    ds = gdal.GetDriverByName("GTiff").Create(path, COLS, ROWS, 1, gdal.GDT_UInt16)
    ds.SetMetadataItem("AREA_OR_POINT", area_or_point)
    ds.SetGCPs([gdal.GCP(-87.0, 30.0, 0.0, written, written),
                gdal.GCP(-87.1, 30.0, 0.0, COLS - 1.0 + written, written),
                gdal.GCP(-87.0, 30.1, 0.0, written, ROWS - 1.0 + written)], wgs84())
    ds = None
    g = resolve_georeference(gdal.Open(path), xml_corners())
    assert g["kind"] == "gcps"
    first = g["gcps"][0]
    assert (first.GCPPixel, first.GCPLine) == pytest.approx((0.5, 0.5))


def test_branch_xml_fallback_warns(tmp_path, capsys):
    g = resolve_georeference(_grd(tmp_path / "c.tif"), xml_corners())
    assert g["kind"] == "xml"
    assert "APPROXIMATE" in capsys.readouterr().out


def test_rotated_geotransform_is_warped_north_up_in_its_own_crs(tmp_path):
    utm = osr.SpatialReference()
    utm.ImportFromEPSG(32616)
    gt = (500000.0, 10.0, 2.0, 3300000.0, 2.0, -10.0)
    georef = {"kind": "geotransform", "projection": utm.ExportToWkt(),
              "geotransform": gt, "gcps": None}
    cc, _ = np.meshgrid(np.arange(COLS) + 0.5, np.arange(ROWS) + 0.5)
    out = write_georeferenced(str(tmp_path / "rot.tif"), cc.astype(np.float32), georef)

    ds = gdal.Open(out)
    assert osr.SpatialReference(wkt=ds.GetProjection()).GetAuthorityCode(None) == "32616"
    ds = None
    inv = gdal.InvGeoTransform(gt)
    checked = 0
    for x, y, v in sample_points(out):
        sx, sy = gdal.ApplyGeoTransform(inv, x, y)
        if 3 < sx < COLS - 3 and 3 < sy < ROWS - 3:
            assert v == pytest.approx(sx, abs=0.05)
            checked += 1
    assert checked > 100


def test_xml_fallback_fits_parsed_indices_not_assumed_corners():
    """Corners listed in a different order still give the same transform,
    because the fit uses each coord's own (col, row)."""
    md = xml_corners()
    swapped = dict(md, coord_first_near=md["coord_last_far"],
                   coord_last_far=md["coord_first_near"])
    _, gt1 = georeference_from_iceye_xml(md, COLS, ROWS)
    _, gt2 = georeference_from_iceye_xml(swapped, COLS, ROWS)
    assert gt1 == pytest.approx(gt2, abs=1e-12)


# ------------------------------------------------------------ float32 / Lee

def _lee_float64_reference(img, size):
    """The pre-float32 implementation, verbatim, as an oracle."""
    valid = np.isfinite(img)
    v = valid.astype(np.float64)
    filled = np.where(valid, img, 0.0).astype(np.float64)
    frac = uniform_filter(v, size, mode="constant")
    safe = frac > 0
    local_mean = np.zeros_like(filled)
    local_mean[safe] = uniform_filter(filled, size, mode="constant")[safe] / frac[safe]
    local_sqr = np.zeros_like(filled)
    local_sqr[safe] = uniform_filter(filled ** 2, size, mode="constant")[safe] / frac[safe]
    local_var = np.clip(local_sqr - local_mean ** 2, 0.0, None)
    overall_var = img[valid].var()
    weights = local_var / (local_var + overall_var + 1e-12)
    out = local_mean + weights * (filled - local_mean)
    return np.where(valid, out, np.nan)


@pytest.mark.parametrize("size", [3, 5, 7])
def test_lee_filter_is_float32_and_matches_float64_reference(size):
    rng = np.random.default_rng(1)
    img = rng.integers(1, 65535, size=(120, 90)).astype(np.float32)
    img[:10] = np.nan
    img[:, -7:] = np.nan
    out = lee_filter(img, size)
    ref = _lee_float64_reference(img.astype(np.float64), size)
    assert out.dtype == np.float32
    assert np.array_equal(np.isnan(out), np.isnan(ref))
    ok = np.isfinite(ref)
    np.testing.assert_allclose(out[ok], ref[ok], rtol=2e-4)


# --------------------------------------------- downloads + vendor listing

def _stage_vendor(tmp_path, dtype=gdal.GDT_UInt16):
    """A GCP-only GRD + XML in a fake 'bucket' dir, and a download_s3_file
    stub that copies them to whatever directory it is given."""
    stem = "ICEYE_X48_GRD_SM_1234567_20260513T154816"
    bucket = tmp_path / "bucket"
    bucket.mkdir()
    rng = np.random.default_rng(0)
    dn = rng.integers(50, 4000, size=(ROWS, COLS)).astype(
        np.float32 if dtype == gdal.GDT_Float32 else np.uint16)
    dn[:12] = 0
    dn[:, :15] = 0
    _grd(bucket / f"{stem}.tif", gcps=gcp_grid(), dtype=dtype, arr=dn)
    corners = xml_corners()
    (bucket / f"{stem}.xml").write_text(
        "<GRD><calibration_factor>1.23e-05</calibration_factor>"
        "<geo_ref_system>WGS84</geo_ref_system>"
        + "".join(f"<{k}>{v}</{k}>" for k, v in corners.items()) + "</GRD>")

    calls = []

    def fake_download(s3path, save_location="/tmp/s3_temp"):
        name = s3path.split("/")[-1]
        os.makedirs(save_location, exist_ok=True)
        dst = os.path.join(save_location, iceye_v2.local_tif_basename(s3path))
        with open(bucket / name, "rb") as src, open(dst, "wb") as out:
            out.write(src.read())
        calls.append(save_location)
        return dst

    return {
        "tifs": [f"s3://csdap-iceye-delivery/disasters/ev/{stem}.tif"],
        "xmls": [f"s3://csdap-iceye-delivery/disasters/ev/{stem}.xml"],
        "download": fake_download, "calls": calls, "dn": dn,
    }


def test_raw_downloads_land_in_download_dir_not_output(tmp_path, monkeypatch):
    v = _stage_vendor(tmp_path)
    monkeypatch.setattr(iceye_v2, "download_s3_file", v["download"])
    out_dir, dl_dir = tmp_path / "out", tmp_path / "dl"

    out = sigmaCalib(v["tifs"], v["xmls"], save_location=str(out_dir),
                     filter_size=5, download_dir=str(dl_dir))

    assert v["calls"] == [str(dl_dir)] * 2
    produced = [os.path.join(d, f) for d, _, fs in os.walk(out_dir) for f in fs]
    assert produced == [out], f"only the product may be in save_location: {produced}"
    assert sorted(os.listdir(dl_dir)) == sorted(
        p.split("/")[-1] for p in v["tifs"] + v["xmls"])


def test_cli_downloads_into_a_scratch_dir_it_deletes(tmp_path, monkeypatch):
    from iceye import process_iceye

    v = _stage_vendor(tmp_path)
    monkeypatch.setattr(iceye_v2, "download_s3_file", v["download"])
    monkeypatch.setattr(process_iceye, "retrieve_iceye_resources",
                        lambda **k: (v["xmls"], v["tifs"]))
    monkeypatch.setattr(process_iceye, "save_cog_png", lambda **k: None)
    out_dir = tmp_path / "out"
    meta = tmp_path / "meta.json"
    meta.write_text('{"ACTIVATION_EVENT": "202605_Flood_AL"}')
    monkeypatch.setattr(sys, "argv", ["process_iceye", "--date", "2026-05-13 15:48:16",
                                      "--output", str(out_dir), "--metadata-json", str(meta)])

    process_iceye.main()

    scratch = set(v["calls"])
    assert len(scratch) == 1
    (dl,) = scratch
    assert not os.path.realpath(dl).startswith(os.path.realpath(str(out_dir)))
    assert not os.path.exists(dl), "download scratch dir outlived the run"
    tifs = [f for _, _, fs in os.walk(out_dir) for f in fs if f.lower().endswith(".tif")]
    assert tifs == ["ICEYE-X48_sigma0-dB_filtered5_2026-05-13T15:48:16Z.tif"]


def test_vendor_listing_is_case_insensitive_and_skips_checksums(monkeypatch):
    stem = "disasters/ev/ICEYE_X48_GRD_SM_1234567_20260513T154816"
    other = "disasters/ev/ICEYE_X48_GRD_SM_7654321_20250101T000000"
    keys = [f"{stem}.TIF", f"{stem}.tif.sha256", f"{stem}.TIF.sig",
            f"{stem}.xml", f"{stem}.xml.sha256", f"{stem}.xml.sig",
            f"{other}.TIF", f"{other}.xml"]
    monkeypatch.setattr(iceye_v2, "retrieve_s3_file_list", lambda b, p: keys)

    xmls, tifs = retrieve_iceye_resources("2026-05-14 00:00:00")

    assert tifs == [f"s3://csdap-iceye-delivery/{stem}.TIF"]
    assert xmls == [f"s3://csdap-iceye-delivery/{stem}.xml"]


# ----------------------------------------------------------- end to end

@pytest.mark.parametrize("dtype", [gdal.GDT_UInt16, gdal.GDT_Float32],
                         ids=["uint16-SM", "float32-SLF"])
def test_end_to_end_gcp_grd_to_valid_tagged_cog(tmp_path, monkeypatch, dtype):
    rio_cogeo = pytest.importorskip("rio_cogeo")
    rasterio = pytest.importorskip("rasterio")
    from shared_utils.cog_utils import convert_to_cog

    v = _stage_vendor(tmp_path, dtype)
    monkeypatch.setattr(iceye_v2, "download_s3_file", v["download"])
    out = sigmaCalib(v["tifs"], v["xmls"], save_location=str(tmp_path / "out"),
                     filter_size=5, download_dir=str(tmp_path / "dl"))
    assert os.path.basename(os.path.dirname(out)) == "Backscatter"

    ds = gdal.Open(out)
    assert ds.GetGCPCount() == 0, "output must carry a geotransform, not GCPs"
    ds = None

    cog = convert_to_cog(out, nodata=ICEYE_NODATA, dst_crs=None,
                         compression="ZSTD", compression_level=9,
                         metadata={"ACTIVATION_EVENT": "202605_Flood_AL",
                                   "SOURCE": "ICEYE"})

    is_valid, errors, _ = rio_cogeo.cog_validate(cog)
    assert is_valid, errors
    with rasterio.open(cog) as src:
        assert src.crs.to_epsg() == 4326
        assert src.transform.b == 0 and src.transform.d == 0
        assert src.nodata == ICEYE_NODATA
        assert src.tags().get("ACTIVATION_EVENT") == "202605_Flood_AL"
        arr = src.read(1)
    # The swath is skewed in a north-up frame: its corners are outside it.
    assert arr[0, 0] == ICEYE_NODATA and arr[-1, -1] == ICEYE_NODATA
    data = arr[arr != ICEYE_NODATA]
    assert data.size and np.isfinite(data).all()
    assert data.min() > -100 and data.max() < 100
