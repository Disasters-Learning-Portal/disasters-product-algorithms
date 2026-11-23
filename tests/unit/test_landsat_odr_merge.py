"""Pins `process_landsat_odr --merge`: one mosaic per satellite PASS.

As first written the merge put every scene in --start-date..--end-date into ONE
first-wins mosaic, ordered by item id (satellite, then path/row, then date), and
named it with the first scene's sensing time. The notebook ships with merging
on, so that is what an operator got.

The scenes used here are a real search result (Earth Search `landsat-c2-l2`,
bbox -88,35,-86,36, 2025-09-06..2025-09-16): six passes, twelve scenes, three
overlapping WRS-2 paths, Landsat 8 and 9 alternating. Unfixed, all twelve became
one file named `LC08_L2SP_ndvi_merged_2025-09-16T16:18:20Z.tif` in which Landsat
8 won every overlap whatever the date -- eleven days of acquisitions stitched
into a product carrying a single timestamp.

Same failure, same fix as the Sentinel-2 STAC path (.clinerules.md rule 66).
Landsat items carry no datatake id, so a pass is identified by satellite + WRS-2
path + a gap in sensing time (group_by_acquisition).
"""

import datetime as dt
import sys

import numpy as np
import pytest

rio = pytest.importorskip("rasterio")
pytest.importorskip("pystac_client")
from rasterio.transform import from_origin  # noqa: E402

from landsat import landsat_odr_functions as odr  # noqa: E402
from landsat import process_landsat_odr as proc  # noqa: E402

UTC = dt.timezone.utc


class FakeItem:
    def __init__(self, item_id, when):
        self.id = item_id
        self.datetime = when
        self.properties = {"gsd": 30}


def _t(text):
    return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)


# id, sensing time -- copied from the live search described above.
LIVE = [
    ("LC09_L2SP_022035_20250906_02_T1", "2025-09-06T16:30:42"),
    ("LC09_L2SP_022036_20250906_02_T2", "2025-09-06T16:31:06"),
    ("LC08_L2SP_021035_20250907_02_T1", "2025-09-07T16:24:27"),
    ("LC08_L2SP_021036_20250907_02_T1", "2025-09-07T16:24:51"),
    ("LC09_L2SP_020035_20250908_02_T1", "2025-09-08T16:18:21"),
    ("LC09_L2SP_020036_20250908_02_T1", "2025-09-08T16:18:45"),
    ("LC08_L2SP_022035_20250914_02_T1", "2025-09-14T16:30:40"),
    ("LC08_L2SP_022036_20250914_02_T1", "2025-09-14T16:31:04"),
    ("LC09_L2SP_021035_20250915_02_T1", "2025-09-15T16:24:32"),
    ("LC09_L2SP_021036_20250915_02_T1", "2025-09-15T16:24:56"),
    ("LC08_L2SP_020035_20250916_02_T1", "2025-09-16T16:18:20"),
    ("LC08_L2SP_020036_20250916_02_T1", "2025-09-16T16:18:44"),
]


def live_items():
    return [FakeItem(item_id, _t(when)) for item_id, when in LIVE]


# ---------------------------------------------------------------------------
# group_by_acquisition
# ---------------------------------------------------------------------------

class TestGroupByAcquisition:

    def test_the_live_search_is_six_passes_of_two_scenes(self):
        passes = odr.group_by_acquisition(live_items())
        assert [[s.id for s in p] for p in passes] == [
            ["LC09_L2SP_022035_20250906_02_T1", "LC09_L2SP_022036_20250906_02_T2"],
            ["LC08_L2SP_021035_20250907_02_T1", "LC08_L2SP_021036_20250907_02_T1"],
            ["LC09_L2SP_020035_20250908_02_T1", "LC09_L2SP_020036_20250908_02_T1"],
            ["LC08_L2SP_022035_20250914_02_T1", "LC08_L2SP_022036_20250914_02_T1"],
            ["LC09_L2SP_021035_20250915_02_T1", "LC09_L2SP_021036_20250915_02_T1"],
            ["LC08_L2SP_020035_20250916_02_T1", "LC08_L2SP_020036_20250916_02_T1"],
        ]

    def test_search_order_does_not_matter(self):
        forward = odr.group_by_acquisition(live_items())
        backward = odr.group_by_acquisition(list(reversed(live_items())))
        assert [[s.id for s in p] for p in forward] == [[s.id for s in p] for p in backward]

    def test_every_item_lands_in_exactly_one_pass(self):
        items = live_items()
        grouped = [s.id for p in odr.group_by_acquisition(items) for s in p]
        assert sorted(grouped) == sorted(i.id for i in items)

    def test_same_path_sixteen_days_later_is_another_pass(self):
        a = FakeItem("LC08_L2SP_021035_20250907_02_T1", _t("2025-09-07T16:24:27"))
        b = FakeItem("LC08_L2SP_021035_20250923_02_T1", _t("2025-09-23T16:24:30"))
        assert len(odr.group_by_acquisition([a, b])) == 2

    def test_two_satellites_on_one_day_are_two_passes(self):
        # Same WRS-2 path on the same UTC date cannot happen for L8 + L9 in
        # orbit, but the key must not rely on that.
        a = FakeItem("LC08_L2SP_021035_20250907_02_T1", _t("2025-09-07T16:24:27"))
        b = FakeItem("LC09_L2SP_021035_20250907_02_T1", _t("2025-09-07T16:24:40"))
        assert len(odr.group_by_acquisition([a, b])) == 2

    def test_a_pass_straddling_utc_midnight_stays_one_pass(self):
        # Eastern Australia: Landsat's ~10:10 local crossing is ~00:00 UTC near
        # 152 E. A calendar-date key would cut this single pass in two.
        a = FakeItem("LC09_L2SP_089078_20250318_02_T1", _t("2025-03-18T23:59:40"))
        b = FakeItem("LC09_L2SP_089079_20250319_02_T1", _t("2025-03-19T00:00:04"))
        passes = odr.group_by_acquisition([b, a])
        assert [[s.id for s in p] for p in passes] == [[a.id, b.id]]

    def test_rows_dropped_by_the_cloud_filter_do_not_split_a_pass(self):
        # Rows 35 and 38 survive --cloud-cover, 36 and 37 do not: ~72 s apart.
        a = FakeItem("LC08_L2SP_021035_20250907_02_T1", _t("2025-09-07T16:24:27"))
        b = FakeItem("LC08_L2SP_021038_20250907_02_T1", _t("2025-09-07T16:25:39"))
        assert len(odr.group_by_acquisition([a, b])) == 1

    def test_empty_input(self):
        assert odr.group_by_acquisition([]) == []


# ---------------------------------------------------------------------------
# Filenames
# ---------------------------------------------------------------------------

SCENE = FakeItem("LC09_L2SP_022035_20250906_02_T1", _t("2025-09-06T16:30:42"))


class TestOutputFilename:

    def test_per_scene_name_ends_in_the_scene_time(self):
        assert odr._build_output_filename(SCENE, "ndvi") == \
            "LC09_L2SP_ndvi_022035_2025-09-06T16:30:42Z.tif"

    def test_per_scene_masked(self):
        assert odr._build_output_filename(SCENE, "true_color", masked=True) == \
            "LC09_L2SP_trueColor_022035_masked_2025-09-06T16:30:42Z.tif"

    def test_merged_name_is_path_date_day(self):
        """Rule 27: a mosaic spans several scene times, so it ends in the date
        and `_day`. It used to end in the first scene's time to the second."""
        assert odr._build_output_filename(SCENE, "ndvi", merged=True) == \
            "LC09_L2SP_ndvi_P022_merged_2025-09-06_day.tif"

    def test_merged_and_masked(self):
        assert odr._build_output_filename(SCENE, "color_infrared", merged=True, masked=True) == \
            "LC09_L2SP_colorInfrared_P022_merged_masked_2025-09-06_day.tif"

    def test_two_passes_on_one_day_do_not_collide(self):
        other_path = FakeItem("LC09_L2SP_038035_20250906_02_T1", _t("2025-09-06T18:09:30"))
        other_sat = FakeItem("LC08_L2SP_022035_20250906_02_T1", _t("2025-09-06T16:30:42"))
        names = {
            odr._build_output_filename(item, "ndvi", merged=True)
            for item in (SCENE, other_path, other_sat)
        }
        assert len(names) == 3

    @pytest.mark.parametrize("kwargs", [{}, {"masked": True}, {"merged": True},
                                        {"merged": True, "masked": True}])
    def test_names_are_fixed_points_of_the_repo_renamers(self, kwargs):
        """A downstream rename pass must not rewrite a name this pipeline wrote
        (a second S3 key for the same product)."""
        from shared_utils.cog_utils import get_final_filename
        from shared_utils.file_naming import create_output_filename

        name = odr._build_output_filename(SCENE, "ndvi", **kwargs)
        assert get_final_filename(name, "202509_Flood_TN") == name
        assert create_output_filename(name, "") == name

    def test_merged_name_parses_as_a_date_and_day(self):
        from shared_utils.file_naming import extract_datetime_from_filename

        name = odr._build_output_filename(SCENE, "ndvi", merged=True)
        assert extract_datetime_from_filename(name) == ("2025-09-06", "day")

    def test_unexpected_item_id_raises(self):
        with pytest.raises(ValueError, match="Unexpected Landsat item id"):
            odr._build_output_filename(FakeItem("not-a-landsat-id", _t("2025-09-06T16:30:42")), "ndvi")


# ---------------------------------------------------------------------------
# The real main(), with search / generation / COG conversion stubbed
# ---------------------------------------------------------------------------

# Column offset per WRS-2 path/row, so scenes of one pass overlap by two columns
# (as consecutive rows do) and the three paths overlap each other.
X0 = {"020035": 80, "020036": 80, "021035": 40, "021036": 40, "022035": 0, "022036": 0}
Y0 = {"035": 0, "036": 20}


def _run(tmp_path, monkeypatch, items, values, merge=True, product="ndvi", cloud_mask=False):
    """Run the real main(). `values[item.id]` is the constant each scene holds,
    so a mosaic's pixel values show exactly which scenes it was built from."""

    def fake_generate_index(item, algorithm, algorithm_name, output_dir, nodata, cloud_mask):
        tile = odr._get_sat_level_tile(item)[2]
        path = f"{output_dir}/{odr._build_output_filename(item, algorithm_name, masked=cloud_mask)}"
        transform = from_origin(400000 + X0[tile] * 30, 4000000 - Y0[tile[3:]] * 30, 30, 30)
        profile = dict(driver="GTiff", count=1, dtype="float32", height=24, width=60,
                       nodata=-9999, crs="EPSG:32616", transform=transform)
        with rio.open(path, "w", **profile) as dst:
            dst.write(np.full((24, 60), values[item.id], np.float32), 1)
        if cloud_mask:
            mask = np.zeros((24, 60), bool)
            mask[0:2, 0:2] = True                 # a cloud in each scene's corner
            odr.write_cloud_mask_sidecar(path, mask, profile)
        return path

    monkeypatch.setattr(proc, "search_landsat", lambda **kw: list(items))
    monkeypatch.setattr(proc, "generate_index", fake_generate_index)
    monkeypatch.setattr(proc, "convert_to_cog", lambda outfile, **kw: outfile)
    monkeypatch.setattr(sys, "argv", [
        "process_landsat_odr", "--product", product,
        "--start-date", "2025-09-06", "--end-date", "2025-09-16",
        "--bbox", "-88", "35", "-86", "36", "--output", str(tmp_path)]
        + (["--merge"] if merge else [])
        + (["--cloud-mask"] if cloud_mask else []))
    proc.main()

    out = {}
    for path in sorted(tmp_path.glob("*.tif")):
        with rio.open(path) as src:
            out[path.name] = set(np.unique(src.read(1)).tolist()) - {-9999.0}
    return out


def test_each_pass_is_its_own_mosaic(tmp_path, monkeypatch):
    items = live_items()
    values = {item.id: float(n) for n, item in enumerate(items, start=1)}
    out = _run(tmp_path, monkeypatch, items, values)

    assert sorted(out) == [
        "LC08_L2SP_ndvi_P020_merged_2025-09-16_day.tif",
        "LC08_L2SP_ndvi_P021_merged_2025-09-07_day.tif",
        "LC08_L2SP_ndvi_P022_merged_2025-09-14_day.tif",
        "LC09_L2SP_ndvi_P020_merged_2025-09-08_day.tif",
        "LC09_L2SP_ndvi_P021_merged_2025-09-15_day.tif",
        "LC09_L2SP_ndvi_P022_merged_2025-09-06_day.tif",
    ]
    # Each mosaic holds ONLY its own pass's two scenes. Unfixed there was one
    # file, and Landsat 8's 09-16 scenes covered Landsat 9's 09-08 ones.
    assert out["LC09_L2SP_ndvi_P022_merged_2025-09-06_day.tif"] == {1.0, 2.0}
    assert out["LC09_L2SP_ndvi_P020_merged_2025-09-08_day.tif"] == {5.0, 6.0}
    assert out["LC08_L2SP_ndvi_P020_merged_2025-09-16_day.tif"] == {11.0, 12.0}


def test_the_scenes_of_one_pass_are_mosaicked_together(tmp_path, monkeypatch):
    items = live_items()[:2]
    out = _run(tmp_path, monkeypatch, items, {i.id: 1.0 for i in items})
    assert list(out) == ["LC09_L2SP_ndvi_P022_merged_2025-09-06_day.tif"]
    with rio.open(tmp_path / next(iter(out))) as src:
        assert (src.height, src.width) == (44, 60)     # two rows, overlap counted once


def test_per_scene_files_are_removed_once_merged(tmp_path, monkeypatch):
    items = live_items()
    out = _run(tmp_path, monkeypatch, items, {i.id: 1.0 for i in items})
    assert all("_merged_" in name for name in out)


def test_without_merge_every_scene_is_its_own_file(tmp_path, monkeypatch):
    items = live_items()
    out = _run(tmp_path, monkeypatch, items, {i.id: 1.0 for i in items}, merge=False)
    assert len(out) == 12
    assert not any("_merged_" in name for name in out)


def test_each_mosaic_keeps_only_its_own_passes_cloud_mask(tmp_path, monkeypatch):
    """The cloud union is per pass. Unioning every scene's mask produced a
    raster the size of the whole search, which no single mosaic matches."""
    items = live_items()
    out = _run(tmp_path, monkeypatch, items, {i.id: 1.0 for i in items}, cloud_mask=True)
    assert len(out) == 6
    for name in out:
        assert "_merged_masked_" in name
        with rio.open(tmp_path / name) as src:
            data = src.read(1)
        # Two scenes, each with a 2x2 cloud in its top-left corner; the second
        # row's cloud lies inside the first row's footprint and must survive.
        assert (data[0:2, 0:2] == -9999).all()
        assert (data[20:22, 0:2] == -9999).all()
        assert (data == -9999).sum() == 8
    assert not (tmp_path / "_cloud_masks").exists()


# ---------------------------------------------------------------------------
# The CLI surface
# ---------------------------------------------------------------------------

class TestProductChoices:

    def _choices(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["process_landsat_odr", "--help"])
        with pytest.raises(SystemExit):
            proc.main()
        usage = capsys.readouterr().out
        import re
        return set(re.search(r"--product \{([^}]+)\}", usage).group(1).split(","))

    def test_every_choice_is_a_product_the_cli_can_make(self, monkeypatch, capsys):
        """`we` was offered and then rejected as "Unsupported Landsat product"
        after the STAC search had already run."""
        choices = self._choices(monkeypatch, capsys)
        assert choices == proc.INDEX_PRODUCTS | proc.COMPOSITE_PRODUCTS
        assert "we" not in choices

    def test_every_choice_has_a_catalog_entry(self, monkeypatch, capsys):
        for product in self._choices(monkeypatch, capsys):
            kind = "index" if product in proc.INDEX_PRODUCTS else "composite"
            entry = odr.get_algorithm(kind, product, proc.DEFAULT_ALGORITHM_FILE)
            assert entry["assets"]

    def test_every_choice_resolves_to_a_published_directory(self, monkeypatch, capsys):
        """The filename token is what the notebook keys the upload on."""
        from shared_utils.product_paths import PRODUCT_DIRS

        landsat_keys = {key.lower(): key for sensor, key in PRODUCT_DIRS if sensor == "landsat"}
        alias = {"swir": "shortwaveinfrared"}
        for product in self._choices(monkeypatch, capsys):
            token = odr._product_token(product).lower()
            assert alias.get(token, token) in landsat_keys, product

    def test_a_bad_bbox_is_rejected_before_the_search(self, monkeypatch):
        monkeypatch.setattr(proc, "search_landsat",
                            lambda **kw: pytest.fail("searched with a transposed bbox"))
        monkeypatch.setattr(sys, "argv", [
            "process_landsat_odr", "--product", "ndvi",
            "--start-date", "2025-09-06", "--end-date", "2025-09-16",
            "--bbox", "-86", "35", "-88", "36"])
        with pytest.raises(ValueError, match="min < max"):
            proc.main()


class TestPackaging:

    def test_the_default_catalog_exists_next_to_the_module(self):
        import os
        assert os.path.isfile(proc.DEFAULT_ALGORITHM_FILE)
        assert os.path.dirname(proc.DEFAULT_ALGORITHM_FILE) == os.path.dirname(proc.__file__)

    @pytest.mark.parametrize("package,filename", [
        ("landsat", "algorithms-landsat.json"),
        ("sentinel2", "algorithms-sentinel2.json"),
    ])
    def test_the_catalog_is_declared_package_data(self, package, filename):
        """The hub and DPS images `pip install` a copy of the tree with no .git,
        so setuptools-scm cannot discover data files: only what
        [tool.setuptools.package-data] names is installed. Without the entry
        the CLI's default --algorithm-file does not exist on the hub -- and
        `--help`, which is all CI's cli-smoke runs, never opens it."""
        import os
        import tomllib

        root = os.path.join(os.path.dirname(__file__), "..", "..")
        with open(os.path.join(root, "pyproject.toml"), "rb") as fh:
            data = tomllib.load(fh)["tool"]["setuptools"]["package-data"]
        assert filename in data.get(package, [])


# ---------------------------------------------------------------------------
# One pass, two UTM zones, composite + cloud mask
# ---------------------------------------------------------------------------

def test_a_pass_that_changes_utm_zone_merges_with_its_cloud_mask(tmp_path, monkeypatch):
    """Consecutive rows of one path can sit in different UTM zones (path 19
    rows 35/36 do). The composite mosaic and the union of the scenes' cloud
    masks are built by two separate merges; they must come out on the same
    grid. They did not when the two merges were handed the scenes in different
    orders (item-id order for the mosaic, search order for the masks) and each
    picked its "dominant" CRS from a 1-1 tie -- `RuntimeError: Cloud mask
    (42, 74) does not match composite (43, 74)`, after every scene had been
    read from a requester-pays bucket."""
    north = FakeItem("LC08_L2SP_019035_20250907_02_T1", _t("2025-09-07T16:12:03"))
    south = FakeItem("LC08_L2SP_019036_20250907_02_T1", _t("2025-09-07T16:12:27"))
    georef = {
        north.id: ("EPSG:32617", from_origin(240000, 3990000, 30, 30)),
        south.id: ("EPSG:32616", from_origin(760000, 3940000, 30, 30)),
    }

    def fake_generate_composite(item, algorithm, algorithm_name, output_dir, cloud_mask):
        crs, transform = georef[item.id]
        path = f"{output_dir}/{odr._build_output_filename(item, algorithm_name, masked=cloud_mask)}"
        profile = dict(driver="GTiff", count=3, dtype="uint8", height=1500, width=1500,
                       nodata=0, crs=crs, transform=transform)
        with rio.open(path, "w", **profile) as dst:
            dst.write(np.full((3, 1500, 1500), 90, np.uint8))
        mask = np.zeros((1500, 1500), bool)
        mask[700:800, 700:800] = True
        odr.write_cloud_mask_sidecar(path, mask, profile)
        return path

    # Earth Search returns newest first; the CLI must not depend on that order.
    monkeypatch.setattr(proc, "search_landsat", lambda **kw: [south, north])
    monkeypatch.setattr(proc, "generate_composite", fake_generate_composite)
    monkeypatch.setattr(proc, "convert_to_cog", lambda outfile, **kw: outfile)
    monkeypatch.setattr(sys, "argv", [
        "process_landsat_odr", "--product", "true_color", "--merge", "--cloud-mask",
        "--start-date", "2025-09-07", "--end-date", "2025-09-07",
        "--bbox", "-84.1", "34.0", "-83.9", "36.0", "--output", str(tmp_path)])
    proc.main()

    out = sorted(p.name for p in tmp_path.glob("*.tif"))
    assert out == ["LC08_L2SP_trueColor_P019_merged_masked_2025-09-07_day.tif"]
    with rio.open(tmp_path / out[0]) as src:
        rgba = src.read()
    assert rgba.shape[0] == 4
    gray = (rgba[:3] == odr.CLOUD_GRAY).all(axis=0)
    assert gray.sum() > 0 and (rgba[3][gray] == 255).all()       # both clouds, opaque
    assert ((rgba[3] == 0) | (rgba[3] == 255)).all()


# ---------------------------------------------------------------------------
# A failed run leaves nothing that looks like a product
# ---------------------------------------------------------------------------

class TestFailedRunCleansUp:
    """`--output` is published by filename: a notebook upload cell or a DPS
    finalize step cannot tell a half-made GeoTIFF from a finished COG. A run
    that died used to leave its intermediates there under product names --
    not COGs, no activation tags -- for the next upload to pick up."""

    def _main(self, tmp_path, monkeypatch, items, generate, convert, merge=False,
              cloud_mask=False):
        monkeypatch.setattr(proc, "search_landsat", lambda **kw: list(items))
        monkeypatch.setattr(proc, "generate_index", generate)
        monkeypatch.setattr(proc, "convert_to_cog", convert)
        monkeypatch.setattr(sys, "argv", [
            "process_landsat_odr", "--product", "ndvi",
            "--start-date", "2025-09-06", "--end-date", "2025-09-16",
            "--bbox", "-88", "35", "-86", "36", "--output", str(tmp_path)]
            + (["--merge"] if merge else []) + (["--cloud-mask"] if cloud_mask else []))
        proc.main()

    @staticmethod
    def _write(item, algorithm_name, output_dir, cloud_mask=False):
        tile = odr._get_sat_level_tile(item)[2]
        path = f"{output_dir}/{odr._build_output_filename(item, algorithm_name, masked=cloud_mask)}"
        profile = dict(driver="GTiff", count=1, dtype="float32", height=24, width=60,
                       nodata=-9999, crs="EPSG:32616",
                       transform=from_origin(400000 + X0[tile] * 30, 4000000 - Y0[tile[3:]] * 30,
                                             30, 30))
        with rio.open(path, "w", **profile) as dst:
            dst.write(np.ones((24, 60), np.float32), 1)
        if cloud_mask:
            odr.write_cloud_mask_sidecar(path, np.zeros((24, 60), bool), profile)
        return path

    def test_a_scene_that_fails_takes_the_earlier_scenes_with_it(self, tmp_path, monkeypatch):
        items = live_items()[:4]

        def generate(item, algorithm, algorithm_name, output_dir, nodata, cloud_mask):
            if item is items[2]:
                # Die after starting to write, as a dropped S3 read would.
                self._write(item, algorithm_name, output_dir, cloud_mask)
                raise OSError("simulated read failure")
            return self._write(item, algorithm_name, output_dir, cloud_mask)

        with pytest.raises(OSError, match="simulated"):
            self._main(tmp_path, monkeypatch, items, generate,
                       lambda outfile, **kw: outfile, cloud_mask=True)
        assert sorted(p.name for p in tmp_path.iterdir()) == []

    def test_a_failed_conversion_keeps_only_the_finished_cogs(self, tmp_path, monkeypatch):
        items = live_items()[:3]
        done = []

        def generate(item, algorithm, algorithm_name, output_dir, nodata, cloud_mask):
            return self._write(item, algorithm_name, output_dir)

        def convert(outfile, **kw):
            if len(done) == 1:
                raise RuntimeError("simulated COG failure")
            done.append(outfile)
            return outfile

        with pytest.raises(RuntimeError, match="simulated"):
            self._main(tmp_path, monkeypatch, items, generate, convert)
        assert sorted(str(p) for p in tmp_path.glob("*.tif")) == sorted(done)
        assert len(done) == 1

    def test_a_failed_merge_leaves_no_partial_mosaic(self, tmp_path, monkeypatch):
        items = live_items()[:4]                      # two passes

        def generate(item, algorithm, algorithm_name, output_dir, nodata, cloud_mask):
            return self._write(item, algorithm_name, output_dir)

        real_merge = proc.merge_products
        calls = []

        def failing_merge(paths, output_path, **kw):
            calls.append(output_path)
            if len(calls) == 2:
                real_merge(paths, output_path, **kw)  # the file exists, then:
                raise RuntimeError("simulated merge failure")
            return real_merge(paths, output_path, **kw)

        monkeypatch.setattr(proc, "merge_products", failing_merge)
        with pytest.raises(RuntimeError, match="simulated"):
            self._main(tmp_path, monkeypatch, items, generate,
                       lambda outfile, **kw: outfile, merge=True)
        assert list(tmp_path.glob("*.tif")) == []

    def test_a_successful_run_keeps_everything(self, tmp_path, monkeypatch):
        items = live_items()[:2]

        def generate(item, algorithm, algorithm_name, output_dir, nodata, cloud_mask):
            return self._write(item, algorithm_name, output_dir, cloud_mask)

        self._main(tmp_path, monkeypatch, items, generate,
                   lambda outfile, **kw: outfile, cloud_mask=True)
        assert len(list(tmp_path.glob("*.tif"))) == 2
        assert not (tmp_path / "_cloud_masks").exists()
