"""dps/blackmarble/bm_noaa.py against the REAL pinned upstream package.

test_blackmarble_noaa_patch.py proves the patch logic is correct against a stub. This file
proves the stub is not fiction: that the upstream `blackmarble` pinned in
dps/environment.yml still has the shape the patch reaches into, and that patching it really
does change what gets requested from Earthdata.

That split exists because CI's pytest env (dev-conda-deps.txt + .[test]) does NOT carry
`blackmarble` -- it is pip-installed only into the DPS worker env. So these tests skip in
CI and run where it matters:

    conda run -n disasters_dps python -m pytest tests/unit/test_blackmarble_noaa_upstream_contract.py

The same guard also runs at image-build time as `bm_noaa.py --self-check`
(dps/Dockerfile), so upstream drift fails a build rather than an activation.

No network and no credentials: earthaccess is stubbed at the module attribute upstream
reads it through, so the REAL upstream download_viirs executes and we assert on the query
it composed.
"""
import importlib.util
import inspect
import os
from datetime import datetime
from pathlib import Path

import pytest

blackmarble = pytest.importorskip(
    "blackmarble",
    reason="upstream blackmarble is only installed in the disasters_dps DPS worker env",
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BM_NOAA_PY = os.path.join(REPO_ROOT, "dps", "blackmarble", "bm_noaa.py")


@pytest.fixture
def bm_noaa():
    spec = importlib.util.spec_from_file_location("bm_noaa_upstream", BM_NOAA_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def upstream_restored():
    """Snapshot and restore the real module state.

    apply_noaa20_patch mutates process-global upstream state by design; without this a
    patched module would leak into every later test in the session.
    """
    import blackmarble.acquire.viirs as viirs
    import blackmarble.export as bm_export

    saved = (viirs.BM_SHORT_NAME, viirs.BM_VERSION, bm_export.create_metadata)
    yield viirs, bm_export
    viirs.BM_SHORT_NAME, viirs.BM_VERSION, bm_export.create_metadata = saved


# --- the shape bm_noaa.py depends on ----------------------------------------------


def test_upstream_still_hardcodes_vnp46a2(bm_noaa, upstream_restored):
    viirs, _ = upstream_restored
    assert viirs.BM_SHORT_NAME == bm_noaa.UPSTREAM_SHORT_NAME


def test_upstream_version_is_the_one_vj146a2_also_publishes(bm_noaa, upstream_restored):
    """Both collections are CMR version 2 (verified live), which is why the swap is safe."""
    viirs, _ = upstream_restored
    assert viirs.BM_VERSION == bm_noaa.VIIRS_VERSION == "2"


def test_upstream_layer_path_is_the_shared_one(bm_noaa, upstream_restored):
    viirs, _ = upstream_restored
    assert viirs.NTL_DATASET_PATH == bm_noaa.UPSTREAM_NTL_DATASET_PATH


def test_download_viirs_still_takes_no_product_argument():
    """The reason a module-constant patch is needed at all.

    The day upstream adds a product parameter, this fails -- which is the signal to delete
    the shim and pass their argument instead.
    """
    import blackmarble.acquire.viirs as viirs

    params = set(inspect.signature(viirs.download_viirs).parameters)
    assert params == {"dt", "bbox", "output_dir"}, (
        f"upstream download_viirs signature changed to {params}; if it now accepts a "
        f"product/short_name argument, drop bm_noaa.py's monkeypatch and pass it directly"
    )


def test_upstream_cli_still_has_no_product_option():
    """Same signal from the CLI side: no --product / --short-name / --platform today."""
    import blackmarble.cli as cli

    params = set(inspect.signature(cli.run).parameters)
    assert not params & {"product", "short_name", "viirs_product", "platform", "collection"}


def test_pipeline_reaches_create_metadata_through_the_export_package():
    """The wrapper is installed on blackmarble.export; if pipeline.py ever imported
    create_metadata by name instead, the wrapper would be bypassed and the COG's
    data_sources tag would keep saying VNP46A2 on a NOAA-20 product."""
    import blackmarble.pipeline as pipeline

    source = inspect.getsource(pipeline)
    assert "export.create_metadata(" in source
    assert "from .export import create_metadata" not in source
    assert "from blackmarble.export import create_metadata" not in source


# --- the patch applied to the real thing ------------------------------------------


def test_patch_applies_cleanly_to_the_real_package(bm_noaa, upstream_restored):
    viirs, bm_export = upstream_restored
    assert bm_noaa.apply_noaa20_patch() == ("VJ146A2", "2")
    assert viirs.BM_SHORT_NAME == "VJ146A2"
    assert getattr(bm_export.create_metadata, "_bm_noaa20_wrapped", False)


def test_real_download_viirs_queries_vj146a2(bm_noaa, upstream_restored, monkeypatch,
                                             tmp_path):
    """The single most important assertion in this file.

    Runs UPSTREAM's own download_viirs -- not a re-implementation -- with earthaccess
    stubbed, and asserts the CMR query it composed asks for VJ146A2. If the patch were a
    no-op, this is where it shows: short_name would come back VNP46A2 while every name,
    tag and S3 prefix downstream still said NOAA-20.
    """
    viirs, _ = upstream_restored
    bm_noaa.apply_noaa20_patch()

    seen = {}

    class FakeEarthaccess:
        @staticmethod
        def login():
            seen["logged_in"] = True

        @staticmethod
        def search_data(**kwargs):
            seen["search"] = kwargs
            return ["granule-1"]

        @staticmethod
        def download(results, local_path=None, show_progress=None):
            seen["download"] = {"results": results, "local_path": local_path}
            return [Path(local_path) / "VJ146A2.A2023166.h05v05.002.h5"]

    monkeypatch.setattr(viirs, "earthaccess", FakeEarthaccess)
    monkeypatch.setattr(viirs, "convert_to_tiff", lambda src, dst, *a, **k: Path(dst))

    result = viirs.download_viirs(
        datetime(2023, 6, 15), (-122.55, 37.69, -122.32, 37.81), tmp_path
    )

    assert seen["search"]["short_name"] == "VJ146A2"
    assert seen["search"]["version"] == "2"
    assert seen["search"]["bounding_box"] == (-122.55, 37.69, -122.32, 37.81)
    assert seen["search"]["temporal"][0] == "2023-06-15"
    assert result["gap_filled_ntl"][0].suffix == ".tif"


def test_unpatched_download_viirs_still_queries_vnp46a2(upstream_restored, monkeypatch,
                                                        tmp_path):
    """The control for the test above: without the patch, the query is Suomi-NPP.

    Together the two prove the difference is caused by the patch and not by the stub.
    """
    viirs, _ = upstream_restored
    seen = {}

    class FakeEarthaccess:
        @staticmethod
        def login():
            pass

        @staticmethod
        def search_data(**kwargs):
            seen.update(kwargs)
            return []

        @staticmethod
        def download(results, local_path=None, show_progress=None):
            return []

    monkeypatch.setattr(viirs, "earthaccess", FakeEarthaccess)
    viirs.download_viirs(datetime(2023, 6, 15), (-122.55, 37.69, -122.32, 37.81), tmp_path)
    assert seen["short_name"] == "VNP46A2"


def test_real_create_metadata_wrapper_rewrites_the_product(bm_noaa, upstream_restored):
    """Upstream stringifies data_sources straight into a COG tag, so the corrected value
    is what an operator reads back off the published raster."""
    _viirs, bm_export = upstream_restored
    bm_noaa.apply_noaa20_patch()

    metadata = bm_export.create_metadata(
        data_sources={
            "viirs": {"files": ["v.h5"], "sensor": "VIIRS", "product": "VNP46A2"},
            "osm": {"road_types": ["motorway"]},
        },
        bbox=(-122.55, 37.69, -122.32, 37.81),
        crs="EPSG:32610",
        resolution=30.0,
    )
    assert "VJ146A2" in metadata["data_sources"]
    assert "VNP46A2" not in metadata["data_sources"]
    assert metadata["bbox"] == "-122.55,37.69,-122.32,37.81"


def test_self_check_passes_against_the_real_package(bm_noaa, upstream_restored, capsys):
    """Exactly what dps/Dockerfile runs at build time."""
    assert bm_noaa.self_check() == 0
    assert "VJ146A2" in capsys.readouterr().out
