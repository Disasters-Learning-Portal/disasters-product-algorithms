"""Assertions for dps/blackmarble/naming.sh (the Black Marble output-name convention).

The helpers are bash, so each case shells out to `source naming.sh && bm_stem ...` --
the same "test the real thing, not a Python re-implementation" approach
tests/integration/test_dps_validate.py takes with dps/_validate.sh. A Python copy of the
formatting would pass happily while the shipped awk drifted.

Why this is worth pinning: the name is assembled from four signed floats whose order is
NOT the bbox's order (bbox is min_lon,min_lat,max_lon,max_lat; the name is NW then SE),
with a hemisphere letter derived from each sign. Every one of those is a silent-wrong
-answer bug if it flips -- the job still succeeds and still uploads, just under a name
that sorts and reads incorrectly.
"""
import os
import shutil
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NAMING_SH = os.path.join(REPO_ROOT, "dps", "blackmarble", "naming.sh")

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)


def bm_stem(bbox, date, product="hdnightlights"):
    """Call the real bash helper and return the stem it prints."""
    result = subprocess.run(
        ["bash", "-c", f'source "{NAMING_SH}"; bm_stem "$1" "$2" "$3"',
         "_", bbox, date, product],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"bm_stem failed: {result.stderr}"
    return result.stdout.strip()


def test_matches_the_reference_filename():
    """The canonical example the convention was specified from."""
    assert bm_stem("-90.96,32.83,-90.79,32.97", "2023-03-25") == (
        "hdnightlights_32_97N90_96W32_83N90_79W_2023-03-25_day"
    )


def test_corner_order_is_northwest_then_southeast():
    """Name order is NW (max_lat,min_lon) then SE (min_lat,max_lon) -- NOT bbox order.

    Latitudes here are far enough apart that a swapped pair is unambiguous.
    """
    stem = bm_stem("-10.00,20.00,-5.00,40.00", "2023-01-02")
    assert stem == "hdnightlights_40_00N10_00W20_00N5_00W_2023-01-02_day"


@pytest.mark.parametrize(
    "bbox,expected_corners",
    [
        # N/W (northern + western): the common US case
        ("-122.55,37.69,-122.32,37.81", "37_81N122_55W37_69N122_32W"),
        # S/E (southern + eastern): both hemisphere letters flip
        ("18.40,-34.00,18.65,-33.85", "33_85S18_40E34_00S18_65E"),
        # N/E
        ("139.60,35.60,139.85,35.75", "35_75N139_60E35_60N139_85E"),
        # Straddling the equator and the prime meridian: signs differ within one bbox
        ("-0.10,-0.20,0.15,0.25", "0_25N0_10W0_20S0_15E"),
    ],
)
def test_hemisphere_letters_follow_each_coordinate_sign(bbox, expected_corners):
    assert bm_stem(bbox, "2023-06-15") == (
        f"hdnightlights_{expected_corners}_2023-06-15_day"
    )


def test_coordinates_are_two_decimal_places_with_underscore_for_point():
    """Extra precision is truncated to 2 dp and the decimal point becomes '_'.

    Unbounded precision would put an arbitrary-length float in the filename.
    """
    stem = bm_stem("-90.96789,32.83123,-90.79456,32.97999", "2023-03-25")
    assert stem == "hdnightlights_32_98N90_97W32_83N90_79W_2023-03-25_day"
    assert "." not in stem.rsplit("_day", 1)[0].replace("2023-03-25", "")


def test_date_token_stays_last():
    """`_YYYY-MM-DD_day` must remain the final tokens (cog_utils._relocate_datetime)."""
    stem = bm_stem("-122.55,37.69,-122.32,37.81", "2023-06-15")
    assert stem.endswith("_2023-06-15_day")


def test_colored_product_gets_its_own_token_not_a_suffix():
    """The colored companion is a different product token, so the date stays last.

    Upstream would have produced `<stem>-colored.tif` (date no longer final); run.sh
    renames onto this stem instead.
    """
    args = ("-122.55,37.69,-122.32,37.81", "2023-06-15")
    plain = bm_stem(*args, product="hdnightlights")
    colored = bm_stem(*args, product="hdnightlightscolored")
    assert colored == plain.replace("hdnightlights_", "hdnightlightscolored_", 1)
    assert colored.endswith("_2023-06-15_day")


def test_activation_event_never_appears_in_the_stem():
    """The event belongs in the S3 prefix + COG tags, never the filename.

    bake_event_metadata.ipynb actively STRIPS event prefixes out of names, so emitting
    one here would fight the backfill tooling.
    """
    stem = bm_stem("-122.55,37.69,-122.32,37.81", "2023-06-15")
    assert "KyleWx" not in stem and "202601" not in stem
