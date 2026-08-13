"""Assertions for dps/blackmarble/bm_noaa.py -- the NOAA-20 (VJ146A2) retarget.

WHAT IS BEING GUARDED
---------------------
Upstream `blackmarble` hardcodes the VIIRS product (`BM_SHORT_NAME = "VNP46A2"` in
blackmarble/acquire/viirs.py) and offers no flag, config field or env var to change it.
bm_noaa.py sets that module constant and then runs upstream's own CLI, so the NOAA-20 DPS
job downloads VJ146A2 instead.

The failure mode that matters is NOT a crash -- it is a NO-OP. If upstream renames the
constant, or moves the HDF5 layer path, a patch that quietly does nothing means the job
downloads Suomi-NPP granules, writes them into the hdnightlightsnoaa20/ folder, bakes a
"VJ146A2 (VIIRS/NOAA-20)" SOURCE tag onto them, publishes to S3, and exits 0. Nothing
downstream can tell. So every test here is ultimately about that: the patch either takes
effect or it fails loudly.

WHY A STUB UPSTREAM
-------------------
CI's pytest job builds its env from dev-conda-deps.txt + .[test], neither of which carries
`blackmarble` (it is pip-installed only into the DPS worker env by dps/environment.yml).
Testing the patch LOGIC against a stub is therefore what makes these assertions run
everywhere. The complementary half -- that the real upstream package still has the shape
the stub imitates -- is test_blackmarble_noaa_upstream_contract.py, which importorskips.
"""
import importlib.util
import os
import sys
import types

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BM_NOAA_PY = os.path.join(REPO_ROOT, "dps", "blackmarble", "bm_noaa.py")

UPSTREAM_LAYER = (
    "HDFEOS/GRIDS/VIIRS_Grid_DNB_2d/Data_Fields/Gap_Filled_DNB_BRDF-Corrected_NTL"
)


def build_stub_blackmarble(short_name="VNP46A2", version="2", layer=UPSTREAM_LAYER):
    """A minimal stand-in for the parts of upstream bm_noaa.py touches.

    Deliberately mirrors upstream's real structure: `download_viirs` reads BM_SHORT_NAME as
    a MODULE GLOBAL at call time (it takes no product argument), and `create_metadata` is
    reached as an attribute of the `blackmarble.export` package -- those two facts are
    exactly what make the patch possible, so the stub must reproduce them.
    """
    root = types.ModuleType("blackmarble")
    acquire = types.ModuleType("blackmarble.acquire")
    viirs = types.ModuleType("blackmarble.acquire.viirs")
    export = types.ModuleType("blackmarble.export")
    cli = types.ModuleType("blackmarble.cli")

    if short_name is not None:
        viirs.BM_SHORT_NAME = short_name
    if version is not None:
        viirs.BM_VERSION = version
    if layer is not None:
        viirs.NTL_DATASET_PATH = layer

    def download_viirs(dt=None, bbox=None, output_dir=None):
        # Reads the globals at CALL time, like upstream does.
        return {"short_name": viirs.BM_SHORT_NAME, "version": viirs.BM_VERSION}

    viirs.download_viirs = download_viirs

    calls = []

    def create_metadata(data_sources=None, *args, **kwargs):
        calls.append({"data_sources": data_sources, "args": args, "kwargs": kwargs})
        return {"data_sources": str(data_sources), **kwargs}

    export.create_metadata = create_metadata
    export.calls = calls

    app_calls = []

    def app():
        app_calls.append({
            "argv": list(sys.argv),
            "short_name": getattr(viirs, "BM_SHORT_NAME", None),
            "version": getattr(viirs, "BM_VERSION", None),
        })

    cli.app = app
    cli.app_calls = app_calls

    acquire.viirs = viirs
    root.acquire = acquire
    root.export = export
    root.cli = cli
    return {
        "blackmarble": root,
        "blackmarble.acquire": acquire,
        "blackmarble.acquire.viirs": viirs,
        "blackmarble.export": export,
        "blackmarble.cli": cli,
    }


def load_bm_noaa():
    """Load bm_noaa.py by path, the way a DPS worker runs it (dps/ is not a package)."""
    spec = importlib.util.spec_from_file_location("bm_noaa_under_test", BM_NOAA_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def stub(monkeypatch):
    """Install a stub upstream into sys.modules for the duration of one test."""
    def _install(**kwargs):
        modules = build_stub_blackmarble(**kwargs)
        for name, module in modules.items():
            monkeypatch.setitem(sys.modules, name, module)
        return types.SimpleNamespace(
            viirs=modules["blackmarble.acquire.viirs"],
            export=modules["blackmarble.export"],
            cli=modules["blackmarble.cli"],
        )
    return _install


@pytest.fixture
def bm_noaa():
    return load_bm_noaa()


# --- the patch itself -------------------------------------------------------------


def test_patch_sets_the_product_to_vj146a2(bm_noaa, stub):
    bm = stub()
    short_name, version = bm_noaa.apply_noaa20_patch()

    assert short_name == "VJ146A2"
    assert bm.viirs.BM_SHORT_NAME == "VJ146A2"
    assert version == "2"


def test_patch_pins_the_version_it_was_verified_against(bm_noaa, stub):
    """VJ146A2's current CMR collection is version 2 -- the same as VNP46A2's.

    Setting it explicitly rather than inheriting upstream's means a future upstream bump to
    a VNP46A2-only version can't silently make every NOAA-20 search return zero granules.
    """
    bm = stub(version="3")
    bm_noaa.apply_noaa20_patch()
    assert bm.viirs.BM_VERSION == "2"


def test_patch_leaves_the_hdf5_layer_path_alone(bm_noaa, stub):
    """VJ146A2 shares VNP46A2's Gap_Filled_DNB_BRDF-Corrected_NTL layer.

    That shared layer is the entire reason a product-name swap is sufficient; rewriting the
    path would be wrong, not merely unnecessary.
    """
    bm = stub()
    bm_noaa.apply_noaa20_patch()
    assert bm.viirs.NTL_DATASET_PATH == UPSTREAM_LAYER


def test_download_reads_the_patched_product_at_call_time(bm_noaa, stub):
    """The mechanism the whole shim rests on.

    Upstream's download_viirs takes no product argument -- it reads the module global when
    it runs. If that ever changed (e.g. the value captured as a default argument), the
    patch would apply and still download Suomi-NPP.
    """
    bm = stub()
    bm_noaa.apply_noaa20_patch()
    assert bm.viirs.download_viirs() == {"short_name": "VJ146A2", "version": "2"}


def test_patch_is_idempotent(bm_noaa, stub):
    """--self-check, the tests, and main() may each apply it; wrappers must not nest."""
    bm = stub()
    bm_noaa.apply_noaa20_patch()
    first = bm.export.create_metadata
    bm_noaa.apply_noaa20_patch()
    bm_noaa.apply_noaa20_patch()

    assert bm.viirs.BM_SHORT_NAME == "VJ146A2"
    assert bm.export.create_metadata is first
    assert bm.export.create_metadata.__wrapped__.__name__ == "create_metadata"


def test_patch_accepts_an_already_patched_module(bm_noaa, stub):
    bm = stub(short_name="VJ146A2")
    assert bm_noaa.apply_noaa20_patch() == ("VJ146A2", "2")


# --- the contract guard -----------------------------------------------------------


def test_missing_short_name_constant_raises(bm_noaa, stub):
    """Upstream renamed it -> the patch would be a silent no-op. Fail instead."""
    stub(short_name=None)
    with pytest.raises(bm_noaa.UpstreamContractError, match="BM_SHORT_NAME no longer exists"):
        bm_noaa.apply_noaa20_patch()


def test_unexpected_short_name_value_raises(bm_noaa, stub):
    """Upstream switched products underneath us -- we no longer know what we are patching."""
    stub(short_name="VNP46A1")
    with pytest.raises(bm_noaa.UpstreamContractError, match="VNP46A1"):
        bm_noaa.apply_noaa20_patch()


def test_missing_version_constant_raises(bm_noaa, stub):
    stub(version=None)
    with pytest.raises(bm_noaa.UpstreamContractError, match="BM_VERSION no longer exists"):
        bm_noaa.apply_noaa20_patch()


def test_changed_layer_path_raises(bm_noaa, stub):
    """If upstream reads a different subdataset, "the products share a layer" is void."""
    stub(layer="HDFEOS/GRIDS/VIIRS_Grid_DNB_2d/Data_Fields/DNB_BRDF-Corrected_NTL")
    with pytest.raises(bm_noaa.UpstreamContractError, match="NTL_DATASET_PATH"):
        bm_noaa.apply_noaa20_patch()


def test_missing_create_metadata_raises(bm_noaa, stub):
    bm = stub()
    del bm.export.create_metadata
    with pytest.raises(bm_noaa.UpstreamContractError, match="create_metadata"):
        bm_noaa.apply_noaa20_patch()


def test_contract_error_message_points_at_the_fix(bm_noaa, stub):
    """A build fails on this message months from now -- it has to say what to do."""
    stub(short_name=None)
    with pytest.raises(bm_noaa.UpstreamContractError) as excinfo:
        bm_noaa.apply_noaa20_patch()
    message = str(excinfo.value)
    assert "veda-black-marble" in message
    assert "bm_noaa.py" in message
    assert "dps/environment.yml" in message


# --- the create_metadata wrapper --------------------------------------------------


def _sources():
    return {
        "landsat": {"files": ["a.tif"], "sensor": "Landsat 8/9", "processing_level": "L2SP"},
        "viirs": {"files": ["v.tif"], "sensor": "VIIRS", "product": "VNP46A2"},
        "osm": {"date_accessed": "2023-06-15", "road_types": ["motorway"]},
    }


def test_metadata_wrapper_rewrites_the_viirs_product(bm_noaa, stub):
    """pipeline.py hardcodes "product": "VNP46A2" in a dict LITERAL, so unlike
    BM_SHORT_NAME it cannot be reassigned -- wrapping create_metadata is the only place it
    can be corrected before rasterio writes it into the COG's data_sources tag."""
    bm = stub()
    bm_noaa.apply_noaa20_patch()
    bm.export.create_metadata(data_sources=_sources())

    viirs_entry = bm.export.calls[0]["data_sources"]["viirs"]
    assert viirs_entry["product"] == "VJ146A2"
    assert viirs_entry["sensor"] == "VIIRS (NOAA-20)"


def test_metadata_wrapper_leaves_other_sources_untouched(bm_noaa, stub):
    bm = stub()
    bm_noaa.apply_noaa20_patch()
    original = _sources()
    bm.export.create_metadata(data_sources=original)

    seen = bm.export.calls[0]["data_sources"]
    assert seen["landsat"] == original["landsat"]
    assert seen["osm"] == original["osm"]
    assert seen["viirs"]["files"] == original["viirs"]["files"]


def test_metadata_wrapper_does_not_mutate_the_callers_dict(bm_noaa, stub):
    """pipeline.py builds the dict inline today, but mutating a caller's argument is the
    kind of side effect that only bites once someone reuses it."""
    bm = stub()
    bm_noaa.apply_noaa20_patch()
    original = _sources()
    bm.export.create_metadata(data_sources=original)
    assert original["viirs"]["product"] == "VNP46A2"


def test_metadata_wrapper_passes_every_other_argument_through(bm_noaa, stub):
    """pipeline.py passes seven kwargs; the wrapper must be transparent to all of them."""
    bm = stub()
    bm_noaa.apply_noaa20_patch()
    bm.export.create_metadata(
        data_sources=_sources(),
        processing_steps=["cloud_masking", "colormap_application"],
        indices_calculated=["ndvi", "ndwi", "ndui"],
        bbox=(-122.55, 37.69, -122.32, 37.81),
        crs="EPSG:32610",
        resolution=30.0,
    )
    kwargs = bm.export.calls[0]["kwargs"]
    assert kwargs["processing_steps"] == ["cloud_masking", "colormap_application"]
    assert kwargs["indices_calculated"] == ["ndvi", "ndwi", "ndui"]
    assert kwargs["bbox"] == (-122.55, 37.69, -122.32, 37.81)
    assert kwargs["crs"] == "EPSG:32610"
    assert kwargs["resolution"] == 30.0


def test_metadata_wrapper_accepts_data_sources_positionally(bm_noaa, stub):
    """data_sources is upstream's first positional parameter; don't assume the keyword."""
    bm = stub()
    bm_noaa.apply_noaa20_patch()
    bm.export.create_metadata(_sources())
    assert bm.export.calls[0]["data_sources"]["viirs"]["product"] == "VJ146A2"


@pytest.mark.parametrize("data_sources", [None, {}, {"landsat": {}}, {"viirs": None}])
def test_metadata_wrapper_tolerates_a_missing_viirs_entry(bm_noaa, stub, data_sources):
    """Never turn a metadata shape we didn't anticipate into a failed activation."""
    bm = stub()
    bm_noaa.apply_noaa20_patch()
    bm.export.create_metadata(data_sources=data_sources)
    assert bm.export.calls[0]["data_sources"] == data_sources


# --- main() / --self-check ---------------------------------------------------------


def test_main_patches_before_running_the_upstream_cli(bm_noaa, stub):
    """Ordering IS the feature: app() invoked before the patch downloads Suomi-NPP."""
    bm = stub()
    assert bm_noaa.main(["--bbox", "-122.55,37.69,-122.32,37.81"]) == 0
    assert bm.cli.app_calls[0]["short_name"] == "VJ146A2"
    assert bm.cli.app_calls[0]["version"] == "2"


def test_main_forwards_argv_to_the_upstream_cli_untouched(bm_noaa, stub, monkeypatch):
    """Every upstream option must keep working -- the shim adds nothing and drops nothing."""
    bm = stub()
    monkeypatch.setattr(sys, "argv", ["bm_noaa.py"])
    args = [
        "--bbox", "-122.55,37.69,-122.32,37.81",
        "--date", "2023-06-15",
        "--config", "fast",
        "--osm-source", "overpass",
        "--output-path", "/tmp/out.tif",
        "--data-dir", "/tmp/data",
        "--log-level", "INFO",
        "--wgs84",
    ]
    bm_noaa.main(args)
    assert bm.cli.app_calls[0]["argv"][1:] == args


def test_self_check_succeeds_without_running_the_pipeline(bm_noaa, stub, capsys):
    """The Dockerfile gate: offline, no token, no network, and it must not start a job."""
    bm = stub()
    assert bm_noaa.main(["--self-check"]) == 0
    assert bm.cli.app_calls == []
    assert "VJ146A2" in capsys.readouterr().out


def test_self_check_survives_extra_arguments(bm_noaa, stub):
    bm = stub()
    assert bm_noaa.main(["--self-check", "--log-level", "DEBUG"]) == 0
    assert bm.cli.app_calls == []


def test_self_check_fails_when_the_contract_moved(bm_noaa, stub, capsys):
    """This is what turns upstream drift into a failed IMAGE BUILD instead of a bad job."""
    stub(short_name=None)
    assert bm_noaa.main(["--self-check"]) == 1
    assert "cannot be applied" in capsys.readouterr().err


def test_self_check_fails_when_blackmarble_is_not_installed(bm_noaa, monkeypatch, capsys):
    for name in list(sys.modules):
        if name == "blackmarble" or name.startswith("blackmarble."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda *a, **k: None, raising=False
    )
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "blackmarble" or name.startswith("blackmarble."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert bm_noaa.main(["--self-check"]) == 1
    assert "not installed" in capsys.readouterr().err


def test_module_constants_match_the_verified_cmr_facts(bm_noaa):
    """Pin the values themselves; they were checked live against CMR, not guessed."""
    assert bm_noaa.VIIRS_SHORT_NAME == "VJ146A2"
    assert bm_noaa.VIIRS_VERSION == "2"
    assert bm_noaa.UPSTREAM_SHORT_NAME == "VNP46A2"
    assert bm_noaa.UPSTREAM_NTL_DATASET_PATH == UPSTREAM_LAYER
