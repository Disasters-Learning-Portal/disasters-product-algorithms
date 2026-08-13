"""End-to-end proof that the NOAA-20 Black Marble job produces the same product as the
Suomi-NPP one, from real NASA Earthdata.

WHAT THIS ANSWERS
-----------------
Every other test in this repo stubs something. This one stubs nothing that matters: it
queries CMR, downloads real VJ146A2 and VNP46A2 granules with a real Earthdata token, runs
the REAL upstream pipeline through the REAL dps/blackmarble{,_noaa}/run.sh, and then
compares the two platforms' finished COGs against each other.

The claim being verified is the one the whole change rests on: **VJ146A2 is a structural
twin of VNP46A2, so retargeting the product is sufficient and changes nothing else.** That
claim was sourced from the Black Marble User Guide and CMR metadata during design; here it
is checked against actual bytes. And the complementary claim: the swap ACTUALLY HAPPENED --
the NOAA-20 job downloads NOAA-20 data, rather than silently re-running Suomi-NPP under a
different product name, which is the one failure mode nothing downstream could detect.

RUNNING IT
----------
Opt-in, because it downloads hundreds of MB and takes minutes:

    export EARTHDATA_TOKEN='<your token>'     # environment only -- never a file in this repo
    DPS_E2E=1 conda run -n disasters_dps python -m pytest -v -ra -s tests/e2e/

Requires the `disasters_dps` env (upstream `blackmarble` + the libgdal-hdf5 driver plugin
for VIIRS .h5). CI does not run this: its pytest env has neither, and it has no token.

TWO TIERS, AND THEY NEED DIFFERENT THINGS:

  * The **VIIRS-layer** tests (everything not marked `slow`) need only `EARTHDATA_TOKEN`.
    They cover the entire surface this change touches -- the product swap, the download,
    the HDF5 equivalence -- and run anywhere with network access:
        DPS_E2E=1 pytest -m 'not slow' tests/e2e/
  * The **whole-pipeline** tests (`slow`) run both real `run.sh` scripts, so they also
    need Landsat and OSM. Landsat comes from the **requester-pays** bucket
    `s3://usgs-landsat`, which upstream reads with obstore -- so they need **ambient AWS
    credentials**. On a machine without them, obstore falls back to the EC2 metadata
    service and fails; the `job_runs` fixture recognises that exact signature and SKIPS
    with an explanation rather than reporting a code failure. Run them on a DPS worker or
    the MAAP hub, where those credentials are ambient.
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SNPP_RUN_SH = os.path.join(REPO_ROOT, "dps", "blackmarble", "run.sh")
NOAA_RUN_SH = os.path.join(REPO_ROOT, "dps", "blackmarble_noaa", "run.sh")
BM_NOAA_PY = os.path.join(REPO_ROOT, "dps", "blackmarble", "bm_noaa.py")

CMR = "https://cmr.earthdata.nasa.gov/search"

# A small San Francisco box on a date both satellites cover -- the same defaults the DPS
# job ships, so this exercises exactly what a bare Submit would run.
BBOX = "-122.55,37.69,-122.32,37.81"
BBOX_TUPLE = (-122.55, 37.69, -122.32, 37.81)
CORNERS = "37_81N122_55W37_69N122_32W"
DATE = "2023-06-15"
EVENT = "202601_KyleWx_US"

PRODUCTS = {"snpp": "VNP46A2", "noaa20": "VJ146A2"}
NTL_LAYER = "Gap_Filled_DNB_BRDF-Corrected_NTL"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("DPS_E2E") != "1",
        reason="opt-in: set DPS_E2E=1 (downloads real NASA data; takes minutes)",
    ),
]


def token():
    value = os.environ.get("EARTHDATA_TOKEN", "").strip()
    if not value:
        pytest.skip("EARTHDATA_TOKEN is not set in the environment")
    return value


def cmr_get(path, **params):
    url = f"{CMR}/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def granule_urls(short_name, version="2", date=DATE, bbox=BBOX):
    """The .h5 download links CMR advertises for one product/date/box."""
    feed = cmr_get(
        "granules.json",
        short_name=short_name, version=version,
        temporal=f"{date}T00:00:00Z,{date}T23:59:59Z",
        bounding_box=bbox, page_size=10,
    )["feed"]["entry"]
    found = []
    for entry in feed:
        links = [
            link["href"] for link in entry.get("links", [])
            if link.get("rel", "").endswith("/data#") and link["href"].endswith(".h5")
        ]
        if links:
            found.append((entry.get("producer_granule_id") or entry["title"], links[0]))
    return found


def fetch(url, dest, byte_range=None):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token()}"})
    if byte_range:
        request.add_header("Range", f"bytes={byte_range[0]}-{byte_range[1]}")
    with urllib.request.urlopen(request, timeout=600) as response:
        payload = response.read()
    if dest:
        with open(dest, "wb") as handle:
            handle.write(payload)
    return payload


@pytest.fixture(scope="session")
def granule_cache(tmp_path_factory):
    """One real granule per product, downloaded once and shared by the data-layer tests."""
    directory = tmp_path_factory.mktemp("granules")
    paths = {}
    for platform, short_name in PRODUCTS.items():
        granules = granule_urls(short_name)
        assert granules, f"CMR returned no {short_name} granules for {DATE} {BBOX}"
        name, url = granules[0]
        destination = directory / name
        fetch(url, destination)
        paths[platform] = destination
    return paths


# =====================================================================================
# 1. CMR: the two products really are the same coverage
# =====================================================================================


def test_cmr_resolves_exactly_one_vj146a2_collection():
    """A short-name that matched two collections would make the swap ambiguous."""
    collections = cmr_get("collections.umm_json", short_name="VJ146A2")
    assert collections["hits"] == 1
    umm = collections["items"][0]["umm"]
    assert umm["Version"] == "2", "bm_noaa.py pins version 2; CMR disagrees"
    assert collections["items"][0]["meta"]["concept-id"] == "C3370789118-LAADS"


def test_vj146a2_temporal_start_is_the_date_floor_run_sh_enforces():
    """dps/blackmarble/platform.sh hardcodes 2018-01-19 -- check it against the source."""
    collections = cmr_get("collections.umm_json", short_name="VJ146A2")
    start = collections["items"][0]["umm"]["TemporalExtents"][0]["RangeDateTimes"][0]
    assert start["BeginningDateTime"].startswith("2018-01-19")


def test_both_products_are_the_same_version():
    """Why bm_noaa.py can pin version "2" for both and swap only the short name."""
    versions = {
        short_name: cmr_get("collections.umm_json", short_name=short_name)
        ["items"][0]["umm"]["Version"]
        for short_name in PRODUCTS.values()
    }
    assert versions["VNP46A2"] == versions["VJ146A2"] == "2"


def test_both_products_cover_the_same_tiles_on_the_same_day():
    """Same grid, same tiling, same day -> the pipeline sees an equivalent input mosaic."""
    def tiles(short_name):
        return sorted(name.split(".")[2] for name, _ in granule_urls(short_name))

    snpp, noaa = tiles("VNP46A2"), tiles("VJ146A2")
    assert snpp and noaa
    assert snpp == noaa, f"tile coverage differs: VNP46A2={snpp} VJ146A2={noaa}"


# =====================================================================================
# 2. The granules download, and are byte-level HDF5
# =====================================================================================


@pytest.mark.parametrize("short_name", sorted(PRODUCTS.values()))
def test_granule_downloads_with_the_earthdata_token(short_name):
    _name, url = granule_urls(short_name)[0]
    head = fetch(url, dest=None, byte_range=(0, 7))
    assert head == b"\x89HDF\r\n\x1a\n", f"{short_name} did not return an HDF5 payload"


# =====================================================================================
# 3. Structural identity of the two HDF5 products
# =====================================================================================


def test_the_two_products_expose_the_same_subdatasets(granule_cache):
    """The core claim. If VJ146A2's layer set differed, a product swap would not be enough
    -- upstream's convert_to_tiff substring-matches ONE layer path and would IndexError
    (or, worse, match a different band)."""
    import rasterio

    layers = {}
    for platform, path in granule_cache.items():
        with rasterio.open(str(path)) as src:
            subdatasets = src.subdatasets
        # Strip the file path; keep only the in-file dataset path.
        layers[platform] = sorted(sub.rsplit('"', 1)[-1].lstrip(":") for sub in subdatasets)

    assert layers["snpp"] == layers["noaa20"]
    assert any(NTL_LAYER in name for name in layers["noaa20"])


def test_the_ntl_layer_has_identical_structure(granule_cache):
    """Same shape, dtype and fill in both products -- everything downstream assumes it."""
    import rasterio

    profiles = {}
    for platform, path in granule_cache.items():
        with rasterio.open(str(path)) as src:
            layer = [sub for sub in src.subdatasets if NTL_LAYER in sub][0]
        with rasterio.open(layer) as band:
            profiles[platform] = {
                "shape": (band.width, band.height),
                "dtype": band.dtypes[0],
                "count": band.count,
                "nodata": band.nodata,
            }

    assert profiles["snpp"] == profiles["noaa20"]
    assert profiles["noaa20"]["shape"] == (2400, 2400)


def test_upstreams_layer_path_resolves_in_a_vj146a2_granule(granule_cache):
    """bm_noaa.py deliberately does NOT patch NTL_DATASET_PATH. Prove that is correct
    against a real NOAA-20 granule rather than against the product documentation."""
    import importlib.util

    import rasterio

    spec = importlib.util.spec_from_file_location("bm_noaa_e2e", BM_NOAA_PY)
    bm_noaa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bm_noaa)

    with rasterio.open(str(granule_cache["noaa20"])) as src:
        matches = [
            sub for sub in src.subdatasets
            if bm_noaa.UPSTREAM_NTL_DATASET_PATH in sub
        ]
    assert len(matches) == 1, (
        "upstream's NTL_DATASET_PATH does not resolve to exactly one subdataset in a "
        "VJ146A2 granule; the unpatched layer path assumption is wrong"
    )


def test_upstream_convert_to_tiff_produces_matching_rasters(granule_cache, tmp_path):
    """Run UPSTREAM's own h5 -> GeoTIFF step on both products and compare the results.

    This is the last data-layer difference that could exist: identical layer metadata but
    a different georeferencing or scaling once GDAL reads it.
    """
    import rasterio
    from blackmarble.acquire.viirs import convert_to_tiff

    profiles = {}
    for platform, path in granule_cache.items():
        output = tmp_path / f"{platform}.tif"
        convert_to_tiff(str(path), str(output))
        with rasterio.open(str(output)) as src:
            profiles[platform] = {
                "crs": src.crs.to_string() if src.crs else None,
                "transform": tuple(src.transform),
                "shape": (src.width, src.height),
                "dtype": src.dtypes[0],
                "count": src.count,
                "nodata": src.nodata,
            }

    assert profiles["snpp"] == profiles["noaa20"]


def test_the_two_products_carry_different_radiance(granule_cache):
    """The control: identical STRUCTURE, different DATA.

    Two satellites imaging at different overpass times cannot produce bit-identical
    radiance. If these matched exactly, the cache (or the search) would be handing back
    the same granule twice and every "identical" assertion above would be vacuous.
    """
    import numpy as np
    import rasterio

    arrays = {}
    for platform, path in granule_cache.items():
        with rasterio.open(str(path)) as src:
            layer = [sub for sub in src.subdatasets if NTL_LAYER in sub][0]
        with rasterio.open(layer) as band:
            arrays[platform] = band.read(1)

    assert not np.array_equal(arrays["snpp"], arrays["noaa20"])


# =====================================================================================
# 4. The patch changes what is actually downloaded
# =====================================================================================


def test_patched_download_viirs_fetches_vj146a2_granules(tmp_path):
    """UPSTREAM's real download_viirs, real Earthdata auth, real files on disk.

    Nothing here is stubbed. If the monkeypatch were a no-op, the filenames landing in
    tmp_path would start with VNP46A2 while the job around them said NOAA-20 -- the exact
    silent failure the shim exists to prevent.
    """
    import importlib.util
    from datetime import datetime

    spec = importlib.util.spec_from_file_location("bm_noaa_e2e_dl", BM_NOAA_PY)
    bm_noaa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bm_noaa)

    import blackmarble.acquire.viirs as viirs

    saved = (viirs.BM_SHORT_NAME, viirs.BM_VERSION)
    try:
        bm_noaa.apply_noaa20_patch()
        os.environ["EARTHDATA_TOKEN"] = token()
        result = viirs.download_viirs(
            datetime.strptime(DATE, "%Y-%m-%d"), BBOX_TUPLE, str(tmp_path)
        )
    finally:
        viirs.BM_SHORT_NAME, viirs.BM_VERSION = saved

    downloaded = [path.name for path in tmp_path.glob("*.h5")]
    assert downloaded, "no granules downloaded"
    assert all(name.startswith("VJ146A2") for name in downloaded), downloaded
    assert not any(name.startswith("VNP46A2") for name in downloaded), downloaded
    assert result["gap_filled_ntl"]


# =====================================================================================
# 5. The whole DPS job, both platforms, compared
# =====================================================================================


# Failures that mean "this MACHINE cannot run the pipeline", not "the code is broken".
# The Black Marble pipeline fuses VIIRS with Landsat and OSM; Landsat comes from the
# REQUESTER-PAYS bucket s3://usgs-landsat, which upstream reads via obstore. With no AWS
# credentials, obstore falls back to the EC2 instance metadata service (169.254.169.254)
# and fails there. Skipping on these exact signatures — and ONLY these — keeps a
# credential-less laptop from looking like a regression, while any other failure still
# fails the test. Everything ABOVE this point (the VIIRS layer, which is all this change
# touches) runs with the Earthdata token alone and is not affected.
ENVIRONMENT_BLOCKERS = {
    "169.254.169.254": (
        "no AWS credentials: upstream reads Landsat from the requester-pays bucket "
        "s3://usgs-landsat and fell back to the EC2 instance metadata service"
    ),
    "Temporary failure in name resolution": "no DNS / network access",
    "Max retries exceeded": "an upstream data service was unreachable",
}


def environment_blocker(log):
    for signature, reason in ENVIRONMENT_BLOCKERS.items():
        if signature in log:
            return reason
    return None


@pytest.fixture(scope="session")
def job_runs(tmp_path_factory):
    """Run BOTH real run.sh scripts for the same bbox/date/event, into one sandbox HOME.

    Session-scoped: each run downloads VIIRS + Landsat + OSM, so they happen once.
    DPS_DRY_RUN=1 keeps the products local (no publish to nasa-disasters-staging) and
    stops _finalize.sh from deleting them before they can be inspected.
    """
    if shutil.which("conda") is None:
        pytest.skip("conda is required to run the DPS run.sh scripts")

    root = tmp_path_factory.mktemp("jobs")
    home = root / "home"
    home.mkdir()

    results = {}
    for platform, script in (("noaa20", NOAA_RUN_SH), ("snpp", SNPP_RUN_SH)):
        cwd = root / platform
        cwd.mkdir()
        environ = dict(os.environ)
        environ.update({
            "HOME": str(home),
            "EARTHDATA_TOKEN": token(),   # skips the MAAP secret lookup
            "DPS_DRY_RUN": "1",
        })
        proc = subprocess.run(
            ["bash", script,
             "--activation_event", EVENT, "--bbox", BBOX, "--date", DATE,
             "--config", "fast", "--osm_source", "overpass"],
            cwd=str(cwd), env=environ, capture_output=True, text=True, timeout=3600,
        )
        log = proc.stdout + proc.stderr
        if proc.returncode != 0:
            blocker = environment_blocker(log)
            if blocker:
                pytest.skip(
                    f"the {platform} pipeline could not run on this machine: {blocker}. "
                    f"Run these on a DPS worker or the MAAP hub, where the AWS "
                    f"credentials Landsat needs are ambient. (The VIIRS-layer tests in "
                    f"this file — which are what the NOAA-20 change actually affects — "
                    f"need only EARTHDATA_TOKEN and are unaffected.)"
                )
        results[platform] = {
            "proc": proc,
            "home": home,
            "cwd": cwd,
            "log": log,
        }
    return results


def product_dir(run, product):
    return run["home"] / "drcs_outputs" / EVENT / DATE.replace("-", "") / product


def products(run, product):
    """Every .tif this run produced -- and NEVER an empty list.

    A test that loops over a glob does nothing at all when the glob is empty, so it
    reports success for a pipeline that produced no output. That is exactly what
    happened the first time this suite ran against a machine without AWS credentials:
    the COG-validity and provenance tests passed over zero files while the jobs had in
    fact failed. Every consumer goes through here.
    """
    found = sorted(product_dir(run, product).glob("*.tif"))
    assert found, f"no products under {product_dir(run, product)}"
    return found


@pytest.mark.slow
@pytest.mark.parametrize("platform,product", [
    ("noaa20", "hdnightlightsnoaa20"),
    ("snpp", "hdnightlights"),
])
def test_the_job_succeeds_and_writes_the_expected_products(job_runs, platform, product):
    run = job_runs[platform]
    assert run["proc"].returncode == 0, run["log"][-4000:]

    assert [path.name for path in products(run, product)] == [
        f"{product}_{CORNERS}_{DATE}_day.tif",
        f"{product}colored_{CORNERS}_{DATE}_day.tif",
    ]


@pytest.mark.slow
def test_the_noaa_job_really_downloaded_noaa20_data(job_runs):
    """Read it out of the job's own log: upstream logs the granules it downloaded."""
    log = job_runs["noaa20"]["log"]
    assert "VJ146A2" in log, "the NOAA-20 job's log never mentions VJ146A2"
    assert "VNP46A2" not in log, "the NOAA-20 job downloaded Suomi-NPP granules"


@pytest.mark.slow
def test_the_snpp_job_really_downloaded_suomi_npp_data(job_runs):
    log = job_runs["snpp"]["log"]
    assert "VNP46A2" in log
    assert "VJ146A2" not in log


@pytest.mark.slow
@pytest.mark.parametrize("platform,product", [
    ("noaa20", "hdnightlightsnoaa20"),
    ("snpp", "hdnightlights"),
])
def test_every_product_is_a_valid_cog(job_runs, platform, product):
    """titiler/VEDA will only tile a real COG; local cog_validate is the same check."""
    from rio_cogeo.cogeo import cog_validate

    for path in products(job_runs[platform], product):
        valid, errors, _warnings = cog_validate(str(path))
        assert valid, f"{path.name} is not a valid COG: {errors}"


@pytest.mark.slow
@pytest.mark.parametrize("platform,product,expected", [
    ("noaa20", "hdnightlightsnoaa20", ("VJ146A2", "NOAA-20")),
    ("snpp", "hdnightlights", ("VNP46A2", "Suomi-NPP")),
])
def test_every_product_carries_correct_provenance(job_runs, platform, product, expected):
    """The published COG must say which satellite it came from -- the pixels cannot."""
    import rasterio

    code, satellite = expected
    for path in products(job_runs[platform], product):
        with rasterio.open(str(path)) as src:
            tags = src.tags()
        assert tags["ACTIVATION_EVENT"] == EVENT
        assert tags["YEAR_MONTH"] == "202601"
        assert tags["HAZARD"] == "KyleWx"
        assert tags["LOCATION"] == "US"
        assert tags["PROCESSOR"].startswith("NASA Disasters COG Processor")
        assert tags["VIIRS_PRODUCT"] == code
        assert tags["VIIRS_PLATFORM"] == satellite
        assert code in tags["SOURCE"]
        # upstream's own data_sources tag must have been corrected too
        assert "VNP46A2" not in tags.get("data_sources", "") or platform == "snpp"


@pytest.mark.slow
def test_the_two_platforms_produce_structurally_identical_products(job_runs):
    """THE comparison this suite exists for.

    Same bbox, same date, same preset, same code path -- so the finished rasters must
    agree on every structural property. A difference here means the platform swap changed
    something it had no business changing (grid, projection, dtype, nodata, band count).
    """
    import rasterio

    def describe(platform, product):
        path = product_dir(job_runs[platform], product) / f"{product}_{CORNERS}_{DATE}_day.tif"
        with rasterio.open(str(path)) as src:
            return {
                "crs": src.crs.to_string(),
                "transform": tuple(round(value, 6) for value in src.transform),
                "shape": (src.width, src.height),
                "dtype": src.dtypes[0],
                "count": src.count,
                "nodata": repr(src.nodata),
                "blockshapes": src.block_shapes,
                "overviews": src.overviews(1),
                "compress": src.profile.get("compress"),
            }

    noaa = describe("noaa20", "hdnightlightsnoaa20")
    snpp = describe("snpp", "hdnightlights")
    assert noaa == snpp


@pytest.mark.slow
def test_the_two_platforms_produce_different_pixels(job_runs):
    """The control for the test above.

    Structural identity is only meaningful alongside this: NOAA-20 and Suomi-NPP observe
    at different times, so the radiance-derived index must differ. If these were equal,
    the NOAA-20 job would in fact be re-running Suomi-NPP.
    """
    import numpy as np
    import rasterio

    def read(platform, product):
        path = product_dir(job_runs[platform], product) / f"{product}_{CORNERS}_{DATE}_day.tif"
        with rasterio.open(str(path)) as src:
            return src.read(1)

    noaa = read("noaa20", "hdnightlightsnoaa20")
    snpp = read("snpp", "hdnightlights")
    assert noaa.shape == snpp.shape
    assert not np.array_equal(np.nan_to_num(noaa), np.nan_to_num(snpp))


@pytest.mark.slow
def test_the_two_jobs_publish_to_non_colliding_paths(job_runs):
    """Both wrote into ONE HOME for one event and date; all four products survive."""
    date_dir = job_runs["noaa20"]["home"] / "drcs_outputs" / EVENT / DATE.replace("-", "")
    found = sorted(
        str(path.relative_to(date_dir)) for path in date_dir.rglob("*.tif")
    )
    assert found == [
        f"hdnightlights/hdnightlights_{CORNERS}_{DATE}_day.tif",
        f"hdnightlights/hdnightlightscolored_{CORNERS}_{DATE}_day.tif",
        f"hdnightlightsnoaa20/hdnightlightsnoaa20_{CORNERS}_{DATE}_day.tif",
        f"hdnightlightsnoaa20/hdnightlightsnoaa20colored_{CORNERS}_{DATE}_day.tif",
    ]


@pytest.mark.slow
def test_the_earthdata_token_never_reaches_the_job_log(job_runs):
    """A DPS job log is readable by anyone who can see the job."""
    secret = token()
    assert set(job_runs) == {"snpp", "noaa20"}
    for platform, run in job_runs.items():
        assert run["log"], f"{platform} job produced no log to check"
        assert secret not in run["log"], f"{platform} job leaked the Earthdata token"


@pytest.mark.slow
@pytest.mark.parametrize("platform,product", [
    ("noaa20", "hdnightlightsnoaa20"),
    ("snpp", "hdnightlights"),
])
def test_products_are_staged_for_dps_upload(job_runs, platform, product):
    """_finalize.sh step 1: DPS uploads output/, so a product missing there is lost."""
    staged = job_runs[platform]["cwd"] / "output" / DATE.replace("-", "") / product
    found = sorted(path.name for path in staged.glob("*.tif"))
    assert found, f"nothing staged under {staged}"
    assert found == [
        f"{product}_{CORNERS}_{DATE}_day.tif",
        f"{product}colored_{CORNERS}_{DATE}_day.tif",
    ]
