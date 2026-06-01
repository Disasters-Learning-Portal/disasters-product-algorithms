"""
Argparse pinning tests for every sensor CLI.

Each `process_*` console script MUST expose `--metadata-json` so notebooks
can dump ACTIVATION_METADATA to a temp JSON file and forward it. Without
this we'd silently lose the activation tags on whichever CLI drops the
flag.

These tests run each CLI with `--help`, which exits with SystemExit(0)
after printing the parser help text to stdout — we capture and assert.

Patterns differ by CLI:
  * capella/satellogic/sentinel2/umbra: argparse runs inside `main()` —
    we invoke `process_<name>.main()` directly.
  * landsat: argparse runs at module level under `if __name__ == "__main__"` —
    we invoke `landsat.cli.process_landsat89_cli` which execs the script
    with `__name__ == '__main__'`.

NOTE on sentinel2: it has argparse at MODULE level (not inside a function),
so we go through the cli shim too — the script gets exec'd fresh each call
which re-runs argparse.
"""

import sys

import pytest


# These imports walk through shared_utils → cog_metadata → osgeo.gdal,
# so skip gracefully on dev machines without GDAL bindings.
pytest.importorskip("osgeo.gdal")
pytest.importorskip("rasterio")


# Parametrize ID -> (invocation_kind, target).
#   "main": call <module>.main() directly
#   "shim": call the cli-shim function (execs the script as __main__)
SENSOR_CLIS = [
    pytest.param(
        "process_capella",
        "main",
        "capella.process_capella",
        id="capella",
    ),
    pytest.param(
        "process_satellogic",
        "main",
        "satellogic.process_satellogic",
        id="satellogic",
    ),
    pytest.param(
        "process_umbra",
        "main",
        "umbra.process_umbra",
        id="umbra",
    ),
    pytest.param(
        "process_sentinel2",
        "shim",
        "sentinel2.cli:process_sentinel2_cli",
        id="sentinel2",
    ),
    pytest.param(
        "process_landsat89",
        "shim",
        "landsat.cli:process_landsat89_cli",
        id="landsat",
    ),
]


@pytest.mark.parametrize("argv0,kind,target", SENSOR_CLIS)
def test_sensor_cli_exposes_metadata_json_flag(argv0, kind, target, capsys, monkeypatch):
    """Every sensor CLI must accept --metadata-json. Run --help and grep."""
    # sys.argv[0] is the program name argparse prints in usage strings.
    monkeypatch.setattr(sys, "argv", [argv0, "--help"])

    if kind == "main":
        module_path = target
        mod = __import__(module_path, fromlist=["main"])
        entry = getattr(mod, "main")
    elif kind == "shim":
        mod_path, func_name = target.split(":")
        mod = __import__(mod_path, fromlist=[func_name])
        entry = getattr(mod, func_name)
    else:
        raise AssertionError(f"unknown kind: {kind}")

    # argparse's --help calls sys.exit(0).
    with pytest.raises(SystemExit) as exc_info:
        entry()
    # Expect a clean exit (0). Any non-zero would mean argparse rejected
    # something or the script crashed before reaching parse_args.
    assert exc_info.value.code == 0, (
        f"{argv0} --help exited non-zero (code={exc_info.value.code})"
    )

    out = capsys.readouterr().out
    assert "--metadata-json" in out, (
        f"{argv0} --help output is missing --metadata-json flag.\n"
        f"---help-output---\n{out}\n----------------"
    )
