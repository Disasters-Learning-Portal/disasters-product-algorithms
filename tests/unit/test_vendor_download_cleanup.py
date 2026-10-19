"""Raw vendor downloads must not outlive the run that fetched them.

Operators running capella/skysat (and umbra/satellogic) workflows watched /tmp
fill up run after run: every CLI downloaded its raw scenes into a shared,
hardcoded ``/tmp/s3_temp`` that nothing ever deleted (capella removed its raw
file only after a SUCCESSFUL COG; umbra, skysat and satellogic never did).

Pinned here, per sensor:

1. **CLI**: a run downloads into a fresh per-run directory
   (``shared_utils.scratch.download_scratch``) and that directory is gone
   afterwards -- on success, on an exception, and on Ctrl-C.
2. **Library**: the processing function actually downloads into the
   ``download_dir`` it was handed (a CLI that creates a scratch dir the library
   then ignores would clean up an empty folder while /tmp keeps growing).
3. **Static**: no vendor module hardcodes ``/tmp/s3_temp`` outside a default
   parameter, and every vendor CLI uses ``download_scratch``.
"""

import ast
import os
import sys
import tempfile

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
SRC = os.path.join(REPO_ROOT, "src")
SENSORS = ("capella", "umbra", "skysat", "satellogic")


class _Boom(RuntimeError):
    pass


def _fake_download(record, fail):
    """A product function stand-in: drop a 'raw scene' where it was told to."""

    def fake(*args, download_dir=None, **kwargs):
        assert download_dir is not None, "CLI did not pass download_dir"
        record.append(download_dir)
        with open(os.path.join(download_dir, "RAW_SCENE.tif"), "wb") as fh:
            fh.write(b"\0" * 1024)
        if fail == "error":
            raise _Boom("processing failed")
        if fail == "interrupt":
            raise KeyboardInterrupt
        return None

    return fake


def _run_cli(sensor, monkeypatch, tmp_path, fail):
    """Run the sensor's real ``main()`` with S3 and the science stubbed out."""
    record = []
    fake = _fake_download(record, fail)
    out = str(tmp_path / "out")

    if sensor == "capella":
        from capella import process_capella as mod
        monkeypatch.setattr(mod, "retrieve_capella_resources",
                            lambda **kw: ["s3://b/p/CAPELLA_X_GEO_HH_1.tif"])
        monkeypatch.setattr(mod, "group_capella_scenes", lambda tifs: [tifs])

        def sigma(*a, **kw):
            fake(*a, **kw)
            return None, None
        monkeypatch.setattr(mod, "sigmaCalib", sigma)
        # --metadata-json is always passed by the notebooks and DPS; capella's
        # CLI requires it in practice (it setdefault()s into the loaded dict).
        meta = tmp_path / "meta.json"
        meta.write_text('{"ACTIVATION_EVENT": "202604_Flood_TX"}')
        argv = ["process_capella", "--date", "20260418193305", "--output", out,
                "--metadata-json", str(meta)]
    elif sensor == "umbra":
        from umbra import process_umbra as mod
        monkeypatch.setattr(mod, "retrieve_umbra_resources",
                            lambda **kw: ["s3://b/p/x_GEC.tif"])
        monkeypatch.setattr(mod, "group_umbra_scenes", lambda tifs: [tifs])
        monkeypatch.setattr(mod, "sigmaCalib", fake)
        argv = ["process_umbra", "--product", "sigma",
                "--date", "2026-08-05 03:54:47", "--output", out]
    elif sensor == "skysat":
        from skysat import process_skysat as mod
        monkeypatch.setattr(mod, "retrieve_skysat_resources",
                            lambda *a, **kw: ["s3://b/p/x_analytic.tif"])
        monkeypatch.setattr(mod, "calc_ndvi", fake)
        argv = ["process_skysat", "--product", "ndvi",
                "--date", "2026-07-14 12:00:00", "--output", out]
    elif sensor == "satellogic":
        from satellogic import process_satellogic as mod
        monkeypatch.setattr(mod, "retrieve_satellogic_resources",
                            lambda *a, **kw: ({}, ["s3://b/p/x_analytic.tif"]))
        monkeypatch.setattr(mod, "group_satellogic_tifs", lambda tifs: [tifs])
        monkeypatch.setattr(mod, "genNDVI", fake)
        argv = ["process_satellogic", "--product", "ndvi", "--level", "L1D",
                "--date", "2026-07-14 12:00:00", "--output", out]
    else:  # pragma: no cover
        raise AssertionError(sensor)

    monkeypatch.setattr(sys, "argv", argv)
    return mod.main, record


@pytest.mark.parametrize("sensor", SENSORS)
@pytest.mark.parametrize("fail", [None, "error", "interrupt"])
def test_cli_download_dir_is_removed(sensor, fail, monkeypatch, tmp_path):
    main, record = _run_cli(sensor, monkeypatch, tmp_path, fail)

    if fail == "error":
        with pytest.raises(_Boom):
            main()
    elif fail == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            main()
    else:
        main()

    assert len(record) == 1, "the product function was never reached"
    download_dir = record[0]
    # A fresh per-run dir under the system temp dir -- never the shared cache.
    assert os.path.realpath(download_dir) != os.path.realpath("/tmp/s3_temp")
    assert os.path.basename(download_dir).startswith(f"{sensor}_dl_")
    assert os.path.dirname(os.path.realpath(download_dir)) == \
        os.path.realpath(tempfile.gettempdir())
    assert not os.path.exists(download_dir), (
        f"{sensor}: raw download dir {download_dir} survived the run "
        f"(fail={fail}) -- this is exactly the /tmp leak"
    )


# --------------------------------------------------------------------------
# Library: each processing function downloads into the download_dir it gets.
# --------------------------------------------------------------------------

class _Downloaded(Exception):
    """Raised by the fake downloader to stop right after the download."""


def _recording_downloader(record):
    def fake(s3path, save_location="/tmp/s3_temp", *a, **kw):
        record.append(save_location)
        raise _Downloaded
    return fake


def _library_call(sensor, monkeypatch, tmp_path, download_dir):
    record = []
    dl = _recording_downloader(record)
    save = str(tmp_path / "out")

    if sensor == "capella":
        from capella import capella_v2 as m
        monkeypatch.setattr(m, "download_s3_file", dl)
        call = lambda: m.sigmaCalib(["s3://b/p/CAPELLA_X_GEO_HH_1.tif"],
                                    save_location=save, download_dir=download_dir)
    elif sensor == "umbra":
        from umbra import umbra_v2 as m
        monkeypatch.setattr(m, "download_s3_file", dl)
        call = lambda: m.sigmaCalib(["s3://b/p/x_GEC.tif"], save,
                                    download_dir=download_dir)
    elif sensor == "skysat":
        from skysat import skysat_v2 as m
        monkeypatch.setattr(m, "download_s3_file", dl)
        paths = ["s3://b/p/20260714_120000_ssc5_u0001_analytic.tif"]
        monkeypatch.setattr(m, "get_skysat_product_files", lambda p, t: paths)
        call = lambda: m.calc_ndvi(paths, save, download_dir=download_dir)
    elif sensor == "satellogic":
        from satellogic import satellogic_v2 as m
        monkeypatch.setattr(m, "download_s3_file", dl)
        monkeypatch.setattr(m, "infer_processing_level", lambda p: "L1D")
        monkeypatch.setattr(m, "getScaleFactor", lambda meta: 1.0)
        monkeypatch.setattr(m, "getSolarZenithAngle", lambda meta: 0.0)
        call = lambda: m.genNDVI(["s3://b/p/x_TOA_0.tif"], {}, save,
                                 download_dir=download_dir)
    else:  # pragma: no cover
        raise AssertionError(sensor)
    return call, record


@pytest.mark.parametrize("sensor", SENSORS)
def test_library_downloads_into_given_dir(sensor, monkeypatch, tmp_path):
    download_dir = str(tmp_path / "scratch")
    os.makedirs(download_dir)
    call, record = _library_call(sensor, monkeypatch, tmp_path, download_dir)
    with pytest.raises(_Downloaded):
        call()
    assert record == [download_dir], (
        f"{sensor} downloaded into {record}, not the download_dir it was given"
    )


# --------------------------------------------------------------------------
# Static guards.
# --------------------------------------------------------------------------

def _py_files(sensor):
    d = os.path.join(SRC, sensor)
    return [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".py")]


@pytest.mark.parametrize("sensor", SENSORS)
def test_no_hardcoded_s3_temp_outside_defaults(sensor):
    """`/tmp/s3_temp` may appear only as a parameter/argparse default."""
    offenders = []
    for path in _py_files(sensor):
        tree = ast.parse(open(path, encoding="utf-8").read())
        allowed = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in node.args.defaults + node.args.kw_defaults:
                    if d is not None:
                        allowed.add(id(d))
            if isinstance(node, ast.keyword) and node.arg == "default":
                allowed.add(id(node.value))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and "/tmp/s3_temp" in node.value and id(node) not in allowed):
                offenders.append(f"{os.path.relpath(path, REPO_ROOT)}:{node.lineno}")
            if isinstance(node, ast.JoinedStr):
                text = "".join(v.value for v in node.values
                               if isinstance(v, ast.Constant) and isinstance(v.value, str))
                if "/tmp/s3_temp" in text:
                    offenders.append(f"{os.path.relpath(path, REPO_ROOT)}:{node.lineno}")
    assert not offenders, (
        "Hardcoded /tmp/s3_temp (a shared dir nothing cleans) -- use the "
        f"download_dir parameter instead: {offenders}"
    )


@pytest.mark.parametrize("sensor", SENSORS)
def test_cli_uses_download_scratch(sensor):
    src = open(os.path.join(SRC, sensor, f"process_{sensor}.py"), encoding="utf-8").read()
    assert "download_scratch(" in src, f"process_{sensor}.py has no download_scratch"
