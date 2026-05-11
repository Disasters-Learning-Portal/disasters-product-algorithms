"""Tests for shared_utils.file_naming module."""

import pytest


class TestExtractDateFromFilename:
    """Tests for extract_date_from_filename(filename)."""

    def test_landsat_date(self):
        from shared_utils.file_naming import extract_date_from_filename
        result = extract_date_from_filename("LC08_trueColor_20250922_185617_046028.tif")
        assert result == "2025-09-22"

    def test_sentinel_date(self):
        from shared_utils.file_naming import extract_date_from_filename
        result = extract_date_from_filename("S2B_MSIL2A_colorInfrared_20251111_161419.tif")
        assert result == "2025-11-11"

    def test_no_date(self):
        from shared_utils.file_naming import extract_date_from_filename
        result = extract_date_from_filename("some_file_no_date.tif")
        assert result is None

    def test_date_in_path(self):
        from shared_utils.file_naming import extract_date_from_filename
        result = extract_date_from_filename("20230415_data.tif")
        assert result == "2023-04-15"


class TestConvertDate:
    """Tests for convert_date(date_str)."""

    def test_standard_conversion(self):
        from shared_utils.file_naming import convert_date
        assert convert_date('20250922') == '2025-09-22'

    def test_another_date(self):
        from shared_utils.file_naming import convert_date
        assert convert_date('20231231') == '2023-12-31'

    def test_short_string_unchanged(self):
        from shared_utils.file_naming import convert_date
        # Non-8-char strings returned as-is
        assert convert_date('2025') == '2025'


class TestCreateCogFilename:
    """Tests for create_cog_filename(original_path, event_name, custom_suffix)."""

    def test_basic_creation(self):
        from shared_utils.file_naming import create_cog_filename
        result = create_cog_filename(
            "/path/LC08_trueColor_20250922_185617.tif",
            "202509_Flood_WA"
        )
        assert "202509_Flood_WA" in result
        assert "2025-09-22" in result
        assert result.endswith(".tif")
        assert "_day" in result

    def test_custom_suffix(self):
        from shared_utils.file_naming import create_cog_filename
        result = create_cog_filename(
            "/path/LC08_trueColor_20250922_185617.tif",
            "Event",
            custom_suffix='night'
        )
        assert "_night" in result

    def test_no_double_underscores(self):
        from shared_utils.file_naming import create_cog_filename
        result = create_cog_filename(
            "/path/LC08_trueColor_20250922_185617.tif",
            "Event"
        )
        assert "__" not in result


class TestParseFilenameComponents:
    """Tests for parse_filename_components(filepath)."""

    def test_extracts_basic_components(self):
        from shared_utils.file_naming import parse_filename_components
        result = parse_filename_components("/data/S2B_MSIL2A_colorInfrared_20251111_161419.tif")
        assert result['directory'] == '/data'
        assert result['filename'] == 'S2B_MSIL2A_colorInfrared_20251111_161419.tif'
        assert result['extension'] == '.tif'

    def test_extracts_date(self):
        from shared_utils.file_naming import parse_filename_components
        result = parse_filename_components("/data/LC08_trueColor_20250922_185617.tif")
        assert result['date'] == '2025-09-22'

    def test_extracts_satellite(self):
        from shared_utils.file_naming import parse_filename_components
        result = parse_filename_components("/data/S2B_MSIL2A_colorInfrared_20251111.tif")
        assert result.get('satellite') == 'S2B'

    def test_extracts_product(self):
        from shared_utils.file_naming import parse_filename_components
        result = parse_filename_components("/data/LC08_NDVI_20250922.tif")
        # Should find the uppercase product pattern
        assert 'product' in result

    def test_no_date_file(self):
        from shared_utils.file_naming import parse_filename_components
        result = parse_filename_components("/data/some_file.tif")
        assert 'date' not in result
