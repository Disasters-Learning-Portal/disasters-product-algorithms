#!/usr/bin/env -S bash --login
set -euo pipefail
# MAAP DPS run script (Capella) -- OGC/CWL schema.
#
# Inputs arrive as NAMED flags via "$@". Boolean inputs may arrive as a bare
# "--flag" (presence) or "--flag true|false" (value), so the parser accepts both.
# NO file input: process_capella FETCHES source rasters from the CSDA Capella
# vendor bucket at run time (DPS-worker read access required, confirmed available).
#
# Output flow: products are written to ~/drcs_outputs/<event>/, copied to output/
# (which DPS uploads -- the safety net), published to s3://nasa-disasters-staging
# (via MAAP workspace credentials), then the COGs are deleted from ~/drcs_outputs
# to free space (the output/ copy is kept). No PNG quicklooks are produced.

basedir=$(dirname "$(readlink -f "$0")")
mkdir -p output

# shared input validators (fail fast, before conda/fetch)
source "${basedir}/../_validate.sh"

# --- defaults (boolean defaults MIRROR algorithm_config.yaml so an input left
# at its form default round-trips correctly whether or not MAAP re-emits the
# flag; --flag or --flag true|false overrides) ---
DATE=""
FILTER_SIZE="5"
ACTIVATION_EVENT="YYYYMM_Hazard_Location"
SOURCE_LABEL=""
# product / bucket / prefix / dst_crs / compression_level / nodata are HARDCODED
# in process_capella.py and are no longer job inputs or flags -- Capella has one
# calibration product and one vendor bucket, and every activation wants the same
# COG encoding. Speckle filtering is likewise always on; only the kernel size is
# tunable. See .clinerules.md rule 37.
# Publishing is ALWAYS ON and the S3 destination is LOCKED for this version --
# neither is a job input nor parsed from a flag. Capella publishes to the MAAP
# staging bucket nasa-disasters-staging (prefix dps_output/<event>/) using short-
# lived MAAP workspace credentials -- the DPS worker's own role can't write there;
# see shared_utils/staging_upload.py + dps/_finalize.sh step 3a. To target a
# different bucket/prefix, publish a new algorithm_version with these constants changed.
ENABLE_S3_UPLOAD="true"
STAGING_UPLOAD="true"
STAGING_BUCKET="nasa-disasters-staging"
STAGING_DEST_BASE="dps_output"
# DELETE_COG is likewise LOCKED (not a job input / flag): after upload the scratch
# COG in ~/drcs_outputs is always removed to free worker disk -- the product already
# lives in nasa-disasters-staging and the DPS output/ bucket, so nothing is lost.
DELETE_COG="true"

# --- parse named flags ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --date)              DATE="$2"; shift 2;;
    --filter_size)       FILTER_SIZE="$2"; shift 2;;
    --activation_event)  ACTIVATION_EVENT="$2"; shift 2;;
    --source_label)      SOURCE_LABEL="$2"; shift 2;;
    *) echo "WARN: ignoring unrecognized arg: $1"; shift;;
  esac
done

# --- input validation (fail fast with a clear message; nothing has run yet) ---
require_nonempty date "${DATE}" "YYYYMMDDHHMMSS, to select a Capella scene"
validate_regex date "${DATE}" '^[0-9]{14}$' 'YYYYMMDDHHMMSS'
validate_activation_event "${ACTIVATION_EVENT}"
require_nonempty source_label "${SOURCE_LABEL}" "e.g. USGS, NASA, NOAA, Capella Space"
# product / bucket / prefix / dst_crs / compression_level / nodata are hardcoded
# in process_capella.py, so their validators no longer run for Capella (same as
# Satellogic since PR #45). filter_size is the one tunable left: the CLI pins it
# with argparse choices=[3,5,7], and validating here too fails the job fast with
# a readable message instead of an argparse traceback.
validate_in_set filter_size "${FILTER_SIZE}" "3 5 7"

OUT_HOME="${HOME}/drcs_outputs/${ACTIVATION_EVENT}"
mkdir -p "${OUT_HOME}"

# --- activation metadata embedded as GeoTIFF tags at COG creation ---
META_JSON="$(mktemp -d)/activation_metadata.json"
printf '{"ACTIVATION_EVENT": "%s", "SOURCE": "%s"}\n' \
  "${ACTIVATION_EVENT}" "${SOURCE_LABEL}" > "${META_JSON}"

# --- run the processor (writes the COG into OUT_HOME) ---
args=( --date "${DATE}"
       --output "${OUT_HOME}"
       --filter_size "${FILTER_SIZE}"
       --metadata-json "${META_JSON}" )

conda run --live-stream --name disasters_dps process_capella "${args[@]}"

# --- shared output handling (png -> output/ -> S3 -> delete COG) ---
source "${basedir}/../_finalize.sh"
