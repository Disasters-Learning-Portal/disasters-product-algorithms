"""Pins the release-alert roster, the @mention line, and the acked/missing diff.

The logic under test lives in shell (.github/scripts/release_roster.sh and
release_alert_body.sh) rather than inline in the workflow YAML, for the same
reason dps/_validate.sh and dps/blackmarble/naming.sh were split out of their
run.sh: a workflow step can only be exercised by cutting a release.

The two things worth pinning hardest:

* The alert must carry EXPLICIT @handles. The team mention it used to carry
  (@Disasters-Learning-Portal/disasters) reached nobody on v1.0.1 — issue #120
  drew zero reactions, zero comments, and produced no notification for a
  maintainer of that team. A regression here is silent: the alert still renders,
  still opens an issue, and still notifies no one.
* An EMPTY reactions list must put the whole roster in "missing". That is the
  normal starting state for every release, and the obvious two-file awk
  `NR==FNR` idiom gets it exactly backwards (with an empty first file the roster
  populates the "seen" array and everyone reads as already acknowledged).
"""
import os
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO, ".github", "scripts")
BODY_SH = os.path.join(SCRIPTS, "release_alert_body.sh")
ROSTER_SH = os.path.join(SCRIPTS, "release_roster.sh")
ROSTER_TXT = os.path.join(REPO, ".github", "release-ack-roster.txt")

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


def run_bash(script, *args, env=None):
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", script, *args], capture_output=True, text=True, env=merged, cwd=REPO
    )


def source_and_run(snippet, env=None):
    """Source release_roster.sh, then run `snippet`."""
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail; source "{ROSTER_SH}"; {snippet}'],
        capture_output=True,
        text=True,
        env=merged,
        cwd=REPO,
    )


@pytest.fixture
def roster(tmp_path):
    """A fixture roster with mixed casing, comments, blanks and inline comments."""
    path = tmp_path / "roster.txt"
    path.write_text(
        "# a comment\n"
        "\n"
        "alice\n"
        "  BobSmith  \n"
        "carol-nasa  # inline comment\n"
        "\n"
    )
    return {"ROSTER_FILE": str(path)}


# --------------------------------------------------------------------------
# roster_handles
# --------------------------------------------------------------------------


@needs_bash
def test_roster_handles_strips_comments_blanks_and_whitespace(roster):
    r = source_and_run("roster_handles", env=roster)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["alice", "BobSmith", "carol-nasa"]


@needs_bash
def test_roster_handles_fails_on_missing_file(tmp_path):
    r = source_and_run("roster_handles", env={"ROSTER_FILE": str(tmp_path / "nope.txt")})
    assert r.returncode != 0
    assert "not found" in r.stderr


@needs_bash
def test_roster_handles_fails_on_comments_only_roster(tmp_path):
    """An empty roster would render an alert mentioning nobody and a '0/0, done' tally."""
    path = tmp_path / "roster.txt"
    path.write_text("# everyone left\n\n")
    r = source_and_run("roster_handles", env={"ROSTER_FILE": str(path)})
    assert r.returncode != 0
    assert "no handles" in r.stderr


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not available")
def test_sourcing_from_zsh_fails_loudly():
    """BASH_SOURCE is unset under zsh, and dirname "" is "." — so the roster path
    would resolve against the caller's cwd. That must error, not read some other
    file. zsh is the team's interactive shell, so this is a live footgun."""
    r = subprocess.run(
        ["zsh", "-c", f'source "{ROSTER_SH}"; roster_handles'],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert r.returncode != 0
    assert "BASH_SOURCE" in r.stderr
    assert not r.stdout.strip(), "must not emit handles from a wrongly-resolved roster"


@needs_bash
def test_real_roster_is_parseable_and_nonempty():
    r = source_and_run("roster_handles")
    assert r.returncode == 0, r.stderr
    handles = r.stdout.split()
    assert len(handles) >= 2
    assert all(not h.startswith("@") for h in handles), "roster holds bare handles, not @mentions"


# --------------------------------------------------------------------------
# roster_split
# --------------------------------------------------------------------------


def _split(tmp_path, reacted_lines, env):
    reacted = tmp_path / "reacted.txt"
    reacted.write_text("".join(f"{x}\n" for x in reacted_lines))
    acked = tmp_path / "acked.txt"
    missing = tmp_path / "missing.txt"
    r = source_and_run(
        f'roster_split "{reacted}" "{acked}" "{missing}"', env=env
    )
    assert r.returncode == 0, r.stderr
    extra = tmp_path / "acked.txt.extra"
    return (
        acked.read_text().split(),
        missing.read_text().split(),
        extra.read_text().split() if extra.exists() else [],
    )


@needs_bash
def test_no_reactions_puts_everyone_in_missing(tmp_path, roster):
    """The starting state for every release, and the case the awk idiom inverts."""
    acked, missing, extra = _split(tmp_path, [], roster)
    assert acked == []
    assert missing == ["alice", "BobSmith", "carol-nasa"]
    assert extra == []


@needs_bash
def test_split_is_case_insensitive_and_keeps_roster_casing(tmp_path, roster):
    acked, missing, _ = _split(tmp_path, ["BOBSMITH"], roster)
    assert acked == ["BobSmith"], "reported with the roster's casing, not the reactor's"
    assert missing == ["alice", "carol-nasa"]


@needs_bash
def test_split_matches_whole_handles_only(tmp_path, roster):
    """A substring must not count: 'bob' is not 'BobSmith'."""
    acked, missing, extra = _split(tmp_path, ["bob"], roster)
    assert acked == []
    assert missing == ["alice", "BobSmith", "carol-nasa"]
    assert extra == ["bob"]


@needs_bash
def test_off_roster_reactor_is_reported_as_extra(tmp_path, roster):
    acked, missing, extra = _split(tmp_path, ["alice", "dave"], roster)
    assert acked == ["alice"]
    assert missing == ["BobSmith", "carol-nasa"]
    assert extra == ["dave"]


@needs_bash
def test_everyone_reacted_leaves_missing_empty(tmp_path, roster):
    acked, missing, _ = _split(tmp_path, ["alice", "bobsmith", "CAROL-NASA"], roster)
    assert sorted(acked) == sorted(["alice", "BobSmith", "carol-nasa"])
    assert missing == []


# --------------------------------------------------------------------------
# roster_validate — the guard against a silently-dead mention
# --------------------------------------------------------------------------

# Stub checkers matching roster_check_github's exit convention
# (0 ok / 1 not a user / 2 not a collaborator), so these run with no network.
_ALL_OK = "_chk() { return 0; }"
_ALICE_UNKNOWN = '_chk() { [ "$1" = alice ] && return 1; return 0; }'
_ALICE_RC2 = '_chk() { [ "$1" = alice ] && return 2; return 0; }'


@needs_bash
def test_validate_passes_when_every_handle_resolves(roster):
    r = source_and_run(f"{_ALL_OK}; roster_validate _chk", env=roster)
    assert r.returncode == 0, r.stderr
    assert "ok    alice" in r.stdout


@needs_bash
def test_validate_fails_on_a_handle_that_is_not_a_github_user(roster):
    """The realistic typo: a mistyped handle almost never hits a real account."""
    r = source_and_run(f"{_ALICE_UNKNOWN}; roster_validate _chk", env=roster)
    assert r.returncode != 0
    assert "not a GitHub user" in r.stderr
    assert "notifies nobody" in r.stderr


@needs_bash
def test_validate_has_no_silent_warn_path(roster):
    """There used to be a `warn` tier for "real user, not a collaborator" (rc 2).
    It was removed: under the workflow's GITHUB_TOKEN the collaborator endpoint
    only resolves DIRECT collaborators, so everyone with team-derived access —
    4 of 10 handles here, including a repo admin — warned wrongly on every run.
    Any nonzero from the checker must now fail, so nothing can pass while
    printing a complaint nobody can act on."""
    r = source_and_run(f"{_ALICE_RC2}; roster_validate _chk", env=roster)
    assert r.returncode != 0
    assert "warn" not in r.stdout.lower() and "warn" not in r.stderr.lower()


@needs_bash
def test_default_checker_does_not_probe_collaborators(roster):
    """Pins the removal at the source: a collaborators API call would reintroduce
    the false warnings."""
    body = open(ROSTER_SH).read()
    fn = body.split("roster_check_github() {")[1].split("}")[0]
    assert "collaborators" not in fn


@needs_bash
def test_validate_reports_every_bad_handle_not_just_the_first(tmp_path):
    path = tmp_path / "roster.txt"
    path.write_text("alice\nbob\ncarol\n")
    r = source_and_run(
        '_chk() { [ "$1" = carol ] && return 0; return 1; }; roster_validate _chk',
        env={"ROSTER_FILE": str(path)},
    )
    assert r.returncode != 0
    assert "BAD   alice" in r.stderr and "BAD   bob" in r.stderr


@needs_bash
def test_validate_fails_on_an_unexpected_checker_exit(roster):
    r = source_and_run('_chk() { return 9; }; roster_validate _chk', env=roster)
    assert r.returncode != 0
    assert "rc=9" in r.stderr


# --------------------------------------------------------------------------
# roster_restrict — release-ack.yaml's `only` input
# --------------------------------------------------------------------------


@needs_bash
def test_restrict_picks_one_handle_case_insensitively(tmp_path):
    f = tmp_path / "missing.txt"
    f.write_text("alice\nBobSmith\ncarol-nasa\n")
    r = source_and_run(f'roster_restrict "bobsmith" "{f}"')
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["BobSmith"], "reported with the file's casing"


@needs_bash
def test_restrict_fails_on_a_handle_that_is_not_there(tmp_path):
    """A typo'd handle must fail, not mention nobody and exit green — a silent
    no-op run reads as 'mentions don't deliver' when it was a typo."""
    f = tmp_path / "missing.txt"
    f.write_text("alice\nBobSmith\n")
    r = source_and_run(f'roster_restrict "alicia" "{f}"')
    assert r.returncode != 0
    assert "not in" in r.stderr
    assert "alice" in r.stderr, "should list the candidates"


@needs_bash
def test_restrict_fails_when_the_person_already_reacted(tmp_path):
    """`only` names someone absent from missing.txt because they already acked."""
    f = tmp_path / "missing.txt"
    f.write_text("alice\n")
    r = source_and_run(f'roster_restrict "BobSmith" "{f}"')
    assert r.returncode != 0


@needs_bash
def test_restrict_does_not_match_substrings(tmp_path):
    f = tmp_path / "missing.txt"
    f.write_text("BobSmith\n")
    r = source_and_run(f'roster_restrict "bob" "{f}"')
    assert r.returncode != 0


# --------------------------------------------------------------------------
# release_alert_body.sh
# --------------------------------------------------------------------------


@needs_bash
def test_body_renders_and_names_the_version():
    r = run_bash(BODY_SH, "v1.2.3")
    assert r.returncode == 0, r.stderr
    assert "v1.2.3" in r.stdout


@needs_bash
def test_body_mentions_every_roster_handle():
    handles = source_and_run("roster_handles").stdout.split()
    body = run_bash(BODY_SH, "v1.2.3").stdout
    first_line = body.splitlines()[0]
    for h in handles:
        assert f"@{h}" in first_line, f"{h} is on the roster but not mentioned in the alert"


@needs_bash
def test_body_does_not_use_the_team_mention():
    """The team mention reached nobody on v1.0.1 (issue #120). Regression is silent."""
    body = run_bash(BODY_SH, "v1.2.3").stdout
    assert "@Disasters-Learning-Portal/disasters" not in body


@needs_bash
def test_body_asks_for_a_reaction():
    body = run_bash(BODY_SH, "v1.2.3").stdout
    assert "React to this issue" in body
    assert "👍" in body


@needs_bash
def test_body_runs_from_an_unrelated_cwd(tmp_path):
    """The roster path resolves from the script's own location, not the caller's cwd."""
    r = subprocess.run(
        ["bash", BODY_SH, "v1.2.3"], capture_output=True, text=True, cwd=str(tmp_path)
    )
    assert r.returncode == 0, r.stderr
    assert "@" in r.stdout.splitlines()[0]


@needs_bash
@pytest.mark.parametrize("bad", ["1.2.3", "v1.2", "vX.Y.Z", ""])
def test_body_rejects_a_bad_version(bad):
    r = run_bash(BODY_SH, bad) if bad else run_bash(BODY_SH)
    assert r.returncode != 0
