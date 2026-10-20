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
