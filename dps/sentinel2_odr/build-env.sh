#!/usr/bin/env -S bash --login
set -euo pipefail
# MAAP DPS build script (Sentinel-2, STAC path). Run ONCE per worker image build.
# Creates the shared `disasters_dps` conda env and installs this repo so the
# `process_sentinel2_odr` console script is on PATH for run.sh's `conda run`.
#
# Identical to dps/sentinel2/build-env.sh -- both Sentinel-2 algorithms share one
# env and one image, and differ only by run_command. It exists separately because
# algorithm_config.yaml's build_command is per-algorithm.

# basedir = dps/sentinel2_odr/ ; repo_root = two levels up.
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
