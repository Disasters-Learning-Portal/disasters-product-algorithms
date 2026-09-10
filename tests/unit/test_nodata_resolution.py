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

Later addition — the PIXEL-level half of the same problem:
  - compression.detect_extreme_float_fill probes the pixel data, not just
    the tag. Real NISAR GUNW granules declare nodata=-9999 while their fill
    pixels hold FLT_MAX; every tag-level check passes, so the corrupt fill
    used to sail straight through into the COG. rio-tiler masks on the
    declared -9999, finds none, and renders FLT_MAX as real data that clamps
    to the top of any rescale — the whole masked area paints solid.
  - convert_to_cog now translates that fill via gdalwarp
    `-srcnodata <actual fill> -dstnodata <safe value>`, and forces the warp
    pass even when the CRS already matches (gdal_translate -a_nodata only
    re-tags; cog_translate copies pixels verbatim).
  - The tag-level fix was itself incomplete for the same reason: it re-tagged
    to -9999 and then passed `-srcnodata -9999`, matching no pixels. The
    tests below assert on PIXEL CONTENT, not just the nodata tag — a tag-only
    assertion passes happily while the data stays broken.
"""

import math
import os

import pytest

# Skip cleanly when the geospatial stack isn't available on the dev machine.
rasterio = pytest.importorskip("rasterio")
gdal = pytest.importorskip("osgeo.gdal")
rio_cogeo = pytest.importorskip("rio_cogeo")

gdal.UseExceptions()


# The float32 fill NISAR GUNW granules actually carry, and the threshold
# `compression.EXTREME_FLOAT_ABS_THRESHOLD` uses to separate fill from data.
# Duplicated here on purpose: a test that imports the production threshold
# would follow it if someone widened it by mistake.
FLT_MAX = 3.4028234663852886e+38
EXTREME_ABS_THRESHOLD = 1e30

# Two distinct real-data values planted in the pixel-corruption fixtures. They
# bracket zero and neither is near the fill, so "the data survived" is a
# checkable claim rather than "the file is non-empty".
REAL_HI = 0.5
REAL_LO = -42.25


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

    def test_extreme_float_tag_also_rewrites_the_pixels(self, tmp_path):
        """The strengthened form of the test above.

        Asserting only `src.nodata == -9999.0` was never enough: the old code
        re-tagged to -9999 and then handed gdalwarp `-srcnodata -9999`, which
        matches nothing, so the FLT_MAX PIXELS survived into the output. The
        tag said one thing and the data said another, and the test passed.
        Assert the pixels."""
        import numpy as np
        from shared_utils.cog_utils import convert_to_cog

        src_path = self._make_corrupted_fixture(tmp_path)
        out = tmp_path / "tag_and_pixels_remapped.tif"
        convert_to_cog(src_path, output_cog=str(out), dst_crs=None, quiet=True)

        with rasterio.open(str(out)) as src:
            assert src.nodata == -9999.0
            data = src.read(1)

        survivors = int(np.count_nonzero(np.abs(data) >= EXTREME_ABS_THRESHOLD))
        assert survivors == 0, (
            f"{survivors} FLT_MAX-class pixels survived the remap; the nodata "
            f"tag says -9999 but the data does not agree"
        )
        # The real-data patch the fixture writes must come through untouched.
        assert np.count_nonzero(np.isclose(data, 0.5)) >= 50


# ------------------------------------------------------------------------ #
# Fix 4a: detect_extreme_float_fill — FLT_MAX detection in the PIXELS.      #
# ------------------------------------------------------------------------ #

class TestDetectExtremeFloatFill:
    """`is_extreme_float_nodata` reads the nodata TAG. This reads the DATA.

    A NISAR GUNW granule declares nodata=-9999 and fills with FLT_MAX; the
    tag-level check returns False for -9999, so nothing upstream of this
    function notices."""

    def _write_raster(self, tmp_path, name, data, nodata, dtype='float32'):
        """Write a small single-band GeoTIFF from an already-built array."""
        from rasterio.transform import from_bounds
        from rasterio.crs import CRS

        path = tmp_path / name
        with rasterio.open(
            str(path), 'w', driver='GTiff',
            height=data.shape[-2], width=data.shape[-1], count=1, dtype=dtype,
            crs=CRS.from_epsg(32610),
            transform=from_bounds(500000, 4000000, 500640, 4000640,
                                  data.shape[-1], data.shape[-2]),
            nodata=nodata,
        ) as dst:
            dst.write(data.reshape((1,) + data.shape[-2:]))
        return str(path)

    @pytest.mark.parametrize('fill', [FLT_MAX, -FLT_MAX, 3.3999999521443642e+38])
    def test_corrupt_float_raster_returns_the_fill_value(self, tmp_path, fill):
        """The tag is a perfectly sane -9999 — the answer has to come from
        the pixels, and it has to be the fill value itself so gdalwarp's
        -srcnodata can name it."""
        import numpy as np
        from shared_utils.compression import detect_extreme_float_fill

        data = np.full((64, 64), fill, dtype='float32')
        data[10:20, 10:20] = REAL_HI
        path = self._write_raster(tmp_path, "corrupt.tif", data, -9999.0)

        with rasterio.open(path) as src:
            assert detect_extreme_float_fill(src) == pytest.approx(fill)

    def test_clean_float_raster_returns_None(self, tmp_path):
        """No false positives on ordinary data. LOS displacement in cm sits
        in roughly -60..60; nothing here is fill."""
        import numpy as np
        from shared_utils.compression import detect_extreme_float_fill

        rng = np.random.default_rng(0)
        data = rng.uniform(-60.0, 60.0, (64, 64)).astype('float32')
        data[0:5, 0:5] = -9999.0  # legitimate declared nodata
        path = self._write_raster(tmp_path, "clean.tif", data, -9999.0)

        with rasterio.open(path) as src:
            assert detect_extreme_float_fill(src) is None

    @pytest.mark.parametrize('big', [1e10, 1e20, 6.02e23])
    def test_large_but_sub_threshold_values_return_None(self, tmp_path, big):
        """The threshold is 1e30. Genuinely large physical values below it
        are data, not fill, and must not trigger a remap."""
        import numpy as np
        from shared_utils.compression import detect_extreme_float_fill

        data = np.full((64, 64), big, dtype='float32')
        path = self._write_raster(tmp_path, "big.tif", data, -9999.0)

        with rasterio.open(path) as src:
            assert detect_extreme_float_fill(src) is None

    def test_integer_raster_returns_None(self, int16_geotiff):
        """Integer dtypes cannot hold FLT_MAX; the probe must bail out before
        reading rather than trying to interpret int pixels as float fill."""
        from shared_utils.compression import detect_extreme_float_fill

        with rasterio.open(int16_geotiff) as src:
            assert detect_extreme_float_fill(src) is None

    def test_conftest_float32_fixture_is_not_flagged(self, float32_geotiff):
        """Regression guard for the ordinary float path: the repo's standard
        float32 fixture (0..1 data, -9999 nodata) must stay untouched, or
        every float COG picks up a pointless forced warp."""
        from shared_utils.compression import detect_extreme_float_fill

        with rasterio.open(float32_geotiff) as src:
            assert detect_extreme_float_fill(src) is None

    def test_returns_the_dominant_fill_not_the_maximum(self, tmp_path):
        """gdalwarp's -srcnodata takes one value, so the probe has to pick
        the fill that actually dominates rather than whichever extreme
        happens to be largest."""
        import numpy as np
        from shared_utils.compression import detect_extreme_float_fill

        data = np.full((64, 64), -FLT_MAX, dtype='float32')   # dominant
        data[0, 0:4] = FLT_MAX                                # larger, rarer
        data[10:20, 10:20] = REAL_HI
        path = self._write_raster(tmp_path, "mixed.tif", data, -9999.0)

        with rasterio.open(path) as src:
            assert detect_extreme_float_fill(src) == pytest.approx(-FLT_MAX)


# ------------------------------------------------------------------------ #
# Fix 4b: convert_to_cog rewrites FLT_MAX fill PIXELS, not just the tag.    #
# ------------------------------------------------------------------------ #

class TestPixelLevelExtremeFillRemap:
    """The real NISAR GUNW shape: nodata TAG = -9999, fill PIXELS = FLT_MAX.

    Every tag-level check passes, so this used to reach the "use existing
    no-data value" branch untouched and ship a COG whose masked area renders
    solid. All assertions here are on pixel content — a tag-only assertion
    cannot see this bug."""

    def _make_pixel_corrupted_fixture(self, tmp_path, name="pixel_corrupt.tif",
                                      tag_nodata=-9999.0, fill=FLT_MAX):
        """float32 raster whose nodata TAG is sane but whose fill PIXELS are
        FLT_MAX. Carries two known real-data patches so the test can prove the
        remap rewrote the fill and nothing else."""
        import numpy as np
        from rasterio.transform import from_bounds
        from rasterio.crs import CRS

        path = tmp_path / name
        data = np.full((1, 64, 64), fill, dtype='float32')
        data[0, 10:20, 10:20] = REAL_HI   # 100 px
        data[0, 30:34, 30:34] = REAL_LO   # 16 px
        with rasterio.open(
            str(path), 'w', driver='GTiff',
            height=64, width=64, count=1, dtype='float32',
            crs=CRS.from_epsg(32610),
            transform=from_bounds(500000, 4000000, 500640, 4000640, 64, 64),
            nodata=tag_nodata,
        ) as dst:
            dst.write(data)
        return str(path)

    def _assert_clean_and_intact(self, out_path):
        """Shared post-condition: safe tag, ZERO FLT_MAX-class pixels, and
        both real-data patches still present at their original values."""
        import numpy as np

        with rasterio.open(str(out_path)) as src:
            assert src.nodata == -9999.0, (
                f"expected nodata tag -9999.0, got {src.nodata!r}"
            )
            data = src.read(1)

        survivors = int(np.count_nonzero(np.abs(data) >= EXTREME_ABS_THRESHOLD))
        assert survivors == 0, (
            f"{survivors} of {data.size} output pixels are still FLT_MAX-class. "
            f"The tag reads -9999 but nothing masks these, so rio-tiler renders "
            f"them as data and clamps them to the top of the rescale."
        )

        valid = data[data != -9999.0]
        assert valid.size > 0, "the remap wiped out the real data too"
        assert valid.max() == pytest.approx(REAL_HI, abs=1e-4)
        assert valid.min() == pytest.approx(REAL_LO, abs=1e-3)
        # Both patches survive resampling (100 and 16 pixels in the source;
        # allow slack for the warp rather than pinning exact counts).
        assert np.count_nonzero(np.isclose(data, REAL_HI)) >= 50
        assert np.count_nonzero(np.isclose(data, REAL_LO)) >= 8
        return data

    def test_sane_tag_with_flt_max_pixels_is_rewritten(self, tmp_path, capsys):
        """THE headline regression. Reprojecting path (EPSG:3857), which is
        what the production pipeline actually runs."""
        from shared_utils.cog_utils import convert_to_cog

        src_path = self._make_pixel_corrupted_fixture(tmp_path)
        out = tmp_path / "pixel_remapped_3857.tif"
        convert_to_cog(
            src_path,
            output_cog=str(out),
            dst_crs='EPSG:3857',
            compression='ZSTD',
            compression_level=22,
            overview_levels=5,
            quiet=False,
        )

        self._assert_clean_and_intact(out)

        # The operator has to be told; this fires on files that looked fine.
        captured = capsys.readouterr()
        assert "FLT_MAX" in captured.out
        assert "-9999" in captured.out

    def test_sane_tag_with_flt_max_pixels_is_rewritten_same_crs(self, tmp_path,
                                                                capsys):
        """dst_crs=None means no reprojection is needed, and the non-warp
        paths cannot fix this: gdal_translate -a_nodata only re-tags and
        cog_translate copies pixels verbatim. The remap has to force a
        same-CRS warp pass anyway."""
        from shared_utils.cog_utils import convert_to_cog

        src_path = self._make_pixel_corrupted_fixture(tmp_path)
        out = tmp_path / "pixel_remapped_samecrs.tif"
        convert_to_cog(src_path, output_cog=str(out), dst_crs=None, quiet=False)

        self._assert_clean_and_intact(out)

        # CRS must be preserved — the forced warp is for the fill, not a
        # reprojection smuggled in through the back door.
        with rasterio.open(str(out)) as src:
            assert src.crs.to_epsg() == 32610

        captured = capsys.readouterr()
        assert "same-CRS warp" in captured.out

    def test_explicit_source_crs_also_gets_the_forced_warp(self, tmp_path):
        """Same as above but the caller names the source's own CRS instead of
        passing None — `needs_reprojection` is still False, so this exercises
        the same forced-warp branch by a different route."""
        from shared_utils.cog_utils import convert_to_cog

        src_path = self._make_pixel_corrupted_fixture(tmp_path)
        out = tmp_path / "pixel_remapped_explicit_crs.tif"
        convert_to_cog(src_path, output_cog=str(out), dst_crs='EPSG:32610',
                       quiet=True)

        self._assert_clean_and_intact(out)

    def test_negative_flt_max_pixels_are_rewritten(self, tmp_path):
        """-FLT_MAX is as common a fill as +FLT_MAX and is just as invisible
        to a -9999 tag; it clamps to the BOTTOM of the rescale instead."""
        from shared_utils.cog_utils import convert_to_cog

        src_path = self._make_pixel_corrupted_fixture(
            tmp_path, name="neg_fill.tif", fill=-FLT_MAX
        )
        out = tmp_path / "neg_fill_cog.tif"
        convert_to_cog(src_path, output_cog=str(out), dst_crs=None, quiet=True)

        self._assert_clean_and_intact(out)

    def test_untagged_source_with_flt_max_pixels_is_rewritten(self, tmp_path):
        """No nodata tag at all. The dtype default (-9999) gets declared
        either way; without the pixel remap that declaration would be a lie."""
        from shared_utils.cog_utils import convert_to_cog

        src_path = self._make_pixel_corrupted_fixture(
            tmp_path, name="untagged.tif", tag_nodata=None
        )
        out = tmp_path / "untagged_cog.tif"
        convert_to_cog(src_path, output_cog=str(out), dst_crs=None, quiet=True)

        self._assert_clean_and_intact(out)

    def test_clean_float_source_is_left_alone(self, float32_geotiff, tmp_path,
                                              capsys):
        """The other side of the guard: an ordinary float32 source must not
        trip the probe, must keep its declared nodata, and must not print a
        FLT_MAX warning."""
        from shared_utils.cog_utils import convert_to_cog

        out = tmp_path / "clean_cog.tif"
        convert_to_cog(float32_geotiff, output_cog=str(out), dst_crs=None,
                       quiet=False)

        with rasterio.open(str(out)) as src:
            assert src.nodata == -9999.0

        captured = capsys.readouterr()
        assert "FLT_MAX" not in captured.out
        assert "same-CRS warp" not in captured.out

    @pytest.mark.parametrize('explicit_nodata', [-9999.0, -9999, 0.0])
    def test_explicit_caller_nodata_still_gets_the_remap(
        self, tmp_path, capsys, explicit_nodata
    ):
        """A caller-supplied nodata= must not wave the corrupt fill through.

        The auto-detect branches are skipped entirely when the caller passes a
        value, so this used to leave remap_extreme_fill unset and ship the same
        broken COG. The per-sensor CLIs all forward a --nodata
        (process_landsat89, process_sentinel2, process_capella,
        process_satellogic), so this is the common path in production, not an
        edge case."""
        from shared_utils.cog_utils import convert_to_cog
        import numpy as np

        src_path = self._make_pixel_corrupted_fixture(
            tmp_path, name=f"explicit_{abs(int(explicit_nodata))}.tif"
        )
        out = tmp_path / f"explicit_{abs(int(explicit_nodata))}_cog.tif"
        convert_to_cog(src_path, output_cog=str(out), nodata=explicit_nodata,
                       dst_crs=None, quiet=False)

        with rasterio.open(str(out)) as src:
            assert src.nodata == pytest.approx(explicit_nodata)
            data = src.read(1)

        survivors = int(np.count_nonzero(np.abs(data) >= EXTREME_ABS_THRESHOLD))
        assert survivors == 0, (
            f"{survivors} of {data.size} pixels are still FLT_MAX-class after "
            f"convert_to_cog(nodata={explicit_nodata!r}). An explicit nodata "
            f"must not bypass the pixel probe."
        )
        assert "FLT_MAX-class fill" in capsys.readouterr().out

    def test_mixed_sign_fill_warns_about_what_it_cannot_remap(self, tmp_path,
                                                              capsys):
        """gdalwarp's -srcnodata names ONE value, so a raster carrying both
        +FLT_MAX and -FLT_MAX keeps whichever is not dominant. That is a real
        limitation — the requirement here is that it is announced, not that it
        happens silently."""
        import numpy as np
        from rasterio.transform import from_bounds
        from rasterio.crs import CRS
        from shared_utils.cog_utils import convert_to_cog

        path = tmp_path / "mixed_sign.tif"
        data = np.full((1, 64, 64), FLT_MAX, dtype='float32')
        data[0, 40:64, :] = -FLT_MAX          # minority: 24 rows
        data[0, 10:20, 10:20] = REAL_HI
        with rasterio.open(
            str(path), 'w', driver='GTiff',
            height=64, width=64, count=1, dtype='float32',
            crs=CRS.from_epsg(32610),
            transform=from_bounds(500000, 4000000, 500640, 4000640, 64, 64),
            nodata=-9999.0,
        ) as dst:
            dst.write(data)

        out = tmp_path / "mixed_sign_cog.tif"
        convert_to_cog(str(path), output_cog=str(out), dst_crs=None, quiet=False)

        captured = capsys.readouterr().out
        assert "distinct FLT_MAX-class fill" in captured, (
            "a raster with more than one extreme sentinel must say so; "
            "silently emitting a half-repaired COG is the failure mode"
        )
        assert "will REMAIN in the output" in captured
