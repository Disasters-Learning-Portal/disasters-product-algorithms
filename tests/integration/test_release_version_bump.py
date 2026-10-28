"""Pins the version arithmetic in release.yaml's "Resolve next version" step.

The version used to be typed into the Run-workflow form. It is now derived:
you pick patch / minor / major, and the workflow reads the highest existing
vX.Y.Z tag and does the arithmetic. That removes the "what was the last tag
again?" lookup, but it moves the risk into a bash block that only ever runs
while cutting a real release — so it is exercised here instead.

The step's `run:` block is extracted from the YAML and executed against
throwaway git repositories with real tags, rather than being reimplemented in
Python. A reimplementation would pin the test's idea of the arithmetic, not
the workflow's.

The cases that actually bite:

* **v1.0.9 → v1.0.10.** A lexical `sort` puts v1.0.9 above v1.0.10, so the
  tenth patch of a series would renumber backwards and collide. Hence
  `sort -V`.
* **A tag on an unmerged branch.** `git describe --tags --abbrev=0` returns
  the nearest tag reachable from HEAD, which can be several versions behind
  the highest tag that exists. Hence sorting all tags rather than describing.
* **A bump value that isn't patch/minor/major.** Unreachable from the UI
  (`type: choice`), but the API and `gh workflow run` will post anything.
  Defaulting to a patch bump would publish a release whose number
  misdescribes it.
"""
import os
import shutil
import subprocess

import pytest

yaml = pytest.importorskip("yaml")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RELEASE_YAML = os.path.join(REPO, ".github", "workflows", "release.yaml")

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def resolve_step():
    """The `run:` script of the version-resolving step."""
    with open(RELEASE_YAML) as fh:
        doc = yaml.safe_load(fh)
    for job in doc["jobs"].values():
        for step in job["steps"]:
            if step.get("name") == "Resolve next version":
                return step["run"]
    raise AssertionError("no 'Resolve next version' step in release.yaml")


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo_with_tags(tmp_path):
    """Build a throwaway repo, tag it, and run the step's bash inside it.

    An empty bare repo stands in for `origin` so the step's `git fetch --tags`
    succeeds without reaching the network.
    """
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    git("config", "user.email", "test@example.com", cwd=work)
    git("config", "user.name", "Test", cwd=work)
    git("remote", "add", "origin", str(origin), cwd=work)
    (work / "f.txt").write_text("x")
    git("add", "f.txt", cwd=work)
    git("commit", "-qm", "initial", cwd=work)

    def run(tags, bump):
        for tag in tags:
            git("tag", tag, cwd=work)
        env = dict(os.environ)
        env["BUMP"] = bump
        env["GITHUB_OUTPUT"] = str(tmp_path / "out")
        env["GITHUB_STEP_SUMMARY"] = str(tmp_path / "summary")
        (tmp_path / "out").write_text("")
        proc = subprocess.run(
            ["bash", "-e", "-c", resolve_step()],
            cwd=work, capture_output=True, text=True, env=env,
        )
        version = None
        for line in (tmp_path / "out").read_text().splitlines():
            if line.startswith("version="):
                version = line.partition("=")[2]
        return proc, version

    return run


# --------------------------------------------------------------------------
# The arithmetic
# --------------------------------------------------------------------------


@needs_git
@pytest.mark.parametrize(
    "tags,bump,expected",
    [
        (["v1.0.3"], "patch", "v1.0.4"),
        (["v1.0.3"], "minor", "v1.1.0"),
        (["v1.0.3"], "major", "v2.0.0"),
        # A minor bump zeroes the patch; a major zeroes both. Carrying the
        # old component through is the classic hand-rolled-semver bug.
        (["v1.4.7"], "minor", "v1.5.0"),
        (["v1.4.7"], "major", "v2.0.0"),
        (["v0.1.0"], "patch", "v0.1.1"),
    ],
)
def test_bump_arithmetic(repo_with_tags, tags, bump, expected):
    proc, version = repo_with_tags(tags, bump)
    assert proc.returncode == 0, proc.stderr
    assert version == expected


@needs_git
def test_tenth_patch_does_not_renumber_backwards(repo_with_tags):
    """v1.0.10 must outrank v1.0.9 — a lexical sort gets this exactly wrong."""
    proc, version = repo_with_tags(
        ["v1.0.8", "v1.0.9", "v1.0.10"], "patch"
    )
    assert proc.returncode == 0, proc.stderr
    assert version == "v1.0.11", "picked the wrong 'highest' tag — sort -V lost"


@needs_git
def test_highest_tag_wins_over_the_most_recent_one(repo_with_tags):
    """Tag order on disk, and ancestry, must not decide the next version.

    Tagging v2.0.0 and then v1.9.0 (a patch series continuing on an older
    line) leaves the LATEST-created tag lower than the highest.
    """
    proc, version = repo_with_tags(["v2.0.0", "v1.9.0"], "patch")
    assert proc.returncode == 0, proc.stderr
    assert version == "v2.0.1"


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


@needs_git
@pytest.mark.parametrize("bump", ["", "PATCH", "pat", "1.2.3", "major minor"])
def test_unknown_bump_is_refused(repo_with_tags, bump):
    """Never silently fall back to a patch bump — the number would lie."""
    proc, version = repo_with_tags(["v1.0.3"], bump)
    assert proc.returncode != 0, f"accepted bump={bump!r} → {version}"
    assert version is None


@needs_git
def test_bumps_from_the_highest_tag_not_the_oldest(repo_with_tags):
    """A run must continue the series, never land on a number already taken.

    (The step's explicit "tag already exists" guard is belt-and-braces for a
    race between the fetch and the push — by construction the computed
    version is always above every tag it just read.)
    """
    proc, version = repo_with_tags(["v1.0.3", "v1.0.4", "v1.0.5"], "patch")
    assert proc.returncode == 0, proc.stderr
    assert version == "v1.0.6"


@needs_git
def test_no_tags_starts_the_numbering_and_warns(repo_with_tags):
    """An empty tag list is also what a broken fetch looks like — warn loudly."""
    proc, version = repo_with_tags([], "patch")
    assert proc.returncode == 0, proc.stderr
    assert version == "v0.1.0"
    assert "::warning" in proc.stdout, proc.stdout


@needs_git
def test_unparseable_highest_tag_is_refused(repo_with_tags):
    proc, version = repo_with_tags(["v1.0.3", "v1.2.3.4"], "patch")
    assert proc.returncode != 0, f"accepted a malformed tag → {version}"


# --------------------------------------------------------------------------
# The form
# --------------------------------------------------------------------------


def test_bump_is_a_choice_with_the_three_semver_levels():
    with open(RELEASE_YAML) as fh:
        inputs = yaml.safe_load(fh)[True]["workflow_dispatch"]["inputs"]
    assert "version" not in inputs, "the typed version box should be gone"
    bump = inputs["bump"]
    assert bump["type"] == "choice", "must render as a dropdown, not a text box"
    assert bump["options"] == ["patch", "minor", "major"]
    assert bump["default"] == "patch"
