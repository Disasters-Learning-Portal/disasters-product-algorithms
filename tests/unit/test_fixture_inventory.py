"""Assertions for the real-data crops in tests/fixtures/.

WHAT THIS PINS
--------------
Every fixture here exists because of ONE property a synthetic raster would not have:
a rotated geotransform, a missing nodata tag, a nodata that is not 0, activation tags
already baked in, or a genuine non-COG layout. That property is invisible in the
filename, so a regenerated or accidentally-overwritten fixture would keep its name, keep
passing whatever test consumes it, and quietly stop testing what it was added for.

So this module asserts the profile of each fixture directly. It is the reason the crops
can be trusted as "real data" rather than "a .tif someone committed once".

It also pins the two things the fixtures collectively make testable and which are easy to
get wrong:

  1. the 3-band uint8 products are exactly the `is_bare_8bit_imagery` conflict case --
     the helper says "8-bit imagery declares no nodata" while the source file's own tag
     says `0`, and `convert_to_cog(nodata=None)` has to resolve that;
  2. a 256x256 crop CANNOT stand in for a non-COG input, because rio-cogeo only requires
     tiling above 512 px in both dimensions. See tests/fixtures/README.md.
"""
import os

import pytest

rasterio = pytest.importorskip("rasterio")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE_DIR = os.path.join(REPO_ROOT, "tests", "fixtures")

MAX_FIXTURE_BYTES = 500_000

# name -> (width, height, bands, dtype, epsg, nodata, rotated, is_valid_cog)
EXPECTED = {
    "umbra_guam_sar_db_crop.tif": (256, 256, 1, "float32", 4326, None, True, True),
    "iceye_guam_sar_db_crop.tif": (256, 256, 1, "float32", 4326, None, False, True),
    "satellogic_truecolor_nodata0_crop.tif": (256, 256, 3, "uint8", 32655, 0.0, False, True),
    "skysat_colorir_nodata0_crop.tif": (256, 256, 3, "uint8", 32655, 0.0, False, True),
    "satellogic_colorir_tagged_cog_crop.tif": (256, 256, 3, "uint8", 32655, 0.0, False, True),
    "blackmarble_brdf_tagged_cog_crop.tif": (256, 256, 1, "float32", 3857, -9999.0, False, True),
    "mwir_rotated_geotransform_crop.tif": (256, 256, 3, "uint8", 4326, 0.0, True, True),
    "cloudmask_byte_nodata255.tif": (327, 543, 1, "uint8", 4326, 255.0, False, True),
    "satellogic_truecolor_striped_noncog_600.tif": (600, 600, 3, "uint8", 32655, 0.0, False, False),
}

# Fixtures that already carry a baked activation event -- the idempotent-skip inputs.
TAGGED = {
    "satellogic_colorir_tagged_cog_crop.tif",
    "blackmarble_brdf_tagged_cog_crop.tif",
}

# The 3-band uint8 products whose source tag (0) contradicts is_bare_8bit_imagery.
BARE_8BIT_CONFLICT = {
    "satellogic_truecolor_nodata0_crop.tif",
    "skysat_colorir_nodata0_crop.tif",
    "satellogic_colorir_tagged_cog_crop.tif",
    "mwir_rotated_geotransform_crop.tif",
    "satellogic_truecolor_striped_noncog_600.tif",
}


def _path(name):
    return os.path.join(FIXTURE_DIR, name)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_fixture_profile_is_unchanged(name):
    """A fixture that lost its distinguishing property is worse than a missing one."""
    path = _path(name)
    assert os.path.exists(path), f"fixture {name} is missing"

    width, height, bands, dtype, epsg, nodata, rotated, _ = EXPECTED[name]
    with rasterio.open(path) as src:
        assert (src.width, src.height) == (width, height)
        assert src.count == bands
        assert src.dtypes[0] == dtype
        assert src.crs.to_epsg() == epsg
        assert src.nodata == nodata
        gt = src.transform
        # Non-zero shear terms == the raster is not north-up.
        assert (gt.b != 0 or gt.d != 0) is rotated


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_fixture_stays_small(name):
    size = os.path.getsize(_path(name))
    assert size < MAX_FIXTURE_BYTES, (
        f"{name} is {size} bytes; fixtures must stay under {MAX_FIXTURE_BYTES}"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_fixture_cog_validity_is_as_documented(name):
    """Pins that exactly one fixture is a genuine non-COG.

    A 256x256 crop passes cog_validate however it was written, so without this the
    non-COG fixture could be silently replaced by a small one and the COG-detection
    tests would go green while testing nothing.
    """
    cogeo = pytest.importorskip("rio_cogeo.cogeo")
    expected_valid = EXPECTED[name][7]
    is_valid, errors, _ = cogeo.cog_validate(_path(name), quiet=True)
    assert is_valid is expected_valid, f"{name}: cog_validate errors={errors}"


@pytest.mark.parametrize("name", sorted(TAGGED))
def test_tagged_fixtures_carry_a_full_activation_event(name):
    """These are the inputs bake_event_metadata must SKIP rather than re-bake."""
    with rasterio.open(_path(name)) as src:
        tags = src.tags()
    assert tags.get("ACTIVATION_EVENT") == "202604_Typhoon_Sinlaku"
    assert tags.get("YEAR_MONTH") == "202604"
    assert tags.get("HAZARD") == "Typhoon"
    assert tags.get("LOCATION") == "Sinlaku"
    assert tags.get("PROCESSOR")


@pytest.mark.parametrize("name", sorted(BARE_8BIT_CONFLICT))
def test_8bit_fixtures_are_the_nodata_conflict_case(name):
    """The source declares nodata=0; the 8-bit carve-out says it should declare none.

    Both halves are asserted because the fixture is only interesting while they
    disagree -- if a future vendor crop arrived without the 0 tag it would still open
    fine and silently stop exercising the conflict.
    """
    from shared_utils.cog_utils import is_bare_8bit_imagery

    with rasterio.open(_path(name)) as src:
        assert src.nodata == 0.0, "source must still declare 0 as nodata"
        assert is_bare_8bit_imagery(src.dtypes[0], src.count) is True


def test_non_north_up_fixtures_exist():
    """Guards the rotated pair as a set -- both are easy to 'tidy up' into north-up."""
    rotated = []
    for name in EXPECTED:
        with rasterio.open(_path(name)) as src:
            if src.transform.b != 0 or src.transform.d != 0:
                rotated.append(name)
    assert set(rotated) == {
        "umbra_guam_sar_db_crop.tif",
        "mwir_rotated_geotransform_crop.tif",
    }
