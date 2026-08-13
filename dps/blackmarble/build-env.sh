#!/usr/bin/env -S bash --login
set -euo pipefail
# MAAP DPS build script (VEDA Black Marble). Run ONCE per worker image build.
#
# Two installs, from two different sources:
#   1. `conda env update` creates the shared `disasters_dps` env from
#      dps/environment.yml, which pip-installs the UPSTREAM blackmarble package
#      (github.com/NASA-IMPACT/veda-black-marble) -- that is what provides the
#      `blackmarble` console script run.sh calls. It is not vendored in this repo.
#   2. `pip install "${repo_root}"` installs THIS repo. Still required even though
#      blackmarble no longer lives here: _finalize.sh imports
#      shared_utils.staging_upload to publish the output COG to S3.

# basedir = dps/blackmarble/ ; repo_root = two levels up.
basedir=$( cd "$(dirname "$0")" ; pwd -P )
repo_root=$( cd "${basedir}/../.." ; pwd -P )

# setuptools-scm guard: if DPS did a shallow / tagless clone, no version can be
# resolved and `pip install` of the repo hard-fails. Pin a fallback version so
# the install always succeeds (real tag still wins when one is reachable).
if ! git -C "${repo_root}" describe --tags >/dev/null 2>&1; then
  echo "WARN: no git tag reachable; pinning SETUPTOOLS_SCM_PRETEND_VERSION fallback"
  export SETUPTOOLS_SCM_PRETEND_VERSION_FOR_DISASTERS_PRODUCT_ALGORITHMS="0.0.0+dps"
fi

conda env update -f "${basedir}/../environment.yml"
conda run --live-stream --name disasters_dps pip install "${repo_root}"
