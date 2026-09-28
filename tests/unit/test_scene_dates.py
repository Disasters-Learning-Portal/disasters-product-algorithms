"""
``--date`` failures in the CSDA vendor CLIs must say what went wrong.

An operator ran ``process_capella --date 2026092727161906`` for a scene the
vendor had not delivered yet and got ``ValueError: unconverted data remains:
1906`` from deep inside ``strptime``. Capella, Umbra, Satellogic and SkySat
all resolved ``--date`` the same way (``strptime`` + closest scene, however far
away), so all four now go through ``shared_utils.scene_dates``:

- a malformed ``--date`` raises a ValueError that names the expected format;
- the closest scene within ``DATE_TOLERANCE`` is used (and the substitution is
  printed); nothing within it raises a FileNotFoundError that says the scene
  may not be delivered yet and lists the nearest dates -- previously a
  different acquisition was silently processed;
- an empty bucket raises a clear error instead of ``min() arg is an empty
  sequence``, and unparseable folders are skipped instead of crashing;
- each CLI prints ``ERROR: ...`` and exits 1 instead of a traceback.
"""

from datetime import datetime, timedelta

import pytest

pytest.importorskip("osgeo.gdal")
pytest.importorskip("rasterio")
pytest.importorskip("scipy")

from shared_utils.scene_dates import DATE_TOLERANCE, parse_date_arg, select_scene_date

SCENE_TIME = datetime(2026, 4, 18, 19, 33, 5)
OTHER_TIME = datetime(2026, 4, 15, 22, 57, 47)


# ------------------------------------------------------------ shared helper --

@pytest.mark.parametrize("value, fmt", [
    ("2026092727161906", "%Y%m%d%H%M%S"),   # the field report: 16 digits, hour 27
    ("2026041819330", "%Y%m%d%H%M%S"),      # 13 digits: strptime alone accepts it
    ("20260418", "%Y%m%d%H%M%S"),
    ("20261318193305", "%Y%m%d%H%M%S"),     # month 13
    ("2026-4-18 19:33:05", "%Y-%m-%d %H:%M:%S"),  # not zero-padded
    ("20260418193305", "%Y-%m-%d %H:%M:%S"),      # wrong sensor's format
    ("2026-04-18T19:33:05", "%Y-%m-%d %H:%M:%S"),
])
def test_parse_rejects_malformed_with_format_named(value, fmt):
    with pytest.raises(ValueError) as exc:
        parse_date_arg(value, fmt, "example", "Sensor")
    msg = str(exc.value)
    assert repr(value) in msg
    assert "unconverted data" not in msg
    assert ("YYYYMMDDHHMMSS" if "%Y%m" in fmt else "YYYY-MM-DD HH:MM:SS") in msg


def test_select_exact_and_within_tolerance(capsys):
    avail = [SCENE_TIME, OTHER_TIME]
    assert select_scene_date(SCENE_TIME, avail, "X scene", "s3://b/p/", "%Y%m%d%H%M%S") == SCENE_TIME
    assert capsys.readouterr().out == ""  # exact match: nothing to report

    near = SCENE_TIME + timedelta(minutes=2)
    assert select_scene_date(near, avail, "X scene", "s3://b/p/", "%Y%m%d%H%M%S") == SCENE_TIME
    assert "using the closest, 20260418193305" in capsys.readouterr().out


def test_select_tolerance_boundary():
    avail = [SCENE_TIME]
    edge = SCENE_TIME - DATE_TOLERANCE
    assert select_scene_date(edge, avail, "X scene", "w", "%Y%m%d%H%M%S") == SCENE_TIME
    with pytest.raises(FileNotFoundError, match="No X scene within"):
        select_scene_date(edge - timedelta(seconds=1), avail, "X scene", "w", "%Y%m%d%H%M%S")


def test_select_outside_tolerance_lists_nearest_in_date_format():
    with pytest.raises(FileNotFoundError) as exc:
        select_scene_date(datetime(2026, 9, 27, 16, 19, 6), [SCENE_TIME, OTHER_TIME],
                          "X scene", "s3://b/p/", "%Y-%m-%d %H:%M:%S", hint="Try list-dates.")
    msg = str(exc.value)
    assert "--date 2026-09-27 16:19:06" in msg
    assert "not have delivered it yet" in msg and "s3://b/p/" in msg
    assert "Try list-dates." in msg
    # printed in the --date format so they can be pasted straight back
    assert "2026-04-18 19:33:05" in msg and "2026-04-15 22:57:47" in msg


def test_select_empty_bucket():
    with pytest.raises(FileNotFoundError, match="No X scenes found under s3://b/p/"):
        select_scene_date(SCENE_TIME, [], "X scene", "s3://b/p/", "%Y%m%d%H%M%S")


# ---------------------------------------------------------------- sensors --
# One row per sensor: its module, retrieve call, a bucket listing holding one
# scene at SCENE_TIME plus an unparseable folder, and the --date format.

def _capella():
    from capella import capella_v2 as mod
    s = "CAPELLA_C05_SP_GEO_HH_20260418193305_20260418193315"
    keys = [f"disasters/{s}/{s}.tif", "disasters/CAPELLA_BAD/x.tif"]
    return dict(mod=mod, keys=keys, fmt="%Y%m%d%H%M%S",
                call=lambda d: mod.retrieve_capella_resources(d, "bkt", "disasters"),
                expect=f"s3://bkt/disasters/{s}/{s}.tif")


def _umbra():
    from umbra import umbra_v2 as mod
    s = "2026-04-18-19-33-05_UMBRA-05"
    keys = [f"disasters/x/{s}/{s}_GEC.tif", "disasters/x/notadate/y.tif"]
    return dict(mod=mod, keys=keys, fmt="%Y-%m-%d %H:%M:%S",
                call=lambda d: mod.retrieve_umbra_resources(d, "bkt", "disasters"),
                expect=f"s3://bkt/disasters/x/{s}/{s}_GEC.tif")


def _satellogic():
    from satellogic import satellogic_v2 as mod
    s = "20260418_193305_SATL_L1D_29"
    keys = [f"disasters/{s}/rasters/a_TOA_0.tif", "disasters/notadate_scene_L1D_00/x.tif"]
    return dict(mod=mod, keys=keys, fmt="%Y-%m-%d %H:%M:%S",
                call=lambda d: mod.retrieve_satellogic_resources(d, "L1D", "bkt", "disasters")[1],
                expect=f"s3://bkt/disasters/{s}/rasters/a_TOA_0.tif")


def _skysat():
    from skysat import skysat_v2 as mod
    f = "20260418_193305_ssc2_u0001_analytic.tif"
    keys = [f"disasters/event/collect_a/{f}", "disasters/event/junk/readme_x.tif"]
    return dict(mod=mod, keys=keys, fmt="%Y-%m-%d %H:%M:%S",
                call=lambda d: mod.retrieve_skysat_resources(d, "bkt", "disasters"),
                expect=f"s3://bkt/disasters/event/collect_a/{f}")


SENSORS = {"capella": _capella, "umbra": _umbra, "satellogic": _satellogic, "skysat": _skysat}


@pytest.fixture(params=sorted(SENSORS))
def sensor(request, monkeypatch):
    cfg = SENSORS[request.param]()
    monkeypatch.setattr(cfg["mod"], "retrieve_s3_file_list", lambda b, p: cfg["keys"])
    cfg["name"] = request.param
    return cfg


def test_sensor_exact_date_resolves(sensor):
    assert sensor["call"](SCENE_TIME.strftime(sensor["fmt"])) == [sensor["expect"]]


def test_sensor_near_date_resolves_to_closest(sensor):
    near = (SCENE_TIME + timedelta(minutes=3)).strftime(sensor["fmt"])
    assert sensor["call"](near) == [sensor["expect"]]


def test_sensor_undelivered_date_raises(sensor):
    later = datetime(2026, 9, 27, 16, 19, 6).strftime(sensor["fmt"])
    with pytest.raises(FileNotFoundError, match="not have delivered it yet"):
        sensor["call"](later)


def test_sensor_malformed_date_raises(sensor):
    with pytest.raises(ValueError, match="is not a valid"):
        sensor["call"]("2026092727161906")


def test_sensor_empty_bucket_raises(sensor, monkeypatch):
    monkeypatch.setattr(sensor["mod"], "retrieve_s3_file_list", lambda b, p: [])
    with pytest.raises(FileNotFoundError, match="scenes found under"):
        sensor["call"](SCENE_TIME.strftime(sensor["fmt"]))


# -------------------------------------------------------------------- CLIs --

CLI_ARGV = {
    "capella": ("capella.process_capella", ["--date", "20260927161906"]),
    "umbra": ("umbra.process_umbra", ["--product", "sigma", "--date", "2026-09-27 16:19:06"]),
    # a good date followed by an undelivered one: must fail before processing any
    "satellogic": ("satellogic.process_satellogic",
                   ["--product", "truecolor", "--level", "L1D",
                    "--date", "2026-04-18 19:33:05,2026-09-27 16:19:06"]),
    "skysat": ("skysat.process_skysat", ["--product", "ndvi", "--date", "2026-09-27 16:19:06"]),
}


def test_cli_exits_with_message_not_traceback(sensor, monkeypatch, tmp_path):
    import importlib

    module, argv = CLI_ARGV[sensor["name"]]
    cli = importlib.import_module(module)
    meta = tmp_path / "meta.json"
    meta.write_text('{"ACTIVATION_EVENT": "202609_Flood_TX"}')
    monkeypatch.setattr("sys.argv", [module, *argv, "--output", str(tmp_path / "out"),
                                     "--metadata-json", str(meta)])
    # anything past resolution would process scenes: make that loud
    for name in ("group_capella_scenes", "group_umbra_scenes", "group_satellogic_tifs",
                 "calc_ndvi"):
        if hasattr(cli, name):
            monkeypatch.setattr(cli, name, lambda *a, **k: pytest.fail("processed a scene"))

    with pytest.raises(SystemExit) as exc:
        cli.main()
    # sys.exit(str) -> exit status 1 with the message on stderr
    assert str(exc.value.code).startswith("ERROR: No ")
    assert "within 0:05:00" in str(exc.value.code)
