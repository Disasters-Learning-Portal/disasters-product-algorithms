"""Assertions for dps/blackmarble/bake_event.py (the activation-event bake).

Black Marble is the one DPS job that writes its own COG instead of going through
shared_utils.convert_to_cog, so it never sees the --metadata-json path that gives every
process_* sensor its event tags. bake_event.py supplies them afterwards by RE-CREATING
each COG with the tags applied at creation.

Two things are worth pinning, and neither is visible from a green job:

  1. the tags actually land (and ACTIVATION_EVENT is split into YEAR_MONTH/HAZARD/
     LOCATION), and
  2. the raster itself is untouched. create_cog_with_metadata defaults
     web_optimized=True, which REPROJECTS to EPSG:3857 -- a default that would silently
     resample every nighttime-lights product if the call ever lost its explicit
     web_optimized=False / target_crs=None.

bake_event.py lives under dps/, which is not an installed package, so it is loaded by
path the way a DPS worker runs it.
"""
import importlib.util
import os

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BAKE_EVENT_PY = os.path.join(REPO_ROOT, "dps", "blackmarble", "bake_event.py")

EVENT = "202601_KyleWx_US"


@pytest.fixture(scope="module")
def bake_event():
    spec = importlib.util.spec_from_file_location("bm_bake_event", BAKE_EVENT_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=["hdnightlights", "hdnightlightsnoaa20"])
def bm_output(request, tmp_path):
    """A Black Marble-shaped output tree: <event>/<YYYYMMDD>/<product>/<stem>.tif.

    Parameterized over both platforms' product tokens: the bake path must behave
    identically whichever algorithm produced the tree.
    """
    product = request.param
    out_home = tmp_path / "drcs_outputs" / EVENT
    product_dir = out_home / "20230615" / product
    product_dir.mkdir(parents=True)
    path = product_dir / f"{product}_37_81N122_55W37_69N122_32W_2023-06-15_day.tif"

    data = np.linspace(0, 100, 64 * 64, dtype="float32").reshape(64, 64)
    with rasterio.open(
        str(path), "w", driver="GTiff", height=64, width=64, count=1,
        dtype="float32", crs="EPSG:4326",
        transform=from_bounds(-122.55, 37.69, -122.32, 37.81, 64, 64),
        tiled=True, blockxsize=64, blockysize=64, compress="zstd",
    ) as dst:
        dst.write(data, 1)
    return out_home, path, data


def test_bake_embeds_event_and_splits_it(bake_event, bm_output):
    out_home, path, _ = bm_output
    bake_event.bake(str(path), EVENT)

    with rasterio.open(str(path)) as src:
        tags = src.tags()

    assert tags["ACTIVATION_EVENT"] == EVENT
    # resolve_metadata derives these three from the event string -- the operator sets
    # only ACTIVATION_EVENT.
    assert tags["YEAR_MONTH"] == "202601"
    assert tags["HAZARD"] == "KyleWx"
    assert tags["LOCATION"] == "US"
    assert tags["PROCESSOR"].startswith("NASA Disasters COG Processor")
    assert "Black Marble" in tags["SOURCE"]


def test_bake_leaves_the_raster_identical(bake_event, bm_output):
    """Guards the web_optimized=False / target_crs=None / preserve_compression=True call.

    If any of those regressed to the function's defaults, the product would be silently
    reprojected to EPSG:3857 and resampled.
    """
    _out_home, path, original = bm_output
    with rasterio.open(str(path)) as src:
        before = (src.crs.to_string(), src.transform, src.width, src.height,
                  src.profile.get("compress"))

    bake_event.bake(str(path), EVENT)

    with rasterio.open(str(path)) as src:
        assert src.crs.to_string() == before[0]      # NOT reprojected to EPSG:3857
        assert src.transform == before[1]
        assert (src.width, src.height) == before[2:4]
        assert src.profile.get("compress") == before[4]
        np.testing.assert_array_equal(src.read(1), original)


def test_main_walks_the_tree_and_bakes_every_tif(bake_event, bm_output, monkeypatch):
    """run.sh points bake_event.py at OUT_HOME, so it must recurse into <date>/<product>.

    Both the plain product and its colored companion have to be stamped -- an operator
    who opens only the colored raster should still see the event.
    """
    out_home, path, _ = bm_output
    colored = path.parent / (
        "hdnightlightscolored_37_81N122_55W37_69N122_32W_2023-06-15_day.tif"
    )
    with rasterio.open(str(path)) as src:
        profile = src.profile
        data = src.read(1)
    with rasterio.open(str(colored), "w", **profile) as dst:
        dst.write(data, 1)

    monkeypatch.setattr(
        "sys.argv",
        ["bake_event.py", "--out-home", str(out_home), "--event", EVENT],
    )
    assert bake_event.main() == 0

    for baked in (path, colored):
        with rasterio.open(str(baked)) as src:
            assert src.tags()["ACTIVATION_EVENT"] == EVENT, f"{baked.name} not baked"


def test_main_fails_loudly_when_no_tif_is_present(bake_event, tmp_path, monkeypatch):
    """An empty tree means the layout moved underneath us -- fail, never skip quietly.

    run.sh already dies on "no COG produced"; reaching bake_event with nothing to bake
    would otherwise publish untagged products under a green job.
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(
        "sys.argv", ["bake_event.py", "--out-home", str(empty), "--event", EVENT]
    )
    assert bake_event.main() == 1


# --- platform-aware provenance tags -------------------------------------------------
#
# Black Marble runs on one of two VIIRS products, and which one is NOT recoverable from
# the pixels. The COG's own tags are the only durable record: a wrong SOURCE /
# VIIRS_PRODUCT publishes a NOAA-20 raster labelled Suomi-NPP (or the reverse), and
# nothing downstream can catch it.


@pytest.mark.parametrize("platform,product,satellite", [
    ("snpp", "VNP46A2", "Suomi-NPP"),
    ("noaa20", "VJ146A2", "NOAA-20"),
])
def test_bake_records_which_viirs_product_was_used(bake_event, bm_output, platform,
                                                   product, satellite):
    _out_home, path, _ = bm_output
    bake_event.bake(str(path), EVENT, platform)

    with rasterio.open(str(path)) as src:
        tags = src.tags()

    # VIIRS_PRODUCT and VIIRS_PLATFORM are their own tags so a consumer can read the code
    # and the satellite directly instead of parsing the prose SOURCE string.
    assert tags["VIIRS_PRODUCT"] == product
    assert tags["VIIRS_PLATFORM"] == satellite
    # SOURCE names the product and the PLATFORM TOKEN -- `noaa20` is the same string as
    # BM_PLATFORM, the hdnightlightsnoaa20 product folder and the S3 prefix, so one grep
    # finds all of them.
    assert product in tags["SOURCE"]
    assert platform in tags["SOURCE"]
    # The other platform's product code / token must never appear -- that is the mislabel.
    other_product = "VJ146A2" if product == "VNP46A2" else "VNP46A2"
    other_token = "noaa20" if platform == "snpp" else "snpp"
    assert other_product not in tags["SOURCE"]
    assert other_token not in tags["SOURCE"]
    assert tags["VIIRS_PRODUCT"] != other_product


def test_bake_defaults_to_suomi_npp(bake_event, bm_output):
    """The Suomi-NPP job predates the --platform flag; omitting it must keep its behavior."""
    _out_home, path, _ = bm_output
    bake_event.bake(str(path), EVENT)

    with rasterio.open(str(path)) as src:
        assert src.tags()["VIIRS_PRODUCT"] == "VNP46A2"


def test_bake_rejects_an_unknown_platform(bake_event, bm_output):
    """Fail rather than fall back to a default -- a silent default would tag a NOAA-20
    product as Suomi-NPP."""
    _out_home, path, _ = bm_output
    with pytest.raises(ValueError, match="unknown platform"):
        bake_event.bake(str(path), EVENT, "noaa-20")


def test_bake_still_leaves_the_raster_identical_under_noaa20(bake_event, bm_output):
    """The web_optimized=False / target_crs=None guard is per-call, so re-assert it on the
    platform-aware path: a regression there silently reprojects every NOAA-20 product."""
    _out_home, path, original = bm_output
    with rasterio.open(str(path)) as src:
        before = (src.crs.to_string(), src.transform, src.width, src.height)

    bake_event.bake(str(path), EVENT, "noaa20")

    with rasterio.open(str(path)) as src:
        assert src.crs.to_string() == before[0]
        assert src.transform == before[1]
        assert (src.width, src.height) == before[2:4]
        np.testing.assert_array_equal(src.read(1), original)


def test_main_passes_the_platform_through(bake_event, bm_output, monkeypatch):
    """run.sh invokes bake_event.py with --platform "${BM_PLATFORM}"; the CLI must honour it."""
    out_home, path, _ = bm_output
    monkeypatch.setattr(
        "sys.argv",
        ["bake_event.py", "--out-home", str(out_home), "--event", EVENT,
         "--platform", "noaa20"],
    )
    assert bake_event.main() == 0

    with rasterio.open(str(path)) as src:
        assert src.tags()["VIIRS_PRODUCT"] == "VJ146A2"


def test_main_rejects_an_unknown_platform_at_the_cli(bake_event, bm_output, monkeypatch):
    """argparse choices, so a typo dies at parse time with a usable message."""
    out_home, _path, _ = bm_output
    monkeypatch.setattr(
        "sys.argv",
        ["bake_event.py", "--out-home", str(out_home), "--event", EVENT,
         "--platform", "jpss1"],
    )
    with pytest.raises(SystemExit) as excinfo:
        bake_event.main()
    assert excinfo.value.code == 2


def test_every_platform_in_the_table_bakes(bake_event, bm_output):
    """Guards against adding a platform to PLATFORMS with an incomplete tag set."""
    _out_home, path, _ = bm_output
    for platform in bake_event.PLATFORMS:
        metadata = bake_event.bake(str(path), EVENT, platform)
        assert metadata["SOURCE"] and metadata["VIIRS_PRODUCT"] and metadata["VIIRS_PLATFORM"]
        assert metadata["ACTIVATION_EVENT"] == EVENT
