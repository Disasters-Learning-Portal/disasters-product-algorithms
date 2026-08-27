#!/usr/bin/env bash
#
# Update this checkout to the latest released version and reinstall it.
#
#   bash tools/update.sh                 # onto main (the released version)
#   bash tools/update.sh --branch dev    # onto dev (unreleased tip)
#   bash tools/update.sh --no-install    # pull only, skip pip
#
# WHY THE REINSTALL IS NOT OPTIONAL
#   The package version is dynamic: setuptools-scm derives it from the newest
#   git tag, and shared_utils.version reads it back through
#   importlib.metadata -- which resolves at INSTALL time, not import time. A
#   bare `git pull` therefore leaves PROCESSOR_STRING (the processor tag baked
#   into every COG we publish) reporting the OLD version. Console scripts
#   (process_landsat89, summarize_raster, ...) are re-pointed here too.
#
# WHY pip IS SAFE HERE
#   [project.dependencies] deliberately excludes GDAL/rasterio/rio-cogeo --
#   those come from conda, and pip wheels would break the dylib coupling. This
#   only installs pure-Python deps, so it cannot clobber the conda geo stack.
#   (Run it inside the same env you use the tools from.)
#
# UNCOMMITTED WORK
#   Stashed before the branch switch and restored afterwards. The stash is
#   tagged with a unique marker and popped BY THAT MARKER, so a stash you
#   already had sitting there is never touched.

set -euo pipefail

# Absolute, and independent of the caller's cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

BRANCH="main"
DO_INSTALL=1

while [ $# -gt 0 ]; do
    case "$1" in
        --branch) BRANCH="${2:?--branch needs a value}"; shift 2 ;;
        --no-install) DO_INSTALL=0; shift ;;
        # Print the header comment as the help text, stopping at the first
        # non-comment line so it can't drift out of sync with a line range.
        -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' \
                       "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "update.sh: unknown argument '$1' (try --help)" >&2; exit 2 ;;
    esac
done

cd "$REPO_ROOT"

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "update.sh: $REPO_ROOT is not a git checkout" >&2
    exit 1
fi

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

ORIGINAL_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
OLD_DESC="$(git describe --tags --always 2>/dev/null || echo unknown)"

# ---------------------------------------------------------------- stash ----
# Marker is unique per run so we can find OUR stash again by name. Popping
# `stash@{0}` blindly would grab whatever the user had stashed already.
STASH_MARKER="update.sh autostash $(date -u +%Y%m%dT%H%M%SZ) $$"
STASHED=0

PRE_EXISTING="$(git stash list | wc -l | tr -d ' ')"
if [ "$PRE_EXISTING" -gt 0 ]; then
    echo "note: you have ${PRE_EXISTING} pre-existing stash entr(y/ies); this script will not touch them."
fi

# --include-untracked so a new file can't collide with one arriving in the pull.
if [ -n "$(git status --porcelain)" ]; then
    say "Stashing uncommitted changes"
    git stash push --include-untracked --message "$STASH_MARKER"
    STASHED=1
fi

# Find our stash by marker; empty if absent. Printed as a ref like stash@{2}.
our_stash_ref() {
    git stash list --format='%gd %gs' \
        | grep -F "$STASH_MARKER" \
        | head -n1 \
        | awk '{print $1}'
}

restore_stash() {
    [ "$STASHED" -eq 1 ] || return 0
    local ref
    ref="$(our_stash_ref)"
    if [ -z "$ref" ]; then
        echo "warning: could not locate the autostash ('$STASH_MARKER'); check \`git stash list\`." >&2
        return 0
    fi
    say "Restoring your uncommitted changes ($ref)"
    if git stash pop "$ref"; then
        STASHED=0
    else
        # Conflict, or the pull changed the same lines. Do NOT drop it.
        cat >&2 <<EOF

warning: your changes could not be re-applied cleanly and are STILL SAVED.
         Recover them with:
             git stash list                 # find: $STASH_MARKER
             git stash pop '$(our_stash_ref)'
         Resolve the conflicts, then continue.
EOF
        return 1
    fi
}

# Any failure past this point must still hand the user their work back.
trap 'rc=$?; restore_stash || true; exit $rc' EXIT

# ----------------------------------------------------------------- pull ----
say "Fetching origin (branches + tags)"
# --force so a moved/re-cut tag updates instead of failing; setuptools-scm
# reads the newest tag, so a stale tag means a wrong reported version.
git fetch --prune --tags --force origin

if [ "$ORIGINAL_BRANCH" != "$BRANCH" ]; then
    say "Switching $ORIGINAL_BRANCH -> $BRANCH"
    git checkout "$BRANCH"
fi

say "Pulling $BRANCH"
# --ff-only: never silently create a merge commit in someone's checkout. If it
# refuses, the local branch has diverged and that needs a human.
if ! git pull --ff-only origin "$BRANCH"; then
    cat >&2 <<EOF

error: '$BRANCH' could not be fast-forwarded -- your local branch has diverged
       from origin/$BRANCH. Inspect with:
           git log --oneline --graph origin/$BRANCH...$BRANCH
       Then reset (DISCARDS local commits on $BRANCH):
           git reset --hard origin/$BRANCH
EOF
    exit 1
fi

# -------------------------------------------------------------- install ----
if [ "$DO_INSTALL" -eq 1 ]; then
    say "Reinstalling (pip install -e .)"
    python -m pip install -e . --quiet
fi

# Restore before reporting, so the summary is the last thing on screen.
trap - EXIT
restore_stash || true

# --------------------------------------------------------------- report ----
NEW_DESC="$(git describe --tags --always 2>/dev/null || echo unknown)"
say "Done"
printf '  branch   : %s\n' "$(git rev-parse --abbrev-ref HEAD)"
printf '  was      : %s\n' "$OLD_DESC"
printf '  now      : %s\n' "$NEW_DESC"
if [ "$DO_INSTALL" -eq 1 ]; then
    printf '  version  : %s\n' \
        "$(python -c 'from shared_utils import __version__; print(__version__)' 2>/dev/null \
           || echo '(import failed -- wrong conda env? run: conda activate <env>)')"
fi
if [ "$ORIGINAL_BRANCH" != "$(git rev-parse --abbrev-ref HEAD)" ]; then
    printf '\n  you were on "%s" before this; return with: git checkout %s\n' \
        "$ORIGINAL_BRANCH" "$ORIGINAL_BRANCH"
fi
