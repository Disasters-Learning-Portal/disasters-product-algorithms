"""Executes the real dps/blackmarble{,_noaa}/run.sh end to end, with conda stubbed.

WHY THIS EXISTS
---------------
run.sh is where the Black Marble job actually lives: it validates the inputs, picks the
platform, builds the output path, chooses which pipeline entry point to invoke, renames
the colored companion, bakes the activation event and hands off to _finalize.sh. None of
that is covered by unit tests of the pieces, and none of it is covered by CI's
`<cli> --help` smoke check either -- a broken orchestration only shows up as a failed (or
worse, a silently wrong) DPS job.

The one thing that is stubbed is `conda`, because the real invocations need a MAAP secret,
an Earthdata token and a 20-minute download. The stub sits first on PATH and:

  * `_get_secret.py`  -> prints a fake token (so the MAAP path is exercised, not skipped)
  * `blackmarble` / `bm_noaa.py` -> records the argv and writes real (tiny) GeoTIFFs at
    --output-path, exactly where and how upstream would, including the `-colored` companion
  * `bake_event.py`   -> runs FOR REAL under this interpreter, so the event tags on the
    finished products are genuine

Everything else -- the validators, the naming, the rename, the layout, the output/ copy --
is the shipped code. The truly-nothing-stubbed version of this is tests/e2e.
"""
import os
import shutil
import stat
import subprocess
import sys
import textwrap

import pytest

rasterio = pytest.importorskip("rasterio")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SNPP_RUN_SH = os.path.join(REPO_ROOT, "dps", "blackmarble", "run.sh")
NOAA_RUN_SH = os.path.join(REPO_ROOT, "dps", "blackmarble_noaa", "run.sh")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")

EVENT = "202601_KyleWx_US"
SF_BBOX = "-122.55,37.69,-122.32,37.81"
SF_CORNERS = "37_81N122_55W37_69N122_32W"
DATE = "2023-06-15"
FAKE_TOKEN = "fake-earthdata-token-9f3a2b7c1d"

CONDA_STUB = r'''#!/usr/bin/env bash
# Stand-in for `conda` on PATH. Records every invocation, then emulates the three things
# dps/blackmarble/run.sh asks conda to run.
set -uo pipefail
printf '%s\n' "$*" >> "${STUB_LOG}"

# strip: run [--live-stream] --name <env>
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

# --- the Earthdata secret lookup ---
if [[ "${cmd}" == "python" && "${1:-}" == *_get_secret.py ]]; then
  printf '%s' "${FAKE_TOKEN}"
  exit 0
fi

# --- the activation-event bake: run it for real ---
if [[ "${cmd}" == "python" && "${1:-}" == *bake_event.py ]]; then
  exec "${TEST_PYTHON}" "$@"
fi

# --- the Black Marble pipeline (upstream CLI, or our NOAA entry point) ---
if [[ "${cmd}" == "blackmarble" || ( "${cmd}" == "python" && "${1:-}" == *bm_noaa.py ) ]]; then
  if [[ "${cmd}" == "python" ]]; then entry="bm_noaa"; shift; else entry="blackmarble"; fi
  printf '%s\n' "${entry} $*" >> "${PIPELINE_LOG}"

  if [[ "${PIPELINE_PRODUCES_NOTHING:-0}" == "1" ]]; then exit 0; fi
  if [[ "${PIPELINE_FAILS:-0}" == "1" ]]; then echo "stub pipeline failure" >&2; exit 3; fi

  out=""
  while [[ $# -gt 0 ]]; do
    [[ "$1" == "--output-path" ]] && { out="$2"; shift 2; continue; }
    shift
  done
  [[ -n "${out}" ]] || { echo "stub: no --output-path" >&2; exit 65; }
  # Upstream writes the NDUI COG at --output-path and derives the colored companion as
  # output_path.replace(".tif", "-colored.tif"). Reproduce BOTH, since the rename of the
  # second one is part of what this suite is checking.
  "${TEST_PYTHON}" "${MAKE_TIF_PY}" "${out}" "${out%.tif}-colored.tif"
  exit 0
fi

echo "stub: unhandled command: ${cmd} $*" >&2
exit 66
'''

MAKE_TIF_PY = textwrap.dedent(
    """
    import sys
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    data = np.linspace(0, 100, 32 * 32, dtype="float32").reshape(32, 32)
    for path in sys.argv[1:]:
        with rasterio.open(
            path, "w", driver="GTiff", height=32, width=32, count=1, dtype="float32",
            crs="EPSG:32610", transform=from_bounds(-122.55, 37.69, -122.32, 37.81, 32, 32),
            tiled=True, blockxsize=32, blockysize=32, compress="deflate",
        ) as dst:
            dst.write(data, 1)
            dst.update_tags(producer="Black Marble Pipeline")
    """
)


class Run:
    """The result of one run.sh invocation, plus helpers to inspect what it did."""

    def __init__(self, proc, home, cwd, pipeline_log, stub_log, event=EVENT):
        self.proc = proc
        self.home = home
        self.cwd = cwd
        self.event = event
        self._pipeline_log = pipeline_log
        self._stub_log = stub_log

    @property
    def ok(self):
        return self.proc.returncode == 0

    @property
    def output(self):
        return self.proc.stdout + self.proc.stderr

    @property
    def pipeline_calls(self):
        if not os.path.exists(self._pipeline_log):
            return []
        with open(self._pipeline_log) as handle:
            return [line.strip() for line in handle if line.strip()]

    @property
    def conda_calls(self):
        if not os.path.exists(self._stub_log):
            return []
        with open(self._stub_log) as handle:
            return [line.strip() for line in handle if line.strip()]

    def out_dir(self, date=DATE, product="hdnightlights"):
        return os.path.join(self.home, "drcs_outputs", self.event,
                            date.replace("-", ""), product)

    def products(self, **kwargs):
        """Product filenames, possibly empty.

        Callers that LOOP over this must assert it is non-empty first (or use
        ``some_products``): a for-loop over an empty list asserts nothing at all, so a
        run that produced no output would report success. Callers that compare against
        an exact expected list are already safe.
        """
        directory = self.out_dir(**kwargs)
        if not os.path.isdir(directory):
            return []
        return sorted(name for name in os.listdir(directory) if name.endswith(".tif"))

    def some_products(self, **kwargs):
        """Same, but never empty -- use this whenever the test loops."""
        found = self.products(**kwargs)
        assert found, f"no products under {self.out_dir(**kwargs)}"
        return found


@pytest.fixture
def run_job(tmp_path):
    """Invoke a run.sh with conda stubbed, HOME and CWD sandboxed, S3 publish disabled."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    conda = bindir / "conda"
    conda.write_text(CONDA_STUB)
    conda.chmod(conda.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    make_tif = tmp_path / "make_tif.py"
    make_tif.write_text(MAKE_TIF_PY)

    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    pipeline_log = tmp_path / "pipeline.log"
    stub_log = tmp_path / "conda.log"

    def _run(script=NOAA_RUN_SH, env=None, **inputs):
        args = []
        for name, value in inputs.items():
            # value None => emit the bare presence form (`--wgs84`), which MAAP uses for
            # booleans; anything else emits the `--name value` form.
            args.append(f"--{name}")
            if value is not None:
                args.append(str(value))

        environ = dict(os.environ)
        # A login shell would re-source the profile; keep the stub first regardless.
        environ["PATH"] = f"{bindir}:{environ['PATH']}"
        environ.update({
            "HOME": str(home),
            "STUB_LOG": str(stub_log),
            "PIPELINE_LOG": str(pipeline_log),
            "TEST_PYTHON": sys.executable,
            "MAKE_TIF_PY": str(make_tif),
            "FAKE_TOKEN": FAKE_TOKEN,
            # _finalize.sh test hook: copy to output/ but skip the S3 publish and the
            # scratch delete, so the products survive for inspection.
            "DPS_DRY_RUN": "1",
        })
        environ.pop("EARTHDATA_TOKEN", None)
        environ.update(env or {})

        proc = subprocess.run(
            ["bash", script, *args],
            cwd=str(cwd), env=environ, capture_output=True, text=True, timeout=300,
        )
        return Run(proc, str(home), str(cwd), str(pipeline_log), str(stub_log),
                   event=inputs.get("activation_event", EVENT))

    return _run


def defaults(**overrides):
    base = {"activation_event": EVENT, "bbox": SF_BBOX, "date": DATE, "config": "fast"}
    base.update(overrides)
    return base


# --- which pipeline each algorithm runs --------------------------------------------


def test_noaa_job_runs_the_vj146a2_entry_point(run_job):
    """The whole point of the NOAA algorithm: it must NOT call the bare upstream console
    script, which would download Suomi-NPP and publish it under a NOAA-20 name."""
    result = run_job(NOAA_RUN_SH, **defaults())
    assert result.ok, result.output
    assert len(result.pipeline_calls) == 1
    assert result.pipeline_calls[0].startswith("bm_noaa ")


def test_snpp_job_still_runs_the_upstream_console_script(run_job):
    """The existing algorithm's behavior must be unchanged by the platform refactor."""
    result = run_job(SNPP_RUN_SH, **defaults())
    assert result.ok, result.output
    assert result.pipeline_calls[0].startswith("blackmarble ")


def test_both_jobs_pass_identical_pipeline_flags(run_job):
    """Only the product differs; every CLI flag upstream sees is the same."""
    noaa = run_job(NOAA_RUN_SH, **defaults()).pipeline_calls[0].split(" ", 1)[1]
    snpp = run_job(SNPP_RUN_SH, **defaults()).pipeline_calls[0].split(" ", 1)[1]
    normalize = lambda call: call.replace("hdnightlightsnoaa20", "hdnightlights")
    assert normalize(noaa) == normalize(snpp)


def test_noaa_job_reports_its_platform_in_the_log(run_job):
    result = run_job(NOAA_RUN_SH, **defaults())
    assert "platform=noaa20 (VJ146A2)" in result.output


def test_snpp_job_reports_its_platform_in_the_log(run_job):
    result = run_job(SNPP_RUN_SH, **defaults())
    assert "platform=snpp (VNP46A2)" in result.output


# --- output naming and layout -------------------------------------------------------


def test_noaa_products_land_in_their_own_product_folder(run_job):
    result = run_job(NOAA_RUN_SH, **defaults())
    assert result.products(product="hdnightlightsnoaa20") == [
        f"hdnightlightsnoaa20_{SF_CORNERS}_{DATE}_day.tif",
        f"hdnightlightsnoaa20colored_{SF_CORNERS}_{DATE}_day.tif",
    ]


def test_snpp_products_keep_their_original_names(run_job):
    result = run_job(SNPP_RUN_SH, **defaults())
    assert result.products(product="hdnightlights") == [
        f"hdnightlights_{SF_CORNERS}_{DATE}_day.tif",
        f"hdnightlightscolored_{SF_CORNERS}_{DATE}_day.tif",
    ]


def test_the_two_platforms_do_not_overwrite_each_other(run_job, tmp_path):
    """The collision guard, exercised for real: run both jobs into one HOME for one
    activation event and date, and assert four distinct products survive."""
    run_job(SNPP_RUN_SH, **defaults())
    result = run_job(NOAA_RUN_SH, **defaults())

    date_dir = os.path.join(result.home, "drcs_outputs", EVENT, DATE.replace("-", ""))
    found = sorted(
        os.path.join(root, name)[len(date_dir) + 1:]
        for root, _dirs, files in os.walk(date_dir) for name in files
        if name.endswith(".tif")
    )
    assert found == [
        f"hdnightlights/hdnightlights_{SF_CORNERS}_{DATE}_day.tif",
        f"hdnightlights/hdnightlightscolored_{SF_CORNERS}_{DATE}_day.tif",
        f"hdnightlightsnoaa20/hdnightlightsnoaa20_{SF_CORNERS}_{DATE}_day.tif",
        f"hdnightlightsnoaa20/hdnightlightsnoaa20colored_{SF_CORNERS}_{DATE}_day.tif",
    ]


def test_the_colored_companion_is_renamed_off_upstreams_suffix(run_job):
    """Upstream writes `<stem>-colored.tif`, which leaves the date token no longer last.
    run.sh renames it onto the colored product's own stem."""
    result = run_job(NOAA_RUN_SH, **defaults())
    names = result.some_products(product="hdnightlightsnoaa20")
    assert not any("-colored" in name for name in names)
    assert all(name.endswith(f"_{DATE}_day.tif") for name in names)


def test_the_date_directory_matches_the_requested_date(run_job):
    result = run_job(NOAA_RUN_SH, **defaults(date="2019-03-04"))
    assert result.ok, result.output
    assert result.some_products(date="2019-03-04", product="hdnightlightsnoaa20")


def test_products_are_copied_into_the_dps_output_dir(run_job):
    """_finalize.sh step 1 -- DPS uploads output/, so a product missing there is lost."""
    result = run_job(NOAA_RUN_SH, **defaults())
    copied = os.path.join(result.cwd, "output", DATE.replace("-", ""),
                          "hdnightlightsnoaa20")
    assert sorted(os.listdir(copied)) == [
        f"hdnightlightsnoaa20_{SF_CORNERS}_{DATE}_day.tif",
        f"hdnightlightsnoaa20colored_{SF_CORNERS}_{DATE}_day.tif",
    ]


# --- the activation-event bake ------------------------------------------------------


def test_every_product_carries_the_noaa20_provenance_tags(run_job):
    """bake_event.py runs for real in this harness, so these are genuine GeoTIFF tags."""
    result = run_job(NOAA_RUN_SH, **defaults())
    directory = result.out_dir(product="hdnightlightsnoaa20")

    for name in result.some_products(product="hdnightlightsnoaa20"):
        with rasterio.open(os.path.join(directory, name)) as src:
            tags = src.tags()
        assert tags["ACTIVATION_EVENT"] == EVENT
        assert tags["YEAR_MONTH"] == "202601"
        assert tags["HAZARD"] == "KyleWx"
        assert tags["LOCATION"] == "US"
        assert tags["VIIRS_PRODUCT"] == "VJ146A2"
        assert tags["VIIRS_PLATFORM"] == "NOAA-20"
        assert "VNP46A2" not in tags["SOURCE"]


def test_snpp_products_are_tagged_suomi_npp(run_job):
    result = run_job(SNPP_RUN_SH, **defaults())
    directory = result.out_dir(product="hdnightlights")
    with rasterio.open(os.path.join(directory, result.some_products()[0])) as src:
        tags = src.tags()
    assert tags["VIIRS_PRODUCT"] == "VNP46A2"
    assert tags["VIIRS_PLATFORM"] == "Suomi-NPP"


def test_a_later_run_does_not_re_tag_the_other_platforms_products(run_job):
    """bake_event.py stamps EVERY .tif under the directory it is given with THIS run's
    platform, so it must be scoped to the product dir this run wrote -- not to OUT_HOME,
    which holds every product for the activation event.

    Pointed at OUT_HOME, a NOAA-20 run of an event that already has Suomi-NPP products
    would relabel them VJ146A2/NOAA-20: wrong provenance on an already-correct raster,
    written by a green job.
    """
    run_job(SNPP_RUN_SH, **defaults())
    result = run_job(NOAA_RUN_SH, **defaults())   # same HOME, same event, same date

    expected = {"hdnightlights": ("VNP46A2", "Suomi-NPP"),
                "hdnightlightsnoaa20": ("VJ146A2", "NOAA-20")}
    for product, (code, satellite) in expected.items():
        directory = result.out_dir(product=product)
        for name in result.some_products(product=product):
            with rasterio.open(os.path.join(directory, name)) as src:
                tags = src.tags()
            assert tags["VIIRS_PRODUCT"] == code, f"{name} was re-tagged"
            assert tags["VIIRS_PLATFORM"] == satellite, f"{name} was re-tagged"


# --- input validation is fail-fast --------------------------------------------------


@pytest.mark.parametrize("inputs,expected", [
    (defaults(activation_event="YYYYMM_Hazard_Location"), "placeholder"),
    (defaults(activation_event="Flood_TX"), "must be YYYYMM_Hazard_Location"),
    (defaults(bbox="-122.55,37.69,-122.32"), "four numbers"),
    (defaults(bbox="-122.55,37.69,-122.32,37.70"), "latitude span"),
    (defaults(bbox="-200,37.69,-122.32,37.81"), "out of range"),
    (defaults(bbox="-122.32,37.69,-122.55,37.81"), "min_lon<max_lon"),
    (defaults(date="20230615"), "expected YYYY-MM-DD"),
    (defaults(date="2023-13-01"), "expected YYYY-MM-DD"),
    (defaults(config="turbo"), "config 'turbo' is invalid"),
    (defaults(osm_source="osmnx"), "osm_source 'osmnx' is invalid"),
])
def test_bad_input_fails_before_anything_runs(run_job, inputs, expected):
    """Fail fast means BEFORE conda: no secret lookup, no download, no partial output.

    Asserting the stub was never invoked is what makes this a real check -- an error
    message alone would still pass if validation had moved after the download.
    """
    result = run_job(NOAA_RUN_SH, **inputs)
    assert not result.ok
    assert expected in result.output
    assert result.conda_calls == []
    assert result.pipeline_calls == []


def test_noaa_job_rejects_a_date_before_the_vj146a2_mission_start(run_job):
    """VJ146A2 begins 2018-01-19; earlier dates return zero granules from Earthdata."""
    result = run_job(NOAA_RUN_SH, **defaults(date="2017-06-15"))
    assert not result.ok
    assert "before 2018-01-19" in result.output
    assert result.conda_calls == []


def test_noaa_job_accepts_the_first_day_vj146a2_exists(run_job):
    result = run_job(NOAA_RUN_SH, **defaults(date="2018-01-19"))
    assert result.ok, result.output


def test_snpp_job_allows_dates_the_noaa_job_rejects(run_job):
    """The two floors are genuinely different gates, not one shared constant."""
    result = run_job(SNPP_RUN_SH, **defaults(date="2015-06-15"))
    assert result.ok, result.output


def test_snpp_job_rejects_a_date_before_its_own_mission_start(run_job):
    result = run_job(SNPP_RUN_SH, **defaults(date="2011-06-15"))
    assert not result.ok
    assert "before 2012-01-19" in result.output


# --- the Suomi-NPP sunset warning (the reason this work exists) ---------------------


def test_snpp_job_warns_past_the_sunset_and_names_the_alternative(run_job):
    """disasters-portal#365: Suomi-NPP delivery ceases 2026-11-01. The archive stays
    readable, so this WARNS and still runs -- it must not fail a historical activation."""
    result = run_job(SNPP_RUN_SH, **defaults(date="2026-12-01"))
    assert result.ok, result.output
    assert "2026-11-01" in result.output
    assert "disasters-blackmarble-noaa-process" in result.output
    assert "VJ146A2" in result.output


def test_snpp_job_does_not_warn_before_the_sunset(run_job):
    result = run_job(SNPP_RUN_SH, **defaults(date="2026-10-31"))
    assert result.ok, result.output
    assert "disasters-blackmarble-noaa-process" not in result.output


def test_noaa_job_never_warns_about_the_sunset(run_job):
    """NOAA-20 is unaffected by the Suomi-NPP outage; warning here would be noise."""
    result = run_job(NOAA_RUN_SH, **defaults(date="2026-12-01"))
    assert result.ok, result.output
    assert "ceases" not in result.output


# --- flags forwarded to the pipeline ------------------------------------------------


def test_wgs84_is_off_by_default(run_job):
    result = run_job(NOAA_RUN_SH, **defaults())
    assert "--wgs84" not in result.pipeline_calls[0]


def test_wgs84_value_form_is_forwarded(run_job):
    """MAAP may send a boolean as `--flag true`..."""
    result = run_job(NOAA_RUN_SH, **defaults(wgs84="true"))
    assert result.ok, result.output
    assert "--wgs84" in result.pipeline_calls[0]


def test_wgs84_value_false_is_not_forwarded(run_job):
    result = run_job(NOAA_RUN_SH, **defaults(wgs84="false"))
    assert result.ok, result.output
    assert "--wgs84" not in result.pipeline_calls[0]


def test_bare_wgs84_flag_is_accepted(run_job):
    """...or as a bare `--flag`. The value-form parser must not swallow the NEXT argument
    when the value is absent -- doing so would silently drop whatever followed it."""
    result = run_job(NOAA_RUN_SH, wgs84=None, **defaults())
    assert result.ok, result.output
    assert "--wgs84" in result.pipeline_calls[0]
    assert f"--date {DATE}" in result.pipeline_calls[0]


@pytest.mark.parametrize("preset", ["fast", "default", "high_quality"])
def test_config_preset_is_forwarded(run_job, preset):
    result = run_job(NOAA_RUN_SH, **defaults(config=preset))
    assert f"--config {preset}" in result.pipeline_calls[0]


def test_unrecognized_arguments_are_ignored_with_a_warning(run_job):
    """MAAP has been known to emit extra flags; a job must not die on one."""
    result = run_job(NOAA_RUN_SH, **defaults(some_future_input="x"))
    assert result.ok, result.output
    assert "ignoring unrecognized arg" in result.output


# --- the Earthdata token ------------------------------------------------------------


def test_the_token_is_fetched_from_maap_secrets_by_name(run_job):
    result = run_job(NOAA_RUN_SH, **defaults(earthdata_secret_name="MY_EDL_TOKEN"))
    assert result.ok, result.output
    assert any("_get_secret.py MY_EDL_TOKEN" in call for call in result.conda_calls)


def test_the_token_never_appears_in_the_job_log(run_job):
    """The reason the token is a MAAP secret and not a job input. A DPS job log is
    readable by anyone who can see the job."""
    result = run_job(NOAA_RUN_SH, **defaults())
    assert result.ok, result.output
    assert FAKE_TOKEN not in result.output


def test_an_ambient_token_skips_the_maap_lookup(run_job):
    """The escape hatch that makes tests/e2e (and a laptop run) possible. Inert in DPS,
    where nothing sets the variable."""
    result = run_job(NOAA_RUN_SH, env={"EARTHDATA_TOKEN": "ambient-token"},
                     **defaults())
    assert result.ok, result.output
    assert not any("_get_secret.py" in call for call in result.conda_calls)
    assert "MAAP secret lookup skipped" in result.output
    assert "ambient-token" not in result.output


# --- failure handling ---------------------------------------------------------------


def test_a_pipeline_that_writes_nothing_fails_the_job(run_job):
    """Never a green job with no product. Upstream exits 0 on some empty cases."""
    result = run_job(NOAA_RUN_SH, env={"PIPELINE_PRODUCES_NOTHING": "1"}, **defaults())
    assert not result.ok
    assert "produced no COG" in result.output
    assert SF_BBOX in result.output and DATE in result.output


def test_a_failing_pipeline_fails_the_job(run_job):
    result = run_job(NOAA_RUN_SH, env={"PIPELINE_FAILS": "1"}, **defaults())
    assert not result.ok


def test_no_products_are_published_when_the_pipeline_fails(run_job):
    result = run_job(NOAA_RUN_SH, env={"PIPELINE_FAILS": "1"}, **defaults())
    assert result.products(product="hdnightlightsnoaa20") == []


# --- scratch handling ---------------------------------------------------------------


def test_the_download_scratch_dir_is_removed(run_job):
    """Raw .h5/.tif downloads are large; leaving them fills the worker's disk. They live
    OUTSIDE OUT_HOME so only finished COGs reach _finalize.sh."""
    result = run_job(NOAA_RUN_SH, **defaults())
    call = result.pipeline_calls[0].split()
    data_dir = call[call.index("--data-dir") + 1]
    assert not os.path.exists(data_dir)
    assert os.path.commonpath([data_dir, result.home]) != result.home


def test_the_platform_env_var_is_not_a_job_input(run_job):
    """Passing --BM_PLATFORM must not be able to turn the NOAA job into a Suomi-NPP one."""
    result = run_job(NOAA_RUN_SH, **defaults(BM_PLATFORM="snpp"))
    assert result.ok, result.output
    assert result.pipeline_calls[0].startswith("bm_noaa ")
    assert result.some_products(product="hdnightlightsnoaa20")
