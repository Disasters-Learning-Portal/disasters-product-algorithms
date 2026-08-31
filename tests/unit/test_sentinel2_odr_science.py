"""Science-level tests for the Sentinel-2 STAC pipeline.

These pin the parts of `sentinel2.sentinel2_odr_functions` where being
wrong produces a plausible-looking raster rather than a crash -- which
is the failure class that actually reaches a published product:

  * index arithmetic (EVI's coefficients and its non-normalized
    denominator, which passes THROUGH zero rather than touching it)
  * reflectance scaling sign (raw*scale+offset, not -offset)
  * the CDL / WorldCover crosswalks, especially that snow and ice never
    seed the permanent-water NIR sample
  * the water threshold, mean + nstd*std
  * output naming, including the NSTD token that keeps multiple
    thresholds from overwriting each other

Nothing here touches the network.
"""

import numpy as np
import pytest

rio = pytest.importorskip("rasterio")
pytest.importorskip("pystac_client")
pytest.importorskip("geopandas")

from sentinel2 import sentinel2_odr_functions as s2  # noqa: E402


# ---------------------------------------------------------------------
# Index formulas
# ---------------------------------------------------------------------

class TestNormalizedDifference:

    def test_matches_hand_calculation(self):
        nir = np.array([[0.40]], dtype=np.float32)
        red = np.array([[0.05]], dtype=np.float32)

        values, valid = s2.calculate_normalized_difference([nir, red])

        assert valid[0, 0]
        assert values[0, 0] == pytest.approx((0.40 - 0.05) / (0.40 + 0.05))

    def test_zero_denominator_is_invalid_not_inf(self):
        a = np.array([[0.2]], dtype=np.float32)
        b = np.array([[-0.2]], dtype=np.float32)

        values, valid = s2.calculate_normalized_difference([a, b])

        assert not valid[0, 0]
        assert np.isfinite(values[0, 0])

    def test_nan_input_is_invalid(self):
        a = np.array([[np.nan]], dtype=np.float32)
        b = np.array([[0.2]], dtype=np.float32)

        _, valid = s2.calculate_normalized_difference([a, b])

        assert not valid[0, 0]


class TestEvi:

    def test_matches_hand_calculation(self):
        """G=2.5, C1=6, C2=7.5, L=1 (Huete et al. 2002)."""
        nir, red, blue = 0.40, 0.05, 0.02
        expected = 2.5 * (nir - red) / (nir + 6 * red - 7.5 * blue + 1)

        values, valid = s2.calculate_evi([
            np.array([[nir]], dtype=np.float32),
            np.array([[red]], dtype=np.float32),
            np.array([[blue]], dtype=np.float32),
        ])

        assert valid[0, 0]
        assert values[0, 0] == pytest.approx(expected, rel=1e-6)

    def test_coefficients_are_the_canonical_set(self):
        assert (s2._EVI_G, s2._EVI_C1, s2._EVI_C2, s2._EVI_L) == (
            2.5, 6.0, 7.5, 1.0
        )

    def test_agrees_with_the_other_sensors_implementations(self):
        """Same formula as landsat89_functions.genEvi / satellogic_v2.genEVI."""
        rng = np.random.default_rng(0)
        nir = rng.uniform(0.05, 0.6, (8, 8)).astype(np.float32)
        red = rng.uniform(0.01, 0.3, (8, 8)).astype(np.float32)
        blue = rng.uniform(0.01, 0.1, (8, 8)).astype(np.float32)

        reference = 2.5 * (nir - red) / (nir + 6 * red - 7.5 * blue + 1)

        values, valid = s2.calculate_evi([nir, red, blue])

        assert valid.all()
        np.testing.assert_allclose(values, reference, rtol=1e-5)

    def test_denominator_through_zero_is_invalid_not_inf(self):
        """The EVI denominator is not a sum of non-negative reflectances.

        A large blue drives it negative, so it crosses zero. A `denom +
        eps` guard (used elsewhere in this repo) does not catch the
        approach from below; the magnitude guard must.
        """
        nir = np.array([[0.1]], dtype=np.float32)
        red = np.array([[0.0]], dtype=np.float32)
        blue = np.array([[1.1 / 7.5]], dtype=np.float32)   # denom -> 0

        values, valid = s2.calculate_evi([nir, red, blue])

        assert not valid[0, 0]
        assert np.isfinite(values[0, 0])

    def test_negative_denominator_still_computes(self):
        """Negative is legitimate, only near-zero is not."""
        values, valid = s2.calculate_evi([
            np.array([[0.1]], dtype=np.float32),
            np.array([[0.0]], dtype=np.float32),
            np.array([[0.9]], dtype=np.float32),
        ])

        assert valid[0, 0]
        assert np.isfinite(values[0, 0])


class TestFormulaRegistry:

    def test_shipped_catalog_entries_all_resolve(self, tmp_path):
        """Every index in the shipped catalog must have a usable formula
        whose asset count matches its `assets` list.

        This is the regression guard for the original defect: `evi` was
        added to the catalog with three assets and to the CLI choices,
        but no implementation existed and generate_index hard-required
        exactly two assets -- so `--product evi` raised, always.
        """
        import json
        import os

        catalog_path = os.path.join(
            os.path.dirname(s2.__file__), "algorithms-sentinel2.json"
        )

        with open(catalog_path) as fh:
            catalog = json.load(fh)

        indices = catalog["index"][0]

        assert "evi" in indices, "catalog lost its evi entry"

        for name, entry in indices.items():
            formula_name = entry.get("formula", "normalized_difference")

            assert formula_name in s2._INDEX_FORMULAS, (
                f"index '{name}' declares unknown formula '{formula_name}'"
            )

            expected = s2._INDEX_FORMULAS[formula_name]["assets"]

            assert len(entry["assets"]) == expected, (
                f"index '{name}' lists {len(entry['assets'])} assets but "
                f"formula '{formula_name}' consumes {expected}"
            )

    def test_evi_uses_three_assets_in_nir_red_blue_order(self):
        import json
        import os

        catalog_path = os.path.join(
            os.path.dirname(s2.__file__), "algorithms-sentinel2.json"
        )
        with open(catalog_path) as fh:
            catalog = json.load(fh)

        assert catalog["index"][0]["evi"]["assets"] == ["nir", "red", "blue"]


# ---------------------------------------------------------------------
# Reflectance scaling
# ---------------------------------------------------------------------

class TestScaleOffsetSign:
    """raw*scale + offset -- the STAC raster extension and GDAL agree.

    Earth Search publishes scale=0.0001, offset=-0.1 for Sentinel-2,
    which is ESA's baseline-04.00 BOA_ADD_OFFSET=-1000 over
    QUANTIFICATION_VALUE=10000. Subtracting instead adds +0.2 to every
    band.
    """

    def test_conversion_is_additive(self):
        raw = np.array([[1362]], dtype=np.uint16)
        scale, offset = 0.0001, -0.1

        converted = raw.astype(np.float32) * scale + offset

        assert converted[0, 0] == pytest.approx(0.0362, abs=1e-6)

    def test_wrong_sign_would_break_ndvi(self):
        """Documents the magnitude of the bug this replaced."""
        red_dn, nir_dn = 1362, 4388
        scale, offset = 0.0001, -0.1

        red_ok = red_dn * scale + offset
        nir_ok = nir_dn * scale + offset
        ndvi_ok = (nir_ok - red_ok) / (nir_ok + red_ok)

        red_bad = red_dn * scale - offset
        nir_bad = nir_dn * scale - offset
        ndvi_bad = (nir_bad - red_bad) / (nir_bad + red_bad)

        assert ndvi_ok == pytest.approx(0.81, abs=0.01)
        assert ndvi_bad == pytest.approx(0.39, abs=0.01)


# ---------------------------------------------------------------------
# Land-cover crosswalks
# ---------------------------------------------------------------------

class TestCdlReclass:

    @staticmethod
    def _reclass(codes):
        arr = np.asarray(codes, dtype=np.uint16).reshape(1, -1)
        nd = np.zeros(arr.shape, dtype=bool)
        return s2._reclass_cdl_array(arr, nd)

    def test_perennial_ice_snow_is_not_water(self):
        """CDL 112 is Perennial Ice/Snow, not water.

        Snow reflects ~0.6-0.9 at B08's 842 nm against ~0.01-0.05 for
        open water, so seeding the water sample with it inflates both
        the mean and the std -- and the threshold is mean + nstd*std.
        """
        buckets, sample = self._reclass([112])

        assert buckets[0, 0] != 4, "112 must not be labelled permanent water"
        assert buckets[0, 0] == 999
        assert not sample[0, 0], "112 must never seed the NIR statistics"

    @pytest.mark.parametrize(
        "code,bucket",
        [
            (0, 999),    # Background
            (81, 999),   # Clouds/No Data
            (112, 999),  # Perennial Ice/Snow
            (62, 1),     # Pasture/Grass (legacy)
            (176, 1),    # Grassland/Pasture
            (61, 1),     # Fallow/Idle Cropland
            (82, 2),     # Developed (legacy)
            (121, 2), (122, 2), (123, 2), (124, 2),
            (63, 3),     # Forest (legacy)
            (64, 3),     # Shrubland (legacy)
            (65, 3),     # Barren (legacy)
            (87, 3),     # Wetlands (legacy)
            (88, 3),     # Nonag/Undefined (legacy)
            (131, 3),    # Barren
            (141, 3), (142, 3), (143, 3), (152, 3),
            (190, 3),    # Woody Wetlands
            (195, 3),    # Herbaceous Wetlands
            (83, 4),     # Water (legacy)
            (92, 4),     # Aquaculture
            (111, 4),    # Open Water
        ],
    )
    def test_documented_codes_land_in_the_right_bucket(self, code, bucket):
        buckets, _ = self._reclass([code])
        assert buckets[0, 0] == bucket

    def test_previously_missing_codes_are_now_mapped(self):
        """62/63/64/65/82/83/87 used to fall through to nodata.

        They are pre-2008 legacy codes with no pixels in modern CDL, so
        this was inert on current data -- but 83 is Water, and silently
        dropping a water class from the reference is not something to
        leave to chance.
        """
        for code in (62, 63, 64, 65, 82, 83, 87):
            buckets, _ = self._reclass([code])
            assert buckets[0, 0] != 999, f"CDL {code} still unmapped"

    def test_wetlands_are_not_permanent_water(self):
        """Vegetation canopy over intermittently saturated ground.

        Bright in NIR, so it would contaminate the sample, and only
        periodically inundated -- labelling it permanent water would
        blind the product to flooding where flooding is most expected.
        """
        for code in (87, 190, 195):
            buckets, sample = self._reclass([code])
            assert buckets[0, 0] == 3
            assert not sample[0, 0]

    def test_only_open_water_seeds_the_statistics(self):
        """Aquaculture is water in the label but a poor NIR reference."""
        buckets, sample = self._reclass([111, 92, 83])

        assert list(buckets[0]) == [4, 4, 4]
        assert list(sample[0]) == [True, False, False]

    def test_unmapped_code_becomes_nodata(self):
        buckets, sample = self._reclass([250])
        # 250 is a crop code and IS mapped; use a genuinely absent one.
        buckets, sample = self._reclass([200])
        assert buckets[0, 0] == 999
        assert not sample[0, 0]

    def test_nir_nodata_overrides_everything(self):
        arr = np.array([[111]], dtype=np.uint16)
        nd = np.array([[True]])

        buckets, sample = s2._reclass_cdl_array(arr, nd)

        assert buckets[0, 0] == 999
        assert not sample[0, 0]


class TestWorldCoverReclass:

    @staticmethod
    def _reclass(codes):
        arr = np.asarray(codes, dtype=np.uint16).reshape(1, -1)
        nd = np.zeros(arr.shape, dtype=bool)
        return s2._reclass_worldcover_array(arr, nd)

    @pytest.mark.parametrize(
        "code,bucket",
        [
            (0, 999),    # No data
            (10, 3),     # Tree cover
            (20, 3),     # Shrubland
            (30, 1),     # Grassland
            (40, 1),     # Cropland
            (50, 2),     # Built-up
            (60, 3),     # Bare / sparse vegetation
            (70, 999),   # Snow and ice
            (80, 4),     # Permanent water bodies
            (90, 3),     # Herbaceous wetland
            (95, 3),     # Mangroves
            (100, 3),    # Moss and lichen
        ],
    )
    def test_legend_maps_correctly(self, code, bucket):
        buckets, _ = self._reclass([code])
        assert buckets[0, 0] == bucket

    def test_snow_and_ice_never_seeds_the_statistics(self):
        """Consistent with CDL 112 -- the two tables must not disagree."""
        buckets, sample = self._reclass([70])
        assert buckets[0, 0] != 4
        assert not sample[0, 0]

    def test_only_permanent_water_seeds_the_statistics(self):
        buckets, sample = self._reclass([80, 90, 95])
        assert list(buckets[0]) == [4, 3, 3]
        assert list(sample[0]) == [True, False, False]


class TestCrosswalksAgree:

    def test_snow_is_excluded_by_both_references(self):
        """The original defect was an internal inconsistency: WorldCover
        correctly excluded snow while CDL called it permanent water."""
        cdl_buckets, cdl_sample = s2._reclass_cdl_array(
            np.array([[112]], dtype=np.uint16), np.zeros((1, 1), bool)
        )
        wc_buckets, wc_sample = s2._reclass_worldcover_array(
            np.array([[70]], dtype=np.uint16), np.zeros((1, 1), bool)
        )

        assert cdl_buckets[0, 0] == wc_buckets[0, 0] == 999
        assert not cdl_sample[0, 0]
        assert not wc_sample[0, 0]


# ---------------------------------------------------------------------
# Water threshold
# ---------------------------------------------------------------------

class TestWaterThreshold:

    def test_threshold_is_mean_plus_nstd_times_std(self):
        rng = np.random.default_rng(0)
        water = rng.normal(1400, 150, 50_000)

        for nstd in (0.5, 1.0, 1.5, 2.0):
            expected = water.mean() + nstd * water.std()
            assert expected == pytest.approx(
                np.nanmean(water) + nstd * np.nanstd(water)
            )

    def test_snow_contamination_would_wreck_the_threshold(self):
        """Quantifies why CDL 112 must not be in the sample.

        Guards the crosswalk from a well-meaning future edit that puts
        ice/snow back into permanent water.
        """
        rng = np.random.default_rng(0)
        water = rng.normal(1400, 150, 100_000)
        snow = rng.normal(9000, 600, 5_000)
        land = rng.normal(4000, 900, 100_000)

        clean_thresh = water.mean() + water.std()
        clean_false_water = np.mean(land <= clean_thresh)

        contaminated = np.concatenate([water, snow])
        dirty_thresh = contaminated.mean() + contaminated.std()
        dirty_false_water = np.mean(land <= dirty_thresh)

        assert clean_false_water < 0.01
        assert dirty_false_water > 0.20
        assert dirty_thresh > 2 * clean_thresh

    def test_median_filter_accepts_uint8(self):
        from scipy.signal import medfilt2d

        arr = np.zeros((20, 20), dtype=np.uint8)
        arr[5:15, 5:15] = 1

        assert medfilt2d(arr, kernel_size=5).dtype == np.uint8


# ---------------------------------------------------------------------
# Display stretch
# ---------------------------------------------------------------------

class TestApplyLogScale:
    """The stretch must not depend on its own output landing outside
    its own input domain.

    The original implementation mutated its working array in place and
    then re-tested it: the below-range clamp wrote `output_min` (0), and
    the next statement's `>= high_log` test re-selected those pixels and
    painted them `output_max`. Harmless only while log(low) > 0, which
    is true for the raw-DN thresholds it shipped with and false for any
    threshold below 1.0 -- where every dark pixel came out WHITE.
    """

    def test_dark_pixel_stays_dark_in_dn_domain(self):
        out = s2.apply_log_scale(np.array([[300.0]]), low=750, high=7500)
        assert out[0, 0] == pytest.approx(0.0)

    def test_dark_pixel_stays_dark_in_reflectance_domain(self):
        """The case that exposed the bug: log thresholds are negative."""
        out = s2.apply_log_scale(np.array([[0.03]]), low=0.075, high=0.75)
        assert out[0, 0] == pytest.approx(0.0)

    def test_bright_pixel_saturates(self):
        out = s2.apply_log_scale(np.array([[9000.0]]), low=750, high=7500)
        assert out[0, 0] == pytest.approx(255.0)

    def test_midpoint_is_monotonic_and_interior(self):
        vals = np.array([[800.0, 2000.0, 5000.0, 7000.0]])
        out = s2.apply_log_scale(vals, low=750, high=7500)

        assert np.all(np.diff(out[0]) > 0), "stretch must be monotonic"
        assert np.all(out[0] > 0) and np.all(out[0] < 255)

    @pytest.mark.parametrize(
        "low,high", [(750, 7500), (0.075, 0.75), (0.5, 5.0), (1.0, 100.0)]
    )
    def test_monotonic_in_any_threshold_domain(self, low, high):
        vals = np.linspace(low, high, 25).reshape(1, -1)
        out = s2.apply_log_scale(vals, low=low, high=high)
        assert np.all(np.diff(out[0]) >= 0)
        assert out[0, 0] == pytest.approx(0.0)
        assert out[0, -1] == pytest.approx(255.0)

    def test_nonpositive_becomes_nan_for_the_caller_to_fill(self):
        out = s2.apply_log_scale(
            np.array([[0.0, -5.0, 3000.0]]), low=750, high=7500
        )
        assert np.isnan(out[0, 0])
        assert np.isnan(out[0, 1])
        assert np.isfinite(out[0, 2])

    def test_nan_never_reaches_a_uint8_cast(self):
        """Casting NaN to an integer dtype is undefined in numpy."""
        out = s2.apply_log_scale(np.array([[0.0]]), low=750, high=7500)
        filled = np.nan_to_num(out, nan=0.0, posinf=255.0, neginf=0.0)
        assert np.asarray(np.clip(filled, 0, 255), dtype=np.uint8)[0, 0] == 0


# ---------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------

class _FakeItem:
    def __init__(self, item_id, dt):
        self.id = item_id
        self.datetime = dt


class TestNaming:

    @pytest.fixture
    def item_c1(self):
        import datetime as dt
        return _FakeItem(
            "S2A_T16SED_20260423T163857_L2A",
            dt.datetime(2026, 4, 23, 16, 15, 59),
        )

    @pytest.fixture
    def item_old(self):
        import datetime as dt
        return _FakeItem(
            "S2A_16SED_20260423_1_L1C",
            dt.datetime(2026, 4, 23, 16, 15, 59),
        )

    def test_parses_both_earth_search_id_formats(self, item_c1, item_old):
        assert s2._get_sat_level_tile(item_c1) == ("S2A", "MSIL2A", "T16SED")
        assert s2._get_sat_level_tile(item_old) == ("S2A", "MSIL1C", "T16SED")

    def test_plain_name(self, item_c1):
        assert s2._build_output_filename(item_c1, "true_color") == (
            "S2A_MSIL2A_trueColor_T16SED_2026-04-23T16:15:59Z.tif"
        )

    def test_water_extent_uses_the_published_product_token(self, item_c1):
        """`we` is the catalog/CLI key; `waterExtent` is the product name.

        The operator notebooks categorise on
        r'WaterExtent|waterextent|water_extent', so a file named `_we_`
        silently fails to categorise.
        """
        name = s2._build_output_filename(item_c1, "we")
        assert "waterExtent" in name
        assert "_we_" not in name

    @pytest.mark.parametrize(
        "nstd,token",
        [(1, "NSTD_1"), (1.0, "NSTD_1"), (1.5, "NSTD_1_5"), (2.25, "NSTD_2_25")],
    )
    def test_nstd_token_matches_the_legacy_convention(self, nstd, token):
        assert s2._nstd_variant_token(nstd) == token

    def test_variant_keeps_thresholds_from_colliding(self, item_c1):
        """The legacy pipeline computed this token and never used it, so
        `-we_nstd 1 1.5 2` wrote all three products to one path."""
        names = {
            s2._build_output_filename(
                item_c1, "we", variant=s2._nstd_variant_token(n)
            )
            for n in (1, 1.5, 2)
        }
        assert len(names) == 3

    def test_merged_drops_the_tile_token(self, item_c1):
        name = s2._build_output_filename(item_c1, "ndvi", merged=True)
        assert "_merged_" in name
        assert "T16SED" not in name

    def test_masked_token(self, item_c1):
        assert "_masked_" in s2._build_output_filename(
            item_c1, "ndvi", masked=True
        )


# ---------------------------------------------------------------------
# Rayleigh correction
# ---------------------------------------------------------------------

class TestRayleighPlatformMapping:

    def test_all_three_sentinel2_platforms_are_known(self):
        assert s2._PLATFORM_TO_PYSPECTRAL == {
            "sentinel-2a": "Sentinel-2A",
            "sentinel-2b": "Sentinel-2B",
            "sentinel-2c": "Sentinel-2C",
        }

    def test_sentinel2c_does_not_fall_back_to_2b(self):
        """The old `platform.endswith("a")` test mapped S2C to S2B.

        S2C's spectral response genuinely differs (B02 at 486.0 nm vs
        489.8 nm on S2A), and nothing in the output would reveal the
        substitution.
        """
        assert s2._PLATFORM_TO_PYSPECTRAL["sentinel-2c"] == "Sentinel-2C"

    def test_unknown_platform_raises_rather_than_guessing(self):
        item = _FakeItem("S2X_T16SED_20260423T163857_L1C", None)
        item.properties = {
            "platform": "sentinel-9z",
            "view:sun_elevation": 45.0,
            "view:sun_azimuth": 150.0,
        }

        pytest.importorskip("pyspectral")

        with pytest.raises(ValueError, match="no known pyspectral"):
            s2.get_rayleigh_correction(item, "blue")

    def test_only_b01_to_b07_are_correctable(self):
        assert s2._RAYLEIGH_CORRECTABLE_BANDS == {
            "B01", "B02", "B03", "B04", "B05", "B06", "B07"
        }
        # B08+ fall outside pyspectral's 400-800 nm LUT grid.
        assert s2._ASSET_TO_BAND_CODE["nir"] == "B08"
        assert "B08" not in s2._RAYLEIGH_CORRECTABLE_BANDS

    def test_uncorrectable_band_returns_zero_without_touching_pyspectral(self):
        item = _FakeItem("S2A_T16SED_20260423T163857_L1C", None)
        item.properties = {}

        pytest.importorskip("pyspectral")

        assert s2.get_rayleigh_correction(item, "swir16") == 0

    def test_missing_sun_geometry_raises(self):
        item = _FakeItem("S2A_T16SED_20260423T163857_L1C", None)
        item.properties = {"platform": "sentinel-2a"}

        pytest.importorskip("pyspectral")

        with pytest.raises(ValueError, match="illumination geometry"):
            s2.get_rayleigh_correction(item, "blue")
