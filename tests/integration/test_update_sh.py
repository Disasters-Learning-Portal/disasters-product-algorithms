"""Functional tests for tools/update.sh against real throwaway git repos.

The script's whole job is to not lose someone's uncommitted work while moving
their checkout, so these drive real `git` rather than asserting on the text.

`--no-install` throughout: pip is not the behaviour under test, and a real
install would mutate the developer's environment.
"""

import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UPDATE_SH = os.path.join(REPO_ROOT, 'tools', 'update.sh')


def git(cwd, *args, check=True):
    return subprocess.run(
        ['git', *args], cwd=cwd, check=check,
        capture_output=True, text=True,
    )


def run_update(clone, *args):
    return subprocess.run(
        ['bash', os.path.join(clone, 'tools', 'update.sh'), '--no-install', *args],
        cwd=clone, capture_output=True, text=True,
    )


@pytest.fixture
def origin_and_clone(tmp_path):
    """A bare origin with a `main` and a `dev`, plus a clone of it."""
    origin = tmp_path / 'origin.git'
    work = tmp_path / 'work'
    clone = tmp_path / 'clone'

    git(tmp_path, 'init', '--bare', '--initial-branch=main', str(origin))

    git(tmp_path, 'clone', str(origin), str(work))
    git(work, 'config', 'user.email', 't@example.com')
    git(work, 'config', 'user.name', 'T')
    os.makedirs(work / 'tools', exist_ok=True)
    # The script under test must exist inside the checkout it operates on --
    # it derives REPO_ROOT from its own location.
    with open(UPDATE_SH) as fh:
        script = fh.read()
    (work / 'tools' / 'update.sh').write_text(script)
    (work / 'tracked.txt').write_text('v1\n')
    git(work, 'add', '-A')
    git(work, 'commit', '-m', 'initial')
    git(work, 'push', 'origin', 'main')
    git(work, 'checkout', '-b', 'dev')
    git(work, 'push', 'origin', 'dev')
    git(work, 'checkout', 'main')

    git(tmp_path, 'clone', str(origin), str(clone))
    git(clone, 'config', 'user.email', 't@example.com')
    git(clone, 'config', 'user.name', 'T')
    return work, clone


def advance_origin(work, text='v2\n', branch='main'):
    git(work, 'checkout', branch)
    (work / 'tracked.txt').write_text(text)
    git(work, 'commit', '-am', f'advance {branch}')
    git(work, 'push', 'origin', branch)


class TestUpdateScript:
    def test_syntax_is_valid(self):
        assert subprocess.run(['bash', '-n', UPDATE_SH]).returncode == 0

    def test_fast_forwards_to_new_origin_commit(self, origin_and_clone):
        work, clone = origin_and_clone
        advance_origin(work)
        res = run_update(clone)
        assert res.returncode == 0, res.stderr
        assert (clone / 'tracked.txt').read_text() == 'v2\n'

    def test_uncommitted_change_survives_the_update(self, origin_and_clone):
        """The core promise: local edits are still there afterwards."""
        work, clone = origin_and_clone
        advance_origin(work)
        (clone / 'mine.txt').write_text('my work\n')
        res = run_update(clone)
        assert res.returncode == 0, res.stderr
        assert (clone / 'mine.txt').read_text() == 'my work\n'
        assert (clone / 'tracked.txt').read_text() == 'v2\n'
        # Nothing of ours should be left parked in the stash.
        assert git(clone, 'stash', 'list').stdout.strip() == ''

    def test_a_pre_existing_stash_is_never_popped(self, origin_and_clone):
        """Why the run marker exists. Popping stash@{0} blindly would restore
        the user's UNRELATED older stash and silently drop ours."""
        work, clone = origin_and_clone
        (clone / 'tracked.txt').write_text('someone elses stashed work\n')
        git(clone, 'stash', 'push', '-m', 'unrelated older stash')
        assert len(git(clone, 'stash', 'list').stdout.strip().splitlines()) == 1

        advance_origin(work)
        (clone / 'mine.txt').write_text('my work\n')
        res = run_update(clone)
        assert res.returncode == 0, res.stderr

        assert (clone / 'mine.txt').read_text() == 'my work\n'
        remaining = git(clone, 'stash', 'list').stdout
        assert 'unrelated older stash' in remaining, "pre-existing stash was consumed"
        assert 'update.sh autostash' not in remaining, "our autostash was left behind"

    def test_switches_branch_and_can_target_dev(self, origin_and_clone):
        work, clone = origin_and_clone
        git(clone, 'checkout', 'dev')
        res = run_update(clone)  # default --branch main
        assert res.returncode == 0, res.stderr
        assert git(clone, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip() == 'main'

        res = run_update(clone, '--branch', 'dev')
        assert res.returncode == 0, res.stderr
        assert git(clone, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip() == 'dev'

    def test_diverged_branch_fails_loudly_and_returns_the_work(self, origin_and_clone):
        """A non-fast-forwardable branch needs a human -- but the user's
        uncommitted work must still come back, not stay trapped in a stash."""
        work, clone = origin_and_clone
        advance_origin(work)
        # Local commit on main that origin does not have -> divergence.
        (clone / 'tracked.txt').write_text('local divergent\n')
        git(clone, 'commit', '-am', 'local only')
        (clone / 'mine.txt').write_text('my work\n')

        res = run_update(clone)
        assert res.returncode != 0
        assert 'diverged' in res.stderr
        assert (clone / 'mine.txt').read_text() == 'my work\n', "work not restored on failure"

    def test_rejects_unknown_argument(self, origin_and_clone):
        _, clone = origin_and_clone
        res = run_update(clone, '--bogus')
        assert res.returncode == 2
        assert 'unknown argument' in res.stderr
