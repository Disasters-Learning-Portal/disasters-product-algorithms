"""Consistency of the DPS registration configs -- every algorithm, both registration paths.

An algorithm is registered from one of two files that describe the SAME process:

    dps/<job>/algorithm_config.yaml   the Register Algorithm GUI path
    dps/ogc/<job>.yml                 the CLI / GitHub-Action path (register-dps.yml)

Nothing in the repo forces them to agree, and every way they can disagree fails LATE and
quietly -- at registration time in a browser, or worse, at submit time for an operator:

  * an algorithm_name MAAP rejects (it requires ^[a-z0-9_-]+$) cannot be registered at all;
  * a `?`-suffixed type in the GUI config makes the Type dropdown error "Please select an
    item in the list"; the same suffix MISSING from the OGC config makes the Submit Job
    form reject every falsy default with "Valid value required";
  * an input the config declares but run.sh never parses is a form field that silently
    does nothing;
  * an OGC config missing from register-dps.yml's dropdown simply cannot be dispatched.

These are cheap to check statically and expensive to discover live, so they are checked
here. The rules come from docs/DPS.md and CLAUDE.md's MAAP DPS section.
"""
import os
import re

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DPS = os.path.join(REPO_ROOT, "dps")
OGC_DIR = os.path.join(DPS, "ogc")
REGISTER_WORKFLOW = os.path.join(
    REPO_ROOT, ".github", "workflows", "register-dps.yml"
)

# MAAP's algorithm_name constraint.
NAME_RE = re.compile(r"^[a-z0-9_-]+$")

# The Register Algorithm GUI hard-codes this Type dropdown
# (algorithms-jupyter-extension/src/constants.ts). The OGC path additionally allows a
# trailing "?" (= minOccurs:0), which the GUI cannot express.
BASE_TYPES = {"string", "int", "File", "Directory", "long", "float", "boolean", "double"}

# dps/<job>/ dirs that are registerable algorithms (have an algorithm_config.yaml).
JOB_DIRS = sorted(
    name for name in os.listdir(DPS)
    if os.path.isfile(os.path.join(DPS, name, "algorithm_config.yaml"))
)

OGC_CONFIGS = sorted(
    name[:-4] for name in os.listdir(OGC_DIR) if name.endswith(".yml")
)


# --- which OGC configs are actually live -------------------------------------------
# Only these are current and registered. The cross-path consistency rules below are
# asserted strictly for them, because for these the two files really do describe one
# deployed process.
#
# `sentinel2` and `sentinel2_stac` are BOTH live on purpose: the STAC pipeline is
# registered alongside the .SAFE one so the two can be compared on real
# activations before either is retired (issue #144, Phase 3/4). They are separate
# processes with separate names -- a rename is not a move, so reusing one name
# would replace rather than add.
CURRENT_OGC = {
    "sentinel2",
    "sentinel2_stac",
    "umbra",
    "blackmarble",
    "blackmarble_noaa",
}

# The rest predate the sensor CLI changes (PR #45 hardcoded most of satellogic's inputs;
# the "-ogc-test" -> "disasters-<sensor>-process" name consolidation never reached them)
# and are NOT registered. Checking them against their GUI configs would assert a
# consistency nobody is maintaining, so they are skipped with a reason rather than
# silently excluded -- `pytest -ra` prints the list every run.
STALE_UNREGISTERED_OGC = {"capella", "landsat", "list_dates", "satellogic"}

# Configs that exist ONLY on the `deploy-algorithm` branch and are deliberately never
# merged into `dev` (docs/DPS.md: deploy-algorithm stays ahead of dev by the generated
# CWL and by whatever is being deployed ad hoc).
#
# They are named HERE, on dev, even though the files are not on dev -- because this test
# file is what deploy-algorithm's CI runs. Without the entry, every pull request on that
# branch fails with "dps/ogc/probe.yml is neither in CURRENT_OGC nor
# STALE_UNREGISTERED_OGC" (observed on run 31713560843). The sets are plain name lists
# and the tests iterate over files that actually exist, so naming a file that is absent
# from dev is inert here.
#
# `probe` is the temporary IAM / Secrets-Manager reachability diagnostic in `dps/probe/`,
# marked DELETE AFTER USE. Remove this entry when the probe itself is retired.
DEPLOY_ONLY_OGC = {"probe"}

# --- known defect in a LIVE config ---------------------------------------------------
# dps/ogc/umbra.yml still declares an `apply_filter` boolean that dps/umbra/run.sh does
# not parse: PR #44 removed the toggle (Umbra speckle filtering is always on) but the OGC
# config kept the input, so the registered algorithm shows an "Apply Lee filter" checkbox
# that does nothing, and filter_size's doc still says "only used when apply_filter is
# true". Left in place deliberately: removing an input is a SCHEMA change, so it only
# takes effect after umbra is re-registered (a re-register over the same name+version
# 409s and silently keeps the old schema -- CLAUDE.md, MAAP DPS section). Listed here so
# the check stays on for every other input while the defect stays visible.
KNOWN_LIVE_CONFIG_DEFECTS = {"umbra": {"apply_filter"}}


def load(path):
    with open(path) as handle:
        return yaml.safe_load(handle)


def blank_to_none(value):
    """A missing default and an empty-string default submit the same value.

    The GUI configs omit `default` where the OGC configs write `default: ""`; that is a
    style difference, not a behavioral one, so it is normalized away rather than listed
    as drift.
    """
    return None if value == "" else value


def gui_config(job):
    return load(os.path.join(DPS, job, "algorithm_config.yaml"))


def ogc_config(job):
    return load(os.path.join(OGC_DIR, f"{job}.yml"))


def inputs_by_name(config):
    return {item["name"]: item for item in config.get("inputs", [])}


def test_the_new_noaa_algorithm_is_registerable_under_the_agreed_name():
    """disasters-blackmarble-NOAA-process was the request; MAAP requires lowercase, so the
    registered name is the lowercased form. Both registration paths must carry it."""
    assert gui_config("blackmarble_noaa")["algorithm_name"] == "disasters-blackmarble-noaa-process"
    assert ogc_config("blackmarble_noaa")["algorithm_name"] == "disasters-blackmarble-noaa-process"


def test_the_noaa_algorithm_is_distinct_from_the_suomi_npp_one():
    """A rename is not a move: registering the same name would replace the Suomi-NPP job
    rather than add a second one (and 409 on a duplicate version)."""
    assert (gui_config("blackmarble_noaa")["algorithm_name"]
            != gui_config("blackmarble")["algorithm_name"])


@pytest.mark.parametrize("job", JOB_DIRS)
def test_gui_algorithm_name_is_maap_legal(job):
    name = gui_config(job)["algorithm_name"]
    assert NAME_RE.match(name), (
        f"dps/{job}/algorithm_config.yaml algorithm_name {name!r} must match "
        f"{NAME_RE.pattern} -- MAAP rejects uppercase, spaces and slashes"
    )


@pytest.mark.parametrize("job", OGC_CONFIGS)
def test_ogc_algorithm_name_is_maap_legal(job):
    name = ogc_config(job)["algorithm_name"]
    assert NAME_RE.match(name), (
        f"dps/ogc/{job}.yml algorithm_name {name!r} must match {NAME_RE.pattern}"
    )


@pytest.mark.parametrize("job", OGC_CONFIGS)
def test_every_ogc_config_is_dispatchable(job):
    """register-dps.yml reads dps/ogc/<algorithm>.yml, and `algorithm` is a `choice` input.

    An OGC config absent from that dropdown cannot be selected, so the algorithm is
    undeployable no matter how correct the config is.
    """
    workflow = load(REGISTER_WORKFLOW)
    # PyYAML parses the bare `on:` key as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    options = triggers["workflow_dispatch"]["inputs"]["algorithm"]["options"]
    assert job in options, (
        f"dps/ogc/{job}.yml exists but {job!r} is not in register-dps.yml's algorithm "
        f"dropdown, so it can never be dispatched"
    )


def test_every_dropdown_option_has_an_ogc_config():
    """The reverse: a dropdown entry with no config fails the run at the sed/stamp step."""
    workflow = load(REGISTER_WORKFLOW)
    triggers = workflow.get("on", workflow.get(True))
    options = triggers["workflow_dispatch"]["inputs"]["algorithm"]["options"]
    missing = [name for name in options if name not in OGC_CONFIGS]
    assert not missing, f"register-dps.yml lists {missing} with no dps/ogc/<name>.yml"


@pytest.fixture(params=sorted(OGC_CONFIGS))
def live_job(request):
    """Each OGC config that is current and registered; the stale ones skip with a reason."""
    job = request.param
    if job in STALE_UNREGISTERED_OGC:
        pytest.skip(
            f"dps/ogc/{job}.yml is stale and not registered (predates the sensor CLI "
            f"changes); only {sorted(CURRENT_OGC)} are live"
        )
    if job in DEPLOY_ONLY_OGC:
        pytest.skip(
            f"dps/ogc/{job}.yml exists only on the deploy-algorithm branch and is not a "
            f"product algorithm; it is not held to the dev/OGC parity rules"
        )
    assert job in CURRENT_OGC, (
        f"dps/ogc/{job}.yml is in no registration-status set (CURRENT_OGC, "
        f"STALE_UNREGISTERED_OGC, DEPLOY_ONLY_OGC) -- add it to one so its status is "
        f"recorded"
    )
    return job


def test_every_ogc_config_has_a_recorded_registration_status():
    """A new OGC config must be classified, so it can't slip past the checks below.

    This runs on `deploy-algorithm` too, which carries configs that are not on `dev` --
    hence DEPLOY_ONLY_OGC. Leaving one out fails that branch's CI, not this one, which is
    why the sets are maintained here rather than on the branch that owns the file.
    """
    unclassified = (
        set(OGC_CONFIGS) - CURRENT_OGC - STALE_UNREGISTERED_OGC - DEPLOY_ONLY_OGC
    )
    assert not unclassified, f"unclassified OGC configs: {sorted(unclassified)}"


def test_the_two_registration_paths_describe_the_same_process(live_job):
    """Whichever path you register from must target one process with one input set."""
    gui, ogc = gui_config(live_job), ogc_config(live_job)

    assert gui["algorithm_name"] == ogc["algorithm_name"]

    gui_names = set(inputs_by_name(gui))
    ogc_names = set(inputs_by_name(ogc)) - KNOWN_LIVE_CONFIG_DEFECTS.get(live_job, set())
    assert gui_names == ogc_names, (
        f"dps/{live_job}/algorithm_config.yaml and dps/ogc/{live_job}.yml declare "
        f"different inputs"
    )


def test_defaults_agree_between_the_two_paths(live_job):
    """A default that differs by path means the same Submit produces different runs."""
    gui = inputs_by_name(gui_config(live_job))
    ogc = inputs_by_name(ogc_config(live_job))
    for name, spec in gui.items():
        assert blank_to_none(spec.get("default")) == blank_to_none(ogc[name].get("default")), (
            f"{live_job} input {name!r}: GUI default {spec.get('default')!r} != OGC "
            f"default {ogc[name].get('default')!r}"
        )


def test_every_live_ogc_input_is_parsed_by_its_run_sh(live_job):
    """An input in a REGISTERED config that run.sh ignores is a live control that does
    nothing -- exactly the umbra apply_filter defect recorded above."""
    config = ogc_config(live_job)
    run_sh_path = config["run_command"].rsplit("/", 2)
    with open(os.path.join(DPS, run_sh_path[-2], run_sh_path[-1])) as handle:
        run_sh = handle.read()
    if "exec " in run_sh:  # thin wrapper: inputs are parsed by the engine it delegates to
        with open(os.path.join(DPS, "blackmarble", "run.sh")) as handle:
            run_sh = handle.read()

    known = KNOWN_LIVE_CONFIG_DEFECTS.get(live_job, set())
    unparsed = {
        name for name in inputs_by_name(config) if f"--{name})" not in run_sh
    } - known
    assert not unparsed, (
        f"dps/ogc/{live_job}.yml declares {sorted(unparsed)}, which the run.sh it points "
        f"at never parses -- the form control would do nothing"
    )


@pytest.mark.parametrize("job", OGC_CONFIGS)
def test_ogc_types_are_base_types_optionally_marked_optional(job):
    for name, spec in inputs_by_name(ogc_config(job)).items():
        declared = str(spec["type"])
        assert declared.rstrip("?") in BASE_TYPES, (
            f"dps/ogc/{job}.yml input {name!r} has type {declared!r}; allowed are "
            f"{sorted(BASE_TYPES)} with an optional trailing '?'"
        )


@pytest.mark.parametrize("job", JOB_DIRS)
def test_gui_types_never_use_the_optional_suffix(job):
    """The GUI's Type dropdown has no `?` entry, so a `?` blocks registration outright."""
    for name, spec in inputs_by_name(gui_config(job)).items():
        declared = str(spec["type"])
        assert not declared.endswith("?"), (
            f"dps/{job}/algorithm_config.yaml input {name!r} is {declared!r}; the Register "
            f"Algorithm GUI rejects the '?' suffix ('Please select an item in the list')"
        )
        assert declared in BASE_TYPES


@pytest.mark.parametrize("job", JOB_DIRS)
def test_gui_run_and_build_commands_point_at_real_executables(job):
    """MAAP clones to /app/<repo>/ and runs these from /app, so the path is repo-prefixed.

    The executable bit matters for blackmarble_noaa in particular: its run.sh `exec`s
    dps/blackmarble/run.sh, and exec of a non-executable file fails.
    """
    config = gui_config(job)
    for key in ("run_command", "build_command"):
        command = config.get(key)
        if not command:
            continue
        relative = command.split()[0]
        prefix = "disasters-product-algorithms/"
        assert relative.startswith(prefix), (
            f"dps/{job}/algorithm_config.yaml {key} must be prefixed with {prefix!r}"
        )
        path = os.path.join(REPO_ROOT, relative[len(prefix):])
        assert os.path.isfile(path), f"{key} {command!r} does not exist"
        assert os.access(path, os.X_OK), f"{key} {command!r} is not executable"


@pytest.mark.parametrize("job", OGC_CONFIGS)
def test_ogc_run_command_is_an_absolute_path_inside_the_image(job):
    """The CWL baseCommand runs from the ADES job CWD, so a repo-relative path would not
    resolve; dps/Dockerfile bakes the repo at /app/disasters-product-algorithms."""
    command = ogc_config(job)["run_command"]
    prefix = "/app/disasters-product-algorithms/"
    assert command.startswith(prefix), (
        f"dps/ogc/{job}.yml run_command {command!r} must be absolute inside the image"
    )
    path = os.path.join(REPO_ROOT, command[len(prefix):])
    assert os.path.isfile(path) and os.access(path, os.X_OK)


@pytest.mark.parametrize("job", JOB_DIRS)
def test_every_declared_input_is_parsed_by_run_sh(job):
    """A form field run.sh never reads is a control that silently does nothing.

    Static check: run.sh parses named CWL flags in a `case` over `--<name>`.
    """
    with open(os.path.join(DPS, job, "run.sh")) as handle:
        run_sh = handle.read()

    unparsed = [
        name for name in inputs_by_name(gui_config(job))
        if f"--{name})" not in run_sh
    ]
    if unparsed and "exec " in run_sh:
        pytest.skip(f"dps/{job}/run.sh delegates via exec; inputs parsed downstream")
    assert not unparsed, (
        f"dps/{job}/algorithm_config.yaml declares {unparsed}, which dps/{job}/run.sh "
        f"never parses"
    )


def test_the_noaa_wrapper_delegates_to_the_shared_engine():
    """blackmarble_noaa/run.sh is intentionally a wrapper -- if it ever grows its own copy
    of the orchestration, the two jobs start drifting apart. Assert it stays thin and
    assert the input contract is enforced where it actually lands."""
    with open(os.path.join(DPS, "blackmarble_noaa", "run.sh")) as handle:
        wrapper = handle.read()
    with open(os.path.join(DPS, "blackmarble", "run.sh")) as handle:
        engine = handle.read()

    assert "BM_PLATFORM=noaa20" in wrapper
    assert "../blackmarble/run.sh" in wrapper
    # every input the NOAA config declares is parsed by the engine it delegates to
    for name in inputs_by_name(gui_config("blackmarble_noaa")):
        assert f"--{name})" in engine, f"engine run.sh never parses --{name}"


def test_both_blackmarble_configs_declare_the_same_inputs():
    """The platform is fixed by which algorithm you submit, never a job input -- so the two
    Black Marble algorithms must expose an identical form."""
    snpp = inputs_by_name(gui_config("blackmarble"))
    noaa = inputs_by_name(gui_config("blackmarble_noaa"))
    assert set(snpp) == set(noaa)
    for name in snpp:
        assert snpp[name]["type"] == noaa[name]["type"]
    assert "platform" not in noaa and "product" not in noaa


@pytest.mark.parametrize("job", JOB_DIRS)
def test_no_earthdata_or_credential_value_is_ever_an_input(job):
    """Credentials come from MAAP secrets at run time; a value input would land in the job
    parameters and the job log."""
    forbidden = {"earthdata_token", "token", "password", "cop_pass", "cop_user",
                 "secret", "api_key"}
    declared = {name.lower() for name in inputs_by_name(gui_config(job))}
    assert not declared & forbidden, f"dps/{job} exposes a credential value as an input"
