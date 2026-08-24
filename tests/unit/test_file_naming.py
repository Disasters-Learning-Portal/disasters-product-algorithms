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
