"""
Unit tests for shared_utils.notebook_helpers.SimpleProcessor.

Focused on the categorization path, which had zero coverage and a silent
mis-routing bug: SimpleProcessor fed `file_naming.categorize_file` a
`{category_name: regex}` dict, but that helper's contract is `{regex: subdir}`
and it returns the dict VALUE. Matching still worked (a category name is itself
a valid regex), so nothing raised — but the "category" handed downstream was the
regex string, and `_get_output_dir` / `_get_nodata_value` / `filename_creators`
are all keyed by category NAME. Every lookup missed: COGs landed under
`uncategorized/trueColor|truecolor|true_color/` with auto-detected nodata
instead of the configured value.

No S3 in here — `connect_to_s3` / `process_all` are not exercised; these drive
the pure categorization and lookup methods directly.
"""

import pytest

from shared_utils.notebook_helpers import SimpleProcessor


# Mirrors the shape both operator templates use: lowercase index names, and a
# complete category set (they enumerate everything they want).
TEMPLATE_PATTERNS = {
    'trueColor': r'trueColor|truecolor|true_color',
    'colorInfrared': r'colorInfrared|colorIR|color_infrared',
    'ndvi': r'NDVI|ndvi',
    'mndwi': r'mNDWI|mndwi|MNDWI',
}

TEMPLATE_OUTPUT_DIRS = {
    'trueColor': 'trueColor',
    'colorInfrared': 'colorIR',
    'ndvi': 'NDVI',
    'mndwi': 'MNDWI',
}

TEMPLATE_NODATA = {
    'trueColor': 0,
    'colorInfrared': 0,
    'ndvi': -9999,
    'mndwi': -9999,
}


def _processor(**overrides):
    config = {
        'event_name': '202510_Flood_AK',
        'bucket': 'nasa-disasters',
        'source_path': 'drcs_activations/202510_Flood_AK/sentinel2',
        'destination_base': 'drcs_activations_new',
        'categorization_patterns': TEMPLATE_PATTERNS,
        'output_dirs': TEMPLATE_OUTPUT_DIRS,
        'nodata_values': TEMPLATE_NODATA,
    }
    config.update(overrides)
    return SimpleProcessor(config)


class TestCategoryLookupInversion:
    """The `{name: regex}` config must be transposed before it reaches
    categorize_file, so the category handed downstream is the NAME."""

    def test_lookup_is_keyed_by_regex_valued_by_name(self):
        lookup = _processor()._category_lookup()
        assert lookup[r'NDVI|ndvi'] == 'ndvi'
        assert lookup[r'trueColor|truecolor|true_color'] == 'trueColor'

    def test_categorize_returns_name_not_regex(self):
        cats = _processor()._categorize_files([
            'src/S2_trueColor_20251015_ak.tif',
            'src/S2_NDVI_20251015_ak.tif',
        ])
        assert set(cats) == {'trueColor', 'ndvi'}
        # The regression: the regex string must never surface as a category.
        assert not any('|' in c for c in cats)

    def test_uncategorized_still_reported(self):
        cats = _processor()._categorize_files(['src/S2_mystery_20251015_ak.tif'])
        assert cats == {}


class TestConfiguredLookupsResolve:
    """OUTPUT_DIRS / NODATA_VALUES / FILENAME_CREATORS are keyed by category
    name; with the regex leaking through they all silently missed."""

    def test_output_dir_resolves_from_config(self):
        p = _processor()
        (category,) = _processor()._categorize_files(['src/S2_NDVI_20251015_ak.tif'])
        assert p._get_output_dir(category) == 'NDVI'

    def test_output_dir_not_uncategorized_fallback(self):
        p = _processor()
        for category in p._categorize_files([
            'src/S2_trueColor_x.tif', 'src/S2_MNDWI_x.tif',
        ]):
            assert not p._get_output_dir(category).startswith('uncategorized/')

    def test_nodata_resolves_from_config(self):
        p = _processor()
        (category,) = p._categorize_files(['src/S2_NDVI_20251015_ak.tif'])
        assert p._get_nodata_value(category) == -9999

    def test_nodata_zero_is_not_confused_with_missing(self):
        # trueColor configures 0, which is falsy — it must come back as 0,
        # not None (auto-detect).
        p = _processor()
        (category,) = p._categorize_files(['src/S2_trueColor_x.tif'])
        assert p._get_nodata_value(category) == 0

    def test_filename_creator_is_found_by_name(self):
        sentinel = lambda path, event: 'CUSTOM.tif'
        p = _processor(filename_creators={'ndvi': sentinel})
        (category,) = p._categorize_files(['src/S2_NDVI_x.tif'])
        assert p.config['filename_creators'].get(category) is sentinel


class TestConfiguredPatternsReplaceDefaults:
    """A configured category set replaces DEFAULT_PATTERNS rather than merging.

    The old `{**default, **user}` keyed the override off the category NAME, so a
    template naming its indices 'ndvi'/'mndwi' got BOTH its own entry and the
    uppercase default — and the default, being first, won the match.
    """

    def test_lowercase_template_name_wins_over_uppercase_default(self):
        p = _processor()
        (category,) = p._categorize_files(['src/S2_MNDWI_20251015_ak.tif'])
        assert category == 'mndwi'
        assert p._get_output_dir(category) == 'MNDWI'

    def test_defaults_apply_when_nothing_configured(self):
        config = {
            'event_name': 'E', 'bucket': 'b', 'source_path': 'p',
            'destination_base': 'd',
        }
        cats = SimpleProcessor(config)._categorize_files(['src/S2_NDVI_x.tif'])
        assert set(cats) == {'NDVI'}

    def test_empty_pattern_dict_falls_back_to_defaults(self):
        p = _processor(categorization_patterns={})
        assert set(p._categorize_files(['src/S2_NDVI_x.tif'])) == {'NDVI'}


class TestMatchOrdering:
    """categorize_file is first-match-wins; the inversion must not reorder."""

    def test_first_configured_category_wins(self):
        p = _processor(categorization_patterns={
            'first': r'trueColor',
            'second': r'trueColor|truecolor',
        })
        (category,) = p._categorize_files(['src/S2_trueColor_x.tif'])
        assert category == 'first'

    def test_duplicate_regex_keeps_first_name(self):
        p = _processor(categorization_patterns={
            'winner': r'NDVI|ndvi',
            'loser': r'NDVI|ndvi',
        })
        assert p._category_lookup()[r'NDVI|ndvi'] == 'winner'


class TestGeneratedFilename:
    """_generate_filename forwards `categories` into create_output_filename,
    which routes it through categorize_file for the AVIRIS passthrough check —
    so it needs the same inversion."""

    def test_standard_name_follows_shared_convention(self):
        p = _processor()
        assert p._generate_filename('src/S2_trueColor_20251015_alaska.tif') == (
            '202510_Flood_AK_S2_trueColor_alaska_2025-10-15_day.tif'
        )

    def test_aviris_passthrough_fires_on_category_name(self):
        # create_output_filename's default passthrough is ('AVIRIS',); it
        # compares against the category NAME, so this only works post-inversion.
        p = _processor(categorization_patterns={'AVIRIS': r'earlylook'})
        assert p._generate_filename('src/ang20250101_earlylook_strip.tif') == (
            '202510_Flood_AK_ang20250101_earlylook_strip.tif'
        )


class TestPreviewReportsConfiguredCompression:
    """preview_processing hardcoded 'ZSTD level 22' while _process_category
    forwards config['compression_level']; both templates ship level 9."""

    def test_preview_prints_configured_level(self, capsys):
        p = _processor(compression='ZSTD', compression_level=9)
        p.files_to_process = p._categorize_files(['src/S2_NDVI_20251015_ak.tif'])
        p.preview_processing()
        assert 'ZSTD level 9' in capsys.readouterr().out


class TestCategorizeFileContractUnchanged:
    """Guard that the fix went in the CALLER, not in categorize_file.

    categorize_file's `{regex: subdir}` contract is used as documented by
    local_file_processing_template.ipynb and pinned by test_file_naming.py.
    """

    def test_regex_to_subdir_direction_still_returns_the_value(self):
        from shared_utils.file_naming import categorize_file
        categories = {r'trueColor|truecolor': 'Sentinel-2/trueColor'}
        assert categorize_file('S2_trueColor_x.tif', categories) == 'Sentinel-2/trueColor'
