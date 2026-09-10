#!/usr/bin/env -S bash --login
set -euo pipefail
# MAAP DPS build script (VEDA Black Marble, NOAA-20). Run ONCE per worker image build.
#
# Identical to the Suomi-NPP job's build: same shared `disasters_dps` conda env from
# dps/environment.yml (which pip-installs the upstream `blackmarble` package), same
# `pip install` of this repo. The NOAA-20 variant adds no dependency of its own -- it
# retargets the SAME upstream package at run time via dps/blackmarble/bm_noaa.py. So
# delegate rather than keeping a second copy that can drift out of step.

exec "$(dirname "$(readlink -f "$0")")/../blackmarble/build-env.sh" "$@"
