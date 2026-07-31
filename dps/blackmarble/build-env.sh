#!/usr/bin/env -S bash --login
set -euo pipefail
# MAAP DPS build script (VEDA Black Marble). Run ONCE per worker image build.
# Creates the shared `disasters_dps` conda env (dps/environment.yml -- which
# carries blackmarble's isolated deps: earthaccess/osmnx/pystac-client/typer/
# obstore/duckdb) and installs this repo so the vendored `blackmarble` package
# is importable for run.sh's `conda run ... python -m blackmarble.cli`.

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
