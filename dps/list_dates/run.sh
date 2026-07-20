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
# assume disasters-prod for CSDA vendor-bucket reads (sets READ_ROLE_ARN)
source "${basedir}/../_env.sh"

# --- defaults (MIRROR algorithm_config.yaml so an input left at its form default
# round-trips correctly whether or not MAAP re-emits the flag) ---
SENSOR="capella"
LEVEL="L1D"
LEVEL_SET=""   # set when the operator explicitly passes --level (see note below)

# --- parse named flags ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sensor) SENSOR="$2"; shift 2;;
    --level)  LEVEL="$2"; LEVEL_SET=1; shift 2;;
    *) echo "WARN: ignoring unrecognized arg: $1" >&2; shift;;
  esac
done

# --- normalize + validate EVERYTHING up front, before any banner / S3 call /
# conda dispatch, so a bad selector aborts the job in ~0s and never "runs".
# normalize_token only trims whitespace and folds case (a pure transform), so
# "Capella" / " L1d " round-trip to the canonical token; a genuinely unknown
# value still falls through to validate_in_set, which dies here in this shell. ---
SENSOR="$(normalize_token "${SENSOR}" lower)"
LEVEL="$(normalize_token "${LEVEL}" upper)"

validate_in_set sensor "${SENSOR}" "capella umbra satellogic"
if [[ "${SENSOR}" == "satellogic" ]]; then
  # Satellogic discovery is LEVEL-SCOPED (process_satellogic requires --level),
  # so the level must be valid before we dispatch.
  validate_in_set level "${LEVEL}" "L1D L1B"
elif [[ -n "${LEVEL_SET}" ]]; then
  # level is meaningless for capella/umbra -- tell the operator we're ignoring a
  # value they deliberately set, rather than silently dropping it.
  echo "NOTE: 'level' only applies to satellogic; ignoring level='${LEVEL}' for ${SENSOR}." >&2
fi

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
    # Satellogic discovery is LEVEL-SCOPED; --level was already validated up
    # front. Forward the normalized value (bucket/prefix are hardcoded in that
    # CLI, so nothing else is passed).
    conda run --live-stream --name disasters_dps process_satellogic \
      --list_dates --level "${LEVEL}" --output output
    ;;
esac
