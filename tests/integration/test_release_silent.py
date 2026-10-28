"""Pins the `silent` release path: cut a release without paging the team.

release.yaml grew a `silent` checkbox. Ticking it still tags, pushes main,
creates the GitHub Release and rebuilds the prod image — it only suppresses
the alert issue that notify-release.yaml would otherwise open.

The signal travels between the two workflows as an HTML-comment marker in the
RELEASE BODY, because that is the only channel available: notify-release.yaml
is driven by the `release: published` event, whose payload carries nothing
about the workflow_dispatch inputs release.yaml was given.

Both failure modes of that arrangement are SILENT, which is why this file
exists — a workflow step is otherwise only exercisable by cutting a real
release:

* The two markers drifting apart. A stray character in either file and a
  silent release notifies the whole roster anyway. Both workflows go green.
* `"${NOTES[@]}"` on a NON-silent release. If that array expansion ever grows
  a way to emit an empty string, `gh release create` receives a bare ''
  argument and fails a release that had nothing wrong with it.

The run blocks are extracted from the YAML and executed for real (with a stub
`gh` on PATH), rather than being asserted against as text — same reasoning as
tests/integration/test_blackmarble_run_sh.py.
"""
import os
import re
import shutil
import subprocess

import pytest

yaml = pytest.importorskip("yaml")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKFLOWS = os.path.join(REPO, ".github", "workflows")
RELEASE_YAML = os.path.join(WORKFLOWS, "release.yaml")
NOTIFY_YAML = os.path.join(WORKFLOWS, "notify-release.yaml")

MARKER = "<!-- notify: skip -->"

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


def load(path):
    with open(path) as fh:
        return yaml.safe_load(fh)


def step_run(path, name):
    """The `run:` script of the named step."""
    doc = load(path)
    for job in doc["jobs"].values():
        for step in job["steps"]:
            if step.get("name") == name:
                return step["run"]
    raise AssertionError(f"no step named {name!r} in {os.path.basename(path)}")


def run_block(script, env, tmp_path):
    """Execute a workflow `run:` block the way the runner does (`bash -e`)."""
    merged = dict(os.environ)
    merged["GITHUB_OUTPUT"] = str(tmp_path / "github_output")
    merged["GITHUB_STEP_SUMMARY"] = str(tmp_path / "step_summary")
    merged.update(env)
    proc = subprocess.run(
        ["bash", "-e", "-c", script], capture_output=True, text=True, env=merged, cwd=REPO
    )
    outputs = {}
    out_file = tmp_path / "github_output"
    if out_file.exists():
        for line in out_file.read_text().splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                outputs[key] = value
    return proc, outputs


# --------------------------------------------------------------------------
# The checkbox itself
# --------------------------------------------------------------------------


def test_release_declares_a_silent_boolean_input():
    inputs = load(RELEASE_YAML)[True]["workflow_dispatch"]["inputs"]
    assert "silent" in inputs, "release.yaml lost its `silent` checkbox"
    assert inputs["silent"]["type"] == "boolean", "must render as a checkbox, not a text box"
    # Default-on would make every routine release silent — the opposite of
    # the intent, and nobody would notice until someone asked why they never
    # hear about releases any more.
    assert inputs["silent"]["default"] is False


def test_marker_is_byte_identical_in_both_workflows():
    """The whole mechanism is this one string agreeing across two files."""
    with open(RELEASE_YAML) as fh:
        release_src = fh.read()
    with open(NOTIFY_YAML) as fh:
        notify_src = fh.read()
    assert MARKER in release_src, "release.yaml no longer writes the skip marker"
    assert MARKER in notify_src, "notify-release.yaml no longer greps for the skip marker"


# --------------------------------------------------------------------------
# Receiving end — notify-release.yaml decides whether to alert
# --------------------------------------------------------------------------


@needs_bash
def test_release_event_with_marker_skips(tmp_path):
    proc, outputs = run_block(
        step_run(NOTIFY_YAML, "Resolve version"),
        {
            "EVENT": "release",
            "RELEASE_TAG": "v1.2.3",
            "IS_PRERELEASE": "false",
            "RELEASE_BODY": f"{MARKER}\n\n## What's Changed\n* something",
            "INPUT_VERSION": "",
        },
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert outputs.get("skip") == "true", f"silent release still alerted: {outputs}"


@needs_bash
def test_release_event_without_marker_alerts(tmp_path):
    """The normal release. A regression here means nobody ever gets told."""
    proc, outputs = run_block(
        step_run(NOTIFY_YAML, "Resolve version"),
        {
            "EVENT": "release",
            "RELEASE_TAG": "v1.2.3",
            "IS_PRERELEASE": "false",
            "RELEASE_BODY": "## What's Changed\n* something",
            "INPUT_VERSION": "",
        },
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert outputs.get("skip") == "false"
    assert outputs.get("version") == "v1.2.3"


@needs_bash
def test_empty_release_body_alerts(tmp_path):
    """A release with no notes at all is a normal release, not a silent one."""
    proc, outputs = run_block(
        step_run(NOTIFY_YAML, "Resolve version"),
        {
            "EVENT": "release",
            "RELEASE_TAG": "v1.2.3",
            "IS_PRERELEASE": "false",
            "RELEASE_BODY": "",
            "INPUT_VERSION": "",
        },
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert outputs.get("skip") == "false"


@needs_bash
def test_manual_dispatch_ignores_the_marker(tmp_path):
    """Running notify-release.yaml by hand is how you announce a silent release.

    The marker must not follow the version around and veto that — the manual
    path never looks at the release body.
    """
    proc, outputs = run_block(
        step_run(NOTIFY_YAML, "Resolve version"),
        {
            "EVENT": "workflow_dispatch",
            "RELEASE_TAG": "",
            "IS_PRERELEASE": "",
            "RELEASE_BODY": MARKER,
            "INPUT_VERSION": "v1.2.3",
        },
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert outputs.get("skip") == "false"
    assert outputs.get("version") == "v1.2.3"


# --------------------------------------------------------------------------
# Sending end — release.yaml writes (or doesn't write) the marker
# --------------------------------------------------------------------------


def create_release_script(version="v1.2.3"):
    script = step_run(RELEASE_YAML, "Create GitHub release with auto-generated notes")
    # The step interpolates the resolved version straight into the run block;
    # the runner substitutes it before bash ever sees it, so we do the same.
    # Substituting by pattern rather than by the exact expression keeps this
    # working across renames — the version moved from `inputs.version` to
    # `steps.ver.outputs.version` when the bump selector replaced the typed
    # box, and a literal replace would have silently stopped matching and
    # left bash a `${{ ... }}` it cannot expand.
    substituted = re.sub(r"\$\{\{[^}]*\}\}", version, script)
    assert "${{" not in substituted, f"unsubstituted expression left in: {substituted}"
    return substituted


@pytest.fixture
def stub_gh(tmp_path):
    """A `gh` on PATH that records its argv, one argument per line."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    argv_log = tmp_path / "gh_argv.txt"
    gh = bindir / "gh"
    gh.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$GH_ARGV_LOG"\n')
    gh.chmod(0o755)
    return {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "GH_ARGV_LOG": str(argv_log)}, argv_log


@needs_bash
def test_silent_release_passes_the_marker_to_gh(stub_gh, tmp_path):
    env, argv_log = stub_gh
    env = {**env, "SILENT": "true", "GH_TOKEN": "stub"}
    proc, _ = run_block(create_release_script(), env, tmp_path)
    assert proc.returncode == 0, proc.stderr

    argv = argv_log.read_text().splitlines()
    assert "--notes" in argv, f"silent release did not mark the body: {argv}"
    assert argv[argv.index("--notes") + 1] == MARKER
    # --generate-notes must survive alongside --notes: GitHub prepends the
    # given body to the generated changelog rather than replacing it, so a
    # silent release is not a release with no notes.
    assert "--generate-notes" in argv


@needs_bash
def test_normal_release_passes_no_notes_and_no_empty_arg(stub_gh, tmp_path):
    env, argv_log = stub_gh
    env = {**env, "SILENT": "false", "GH_TOKEN": "stub"}
    proc, _ = run_block(create_release_script(), env, tmp_path)
    assert proc.returncode == 0, proc.stderr

    argv = argv_log.read_text().splitlines()
    assert "--notes" not in argv
    assert MARKER not in argv
    assert "" not in argv, f"empty array expansion leaked a bare '' argument: {argv}"
    assert argv[:2] == ["release", "create"]
    assert "--generate-notes" in argv
