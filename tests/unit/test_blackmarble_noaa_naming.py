"""The Black Marble output-name convention applied to the NOAA-20 product tokens.

tests/unit/test_blackmarble_naming.py pins the coordinate/date grammar of
dps/blackmarble/naming.sh. This file pins what the NOAA-20 algorithm does with it: the
product token is the ONLY thing that changes, and the resulting names must not collide with
the Suomi-NPP job's.

Collision is the failure worth guarding. Both algorithms write to
s3://nasa-disasters-staging/dps_output/<event>/<YYYYMMDD>/<product>/ (_finalize.sh keys each
object by its OUT_HOME-relative path), so if the two produced the same product token and
stem, running both for one activation would publish one product over the other -- with no
error, under a green job, and with a SOURCE tag that then contradicts the pixels.
"""
import os
import shutil
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NAMING_SH = os.path.join(REPO_ROOT, "dps", "blackmarble", "naming.sh")
PLATFORM_SH = os.path.join(REPO_ROOT, "dps", "blackmarble", "platform.sh")
VALIDATE_SH = os.path.join(REPO_ROOT, "dps", "_validate.sh")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")

SF_BBOX = "-122.55,37.69,-122.32,37.81"
SF_CORNERS = "37_81N122_55W37_69N122_32W"


def stem_for(platform, bbox=SF_BBOX, date="2023-06-15", colored=False):
    """Build a stem exactly the way run.sh does: product token from the platform table,
    colored companion as "${PRODUCT}colored", then bm_stem."""
    script = f'''
      source "{VALIDATE_SH}"
      source "{PLATFORM_SH}"
      source "{NAMING_SH}"
      product="$(bm_platform_product "$1")"
      [[ "$4" == "colored" ]] && product="${{product}}colored"
      bm_stem "$2" "$3" "${{product}}"
    '''
    result = subprocess.run(
        ["bash", "-c", script, "_", platform, bbox, date,
         "colored" if colored else "plain"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"stem_for failed: {result.stderr}"
    return result.stdout.strip()


def test_noaa20_reference_filename():
    assert stem_for("noaa20") == f"hdnightlightsnoaa20_{SF_CORNERS}_2023-06-15_day"


def test_noaa20_colored_companion():
    """Upstream derives the second raster as `<output_path>-colored.tif`, which would push
    the date out of final position; run.sh renames it onto this stem instead."""
    assert (stem_for("noaa20", colored=True)
            == f"hdnightlightsnoaa20colored_{SF_CORNERS}_2023-06-15_day")


def test_snpp_names_are_unchanged_by_the_platform_table():
    """Suomi-NPP products are already published under these names -- introducing the
    platform table must not have moved them."""
    assert stem_for("snpp") == f"hdnightlights_{SF_CORNERS}_2023-06-15_day"
    assert stem_for("snpp", colored=True) == f"hdnightlightscolored_{SF_CORNERS}_2023-06-15_day"


@pytest.mark.parametrize("colored", [False, True])
def test_the_two_platforms_never_produce_the_same_stem(colored):
    assert stem_for("snpp", colored=colored) != stem_for("noaa20", colored=colored)


def test_noaa20_plain_stem_is_not_a_prefix_collision_with_snpp_colored():
    """`hdnightlights` is a strict prefix of `hdnightlightsnoaa20`, so a glob or a
    startswith() check written against the Suomi-NPP token would sweep up NOAA-20 products.
    The full stems must still be unambiguous under equality."""
    names = {
        stem_for("snpp"), stem_for("snpp", colored=True),
        stem_for("noaa20"), stem_for("noaa20", colored=True),
    }
    assert len(names) == 4


def test_date_token_stays_last_for_noaa20():
    """`_YYYY-MM-DD_day` is the repo-wide time-less-individual token
    (cog_utils._relocate_datetime); the platform must not push it out of final position."""
    for colored in (False, True):
        assert stem_for("noaa20", colored=colored).endswith("_2023-06-15_day")


def test_activation_event_never_appears_in_the_noaa20_stem():
    """The event lives in the S3 prefix and the COG tags. bake_event_metadata.ipynb
    actively STRIPS event prefixes out of names it finds, so emitting one here would fight
    the backfill tooling."""
    stem = stem_for("noaa20")
    assert "KyleWx" not in stem and "202601" not in stem


@pytest.mark.parametrize("bbox,corners", [
    ("-90.96,32.83,-90.79,32.97", "32_97N90_96W32_83N90_79W"),
    ("18.40,-34.00,18.65,-33.85", "33_85S18_40E34_00S18_65E"),
    ("-0.10,-0.20,0.15,0.25", "0_25N0_10W0_20S0_15E"),
])
def test_coordinate_grammar_is_shared_with_the_snpp_job(bbox, corners):
    """The platform changes only the product token -- the corner grammar is one convention."""
    assert stem_for("noaa20", bbox=bbox) == f"hdnightlightsnoaa20_{corners}_2023-06-15_day"
    assert stem_for("snpp", bbox=bbox) == f"hdnightlights_{corners}_2023-06-15_day"


def test_the_product_folder_matches_the_filename_prefix():
    """run.sh writes into <YYYYMMDD>/${PRODUCT}/ and names files ${PRODUCT}_... -- the two
    are derived from the same token, and _finalize.sh publishes that whole relative path."""
    script = f'''
      source "{VALIDATE_SH}"; source "{PLATFORM_SH}"; printf %s "$(bm_platform_product noaa20)"
    '''
    product = subprocess.run(["bash", "-c", script], capture_output=True,
                             text=True).stdout.strip()
    assert stem_for("noaa20").startswith(product + "_")
