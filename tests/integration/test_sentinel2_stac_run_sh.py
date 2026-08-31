"""Executes the real dps/sentinel2_stac/run.sh end to end, with conda stubbed.

WHY THIS EXISTS
---------------
run.sh is where the STAC Sentinel-2 job actually lives: it validates the inputs,
loops the singular --product flag, converts the comma bbox into four CLI args,
writes the activation metadata, decides which COGs are products and which are
scratch, and hands off to _finalize.sh. None of that is covered by unit tests of
the pieces, and CI's `<cli> --help` smoke check does not execute any of it -- a
broken orchestration only shows up as a failed, or silently wrong, DPS job.

The one thing stubbed is `conda`, because the real invocation needs the
disasters_dps env and a multi-gigabyte read from Earth Search. The stub sits
first on PATH, records the argv of every `process_sentinel2_stac` call, and
writes tiny real GeoTIFFs where the CLI would. Everything else -- the
validators, the product loop, the bbox conversion, the publish filter, the
output/ copy -- is the shipped code.

Mirrors tests/integration/test_blackmarble_run_sh.py, which does the same for
Black Marble.
"""
import json
import os
import shutil
import stat
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUN_SH = os.path.join(REPO_ROOT, "dps", "sentinel2_stac", "run.sh")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")

EVENT = "202601_KyleWx_US"
BBOX = "-93.70,42.00,-93.55,42.10"
START = "2025-08-12"
END = "2025-08-14"


# `conda run [--live-stream] --name <env> process_sentinel2_stac <args...>`
# Records the args, then emulates the CLI by touching a file per product at
# --output. Also emulates the water-extent reference cache, so the publish
# filter is exercised against something real.
CONDA_STUB = r'''#!/usr/bin/env bash
set -uo pipefail

[[ "${1:-}" == "run" ]] || { echo "stub: unexpected conda subcommand $*" >&2; exit 64; }
shift
while [[ $# -gt 0 ]]; do
  case "$1" in
    --live-stream) shift ;;
    --name) shift 2 ;;
    *) break ;;
  esac
done

cmd="${1:-}"; shift || true
[[ "${cmd}" == "process_sentinel2_stac" ]] || { echo "stub: unexpected command ${cmd}" >&2; exit 64; }

# Record this invocation's argv, one JSON array per line.
"${TEST_PYTHON}" -c '
import json, sys
with open(sys.argv[1], "a") as fh:
    fh.write(json.dumps(sys.argv[2:]) + "\n")
' "${INVOCATION_LOG}" "$@"

# Pull out --product and --output so we can emulate the CLI writing there.
product=""; outdir=""
prev=""
for a in "$@"; do
  case "${prev}" in
    --product) product="$a" ;;
    --output)  outdir="$a" ;;
  esac
  prev="$a"
done

mkdir -p "${outdir}"
: > "${outdir}/S2A_MSIL2A_${product}_T15TVG_2025-08-13T17:12:26Z.tif"

# The water-extent path caches its land-cover reference under the output dir.
# These are NOT products and must never be published.
if [[ "${product}" == "we" ]]; then
  mkdir -p "${outdir}/water_extent_reference"
  : > "${outdir}/water_extent_reference/WorldCover_2021_T15TVG.tif"
  : > "${outdir}/water_extent_reference/CDL_2024_30m.tif"
fi
exit 0
'''


class Run:
    def __init__(self, proc, home, cwd, invocation_log, event):
        self.proc = proc
        self.home = home
        self.cwd = cwd
        self._log = invocation_log
        self.event = event

    @property
    def ok(self):
        return self.proc.returncode == 0

    @property
    def output(self):
        return self.proc.stdout + self.proc.stderr

    def invocations(self):
        if not os.path.exists(self._log):
            return []
        with open(self._log) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def flag(self, argv, name):
        """All values following `name` in one argv list."""
        return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == name]

    def out_home_files(self):
        d = os.path.join(self.home, "drcs_outputs", self.event)
        if not os.path.isdir(d):
            return []
        return sorted(os.listdir(d))

    def output_dir_files(self):
        d = os.path.join(self.cwd, "output")
        if not os.path.isdir(d):
            return []
        return sorted(
            os.path.relpath(os.path.join(root, f), d)
            for root, _, files in os.walk(d)
            for f in files
        )


@pytest.fixture
def run_job(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    conda = bindir / "conda"
    conda.write_text(CONDA_STUB)
    conda.chmod(conda.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    invocation_log = tmp_path / "invocations.jsonl"

    def _run(**inputs):
        args = []
        for name, value in inputs.items():
            args.append(f"--{name}")
            if value is not None:
                args.append(str(value))

        environ = dict(os.environ)
        environ["PATH"] = f"{bindir}:{environ['PATH']}"
        environ.update({
            "HOME": str(home),
            "TEST_PYTHON": sys.executable,
            "INVOCATION_LOG": str(invocation_log),
            # _finalize.sh test hook: copy to output/ but skip the S3 publish
            # and the scratch delete, so products survive for inspection.
            "DPS_DRY_RUN": "1",
        })

        proc = subprocess.run(
            ["bash", str(RUN_SH), *args],
            cwd=str(cwd), env=environ, capture_output=True, text=True, timeout=300,
        )
        return Run(proc, str(home), str(cwd), str(invocation_log),
                   inputs.get("activation_event", EVENT))

    return _run


def defaults(**overrides):
    base = {
        "activation_event": EVENT,
        "bbox": BBOX,
        "start_date": START,
        "end_date": END,
        "level": "2",
        "products": "true_color ndvi",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------

class TestHappyPath:

    def test_job_succeeds_and_publishes_one_cog_per_product(self, run_job):
        result = run_job(**defaults())
        assert result.ok, result.output

        assert len(result.invocations()) == 2, (
            "expected one CLI call per product"
        )
        assert result.out_home_files() == [
            "S2A_MSIL2A_ndvi_T15TVG_2025-08-13T17:12:26Z.tif",
            "S2A_MSIL2A_true_color_T15TVG_2025-08-13T17:12:26Z.tif",
        ]
        assert result.output_dir_files() == result.out_home_files()

    def test_product_flag_is_singular_and_looped(self, run_job):
        """The STAC CLI takes exactly one --product, unlike the .SAFE CLI's -p list."""
        result = run_job(**defaults(products="ndvi evi we"))
        assert result.ok, result.output

        products = [result.flag(argv, "--product")[0] for argv in result.invocations()]
        assert products == ["ndvi", "evi", "we"]
        for argv in result.invocations():
            assert len(result.flag(argv, "--product")) == 1

    def test_bbox_commas_become_four_separate_cli_args(self, run_job):
        """--bbox takes 4 floats; the DPS input is one comma string."""
        result = run_job(**defaults(products="ndvi"))
        assert result.ok, result.output

        argv = result.invocations()[0]
        i = argv.index("--bbox")
        assert argv[i + 1:i + 5] == ["-93.70", "42.00", "-93.55", "42.10"]

    def test_activation_metadata_is_passed_and_well_formed(self, run_job):
        result = run_job(**defaults(products="ndvi"))
        assert result.ok, result.output
        assert result.flag(result.invocations()[0], "--metadata-json")

    def test_event_is_not_in_the_filename(self, run_job):
        """The STAC pipeline keeps the event in the tags + S3 prefix only."""
        result = run_job(**defaults(products="ndvi"))
        assert result.ok, result.output
        assert all(EVENT not in f for f in result.out_home_files())

    def test_a_space_separated_bbox_also_works(self, run_job):
        result = run_job(**defaults(products="ndvi",
                                    bbox="-93.70 42.00 -93.55 42.10"))
        assert result.ok, result.output

    def test_quoted_values_are_tolerated(self, run_job):
        """Operators paste examples including the surrounding quotes."""
        result = run_job(**defaults(products='"ndvi evi"'))
        assert result.ok, result.output
        assert len(result.invocations()) == 2


# ---------------------------------------------------------------------
# The publish filter
# ---------------------------------------------------------------------

class TestOnlyProductsArePublished:

    def test_water_extent_reference_cache_is_never_published(self, run_job):
        """The CDL / WorldCover rasters are large, are not products, and live
        under the CLI's --output dir. A recursive copy would ship them."""
        result = run_job(**defaults(products="we"))
        assert result.ok, result.output

        published = result.out_home_files() + result.output_dir_files()
        assert published, "nothing was published at all"
        assert not any("WorldCover" in f for f in published), published
        assert not any("CDL" in f for f in published), published
        assert not any("water_extent_reference" in f for f in published), published

    def test_the_reference_cache_really_was_created(self, run_job):
        """Guards the test above from passing vacuously."""
        result = run_job(**defaults(products="we"))
        assert result.ok, result.output
        # The stub writes the cache; if it stopped, the filter test proves nothing.
        assert any("--product" in a for a in [x for argv in result.invocations() for x in argv])


# ---------------------------------------------------------------------
# Conditional flags
# ---------------------------------------------------------------------

class TestConditionalFlags:

    def test_merge_defaults_on_and_mask_defaults_off(self, run_job):
        """Defaults must mirror algorithm_config.yaml, not a blanket false."""
        result = run_job(**defaults(products="ndvi"))
        assert result.ok, result.output
        argv = result.invocations()[0]
        assert "--merge" in argv
        assert "--cloud-mask" not in argv

    def test_merge_false_drops_the_flag(self, run_job):
        result = run_job(**defaults(products="ndvi", merge="false"))
        assert result.ok, result.output
        assert "--merge" not in result.invocations()[0]

    def test_mask_true_adds_cloud_mask(self, run_job):
        result = run_job(**defaults(products="ndvi", mask="true"))
        assert result.ok, result.output
        assert "--cloud-mask" in result.invocations()[0]

    def test_bare_boolean_presence_form_is_accepted(self, run_job):
        """MAAP may emit a bare `--mask` with no value."""
        result = run_job(**defaults(products="ndvi", mask=None))
        assert result.ok, result.output
        assert "--cloud-mask" in result.invocations()[0]

    def test_we_nstd_is_forwarded_as_multiple_values(self, run_job):
        result = run_job(**defaults(products="we", we_nstd="1 1.5"))
        assert result.ok, result.output
        argv = result.invocations()[0]
        i = argv.index("--we-nstd")
        assert argv[i + 1:i + 3] == ["1", "1.5"]


# ---------------------------------------------------------------------
# Validation — every one of these must fail BEFORE any CLI call
# ---------------------------------------------------------------------

class TestValidationFailsFast:

    def _assert_rejected(self, result, needle):
        assert not result.ok, f"should have failed: {result.output}"
        assert needle in result.output, result.output
        assert result.invocations() == [], "validation must run before the CLI"

    def test_placeholder_activation_event_is_rejected(self, run_job):
        self._assert_rejected(
            run_job(**defaults(activation_event="YYYYMM_Hazard_Location")),
            "activation_event",
        )

    def test_transposed_bbox_is_rejected(self, run_job):
        self._assert_rejected(
            run_job(**defaults(bbox="-93.55,42.10,-93.70,42.00")),
            "min_lon<max_lon",
        )

    def test_out_of_range_bbox_is_rejected(self, run_job):
        self._assert_rejected(
            run_job(**defaults(bbox="-193.0,42.00,-93.55,42.10")), "out of range"
        )

    def test_a_small_bbox_is_ACCEPTED(self, run_job):
        """Unlike Black Marble, Sentinel-2 has no minimum AOI: a STAC search
        returns whole 110 km tiles however small the box is."""
        result = run_job(**defaults(products="ndvi",
                                    bbox="-93.70,42.00,-93.69,42.01"))
        assert result.ok, result.output

    def test_pre_launch_date_is_rejected(self, run_job):
        self._assert_rejected(
            run_job(**defaults(start_date="2014-01-01")), "before 2015-06-23"
        )

    def test_malformed_date_is_rejected(self, run_job):
        self._assert_rejected(
            run_job(**defaults(start_date="20250812")), "YYYY-MM-DD"
        )

    def test_start_after_end_is_rejected(self, run_job):
        self._assert_rejected(
            run_job(**defaults(start_date="2025-08-20", end_date="2025-08-14")),
            "after end_date",
        )

    def test_unknown_product_is_rejected(self, run_job):
        self._assert_rejected(run_job(**defaults(products="ndvi bogus")), "bogus")

    def test_empty_products_is_rejected(self, run_job):
        self._assert_rejected(run_job(**defaults(products="")), "products")

    def test_bad_level_is_rejected(self, run_job):
        self._assert_rejected(run_job(**defaults(level="3")), "level")

    def test_cloud_cover_above_100_is_rejected(self, run_job):
        self._assert_rejected(run_job(**defaults(cloud_cover="150")), "cloud_cover")

    def test_non_numeric_we_nstd_is_rejected(self, run_job):
        self._assert_rejected(
            run_job(**defaults(products="we", we_nstd="high")), "we_nstd"
        )

    def test_cloud_mask_on_l1c_is_rejected_with_the_reason(self, run_job):
        """L1C has no Scene Classification Layer. The CLI rejects it too, but
        catching it here saves a STAC round-trip and says why."""
        result = run_job(**defaults(level="1", mask="true"))
        self._assert_rejected(result, "Scene Classification Layer")


# ---------------------------------------------------------------------
# Coexistence with the .SAFE algorithm
# ---------------------------------------------------------------------

class TestCoexistence:

    def test_it_never_asks_for_copernicus_credentials(self, run_job):
        """The STAC path reads public data; a MAAP secret lookup here would be
        a regression toward the .SAFE job's requirements."""
        result = run_job(**defaults(products="ndvi"))
        assert result.ok, result.output
        assert "_get_secret" not in result.output
        assert "COP_USER" not in result.output

    def test_run_sh_does_not_shell_out_to_7z(self):
        """p7zip is the .SAFE job's dependency. If the STAC job ever grows a
        7z call, dropping p7zip in Phase 5 would break it.

        Checked against CODE only -- the header comment legitimately mentions
        7z while explaining what the .SAFE job does differently.
        """
        with open(RUN_SH) as fh:
            code = "\n".join(
                line for line in fh.read().splitlines()
                if not line.lstrip().startswith("#")
            )
        assert "7z" not in code

    def test_both_sentinel2_run_scripts_exist_and_differ(self):
        legacy = os.path.join(REPO_ROOT, "dps", "sentinel2", "run.sh")
        assert os.path.exists(legacy), "the .SAFE algorithm must stay until Phase 5"
        assert os.path.exists(RUN_SH)
        with open(legacy) as a, open(RUN_SH) as b:
            assert a.read() != b.read()
