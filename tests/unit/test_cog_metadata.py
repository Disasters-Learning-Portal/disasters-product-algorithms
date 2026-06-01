"""
Tests for shared_utils.cog_metadata + the `metadata=` kwarg on
shared_utils.cog_utils.convert_to_cog.

CI regression coverage for the metadata-embedding path. Catches the
GDAL 3.10 "would break COG layout" trap (the obvious post-step
gdal.Open(GA_Update) + SetMetadata approach silently destroys COG
optimization). Until this commit, no test exercised cog_metadata at
all — the path was added 2026-05-29 and these tests pin its behavior.
"""

import json
import os

import pytest

# Hard skips for the geospatial stack — these tests need a real GDAL,
# rasterio, and rio_cogeo. CI's cli-smoke job bootstraps from
# dev-conda-deps.txt which includes all three; dev machines without the
# stack will skip these tests cleanly.
rasterio = pytest.importorskip("rasterio")
gdal = pytest.importorskip("osgeo.gdal")
rio_cogeo = pytest.importorskip("rio_cogeo")


# -------------------------------------------------------------------- #
# load_metadata_json — pure-Python edge cases (no geospatial deps)     #
# -------------------------------------------------------------------- #

class TestLoadMetadataJson:
    """Pure-Python helper used by every sensor CLI to parse --metadata-json."""

    def test_none_returns_none(self):
        from shared_utils.cog_metadata import load_metadata_json
        assert load_metadata_json(None) is None

    def test_empty_string_returns_none(self):
        from shared_utils.cog_metadata import load_metadata_json
        assert load_metadata_json('') is None

    def test_missing_file_raises_FileNotFoundError(self, tmp_path):
        from shared_utils.cog_metadata import load_metadata_json
        nonexistent = tmp_path / "does_not_exist.json"
        with pytest.raises(FileNotFoundError):
            load_metadata_json(str(nonexistent))

    def test_valid_json_object_loads_as_dict(self, tmp_path):
        from shared_utils.cog_metadata import load_metadata_json
        p = tmp_path / "meta.json"
        p.write_text(json.dumps({
            "ACTIVATION_EVENT": "201808_Flood_TX",
            "SOURCE": "USGS",
        }))
        result = load_metadata_json(str(p))
        assert result == {
            "ACTIVATION_EVENT": "201808_Flood_TX",
            "SOURCE": "USGS",
        }

    def test_non_object_json_raises_ValueError(self, tmp_path):
        from shared_utils.cog_metadata import load_metadata_json
        p = tmp_path / "list.json"
        p.write_text(json.dumps(["not", "a", "dict"]))
        with pytest.raises(ValueError, match="must contain a JSON object"):
            load_metadata_json(str(p))

    def test_values_coerced_to_strings(self, tmp_path):
        """GeoTIFF tags must be strings — verify ints/floats are stringified."""
        from shared_utils.cog_metadata import load_metadata_json
        p = tmp_path / "mixed.json"
        p.write_text(json.dumps({"YEAR": 2018, "RATIO": 1.5}))
        result = load_metadata_json(str(p))
        assert result == {"YEAR": "2018", "RATIO": "1.5"}


# -------------------------------------------------------------------- #
# resolve_metadata — auto-derivation behavior                          #
# -------------------------------------------------------------------- #

class TestResolveMetadata:
    """Auto-derive YEAR_MONTH/HAZARD/LOCATION from ACTIVATION_EVENT."""

    def test_manual_with_ACTIVATION_EVENT_splits_into_parts(self):
        from shared_utils.cog_metadata import resolve_metadata
        result = resolve_metadata(
            'irrelevant_filename.tif',
            mode='manual',
            manual_metadata={'ACTIVATION_EVENT': '201808_Flood_TX', 'SOURCE': 'USGS'},
        )
        assert result['ACTIVATION_EVENT'] == '201808_Flood_TX'
        assert result['SOURCE'] == 'USGS'
        assert result['YEAR_MONTH'] == '201808'
        assert result['HAZARD'] == 'Flood'
        assert result['LOCATION'] == 'TX'

    def test_manual_without_ACTIVATION_EVENT_passes_through(self):
        from shared_utils.cog_metadata import resolve_metadata
        result = resolve_metadata(
            'irrelevant_filename.tif',
            mode='manual',
            manual_metadata={'SOURCE': 'TBD'},
        )
        assert result == {'SOURCE': 'TBD'}


# -------------------------------------------------------------------- #
# convert_to_cog(metadata=...) — the headline test                     #
#                                                                      #
# Pins the contract: metadata gets embedded as GeoTIFF tags during COG #
# creation (via rio_cogeo.cog_translate), the result still passes      #
# cog_validate, and the no-metadata fast path keeps producing valid    #
# COGs with no leaked tags.                                            #
# -------------------------------------------------------------------- #

class TestConvertToCogMetadata:

    USER_METADATA = {
        'ACTIVATION_EVENT': '201808_Flood_TX',
        'SOURCE': 'USGS',
        'PROCESSOR': 'NASA Disasters COG Processor v0.0.0-test',
    }

    def test_metadata_embeds_and_roundtrips(self, float32_geotiff, tmp_path):
        from shared_utils.cog_utils import convert_to_cog
        from rio_cogeo.cogeo import cog_validate

        out = tmp_path / "out_with_meta.tif"
        result_path = convert_to_cog(
            float32_geotiff,
            output_cog=str(out),
            dst_crs=None,  # skip warp for test speed
            compression='ZSTD',
            compression_level=22,
            overview_levels=5,
            metadata=self.USER_METADATA,
            quiet=True,
        )
        assert result_path == str(out)
        assert out.exists()

        # cog_validate still passes (the GDAL 3.10 SetMetadata trap would
        # fail here with `valid=False` + IFD-offset errors).
        is_valid, errors, _ = cog_validate(str(out), quiet=True)
        assert is_valid, f"Output is not a valid COG: errors={errors}"

        # Roundtrip the user keys via GDAL
        ds = gdal.Open(str(out), gdal.GA_ReadOnly)
        actual = ds.GetMetadata()
        del ds
        for key, expected_value in self.USER_METADATA.items():
            assert actual.get(key) == expected_value, (
                f"Tag {key!r} did not roundtrip: expected={expected_value!r}, "
                f"got={actual.get(key)!r}"
            )

    def test_metadata_auto_augments_with_resolve_metadata(self, float32_geotiff, tmp_path):
        """When ACTIVATION_EVENT is set, YEAR_MONTH/HAZARD/LOCATION
        are derived via resolve_metadata before embedding."""
        from shared_utils.cog_utils import convert_to_cog

        out = tmp_path / "out_auto_aug.tif"
        convert_to_cog(
            float32_geotiff,
            output_cog=str(out),
            dst_crs=None,
            metadata=self.USER_METADATA,  # has ACTIVATION_EVENT='201808_Flood_TX'
            quiet=True,
        )

        ds = gdal.Open(str(out), gdal.GA_ReadOnly)
        actual = ds.GetMetadata()
        del ds
        assert actual.get('YEAR_MONTH') == '201808'
        assert actual.get('HAZARD') == 'Flood'
        assert actual.get('LOCATION') == 'TX'

    def test_no_metadata_path_still_works(self, float32_geotiff, tmp_path):
        """Regression: the metadata=None fast path (subprocess rio cogeo
        create) still produces a valid COG."""
        from shared_utils.cog_utils import convert_to_cog
        from rio_cogeo.cogeo import cog_validate

        out = tmp_path / "out_no_meta.tif"
        convert_to_cog(
            float32_geotiff,
            output_cog=str(out),
            dst_crs=None,
            quiet=True,
        )
        assert out.exists()
        is_valid, errors, _ = cog_validate(str(out), quiet=True)
        assert is_valid, f"No-metadata regression: errors={errors}"

    def test_no_metadata_path_does_not_leak_user_keys(self, float32_geotiff, tmp_path):
        """Regression: metadata=None must NOT add user keys to the output."""
        from shared_utils.cog_utils import convert_to_cog

        out = tmp_path / "out_no_meta_leak.tif"
        convert_to_cog(
            float32_geotiff,
            output_cog=str(out),
            dst_crs=None,
            quiet=True,
        )

        ds = gdal.Open(str(out), gdal.GA_ReadOnly)
        actual = ds.GetMetadata()
        del ds
        leaked = [k for k in self.USER_METADATA if k in actual]
        assert leaked == [], (
            f"User metadata keys leaked into no-metadata output: {leaked}"
        )

    @pytest.mark.parametrize('sentinel', ['None', 'none', '', 'NONE'])
    def test_dst_crs_string_sentinels_normalize_to_None(self, sentinel, float32_geotiff, tmp_path):
        """Notebooks pass TARGET_CRS through as a string; convert_to_cog must
        treat 'None' / 'none' / '' as 'preserve native CRS' and skip the warp.
        Without this normalization gdalwarp tries to project to CRS 'None'
        and fails."""
        from shared_utils.cog_utils import convert_to_cog
        out = tmp_path / f"out_sentinel_{sentinel or 'empty'}.tif"
        # If normalization isn't happening, this raises during gdalwarp.
        result = convert_to_cog(
            float32_geotiff,
            output_cog=str(out),
            dst_crs=sentinel,
            quiet=True,
        )
        assert result == str(out)
        assert out.exists()

    def test_gdal_backend_with_metadata_raises_NotImplementedError(self, float32_geotiff, tmp_path):
        """The gdal backend doesn't yet support metadata embedding —
        verify the loud error fires rather than silently dropping tags."""
        from shared_utils.cog_utils import convert_to_cog

        out = tmp_path / "out_gdal_backend.tif"
        with pytest.raises(NotImplementedError, match="backend='gdal'"):
            convert_to_cog(
                float32_geotiff,
                output_cog=str(out),
                dst_crs=None,
                backend='gdal',
                metadata=self.USER_METADATA,
                quiet=True,
            )
