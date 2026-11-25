"""Pins the pixel values `process_landsat_odr` writes.

The Landsat STAC ("ODR") module was mirrored from the Sentinel-2 one and
inherited conventions the repo had already moved away from for Landsat
(.clinerules.md rule 64). Each class below is a defect that shipped in the first
version of the module, reproduced on synthetic rasters with the real functions:

  * The catalog's `min: 0` for NDVI and NBR -- a display range -- was applied as
    a VALIDITY range, so every negative NDVI (water, snow, most urban) and every
    negative NBR (the burn scar itself) was written as nodata.
  * Reflectances were not clamped to the USGS valid range [0, 1]. LaSRC returns
    slightly negative NIR/SWIR over clear water, so NDWI and MNDWI exceeded 1
    and those pixels became nodata holes -- in the products used to map floods.
  * A missing raster:bands scale/offset silently became scale 1 / offset 0, and
    the index was then computed on raw DN.
  * The search had no platform filter, so a pre-2024 window also processed
    Landsat 7 SLC-off scenes as "Landsat 8/9".
  * The dominant CRS of a merge was chosen from a set, i.e. by hash seed.
  * A valid composite pixel darker than the stretch floor came out as 0, the
    fill marker: transparent in the alpha band, a "hole" in a merge.

Constants are the ones a live Earth Search `landsat-c2-l2` item carries (scale
2.75e-05, offset -0.2, nodata 0 on every reflectance band).
"""

import datetime as dt

import numpy as np
import pytest

rio = pytest.importorskip("rasterio")
pytest.importorskip("pystac_client")
from rasterio.session import DummySession  # noqa: E402
from rasterio.transform import from_origin  # noqa: E402

from landsat import landsat89_functions as legacy  # noqa: E402
from landsat import landsat_odr_functions as odr  # noqa: E402
from landsat import process_landsat_odr as proc  # noqa: E402

SCALE, OFFSET = 2.75e-05, -0.2
NODATA = -9999.0


def dn(reflectance):
    """The DN that Collection 2 Level-2 stores for a reflectance."""
    return int(round((reflectance - OFFSET) / SCALE))


class FakeAsset(dict):
    """A STAC asset in the dict form get_asset_metadata accepts."""

    def __init__(self, href, scale=SCALE, offset=OFFSET, nodata=0):
        band = {"nodata": nodata}
        if scale is not None:
            band["scale"] = scale
        if offset is not None:
            band["offset"] = offset
        super().__init__({"href": href, "raster:bands": [band]})


class FakeItem:
    def __init__(self, assets, item_id="LC09_L2SP_022040_20240919_02_T1"):
        self.id = item_id
        self.datetime = dt.datetime(2024, 9, 19, 16, 32, 22, tzinfo=dt.timezone.utc)
        self.properties = {"gsd": 30, "platform": "landsat-9"}
        self.assets = assets


@pytest.fixture(autouse=True)
def no_aws(monkeypatch):
    """Reads go through `rio.Env(_session())`, a requester-pays AWS session.
    The rasters here are local files; never touch the credential chain."""
    monkeypatch.setattr(odr, "_session", lambda unsigned=False: DummySession())


def write_band(path, values, dtype="uint16", crs="EPSG:32616", x0=400000):
    arr = np.asarray(values, dtype=dtype)
    if arr.ndim == 1:
        arr = arr[np.newaxis, :]
    with rio.open(path, "w", driver="GTiff", count=1, dtype=dtype, height=arr.shape[0],
                  width=arr.shape[1], crs=crs,
                  transform=from_origin(x0, 4000000, 30, 30)) as dst:
        dst.write(arr, 1)
    return str(path)


def scene(tmp_path, cloud_bits=None, **reflectances):
    """A FakeItem whose bands hold the given per-pixel reflectances (None = fill)."""
    assets = {}
    width = None
    for name, values in reflectances.items():
        width = len(values)
        dns = [0 if v is None else dn(v) for v in values]
        assets[name] = FakeAsset(write_band(tmp_path / f"{name}.tif", dns))
    qa = np.zeros(width, np.uint16) if cloud_bits is None else np.asarray(cloud_bits, np.uint16)
    assets["qa_pixel"] = FakeAsset(write_band(tmp_path / "qa.tif", qa), scale=None, offset=None,
                                   nodata=1)
    return FakeItem(assets)


def run_index(tmp_path, product, item, cloud_mask=False):
    algorithm = odr.get_algorithm("index", product, proc.DEFAULT_ALGORITHM_FILE)
    out = odr.generate_index(item=item, algorithm=algorithm, algorithm_name=product,
                             output_dir=str(tmp_path / "out"), nodata=proc.INDEX_NODATA,
                             cloud_mask=cloud_mask)
    with rio.open(out) as src:
        assert src.nodata == NODATA and src.dtypes[0] == "float32"
        return src.read(1)[0]


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def catalog():
    return odr.load_algorithms(proc.DEFAULT_ALGORITHM_FILE)


class TestCatalog:

    def test_the_default_does_not_depend_on_the_working_directory(self, tmp_path, monkeypatch,
                                                                    catalog):
        """The library default was "algorithms-landsat.json", relative to
        wherever the caller happened to be."""
        monkeypatch.chdir(tmp_path)
        assert odr.load_algorithms() == catalog
        assert odr.get_algorithm("index", "ndvi") == catalog["index"][0]["ndvi"]

    def test_index_ranges_are_validity_ranges(self, catalog):
        """`generate_index` drops whatever falls outside min..max, so the range
        must be the index's whole physical range. NDVI and NBR shipped with
        `min: 0`, a colour-ramp setting."""
        for name, entry in catalog["index"][0].items():
            assert (entry["min"], entry["max"]) == (-1, 1), name

    def test_band_pairs(self, catalog):
        index = catalog["index"][0]
        assert index["ndvi"]["assets"] == ["nir08", "red"]
        assert index["ndwi"]["assets"] == ["green", "nir08"]
        assert index["mndwi"]["assets"] == ["green", "swir16"]
        assert index["nbr"]["assets"] == ["nir08", "swir22"]
        assert index["evi"]["assets"] == ["nir08", "red", "blue"]
        assert index["evi"]["formula"] == "evi"

    def test_composite_band_order(self, catalog):
        composite = catalog["composite"][0]
        assert composite["true_color"]["assets"] == ["red", "green", "blue"]
        assert composite["color_infrared"]["assets"] == ["nir08", "red", "green"]
        assert composite["natural_color"]["assets"] == ["swir16", "nir08", "red"]
        assert composite["swir"]["assets"] == ["swir22", "nir08", "red"]

    def test_every_entry_resolves_to_a_known_formula(self, catalog):
        for name, entry in catalog["index"][0].items():
            formula = odr._INDEX_FORMULAS[entry.get("formula", "normalized_difference")]
            assert len(entry["assets"]) == formula["assets"], name


# ---------------------------------------------------------------------------
# DN -> reflectance
# ---------------------------------------------------------------------------

class TestReadAlgorithmBand:

    def test_conversion_is_additive_and_fill_is_nan(self, tmp_path):
        path = write_band(tmp_path / "b.tif", [dn(0.45), 0, dn(0.0), 1])
        band, _ = odr.read_algorithm_band(FakeAsset(path), item_gsd=30)
        assert band[0, 0] == pytest.approx(0.45, abs=1e-4)
        assert np.isnan(band[0, 1])                          # DN 0 is fill, not -0.2
        assert band[0, 2] == pytest.approx(0.0, abs=1e-4)
        assert band[0, 3] == pytest.approx(-0.2 + SCALE)     # DN 1 is real, and negative

    @pytest.mark.parametrize("missing", ["scale", "offset"])
    def test_missing_scale_or_offset_raises(self, tmp_path, missing):
        """Guessing 1 / 0 computes the index on raw DN: finite, plausible, wrong."""
        path = write_band(tmp_path / "b.tif", [dn(0.45)])
        with pytest.raises(ValueError, match="scale/offset"):
            odr.read_algorithm_band(FakeAsset(path, **{missing: None}), item_gsd=30)

    def test_raw_dn_read_needs_no_scale(self, tmp_path):
        """Composites stretch raw DN (apply_scale=False) and QA_PIXEL has none."""
        path = write_band(tmp_path / "b.tif", [12345])
        band, _ = odr.read_algorithm_band(FakeAsset(path, scale=None, offset=None),
                                          item_gsd=30, apply_scale=False)
        assert band[0, 0] == 12345


# ---------------------------------------------------------------------------
# Indices
# ---------------------------------------------------------------------------

class TestNormalizedDifference:

    def test_matches_hand_calculation(self):
        values, valid = odr.calculate_normalized_difference(
            [np.array([0.45]), np.array([0.04])])
        assert valid[0] and values[0] == pytest.approx((0.45 - 0.04) / (0.45 + 0.04))

    def test_clear_water_is_one_not_out_of_range(self):
        """green 0.02, NIR -0.005: unclamped this is 1.67, which was then
        written as nodata."""
        values, valid = odr.calculate_normalized_difference(
            [np.array([0.02]), np.array([-0.005])])
        assert valid[0] and values[0] == 1.0

    def test_two_negative_bands_are_nodata_not_a_made_up_value(self):
        """Unclamped, (-0.002 - -0.005) / (-0.002 + -0.005) is a finite -0.43."""
        _, valid = odr.calculate_normalized_difference(
            [np.array([-0.002]), np.array([-0.005])])
        assert not valid[0]

    def test_nan_input_is_invalid(self):
        _, valid = odr.calculate_normalized_difference(
            [np.array([np.nan, 0.3]), np.array([0.2, np.nan])])
        assert not valid.any()

    def test_result_never_leaves_minus_one_to_one(self):
        rng = np.random.default_rng(0)
        a = rng.uniform(-0.2, 1.5, 5000).astype(np.float32)
        b = rng.uniform(-0.2, 1.5, 5000).astype(np.float32)
        values, valid = odr.calculate_normalized_difference([a, b])
        assert np.abs(values[valid]).max() <= 1.0

    def test_agrees_with_the_legacy_landsat_pipeline(self):
        """One sensor, two pipelines: the same reflectances must give the same
        index, pixel for pixel (.clinerules.md rule 64)."""
        rng = np.random.default_rng(1)
        a = rng.uniform(-0.1, 1.2, 5000).astype(np.float32)
        b = rng.uniform(-0.1, 1.2, 5000).astype(np.float32)
        values, valid = odr.calculate_normalized_difference([a, b])
        ours = np.where(valid, values, NODATA)
        theirs = legacy.normalized_difference(a, b, np.zeros(a.shape, bool))
        np.testing.assert_allclose(ours, theirs, rtol=0, atol=1e-6)


class TestEvi:

    def test_matches_hand_calculation(self):
        nir, red, blue = 0.45, 0.04, 0.03
        expected = 2.5 * (nir - red) / (nir + 6 * red - 7.5 * blue + 1)
        values, valid = odr.calculate_evi([np.array([nir]), np.array([red]), np.array([blue])])
        assert valid[0] and values[0] == pytest.approx(expected, rel=1e-5)

    def test_inputs_are_clamped(self):
        clamped, _ = odr.calculate_evi([np.array([0.0]), np.array([0.0]), np.array([0.02])])
        negative, _ = odr.calculate_evi([np.array([-0.005]), np.array([-0.002]), np.array([0.02])])
        assert negative[0] == clamped[0]

    def test_denominator_through_zero_is_invalid_not_inf(self):
        # nir + 6*red - 7.5*blue + 1 == 0  ->  blue = (1 + nir + 6*red) / 7.5
        nir, red = 0.1, 0.05
        blue = (1 + nir + 6 * red) / 7.5
        values, valid = odr.calculate_evi([np.array([nir]), np.array([red]), np.array([blue])])
        assert not valid[0] and np.isfinite(values[0])


# Pixel classes, left to right. Reflectances are typical LaSRC values.
#                 vegetation  clear water  turbid water  burn scar  fill
GREEN = [0.070, 0.020, 0.080, 0.050, None]
RED = [0.040, 0.010, 0.070, 0.080, None]
NIR = [0.450, -0.005, 0.030, 0.120, None]
SWIR16 = [0.200, -0.008, 0.010, 0.250, None]
SWIR22 = [0.100, -0.006, 0.005, 0.250, None]
BLUE = [0.030, 0.030, 0.060, 0.040, None]


@pytest.fixture
def landscape(tmp_path):
    return scene(tmp_path, green=GREEN, red=RED, nir08=NIR, swir16=SWIR16,
                 swir22=SWIR22, blue=BLUE)


class TestGenerateIndex:
    """The real generate_index + the shipped catalog, on the rasters above."""

    def test_negative_ndvi_is_written_not_dropped(self, tmp_path, landscape):
        ndvi = run_index(tmp_path, "ndvi", landscape)
        assert ndvi[0] == pytest.approx((0.45 - 0.04) / (0.45 + 0.04), abs=1e-4)
        assert ndvi[2] == pytest.approx((0.03 - 0.07) / (0.03 + 0.07), abs=1e-3)   # -0.4
        assert ndvi[1] == -1.0                 # clear water: NIR clamps to 0
        assert ndvi[4] == NODATA

    def test_the_burn_scar_survives_in_nbr(self, tmp_path, landscape):
        """NBR is negative exactly where the fire was. With `min: 0` the
        product had a hole there."""
        nbr = run_index(tmp_path, "nbr", landscape)
        assert nbr[3] == pytest.approx((0.12 - 0.25) / (0.12 + 0.25), abs=1e-3)    # -0.35
        assert nbr[3] < 0
        assert nbr[0] > 0

    @pytest.mark.parametrize("product", ["ndwi", "mndwi"])
    def test_clear_water_reads_one_in_the_water_indices(self, tmp_path, landscape, product):
        index = run_index(tmp_path, product, landscape)
        assert index[1] == 1.0
        assert index[2] > 0                    # turbid water still positive
        assert index[0] < 0                    # vegetation still negative
        assert index[4] == NODATA

    @pytest.mark.parametrize("product", ["ndvi", "ndwi", "mndwi", "evi"])
    def test_only_fill_is_nodata(self, tmp_path, landscape, product):
        index = run_index(tmp_path, product, landscape)
        assert (index == NODATA).tolist() == [False, False, False, False, True]
        assert np.abs(index[:4]).max() <= 1.0

    def test_evi_above_one_is_clipped_not_dropped(self, tmp_path):
        """EVI really does exceed 1 over dense canopy. Dropping those pixels
        would punch holes in the healthiest vegetation, so EVI is the one
        index that clips (as landsat89_functions.genEvi does)."""
        nir, red, blue = 0.90, 0.05, 0.10
        assert 2.5 * (nir - red) / (nir + 6 * red - 7.5 * blue + 1) > 1.4
        item = scene(tmp_path, nir08=[nir], red=[red], blue=[blue])
        assert run_index(tmp_path, "evi", item).tolist() == [1.0]

    def test_zero_over_zero_is_nodata(self, tmp_path, landscape):
        """NBR over clear water: NIR and SWIR2 are both below zero, both clamp
        to 0, and 0/0 has no value. The only non-fill case that is nodata --
        the legacy pipeline does the same."""
        nbr = run_index(tmp_path, "nbr", landscape)
        assert (nbr == NODATA).tolist() == [False, True, False, False, True]

    def test_cloud_mask_sets_flagged_pixels_to_nodata(self, tmp_path):
        # QA_PIXEL bits: 1 dilated cloud, 2 cirrus, 3 cloud, 4 shadow are masked;
        # 0 fill, 5 snow, 6 clear, 7 water are not.
        bits = [1 << 1, 1 << 2, 1 << 3, 1 << 4, 1 << 5, 1 << 6, 1 << 7, 0]
        item = scene(tmp_path, cloud_bits=bits, nir08=[0.45] * 8, red=[0.04] * 8)
        ndvi = run_index(tmp_path, "ndvi", item, cloud_mask=True)
        assert (ndvi == NODATA).tolist() == [True, True, True, True, False, False, False, False]


# ---------------------------------------------------------------------------
# Composites
# ---------------------------------------------------------------------------

def run_composite(tmp_path, item, cloud_mask=False):
    algorithm = odr.get_algorithm("composite", "true_color", proc.DEFAULT_ALGORITHM_FILE)
    out = odr.generate_composite(item=item, algorithm=algorithm, algorithm_name="true_color",
                                 output_dir=str(tmp_path / "out"), cloud_mask=cloud_mask)
    sidecar = [odr.cloud_mask_sidecar_path(out)] if cloud_mask else None
    odr.finalize_composite_rgba(out, mask_paths=sidecar, gray=odr.CLOUD_GRAY)
    with rio.open(out) as src:
        assert src.count == 4 and src.nodata is None
        assert src.colorinterp[3] == rio.enums.ColorInterp.alpha
        return src.read()[:, 0, :]


class TestComposite:

    def test_fill_is_transparent_and_everything_else_opaque(self, tmp_path):
        item = scene(tmp_path, red=[0.04, None], green=[0.07, None], blue=[0.03, None])
        rgba = run_composite(tmp_path, item)
        assert rgba[3].tolist() == [255, 0]
        assert rgba[:3, 1].tolist() == [0, 0, 0]

    def test_a_valid_pixel_below_the_stretch_floor_stays_opaque(self, tmp_path):
        """Reflectance -0.07 is DN 4727, under COMPOSITE_LOG_LOW (5000), so it
        stretches to 0 -- the value reserved for fill. It was published
        transparent; it is a real (deep-shadow) observation."""
        assert dn(-0.07) < odr.COMPOSITE_LOG_LOW
        item = scene(tmp_path, red=[-0.07], green=[-0.07], blue=[-0.07])
        rgba = run_composite(tmp_path, item)
        assert rgba[:, 0].tolist() == [1, 1, 1, 255]

    def test_stretch_is_monotonic_and_stays_in_range(self, tmp_path):
        ramp = [0.0, 0.05, 0.1, 0.3, 0.6, 0.9, 1.2]
        item = scene(tmp_path, red=ramp, green=ramp, blue=ramp)
        red = run_composite(tmp_path, item)[0].astype(int)
        assert (np.diff(red) >= 0).all() and red[0] >= 1 and red[-1] == 255

    def test_masked_cloud_is_opaque_gray(self, tmp_path):
        item = scene(tmp_path, cloud_bits=[1 << 3, 0], red=[0.6, 0.04],
                     green=[0.6, 0.07], blue=[0.6, 0.03])
        rgba = run_composite(tmp_path, item, cloud_mask=True)
        assert rgba[:, 0].tolist() == [odr.CLOUD_GRAY] * 3 + [255]
        assert rgba[3, 1] == 255 and rgba[0, 1] != odr.CLOUD_GRAY


# ---------------------------------------------------------------------------
# Search and merge plumbing
# ---------------------------------------------------------------------------

class _FakeCatalog:
    def __init__(self):
        self.kwargs = None

    def search(self, **kwargs):
        self.kwargs = kwargs
        return self

    def items(self):
        return iter(())

    def item_collection_as_dict(self):
        return {"type": "FeatureCollection", "features": []}


class TestSearch:

    @pytest.mark.parametrize("search", ["search_landsat", "search_landsat_with_catalog"])
    def test_only_landsat_8_and_9_are_searched(self, monkeypatch, search):
        """`landsat-c2-l2` also holds Landsat 4, 5 and 7. A 2021 window returned
        four LE07 (SLC-off) scenes beside four LC08 ones."""
        catalog = _FakeCatalog()
        monkeypatch.setattr(odr, "connect_to_stac", lambda url: catalog)
        getattr(odr, search)("2021-09-01", "2021-09-12", [-91.5, 29.5, -90.5, 30.5],
                             cloud_cover=40)
        assert catalog.kwargs["collections"] == ["landsat-c2-l2"]
        assert catalog.kwargs["query"]["platform"] == {"in": ["landsat-8", "landsat-9"]}
        assert catalog.kwargs["query"]["eo:cloud_cover"] == {"lt": 40}


class TestMergeProducts:

    def _two_zone_inputs(self, tmp_path):
        # Either side of the UTM 15/16 boundary (90 W), one scene per zone:
        # a tie for "most common CRS".
        a = write_band(tmp_path / "a.tif", np.full((20, 20), 1.0), dtype="float32",
                       crs="EPSG:32615", x0=760000)
        b = write_band(tmp_path / "b.tif", np.full((20, 20), 2.0), dtype="float32",
                       crs="EPSG:32616", x0=240000)
        return a, b

    def test_a_crs_tie_is_not_settled_by_set_order(self, tmp_path):
        """The dominant CRS was `max(set(crs_list), key=crs_list.count)`. On
        a tie that returns whichever CRS the set yields first, and set order
        follows the hash seed: the same job came out in one UTM zone or the
        other from run to run. A tie now goes to the first CRS in WKT order.

        Comparing two input orders inside one process cannot show this (both
        see the same set order), so the pair of zones used here is chosen as
        one the old rule gets wrong in THIS process, whatever its seed."""
        for zone in range(1, 60):
            a = write_band(tmp_path / f"a{zone}.tif", np.full((20, 20), 1.0), dtype="float32",
                           crs=f"EPSG:{32600 + zone}", x0=760000)
            b = write_band(tmp_path / f"b{zone}.tif", np.full((20, 20), 2.0), dtype="float32",
                           crs=f"EPSG:{32601 + zone}", x0=240000)
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
        """The tie-break sorts by WKT, and an ungeoreferenced raster has no
        CRS object to ask for one."""
        a = write_band(tmp_path / "a.tif", np.full((4, 4), 1.0), dtype="float32", crs=None)
        b = write_band(tmp_path / "b.tif", np.full((4, 4), 2.0), dtype="float32", crs=None,
                       x0=400120)
        out = odr.merge_products([a, b], str(tmp_path / "merged.tif"))
        with rio.open(out) as merged:
            assert merged.crs is None and merged.width == 8

    def test_sources_are_never_modified(self, tmp_path):
        """Rules 18 / 26: a mismatched-CRS input is reprojected into a temp
        dir, never rewritten in place."""
        a, b = self._two_zone_inputs(tmp_path)
        before = {p: open(p, "rb").read() for p in (a, b)}
        odr.merge_products([a, b], str(tmp_path / "m.tif"))
        assert {p: open(p, "rb").read() for p in (a, b)} == before

    def test_a_single_input_is_copied(self, tmp_path):
        a, _ = self._two_zone_inputs(tmp_path)
        out = odr.merge_products([a], str(tmp_path / "one.tif"))
        with rio.open(out) as src:
            assert src.crs.to_epsg() == 32615 and (src.read(1) == 1.0).all()
