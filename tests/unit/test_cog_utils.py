"""Tests for shared_utils.cog_utils module."""

import pytest
import os
import numpy as np

rasterio = pytest.importorskip("rasterio")


class TestSetNodataValue:
    """Tests for set_nodata_value(dtype, manual_nodata=None)."""

    def test_uint8_returns_0(self):
        from shared_utils.cog_utils import set_nodata_value
        assert set_nodata_value('uint8') == 0

    def test_uint16_returns_0(self):
        from shared_utils.cog_utils import set_nodata_value
        assert set_nodata_value('uint16') == 0

    def test_int8_returns_neg128(self):
        from shared_utils.cog_utils import set_nodata_value
        assert set_nodata_value('int8') == -128

    def test_int16_returns_neg9999(self):
        from shared_utils.cog_utils import set_nodata_value
        assert set_nodata_value('int16') == -9999

    def test_int32_returns_neg9999(self):
        from shared_utils.cog_utils import set_nodata_value
        assert set_nodata_value('int32') == -9999

    def test_float32_returns_neg9999_float(self):
        from shared_utils.cog_utils import set_nodata_value
        assert set_nodata_value('float32') == -9999.0

    def test_float64_returns_neg9999_float(self):
        from shared_utils.cog_utils import set_nodata_value
        assert set_nodata_value('float64') == -9999.0

    def test_unknown_dtype_raises_ValueError(self):
        """Previously fell back to -9999.0 silently for any unrecognized dtype.
        Tightened 2026-06-01 to refuse-and-raise: -9999.0 is a nonsense default
        for complex bands (and would have been silently invalid for uint32/
        int64 too). New behavior pinned in tests/unit/test_nodata_resolution.py
        ::TestSetNodataValueExpandedLadder."""
        from shared_utils.cog_utils import set_nodata_value
        with pytest.raises(ValueError, match="No default nodata"):
            set_nodata_value('complex64')

    def test_valid_manual_nodata(self):
        from shared_utils.cog_utils import set_nodata_value
        assert set_nodata_value('uint8', manual_nodata=255) == 255

    def test_invalid_manual_nodata_falls_back(self):
        from shared_utils.cog_utils import set_nodata_value
        # -1 is out of range for uint8, should fall back to default (0)
        assert set_nodata_value('uint8', manual_nodata=-1) == 0


class TestValidateNodataForDtype:
    """Tests for validate_nodata_for_dtype(nodata, dtype)."""

    def test_valid_uint8_zero(self):
        from shared_utils.cog_utils import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(0, 'uint8')
        assert result['valid'] is True
        assert result['error'] is None

    def test_negative_uint8_invalid(self):
        from shared_utils.cog_utils import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(-1, 'uint8')
        assert result['valid'] is False
        assert result['error'] is not None

    def test_overflow_uint8_invalid(self):
        from shared_utils.cog_utils import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(256, 'uint8')
        assert result['valid'] is False

    def test_valid_int16(self):
        from shared_utils.cog_utils import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(-9999, 'int16')
        assert result['valid'] is True

    def test_valid_float32(self):
        from shared_utils.cog_utils import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(-9999.0, 'float32')
        assert result['valid'] is True

    def test_none_nodata_invalid(self):
        from shared_utils.cog_utils import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(None, 'uint8')
        assert result['valid'] is False

    def test_nan_float32_valid(self):
        from shared_utils.cog_utils import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(float('nan'), 'float32')
        assert result['valid'] is True

    def test_returns_dict_with_expected_keys(self):
        from shared_utils.cog_utils import validate_nodata_for_dtype
        result = validate_nodata_for_dtype(0, 'uint8')
        assert 'valid' in result
        assert 'error' in result


class TestDetermineResamplingMethod:
    """Tests for determine_resampling_method(src_path)."""

    def test_rgb_3band_returns_cubic(self, uint8_geotiff):
        from shared_utils.cog_utils import determine_resampling_method
        method, overview = determine_resampling_method(uint8_geotiff)
        assert method == 'cubic'
        assert overview == 'average'

    def test_float_1band_returns_bilinear(self, float32_geotiff):
        from shared_utils.cog_utils import determine_resampling_method
        method, overview = determine_resampling_method(float32_geotiff)
        assert method == 'bilinear'
        assert overview == 'average'

    def test_mask_file_returns_nearest(self, categorical_geotiff):
        from shared_utils.cog_utils import determine_resampling_method
        method, overview = determine_resampling_method(categorical_geotiff)
        assert method == 'nearest'
        assert overview == 'mode'


class TestGetCompressionProfile:
    """Tests for get_compression_profile(...)."""

    def test_default_profile(self):
        from shared_utils.cog_utils import get_compression_profile
        profile = get_compression_profile()
        assert profile['compress'] == 'ZSTD'
        assert profile['predictor'] == '2'
        assert profile['level'] == 22

    def test_float32_predictor(self):
        from shared_utils.cog_utils import get_compression_profile
        profile = get_compression_profile(dtype='float32')
        assert profile['predictor'] == '3'

    def test_uint8_predictor(self):
        from shared_utils.cog_utils import get_compression_profile
        profile = get_compression_profile(dtype='uint8')
        assert profile['predictor'] == '2'

    def test_large_file_bigtiff(self):
        from shared_utils.cog_utils import get_compression_profile
        profile = get_compression_profile(file_size_gb=5.0)
        assert profile['bigtiff'] == 'YES'

    def test_very_large_file_small_blocks(self):
        from shared_utils.cog_utils import get_compression_profile
        profile = get_compression_profile(file_size_gb=15.0)
        assert profile['blockxsize'] == 256

    def test_invalid_compression_falls_back(self):
        from shared_utils.cog_utils import get_compression_profile
        profile = get_compression_profile(compression='INVALID')
        assert profile['compress'] == 'ZSTD'


class TestValidateCog:
    """Tests for validate_cog(cog_path)."""

    def test_returns_tuple(self, uint8_geotiff):
        from shared_utils.cog_utils import validate_cog
        result = validate_cog(uint8_geotiff)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_result_dict_has_expected_keys(self, uint8_geotiff):
        from shared_utils.cog_utils import validate_cog
        _, details = validate_cog(uint8_geotiff)
        assert 'valid' in details
        assert 'errors' in details
        assert 'warnings' in details

    def test_nonexistent_file(self):
        from shared_utils.cog_utils import validate_cog
        is_valid, details = validate_cog('/nonexistent/path/file.tif')
        assert is_valid is False


class TestGetFinalFilename:
    """Tests for get_final_filename(original_path, event_name, tif_only)."""

    def test_no_event_name_returns_original(self):
        from shared_utils.cog_utils import get_final_filename
        path = "/path/LC08_trueColor_20250922_185617_046028.tif"
        assert get_final_filename(path, None) == path

    def test_landsat_with_event_name(self):
        from shared_utils.cog_utils import get_final_filename
        path = "/path/LC08_trueColor_20250922_185617_046028.tif"
        result = get_final_filename(path, "202512_Flood_WA")
        assert "202512_Flood_WA" not in result   # event name no longer prefixed
        # Individual scene with a time -> full ISO 8601 Zulu, ends in Z (not _day).
        assert result.endswith("2025-09-22T18:56:17Z.tif")
        assert "LC08_trueColor" in result

    def test_sentinel2_with_event_name(self):
        from shared_utils.cog_utils import get_final_filename
        path = "/path/S2B_MSIL2A_colorInfrared_20251111_161419_T17RLN.tif"
        result = get_final_filename(path, "202511_Fire_CA")
        assert "202511_Fire_CA" not in result   # event name no longer prefixed
        assert "2025-11-11" in result
        assert "S2B_MSIL2A_colorInfrared" in result

    def test_merged_file_with_event_name(self):
        from shared_utils.cog_utils import get_final_filename
        path = "/path/LC08_trueColor_20250922_merged.tif"
        result = get_final_filename(path, "202512_Flood_WA")
        assert "202512_Flood_WA" not in result   # event name no longer prefixed
        assert "merged" in result
        assert "2025-09-22" in result

    def test_no_date_returns_original(self):
        from shared_utils.cog_utils import get_final_filename
        path = "/path/some_file_no_date.tif"
        result = get_final_filename(path, "SomeEvent")
        assert result == path


class TestRenameWithEvent:
    """Tests for rename_with_event(file_path, event_name, quiet)."""

    def test_landsat_rename(self, tmp_path):
        from shared_utils.cog_utils import rename_with_event
        # Create a file with Landsat naming pattern
        src = tmp_path / "LC08_trueColor_20250922_185617_046028.tif"
        src.write_bytes(b"dummy")
        result = rename_with_event(str(src), "202512_Flood_WA", quiet=True)
        assert os.path.exists(result)
        assert "202512_Flood_WA" not in os.path.basename(result)   # event name no longer prefixed
        # Individual scene with a time -> full ISO 8601 Zulu, ends in Z (not _day).
        assert os.path.basename(result).endswith("2025-09-22T18:56:17Z.tif")

    def test_sentinel2_rename(self, tmp_path):
        from shared_utils.cog_utils import rename_with_event
        src = tmp_path / "S2B_MSIL2A_colorInfrared_20251111_161419_T17RLN.tif"
        src.write_bytes(b"dummy")
        result = rename_with_event(str(src), "202511_Fire_CA", quiet=True)
        assert os.path.exists(result)
        assert "202511_Fire_CA" not in os.path.basename(result)   # event name no longer prefixed
        assert "2025-11-11" in os.path.basename(result)

    def test_invalid_filename_raises_valueerror(self, tmp_path):
        from shared_utils.cog_utils import rename_with_event
        src = tmp_path / "ab.tif"
        src.write_bytes(b"dummy")
        with pytest.raises(ValueError):
            rename_with_event(str(src), "Event", quiet=True)

    def test_missing_file_raises_filenotfounderror(self):
        from shared_utils.cog_utils import rename_with_event
        with pytest.raises(FileNotFoundError):
            rename_with_event("/nonexistent/file.tif", "Event", quiet=True)


# ---------------------------------------------------------------------------
# nodata sentinel semantics: None (auto) vs False (opt-out) vs a number.
#
# `False` was added for Satellogic's color composites, which carry a 4th alpha
# band and where 0 is a legitimate 8-bit sample. It is expected to be reused by
# other sensors, so the three-way contract is pinned hard here:
#
#   nodata=None   -> inherit the source's tag, else auto-detect from dtype
#   nodata=False  -> declare NO nodata, and strip any tag the source carries
#   nodata=<num>  -> declare exactly that, after dtype validation
#   nodata=True   -> ValueError (bool subclasses int; 1 is not a sentinel)
# ---------------------------------------------------------------------------

def _write(path, count, dtype, fill, nodata=None, alpha=False, epsg=32617):
    """Minimal 32x32 GeoTIFF helper for the nodata matrix below.

    `alpha=True` tags the LAST band as alpha, at whatever dtype/band count is
    asked for — uint8 RGBA, uint16 RGBA and float32 gray+alpha all matter,
    because the nodata-vs-alpha rule is dtype-independent.
    """
    from rasterio.enums import ColorInterp
    from rasterio.transform import from_bounds
    from rasterio.crs import CRS

    data = np.full((count, 32, 32), fill, dtype=dtype)
    transform = from_bounds(500000, 3200000, 500320, 3200320, 32, 32)
    kwargs = dict(
        driver='GTiff', height=32, width=32, count=count, dtype=dtype,
        crs=CRS.from_epsg(epsg), transform=transform,
    )
    if nodata is not None:
        kwargs['nodata'] = nodata
    with rasterio.open(str(path), 'w', **kwargs) as dst:
        if alpha:
            opaque = (
                np.iinfo(dtype).max
                if np.issubdtype(np.dtype(dtype), np.integer) else 1
            )
            last = count - 1
            data[last] = opaque
            data[last, :8, :] = 0          # nodata fill border
            data[:last, 16:20, 16:20] = 0  # legitimately-black, but VALID imagery
        dst.write(data)
    if alpha:
        # Set colour interpretation on REOPEN, not during creation: the
        # in-create setter silently drops alpha for a 2-band (gray + alpha)
        # file, which would make the fixture quietly assert nothing.
        colors = [ColorInterp.red, ColorInterp.green, ColorInterp.blue]
        with rasterio.open(str(path), 'r+') as dst:
            dst.colorinterp = (
                colors[:count - 1] if count == 4 else [ColorInterp.gray]
            ) + [ColorInterp.alpha]
        with rasterio.open(str(path)) as chk:
            assert chk.colorinterp[-1] is ColorInterp.alpha, (
                "fixture failed to persist an alpha band"
            )
    return str(path)


@pytest.fixture
def rgba_geotiff(tmp_path):
    """32x32 uint8 RGBA, band 4 = alpha, NO nodata tag (what dump_geotiff_rgb writes)."""
    return _write(tmp_path / "rgba.tif", 4, 'uint8', 120, alpha=True)


@pytest.fixture
def rgb_geotiff(tmp_path):
    """The PRE-FIX shape: 3-band uint8 with black-but-valid imagery, no nodata tag."""
    p = _write(tmp_path / "rgb.tif", 3, 'uint8', 120)
    with rasterio.open(p, 'r+') as dst:
        band = dst.read(1)
        band[16:20, 16:20] = 0
        for i in (1, 2, 3):
            dst.write(band, i)
    return p


class TestNodataSentinelResolution:
    """None / False / number resolve to three distinct declared outcomes."""

    def test_none_declares_no_nodata_for_uint8(self, tmp_path):
        """8-bit imagery never auto-declares a nodata value.

        Previously this resolved to the dtype default of 0, which masked
        legitimately-black pixels. See is_bare_8bit_imagery.
        """
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "u8.tif", 1, 'uint8', 120)
        out = str(tmp_path / "u8_cog.tif")
        convert_to_cog(src, out, nodata=None, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata is None

    def test_none_still_auto_detects_zero_for_uint16(self, tmp_path):
        """The 8-bit carve-out must not leak into other integer dtypes.

        uint16 keeps the dtype default (0) -- it is used for classified /
        quality rasters where 0 genuinely is the fill class.
        """
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "u16.tif", 1, 'uint16', 1200)
        out = str(tmp_path / "u16_cog.tif")
        convert_to_cog(src, out, nodata=None, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata == 0

    def test_explicit_numeric_nodata_still_wins_for_uint8(self, tmp_path):
        """The carve-out applies to auto-detect only; a caller can override."""
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "u8_ovr.tif", 1, 'uint8', 120)
        out = str(tmp_path / "u8_ovr_cog.tif")
        convert_to_cog(src, out, nodata=255, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata == 255

    def test_uint8_source_nodata_tag_is_stripped(self, tmp_path):
        """An 8-bit file that already carries nodata=0 gets the tag removed.

        Files produced before this change declare 0; re-running them through
        convert_to_cog must not silently preserve the bug.
        """
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "u8_tagged.tif", 3, 'uint8', 120, nodata=0)
        out = str(tmp_path / "u8_tagged_cog.tif")
        convert_to_cog(src, out, nodata=None, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata is None

    def test_none_auto_detects_neg9999_for_float32(self, tmp_path):
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "f32.tif", 1, 'float32', 1.5)
        out = str(tmp_path / "f32_cog.tif")
        convert_to_cog(src, out, nodata=None, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata == -9999.0

    def test_none_inherits_an_existing_source_tag(self, tmp_path):
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "tagged.tif", 1, 'float32', 1.5, nodata=-1234.0)
        out = str(tmp_path / "tagged_cog.tif")
        convert_to_cog(src, out, nodata=None, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata == -1234.0

    @pytest.mark.parametrize("dtype,fill", [('uint8', 120), ('float32', 1.5), ('int16', 42)])
    def test_false_declares_no_nodata_for_any_dtype(self, tmp_path, dtype, fill):
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / f"{dtype}.tif", 1, dtype, fill)
        out = str(tmp_path / f"{dtype}_cog.tif")
        convert_to_cog(src, out, nodata=False, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata is None

    def test_explicit_number_is_declared_verbatim(self, tmp_path):
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "n.tif", 1, 'float32', 1.5)
        out = str(tmp_path / "n_cog.tif")
        convert_to_cog(src, out, nodata=-9999.0, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata == -9999.0

    def test_zero_and_false_are_not_interchangeable(self, tmp_path):
        """The crux: `0 == False` in Python, but they must NOT behave alike.
        Resolution is by identity/type, never by truthiness or equality."""
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "z.tif", 1, 'uint8', 120)

        zero_out = str(tmp_path / "zero.tif")
        convert_to_cog(src, zero_out, nodata=0, dst_crs=None, quiet=True)
        false_out = str(tmp_path / "false.tif")
        convert_to_cog(src, false_out, nodata=False, dst_crs=None, quiet=True)

        with rasterio.open(zero_out) as z, rasterio.open(false_out) as f:
            assert z.nodata == 0
            assert f.nodata is None


class TestNodataFalseStripsSourceTag:
    """`False` must override a tag the SOURCE file already carries.

    Both `rio cogeo create` and cog_translate fall back to `src.nodata` when
    none is supplied, so passing None downstream is not sufficient — the tag is
    stripped via a lazy VRT first. Without this the opt-out is silently ignored
    for any producer that writes a nodata tag.
    """

    def test_false_strips_existing_tag(self, tmp_path):
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "tagged.tif", 1, 'float32', 1.5, nodata=-9999.0)
        out = str(tmp_path / "stripped.tif")
        convert_to_cog(src, out, nodata=False, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata is None

    def test_false_strips_existing_tag_through_reprojection(self, tmp_path):
        """The warp step must read the stripped VRT, not the original file."""
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "tagged_warp.tif", 1, 'float32', 1.5, nodata=-9999.0)
        out = str(tmp_path / "stripped_warp.tif")
        convert_to_cog(src, out, nodata=False, dst_crs='EPSG:3857', quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata is None
            assert s.crs.to_epsg() == 3857

    def test_false_strips_existing_tag_through_metadata_path(self, tmp_path):
        """metadata=... routes through in-process cog_translate (the DPS path)."""
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "tagged_meta.tif", 1, 'float32', 1.5, nodata=-9999.0)
        out = str(tmp_path / "stripped_meta.tif")
        convert_to_cog(
            src, out, nodata=False, dst_crs=None, quiet=True,
            metadata={'ACTIVATION_EVENT': '202406_Flood_TX'},
        )
        with rasterio.open(out) as s:
            assert s.nodata is None
            assert s.tags().get('ACTIVATION_EVENT') == '202406_Flood_TX'

    def test_strip_does_not_disturb_filename_derived_metadata(self, tmp_path):
        """The VRT redirects PIXELS only. resolve_metadata still parses the
        activation event from the real basename -- if the VRT path leaked into
        that call, HAZARD/LOCATION would be derived from '...nonodata.tmp.vrt'."""
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "202406_Flood_TX_scene.tif", 1, 'float32', 1.5,
                     nodata=-9999.0)
        out = str(tmp_path / "meta_name.tif")
        convert_to_cog(
            src, out, nodata=False, dst_crs=None, quiet=True,
            metadata={'ACTIVATION_EVENT': '202406_Flood_TX'},
        )
        with rasterio.open(out) as s:
            tags = s.tags()
        assert tags.get('HAZARD') == 'Flood'
        assert tags.get('LOCATION') == 'TX'

    def test_false_preserves_pixel_values(self, tmp_path):
        """Stripping the tag must not touch the data."""
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "pix.tif", 1, 'float32', 1.5, nodata=-9999.0)
        out = str(tmp_path / "pix_cog.tif")
        convert_to_cog(src, out, nodata=False, dst_crs=None, quiet=True)
        with rasterio.open(src) as a, rasterio.open(out) as b:
            np.testing.assert_array_equal(a.read(1), b.read(1))

    def test_strip_vrt_is_cleaned_up(self, tmp_path):
        import glob
        import tempfile as _tempfile
        from shared_utils.cog_utils import convert_to_cog

        pattern = os.path.join(_tempfile.gettempdir(), '*.nonodata.tmp.vrt')
        before = set(glob.glob(pattern))
        src = _write(tmp_path / "cleanup.tif", 1, 'float32', 1.5, nodata=-9999.0)
        convert_to_cog(src, str(tmp_path / "cleanup_cog.tif"),
                       nodata=False, dst_crs=None, quiet=True)
        assert set(glob.glob(pattern)) == before, "temp VRT leaked into /tmp"


class TestNodataBoolGuard:
    """bool subclasses int, so True/False must be resolved by TYPE, not value."""

    def test_true_raises_valueerror(self, tmp_path):
        """Otherwise `True` would validate as the numeric 1 and be declared."""
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "t.tif", 1, 'uint8', 120)
        with pytest.raises(ValueError, match="nodata=True is not a no-data value"):
            convert_to_cog(src, str(tmp_path / "t_cog.tif"),
                           nodata=True, dst_crs=None, quiet=True)

    def test_numpy_false_behaves_like_python_false(self, tmp_path):
        """np.False_ is NOT the `False` singleton -- an `is False` check misses it."""
        assert np.False_ is not False  # guards the premise of this test
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "npf.tif", 1, 'uint8', 120)
        out = str(tmp_path / "npf_cog.tif")
        convert_to_cog(src, out, nodata=np.False_, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata is None

    def test_numpy_true_raises(self, tmp_path):
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "npt.tif", 1, 'uint8', 120)
        with pytest.raises(ValueError, match="nodata=True is not a no-data value"):
            convert_to_cog(src, str(tmp_path / "npt_cog.tif"),
                           nodata=np.bool_(True), dst_crs=None, quiet=True)

    def test_false_skips_dtype_validation(self, tmp_path):
        """An out-of-range NUMBER raises; the False sentinel never does."""
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "v.tif", 1, 'uint8', 120)
        with pytest.raises(ValueError, match="nodata=999"):
            convert_to_cog(src, str(tmp_path / "bad.tif"),
                           nodata=999, dst_crs=None, quiet=True)
        # Same file, sentinel instead of a number: no raise.
        out = str(tmp_path / "ok.tif")
        convert_to_cog(src, out, nodata=False, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata is None


class TestNodataVersusAlphaBand:
    """The motivating case: a scalar nodata SHADOWS an alpha band."""

    def test_false_preserves_the_alpha_band(self, rgba_geotiff, tmp_path):
        from rasterio.enums import ColorInterp
        from shared_utils.cog_utils import convert_to_cog
        out = str(tmp_path / "rgba_cog.tif")
        convert_to_cog(rgba_geotiff, out, nodata=False, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.count == 4
            assert s.colorinterp[3] is ColorInterp.alpha
            assert s.nodata is None

    def test_false_keeps_black_pixels_valid_and_border_masked(self, rgba_geotiff, tmp_path):
        """An RGB render (titiler reads indexes 1,2,3) must see the right mask."""
        from shared_utils.cog_utils import convert_to_cog
        out = str(tmp_path / "rgba_mask.tif")
        convert_to_cog(rgba_geotiff, out, nodata=False, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            rgb_mask = s.read_masks([1, 2, 3])
        assert (rgb_mask[:, 16:20, 16:20] == 255).all(), (
            "black-but-valid imagery must stay valid"
        )
        assert (rgb_mask[:, :8] == 0).all(), "the alpha-marked fill border must be masked"

    def test_scalar_nodata_masks_black_but_valid_imagery(self, rgba_geotiff, tmp_path):
        """The regression: nodata=0 on an RGBA input masks real black pixels."""
        from shared_utils.cog_utils import convert_to_cog
        out = str(tmp_path / "rgba_shadow.tif")
        convert_to_cog(rgba_geotiff, out, nodata=0, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata == 0
            rgb_mask = s.read_masks([1, 2, 3])
        assert (rgb_mask[:, 16:20, 16:20] == 0).all(), (
            "with nodata=0 the black-but-valid imagery is masked -- exactly "
            "what the alpha band exists to prevent"
        )

    def test_rasterio_warns_that_nodata_shadows_alpha(self, rgba_geotiff, tmp_path):
        import warnings
        from shared_utils.cog_utils import convert_to_cog
        out = str(tmp_path / "rgba_warn.tif")
        convert_to_cog(rgba_geotiff, out, nodata=0, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                s.dataset_mask()
        assert any("shadowing the alpha band" in str(w.message) for w in caught)

    def test_three_band_composite_with_nodata_zero_is_the_original_bug(
        self, rgb_geotiff, tmp_path
    ):
        """Pre-fix shape: no alpha, nodata=0 -> black imagery reads as nodata."""
        from shared_utils.cog_utils import convert_to_cog
        out = str(tmp_path / "rgb_cog.tif")
        convert_to_cog(rgb_geotiff, out, nodata=0, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            mask = s.dataset_mask()
        assert (mask[16:20, 16:20] == 0).all()

    def test_false_output_is_a_valid_cog(self, rgba_geotiff, tmp_path):
        from shared_utils.cog_utils import convert_to_cog, validate_cog
        out = str(tmp_path / "rgba_valid.tif")
        convert_to_cog(rgba_geotiff, out, nodata=False, dst_crs=None, quiet=True)
        is_valid, details = validate_cog(out)
        assert is_valid, details

    def test_false_through_metadata_path_keeps_alpha_and_tags(self, rgba_geotiff, tmp_path):
        from rasterio.enums import ColorInterp
        from shared_utils.cog_utils import convert_to_cog
        out = str(tmp_path / "rgba_meta.tif")
        convert_to_cog(
            rgba_geotiff, out, nodata=False, dst_crs=None, quiet=True,
            metadata={'ACTIVATION_EVENT': '202406_Flood_TX', 'SOURCE': 'CSDA'},
        )
        with rasterio.open(out) as s:
            assert s.nodata is None
            assert s.count == 4
            assert s.colorinterp[3] is ColorInterp.alpha
            assert s.tags().get('ACTIVATION_EVENT') == '202406_Flood_TX'


# ---------------------------------------------------------------------------
# An alpha band suppresses nodata auto-detection at ANY bit depth.
#
# The 8-bit-only rule (is_bare_8bit_imagery) was too narrow: shadowing is a
# property of declaring a scalar alongside alpha, not of the dtype. A uint16
# RGBA -- the shape Satellogic source rasters arrive in -- auto-detected the
# uint16 default of 0 and shadowed its own alpha band.
#
#   carries alpha (any dtype)     -> no nodata; alpha carries validity
#   8-bit, 1 or 3 bands, no alpha -> no nodata; 0 is a legitimate sample
#   uint16/int16+, 1 or 3 bands   -> unchanged (0 / -9999)
#   float                         -> unchanged (-9999)
#   explicit numeric nodata=N     -> still wins
# ---------------------------------------------------------------------------

@pytest.fixture
def rgba_uint16_geotiff(tmp_path):
    """32x32 uint16 RGBA, band 4 = alpha, NO nodata tag. The headline case."""
    return _write(tmp_path / "rgba_u16.tif", 4, 'uint16', 1200, alpha=True)


@pytest.fixture
def multispectral_uint16_geotiff(tmp_path):
    """32x32 uint16 4-band B/G/R/NIR: FOUR bands and NO alpha.

    The counterexample that forbids inferring alpha from a band count of 4 --
    this is Satellogic's own TOA stack, and it must keep declaring nodata.
    """
    return _write(tmp_path / "ms_u16.tif", 4, 'uint16', 1200)


class TestCarriesAlphaBandPredicate:
    """The predicate itself: colour interpretation, never band count."""

    def test_alpha_last_band_is_detected(self):
        from rasterio.enums import ColorInterp
        from shared_utils.cog_utils import carries_alpha_band
        assert carries_alpha_band(
            (ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha)
        )

    def test_gray_plus_alpha_is_detected(self):
        from rasterio.enums import ColorInterp
        from shared_utils.cog_utils import carries_alpha_band
        assert carries_alpha_band((ColorInterp.gray, ColorInterp.alpha))

    def test_four_undefined_bands_are_not_alpha(self):
        """What GDAL reports for a 4-band uint16 multispectral stack."""
        from rasterio.enums import ColorInterp
        from shared_utils.cog_utils import carries_alpha_band
        assert not carries_alpha_band(
            (ColorInterp.gray, ColorInterp.undefined,
             ColorInterp.undefined, ColorInterp.undefined)
        )

    def test_rgb_is_not_alpha(self):
        from rasterio.enums import ColorInterp
        from shared_utils.cog_utils import carries_alpha_band
        assert not carries_alpha_band(
            (ColorInterp.red, ColorInterp.green, ColorInterp.blue)
        )

    def test_empty_colorinterp_is_not_alpha(self):
        from shared_utils.cog_utils import carries_alpha_band
        assert not carries_alpha_band(())

    def test_is_bare_8bit_imagery_is_unchanged(self):
        """The 8-bit predicate keeps its name and its exact behaviour."""
        from shared_utils.cog_utils import is_bare_8bit_imagery
        assert is_bare_8bit_imagery('uint8', 1)
        assert is_bare_8bit_imagery('uint8', 3)
        assert is_bare_8bit_imagery('int8', 3)
        assert not is_bare_8bit_imagery('uint8', 4)
        assert not is_bare_8bit_imagery('uint16', 3)
        assert not is_bare_8bit_imagery('float32', 1)


class TestAlphaBandSuppressesNodataAtAnyDtype:
    """nodata=None on an alpha-carrying raster resolves to NO nodata."""

    def test_uint16_rgba_declares_no_nodata(self, rgba_uint16_geotiff, tmp_path):
        """The headline: uint16 RGBA used to auto-detect 0 and shadow its alpha."""
        from rasterio.enums import ColorInterp
        from shared_utils.cog_utils import convert_to_cog
        out = str(tmp_path / "u16_rgba_cog.tif")
        convert_to_cog(rgba_uint16_geotiff, out, nodata=None, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata is None
            assert s.count == 4
            assert s.colorinterp[3] is ColorInterp.alpha

    def test_uint16_rgba_alpha_still_carries_validity(self, rgba_uint16_geotiff, tmp_path):
        """The point of the fix: black imagery stays valid, the fill border does not."""
        from shared_utils.cog_utils import convert_to_cog
        out = str(tmp_path / "u16_rgba_mask.tif")
        convert_to_cog(rgba_uint16_geotiff, out, nodata=None, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            rgb_mask = s.read_masks([1, 2, 3])
        assert (rgb_mask[:, 16:20, 16:20] == 255).all(), (
            "black-but-valid uint16 imagery must stay valid"
        )
        assert (rgb_mask[:, :8] == 0).all(), "the alpha-marked fill border must be masked"

    def test_uint16_rgba_source_nodata_tag_is_stripped(self, tmp_path):
        """A tag written under the old behaviour is removed, not preserved."""
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "u16_rgba_tagged.tif", 4, 'uint16', 1200,
                     nodata=0, alpha=True)
        out = str(tmp_path / "u16_rgba_tagged_cog.tif")
        convert_to_cog(src, out, nodata=None, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata is None

    def test_uint8_rgba_declares_no_nodata(self, rgba_geotiff, tmp_path):
        """8-bit RGBA reaches the same outcome without needing nodata=False.

        is_bare_8bit_imagery covers 1 and 3 bands only, so a 4-band uint8 file
        previously fell through to the dtype default of 0.
        """
        from shared_utils.cog_utils import convert_to_cog
        out = str(tmp_path / "u8_rgba_auto.tif")
        convert_to_cog(rgba_geotiff, out, nodata=None, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata is None

    def test_float32_gray_plus_alpha_declares_no_nodata(self, tmp_path):
        """Dtype-independence, checked past the integer dtypes."""
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "f32_alpha.tif", 2, 'float32', 1.5, alpha=True)
        out = str(tmp_path / "f32_alpha_cog.tif")
        convert_to_cog(src, out, nodata=None, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata is None, "float32 + alpha must not fall back to -9999"

    def test_explicit_numeric_nodata_still_wins_over_alpha(
        self, rgba_uint16_geotiff, tmp_path
    ):
        """The carve-out is auto-detect only; a caller can still override."""
        from shared_utils.cog_utils import convert_to_cog
        out = str(tmp_path / "u16_rgba_ovr.tif")
        convert_to_cog(rgba_uint16_geotiff, out, nodata=42, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata == 42

    def test_uint16_rgba_output_is_a_valid_cog(self, rgba_uint16_geotiff, tmp_path):
        from shared_utils.cog_utils import convert_to_cog, validate_cog
        out = str(tmp_path / "u16_rgba_valid.tif")
        convert_to_cog(rgba_uint16_geotiff, out, nodata=None, dst_crs=None, quiet=True)
        is_valid, details = validate_cog(out)
        assert is_valid, details

    def test_uint16_rgba_through_metadata_path(self, rgba_uint16_geotiff, tmp_path):
        """The cog_translate route resolves nodata identically."""
        from rasterio.enums import ColorInterp
        from shared_utils.cog_utils import convert_to_cog
        out = str(tmp_path / "u16_rgba_meta.tif")
        convert_to_cog(
            rgba_uint16_geotiff, out, nodata=None, dst_crs=None, quiet=True,
            metadata={'ACTIVATION_EVENT': '202406_Flood_TX', 'SOURCE': 'CSDA'},
        )
        with rasterio.open(out) as s:
            assert s.nodata is None
            assert s.colorinterp[3] is ColorInterp.alpha
            assert s.tags().get('ACTIVATION_EVENT') == '202406_Flood_TX'


class TestFourBandMultispectralKeepsItsNodata:
    """The anti-over-reach pin: 4 bands is NOT evidence of an alpha band.

    Satellogic's own uint16 B/G/R/NIR TOA stack has four bands and no alpha.
    Inferring alpha from the band count would strip nodata from every source
    multispectral raster in the archive.
    """

    def test_uint16_four_band_no_alpha_auto_detects_zero(
        self, multispectral_uint16_geotiff, tmp_path
    ):
        from shared_utils.cog_utils import convert_to_cog
        out = str(tmp_path / "ms_u16_cog.tif")
        convert_to_cog(multispectral_uint16_geotiff, out, nodata=None,
                       dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata == 0, (
                "a 4-band multispectral stack has no alpha band and must keep "
                "the uint16 dtype default"
            )

    def test_uint16_four_band_no_alpha_has_no_alpha_colorinterp(
        self, multispectral_uint16_geotiff
    ):
        """Guards the fixture itself: GDAL must not be tagging band 4 alpha."""
        from rasterio.enums import ColorInterp
        from shared_utils.cog_utils import carries_alpha_band
        with rasterio.open(multispectral_uint16_geotiff) as s:
            assert s.count == 4
            assert s.colorinterp[3] is not ColorInterp.alpha
            assert not carries_alpha_band(s.colorinterp)

    def test_uint16_four_band_no_alpha_inherits_an_existing_tag(self, tmp_path):
        """An operator-set tag on a multispectral stack survives untouched."""
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "ms_tagged.tif", 4, 'uint16', 1200, nodata=65535)
        out = str(tmp_path / "ms_tagged_cog.tif")
        convert_to_cog(src, out, nodata=None, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata == 65535

    def test_int16_three_band_still_declares_neg9999(self, tmp_path):
        """Non-8-bit, non-alpha integer rasters are untouched by both rules."""
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "i16_3b.tif", 3, 'int16', 1200)
        out = str(tmp_path / "i16_3b_cog.tif")
        convert_to_cog(src, out, nodata=None, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata == -9999


class TestCreationOptionParity:
    """convert_to_cog has two backends and they must produce the same GDAL
    creation options.

    Regression (2026-08-26): the in-process `cog_translate` backend -- the one
    every metadata-embedding caller uses, including both simple_disaster
    notebooks and every `--metadata-json` CLI -- built its profile
    independently of the subprocess `rio cogeo create` backend and was missing
    two entries.

    `NUM_THREADS=ALL_CPUS` cost ~1.9x (single-threaded compression).
    `BIGTIFF=IF_SAFER` was far worse: rio-cogeo writes an UNCOMPRESSED scratch
    dataset and then adds overviews to it, so a source under 4 GB raw can cross
    the classic-TIFF 4 GB offset ceiling after GDAL's IF_NEEDED heuristic has
    already chosen classic. libtiff then thrashes in TIFFRewriteDirectory and
    GDALClose never practically returns. A 3.70 GB SkySat scene (4.38 GB with
    overviews) ran >27 min without it and 55.6s with it.

    Neither shows up on a small fixture, so assert on the options themselves.
    """

    COMPRESSIONS = ['ZSTD', 'DEFLATE', 'LZW']

    @pytest.mark.parametrize('compression', COMPRESSIONS)
    def test_threading_and_bigtiff_always_present(self, compression):
        from shared_utils.cog_utils import build_creation_options
        opts = build_creation_options(compression, 9)
        assert opts['NUM_THREADS'] == 'ALL_CPUS'
        assert opts['BIGTIFF'] == 'IF_SAFER'

    @pytest.mark.parametrize('compression', COMPRESSIONS)
    def test_cog_translate_profile_carries_every_creation_option(self, compression):
        """The in-process backend must not drop any shared creation option."""
        from shared_utils.cog_utils import (
            build_creation_options, _build_cog_translate_profile,
        )
        opts = build_creation_options(compression, 9)
        profile = _build_cog_translate_profile(compression, 9)
        missing = {k: v for k, v in opts.items() if profile.get(k) != v}
        assert not missing, f"in-process profile dropped {missing}"

    def test_gdal_config_sets_num_threads(self):
        """cog_translate runs under a bare rasterio.Env() unless we pass this."""
        from shared_utils.cog_utils import COG_GDAL_CONFIG
        assert COG_GDAL_CONFIG['GDAL_NUM_THREADS'] == 'ALL_CPUS'

    def test_compression_level_reaches_the_right_key(self):
        from shared_utils.cog_utils import build_creation_options
        assert build_creation_options('ZSTD', 17)['ZSTD_LEVEL'] == 17
        assert build_creation_options('DEFLATE', 7)['ZLEVEL'] == 7

    def test_subprocess_backend_passes_every_creation_option(self, tmp_path, monkeypatch):
        """Capture the real `rio cogeo create` argv and diff it against the
        shared builder -- this is what fails if someone edits one branch only."""
        import subprocess as _sp
        from shared_utils import cog_utils

        captured = {}
        real_run = _sp.run  # bind BEFORE patching; cog_utils.subprocess IS _sp

        def fake_run(cmd, *a, **k):
            if isinstance(cmd, list) and cmd[:3] == ['rio', 'cogeo', 'create']:
                captured['cmd'] = cmd
                # Produce the output the caller expects to exist.
                real_run(['rio', 'cogeo', 'create', cmd[3], cmd[4]], check=True)
                return _sp.CompletedProcess(cmd, 0, stdout='', stderr='')
            return real_run(cmd, *a, **k)

        monkeypatch.setattr(cog_utils.subprocess, 'run', fake_run)

        src = _write(tmp_path / "parity.tif", 3, 'uint8', 40)
        out = str(tmp_path / "parity_cog.tif")
        cog_utils.convert_to_cog(
            src, out, nodata=None, dst_crs=None, quiet=True,
            compression='ZSTD', compression_level=9,
        )

        assert 'cmd' in captured, "subprocess backend was not exercised"
        cmd = captured['cmd']
        passed = {cmd[i + 1] for i, tok in enumerate(cmd) if tok == '--co'}
        expected = {
            f'{k}={v}'
            for k, v in cog_utils.build_creation_options('ZSTD', 9).items()
        }
        assert expected <= passed, f"subprocess backend dropped {expected - passed}"
