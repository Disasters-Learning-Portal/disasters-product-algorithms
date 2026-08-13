"""Assertions for dps/blackmarble/platform.sh (the VIIRS platform table).

One Black Marble engine serves two registered algorithms -- Suomi-NPP (VNP46A2) and
NOAA-20 (VJ146A2) -- and EVERYTHING that differs between them lives in this bash table:
the product/folder token, the product short name, the mission-start date floor and the
SOURCE string baked into the COG.

Every one of those is a silent-wrong-answer bug if it drifts. A wrong product token makes
the NOAA-20 job overwrite the Suomi-NPP product for the same event and date; a wrong date
floor lets a job through that Earthdata answers with zero granules; a wrong SOURCE
mislabels the provenance of a published product. In all three cases the job still exits 0.

The helpers are bash, so each case shells out to `source platform.sh && <fn> <platform>`,
the same "test the real thing, not a Python re-implementation" approach
tests/unit/test_blackmarble_naming.py and tests/integration/test_dps_validate.py take.
_validate.sh is sourced alongside it because the unknown-platform branch calls `die`,
exactly as run.sh has both sourced.
"""
import os
import shutil
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLATFORM_SH = os.path.join(REPO_ROOT, "dps", "blackmarble", "platform.sh")
VALIDATE_SH = os.path.join(REPO_ROOT, "dps", "_validate.sh")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")

PLATFORMS = ("snpp", "noaa20")


def call(fn, *args, expect_ok=True):
    """Run one platform.sh helper in a subshell and return its stdout."""
    result = subprocess.run(
        ["bash", "-c",
         f'source "{VALIDATE_SH}"; source "{PLATFORM_SH}"; "$@"', "_", fn, *args],
        capture_output=True, text=True,
    )
    if expect_ok:
        assert result.returncode == 0, f"{fn} {args} failed: {result.stderr}"
    return result


def value(fn, *args):
    return call(fn, *args).stdout.strip()


def test_exactly_two_platforms_are_supported():
    """bm_platforms feeds run.sh's validate_in_set, so it is the authoritative list."""
    assert value("bm_platforms").split() == list(PLATFORMS)


@pytest.mark.parametrize("platform,expected", [
    ("snpp", "hdnightlights"),
    ("noaa20", "hdnightlightsnoaa20"),
])
def test_product_token_per_platform(platform, expected):
    assert value("bm_platform_product", platform) == expected


def test_product_tokens_are_distinct_so_outputs_cannot_collide():
    """_finalize.sh keys S3 objects by <YYYYMMDD>/<product>/<file>.

    If both platforms used the same token, running both algorithms for one activation
    event and date would publish one product over the other -- silently, under a green job.
    """
    tokens = {value("bm_platform_product", p) for p in PLATFORMS}
    assert len(tokens) == len(PLATFORMS)


def test_colored_companion_token_derives_from_the_product_token():
    """run.sh builds PRODUCT_COLORED as "${PRODUCT}colored"; pin both halves.

    The Suomi-NPP value is the one already published to nasa-disasters-staging, so it must
    not change when the table was introduced.
    """
    assert value("bm_platform_product", "snpp") + "colored" == "hdnightlightscolored"
    assert (value("bm_platform_product", "noaa20") + "colored"
            == "hdnightlightsnoaa20colored")


@pytest.mark.parametrize("platform,expected", [
    ("snpp", "VNP46A2"),
    ("noaa20", "VJ146A2"),
])
def test_short_name_per_platform(platform, expected):
    """These are the CMR collection short names, verified live against
    cmr.earthdata.nasa.gov (VNP46A2 -> C3365931269-LAADS, VJ146A2 -> C3370789118-LAADS)."""
    assert value("bm_platform_short_name", platform) == expected


@pytest.mark.parametrize("platform,expected", [
    # CMR TemporalExtents.BeginningDateTime for each collection.
    ("snpp", "2012-01-19"),
    ("noaa20", "2018-01-19"),
])
def test_min_date_is_the_mission_start(platform, expected):
    assert value("bm_platform_min_date", platform) == expected


def test_noaa20_starts_later_than_snpp():
    """NOAA-20 launched six years after Suomi-NPP, so its floor must be the later one.

    Swapping the two would let a 2015 NOAA-20 job through to a zero-granule search.
    """
    assert value("bm_platform_min_date", "noaa20") > value("bm_platform_min_date", "snpp")


@pytest.mark.parametrize("platform,product", [
    ("snpp", "VNP46A2"),
    ("noaa20", "VJ146A2"),
])
def test_source_string_names_the_product_and_the_platform_token(platform, product):
    """SOURCE carries the PLATFORM TOKEN, not the satellite's prose name.

    `noaa20` is the same string as BM_PLATFORM, as the `hdnightlightsnoaa20` product
    folder, and as the S3 prefix -- so one grep finds all four. The satellite's proper
    name (NOAA-20 / Suomi-NPP) rides along in the separate VIIRS_PLATFORM tag that
    bake_event.py writes.
    """
    source = value("bm_platform_source", platform)
    assert product in source
    assert platform in source
    assert "Black Marble" in source
    # The other platform's product code / token must NOT appear -- that is the mislabel.
    other_product = "VJ146A2" if product == "VNP46A2" else "VNP46A2"
    other_token = "noaa20" if platform == "snpp" else "snpp"
    assert other_product not in source
    assert other_token not in source


def test_noaa20_source_is_greppable_by_the_platform_token():
    """The literal an operator searches S3/metadata for."""
    assert "noaa20" in value("bm_platform_source", "noaa20")
    assert "noaa20" in value("bm_platform_product", "noaa20")


def test_source_strings_agree_with_bake_event_py():
    """platform.sh is what run.sh prints; bake_event.py's PLATFORMS is what reaches the COG.

    They are two tables describing one fact, so they have to be checked against each other
    -- otherwise the log and the embedded tag can disagree about which satellite ran.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "bm_bake_event", os.path.join(REPO_ROOT, "dps", "blackmarble", "bake_event.py")
    )
    bake_event = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bake_event)

    assert set(bake_event.PLATFORMS) == set(PLATFORMS)
    for platform in PLATFORMS:
        assert bake_event.PLATFORMS[platform]["SOURCE"] == value(
            "bm_platform_source", platform
        )
        assert bake_event.PLATFORMS[platform]["VIIRS_PRODUCT"] == value(
            "bm_platform_short_name", platform
        )


@pytest.mark.parametrize("fn", [
    "bm_platform_product",
    "bm_platform_short_name",
    "bm_platform_min_date",
    "bm_platform_source",
])
def test_unknown_platform_dies_rather_than_defaulting(fn):
    """A typo must abort, never fall through to a default and process the wrong satellite."""
    result = call(fn, "noaa-20", expect_ok=False)
    assert result.returncode != 0
    assert "unknown Black Marble platform" in result.stderr


def test_sunset_date_is_the_announced_suomi_npp_cutoff():
    """2026-11-01 is the date in the NASA Earthdata alert behind disasters-portal#365."""
    out = subprocess.run(
        ["bash", "-c",
         f'source "{VALIDATE_SH}"; source "{PLATFORM_SH}"; printf %s "$BM_SNPP_SUNSET_DATE"'],
        capture_output=True, text=True,
    )
    assert out.returncode == 0
    assert out.stdout.strip() == "2026-11-01"
