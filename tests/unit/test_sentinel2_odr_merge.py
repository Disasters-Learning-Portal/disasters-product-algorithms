"""Pins `process_sentinel2_odr --merge`: one mosaic per ACQUISITION.

The merge grouped tiles only by variant (nstd), so every scene in
--start-date..--end-date went into ONE first-wins mosaic, ordered by item id
(satellite letter, then tile, then date). Wherever tiles overlapped, S2A beat
S2C whatever the dates, and within one satellite the EARLIEST date won -- an
activation range spanning the event produced a "post-event" mosaic built from
pre-event pixels. The DPS config already promised "one product per date".

Grouping is by `s2:datatake_id` (one satellite pass) with its processing-
baseline suffix dropped; the same acquisition listed at two baselines keeps
only the newest, because both copies write the same per-tile filename.
Datatake ids and the `_0`/`_1` duplicate pattern are copied from live Earth
Search items (sentinel-2-l1c, Iowa T15TVG, 2021-07-15).
"""

import datetime as dt
import sys

import numpy as np
import pytest

rio = pytest.importorskip("rasterio")
pytest.importorskip("pystac_client")
from rasterio.transform import from_origin  # noqa: E402

from sentinel2 import process_sentinel2_odr as proc  # noqa: E402
from sentinel2 import sentinel2_odr_functions as odr  # noqa: E402


class FakeItem:
    def __init__(self, item_id, when, datatake, baseline=None, platform="sentinel-2a",
                 product_uri=None):
        self.id = item_id
        self.datetime = when
        self.properties = {"s2:datatake_id": datatake, "platform": platform}
        if baseline is not None:
            self.properties["s2:processing_baseline"] = baseline
        if product_uri is not None:
            self.properties["s2:product_uri"] = product_uri


# Two tiles per pass, overlapping in columns 4-5 (as adjacent MGRS tiles do).
TILE_X0 = {"T15TVG": 0, "15TVG": 0, "T15TVF": 40, "15TVF": 40}


def _run(tmp_path, monkeypatch, items, values, level="2"):
    """Run the real main() with search/generation/COG stubbed out.

    `values[item.id]` is the constant each scene's product holds, so a
    merged raster's pixel values show exactly which scenes it was built from.
    """
    def fake_generate_index(item, algorithm, algorithm_name, output_dir, nodata, cloud_mask):
        tile = odr._get_sat_level_tile(item)[2]
        path = f"{output_dir}/{odr._build_output_filename(item, algorithm_name)}"
        with rio.open(path, "w", driver="GTiff", count=1, dtype="float32", height=4, width=6,
                      nodata=-9999, crs="EPSG:32615",
                      transform=from_origin(400000 + TILE_X0[tile], 4600000, 10, 10)) as d:
            d.write(np.full((4, 6), values[item.id], np.float32), 1)
        return path

    monkeypatch.setattr(proc, "search_sentinel2", lambda **kw: list(items))
    monkeypatch.setattr(proc, "generate_index", fake_generate_index)
    monkeypatch.setattr(proc, "convert_to_cog", lambda outfile, **kw: outfile)
    monkeypatch.setattr(sys, "argv", [
        "process_sentinel2_odr", "--product", "ndvi", "--level", level,
        "--start-date", "2021-07-01", "--end-date", "2021-07-31",
        "--bbox", "-93.7", "41.5", "-93.5", "41.7", "--merge",
        "--output", str(tmp_path)])
    proc.main()
    out = {}
    for p in sorted(tmp_path.glob("*_merged_*.tif")):
        with rio.open(p) as src:
            out[p.name] = set(np.unique(src.read(1)).tolist()) - {-9999.0}
    return out


PRE = dt.datetime(2021, 7, 10, 17, 12, 10, tzinfo=dt.timezone.utc)
POST = dt.datetime(2021, 7, 12, 17, 22, 6, tzinfo=dt.timezone.utc)


def test_each_pass_is_its_own_mosaic(tmp_path, monkeypatch):
    # pre-event S2A pass (value 1) and post-event S2C pass (value 2), same
    # two tiles. Pre-fix: ONE mosaic, every overlapping pixel from S2A/pre.
    items = [
        FakeItem("S2C_T15TVG_20210712T170833_L2A", POST, "GS2C_20210712T165901_000002_N05.11", "05.11", "sentinel-2c"),
        FakeItem("S2C_T15TVF_20210712T170833_L2A", POST, "GS2C_20210712T165901_000002_N05.11", "05.11", "sentinel-2c"),
        FakeItem("S2A_T15TVG_20210710T170833_L2A", PRE, "GS2A_20210710T165901_000001_N05.11", "05.11"),
        FakeItem("S2A_T15TVF_20210710T170833_L2A", PRE, "GS2A_20210710T165901_000001_N05.11", "05.11"),
    ]
    values = {i.id: (2.0 if i.id.startswith("S2C") else 1.0) for i in items}
    out = _run(tmp_path, monkeypatch, items, values)
    assert len(out) == 2, out
    pre = next(v for k, v in out.items() if "2021-07-10" in k)
    post = next(v for k, v in out.items() if "2021-07-12" in k)
    assert pre == {1.0}
    assert post == {2.0}          # pre-fix: no such file; the one mosaic read {1.0}


def test_tiles_of_one_pass_are_mosaicked_together(tmp_path, monkeypatch):
    items = [
        FakeItem("S2A_T15TVG_20210710T170833_L2A", PRE, "GS2A_20210710T165901_000001_N05.11", "05.11"),
        FakeItem("S2A_T15TVF_20210710T170833_L2A", PRE, "GS2A_20210710T165901_000001_N05.11", "05.11"),
    ]
    out = _run(tmp_path, monkeypatch, items, {i.id: 1.0 for i in items})
    assert len(out) == 1
    with rio.open(next(tmp_path.glob("*_merged_*.tif"))) as src:
        assert src.width == 10    # both tiles, overlap counted once


def test_superseded_baseline_dropped_whatever_the_search_order(tmp_path, monkeypatch):
    # Live pattern: sentinel-2-l1c lists one acquisition as _1 (N05.00) and
    # _0 (N03.01) with an identical datetime. Both write the same per-tile
    # filename, so pre-fix the copy generated LAST won (here the old _0).
    when = dt.datetime(2021, 7, 15, 17, 12, 10, tzinfo=dt.timezone.utc)
    items = [
        FakeItem("S2A_15TVG_20210715_1_L1C", when, "GS2A_20210715T165901_031669_N05.00", "05.00"),
        FakeItem("S2A_15TVG_20210715_0_L1C", when, "GS2A_20210715T165901_031669_N03.01", "03.01"),
    ]
    values = {"S2A_15TVG_20210715_1_L1C": 5.0, "S2A_15TVG_20210715_0_L1C": 3.0}
    out = _run(tmp_path, monkeypatch, items, values, level="1")
    assert list(out.values()) == [{5.0}]


class TestAcquisitionKey:

    def test_baseline_suffix_dropped(self):
        a = FakeItem("x", PRE, "GS2A_20210715T165901_031669_N03.01")
        b = FakeItem("y", PRE, "GS2A_20210715T165901_031669_N05.00")
        assert odr.acquisition_key(a) == odr.acquisition_key(b) == "GS2A_20210715T165901_031669"

    def test_pass_straddling_utc_midnight_is_one_key(self):
        # Brisbane-style: one datatake, tiles either side of 00:00 UTC.
        dtk = "GS2B_20250318T235851_041955_N05.11"
        a = FakeItem("a", dt.datetime(2025, 3, 18, 23, 59, 50, tzinfo=dt.timezone.utc), dtk)
        b = FakeItem("b", dt.datetime(2025, 3, 19, 0, 0, 5, tzinfo=dt.timezone.utc), dtk)
        assert odr.acquisition_key(a) == odr.acquisition_key(b)

    def test_fallback_without_datatake(self):
        item = FakeItem("x", PRE, None)
        item.properties.pop("s2:datatake_id")
        assert odr.acquisition_key(item) == "sentinel-2a_20210710"

    def test_processing_baseline_from_datatake_suffix(self):
        assert odr.processing_baseline(FakeItem("x", PRE, "GS2A_X_N05.11")) == 5.11
        assert odr.processing_baseline(FakeItem("x", PRE, "GS2A_X")) == 0.0


# ---------------------------------------------------------------------------
# Merged filename: repo convention (rule 27) -- date only + `_day` -- with
# ESA's relative orbit so same-day passes of one satellite don't collide.
# Property values copied from a live sentinel-2-c1-l2a item (S2C T15TVF
# 2025-03-19): datatake GS2C_20250319T171041_002799_N05.11, product_uri
# S2C_MSIL2A_20250319T171041_N0511_R112_T15TVF_20250319T222513.SAFE.
# ---------------------------------------------------------------------------

LIVE = FakeItem(
    "S2C_T15TVF_20250319T171419_L2A",
    dt.datetime(2025, 3, 19, 17, 22, 40, tzinfo=dt.timezone.utc),
    "GS2C_20250319T171041_002799_N05.11", "05.11", "sentinel-2c",
    "S2C_MSIL2A_20250319T171041_N0511_R112_T15TVF_20250319T222513.SAFE")


class TestMergedFilename:

    def test_merged_name_is_date_day_with_orbit(self):
        assert odr._build_output_filename(LIVE, "ndvi", merged=True) == \
            "S2C_MSIL2A_ndvi_R112_merged_2025-03-19_day.tif"

    def test_merged_masked_variant(self):
        assert odr._build_output_filename(
            LIVE, "we", merged=True, masked=True, variant="NSTD_1_5") == \
            "S2C_MSIL2A_waterExtent_NSTD_1_5_R112_merged_masked_2025-03-19_day.tif"

    def test_per_tile_name_unchanged(self):
        assert odr._build_output_filename(LIVE, "ndvi") == \
            "S2C_MSIL2A_ndvi_T15TVF_2025-03-19T17:22:40Z.tif"

    def test_date_is_pass_start_not_tile_time(self):
        # A pass starting 23:58 UTC whose tile is sensed after midnight is
        # dated by the PASS (datatake), so the whole pass gets one date.
        item = FakeItem("S2B_T56JNQ_20250319T000244_L2A",
                        dt.datetime(2025, 3, 19, 0, 0, 5, tzinfo=dt.timezone.utc),
                        "GS2B_20250318T235851_041955_N05.11", "05.11", "sentinel-2b",
                        "S2B_MSIL2A_20250318T235851_N0511_R030_T56JNQ_20250319T010000.SAFE")
        assert odr._build_output_filename(item, "ndvi", merged=True).endswith(
            "_R030_merged_2025-03-18_day.tif")

    def test_orbit_fallbacks(self):
        item = FakeItem("S2C_T15TVF_20250319T171419_L2A", LIVE.datetime,
                        "GS2C_20250319T171041_002799_N05.11")
        assert odr._orbit_token(item) == "A002799"       # absolute orbit
        item.properties["sat:relative_orbit"] = 112
        assert odr._orbit_token(item) == "R112"

    def test_merged_name_is_fixed_point_of_repo_renamers(self):
        from shared_utils.cog_utils import get_final_filename
        from shared_utils.file_naming import extract_datetime_from_filename
        name = odr._build_output_filename(LIVE, "ndvi", merged=True)
        assert get_final_filename(name, "202503_Flood_IA") == name
        assert extract_datetime_from_filename(name) == ("2025-03-19", "day")


def test_two_same_day_passes_of_one_satellite_get_two_files(tmp_path, monkeypatch):
    # High latitudes: adjacent orbits of ONE satellite can both cover the AOI
    # on one day. Date-only names alone would collide; the orbit keeps them.
    day = dt.datetime(2025, 6, 21, 11, 0, 0, tzinfo=dt.timezone.utc)
    items = [
        FakeItem("S2A_T15TVG_20250621T110000_L2A", day, "GS2A_20250621T105000_052000_N05.11",
                 "05.11", product_uri="S2A_MSIL2A_20250621T105000_N0511_R051_T15TVG_X.SAFE"),
        FakeItem("S2A_T15TVF_20250621T124000_L2A", day.replace(hour=12, minute=40),
                 "GS2A_20250621T123000_052001_N05.11", "05.11",
                 product_uri="S2A_MSIL2A_20250621T123000_N0511_R052_T15TVF_X.SAFE"),
    ]
    out = _run(tmp_path, monkeypatch, items, {items[0].id: 1.0, items[1].id: 2.0})
    assert sorted(out) == ["S2A_MSIL2A_ndvi_R051_merged_2025-06-21_day.tif",
                           "S2A_MSIL2A_ndvi_R052_merged_2025-06-21_day.tif"]


# ---------------------------------------------------------------------------
# Water extent across the tiles of ONE pass:
#   * one NIR threshold per acquisition, pooled over its tiles
#   * in the merge, a cloud flag (class 5) from any tile wins
# Measured on real data (Iowa T15TVG/T15TVF, 2024-07-22): per-tile thresholds
# were 0.425 vs 0.462 and the tiles disagreed on water/not-water for 5.5% of
# their overlap; across 12 passes their SCL cloud flags disagreed on 0.7-9.7%
# of overlap pixels, 13% of the disputes being cloud shadow.
# ---------------------------------------------------------------------------

class _Asset:
    extra_fields = {}


def _we_items():
    when = dt.datetime(2025, 7, 1, 10, 30, tzinfo=dt.timezone.utc)
    dtk = "GS2A_20250701T102000_052000_N05.11"
    uri = "S2A_MSIL2A_20250701T102000_N0511_R065_T33UUP_X.SAFE"
    items = [FakeItem(f"S2A_{t}_20250701T102000_L2A", when, dtk, "05.11", product_uri=uri)
             for t in ("T33UUP", "T33UUQ", "T33UVP")]
    for it in items:
        it.assets = {"nir": _Asset()}
    return items


def _we_scene(item_id):
    """20x40 B08 DN. Cols 0-9 WorldCover 80 water, 10-39 cropland at 1450.
    Tile UUP's water is dark (1150/1250), UUQ's brighter (1550/1650): per
    tile, UUP's threshold (1250) calls the cropland dry and UUQ's (1650)
    calls it water -- a seam. UVP has no water at all."""
    nir = np.full((20, 40), 1450, np.uint16)
    wc = np.full((20, 40), 40, np.uint8)
    if "UVP" not in item_id:
        lo, hi = (1150, 1250) if "UUP" in item_id else (1550, 1650)
        nir[:, 0:10] = np.where(np.indices((20, 10)).sum(0) % 2, lo, hi)
        wc[:, 0:10] = 80
    return nir, wc


def _run_we(tmp_path, monkeypatch, items, merge):
    profile = {"crs": "EPSG:32633", "transform": from_origin(500000, 5000000, 10, 10),
               "width": 40, "height": 20, "count": 1, "dtype": "uint16", "driver": "GTiff"}
    current = {}

    def fake_read(asset, **kw):
        nir, wc = _we_scene(current["id"])
        return nir, dict(profile)

    def fake_worldcover(nir_profile, year, output_path):
        _, wc = _we_scene(current["id"])
        with rio.open(output_path, "w", **{**profile, "dtype": "uint8"}) as d:
            d.write(wc, 1)

    real = odr.generate_water_extent

    def tracking_generate(item, **kw):
        current["id"] = item.id
        return real(item=item, **kw)

    monkeypatch.setattr(odr, "get_asset_metadata", lambda a: ("x", 1e-4, -0.1, 10, 0))
    monkeypatch.setattr(odr, "read_algorithm_band", fake_read)
    monkeypatch.setattr(odr, "_download_worldcover_for_water_extent", fake_worldcover)
    monkeypatch.setattr(proc, "generate_water_extent", tracking_generate)
    monkeypatch.setattr(proc, "search_sentinel2", lambda **kw: list(items))
    monkeypatch.setattr(proc, "convert_to_cog", lambda outfile, **kw: outfile)
    monkeypatch.setattr(sys, "argv", [
        "process_sentinel2_odr", "--product", "we", "--level", "2",
        "--start-date", "2025-07-01", "--end-date", "2025-07-01",
        "--bbox", "14.9", "45.0", "15.1", "45.2", "--output", str(tmp_path)]
        + (["--merge"] if merge else []))
    proc.main()
    out = {}
    for p in sorted(tmp_path.glob("*waterExtent*.tif")):
        with rio.open(p) as src:
            out[p.name] = src.read(1)
    return out


def test_tiles_of_one_pass_share_one_threshold(tmp_path, monkeypatch):
    out = _run_we(tmp_path, monkeypatch, _we_items(), merge=False)
    land = {next(t for t in name.split("_") if t.startswith("T33")): arr[3:17, 13:37]
            for name, arr in out.items()}
    # Pooled threshold ~1606 DN: the shared cropland is classified the SAME
    # way in every tile of the pass. Pre-fix: UUP 0 (dry), UUQ 4 (flooded).
    assert (land["T33UUP"] == land["T33UUQ"]).all()
    assert (land["T33UUP"] == 4).all()


def test_tile_without_reference_water_uses_the_pass_threshold(tmp_path, monkeypatch):
    # Pre-fix: UVP raised "No valid permanent-water pixels" and the run died.
    out = _run_we(tmp_path, monkeypatch, _we_items(), merge=False)
    uvp = next(a for n, a in out.items() if "T33UVP" in n)
    assert (uvp[3:17, 3:37] == 4).all()


def _we_tif(path, arr):
    with rio.open(path, "w", driver="GTiff", count=1, dtype="uint8", height=arr.shape[0],
                  width=arr.shape[1], nodata=0, crs="EPSG:32633",
                  transform=from_origin(500000, 5000000, 10, 10)) as d:
        d.write(arr.astype(np.uint8), 1)
    return str(path)


@pytest.mark.parametrize("order", ["clear_first", "cloud_first"])
def test_cloud_flag_from_any_tile_wins(tmp_path, order):
    # Same pixel, same pass: one tile's SCL says cloud/shadow (5), the other
    # says flooded crop (4). Pre-fix (first-wins) the answer was 4 or 5
    # depending on item-id order; it must be 5 either way.
    clear = _we_tif(tmp_path / "a.tif", np.array([[4, 1, 0, 2]]))
    cloud = _we_tif(tmp_path / "b.tif", np.array([[5, 1, 3, 0]]))
    inputs = [clear, cloud] if order == "clear_first" else [cloud, clear]
    out = odr.merge_products(inputs, str(tmp_path / "m.tif"), method=odr.water_extent_merge_method)
    with rio.open(out) as src:
        got = src.read(1)[0].tolist()
    assert got[0] == 5            # cloud wins
    assert got[1] == 1            # agreement unchanged
    assert got[2] == 3            # nodata (0) yields to data
    assert got[3] == 2


def test_pooled_statistics_rules():
    from shared_utils.landcover import pooled_water_statistics
    mean, std = pooled_water_statistics([(np.array([1., 3.]), np.array([1., 3.])),
                                         (np.array([]), np.array([]))])
    assert (mean, std) == (2.0, 1.0)
    with pytest.raises(RuntimeError, match="any tile of this acquisition"):
        pooled_water_statistics([(np.array([]), np.array([]))])
