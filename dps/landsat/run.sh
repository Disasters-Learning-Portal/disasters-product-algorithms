#!/usr/bin/env -S bash --login
set -euo pipefail
# MAAP DPS run script (Landsat) -- OGC/CWL schema.
#
# Inputs arrive as NAMED flags via "$@". Boolean inputs may arrive as a bare
# "--flag" (presence) or "--flag true|false" (value), so the parser accepts both.
# The File input is localized by DPS to a path.
#
# Output flow handled by dps/_finalize.sh: ~/drcs_outputs -> output/ -> S3
# (nasa-disasters-staging, via MAAP workspace credentials) -> delete COG.

basedir=$(dirname "$(readlink -f "$0")")
mkdir -p output

# shared input validators (fail fast, before conda/staging)
source "${basedir}/../_validate.sh"

# --- defaults (boolean defaults MIRROR algorithm_config.yaml so an input left
# at its form default round-trips correctly whether or not MAAP re-emits the
# flag; --flag or --flag true|false overrides) ---
FILE_PATH=""
ACTIVATION_EVENT="YYYYMM_Hazard_Location"
PRODUCTS="true"
SOURCE_LABEL=""
DST_CRS="native"
MERGE="true"
MASK="true"
PROCESS_DATE=""
PROCESS_TILE=""
WE_NSTD="1 1.5"
COMPRESSION_LEVEL="22"
NODATA=""
# Publishing is ALWAYS ON and the S3 destination is LOCKED for this version --
# neither is a job input nor parsed from a flag. Landsat publishes to the MAAP
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
    --file_path_of_raw_data) FILE_PATH="$2"; shift 2;;
    --activation_event)      ACTIVATION_EVENT="$2"; shift 2;;
    --products)              PRODUCTS="$2"; shift 2;;
    --source_label)          SOURCE_LABEL="$2"; shift 2;;
    --dst_crs)               DST_CRS="$2"; shift 2;;
    --process_date)          PROCESS_DATE="$2"; shift 2;;
    --process_tile)          PROCESS_TILE="$2"; shift 2;;
    --we_nstd)               WE_NSTD="$2"; shift 2;;
    --compression_level)     COMPRESSION_LEVEL="$2"; shift 2;;
    --nodata)                NODATA="$2"; shift 2;;
    --merge)                 if [[ "${2:-}" =~ ^(true|false)$ ]]; then MERGE="$2"; shift 2; else MERGE="true"; shift; fi ;;
    --mask)                  if [[ "${2:-}" =~ ^(true|false)$ ]]; then MASK="$2"; shift 2; else MASK="true"; shift; fi ;;
    *) echo "WARN: ignoring unrecognized arg: $1"; shift;;
  esac
done

# --- input validation (fail fast with a clear message; nothing has run yet) ---
validate_granule file_path_of_raw_data "${FILE_PATH}" "tar zip"
validate_activation_event "${ACTIVATION_EVENT}"
require_nonempty source_label "${SOURCE_LABEL}" "e.g. USGS, NASA, NOAA"
validate_dst_crs "${DST_CRS}"
validate_int_range compression_level "${COMPRESSION_LEVEL}" 1 22
# optical products validated here (the CLI's own check ends in quit() -> exit 0);
# token set mirrors the accepted list in src/landsat/process_landsat89.py.
# shellcheck disable=SC2086  # intentional word-split of the space-separated list
for t in ${PRODUCTS}; do validate_in_set products "$t" \
  "all true tc truecolor pan panchromatic nat natural naturalcolor nc colorir colorinfrared cir mndwi ndvi evi ndwi nbr we waterextent"; done
[[ -n "${PROCESS_DATE}" ]] && validate_regex process_date "${PROCESS_DATE}" '^[0-9]{8}$' 'YYYYMMDD'
[[ -n "${PROCESS_TILE}" ]] && validate_regex process_tile "${PROCESS_TILE}" '^[0-9]{6}$' 'path/row e.g. 171035'
# shellcheck disable=SC2086
[[ -n "${WE_NSTD}" ]] && for n in ${WE_NSTD}; do validate_number we_nstd "$n"; done
[[ -n "${NODATA}"  ]] && validate_number nodata  "${NODATA}"

OUT_HOME="${HOME}/drcs_outputs/${ACTIVATION_EVENT}"
mkdir -p "${OUT_HOME}"

# --- stage the granule into an input dir (process_landsat89 takes a directory) ---
INPUT_DIR="$(mktemp -d)/input"
mkdir -p "${INPUT_DIR}"
cp -rL "${FILE_PATH}" "${INPUT_DIR}/"

# --- activation metadata embedded as GeoTIFF tags at COG creation ---
META_JSON="${INPUT_DIR}/activation_metadata.json"
printf '{"ACTIVATION_EVENT": "%s", "SOURCE": "%s"}\n' \
  "${ACTIVATION_EVENT}" "${SOURCE_LABEL}" > "${META_JSON}"

# --- build the CLI argument list (CLI writes products to <input_dir>/output/) ---
# shellcheck disable=SC2206  # intentional word-split of space-separated lists
args=( "${INPUT_DIR}"
       -p ${PRODUCTS}
       -dst_crs "${DST_CRS}"
       -event "${ACTIVATION_EVENT}"
       -compression_level "${COMPRESSION_LEVEL}"
       --metadata-json "${META_JSON}" )
[[ "${MERGE}" == "true" ]] && args+=( -merge )
[[ "${MASK}"  == "true" ]] && args+=( -mask )
[[ -n "${PROCESS_DATE}" ]] && args+=( -date ${PROCESS_DATE} )
[[ -n "${PROCESS_TILE}" ]] && args+=( -tile ${PROCESS_TILE} )
[[ -n "${WE_NSTD}" ]]      && args+=( -we_nstd ${WE_NSTD} )
[[ -n "${NODATA}" ]]       && args+=( -nodata "${NODATA}" )

conda run --live-stream --name disasters_dps process_landsat89 "${args[@]}"

# --- move the produced COGs into OUT_HOME, then run shared output handling ---
cp -r "${INPUT_DIR}/output/." "${OUT_HOME}/" 2>/dev/null || true
source "${basedir}/../_finalize.sh"
