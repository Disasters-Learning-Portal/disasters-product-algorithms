"""Tests for shared_utils.file_naming module."""

import pytest


class TestExtractDatetimeFromFilename:
    """Tests for extract_datetime_from_filename(filename)."""

    def test_landsat_8digit_date(self):
        from shared_utils.file_naming import extract_datetime_from_filename
        matched, granularity = extract_datetime_from_filename(
            "LC08_trueColor_20250922_185617_046028.tif"
        )
        assert matched == "20250922"
        assert granularity == "day"

    def test_sentinel_8digit_date(self):
        from shared_utils.file_naming import extract_datetime_from_filename
        matched, granularity = extract_datetime_from_filename(
            "S2B_MSIL2A_colorInfrared_20251111_161419.tif"
        )
        assert matched == "20251111"
        assert granularity == "day"

    def test_no_date(self):
        from shared_utils.file_naming import extract_datetime_from_filename
        assert extract_datetime_from_filename("some_file_no_date.tif") == (None, None)

    def test_date_at_start_of_stem(self):
        from shared_utils.file_naming import extract_datetime_from_filename
        matched, granularity = extract_datetime_from_filename("20230415_data.tif")
        assert matched == "20230415"
        assert granularity == "day"

    def test_iso_hyphenated_date(self):
        from shared_utils.file_naming import extract_datetime_from_filename
        matched, granularity = extract_datetime_from_filename("noaa_2025-01-11_thermal.tif")
        assert matched == "2025-01-11"
        assert granularity == "day"

    def test_iso_full_timestamp_with_z(self):
        from shared_utils.file_naming import extract_datetime_from_filename
        matched, granularity = extract_datetime_from_filename(
            "sentinel_2025-01-11T19:46:16Z_red.tif"
        )
        assert matched == "2025-01-11T19:46:16Z"
        assert granularity == "hour"

    def test_compact_datetime(self):
        from shared_utils.file_naming import extract_datetime_from_filename
        matched, granularity = extract_datetime_from_filename(
            "sentinel_20250111T194616Z_red.tif"
        )
        assert matched == "20250111T194616Z"
        assert granularity == "hour"

    def test_iso_date_with_hour(self):
        from shared_utils.file_naming import extract_datetime_from_filename
        matched, granularity = extract_datetime_from_filename("planet_2025-01-11T19_blue.tif")
        assert matched == "2025-01-11T19"
        assert granularity == "hour"

    def test_mixed_iso_date_with_compact_time(self):
        """Hyphenated date + compact time (the form vendor SkySat deliveries use).

        Regression: with no entry of its own, the less-specific `YYYY-MM-DDTHH`
        pattern matched a PREFIX of this stamp, so the match ended mid-token
        ("2026-08-12T12") and left "3721Z" welded to the product name.
        """
        from shared_utils.file_naming import extract_datetime_from_filename
        matched, granularity = extract_datetime_from_filename(
            "SkySat_SR_TrueColor_2026-08-12T123721Z"
        )
        assert matched == "2026-08-12T123721Z"
        assert granularity == "hour"

    def test_mixed_iso_without_trailing_z(self):
        from shared_utils.file_naming import extract_datetime_from_filename
        matched, granularity = extract_datetime_from_filename("skysat_2026-08-12T123721_tc.tif")
        assert matched == "2026-08-12T123721"
        assert granularity == "hour"


class TestCategorizeFile:
    """Tests for categorize_file(filename, categories)."""

    CATEGORIES = {
        r'trueColor|truecolor|true_color': 'Sentinel-2/trueColor',
        r'colorInfrared|colorIR|color_infrared': 'Sentinel-2/colorIR',
        r'naturalColor|natural_color': 'Sentinel-2/naturalColor',
        r'wood': 'GAIA',
    }

    def test_matches_truecolor(self):
        from shared_utils.file_naming import categorize_file
        assert categorize_file("S2B_trueColor_20250101.tif", self.CATEGORIES) == "Sentinel-2/trueColor"

    def test_case_insensitive(self):
        from shared_utils.file_naming import categorize_file
        assert categorize_file("S2B_TRUECOLOR_20250101.tif", self.CATEGORIES) == "Sentinel-2/trueColor"

    def test_first_match_wins(self):
        from shared_utils.file_naming import categorize_file
        # 'trueColor' is the first dict entry; even if a filename theoretically
        # matches multiple, the first one in dict order is returned.
        assert categorize_file("trueColor_naturalColor.tif", self.CATEGORIES) == "Sentinel-2/trueColor"

    def test_uncategorized(self):
        from shared_utils.file_naming import categorize_file
        assert categorize_file("S2B_unknown_20250101.tif", self.CATEGORIES) == "uncategorized"

    def test_gaia_pattern(self):
        from shared_utils.file_naming import categorize_file
        assert categorize_file("low-durability-wood-framed-1.tif", self.CATEGORIES) == "GAIA"


class TestCreateOutputFilename:
    """Tests for create_output_filename(original_path, event_name, ...)."""

    def test_basic_8digit_date_is_hyphenated(self):
        from shared_utils.file_naming import create_output_filename
        result = create_output_filename(
            "/path/LC08_trueColor_20250922_185617.tif",
            "202509_Flood_WA"
        )
        assert result.startswith("202509_Flood_WA_")
        # 8-digit YYYYMMDD must be normalized to YYYY-MM-DD in the embedded date.
        assert "2025-09-22" in result
        assert result.endswith("_day.tif")

    def test_no_double_underscores(self):
        from shared_utils.file_naming import create_output_filename
        result = create_output_filename(
            "/path/LC08_trueColor_20250922.tif",
            "Event"
        )
        # Stem-strip leaves no consecutive underscores around the removed date.
        assert "__" not in result

    def test_no_date_falls_back_to_day_suffix(self):
        from shared_utils.file_naming import create_output_filename
        result = create_output_filename(
            "/path/umbra_low-durability-wood-framed-1.tif",
            "Event"
        )
        assert result == "Event_umbra_low-durability-wood-framed-1_day.tif"

    def test_hour_granularity_preserved_for_iso_timestamps(self):
        from shared_utils.file_naming import create_output_filename
        result = create_output_filename(
            "sentinel_20250111T194616Z_red.tif",
            "Event"
        )
        # Hour-granularity datetimes are NOT hyphenated; they keep their raw form
        # and use the _hour suffix.
        assert "20250111T194616Z" in result
        assert result.endswith("_hour.tif")

    def test_passthrough_category_uses_no_change(self):
        from shared_utils.file_naming import create_output_filename
        categories = {r'earlylook': 'AVIRIS'}
        result = create_output_filename(
            "ang20250101_earlylook_strip.tif",
            "Event",
            categories=categories,
        )
        # AVIRIS is the default passthrough — keep the original stem verbatim.
        assert result == "Event_ang20250101_earlylook_strip.tif"


class TestAlreadyNamedFilesAreAFixedPoint:
    """Regression: sources that already carry the event prefix and/or a
    trailing datetime stamp must not accumulate a second copy of either.

    The observed failure (SkySat delivery, event 202607_Fire_OR):
        in : 202607_Fire_OR_SkySat_SR_TrueColor_2026-08-12T153802Z.tif
        out: 202607_Fire_OR_202607_Fire_OR_SkySat_SR_TrueColor_2026-08-12T153802Z_day.tif
    """

    EVENT = "202607_Fire_OR"
    STAMPED = "202607_Fire_OR_SkySat_SR_TrueColor_2026-08-12T153802Z.tif"

    def test_event_prefix_is_not_doubled(self):
        from shared_utils.file_naming import create_output_filename
        result = create_output_filename(self.STAMPED, self.EVENT)
        assert not result.startswith(f"{self.EVENT}_{self.EVENT}")
        assert result.count(self.EVENT) == 1

    def test_iso_zulu_stem_keeps_its_stamp_and_gains_no_day_suffix(self):
        from shared_utils.file_naming import create_output_filename
        result = create_output_filename(self.STAMPED, self.EVENT)
        # Z is the completion marker (cog_utils convention) - no _day alongside it.
        assert result == self.STAMPED
        assert "_day" not in result

    def test_missing_event_prefix_is_still_added_to_a_stamped_name(self):
        from shared_utils.file_naming import create_output_filename
        result = create_output_filename(
            "SkySat_SR_TrueColor_2026-08-12T153802Z.tif", self.EVENT
        )
        assert result == self.STAMPED

    def test_rerun_over_own_day_output_does_not_stack_suffixes(self):
        from shared_utils.file_naming import create_output_filename
        first = create_output_filename("SkySat_SR_TrueColor_20260812.tif", self.EVENT)
        assert first == "202607_Fire_OR_SkySat_SR_TrueColor_2026-08-12_day.tif"
        assert create_output_filename(first, self.EVENT) == first  # no _day_day

    def test_rerun_over_own_hour_output_does_not_stack_suffixes(self):
        from shared_utils.file_naming import create_output_filename
        first = create_output_filename("sentinel_20250111T194616Z_red.tif", "Event")
        assert first.endswith("_hour.tif")
        assert create_output_filename(first, "Event") == first  # no _hour_hour

    @pytest.mark.parametrize("name", [
        "202607_Fire_OR_SkySat_SR_TrueColor_2026-08-12T153802Z.tif",
        "SkySat_SR_TrueColor_20260812.tif",
        "LC08_trueColor_20250922_185617.tif",
        "sentinel_2025-01-11T19:46:16Z_red.tif",
        "umbra_low-durability-wood-framed-1.tif",
    ])
    def test_second_pass_is_a_no_op(self, name):
        from shared_utils.file_naming import create_output_filename
        once = create_output_filename(name, self.EVENT)
        assert create_output_filename(once, self.EVENT) == once


class TestCreateNisarFilename:
    """Tests for create_nisar_filename(original_path, event_name).

    NISAR interferograms are derived from TWO acquisitions, so the name carries
    two dates. create_output_filename relocates only the first datetime it
    finds, which on a GUNW name promotes the REFERENCE date to the canonical
    trailing slot and leaves the SECONDARY date welded mid-name as a bare
    YYYYMMDD:

        NISAR_D54_GUNW_20260617_20260629_unw_..._cm.tif
          -> <EVENT>_NISAR_D54_GUNW_20260629_unw_..._cm_2026-06-17_day.tif

    Both dates are the product, so both are kept, in source order, adjacent,
    immediately before _day.
    """

    EVENT = "202606_Earthquake_Venezuela"

    def test_pair_dates_move_to_the_end_in_order(self):
        from shared_utils.file_naming import create_nisar_filename
        result = create_nisar_filename(
            "NISAR_D54_GUNW_20260617_20260629_unw_delon_deRamp_maskWater_cm.tif",
            self.EVENT,
        )
        assert result == (
            "202606_Earthquake_Venezuela_NISAR_D54_GUNW_unw_delon_deRamp_maskWater_cm"
            "_2026-06-17_2026-06-29_day.tif"
        )

    def test_shared_builder_strands_the_secondary_date(self):
        """Pins WHY this builder exists — remove it and this is what you get."""
        from shared_utils.file_naming import create_output_filename
        bad = create_output_filename(
            "NISAR_D54_GUNW_20260617_20260629_unw_delon_deRamp_maskWater_cm.tif",
            self.EVENT,
        )
        assert "_20260629_" in bad          # unhyphenated, mid-name
        assert bad.endswith("_2026-06-17_day.tif")  # reference date promoted

    def test_no_orphan_date_token_remains_in_the_stem(self):
        from shared_utils.file_naming import create_nisar_filename
        result = create_nisar_filename(
            "NISAR_D54_GUNW_20260617_20260629_unw_delon_deRamp_maskWater_cm.tif",
            self.EVENT,
        )
        assert "20260617" not in result
        assert "20260629" not in result

    def test_no_double_underscores(self):
        from shared_utils.file_naming import create_nisar_filename
        for src in (
            "NISAR_D54_GUNW_20260617_20260629_unw_cm.tif",
            "20260617_20260629_NISAR_GUNW_unw_cm.tif",
            "NISAR_GUNW_unw_cm_20260617_20260629.tif",
        ):
            assert "__" not in create_nisar_filename(src, self.EVENT), src

    def test_trailing_variant_tokens_are_preserved(self):
        from shared_utils.file_naming import create_nisar_filename
        crop = create_nisar_filename(
            "NISAR_GUNW_20260613_20260625_unw_delon_deRamp_maskWater_cm-crop2.tif",
            self.EVENT,
        )
        clip = create_nisar_filename(
            "NISAR_GUNW_A61_20260618_20260630_unw_delon_deRamp_maskWater_cm_clipped.tif",
            self.EVENT,
        )
        assert "cm-crop2" in crop
        assert "A61" in clip and "cm_clipped" in clip

    def test_variants_of_the_same_pair_do_not_collide(self):
        from shared_utils.file_naming import create_nisar_filename
        plain = create_nisar_filename(
            "NISAR_GUNW_20260613_20260625_unw_delon_deRamp_maskWater_cm.tif", self.EVENT)
        crop = create_nisar_filename(
            "NISAR_GUNW_20260613_20260625_unw_delon_deRamp_maskWater_cm-crop2.tif", self.EVENT)
        assert plain != crop

    def test_last_date_token_is_the_secondary_acquisition(self):
        """Downstream code that reads the final date gets the post-event scene."""
        import re
        from shared_utils.file_naming import create_nisar_filename
        result = create_nisar_filename(
            "NISAR_D54_GUNW_20260617_20260629_unw_cm.tif", self.EVENT)
        assert re.findall(r'\d{4}-\d{2}-\d{2}', result)[-1] == "2026-06-29"

    def test_single_date_falls_back_to_the_shared_convention(self):
        from shared_utils.file_naming import create_nisar_filename, create_output_filename
        src = "NISAR_GUNW_20260617_unw_cm.tif"
        assert create_nisar_filename(src, self.EVENT) == \
            create_output_filename(src, self.EVENT)

    def test_no_date_falls_back_to_the_shared_convention(self):
        from shared_utils.file_naming import create_nisar_filename
        assert create_nisar_filename("NISAR_GUNW_unw_cm.tif", self.EVENT) == \
            "202606_Earthquake_Venezuela_NISAR_GUNW_unw_cm_day.tif"

    def test_non_date_digit_runs_are_not_mistaken_for_dates(self):
        from shared_utils.file_naming import create_nisar_filename, create_output_filename
        # 20261332 has no month 13 -> only one real date -> single-date fallback.
        src = "NISAR_GUNW_20261332_20260629_unw_cm.tif"
        assert create_nisar_filename(src, self.EVENT) == \
            create_output_filename(src, self.EVENT)

    def test_event_prefix_is_not_doubled(self):
        from shared_utils.file_naming import create_nisar_filename
        src = f"{self.EVENT}_NISAR_D54_GUNW_20260617_20260629_unw_cm.tif"
        result = create_nisar_filename(src, self.EVENT)
        assert result.count(self.EVENT) == 1

    @pytest.mark.parametrize("name", [
        "NISAR_D54_GUNW_20260617_20260629_unw_delon_deRamp_maskWater_cm.tif",
        "NISAR_GUNW_20260613_20260625_unw_delon_deRamp_maskWater_cm-crop2.tif",
        "NISAR_GUNW_A61_20260618_20260630_unw_delon_deRamp_maskWater_cm_clipped.tif",
        "NISAR_GUNW_20260617_unw_cm.tif",
        "NISAR_GUNW_unw_cm.tif",
    ])
    def test_second_pass_is_a_no_op(self, name):
        from shared_utils.file_naming import create_nisar_filename
        once = create_nisar_filename(name, self.EVENT)
        assert create_nisar_filename(once, self.EVENT) == once


class TestPrefixEvent:
    """Tests for prefix_event(stem, event_name)."""

    def test_prepends_when_absent(self):
        from shared_utils.file_naming import prefix_event
        assert prefix_event("SkySat_TrueColor", "202607_Fire_OR") == \
            "202607_Fire_OR_SkySat_TrueColor"

    def test_no_op_when_already_prefixed(self):
        from shared_utils.file_naming import prefix_event
        assert prefix_event("202607_Fire_OR_SkySat", "202607_Fire_OR") == \
            "202607_Fire_OR_SkySat"

    def test_no_op_when_stem_is_exactly_the_event(self):
        from shared_utils.file_naming import prefix_event
        assert prefix_event("202607_Fire_OR", "202607_Fire_OR") == "202607_Fire_OR"

    def test_partial_token_match_is_not_treated_as_a_prefix(self):
        from shared_utils.file_naming import prefix_event
        # "202607_Fire_ORegon..." shares a character prefix but not a token
        # boundary - it must still be prefixed.
        assert prefix_event("202607_Fire_ORegon_scene", "202607_Fire_OR") == \
            "202607_Fire_OR_202607_Fire_ORegon_scene"

    def test_empty_event_is_a_no_op(self):
        from shared_utils.file_naming import prefix_event
        assert prefix_event("SkySat_TrueColor", "") == "SkySat_TrueColor"


class TestNoChange:
    """Tests for no_change(original_path, event_name)."""

    def test_prepends_event_keeps_extension(self):
        from shared_utils.file_naming import no_change
        assert no_change("/data/foo.tif", "evt") == "evt_foo.tif"

    def test_preserves_complex_stem(self):
        from shared_utils.file_naming import no_change
        assert no_change("ang_20250101T01_20250101T05_strip.tif", "Event") == \
            "Event_ang_20250101T01_20250101T05_strip.tif"

    def test_does_not_double_an_existing_event_prefix(self):
        from shared_utils.file_naming import no_change
        assert no_change("/data/Event_ang_earlylook.tif", "Event") == \
            "Event_ang_earlylook.tif"


class TestStripEventPrefix:
    """Tests for strip_event_prefix(name, event_name=None).

    The inverse of prefix_event(), for pipelines that keep the activation in the
    GeoTIFF tags + the S3 prefix instead of the filename
    (notebooks/simple_disaster_staging.ipynb).
    """

    def test_strips_the_generic_event_shape_with_no_event_name(self):
        from shared_utils.file_naming import strip_event_prefix
        assert strip_event_prefix("202607_Fire_OR_SkySat_TrueColor.tif") == \
            "SkySat_TrueColor.tif"

    def test_event_name_wins_over_the_generic_shape(self):
        """A location token with underscores is only strippable via event_name.

        The generic YYYYMM_Hazard_Location_ shape would eat exactly three tokens
        and leave 'Mexico_NDVI.tif' behind.
        """
        from shared_utils.file_naming import strip_event_prefix
        assert strip_event_prefix("202508_Flood_New_Mexico_NDVI.tif",
                                  "202508_Flood_New_Mexico") == "NDVI.tif"
        assert strip_event_prefix("202508_Flood_New_Mexico_NDVI.tif") == "Mexico_NDVI.tif"

    def test_event_name_match_is_case_insensitive(self):
        from shared_utils.file_naming import strip_event_prefix
        assert strip_event_prefix("202606_earthquake_venezuela_NDVI_20260101.tif",
                                  "202606_Earthquake_Venezuela") == "NDVI_20260101.tif"

    def test_strips_a_misnamed_event_that_is_not_event_name(self):
        """Right shape, wrong event -- operators mislabel. Still cleaned."""
        from shared_utils.file_naming import strip_event_prefix
        assert strip_event_prefix("202607_Fire_OR_SkySat_TrueColor.tif",
                                  "202606_Earthquake_Venezuela") == "SkySat_TrueColor.tif"

    def test_leaves_an_unprefixed_name_alone(self):
        from shared_utils.file_naming import strip_event_prefix
        assert strip_event_prefix("SkySat_SR_TrueColor_20260812.tif",
                                  "202606_Earthquake_Venezuela") == \
            "SkySat_SR_TrueColor_20260812.tif"

    def test_an_8digit_date_head_is_not_an_event_prefix(self):
        """`\\d{6}` must not bite the front off a YYYYMMDD run: 202608 then '1',
        not '_', so the anchored pattern cannot match."""
        from shared_utils.file_naming import strip_event_prefix
        assert strip_event_prefix("20260812_SkySat_SR_TrueColor.tif") == \
            "20260812_SkySat_SR_TrueColor.tif"

    def test_preserves_a_directory_component(self):
        from shared_utils.file_naming import strip_event_prefix
        assert strip_event_prefix("drcs_activations/evt/202607_Fire_OR_NDVI.tif") == \
            "drcs_activations/evt/NDVI.tif"

    def test_refuses_to_strip_away_the_whole_stem(self):
        """A name that is nothing BUT the event would strip to '.tif'."""
        from shared_utils.file_naming import strip_event_prefix
        assert strip_event_prefix("202607_Fire_OR_.tif", "202607_Fire_OR") == \
            "202607_Fire_OR_.tif"

    def test_is_idempotent(self):
        from shared_utils.file_naming import strip_event_prefix
        once = strip_event_prefix("202607_Fire_OR_SkySat_TrueColor.tif", "202607_Fire_OR")
        assert strip_event_prefix(once, "202607_Fire_OR") == once

    def test_round_trips_with_prefix_event(self):
        from shared_utils.file_naming import prefix_event, strip_event_prefix
        stem = "SkySat_TrueColor"
        assert strip_event_prefix(prefix_event(stem, "202607_Fire_OR"),
                                  "202607_Fire_OR") == stem
