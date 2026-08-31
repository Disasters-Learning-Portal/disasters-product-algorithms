"""
Output-name convention for the calibrated SAR products (Umbra / Capella / ICEYE).

The shape is

    <platform>_<product>_filtered<N>_<YYYY-MM-DDTHH:MM:SSZ>.tif

and every part of it is load-bearing -- see the docstring on
``create_sar_output_filename``. Before this was centralized, each sensor
hand-rolled the f-string and got a different part wrong:

    Umbra    202608_Umbra-07_sigma02026-08-05T03:54:47Z_filtered5.tif
    Capella  202604_Capella-18_sigma02026-04-18T19:33:05Z_filtered3.tif
    ICEYE    202605_ICEYE-X48_sigma0_2026-05-13T15:48:16Z_filtered5.tif

i.e. no separator at all between the product token and the date (Umbra,
Capella); in all three the ``_filtered<N>`` token stranded AFTER the timestamp,
which takes the stem out of the repo's canonical "ends in an ISO-Zulu stamp"
form; and all three led with a redundant acquisition ``YYYYMM`` that reads as
half an activation-event prefix.

These tests exercise the calib functions' naming through the module-level
helpers rather than through the full calibration, which needs vendor S3.
"""

import re
from datetime import datetime

import pytest

from shared_utils.file_naming import (
    _STAMPED_END_RE,
    create_output_filename,
    create_sar_output_filename,
    extract_datetime_from_filename,
)


ISO_ZULU = r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z'


class TestCreateSarOutputFilename:

    def test_umbra_sigma0_shape(self):
        assert create_sar_output_filename(
            "Umbra-07", "sigma0", datetime(2026, 8, 5, 3, 54, 47), 5
        ) == "Umbra-07_sigma0_filtered5_2026-08-05T03:54:47Z.tif"

    def test_capella_sigma0_shape(self):
        assert create_sar_output_filename(
            "Capella-18", "sigma0", datetime(2026, 4, 18, 19, 33, 5), 3
        ) == "Capella-18_sigma0_filtered3_2026-04-18T19:33:05Z.tif"

    def test_iceye_sigma0_shape(self):
        assert create_sar_output_filename(
            "ICEYE-X48", "sigma0", datetime(2026, 5, 13, 15, 48, 16), 5
        ) == "ICEYE-X48_sigma0_filtered5_2026-05-13T15:48:16Z.tif"

    @pytest.mark.parametrize("product", ["sigma0", "beta0", "gamma0"])
    def test_product_token_is_separated_from_the_date(self, product):
        """The reported regression: `sigma0` welded onto `2026-08-05...`."""
        name = create_sar_output_filename(
            "Umbra-07", product, datetime(2026, 8, 5, 3, 54, 47), 5
        )
        assert f"_{product}_" in name
        assert f"{product}2026" not in name

    def test_filter_token_precedes_the_date(self):
        name = create_sar_output_filename(
            "Umbra-07", "sigma0", datetime(2026, 8, 5, 3, 54, 47), 5
        )
        assert name.index("_filtered5_") < name.index("2026-08-05T03:54:47Z")

    def test_no_acquisition_yyyymm_head(self):
        """The activation lives in the tags + the S3 prefix, not the name.

        The old `202608_` head was the acquisition month, but under an
        `s3://.../202608_KyleWx_AL/` prefix it read as a mangled event prefix.
        """
        name = create_sar_output_filename(
            "Umbra-07", "sigma0", datetime(2026, 8, 5, 3, 54, 47), 5
        )
        assert name.startswith("Umbra-07_")
        assert not re.match(r'^\d{6}_', name)

    def test_no_filter_size_omits_the_token(self):
        assert create_sar_output_filename(
            "Umbra-07", "sigma0", datetime(2026, 8, 5, 3, 54, 47)
        ) == "Umbra-07_sigma0_2026-08-05T03:54:47Z.tif"

    def test_stem_ends_in_iso_zulu(self):
        """The whole point of moving the filter token: the stem is canonical."""
        stem = create_sar_output_filename(
            "Umbra-07", "sigma0", datetime(2026, 8, 5, 3, 54, 47), 5
        )[:-len(".tif")]
        assert re.search(ISO_ZULU + '$', stem)
        assert _STAMPED_END_RE.search(stem)

    def test_canonical_stem_is_a_fixed_point_downstream(self):
        """create_output_filename must not relocate the stamp on a re-run.

        With the old trailing `_filtered5` the stem was NOT canonical, so a
        downstream rename tore the datetime out of the middle and appended it
        again with a bogus granularity suffix -- silently producing a second,
        differently-named copy of the same product.
        """
        name = create_sar_output_filename(
            "Umbra-07", "sigma0", datetime(2026, 8, 5, 3, 54, 47), 5
        )
        renamed = create_output_filename(name, "202608_KyleWx_AL")
        assert renamed == f"202608_KyleWx_AL_{name}"
        # ...and applying it again changes nothing.
        assert create_output_filename(renamed, "202608_KyleWx_AL") == renamed

    def test_old_shape_was_not_a_fixed_point(self):
        """Pins WHY the ordering matters, so the token can't drift back."""
        old = "202608_Umbra-07_sigma02026-08-05T03:54:47Z_filtered5.tif"
        assert not _STAMPED_END_RE.search(old[:-len(".tif")])
        assert create_output_filename(old, "202608_KyleWx_AL") == (
            "202608_KyleWx_AL_202608_Umbra-07_sigma0_filtered5"
            "_2026-08-05T03:54:47Z_hour.tif"
        )

    def test_datetime_is_recoverable_from_the_name(self):
        name = create_sar_output_filename(
            "Umbra-07", "sigma0", datetime(2026, 8, 5, 3, 54, 47), 5
        )
        matched, granularity = extract_datetime_from_filename(name)
        assert (matched, granularity) == ("2026-08-05T03:54:47Z", "hour")

    def test_underscore_split_recovers_every_field(self):
        name = create_sar_output_filename(
            "Umbra-07", "sigma0", datetime(2026, 8, 5, 3, 54, 47), 5
        )
        assert name[:-len(".tif")].split("_") == [
            "Umbra-07", "sigma0", "filtered5", "2026-08-05T03:54:47Z",
        ]


class TestSensorCallSites:
    """The per-sensor wrappers that feed create_sar_output_filename."""

    def test_umbra_output_name_from_a_vendor_gec_basename(self):
        from umbra.umbra_v2 import _umbra_output_name

        assert _umbra_output_name(
            "/tmp/s3_temp/2026-08-05-03-54-47_UMBRA-07_GEC.tif", "sigma0", 5
        ) == "Umbra-07_sigma0_filtered5_2026-08-05T03:54:47Z.tif"

    @pytest.mark.parametrize("product", ["sigma0", "beta0", "gamma0"])
    def test_umbra_all_three_calib_products(self, product):
        from umbra.umbra_v2 import _umbra_output_name

        assert _umbra_output_name(
            "/tmp/s3_temp/2026-08-05-03-54-47_UMBRA-07_GEC.tif", product, 3
        ) == f"Umbra-07_{product}_filtered3_2026-08-05T03:54:47Z.tif"

    def test_iceye_amp_db_variants_keep_the_product_slot(self):
        """iceye derives its two products by substituting the product token."""
        base = create_sar_output_filename(
            "ICEYE-X48", "sigma0", datetime(2026, 5, 13, 15, 48, 16), 5
        )
        assert base.replace("sigma0", "sigma0-amp") == (
            "ICEYE-X48_sigma0-amp_filtered5_2026-05-13T15:48:16Z.tif"
        )
        assert base.replace("sigma0", "sigma0-dB") == (
            "ICEYE-X48_sigma0-dB_filtered5_2026-05-13T15:48:16Z.tif"
        )
