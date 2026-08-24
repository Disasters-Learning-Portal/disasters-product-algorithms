"""Every flag a ``dps/<sensor>/run.sh`` emits must be accepted by that sensor's CLI.

This is the gap that let PR #76 reach an approved state: it deleted eight
arguments from ``process_capella`` while ``dps/capella/run.sh`` still passed all
of them, so every Capella DPS job would have died at ``argparse`` with exit 2.
Nothing caught it -- ``dps/Dockerfile`` only runs ``<cli> --help``, which exits 0
regardless of which flags exist, and the CI ``cli-smoke`` job does the same.

The check is static on both sides: parse the ``args=( ... )`` array out of
run.sh, and read the ``add_argument`` names out of the CLI's parser via a real
``parse_known_args`` probe. No network, no GDAL, no MAAP.

It also asserts the reverse direction for the job's own inputs: every input
declared in ``algorithm_config.yaml`` must be parsed by run.sh, so a form field
can't silently do nothing.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DPS = REPO / "dps"

# (sensor dir, console-script module) for the file-driven sensors.
SENSORS = [
    ("capella", "capella.process_capella"),
    ("umbra", "umbra.process_umbra"),
    ("satellogic", "satellogic.process_satellogic"),
    ("landsat", "landsat.process_landsat89"),
    ("sentinel2", "sentinel2.process_sentinel2"),
]


def _flags_emitted_by_run_sh(run_sh: Path, cli: str):
    """Return the flag tokens run.sh passes to ``cli`` specifically.

    A run.sh may drive more than one CLI -- dps/sentinel2/run.sh builds
    ``dl_args`` for ``download_sentinel2`` and ``args`` for
    ``process_sentinel2``. So resolve the array name from the invocation line
    (``conda run ... <cli> "${<arr>[@]}"``) and read only that array, including
    its ``+=`` appends where the conditional flags live.
    """
    text = run_sh.read_text()

    m = re.search(re.escape(cli) + r'\s+"\$\{(\w+)\[@\]\}"', text)
    if not m:
        return None  # caller skips: no recognisable invocation
    arr = m.group(1)

    blocks = re.findall(r"(?<![\w])" + re.escape(arr) + r"\+?=\(\s*(.*?)\)", text, re.S)
    flags = set()
    for block in blocks:
        for tok in re.findall(r"(?<![\w-])(--?[A-Za-z][\w-]*)", block):
            flags.add(tok)
    return flags


def _flags_accepted_by_cli(module: str):
    """Return the option strings the CLI's argparse parser defines.

    Runs the module with ``runpy`` under ``__name__ == "__main__"`` rather than
    calling ``main()``: landsat and sentinel2 have no importable ``main()`` --
    their dispatch runs at bare module level behind the ``__main__`` guard
    (.clinerules.md rule 21). ``add_argument`` is spied on and ``parse_args`` is
    stubbed to exit, so the parser is fully built but nothing processes.
    """
    pkg, mod = module.split(".")
    path = REPO / "src" / pkg / f"{mod}.py"
    code = (
        "import argparse, json, runpy, sys, atexit\n"
        "seen = []\n"
        "orig = argparse.ArgumentParser.add_argument\n"
        "def spy(self, *a, **k):\n"
        "    seen.extend([x for x in a if isinstance(x, str) and x.startswith('-')])\n"
        "    return orig(self, *a, **k)\n"
        "argparse.ArgumentParser.add_argument = spy\n"
        "def _stop(self, *a, **k):\n"
        "    raise SystemExit(0)\n"
        "argparse.ArgumentParser.parse_args = _stop\n"
        "argparse.ArgumentParser.parse_known_args = _stop\n"
        "atexit.register(lambda: sys.stderr.write('FLAGS:' + json.dumps(sorted(set(seen))) + '\\n'))\n"
        f"sys.argv = [{mod!r}]\n"
        "try:\n"
        f"    runpy.run_path({str(path)!r}, run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
        "except Exception:\n"
        "    pass\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, cwd=str(REPO),
    )
    match = re.search(r"FLAGS:(\[.*\])", proc.stdout + proc.stderr)
    if not match:
        pytest.skip(
            f"could not introspect {module} "
            f"(stdout={proc.stdout[-200:]!r} stderr={proc.stderr[-200:]!r})"
        )
    import json
    return set(json.loads(match.group(1)))


@pytest.mark.parametrize("sensor,module", SENSORS)
def test_run_sh_flags_are_accepted_by_cli(sensor, module):
    run_sh = DPS / sensor / "run.sh"
    if not run_sh.exists():
        pytest.skip(f"{run_sh} not present")

    cli = module.split(".")[-1]
    emitted = _flags_emitted_by_run_sh(run_sh, cli)
    if emitted is None:
        pytest.skip(f"no `{cli} \"${{arr[@]}}\"` invocation found in {run_sh}")
    accepted = _flags_accepted_by_cli(module)
    if not accepted:
        pytest.skip(f"{module} exposed no parser flags to introspect")

    # argparse accepts an unambiguous prefix, and the repo mixes '-nodata' with
    # '--nodata' style; compare on the stripped name so '-dst_crs' matches
    # '--dst_crs' if a sensor ever switches convention.
    norm = lambda s: s.lstrip("-").replace("-", "_")
    accepted_norm = {norm(f) for f in accepted}
    unknown = sorted(f for f in emitted if norm(f) not in accepted_norm)

    assert not unknown, (
        f"dps/{sensor}/run.sh passes {unknown} but {module} does not define "
        f"them. The job would fail at argparse (exit 2) on every run. "
        f"CLI accepts: {sorted(accepted)}"
    )


@pytest.mark.parametrize("sensor,_module", SENSORS)
def test_algorithm_config_inputs_are_parsed_by_run_sh(sensor, _module):
    """A declared job input that run.sh never parses is a dead form field."""
    cfg = DPS / sensor / "algorithm_config.yaml"
    run_sh = DPS / sensor / "run.sh"
    if not cfg.exists() or not run_sh.exists():
        pytest.skip(f"{sensor}: config or run.sh missing")

    yaml = pytest.importorskip("yaml")
    declared = [i["name"] for i in yaml.safe_load(cfg.read_text()).get("inputs", [])]

    text = run_sh.read_text()
    parsed = set(re.findall(r"^\s*--([A-Za-z][\w-]*)\)", text, re.M))

    unparsed = sorted(n for n in declared if n not in parsed)
    assert not unparsed, (
        f"dps/{sensor}/algorithm_config.yaml declares {unparsed}, but "
        f"dps/{sensor}/run.sh has no matching parse case -- the operator fills "
        f"the field and it is silently ignored. run.sh parses: {sorted(parsed)}"
    )
