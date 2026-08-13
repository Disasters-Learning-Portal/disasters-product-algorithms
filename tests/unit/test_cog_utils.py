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
    """Minimal 32x32 GeoTIFF helper for the nodata matrix below."""
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
            data[3] = 255
            data[3, :8, :] = 0          # nodata fill border
            data[:3, 16:20, 16:20] = 0  # legitimately-black, but VALID imagery
        dst.write(data)
        if alpha:
            dst.colorinterp = [
                ColorInterp.red, ColorInterp.green,
                ColorInterp.blue, ColorInterp.alpha,
            ]
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

    def test_none_auto_detects_zero_for_uint8(self, tmp_path):
        from shared_utils.cog_utils import convert_to_cog
        src = _write(tmp_path / "u8.tif", 1, 'uint8', 120)
        out = str(tmp_path / "u8_cog.tif")
        convert_to_cog(src, out, nodata=None, dst_crs=None, quiet=True)
        with rasterio.open(out) as s:
            assert s.nodata == 0

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
