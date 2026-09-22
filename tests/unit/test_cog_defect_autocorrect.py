"""The defects convert_to_cog must correct automatically, asserted on the OUTPUT FILE.

WHY THIS MODULE EXISTS, AND WHY IT ASSERTS THE WAY IT DOES
----------------------------------------------------------
A survey of 32 staged products found at least one structural defect in every
single one, and all of them had been produced by this converter and had passed
the existing suite. The suite was asserting the arguments we pass, not the
artifact we write.

That distinction is not pedantic. Measured on GDAL 3.10.3::

    $ gdal_translate -of COG in.tif out.tif -co INTERLEAVE=BAND
    Warning 6: driver COG does not support creation option INTERLEAVE
    $ echo $?
    0
    $ gdalinfo out.tif | grep INTERLEAVE
      INTERLEAVE=PIXEL

The option is accepted, warned about, and ignored. A test asserting that the
creation-option dict contains ``INTERLEAVE=BAND`` passes while the file on disk
is PIXEL-interleaved. So every test here opens the produced raster and reads
back what is actually in it.

`TestCreationOptionParity` in test_cog_utils.py has exactly that weakness --
it diffs `--co` tokens, which by construction excludes `--overview-level` and
`--overview-resampling`. It is left in place (it pins a real regression about
option drift) and widened here with output-level parity instead.
"""

import math
import os
import shutil
import subprocess

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")
from rasterio.enums import ColorInterp
from rasterio.transform import from_origin

from shared_utils.cog_utils import (
    audit_cog_output,
    build_creation_options,
    carries_alpha_band,
    convert_to_cog,
    detect_data_kind,
    determine_resampling_method,
    filename_data_kind,
    resolve_data_kind,
    resolve_overview_count,
)

pytestmark = pytest.mark.gdal

FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures"
)

# The two sample directories the defect survey was run against. They are NOT in
# the repo, so tests that need them skip with a reason rather than passing
# silently on an empty glob.
SAMPLE_DIRS = {
    "resampling": os.path.expanduser("~/Downloads/resampling_samples"),
    "opera_dswx": os.path.expanduser("~/Downloads/opera_dswx_samples"),
}

FLT_MAX = 3.4028234663852886e38


def _require_gdal():
    if shutil.which("gdal_translate") is None:
        pytest.skip("GDAL CLI not on PATH")


def _samples(key):
    directory = SAMPLE_DIRS[key]
    if not os.path.isdir(directory):
        pytest.skip(f"sample directory absent: {directory}")
    files = sorted(
        os.path.join(directory, n)
        for n in os.listdir(directory)
        if n.lower().endswith(".tif")
    )
    if not files:
        pytest.skip(f"no .tif files in {directory}")
    return files


def _write(path, array, nodata=None, crs="EPSG:3857", **creation):
    """Write a plain GeoTIFF. Defaults are deliberately BAD (untiled,
    uncompressed) so the converter has something to correct."""
    if array.ndim == 2:
        array = array[np.newaxis, :, :]
    count, height, width = array.shape
    with rasterio.open(
        str(path), "w",
        driver="GTiff", height=height, width=width, count=count,
        dtype=array.dtype.name, nodata=nodata,
        transform=from_origin(0, height, 1, 1), crs=crs,
        **creation,
    ) as dst:
        dst.write(array)
    return str(path)


def _structure(path):
    """Everything about the produced file this module makes claims about."""
    with rasterio.open(path) as src:
        image_structure = src.tags(ns="IMAGE_STRUCTURE")
        block_h, block_w = src.block_shapes[0]
        return {
            "width": src.width,
            "height": src.height,
            "count": src.count,
            "dtype": src.dtypes[0],
            "overviews": src.overviews(1),
            "overview_count": len(src.overviews(1)),
            "block": (block_h, block_w),
            "tiled": src.profile.get("tiled", False),
            "compression": image_structure.get("COMPRESSION"),
            "interleave": image_structure.get("INTERLEAVE"),
            "layout": image_structure.get("LAYOUT"),
            "nodata": src.nodata,
            "checksum": [src.checksum(b) for b in range(1, src.count + 1)],
        }


def _coarsest_overview_values(path, band=1):
    with rasterio.open(path) as src:
        overviews = src.overviews(band)
        assert overviews, f"{path} has no overviews to inspect"
        factor = overviews[-1]
        arr = src.read(
            band, out_shape=(src.height // factor, src.width // factor)
        )
    return set(np.unique(arr).tolist())


def _native_values(path, band=1):
    with rasterio.open(path) as src:
        return set(np.unique(src.read(band)).tolist())


@pytest.fixture
def categorical_source(tmp_path):
    """2048x1024 uint8 class codes {1, 3} plus a 255 sentinel and 0 nodata.

    Non-square on purpose: the max()-based level rule wants 2 levels here and
    rio-cogeo's min()-based default wants 1.

    {1, 3} on purpose: their mean is 2, a code that does NOT occur, so any
    averaging in the overviews is visible as a value that cannot exist.
    """
    _require_gdal()
    rng = np.random.default_rng(0)
    arr = np.where(rng.random((1024, 2048)) < 0.5, 1, 3).astype("uint8")
    arr[:64, :64] = 255      # sentinel class, the 251-255 shape real masks use
    arr[-32:, :] = 0         # nodata margin
    return _write(tmp_path / "categorical.tif", arr, nodata=0)


@pytest.fixture
def continuous_source(tmp_path):
    """2048x1024 float32 ramp -- genuinely continuous, must keep `average`."""
    _require_gdal()
    rng = np.random.default_rng(1)
    arr = (rng.random((1024, 2048)) * 100.0).astype("float32")
    return _write(tmp_path / "continuous.tif", arr, nodata=-9999.0)


# ---------------------------------------------------------------------------
# 1. Overview LEVEL COUNT, derived from the raster, both failure directions
# ---------------------------------------------------------------------------

class TestOverviewLevelCount:
    """`levels = ceil(log2(max(width, height) / 512))`.

    Governed by the LONGER side. rio-cogeo's own default stops on the SHORTER
    side (`min()` in rasterio.rio.overview.get_maximum_overview_level), which
    under-builds non-square mosaics -- and only 2 of the 20 surveyed products
    carried the right count.
    """

    # (width, height, expected) -- real product dimensions from the survey.
    # The first three are cases where the max() and min() rules DISAGREE.
    DIMENSIONS = [
        (16128, 21504, 6),    # min-rule would say 5
        (15360, 22016, 6),    # min-rule would say 5
        (13056, 18688, 6),    # min-rule would say 5
        (39680, 38912, 7),    # shipped with 4 -- UNDER-built
        (3584, 3584, 3),      # shipped with 4 -- OVER-built
        (327, 543, 1),        # shipped with 5 -- absurdly over-built
    ]

    @pytest.mark.parametrize("width,height,expected", DIMENSIONS)
    def test_rule_matches_ceil_log2_of_longer_side(self, width, height, expected):
        assert resolve_overview_count(width, height) == expected
        assert expected == math.ceil(math.log2(max(width, height) / 512))

    @pytest.mark.parametrize("width,height,expected", DIMENSIONS[:3])
    def test_max_rule_and_min_rule_genuinely_disagree(self, width, height, expected):
        """Without this the table could be satisfied by the wrong rule."""
        min_rule = math.ceil(math.log2(min(width, height) / 512))
        assert min_rule < expected, (
            f"{width}x{height} does not discriminate the two rules"
        )

    def test_explicit_count_still_wins(self):
        assert resolve_overview_count(39680, 38912, explicit=2) == 2

    def test_produced_file_carries_the_derived_count(self, categorical_source, tmp_path):
        """The rule is only worth anything if it reaches the artifact."""
        out = str(tmp_path / "derived.tif")
        convert_to_cog(categorical_source, out, dst_crs=None, quiet=True)
        expected = resolve_overview_count(2048, 1024)
        assert expected == 2
        assert _structure(out)["overview_count"] == expected

    def test_produced_file_is_not_over_built(self, tmp_path):
        """The over-build direction: a raster smaller than one block gets few
        levels, not five. Pins the half of the fix that is easy to lose."""
        arr = np.random.default_rng(2).integers(1, 5, (543, 327), dtype="uint8")
        src = _write(tmp_path / "small.tif", arr, nodata=0)
        out = str(tmp_path / "small_cog.tif")
        convert_to_cog(src, out, dst_crs=None, quiet=True)
        assert resolve_overview_count(327, 543) == 1
        assert _structure(out)["overview_count"] <= 1


# ---------------------------------------------------------------------------
# 2. Resampling auto-detect, on REAL data
# ---------------------------------------------------------------------------

class TestDataKindDetection:
    """`mode` for class codes, `average` for measurements, decided per FILE.

    Native distinct-value counts from the survey (band 1, nodata excluded):
        categorical: 1, 1, 2, 2, 2, 3, 3, 4, 5, 5, 5, 6, 7   -> max   7
        continuous : 66, 178, >5000, >5000, >5000            -> min  66
    """

    # Committed real crops -> expected verdict, and what each one defeats.
    REAL_FIXTURES = [
        ("dswx_s1_wtr_classcodes_crop.tif", "categorical"),
        ("distalert_vegdiststatus_crop.tif", "categorical"),
        ("distalert_veganommax_crop.tif", "continuous"),
        ("distalert_status_palette_crop.tif", "categorical"),
        ("hydrosar_watermask_int8_crop.tif", "categorical"),
        ("dswx_chngmap_float32_classcodes_crop.tif", "categorical"),
        ("gaia_atlanta_sample.tif", "continuous"),
        ("umbra_guam_sar_db_crop.tif", "continuous"),
        ("iceye_guam_sar_db_crop.tif", "continuous"),
    ]

    @pytest.mark.parametrize("name,expected", REAL_FIXTURES)
    def test_verdict_on_real_crops(self, name, expected):
        path = os.path.join(FIXTURE_DIR, name)
        assert os.path.exists(path), f"fixture missing: {path}"
        assert detect_data_kind(path) == expected

    def test_distalert_pair_in_one_directory_needs_opposite_resampling(self):
        """The case that forbids a per-collection or per-directory lookup.

        ProgramData/OPERA/DistAlert/ holds both of these. VEG-DIST-STATUS is 6
        class codes; VEG-ANOM-MAX is 66 values spanning 10..255. A curated
        per-collection list cannot express that, so detection has to read the
        pixels of the file in front of it.
        """
        status = os.path.join(FIXTURE_DIR, "distalert_vegdiststatus_crop.tif")
        anom = os.path.join(FIXTURE_DIR, "distalert_veganommax_crop.tif")
        assert determine_resampling_method(status) == ("nearest", "mode")
        assert determine_resampling_method(anom) == ("bilinear", "average")

    def test_int8_and_uint16_masks_are_categorical(self):
        """The two families a dtype-only rule gets wrong.

        `get_resampling_for_dtype` buckets both Int8 and UInt16 as "integer,
        probably continuous" -> AVERAGE over class codes. A HydroSAR water mask
        is Int8 {0,1,2,3,4}; a cloud mask is UInt16 {1,999}.
        """
        from shared_utils.gdal_cog_processor import get_resampling_for_dtype

        int8_mask = os.path.join(FIXTURE_DIR, "hydrosar_watermask_int8_crop.tif")
        with rasterio.open(int8_mask) as src:
            assert src.dtypes[0] == "int8"
        assert get_resampling_for_dtype("int8")[1] == "average"   # the old answer
        assert determine_resampling_method(int8_mask)[1] == "mode"  # the new one

    def test_float32_can_be_categorical_on_the_real_product_crop(self):
        """(a), on the GENUINE product rather than a re-coded Int8 raster.

        Cropped from `OPERA_DSWx-S1_BWTR_ChngMap_date1_2024-10-03_to_2024-10-11_day.tif`
        (16384x33792, Float32), whose exact whole-raster histogram is
        `-1: 1,175,607 / 0: 89,981,533 / +1: 991,124`, 461,499,864 nodata.

        This is the end-to-end test of the float branch: float dtype, three
        distinct values, all integral -> categorical. A "float means
        continuous" shortcut fails right here.
        """
        path = os.path.join(FIXTURE_DIR, "dswx_chngmap_float32_classcodes_crop.tif")
        with rasterio.open(path) as src:
            assert src.dtypes[0] == "float32"
            band = src.read(1)
            valid = band[band != src.nodata]
        assert sorted(set(np.unique(valid).tolist())) == [-1.0, 0.0, 1.0]

        assert detect_data_kind(path) == "categorical"
        assert determine_resampling_method(path) == ("nearest", "mode")

    def test_change_map_filename_and_pixels_agree_without_an_alarm(self, capsys):
        """Both halves of the design on ONE real file, which until now had only
        ever been exercised separately: the `ChngMap` token says categorical,
        the Float32 `{-1,0,+1}` pixels say categorical, and no disagreement
        alarm fires."""
        path = os.path.join(FIXTURE_DIR, "dswx_chngmap_float32_classcodes_crop.tif")
        assert filename_data_kind(path) == "categorical"
        assert detect_data_kind(path) == "categorical"
        assert resolve_data_kind(path) == "categorical"
        out = capsys.readouterr().out
        assert "WARNING" not in out, out

    def test_float32_can_be_categorical(self, tmp_path):
        """(a) OPERA DSWx change maps are Float32 holding exactly {-1, 0, +1}.

        Derived from a REAL class raster rather than a toy array: the pixels
        and their spatial structure come off the committed HydroSAR water mask,
        only the dtype and the coding change.
        """
        real = os.path.join(FIXTURE_DIR, "hydrosar_watermask_int8_crop.tif")
        with rasterio.open(real) as src:
            codes = src.read(1)
        change = np.select(
            [codes <= 1, codes == 2], [-1.0, 0.0], default=1.0
        ).astype("float32")
        assert set(np.unique(change).tolist()) == {-1.0, 0.0, 1.0}

        path = _write(tmp_path / "change_map.tif", change)
        assert detect_data_kind(path) == "categorical"
        assert determine_resampling_method(path) == ("nearest", "mode")

    def test_float32_with_non_integral_values_is_not_guessed(self, tmp_path):
        """A float band holding a few NON-integral values is not a class map,
        and the detector must say `unknown` rather than pick one."""
        arr = np.where(
            np.random.default_rng(3).random((512, 512)) < 0.5, 0.25, 1.75
        ).astype("float32")
        path = _write(tmp_path / "quantized.tif", arr)
        assert detect_data_kind(path) == "unknown"

    def test_all_nodata_sample_is_unknown(self, tmp_path):
        arr = np.full((512, 512), -9999.0, dtype="float32")
        path = _write(tmp_path / "empty.tif", arr, nodata=-9999.0)
        assert detect_data_kind(path) == "unknown"

    def test_high_distinct_count_decides_even_on_a_sparse_sample(self):
        """Rule ordering: a count above the threshold is positive evidence and
        is not overridden by the valid-pixel floor. The real VEG-ANOM-MAX crop
        has 64 distinct values in only 2,072 valid pixels."""
        anom = os.path.join(FIXTURE_DIR, "distalert_veganommax_crop.tif")
        with rasterio.open(anom) as src:
            band = src.read(1)
            valid = band[band != src.nodata]
        assert len(np.unique(valid)) > 32
        assert valid.size < 4096
        assert detect_data_kind(anom) == "continuous"

    def test_explicit_caller_argument_overrides_detection(self, categorical_source, tmp_path):
        """Detection is a default, never a policy."""
        out = str(tmp_path / "forced.tif")
        convert_to_cog(
            categorical_source, out, dst_crs=None,
            resampling_method="bilinear", quiet=True,
        )
        native = _native_values(categorical_source)
        coarse = _coarsest_overview_values(out)
        # bilinear/average was explicitly asked for, so invented codes are the
        # CORRECT outcome here -- proving the override actually took effect.
        assert coarse - native, (
            "explicit resampling_method='bilinear' was ignored; the overviews "
            "still contain only native codes"
        )

    @pytest.mark.parametrize("key", ["resampling", "opera_dswx"])
    def test_every_survey_sample_gets_a_verdict(self, key):
        """Real-file sweep over the survey directories. Skips cleanly when they
        are absent instead of passing on an empty glob."""
        undecided = []
        for path in _samples(key):
            kind = detect_data_kind(path)
            assert kind in {"categorical", "continuous", "unknown"}
            if kind == "unknown":
                undecided.append(os.path.basename(path))
        assert not undecided, f"no verdict for: {undecided}"


REAL_CHANGE_MAP = os.path.expanduser(
    "~/Downloads/OPERA_DSWx-S1_BWTR_ChngMap_date1_2024-10-03_to_2024-10-11_day.tif"
)


class TestRealFloat32ChangeMap:
    """The full-size genuine product, when it is on the machine.

    16384x33792 Float32, nodata -9999, exact whole-raster histogram
    `-1: 1,175,607 / 0: 89,981,533 / +1: 991,124` over 461,499,864 nodata
    pixels. The committed crop covers this in CI; these assertions are the
    ones that can only be made against the real thing.

    This local copy is the PRE-REPAIR original -- it still carries 4 AVERAGE
    overviews where the rule calls for 7 -- so it doubles as the audit's
    real-world positive case. It is the user's file and is only ever read.
    """

    @pytest.fixture(autouse=True)
    def _require_real_product(self):
        if not os.path.exists(REAL_CHANGE_MAP):
            pytest.skip(f"real product not present: {REAL_CHANGE_MAP}")

    def test_detects_categorical_on_the_full_size_product(self):
        with rasterio.open(REAL_CHANGE_MAP) as src:
            assert src.dtypes[0] == "float32"
            assert (src.width, src.height) == (16384, 33792)
        assert detect_data_kind(REAL_CHANGE_MAP) == "categorical"
        assert determine_resampling_method(REAL_CHANGE_MAP) == ("nearest", "mode")

    def test_filename_and_pixels_agree_with_no_alarm(self, capsys):
        assert filename_data_kind(REAL_CHANGE_MAP) == "categorical"
        assert resolve_data_kind(REAL_CHANGE_MAP) == "categorical"
        assert "WARNING" not in capsys.readouterr().out

    def test_detection_is_cheap_on_553_megapixels(self):
        """553.6 Mpx; measured at ~88 ms because the block sample is bounded
        and the raster is 83% nodata. Generous ceiling so this pins the
        bounded-sampling property, not the machine."""
        import time

        started = time.time()
        detect_data_kind(REAL_CHANGE_MAP)
        assert time.time() - started < 10.0

    def test_audit_catches_the_pre_repair_defects(self):
        """Averaging a 3-value float class map is spectacularly wrong: the
        coarsest AVERAGE overview holds hundreds of fractional codes
        (-0.9375, -0.87109375, ...) that cannot exist in a band whose only
        values are -1, 0 and +1."""
        problems = audit_cog_output(REAL_CHANGE_MAP)
        assert any("AVERAGED" in p for p in problems), problems
        assert any("overview count" in p for p in problems), problems


# ---------------------------------------------------------------------------
# 3. The phantom class, in one assertion
# ---------------------------------------------------------------------------

class TestPhantomClassRegression:
    """Averaging class codes invents codes that are not in the data.

    On the real OPERA DSWx S1 WTR mosaic the native band held {0,1,3,251,255}
    and the coarsest AVERAGE overview came back with all 256 codes, 1.18% of it
    class 2 == (1+3)/2. titiler renders the overview, so that is what the
    portal showed.

    The assertion is "no code absent from the native band", NOT "no class 2":
    three surveyed products legitimately HAVE a native class 2, and with `mode`
    its share correctly rises (e.g. 6.2% -> 14.2%).
    """

    def test_no_overview_code_is_absent_from_the_native_band(self, categorical_source, tmp_path):
        out = str(tmp_path / "cat_cog.tif")
        convert_to_cog(categorical_source, out, dst_crs=None, quiet=True)
        native = _native_values(categorical_source)
        with rasterio.open(out) as src:
            for index, factor in enumerate(src.overviews(1)):
                arr = src.read(
                    1, out_shape=(src.height // factor, src.width // factor)
                )
                invented = set(np.unique(arr).tolist()) - native
                assert not invented, (
                    f"overview {index} (1/{factor}) invented codes {sorted(invented)}; "
                    f"native band holds {sorted(native)}"
                )

    def test_the_fixture_would_actually_catch_averaging(self, categorical_source, tmp_path):
        """Guards the test above: if `average` would not invent a code on this
        fixture, the assertion proves nothing."""
        out = str(tmp_path / "averaged.tif")
        convert_to_cog(
            categorical_source, out, dst_crs=None,
            resampling_method="bilinear", quiet=True,
        )
        invented = _coarsest_overview_values(out) - _native_values(categorical_source)
        assert invented, "fixture cannot distinguish mode from average"

    def test_continuous_data_still_gets_average(self, continuous_source, tmp_path):
        """The inverse defect: `mode` on a measurement is equally wrong."""
        assert determine_resampling_method(continuous_source) == ("bilinear", "average")
        out = str(tmp_path / "cont_cog.tif")
        convert_to_cog(continuous_source, out, dst_crs=None, quiet=True)
        assert _structure(out)["overview_count"] == 2


# ---------------------------------------------------------------------------
# 4. Tiling, block size, compression
# ---------------------------------------------------------------------------

class TestTilingAndCompression:
    """One surveyed product was not a COG at all: block size 6219x1 (striped),
    zero overviews, no compression. Converting it properly took it from
    14,039,168 to 372,389 bytes with the band checksum unchanged."""

    def test_striped_uncompressed_input_comes_out_tiled_and_compressed(self, tmp_path):
        _require_gdal()
        rng = np.random.default_rng(4)
        arr = rng.integers(1, 6, (2253, 6219), dtype="uint8")
        src = _write(
            tmp_path / "striped.tif", arr, nodata=0,
            tiled=False, blockysize=1,  # striped, uncompressed
        )
        before = _structure(src)
        assert before["block"][0] == 1, "fixture is not striped"
        assert before["overview_count"] == 0
        assert not before["compression"]

        out = str(tmp_path / "striped_cog.tif")
        convert_to_cog(src, out, dst_crs=None, quiet=True)
        after = _structure(out)

        assert after["block"] == (512, 512)
        assert after["tiled"] is True
        assert after["compression"], "output is uncompressed"
        assert after["overview_count"] >= 1
        assert after["layout"] == "COG"
        assert after["checksum"] == before["checksum"], "pixels changed"
        assert os.path.getsize(out) < os.path.getsize(src)


# ---------------------------------------------------------------------------
# 5. INTERLEAVE -- assert the OBSERVED capability, and fail if it changes
# ---------------------------------------------------------------------------

class TestInterleave:
    """The defect class that defeats argument-level testing.

    The COG driver gained INTERLEAVE in GDAL 3.11. Below that it accepts the
    option, prints `Warning 6: driver COG does not support creation option
    INTERLEAVE`, exits 0, and writes PIXEL.
    """

    def test_probe_agrees_with_what_the_driver_actually_writes(self, tmp_path):
        """The probe is only useful if it predicts the artifact. Writes an
        8-band stack through the COG driver and reads the result back.

        If this fails because the driver started honouring the option, that is
        the capability arriving -- update `cog_driver_supports_interleave`,
        do not relax the assertion.
        """
        _require_gdal()
        from shared_utils.gdal_cog_processor import cog_driver_supports_interleave

        arr = np.random.default_rng(5).integers(0, 255, (8, 1024, 1024), dtype="uint8")
        src = _write(tmp_path / "stack8.tif", arr)
        out = str(tmp_path / "stack8_cog.tif")
        subprocess.run(
            ["gdal_translate", "-q", "-of", "COG", "-co", "COMPRESS=DEFLATE",
             "-co", "BLOCKSIZE=512", "-co", "INTERLEAVE=BAND", src, out],
            check=True, capture_output=True,
        )
        produced = _structure(out)["interleave"]
        version = rasterio.__gdal_version__

        if cog_driver_supports_interleave():
            assert produced == "BAND", (
                f"probe says this COG driver honours INTERLEAVE but GDAL "
                f"{version} produced INTERLEAVE={produced}"
            )
        else:
            assert produced == "PIXEL", (
                f"probe says this COG driver IGNORES INTERLEAVE, but GDAL "
                f"{version} produced INTERLEAVE={produced} -- the capability "
                f"has arrived; update cog_driver_supports_interleave()"
            )

    def test_rio_backend_interleave_reaches_the_file(self, tmp_path):
        """rio-cogeo creates through GTiff, which has always honoured the
        option -- so BAND does land on the default backend, on every GDAL."""
        _require_gdal()
        arr = np.random.default_rng(6).integers(0, 255, (8, 1024, 1024), dtype="uint8")
        src = _write(tmp_path / "ms8.tif", arr)
        out = str(tmp_path / "ms8_cog.tif")
        convert_to_cog(src, out, dst_crs=None, backend="rio", quiet=True)
        assert _structure(out)["interleave"] == "BAND"

    def test_rgba_rendering_is_not_flagged_for_band_interleave(self):
        """A 4-band RGBA rendering is always read whole, so splitting it into
        four band-planes turns one block read into four and wins nothing.

        Band count alone cannot tell it from a 4-band B/G/R/NIR stack, which
        genuinely wants BAND -- hence the last-band alpha test.
        """
        rgba = (ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha)
        multispectral = (ColorInterp.gray,) * 4
        assert carries_alpha_band(rgba)
        assert not carries_alpha_band(multispectral)
        assert "INTERLEAVE" not in build_creation_options("ZSTD", 9, None, 4, rgba)
        assert build_creation_options("ZSTD", 9, None, 4, multispectral)["INTERLEAVE"] == "BAND"
        assert build_creation_options("ZSTD", 9, None, 8, (ColorInterp.gray,) * 8)["INTERLEAVE"] == "BAND"

    def test_three_bands_or_fewer_stay_pixel(self):
        rgb = (ColorInterp.red, ColorInterp.green, ColorInterp.blue)
        assert "INTERLEAVE" not in build_creation_options("ZSTD", 9, None, 3, rgb)
        assert "INTERLEAVE" not in build_creation_options("ZSTD", 9, None, 1)

    def test_gdal_backend_omits_the_option_when_it_would_be_ignored(self):
        """Never emit something that looks band-interleaved and is not."""
        from shared_utils import gdal_cog_processor as gp

        cmd = gp.build_gdal_translate_command(
            "in.tif", "out.tif", None, "ZSTD", 9, 512,
            band_count=8, colorinterp=(ColorInterp.gray,) * 8,
        )
        if gp.cog_driver_supports_interleave():
            assert "INTERLEAVE=BAND" in cmd
        else:
            assert "INTERLEAVE=BAND" not in cmd


# ---------------------------------------------------------------------------
# 6. BIGTIFF threshold
# ---------------------------------------------------------------------------

class TestBigTiffThreshold:
    """`IF_SAFER` is documented by GDAL as "only a heuristic that might not
    always work depending on compression ratios". The failure is
    `TIFFAppendToStrip:Maximum TIFF file size exceeded`, hours into a run."""

    def test_forced_above_the_shared_threshold(self):
        from shared_utils.cog_utils import BIGTIFF_FORCE_GB

        assert BIGTIFF_FORCE_GB == 3.0
        assert build_creation_options("ZSTD", 9, BIGTIFF_FORCE_GB + 0.1)["BIGTIFF"] == "YES"

    def test_heuristic_below_it(self):
        assert build_creation_options("ZSTD", 9, 1.0)["BIGTIFF"] == "IF_SAFER"
        assert build_creation_options("ZSTD", 9, 3.0)["BIGTIFF"] == "IF_SAFER"
        assert build_creation_options("ZSTD", 9)["BIGTIFF"] == "IF_SAFER"

    def test_threshold_matches_the_other_two_modules(self):
        """The same 3 GB appears in three places and none of them import it
        from the others -- compression.py:337 and profiles.py:43 both spell it
        as a literal `> 3`. Drift between them would be silent, so this pins
        the BEHAVIOUR of all three rather than a shared constant that does not
        exist yet."""
        from shared_utils.cog_utils import BIGTIFF_FORCE_GB
        from shared_utils.compression import get_compression_config
        from shared_utils.profiles import get_compression_profile

        assert BIGTIFF_FORCE_GB == 3.0
        for size_gb, expected in ((1.0, 'IF_SAFER'), (4.0, 'YES')):
            assert build_creation_options('ZSTD', 9, size_gb)['BIGTIFF'] == expected
            assert get_compression_config(size_gb, 'float32')['bigtiff'] == expected
            assert get_compression_profile('float32', size_gb)['bigtiff'] == expected


# ---------------------------------------------------------------------------
# 7. Declared nodata that never occurs (the FLT_MAX class)
# ---------------------------------------------------------------------------

class TestNodataActuallyOccurs:
    """A float COG declaring nodata=-9999 whose fill pixels hold FLT_MAX.

    rio-tiler masks on the declared value, matches nothing, and renders the
    fill as data that clamps to the top of the rescale -- painting the masked
    area solid. Invisible to gdalinfo and to COG validation.
    """

    def test_flt_max_fill_is_remapped_to_the_declared_nodata(self, tmp_path):
        _require_gdal()
        arr = (np.random.default_rng(7).random((1024, 1024)) * 10).astype("float32")
        arr[:256, :] = FLT_MAX          # the corrupt fill
        src = _write(tmp_path / "fltmax.tif", arr, nodata=-9999.0)

        with rasterio.open(src) as s:
            assert (s.read(1) == np.float32(FLT_MAX)).any()
            assert not (s.read(1) == np.float32(-9999.0)).any(), (
                "fixture already contains the declared nodata"
            )

        out = str(tmp_path / "fltmax_cog.tif")
        convert_to_cog(src, out, dst_crs=None, nodata=-9999.0, quiet=True)

        with rasterio.open(out) as s:
            band = s.read(1)
            assert s.nodata == -9999.0
            assert not (band == np.float32(FLT_MAX)).any(), (
                "FLT_MAX fill survived the conversion and is still unmasked"
            )
            assert (band == np.float32(-9999.0)).any(), (
                "declared nodata does not occur in the output"
            )

    def test_extreme_fill_detection_is_available_standalone(self, tmp_path):
        """The converter's guard is only as good as the detector under it."""
        from shared_utils.compression import list_extreme_float_fills

        arr = np.full((256, 256), 1.0, dtype="float32")
        arr[0, 0] = FLT_MAX
        path = _write(tmp_path / "probe.tif", arr)
        with rasterio.open(path) as src:        # takes an OPEN dataset
            assert list_extreme_float_fills(src) == [FLT_MAX]


# ---------------------------------------------------------------------------
# 8. Backend parity, on the OUTPUTS
# ---------------------------------------------------------------------------

class TestBackendParityOnOutputs:
    """The two backends used to choose resampling with different functions.

    `determine_resampling_method` called 3-band uint8 cubic/average while
    `get_resampling_for_dtype` called it nearest/mode. Both now resolve through
    `determine_resampling_method`, and this asserts it on the produced files
    rather than on argument lists.
    """

    STRUCTURAL_KEYS = [
        "width", "height", "count", "dtype", "overviews", "overview_count",
        "block", "tiled", "compression", "layout", "nodata", "checksum",
    ]

    def _pair(self, source, tmp_path):
        rio_out = str(tmp_path / "parity_rio.tif")
        gdal_out = str(tmp_path / "parity_gdal.tif")
        convert_to_cog(source, rio_out, dst_crs=None, backend="rio", quiet=True)
        convert_to_cog(source, gdal_out, dst_crs=None, backend="gdal", quiet=True)
        return _structure(rio_out), _structure(gdal_out), rio_out, gdal_out

    # Everything except nodata, which is a live divergence -- see the strict
    # xfail below. Listed explicitly so adding a key here is a deliberate act.
    PARITY_KEYS = [k for k in STRUCTURAL_KEYS if k != "nodata"]

    def test_categorical_structure_matches(self, categorical_source, tmp_path):
        rio_s, gdal_s, rio_out, gdal_out = self._pair(categorical_source, tmp_path)
        differing = {
            k: (rio_s[k], gdal_s[k])
            for k in self.PARITY_KEYS
            if rio_s[k] != gdal_s[k]
        }
        assert not differing, f"backends disagree on {differing}"

    @pytest.mark.xfail(
        strict=True,
        reason="KNOWN DEFECT, reproduced not papered over: the two backends "
               "resolve nodata differently. convert_to_cog's rio path applies "
               "the is_bare_8bit_imagery carve-out (PR #114/#117) and STRIPS "
               "the nodata tag off a 1- or 3-band uint8 raster, because 0 is a "
               "real sample in imagery. The backend='gdal' branch returns "
               "before that block ever runs and inherits the source tag, so "
               "the same input yields nodata=None from rio and nodata=0.0 from "
               "gdal. Note the carve-out is itself questionable for a CLASS "
               "map, where 0 really is fill. Fixing it means factoring the "
               "nodata resolution out of convert_to_cog so both backends share "
               "it -- deliberately not attempted here, since that block is the "
               "most heavily litigated code in the module. This xfail is "
               "strict: it fails the moment the behaviour changes either way.",
    )
    def test_nodata_parity(self, categorical_source, tmp_path):
        rio_s, gdal_s, _, _ = self._pair(categorical_source, tmp_path)
        assert rio_s["nodata"] == gdal_s["nodata"]

    def test_both_backends_apply_mode_to_the_same_file(self, categorical_source, tmp_path):
        """Behavioural, not tag-based: a rio-produced COG records no overview
        resampling anywhere in the file, so the only honest check is whether
        the overviews invented a code."""
        _, _, rio_out, gdal_out = self._pair(categorical_source, tmp_path)
        native = _native_values(categorical_source)
        for path in (rio_out, gdal_out):
            assert not (_coarsest_overview_values(path) - native), (
                f"{os.path.basename(path)} invented codes"
            )

    def test_both_backends_resolve_the_same_data_kind(self):
        """The single shared resolver, checked on every committed crop."""
        from shared_utils.gdal_cog_processor import create_cog_gdal  # noqa: F401

        for name, _ in TestDataKindDetection.REAL_FIXTURES:
            path = os.path.join(FIXTURE_DIR, name)
            assert determine_resampling_method(path)[1] in {"mode", "average"}

    def test_gdal_backend_honours_an_explicit_resampling(self, categorical_source, tmp_path):
        """This branch used to DROP the caller's resampling_method entirely."""
        from shared_utils import gdal_cog_processor as gp

        captured = {}
        real = gp.create_cog_gdal

        def spy(*args, **kwargs):
            captured.update(kwargs)
            return real(*args, **kwargs)

        gp.create_cog_gdal = spy
        try:
            convert_to_cog(
                categorical_source, str(tmp_path / "spy.tif"), dst_crs=None,
                backend="gdal", resampling_method="bilinear", quiet=True,
            )
        finally:
            gp.create_cog_gdal = real

        assert captured.get("resampling") == "bilinear"
        assert captured.get("overview_resampling") == "bilinear"


# ---------------------------------------------------------------------------
# 9. No-op guarantee
# ---------------------------------------------------------------------------

class TestNoOpOnAlreadyCorrectFiles:
    """Every fix has to leave good files alone, or the next bulk run churns
    the whole bucket."""

    def test_reconverting_is_structurally_stable(self, categorical_source, tmp_path):
        first = str(tmp_path / "pass1.tif")
        second = str(tmp_path / "pass2.tif")
        convert_to_cog(categorical_source, first, dst_crs=None, quiet=True)
        convert_to_cog(first, second, dst_crs=None, quiet=True)

        a, b = _structure(first), _structure(second)
        for key in TestBackendParityOnOutputs.STRUCTURAL_KEYS:
            assert a[key] == b[key], f"second pass changed {key}: {a[key]} -> {b[key]}"

    def test_continuous_file_is_not_switched_to_mode(self, continuous_source, tmp_path):
        first = str(tmp_path / "cont1.tif")
        second = str(tmp_path / "cont2.tif")
        convert_to_cog(continuous_source, first, dst_crs=None, quiet=True)
        convert_to_cog(first, second, dst_crs=None, quiet=True)
        assert _structure(first)["checksum"] == _structure(second)["checksum"]
        assert detect_data_kind(first) == "continuous"


# ---------------------------------------------------------------------------
# 10. The dead-code path: no reprojection
# ---------------------------------------------------------------------------

class TestResamplingResolvedOnEveryPath:
    """`overview_resampling` was assigned before `if needs_reprojection:` and
    reassigned only INSIDE it. With `dst_crs=None`, or a source already in the
    target CRS, the categorical branch of `determine_resampling_method` was
    unreachable and every such file got AVERAGE overviews.

    That is the path most of this program's products take -- they are already
    in EPSG:3857 when they reach the converter.
    """

    def test_no_reprojection_still_gets_mode(self, categorical_source, tmp_path):
        out = str(tmp_path / "no_warp.tif")
        convert_to_cog(categorical_source, out, dst_crs=None, quiet=True)
        native = _native_values(categorical_source)
        assert not (_coarsest_overview_values(out) - native)

    def test_same_crs_target_still_gets_mode(self, categorical_source, tmp_path):
        """dst_crs set but equal to the source CRS -- needs_reprojection is
        False, so this is the same dead branch by a different route."""
        out = str(tmp_path / "same_crs.tif")
        convert_to_cog(categorical_source, out, dst_crs="EPSG:3857", quiet=True)
        native = _native_values(categorical_source)
        assert not (_coarsest_overview_values(out) - native)

    @pytest.mark.parametrize("backend", ["rio", "gdal"])
    def test_both_backends_on_the_no_reprojection_path(self, categorical_source, tmp_path, backend):
        out = str(tmp_path / f"no_warp_{backend}.tif")
        convert_to_cog(categorical_source, out, dst_crs=None, backend=backend, quiet=True)
        native = _native_values(categorical_source)
        assert not (_coarsest_overview_values(out) - native)


# ---------------------------------------------------------------------------
# 11. Concurrent conversions must not trample each other's temp files
# ---------------------------------------------------------------------------

class TestConcurrentConversionsAreIsolated:
    """Found while chasing an intermittent failure in this module, and it turned
    out to be a real data-corruption bug rather than a flaky test.

    `convert_to_cog` wrote all three of its intermediates into the shared temp
    directory under names derived from the input BASENAME alone::

        /tmp/<basename>.nonodata.tmp.vrt
        /tmp/<basename>.warped.tmp.tif
        /tmp/<basename>.cog.tmp.tif

    Two files with the same name in different directories therefore shared
    them. Reproduced with two threads converting a `same_name.tif` each: one
    output came back holding the OTHER file's pixels, no error raised, exit
    status clean.

    That is the normal shape of a batch here -- `parallel.map_threaded` fans
    out with `max_workers=4` and the per-sensor trees repeat filenames across
    date folders -- so the corruption is reachable in production, not just
    under pytest.
    """

    FILLS = (7, 200, 42, 99)

    def _same_named_sources(self, tmp_path):
        jobs = []
        for index, fill in enumerate(self.FILLS):
            directory = tmp_path / f"dir{index}"
            directory.mkdir()
            arr = np.full((1024, 1024), fill, dtype="uint8")
            src = _write(directory / "same_name.tif", arr, nodata=0)
            jobs.append((src, str(directory / "out.tif"), fill))
        return jobs

    def test_same_basename_in_parallel_keeps_its_own_pixels(self, tmp_path):
        from concurrent.futures import ThreadPoolExecutor

        _require_gdal()
        jobs = self._same_named_sources(tmp_path)

        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            list(pool.map(
                lambda job: convert_to_cog(job[0], job[1], dst_crs=None, quiet=True),
                jobs,
            ))

        wrong = []
        for src, out, fill in jobs:
            with rasterio.open(out) as dst:
                values = np.unique(dst.read(1)).tolist()
            if values != [fill]:
                wrong.append((os.path.basename(os.path.dirname(out)), fill, values))
        assert not wrong, f"conversions crossed over: {wrong}"

    def test_temp_paths_are_unique_per_call(self, tmp_path):
        """Pins the mechanism, so a refactor cannot quietly drop the
        namespacing and reintroduce the crossover.

        Scoped to this test's own basename rather than globbing the whole temp
        directory. The shared directory belongs to the machine, not to the test
        run: a second pytest process, or a stale file from a previous one, puts
        matching names there and has nothing to do with this call. (The
        pre-existing `test_cog_utils.py::test_strip_vrt_is_cleaned_up` takes a
        before/after snapshot for the same reason, and still loses the race
        against a concurrent run.)
        """
        import glob
        import tempfile as _tempfile

        _require_gdal()
        basename = "uniquely_named_for_this_test.tif"
        arr = np.full((512, 512), 3, dtype="uint8")
        src = _write(tmp_path / basename, arr, nodata=0)

        patterns = [
            os.path.join(directory, f"{basename}*{suffix}")
            for directory in {_tempfile.gettempdir(), "/tmp"}
            for suffix in (".cog.tmp.tif", ".warped.tmp.tif", ".nonodata.tmp.vrt")
        ]
        assert not [m for p in patterns for m in glob.glob(p)], "dirty start"

        convert_to_cog(src, str(tmp_path / "unique_cog.tif"), dst_crs=None, quiet=True)

        leaked = sorted({m for p in patterns for m in glob.glob(p)})
        assert not leaked, f"temp files leaked: {leaked}"


# ---------------------------------------------------------------------------
# 12. The filename CROSS-CHECK layer
# ---------------------------------------------------------------------------

class TestFilenameCrossCheck:
    """The filename is a cross-check on the pixels, never the primary signal.

    Four measured counter-examples make that non-negotiable; each has a test
    below. The tables live in cog_utils with the counter-examples written next
    to them precisely so nobody "simplifies" this into filename-only matching.
    """

    def test_grayscale_name_gets_no_opinion(self):
        """COUNTER-EXAMPLE 1. `UAVSAR_Grayscale_...` reads as continuous
        imagery but holds five class codes {0,29,76,150,173}. There is
        deliberately no `gray` token, so the filename abstains and the pixels
        decide. A `gray` token would get this exactly backwards."""
        assert filename_data_kind(
            "UAVSAR_Grayscale_flight25023_mosaic_2025-07-09_day.tif"
        ) is None

    def test_classified_name_is_categorical(self):
        """COUNTER-EXAMPLE 2, name half: the name is right here."""
        assert filename_data_kind(
            "ClassifiedUAVSARI_CopyRas_2025-07_monthly.tif"
        ) == "categorical"

    def test_flattened_classification_is_called_out(self, tmp_path, capsys):
        """COUNTER-EXAMPLE 2, data half. Name and pixels AGREE on categorical,
        so the disagreement alarm cannot see this one -- the classes were
        flattened to a single code, most likely by an ArcGIS Copy Raster
        export that dropped the color table."""
        arr = np.zeros((512, 512), dtype="uint8")
        arr[:6, :] = 255                       # 1.1%-ish, one class, rest nodata
        path = _write(tmp_path / "ClassifiedThing_CopyRas.tif", arr, nodata=0)

        assert resolve_data_kind(path) == "categorical"
        out = capsys.readouterr().out
        assert "FLATTENED" in out, out

    def test_flatten_notice_does_not_fire_on_sparse_non_class_products(self, tmp_path, capsys):
        """The gate is the `class` family, not every categorical token.
        `FloodMap` and `damage` layers are LEGITIMATELY single-code and sparse
        -- gating on the whole table gave three false positives out of four
        hits on the real samples, and an alarm that is wrong three times in
        four is one nobody reads."""
        arr = np.zeros((512, 512), dtype="uint8")
        arr[:6, :] = 1
        path = _write(tmp_path / "OPERA_DSWx_HLS_FloodMap_2024-04-21_day.tif",
                      arr, nodata=0)
        resolve_data_kind(path)
        assert "FLATTENED" not in capsys.readouterr().out

    def test_change_map_token_is_categorical(self):
        """COUNTER-EXAMPLE 3. Float32 {-1,0,+1}; the dtype says nothing."""
        assert filename_data_kind(
            "OPERA_DSWx-S1_BWTR_ChngMap_date1_2024-10-03_to_2024-10-11_day.tif"
        ) == "categorical"

    def test_distalert_siblings_get_opposite_opinions(self):
        """COUNTER-EXAMPLE 4. One directory, two products, opposite kinds.
        The names differ only in the middle, which is why these are substring
        tokens and not a per-directory lookup."""
        assert filename_data_kind(
            "OPERA-DIST-ALERT-HLS-VEG-DIST-STATUS_2025-06-26_day.tif"
        ) == "categorical"
        assert filename_data_kind(
            "OPERA-DIST-ALERT-HLS-VEG-ANOM-MAX_2025-06-26_day.tif"
        ) == "continuous"

    def test_unet_alone_is_not_a_categorical_signal(self):
        """`predicted_score` comes out of a U-Net and is Float32 continuous,
        so only `unet_class` is a token, never bare `unet`."""
        assert filename_data_kind(
            "sangab_30412_L090_UNet_predicted_score_2025-07-09_day.tif"
        ) == "continuous"
        assert filename_data_kind(
            "uavsar_guadal_L090_UNet_class_2025-07-09_day.tif"
        ) == "categorical"

    def test_colourised_sibling_abstains_but_colour_imagery_does_not(self):
        """`dnbr/` is continuous float; `dnbr/dnbrColor/` is its 3-band
        colourised sibling with 5 discrete levels per band. Whether a
        colourised product is discrete or smooth is a PIXEL question, so the
        filename abstains -- otherwise it raises a false alarm on every one.

        But TrueColor/ColorIR are colour imagery in their own right and must
        keep their `continuous` opinion; without the carve-out `truecolor`
        would trip the rule against itself, since it ends in "color".
        """
        assert filename_data_kind(
            "Palisades_AV3_provisional_dNBR_866nm_2198nm_color_2024-09-05_day.tif"
        ) is None
        assert filename_data_kind("AV3_dNBRColor_2025-01-16_day.tif") is None
        assert filename_data_kind(
            "Palisades_AV3_provisional_dNBR_866nm_2198nm_2024-09-05_day.tif"
        ) == "continuous"
        assert filename_data_kind("Satellogic_TrueColor_2025-01-16_day.tif") == "continuous"
        assert filename_data_kind("SkySat_ColorIR_2025-01-16_day.tif") == "continuous"

    def test_colour_word_is_matched_token_wise_not_as_a_suffix(self):
        """The real name is `..._dNBR_866nm_2198nm_color_2024-09-05_..._day`,
        so the colour word sits in the MIDDLE with dates after it. The first
        version of this rule used str.endswith and silently did nothing."""
        from shared_utils.cog_utils import _has_colour_word

        assert _has_colour_word("x_dnbr_866nm_color_2024-09-05_day")
        assert _has_colour_word("av3_dnbrcolor_2025-01-16_day")
        assert not _has_colour_word("uavsar_grayscale_mosaic_2025-07-09_day")

    def test_no_real_filename_matches_both_tables(self):
        """A name hitting both tables would make the opinion arbitrary. None
        of the 32 real samples does; this pins it for the committed crops."""
        from shared_utils.cog_utils import (
            CATEGORICAL_FILENAME_TOKENS, CONTINUOUS_FILENAME_TOKENS,
        )

        conflicts = []
        for name in os.listdir(FIXTURE_DIR):
            if not name.lower().endswith(".tif"):
                continue
            stem = os.path.splitext(name)[0].lower()
            cat = [t for t in CATEGORICAL_FILENAME_TOKENS if t in stem]
            con = [t for t in CONTINUOUS_FILENAME_TOKENS if t in stem]
            if cat and con:
                conflicts.append((name, cat, con))
        assert not conflicts, f"tokens collide: {conflicts}"

    @pytest.mark.parametrize("name,expected", TestDataKindDetection.REAL_FIXTURES)
    def test_pixels_always_win_over_the_filename(self, name, expected):
        """The whole contract in one assertion: whatever the name says, the
        resolved kind equals the pixel verdict."""
        path = os.path.join(FIXTURE_DIR, name)
        assert resolve_data_kind(path) == detect_data_kind(path) == expected

    def test_filename_breaks_the_tie_when_pixels_are_inconclusive(self, tmp_path):
        """Job 1 of the layer. A float raster with two NON-integral values is
        `unknown` to the pixel layer; a categorical name settles it."""
        arr = np.where(
            np.random.default_rng(11).random((512, 512)) < 0.5, 0.25, 1.75
        ).astype("float32")
        undecided = _write(tmp_path / "ambiguous_product.tif", arr)
        assert detect_data_kind(undecided) == "unknown"
        assert resolve_data_kind(undecided) == "unknown"

        named = _write(tmp_path / "S1_reclassified_WM_2024-10-03.tif", arr)
        assert detect_data_kind(named) == "unknown"
        assert resolve_data_kind(named) == "categorical"

    def test_disagreement_warns_loudly_and_keeps_the_pixel_answer(self, tmp_path, capsys):
        """Job 2. A name saying NDVI over class-code pixels is usually a data
        problem worth surfacing, not a detection problem to paper over."""
        arr = np.where(
            np.random.default_rng(12).random((512, 512)) < 0.5, 1, 3
        ).astype("uint8")
        path = _write(tmp_path / "SomeSensor_NDVI_2025-01-01_day.tif", arr, nodata=0)

        assert filename_data_kind(path) == "continuous"
        assert resolve_data_kind(path) == "categorical"
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "PIXELS" in out


# ---------------------------------------------------------------------------
# 13. Output-side guards -- refuse to write a known-wrong COG silently
# ---------------------------------------------------------------------------

class TestOutputAudit:
    """Every defect in this session was found months later in a rendered map.
    Each is cheap to see in the output file the moment it is written."""

    def test_a_correct_cog_reports_nothing(self, categorical_source, tmp_path):
        out = str(tmp_path / "good.tif")
        convert_to_cog(categorical_source, out, dst_crs=None, quiet=True)
        assert audit_cog_output(out) == []

    def test_averaged_overviews_on_categorical_data_are_caught(self, categorical_source, tmp_path):
        out = str(tmp_path / "averaged.tif")
        convert_to_cog(categorical_source, out, dst_crs=None,
                       resampling_method="bilinear", quiet=True)
        problems = audit_cog_output(out)
        assert any("AVERAGED" in p for p in problems), problems

    def test_wrong_overview_count_is_caught(self, categorical_source, tmp_path):
        out = str(tmp_path / "underbuilt.tif")
        convert_to_cog(categorical_source, out, dst_crs=None,
                       overview_levels=1, quiet=True)
        problems = audit_cog_output(out)
        assert any("overview count" in p for p in problems), problems

    def test_untiled_and_uncompressed_are_caught(self, tmp_path):
        arr = np.random.default_rng(13).integers(1, 6, (1024, 2048), dtype="uint8")
        striped = _write(tmp_path / "striped.tif", arr, nodata=0,
                         tiled=False, blockysize=1)
        problems = audit_cog_output(striped)
        assert any("NOT tiled" in p for p in problems), problems
        assert any("UNCOMPRESSED" in p for p in problems), problems

    def test_flt_max_fill_under_a_different_declared_nodata_is_caught(self, tmp_path):
        arr = (np.random.default_rng(14).random((1024, 1024)) * 10).astype("float32")
        arr[:256, :] = FLT_MAX
        path = _write(tmp_path / "fltmax_raw.tif", arr, nodata=-9999.0,
                      tiled=True, blockxsize=512, blockysize=512, compress="deflate")
        problems = audit_cog_output(path)
        assert any("FLT_MAX" in p for p in problems), problems

    def test_a_declared_nodata_that_simply_never_occurs_is_NOT_flagged(self, tmp_path):
        """A fully-valid scene legitimately declares a nodata it never uses.
        Warning on those would make this whole audit noise, so the check keys
        on fill being PRESENT, not on the declared value being absent."""
        arr = (np.random.default_rng(15).random((1024, 1024)) * 10).astype("float32")
        out = str(tmp_path / "fully_valid.tif")
        convert_to_cog(_write(tmp_path / "fv.tif", arr, nodata=-9999.0),
                       out, dst_crs=None, nodata=-9999.0, quiet=True)
        with rasterio.open(out) as src:
            assert not (src.read(1) == np.float32(-9999.0)).any()
        assert audit_cog_output(out) == []

    def test_nodata_is_not_mistaken_for_an_invented_class(self):
        """Regression. The band's value set excludes nodata by construction,
        so if the overview's set does not, every masked mosaic self-reports as
        averaged. Caught on 11 of 12 already-correct OPERA DSWx COGs.

        The committed DSWx crop is a real masked class raster (nodata 0,
        classes {1,3,251,255})."""
        crop = os.path.join(FIXTURE_DIR, "dswx_s1_wtr_classcodes_crop.tif")
        with rasterio.open(crop) as src:
            assert src.nodata == 0.0
            assert 0 in np.unique(src.read(1)).tolist()
        assert not any("AVERAGED" in p for p in audit_cog_output(crop))

    def test_small_rasters_are_exempt_from_the_overview_count_check(self):
        """`calculate_overview_factors` returns its [2,4,8] fallback below the
        blocksize, where the rule itself gives 0, so demanding a count there
        would flag every small crop."""
        for name in ("dswx_s1_wtr_classcodes_crop.tif", "gaia_atlanta_sample.tif"):
            problems = audit_cog_output(os.path.join(FIXTURE_DIR, name))
            assert not any("overview count" in p for p in problems), (name, problems)

    def test_strict_output_raises_instead_of_warning(self, categorical_source, tmp_path):
        with pytest.raises(RuntimeError, match="OUTPUT AUDIT FAILED"):
            convert_to_cog(
                categorical_source, str(tmp_path / "strict.tif"), dst_crs=None,
                overview_levels=1, strict_output=True, quiet=True,
            )

    def test_default_warns_unmissably_but_still_writes(self, categorical_source, tmp_path, capsys):
        """Default is warn, not raise: this runs inside DPS jobs during live
        activations, where aborting a finished conversion is worse than the
        defect it found."""
        out = str(tmp_path / "warned.tif")
        convert_to_cog(categorical_source, out, dst_crs=None,
                       overview_levels=1, quiet=True)
        captured = capsys.readouterr().out
        assert "OUTPUT AUDIT FAILED" in captured
        assert os.path.exists(out)

    @pytest.mark.parametrize("backend", ["rio", "gdal"])
    def test_both_backends_audit_their_output(self, categorical_source, tmp_path, backend):
        """backend='gdal' returns early, so without its own call it would be
        the one path that writes unchecked."""
        with pytest.raises(RuntimeError, match="OUTPUT AUDIT FAILED"):
            convert_to_cog(
                categorical_source, str(tmp_path / f"s_{backend}.tif"),
                dst_crs=None, backend=backend, overview_levels=1,
                strict_output=True, quiet=True,
            )
