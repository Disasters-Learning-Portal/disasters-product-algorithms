"""Pins which branch each hub-image build actually builds.

`actions/checkout` defaults to the ref the workflow was dispatched from, and
the "Use workflow from" dropdown defaults to the repo DEFAULT branch (`main`)
with no per-workflow way to change it. So before these pins, the obvious thing
to do — open the dev image build, leave the dropdown alone, hit Run — pushed
MAIN's code under the `-dev` tag. The reverse was worse: dispatching the prod
build from `dev` published dev's code as the `:latest` tag every hub pod pulls
on start.

Neither failure announced itself. The build went green, the digest changed,
and the tag simply lied about its contents — the only way to notice was to
inspect the running image. That is why this is pinned rather than left to a
comment.

The two things worth pinning hardest:

* The branches must not be SWAPPED. `dev` → `-dev` tag, `main` → `:latest`.
  A swap is a one-word edit that no other test would catch and that ships
  dev code to every operator.
* The PUSH path must keep resolving to `github.sha`. Pinning the ref to a
  bare branch name would quietly change push builds from "the commit that
  was pushed" to "whatever the tip has drifted to since", so two quick
  pushes could build the same code twice and never build the first one.
"""
import os

import pytest

yaml = pytest.importorskip("yaml")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKFLOWS = os.path.join(REPO, ".github", "workflows")

# workflow file -> the branch it must ALWAYS build
BUILDS = {
    "build-and-push-dev.yaml": "dev",
    "build-and-push.yaml": "main",
}


def load(name):
    with open(os.path.join(WORKFLOWS, name)) as fh:
        return yaml.safe_load(fh)


def checkout_ref(name):
    """The `ref:` expression on the workflow's actions/checkout step."""
    doc = load(name)
    for job in doc["jobs"].values():
        for step in job["steps"]:
            if str(step.get("uses", "")).startswith("actions/checkout"):
                return step.get("with", {}).get("ref")
    raise AssertionError(f"no actions/checkout step in {name}")


@pytest.mark.parametrize("name,branch", sorted(BUILDS.items()))
def test_manual_dispatch_is_pinned_to_the_right_branch(name, branch):
    ref = checkout_ref(name)
    assert ref is not None, (
        f"{name} does not pin `ref:` — a manual dispatch builds whatever branch "
        f"the Run-workflow dropdown was left on, and that dropdown defaults to main"
    )
    assert "github.event_name == 'workflow_dispatch'" in ref, ref
    assert f"'{branch}'" in ref, f"{name} must pin the dispatch path to {branch!r}: {ref}"


@pytest.mark.parametrize("name,branch", sorted(BUILDS.items()))
def test_push_path_still_builds_the_pushed_commit(name, branch):
    """Pinning must not regress push builds to the branch tip."""
    assert "github.sha" in checkout_ref(name), (
        f"{name} lost `github.sha` on the push path — push builds would follow "
        f"the branch tip instead of the commit that triggered them"
    )


def test_dev_and_prod_are_not_swapped():
    """The catastrophic one-word edit: prod publishing dev's code as :latest."""
    dev_ref = checkout_ref("build-and-push-dev.yaml")
    prod_ref = checkout_ref("build-and-push.yaml")
    assert "'main'" not in dev_ref, f"dev image build pins main: {dev_ref}"
    assert "'dev'" not in prod_ref, f"PROD image build pins dev: {prod_ref}"


@pytest.mark.parametrize("name,branch", sorted(BUILDS.items()))
def test_run_name_states_which_branch_was_built(name, branch):
    """The dropdown is a lie the UI keeps telling; the run title corrects it.

    GitHub renders no text in the Run-workflow dialog for an input-less
    workflow, so `run-name` is where a reader can actually be told.
    """
    run_name = load(name).get("run-name")
    assert run_name, f"{name} has no run-name stating its source branch"
    assert branch in run_name, f"{name} run-name does not name {branch!r}: {run_name}"


def test_release_form_says_it_releases_from_dev():
    """release.yaml ignores the dropdown too — it always merges origin/dev.

    An input `description` is the only text GitHub renders inside the
    Run-workflow dialog, so that is where this has to be said.
    """
    doc = load("release.yaml")
    description = doc[True]["workflow_dispatch"]["inputs"]["bump"]["description"]
    assert "dev" in description, description
    assert "main" in description, description
