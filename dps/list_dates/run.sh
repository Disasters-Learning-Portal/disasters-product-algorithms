#!/usr/bin/env -S bash --login
set -euo pipefail
# MAAP DPS run script (list-dates discovery) -- OGC/CWL schema.
#
# Inputs arrive as NAMED flags via "$@". This is a DISCOVERY tool: it lists the
# scene dates available in a chosen sensor's CSDA vendor S3 bucket and exits. It
# does NOT fetch rasters, process, or produce a COG -- so there is NO _finalize.sh
# step. The only artifact is available_<sensor>_dates.csv, written into output/
# (which DPS uploads) alongside the report printed to the job log.
#
# NO file input: the underlying process_<sensor> --list_dates path LISTS the CSDA
# vendor bucket at run time (DPS-worker read access required -- the same access
# the per-sensor processing algorithms already rely on).

basedir=$(dirname "$(readlink -f "$0")")
mkdir -p output

# shared input validators (fail fast, before conda)
source "${basedir}/../_validate.sh"

# --- defaults (MIRROR algorithm_config.yaml so an input left at its form default
# round-trips correctly whether or not MAAP re-emits the flag) ---
SENSOR="capella"
LEVEL="L1D"

# --- parse named flags ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sensor) SENSOR="$2"; shift 2;;
    --level)  LEVEL="$2"; shift 2;;
    *) echo "WARN: ignoring unrecognized arg: $1"; shift;;
  esac
done

# --- validate the sensor selector (fail fast with a clear message) ---
validate_in_set sensor "${SENSOR}" "capella umbra satellogic"

# --- dispatch: list available vendor scene dates for the chosen sensor and exit.
# Each CLI prints an aligned report (most recently added to S3 first) and writes
# output/available_<sensor>_dates.csv when passed --output output. ---
echo "Listing available ${SENSOR} scene dates (most recently added to S3 first)..."
echo "The report also lands in output/available_${SENSOR}_dates.csv (open it from the Jobs panel: Outputs -> Open in File Browser)."

case "${SENSOR}" in
  capella)
    conda run --live-stream --name disasters_dps process_capella \
      --list_dates --output output
    ;;
  umbra)
    conda run --live-stream --name disasters_dps process_umbra \
      --list_dates --output output
    ;;
  satellogic)
    # Satellogic discovery is LEVEL-SCOPED, and process_satellogic requires
    # --level, so validate and forward it (bucket/prefix are hardcoded in that
    # CLI, so nothing else is passed).
    validate_in_set level "${LEVEL}" "L1D L1B"
    conda run --live-stream --name disasters_dps process_satellogic \
      --list_dates --level "${LEVEL}" --output output
    ;;
esac
