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


class TestNoChange:
    """Tests for no_change(original_path, event_name)."""

    def test_prepends_event_keeps_extension(self):
        from shared_utils.file_naming import no_change
        assert no_change("/data/foo.tif", "evt") == "evt_foo.tif"

    def test_preserves_complex_stem(self):
        from shared_utils.file_naming import no_change
        assert no_change("ang_20250101T01_20250101T05_strip.tif", "Event") == \
            "Event_ang_20250101T01_20250101T05_strip.tif"
