#!/usr/bin/env -S bash --login
set -euo pipefail
# MAAP DPS run script (VEDA Black Marble, NOAA-20 / VJ146A2) -- OGC/CWL schema.
#
# Registered as `disasters-blackmarble-noaa-process`. Deliberately a THIN WRAPPER: the
# NOAA-20 job is the same pipeline, the same inputs, the same validation and the same
# output flow as the Suomi-NPP job -- only the VIIRS product it downloads differs
# (VJ146A2 instead of VNP46A2). Copying dps/blackmarble/run.sh here would be ~200 lines
# of duplicated orchestration guaranteed to drift, so the platform is selected by
# BM_PLATFORM and the real script is exec'd.
#
# WHY A SEPARATE ALGORITHM RATHER THAN AN INPUT: Suomi-NPP data product delivery ceases
# 2026-11-01 (disasters-portal#365) and the two products have different coverage windows
# (VNP46A2 from 2012-01-19, VJ146A2 from 2018-01-19), so which satellite an activation
# ran on is a property worth carrying in the algorithm identity, the S3 product folder and
# the COG's own tags -- not a checkbox that is easy to leave wrong at submit time.
#
# exec (not source/call) so signals, exit status and $0 all belong to the real script:
# dps/blackmarble/run.sh derives basedir from $0, and exec'ing hands it its own path, so
# ../_validate.sh, ../_finalize.sh, platform.sh and naming.sh resolve normally.

export BM_PLATFORM=noaa20
exec "$(dirname "$(readlink -f "$0")")/../blackmarble/run.sh" "$@"
