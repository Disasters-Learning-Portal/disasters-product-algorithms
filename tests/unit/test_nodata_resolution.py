"""
Tests for nodata resolution across shared_utils.

Pins the behavior changed in plan addendum 3 (2026-06-01):
  - set_nodata_value covers uint32/int64/uint64 with explicit defaults and
    raises ValueError for unsupported dtypes (was: silently returned -9999.0).
  - validate_nodata_for_dtype is strict for unknown dtypes (was: permissive,
    short-circuited range checks for typos).
  - cog_utils.convert_to_cog defaults to strict_nodata=True (raises on
    out-of-range caller values) with opt-out kwarg for legacy callers.
  - compression.is_extreme_float_nodata detects FLT_MAX-class corruption
    patterns (the same ones the standalone update_nodata_cog.py script
    handles), now wired into the convert_to_cog default path so the engine
    doesn't silently adopt them from source files.

Without these tests the four silent-failure modes documented in the plan
addendum could silently regress and ship subtly wrong COGs to
veda-data-airflow.
"""

import math
import os

import pytest

# Skip cleanly when the geospatial stack isn't available on the dev machine.
rasterio = pytest.importorskip("rasterio")
gdal = pytest.importorskip("osgeo.gdal")
rio_cogeo = pytest.importorskip("rio_cogeo")

gdal.UseExceptions()


# ------------------------------------------------------------------------ #
# Fix 1: set_nodata_value — expanded ladder + raise for unsupported.       #
# ------------------------------------------------------------------------ #

class TestSetNodataValueExpandedLadder:

    def test_uint32_returns_0(self):
        from shared_utils.cog_utils import set_nodata_value
        assert set_nodata_value('uint32') == 0

    def test_uint64_returns_0(self):
        from shared_utils.cog_utils import set_nodata_value
        assert set_nodata_value('uint64') == 0

    def test_int64_returns_neg_9999(self):
        from shared_utils.cog_utils import set_nodata_value
        assert set_nodata_value('int64') == -9999

    @pytest.mark.parametrize('dtype', ['complex64', 'complex128', 'bool', 'WeirdType', 'object'])
    def test_unsupported_dtype_raises_ValueError(self, dtype):
        from shared_utils.cog_utils import set_nodata_value
        with pytest.raises(ValueError, match="No default nodata for dtype"):
            set_nodata_value(dtype)

    def test_known_dtype_with_valid_manual_returns_manual(self):
        """If manual_nodata is provided AND validates for the dtype, it's
        returned as-is — the dtype-default lookup is skipped."""
        from shared_utils.cog_utils import set_nodata_value
        # Pass an explicit valid nodata for int16; should come back unchanged.
        assert set_nodata_value('int16', manual_nodata=-32768) == -32768


# ------------------------------------------------------------------------ #
# Fix 2a: validate_nodata_for_dtype — strict for unknown dtype.            #
# ------------------------------------------------------------------------ #

class TestValidateNodataForDtypeStrict:

    def test_unknown_dtype_returns_invalid(self):
        from shared_utils.cog_utils import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(0, 'TotallyMadeUpDtype')
        assert result['valid'] is False
        assert 'TotallyMadeUpDtype' in result['error']

    def test_known_dtype_still_validates_correctly(self):
        """Sanity: the strictness for unknown dtypes doesn't break the
        known-dtype range checks."""
        from shared_utils.cog_utils import validate_nodata_for_dtype
        assert validate_nodata_for_dtype(0, 'uint8')['valid'] is True
        assert validate_nodata_for_dtype(999, 'uint8')['valid'] is False
        assert validate_nodata_for_dtype(-9999, 'int16')['valid'] is True


# ------------------------------------------------------------------------ #
# Fix 3: is_extreme_float_nodata — FLT_MAX corruption detection.           #
# ------------------------------------------------------------------------ #

class TestIsExtremeFloatNodata:

    @pytest.mark.parametrize('bad_value', [
        3.4028234663852886e+38,
        -3.4028234663852886e+38,
        3.40282346638529e+38,
        -3.40282346638529e+38,
        3.3999999521443642e+38,
        -3.3999999521443642e+38,
    ])
    def test_known_corruption_patterns_detected(self, bad_value):
        from shared_utils.compression import is_extreme_float_nodata
        assert is_extreme_float_nodata(bad_value) is True

    @pytest.mark.parametrize('safe_value', [0, 0.0, -9999, -9999.0, 255, 65535, -32768, 1.5, 1e10])
    def test_safe_values_not_flagged(self, safe_value):
        from shared_utils.compression import is_extreme_float_nodata
        assert is_extreme_float_nodata(safe_value) is False

    def test_none_returns_False(self):
        from shared_utils.compression import is_extreme_float_nodata
        assert is_extreme_float_nodata(None) is False

    def test_nan_returns_False(self):
        """NaN is a legitimate nodata sentinel for float bands — must NOT
        be classed as corruption."""
        from shared_utils.compression import is_extreme_float_nodata
        assert is_extreme_float_nodata(math.nan) is False

    def test_inf_returns_False(self):
        from shared_utils.compression import is_extreme_float_nodata
        assert is_extreme_float_nodata(math.inf) is False
        assert is_extreme_float_nodata(-math.inf) is False

    def test_non_numeric_returns_False(self):
        from shared_utils.compression import is_extreme_float_nodata
        assert is_extreme_float_nodata("3.4e38") is False
        assert is_extreme_float_nodata([3.4e38]) is False


# ------------------------------------------------------------------------ #
# Fix 2b: convert_to_cog strict_nodata kwarg.                              #
# Fix 3 (wired): extreme-float source-nodata gets remapped + warns.         #
# ------------------------------------------------------------------------ #

class TestConvertToCogStrictNodata:

    def test_invalid_caller_nodata_raises_by_default(self, uint8_geotiff, tmp_path):
        from shared_utils.cog_utils import convert_to_cog
        out = tmp_path / "out.tif"
        with pytest.raises(ValueError, match="nodata=999"):
            convert_to_cog(
                uint8_geotiff,
                output_cog=str(out),
                nodata=999,         # out of [0, 255] for uint8
                dst_crs=None,
                quiet=True,
            )

    def test_strict_nodata_False_signature_accepts_kwarg(self, uint8_geotiff, tmp_path):
        """strict_nodata=False is the documented escape hatch. We can't
        empirically test the warn-fallthrough path because rasterio itself
        rejects out-of-range nodata downstream (gdalwarp / rio_cogeo's
        DatasetWriter raises before convert_to_cog can return). The kwarg
        therefore exists as a forward-looking signal — if a future rasterio
        relaxes its checks, the warn-only mode becomes reachable.
        Here we only verify the kwarg is accepted on the happy path."""
        from shared_utils.cog_utils import convert_to_cog
        out = tmp_path / "out.tif"
        convert_to_cog(
            uint8_geotiff,
            output_cog=str(out),
            nodata=0,           # in-range
            dst_crs=None,
            strict_nodata=False,
            quiet=True,
        )
        assert out.exists()


class TestExtremeFloatRemap:

    def _make_corrupted_fixture(self, tmp_path, bad_nodata=3.4028234663852886e+38):
        """Build a float32 fixture whose nodata tag matches the FLT_MAX
        corruption pattern. Returns the path."""
        import numpy as np
        from rasterio.transform import from_bounds
        from rasterio.crs import CRS

        path = tmp_path / "extreme_nodata.tif"
        data = np.full((1, 64, 64), bad_nodata, dtype='float32')
        data[0, 10:20, 10:20] = 0.5  # a few real-data pixels
        with rasterio.open(
            str(path), 'w', driver='GTiff',
            height=64, width=64, count=1, dtype='float32',
            crs=CRS.from_epsg(32610),
            transform=from_bounds(500000, 4000000, 500640, 4000640, 64, 64),
            nodata=bad_nodata,
        ) as dst:
            dst.write(data)
        return str(path)

    def test_extreme_float_source_nodata_is_remapped(self, tmp_path, capsys):
        """The headline regression test: a file whose nodata tag matches the
        FLT_MAX pattern goes through convert_to_cog and comes out with the
        dtype default (-9999.0 for float32), NOT the original corrupted
        value."""
        from shared_utils.cog_utils import convert_to_cog

        src_path = self._make_corrupted_fixture(tmp_path)
        out = tmp_path / "remapped.tif"
        convert_to_cog(
            src_path,
            output_cog=str(out),
            dst_crs=None,
            compression='ZSTD',
            compression_level=22,
            overview_levels=5,
            quiet=False,
        )

        # The output COG's nodata tag should now be the safe default.
        with rasterio.open(str(out)) as src:
            assert src.nodata == -9999.0, (
                f"expected remap to -9999.0, got {src.nodata!r}"
            )

        # And the warning message must show up in stdout so operators
        # can see it in their notebook logs.
        captured = capsys.readouterr()
        assert "FLT_MAX" in captured.out or "extreme" in captured.out.lower()
