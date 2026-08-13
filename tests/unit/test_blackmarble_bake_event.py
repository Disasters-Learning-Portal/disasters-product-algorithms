"""Assertions for dps/blackmarble/bake_event.py (the activation-event bake).

Black Marble is the one DPS job that writes its own COG instead of going through
shared_utils.convert_to_cog, so it never sees the --metadata-json path that gives every
process_* sensor its event tags. bake_event.py supplies them afterwards by RE-CREATING
each COG with the tags applied at creation.

Two things are worth pinning, and neither is visible from a green job:

  1. the tags actually land (and ACTIVATION_EVENT is split into YEAR_MONTH/HAZARD/
     LOCATION), and
  2. the raster lands in EPSG:3857 WITHOUT moving. Upstream writes an unnamed per-bbox
     Albers (crs.to_epsg() is None), which is what breaks veda-data-airflow's build_stac,
     so the bake -- the one place the COG is re-created -- is where the product gets a
     named projection. web_optimized must stay False: rio-cogeo's True also forces 3857
     but snaps onto the WebMercatorQuad grid, changing resolution and extent.

bake_event.py lives under dps/, which is not an installed package, so it is loaded by
path the way a DPS worker runs it.
"""
import importlib.util
import os

import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds
from rasterio.warp import transform_bounds

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BAKE_EVENT_PY = os.path.join(REPO_ROOT, "dps", "blackmarble", "bake_event.py")

EVENT = "202601_KyleWx_US"


def assert_same_ground(src, before_bounds):
    """The warp must change the PROJECTION without relocating the raster.

    Reprojecting an Albers rectangle into Web Mercator yields a slightly curved
    quadrilateral, so its axis-aligned bounds legitimately grow by up to about a pixel.
    What must NOT happen is the footprint sliding -- so this checks the CENTRE (which a
    shift moves and a bbox-expansion does not) and that no ground was dropped.
    """
    after = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
    px_deg = (after[2] - after[0]) / src.width

    for axis, (got, want) in enumerate(zip(after, before_bounds)):
        # grew outward, never inward: west/south may only decrease, east/north increase
        slack = 2 * px_deg
        if axis in (0, 1):
            assert want - slack <= got <= want + 1e-9, f"footprint moved (axis {axis})"
        else:
            assert want - 1e-9 <= got <= want + slack, f"footprint moved (axis {axis})"

    before_centre = ((before_bounds[0] + before_bounds[2]) / 2,
                     (before_bounds[1] + before_bounds[3]) / 2)
    after_centre = ((after[0] + after[2]) / 2, (after[1] + after[3]) / 2)
    for got, want in zip(after_centre, before_centre):
        assert abs(got - want) < px_deg, (
            f"centre shifted by {abs(got - want):.6f} deg (> one {px_deg:.6f} deg pixel): "
            f"{after_centre} vs {before_centre}"
        )


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
    # The source CRS is the per-bbox ad-hoc Albers upstream actually emits -- unnamed, with
    # no EPSG authority code -- NOT a tidy EPSG:4326. That is the input the bake has to cope
    # with, and an EPSG-backed stand-in would not exercise the same code path.
    albers = CRS.from_dict({
        "proj": "aea", "lon_0": -122.435, "lat_0": 37.75,
        "lat_1": 37.71, "lat_2": 37.79, "datum": "WGS84", "units": "m", "no_defs": None,
    })
    assert albers.to_epsg() is None
    with rasterio.open(
        str(path), "w", driver="GTiff", height=64, width=64, count=1,
        dtype="float32", crs=albers,
        transform=from_bounds(-10170, -6660, 10140, 6690, 64, 64),
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


def test_bake_reprojects_to_web_mercator(bake_event, bm_output):
    """Guards the target_crs=EPSG:3857 / web_optimized=False / preserve_compression=True call.

    Upstream emits an unnamed per-bbox Albers with no EPSG code, which is what breaks
    veda-data-airflow's build_stac (rio_stac.get_dataset_geom). The bake is the one place
    the COG is re-created, so it is where the product gets a NAMED projection.

    web_optimized must stay False: rio-cogeo's True ALSO forces 3857, but by snapping onto
    the WebMercatorQuad grid, which changes resolution and extent.
    """
    _out_home, path, _original = bm_output
    with rasterio.open(str(path)) as src:
        before_bounds = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
        before_compress = src.profile.get("compress")

    bake_event.bake(str(path), EVENT)

    with rasterio.open(str(path)) as src:
        assert src.crs.to_epsg() == 3857
        assert src.profile.get("compress") == before_compress  # preserve_compression=True
        assert_same_ground(src, before_bounds)
        assert src.read(1).any(), "reprojection produced an empty raster"


# Upstream's provenance block, as it appears on a real product (see the tags on
# tests/fixtures/blackmarble_sf_misregistered_crop.tif).
UPSTREAM_TAGS = {
    "bbox": "-122.55,37.69,-122.32,37.81",
    "crs": "+proj=aea +lon_0=-122.435 +lat_0=37.75 +datum=WGS84 +units=m",
    "producer": "Black Marble Pipeline",
    "resolution": "30.0",
    "data_sources": "{'viirs': {'files': ['VNP46A2.A2023166.h05v05.002.tif']}}",
    "processing_steps": "cloud_masking,spatial_reprojection,colormap_application",
    "indices": "ndvi,ndwi,ndui",
    "creation_date": "2026-08-12T15:36:53.625633",
}


def test_bake_preserves_upstream_provenance(bake_event, bm_output):
    """Reprojecting drops EVERY source tag, so they must be re-supplied explicitly.

    With target_crs set, create_cog_with_metadata wraps the source in a WarpedVRT, which
    carries no dataset tags -- cog_translate then has nothing to copy. Without the
    carry-forward this silently discards `data_sources`, the record of which VIIRS and
    Landsat granules the product was actually built from. That is the one tag you cannot
    reconstruct after the fact.
    """
    _out_home, path, _ = bm_output
    with rasterio.open(str(path), "r+") as src:
        src.update_tags(**UPSTREAM_TAGS)

    bake_event.bake(str(path), EVENT)

    with rasterio.open(str(path)) as src:
        tags = src.tags()

    for key in ("bbox", "producer", "data_sources", "processing_steps", "indices",
                "creation_date"):
        assert tags.get(key) == UPSTREAM_TAGS[key], f"{key} was lost by the reprojection"
    assert "VNP46A2" in tags["data_sources"]


def test_bake_rewrites_the_tags_the_warp_invalidates(bake_event, bm_output):
    """`crs` and `resolution` describe the PRE-warp grid, so copying them verbatim would
    make the file describe a projection and a pixel size it does not have."""
    _out_home, path, _ = bm_output
    with rasterio.open(str(path), "r+") as src:
        src.update_tags(**UPSTREAM_TAGS)

    bake_event.bake(str(path), EVENT)

    with rasterio.open(str(path)) as src:
        tags = src.tags()
        actual_res = abs(src.transform.a)

    assert tags["crs"] == "EPSG:3857"
    assert tags["crs"] != UPSTREAM_TAGS["crs"]
    # Web Mercator metres are not ground metres -- the pixel grows by ~1/cos(lat).
    assert float(tags["resolution"]) != float(UPSTREAM_TAGS["resolution"])
    assert abs(float(tags["resolution"]) - actual_res) < 0.01, (
        "the resolution tag must describe the raster we actually wrote"
    )


def test_bake_records_the_original_projection(bake_event, bm_output):
    """The per-bbox ad-hoc Albers has no EPSG code, so once the raster is in Web Mercator
    it is unrecoverable from anything else in the file. Keep it, or the native-grid product
    cannot be reconstructed and the resampling becomes invisible."""
    _out_home, path, _ = bm_output
    with rasterio.open(str(path)) as src:
        native_wkt = src.crs.to_wkt()

    bake_event.bake(str(path), EVENT)

    with rasterio.open(str(path)) as src:
        tags = src.tags()
        assert src.crs.to_epsg() == 3857

    assert tags["SOURCE_CRS"] == native_wkt
    assert CRS.from_wkt(tags["SOURCE_CRS"]).to_epsg() is None
    assert "aea" in tags["SOURCE_CRS_PROJ4"]
    assert "lon_0=-122.435" in tags["SOURCE_CRS_PROJ4"]


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


def test_bake_still_reprojects_under_noaa20(bake_event, bm_output):
    """The target_crs / web_optimized arguments are per-call, so re-assert them on the
    platform-aware path: a regression there leaves every NOAA-20 product in the unnamed
    Albers that build_stac cannot ingest."""
    _out_home, path, _original = bm_output
    with rasterio.open(str(path)) as src:
        before_bounds = transform_bounds(src.crs, "EPSG:4326", *src.bounds)

    bake_event.bake(str(path), EVENT, "noaa20")

    with rasterio.open(str(path)) as src:
        assert src.crs.to_epsg() == 3857
        assert_same_ground(src, before_bounds)


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
