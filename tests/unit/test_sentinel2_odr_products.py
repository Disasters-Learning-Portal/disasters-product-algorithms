"""Pins what PR #190 added to the Sentinel-2 STAC ("ODR") pipeline.

The PR added a cloud-mask product, RGBA colour composites and a cloud-mask
union for mosaics -- about 460 lines -- and no tests. None of it was exercised
by the existing suites, so each class below either pins new behaviour for the
first time or reproduces a defect found reviewing it:

  * `generate_composite` inherited the SOURCE asset's driver, so an L1C
    composite (JPEG-2000 assets) was written as a lossy JP2 named `.tif`.
  * The new alpha band was derived from "all three stretched bands are 0", and
    the stretch maps every valid value at or below its floor to 0 -- so dark
    valid pixels (clear water, shadow) were published transparent.
  * The dominant CRS of a merge came from a set, i.e. from the hash seed.
  * A second water-threshold formula (median + MAD) had been added on the one
    code path no CLI, DPS or notebook run takes.
  * The national CDL download URL returns 404 for every year (it moved on the
    NASS site), so a CONUS water-extent run with a cold cache could not start.
    The new location then refused most requests for the whole file, and the
    download was a single attempt at the whole file.

Reads are served from local GeoTIFFs; nothing here touches the network.
"""

import datetime as dt
import os
import zipfile

import numpy as np
import pytest

rio = pytest.importorskip("rasterio")
pytest.importorskip("pystac_client")
requests = pytest.importorskip("requests")
from rasterio.session import DummySession  # noqa: E402
from rasterio.transform import from_origin  # noqa: E402

from sentinel2 import process_sentinel2_odr as proc  # noqa: E402
from sentinel2 import sentinel2_odr_functions as odr  # noqa: E402

CATALOG = os.path.join(os.path.dirname(odr.__file__), "algorithms-sentinel2.json")
TRANSFORM = from_origin(500000, 4700000, 10, 10)


class FakeItem:
    """A sentinel-2-c1-l2a item (processing baseline 05.11), as far as needed."""

    def __init__(self, assets, item_id="S2C_T15TVG_20250813T170704_L2A"):
        self.id = item_id
        self.datetime = dt.datetime(2025, 8, 13, 17, 12, 26, tzinfo=dt.timezone.utc)
        self.properties = {
            "platform": "sentinel-2c",
            "s2:datatake_id": "GS2C_20250813T170704_004999_N05.11",
            "s2:product_uri": "S2C_MSIL2A_20250813T170704_N0511_R069_T15TVG_X.SAFE",
        }
        self.assets = assets


@pytest.fixture(autouse=True)
def no_aws(monkeypatch):
    monkeypatch.setattr(odr, "_session", lambda unsigned=False: DummySession())


def write_raster(path, array, dtype, crs="EPSG:32615", transform=TRANSFORM, nodata=None):
    arr = np.asarray(array, dtype=dtype)
    if arr.ndim == 2:
        arr = arr[np.newaxis]
    profile = dict(driver="GTiff", count=arr.shape[0], dtype=dtype, height=arr.shape[1],
                   width=arr.shape[2], crs=crs, transform=transform)
    if nodata is not None:
        profile["nodata"] = nodata
    with rio.open(path, "w", **profile) as dst:
        dst.write(arr)
    return str(path)


# ---------------------------------------------------------------------------
# Colour composites
# ---------------------------------------------------------------------------

def run_composite(tmp_path, monkeypatch, dn_by_band, cloud=None, source_driver="GTiff",
                  item_id="S2C_T15TVG_20250813T170704_L2A"):
    """The real generate_composite + finalize_composite_rgba on given raw DN.

    `dn_by_band` maps asset name -> one row of raw DN. `source_driver` is the
    driver the source profile claims, as read_algorithm_band would return it.
    """
    width = len(next(iter(dn_by_band.values())))
    profile = dict(driver=source_driver, count=1, dtype="uint16", height=1, width=width,
                   crs="EPSG:32615", transform=TRANSFORM)

    def fake_read(asset, **kwargs):
        return np.asarray([dn_by_band[asset]], dtype=np.uint16).copy(), dict(profile)

    monkeypatch.setattr(odr, "read_algorithm_band", fake_read)
    if cloud is not None:
        monkeypatch.setattr(
            odr, "get_cloud_mask",
            lambda item, out_shape, **kw: np.asarray([cloud], dtype=bool))

    item = FakeItem({name: name for name in dn_by_band}, item_id=item_id)
    algorithm = odr.get_algorithm("composite", "true_color", CATALOG)
    out_dir = tmp_path / "out"
    out = odr.generate_composite(item=item, algorithm=algorithm, algorithm_name="true_color",
                                 output_dir=str(out_dir), cloud_mask=cloud is not None)
    with rio.open(out) as src:
        written_driver = src.driver
    odr.finalize_composite_rgba(
        out, [odr.cloud_mask_sidecar_path(out)] if cloud is not None else None)
    with rio.open(out) as src:
        meta = dict(driver=src.driver, count=src.count, nodata=src.nodata,
                    colorinterp=src.colorinterp)
        return src.read()[:, 0, :], meta, written_driver, sorted(os.listdir(out_dir))


class TestComposite:

    def test_the_intermediate_is_a_geotiff_whatever_the_source_was(self, tmp_path, monkeypatch):
        """sentinel-2-l1c assets are JPEG-2000. Inheriting that driver wrote a
        lossy quality-25 JP2 called `.tif`: 88% of valid pixels changed, the
        ringing made off-swath pixels non-zero, and a `.aux.xml` declaring
        nodata=0 was left next to the product."""
        bright = [3000, 3000]
        rgba, meta, written_driver, files = run_composite(
            tmp_path, monkeypatch, {"red": bright, "green": bright, "blue": bright},
            source_driver="JP2OpenJPEG")
        assert written_driver == "GTiff"
        assert meta["driver"] == "GTiff"
        assert not [f for f in files if f.endswith(".aux.xml")]

    def test_output_is_rgba_with_no_nodata_tag(self, tmp_path, monkeypatch):
        """An alpha band is the single statement of validity; a scalar nodata
        next to it would shadow it (CLAUDE.md, the nodata three-way contract)."""
        bright = [3000, 0]
        rgba, meta, _, _ = run_composite(
            tmp_path, monkeypatch, {"red": bright, "green": bright, "blue": bright})
        assert meta["count"] == 4 and meta["nodata"] is None
        assert meta["colorinterp"][3] == rio.enums.ColorInterp.alpha
        assert proc.COMPOSITE_NODATA is False

    def test_fill_is_transparent(self, tmp_path, monkeypatch):
        bands = {"red": [3000, 0], "green": [3200, 0], "blue": [2800, 0]}
        rgba, _, _, _ = run_composite(tmp_path, monkeypatch, bands)
        assert rgba[3].tolist() == [255, 0]
        assert rgba[:3, 1].tolist() == [0, 0, 0]

    def test_a_valid_dark_pixel_stays_opaque(self, tmp_path, monkeypatch):
        """Raw DN 600 is under the stretch floor (750), so it stretches to 0 --
        the value the alpha band reads as "no data". On an offset-0 L1C scene
        that is every pixel with reflectance <= 0.075: clear water, shadow,
        dark forest. They were published transparent."""
        dark = [600, 700]
        rgba, _, _, _ = run_composite(
            tmp_path, monkeypatch, {"red": dark, "green": dark, "blue": dark})
        assert rgba[3].tolist() == [255, 255]
        assert rgba[:3].min() == 1 and rgba[:3].max() == 1

    def test_a_dark_l1c_pixel_stays_opaque_after_the_rayleigh_correction(
            self, tmp_path, monkeypatch):
        """L1C subtracts an atmospheric correction before the stretch, which
        takes dark water to zero or below. What was observed must be read off
        the raw band, before that subtraction."""
        monkeypatch.setattr(odr, "get_rayleigh_correction", lambda item, name: 0.08)
        dn = [600, 0]                              # 600 - 800 < 0; 0 is fill
        rgba, _, _, _ = run_composite(
            tmp_path, monkeypatch, {"red": dn, "green": dn, "blue": dn},
            item_id="S2C_T15TVG_20250813T170704_L1C")
        assert rgba[:, 0].tolist() == [1, 1, 1, 255]
        assert rgba[:, 1].tolist() == [0, 0, 0, 0]

    def test_a_single_dark_band_is_not_a_hole_for_the_merge(self, tmp_path, monkeypatch):
        """The mosaic merges per band with nodata 0, so one band at 0 was
        filled from the neighbouring tile -- a colour that never existed."""
        bands = {"red": [600], "green": [3200], "blue": [2800]}
        rgba, _, _, _ = run_composite(tmp_path, monkeypatch, bands)
        assert rgba[0, 0] == 1 and rgba[1, 0] > 1 and rgba[2, 0] > 1

    def test_masked_cloud_is_opaque_gray_and_fill_stays_transparent(self, tmp_path, monkeypatch):
        bands = {"red": [6000, 3000, 0], "green": [6000, 3200, 0], "blue": [6000, 2800, 0]}
        rgba, _, _, files = run_composite(
            tmp_path, monkeypatch, bands, cloud=[True, False, False])
        assert rgba[:, 0].tolist() == [odr.CLOUD_GRAY] * 3 + [255]
        assert rgba[3].tolist() == [255, 255, 0]
        assert rgba[0, 1] != odr.CLOUD_GRAY

    def test_only_three_band_input_is_accepted(self, tmp_path):
        path = write_raster(tmp_path / "one.tif", np.ones((1, 4, 4)), "uint8")
        with pytest.raises(ValueError, match="3-band"):
            odr.finalize_composite_rgba(path)


# ---------------------------------------------------------------------------
# The cloud-mask product
# ---------------------------------------------------------------------------

# Every SCL class: 0 no data, 1 saturated, 2 dark, 3 cloud shadow, 4 vegetation,
# 5 bare, 6 water, 7 unclassified, 8 cloud medium, 9 cloud high, 10 thin
# cirrus, 11 snow.
SCL = np.arange(12, dtype=np.uint8).reshape(1, 12)


def scl_item(tmp_path, scl=SCL, **kwargs):
    path = write_raster(tmp_path / "scl.tif", scl, "uint8",
                        transform=from_origin(500000, 4700000, 20, 20))
    return FakeItem({"scl": {"href": path}}, **kwargs)


class TestCloudMaskProduct:

    def test_classes_dtype_and_nodata(self, tmp_path):
        """1 = cloud shadow, cloud (medium / high probability) or thin cirrus --
        the classes --cloud-mask masks -- and 0 everything else. 0 is also the
        nodata value, so a viewer draws only the cloud; clear sky and off-swath
        cannot be told apart in this product. (The legacy `.SAFE` cloud mask,
        published to the same CloudMask/ folder, is 0 clear / 1 cloud / 999
        nodata -- see docs/PRODUCT_S3_LAYOUT.md.)"""
        out = odr.generate_cloud_mask(item=scl_item(tmp_path), output_dir=str(tmp_path / "out"))
        with rio.open(out) as src:
            assert src.dtypes[0] == "uint8" and src.count == 1
            assert src.nodata == odr.CLOUD_MASK_NODATA == 0
            assert src.res == (20.0, 20.0)
            assert src.read(1)[0].tolist() == [0, 0, 0, 1, 0, 0, 0, 0, 1, 1, 1, 0]

    def test_it_uses_the_same_classes_as_the_cloud_mask_toggle(self, tmp_path):
        item = scl_item(tmp_path)
        toggle = odr.get_cloud_mask(item, out_shape=SCL.shape)
        out = odr.generate_cloud_mask(item=item, output_dir=str(tmp_path / "out"))
        with rio.open(out) as src:
            assert (src.read(1) == 1).tolist() == toggle.tolist()

    def test_filename_token_and_no_masked_suffix(self, tmp_path):
        out = odr.generate_cloud_mask(item=scl_item(tmp_path), output_dir=str(tmp_path / "out"))
        assert os.path.basename(out) == "S2C_MSIL2A_cloudMask_T15TVG_2025-08-13T17:12:26Z.tif"

    def test_no_scl_asset_is_an_error(self, tmp_path):
        """L1C has no Scene Classification Layer."""
        with pytest.raises(KeyError, match="scl"):
            odr.generate_cloud_mask(item=FakeItem({}), output_dir=str(tmp_path))

    def test_finalize_attaches_the_colour_table_and_keeps_the_data(self, tmp_path):
        out = odr.generate_cloud_mask(item=scl_item(tmp_path), output_dir=str(tmp_path / "out"))
        with rio.open(out) as src:
            before = src.read(1)
        odr.finalize_cloud_mask(out)
        with rio.open(out) as src:
            assert (src.read(1) == before).all() and src.nodata == 0
            colormap = src.colormap(1)
        assert colormap[1] == (odr.CLOUD_GRAY,) * 3 + (255,)
        assert colormap[0][3] == 0

    def test_cli_nodata_is_the_number_zero_not_false(self):
        """`False` means "declare no nodata" to convert_to_cog, and a bare
        1-band uint8 raster with no explicit nodata has it stripped."""
        assert proc.CLOUD_MASK_NODATA == 0 and proc.CLOUD_MASK_NODATA is not False


# ---------------------------------------------------------------------------
# Keeping every tile's cloud mask through a merge
# ---------------------------------------------------------------------------

class TestCloudUnion:

    def _tile(self, tmp_path, name, x0, value, cloud_cols):
        profile = dict(driver="GTiff", count=1, dtype="float32", height=2, width=6,
                       nodata=-9999, crs="EPSG:32615",
                       transform=from_origin(500000 + x0 * 10, 4700000, 10, 10))
        path = str(tmp_path / name)
        data = np.full((2, 6), value, np.float32)
        mask = np.zeros((2, 6), bool)
        mask[:, cloud_cols] = True
        data[mask] = -9999                       # a masked index stores cloud as nodata
        with rio.open(path, "w", **profile) as dst:
            dst.write(data, 1)
        odr.write_cloud_mask_sidecar(path, mask, profile)
        return path

    def test_a_cloud_in_one_tile_is_not_filled_from_its_neighbour(self, tmp_path):
        """Cloud is stored as plain nodata, which a first-wins merge treats as
        a hole and fills from the overlapping tile."""
        west = self._tile(tmp_path, "west.tif", 0, 1.0, cloud_cols=[4, 5])
        east = self._tile(tmp_path, "east.tif", 4, 2.0, cloud_cols=[])     # overlaps cols 4-5
        merged = str(tmp_path / "merged.tif")
        odr.merge_products([west, east], merged)

        with rio.open(merged) as src:
            assert src.read(1)[0].tolist() == [1, 1, 1, 1, 2, 2, 2, 2, 2, 2]   # filled

        odr.apply_cloud_union(merged, [odr.cloud_mask_sidecar_path(p) for p in (west, east)])
        with rio.open(merged) as src:
            assert src.read(1)[0].tolist() == [1, 1, 1, 1, -9999, -9999, 2, 2, 2, 2]
        assert not os.path.exists(merged + ".cloudunion.tmp.tif")

    def test_sidecar_sits_beside_the_product_on_its_grid(self, tmp_path):
        tile = self._tile(tmp_path, "t.tif", 0, 1.0, cloud_cols=[0])
        sidecar = odr.cloud_mask_sidecar_path(tile)
        assert sidecar == str(tmp_path / "_cloud_masks" / "t.tif")
        with rio.open(tile) as product, rio.open(sidecar) as mask:
            assert (mask.transform, mask.crs, mask.shape) == (
                product.transform, product.crs, product.shape)

    def test_a_product_without_nodata_cannot_be_stamped(self, tmp_path):
        path = write_raster(tmp_path / "m.tif", np.ones((2, 2)), "float32")
        mask = write_raster(tmp_path / "c.tif", np.zeros((2, 2)), "uint8")
        with pytest.raises(ValueError, match="no nodata"):
            odr.apply_cloud_union(path, [mask])


# ---------------------------------------------------------------------------
# Merge plumbing
# ---------------------------------------------------------------------------

class TestMergeProducts:

    def _two_zone_tiles(self, tmp_path):
        # One tile each side of the UTM 15/16 boundary: a 1-1 tie.
        a = write_raster(tmp_path / "a.tif", np.full((20, 20), 1.0), "float32",
                         crs="EPSG:32615", transform=from_origin(760000, 4700000, 10, 10),
                         nodata=-9999)
        b = write_raster(tmp_path / "b.tif", np.full((20, 20), 2.0), "float32",
                         crs="EPSG:32616", transform=from_origin(240000, 4700000, 10, 10),
                         nodata=-9999)
        return a, b

    def test_a_crs_tie_is_not_settled_by_set_order(self, tmp_path):
        """`max(set(crs_list), key=crs_list.count)` returned whichever CRS the
        set yielded first, and set order follows the hash seed: the same job
        came out in one UTM zone or the other from run to run. A tie now goes
        to the first CRS in WKT order. Two input orders inside one process see
        the same set order, so the pair of zones used here is chosen as one the
        old rule gets wrong in THIS process, whatever its seed."""
        for zone in range(1, 60):
            a = write_raster(tmp_path / f"a{zone}.tif", np.ones((20, 20)), "float32",
                             crs=f"EPSG:{32600 + zone}", nodata=-9999,
                             transform=from_origin(760000, 4700000, 10, 10))
            b = write_raster(tmp_path / f"b{zone}.tif", np.ones((20, 20)), "float32",
                             crs=f"EPSG:{32601 + zone}", nodata=-9999,
                             transform=from_origin(240000, 4700000, 10, 10))
            crs_list = []
            for path in (a, b):
                with rio.open(path) as src:
                    crs_list.append(src.crs)
            expected = min(crs_list, key=lambda crs: crs.to_wkt())
            if max(set(crs_list), key=crs_list.count) != expected:
                break
        else:
            pytest.skip("set order matched WKT order for every zone pair in this process")

        for order in ([a, b], [b, a]):
            with rio.open(odr.merge_products(order, str(tmp_path / "merged.tif"))) as merged:
                assert merged.crs == expected

    def test_inputs_without_a_crs_still_merge(self, tmp_path):
        a = write_raster(tmp_path / "a.tif", np.ones((4, 4)), "float32", crs=None, nodata=-9999)
        b = write_raster(tmp_path / "b.tif", np.ones((4, 4)), "float32", crs=None, nodata=-9999,
                         transform=from_origin(500040, 4700000, 10, 10))
        with rio.open(odr.merge_products([a, b], str(tmp_path / "merged.tif"))) as merged:
            assert merged.crs is None and merged.width == 8

    def test_priority_value_matches_the_water_extent_merge_method(self, tmp_path):
        def tif(name, row):
            return write_raster(tmp_path / name, np.array([row]), "uint8", nodata=0)

        clear, cloud = tif("clear.tif", [4, 1, 0, 2]), tif("cloud.tif", [5, 1, 3, 0])
        for order in ([clear, cloud], [cloud, clear]):
            by_value = odr.merge_products(order, str(tmp_path / "v.tif"), priority_value=5)
            with rio.open(by_value) as src:
                got = src.read(1)[0].tolist()
            by_method = odr.merge_products(order, str(tmp_path / "m.tif"),
                                           method=odr.water_extent_merge_method)
            with rio.open(by_method) as src:
                assert src.read(1)[0].tolist() == got == [5, 1, 3, 2]


# ---------------------------------------------------------------------------
# The cloud-mask product through the real main()
# ---------------------------------------------------------------------------

class TestCloudMaskCli:

    def _run(self, tmp_path, monkeypatch, *extra):
        import sys

        west = scl_item(tmp_path, scl=[[9, 4, 4, 4]], item_id="S2C_T15TVG_20250813T170704_L2A")
        (tmp_path / "e").mkdir()
        east = scl_item(tmp_path / "e", scl=[[4, 4, 4, 3]],
                        item_id="S2C_T15TVF_20250813T170704_L2A")
        monkeypatch.setattr(proc, "search_sentinel2", lambda **kw: [west, east])
        monkeypatch.setattr(proc, "convert_to_cog", lambda outfile, **kw: outfile)
        out = tmp_path / "out"
        monkeypatch.setattr(sys, "argv", [
            "process_sentinel2_odr", "--product", "cloud_mask", "--level", "2",
            "--start-date", "2025-08-13", "--end-date", "2025-08-13",
            "--bbox", "-93.7", "42.0", "-93.5", "42.1", "--output", str(out), *extra])
        proc.main()
        return out

    def test_level_1_is_rejected_before_the_search(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setattr(proc, "search_sentinel2",
                            lambda **kw: pytest.fail("searched for an L1C cloud mask"))
        monkeypatch.setattr(sys, "argv", [
            "process_sentinel2_odr", "--product", "cloud_mask", "--level", "1",
            "--start-date", "2025-08-13", "--end-date", "2025-08-13",
            "--bbox", "-93.7", "42.0", "-93.5", "42.1", "--output", str(tmp_path)])
        with pytest.raises(ValueError, match="requires Sentinel-2 L2A"):
            proc.main()

    def test_per_tile_products_carry_the_colour_table(self, tmp_path, monkeypatch):
        out = self._run(tmp_path, monkeypatch)
        names = sorted(p.name for p in out.glob("*.tif"))
        assert names == ["S2C_MSIL2A_cloudMask_T15TVF_2025-08-13T17:12:26Z.tif",
                         "S2C_MSIL2A_cloudMask_T15TVG_2025-08-13T17:12:26Z.tif"]
        with rio.open(out / names[0]) as src:
            assert src.colormap(1)[1] == (odr.CLOUD_GRAY,) * 3 + (255,)

    def test_merge_keeps_cloud_from_every_tile_and_never_says_masked(self, tmp_path, monkeypatch):
        """Both tiles cover the same four pixels here, each with one cloudy
        pixel, and the mosaic keeps both. (0 is the product's nodata, so on
        this 0/1 mask a first-valid merge would do the same.) And
        `--cloud-mask` on the mask itself must not add `_masked` to its
        name."""
        out = self._run(tmp_path, monkeypatch, "--merge", "--cloud-mask")
        names = sorted(p.name for p in out.glob("*.tif"))
        assert names == ["S2C_MSIL2A_cloudMask_R069_merged_2025-08-13_day.tif"]
        with rio.open(out / names[0]) as src:
            assert src.read(1)[0].tolist() == [1, 0, 0, 1]
            assert src.nodata == 0
            assert src.colormap(1)[1] == (odr.CLOUD_GRAY,) * 3 + (255,)
        assert not (out / "_cloud_masks").exists()


# ---------------------------------------------------------------------------
# The national CDL download
# ---------------------------------------------------------------------------

CDL_URL = ("https://www.nass.usda.gov/sites/default/files/"
           "Research_and_Science/Cropland/Release/datasets/2025_30m_cdls.zip")


class _Response:
    def __init__(self, status, body=b"", headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error", response=self)

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self._body), 7):       # several chunks per part
            yield self._body[start:start + 7]


class FakeServer:
    """Answers range requests for `payload`. `faults` maps a 1-based request
    number (or "all") to what goes wrong with that request."""

    def __init__(self, payload, faults=None):
        self.payload, self.faults = payload, faults or {}
        self.ranges, self.urls = [], []

    def get(self, url, headers=None, stream=False, timeout=None):
        self.urls.append(url)
        self.ranges.append(headers["Range"])
        fault = self.faults.get(len(self.ranges), self.faults.get("all"))
        if fault == "reset":
            raise requests.exceptions.ConnectionError("reset by peer")
        if fault == "whole":                              # the range is ignored
            return _Response(200, self.payload)
        if isinstance(fault, int):
            return _Response(fault)
        first, last = map(int, headers["Range"][len("bytes="):].split("-"))
        body = self.payload[first:last + 1]
        if fault == "short":
            body = body[:-1]
        total = len(self.payload) + (1 if fault == "resized" else 0)
        return _Response(206, body, {
            "Content-Range": f"bytes {first}-{first + len(body) - 1}/{total}"})


@pytest.fixture
def serve(monkeypatch):
    """Install a FakeServer, with parts of 10 bytes and no real waiting."""
    def install(payload, faults=None, part_bytes=10):
        server = FakeServer(payload, faults)
        server.waits = []
        monkeypatch.setattr(odr.requests, "get", server.get)
        monkeypatch.setattr(odr.time, "sleep", server.waits.append)
        monkeypatch.setattr(odr, "CDL_PART_BYTES", part_bytes)
        return server
    return install


PAYLOAD = bytes(range(35))
PARTS = ["bytes=0-9", "bytes=10-19", "bytes=20-29", "bytes=30-34"]


class TestDownloadInParts:

    def test_the_file_arrives_whole(self, tmp_path, serve):
        server = serve(PAYLOAD)
        odr._download_in_parts("u", str(tmp_path / "f"))
        assert (tmp_path / "f").read_bytes() == PAYLOAD
        assert server.ranges == PARTS and server.waits == []

    def test_a_file_that_is_an_exact_number_of_parts(self, tmp_path, serve):
        server = serve(PAYLOAD[:30])
        odr._download_in_parts("u", str(tmp_path / "f"))
        assert (tmp_path / "f").read_bytes() == PAYLOAD[:30]
        assert server.ranges == PARTS[:3]

    @pytest.mark.parametrize("fault", [500, 503, "reset", "short", "whole"])
    def test_only_the_failed_part_is_asked_for_again(self, tmp_path, serve, fault):
        server = serve(PAYLOAD, {2: fault})
        odr._download_in_parts("u", str(tmp_path / "f"))
        assert (tmp_path / "f").read_bytes() == PAYLOAD      # nothing doubled
        assert server.ranges == [PARTS[0], PARTS[1], PARTS[1], PARTS[2], PARTS[3]]
        assert server.waits == [2]

    def test_it_gives_up_on_a_part_that_never_arrives(self, tmp_path, serve):
        server = serve(PAYLOAD, {"all": 500})
        with pytest.raises(requests.exceptions.RequestException):
            odr._download_in_parts("u", str(tmp_path / "f"))
        assert server.ranges == [PARTS[0]] * odr.CDL_PART_ATTEMPTS
        assert server.waits == [2, 4, 8, 16, 30]

    def test_a_year_that_is_not_published_fails_at_once(self, tmp_path, serve):
        server = serve(PAYLOAD, {"all": 404})
        with pytest.raises(requests.exceptions.HTTPError):
            odr._download_in_parts("u", str(tmp_path / "f"))
        assert len(server.ranges) == 1 and server.waits == []

    def test_a_file_that_changes_mid_download_is_refused(self, tmp_path, serve):
        serve(PAYLOAD, {2: "resized"})
        with pytest.raises(requests.exceptions.RequestException, match="changed size"):
            odr._download_in_parts("u", str(tmp_path / "f"))


def _cdl_zip(tmp_path):
    """A tiny stand-in for the national archive: one Albers GeoTIFF of class
    111 around the test tile."""
    from rasterio.warp import transform_bounds

    left, _, _, top = transform_bounds(
        "EPSG:32615", "EPSG:5070", 500000, 4699900, 500100, 4700000)
    tif = tmp_path / "2025_30m_cdls.tif"
    write_raster(tif, np.full((60, 60), 111), "uint8", crs="EPSG:5070",
                 transform=from_origin(left - 600, top + 600, 30, 30), nodata=0)
    archive = tmp_path / "archive.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.write(tif, "2025_30m_cdls.tif")
        z.writestr("2025_30m_cdls.tif.xml", "<x/>")
    return archive.read_bytes()


class TestCdlDownload:

    def _run(self, tmp_path):
        profile = dict(crs="EPSG:32615", transform=TRANSFORM, width=10, height=10)
        return odr._download_cdl_for_water_extent(
            profile, 2025, str(tmp_path / "cdl_subset.tif"), str(tmp_path / "ref"))

    def test_it_asks_for_the_file_where_nass_now_serves_it(self, tmp_path, serve):
        """Checked 2026-11-23 with one-byte range requests:
          /Research_and_Science/Cropland/Release/datasets/2025_30m_cdls.zip      -> 404
          /sites/default/files/Research_and_Science/.../2025_30m_cdls.zip        -> 206
        (1 887 184 211 bytes; 2024 is 1 656 243 429.)"""
        server = serve(b"", {"all": "reset"})
        with pytest.raises(RuntimeError, match="Failed to download"):
            self._run(tmp_path)
        assert set(server.urls) == {CDL_URL}

    def test_it_is_fetched_through_faults_then_cached(self, tmp_path, serve):
        payload = _cdl_zip(tmp_path)
        server = serve(payload, {1: 500, 2: 500, 4: "short"}, part_bytes=len(payload) // 3)
        with rio.open(self._run(tmp_path)) as src:
            assert src.shape == (10, 10) and (src.read(1) == 111).all()
        assert (tmp_path / "ref" / "CDL_2025_30m_download.zip").read_bytes() == payload
        assert not list((tmp_path / "ref").glob("*.partial"))

        # The next scene of the run uses the cache.
        asked = len(server.ranges)
        self._run(tmp_path)
        assert len(server.ranges) == asked

    def test_a_download_that_fails_leaves_nothing_behind(self, tmp_path, serve):
        serve(PAYLOAD, {3: "reset", 4: 500, 5: 500, 6: 500, 7: 500, 8: 500})
        with pytest.raises(RuntimeError, match="Failed to download the 2025 CDL"):
            self._run(tmp_path)
        assert os.listdir(tmp_path / "ref") == []

    def test_a_body_that_is_not_the_archive_is_not_cached(self, tmp_path, serve):
        serve(b"<html>maintenance</html>")
        with pytest.raises(RuntimeError, match="not a zip archive"):
            self._run(tmp_path)
        assert os.listdir(tmp_path / "ref") == []

    def test_the_crop_covers_the_whole_scene(self, tmp_path):
        """The crop window was rounded with `round_lengths(op="ceil")`, but
        rasterio ignores `op` and rounds to the NEAREST pixel. Here the scene
        ends 0.44 of a CDL pixel past the rounded window, and its last column
        and row came back with no land-cover class."""
        reference_dir = tmp_path / "ref"
        reference_dir.mkdir()
        write_raster(reference_dir / "CDL_2025_30m.tif", np.full((20, 20), 111), "uint8",
                     crs="EPSG:5070", transform=from_origin(0, 3000, 30, 30), nodata=0)
        profile = dict(crs="EPSG:5070", width=11, height=11,
                       transform=from_origin(23.1, 3000 - 23.1, 10, 10))
        out = odr._download_cdl_for_water_extent(
            profile, 2025, str(tmp_path / "m.tif"), str(reference_dir))
        with rio.open(out) as src:
            assert (src.read(1) == 111).all()

    def test_the_year_is_capped_at_the_newest_published_cdl(self):
        assert odr.LATEST_CDL_YEAR == 2025
