#!/usr/bin/env -S bash --login
set -euo pipefail
# MAAP DPS run script (Umbra) -- OGC/CWL schema.
#
# Inputs arrive as NAMED flags via "$@". Boolean inputs may arrive as a bare
# "--flag" (presence) or "--flag true|false" (value), so the parser accepts both.
# NO file input: process_umbra FETCHES the source GEC raster from the CSDA vendor
# bucket at run time (DPS-worker read access required, confirmed available).
#
# Output flow handled by dps/_finalize.sh: ~/drcs_outputs -> output/ -> S3
# (nasa-disasters-staging, via MAAP workspace credentials) -> delete COG.

basedir=$(dirname "$(readlink -f "$0")")
mkdir -p output

# shared input validators (fail fast, before conda/fetch)
source "${basedir}/../_validate.sh"
# assume disasters-prod for CSDA vendor-bucket reads (sets READ_ROLE_ARN)
source "${basedir}/../_env.sh"

# --- defaults (boolean defaults MIRROR algorithm_config.yaml so an input left
# at its form default round-trips correctly whether or not MAAP re-emits the
# flag; --flag or --flag true|false overrides) ---
DATE=""
PRODUCT="sigma"
BUCKET="csda-data-vendor-umbra"
PREFIX="disasters"
FILTER_SIZE="5"
DST_CRS="native"
ACTIVATION_EVENT="YYYYMM_Hazard_Location"
SOURCE_LABEL=""
COMPRESSION_LEVEL="22"
NODATA=""
# Publishing is ALWAYS ON and the S3 destination is LOCKED for this version --
# neither is a job input nor parsed from a flag. Umbra publishes to the MAAP
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
    --product)           PRODUCT="$2"; shift 2;;
    --bucket)            BUCKET="$2"; shift 2;;
    --prefix)            PREFIX="$2"; shift 2;;
    --filter_size)       FILTER_SIZE="$2"; shift 2;;
    --dst_crs)           DST_CRS="$2"; shift 2;;
    --activation_event)  ACTIVATION_EVENT="$2"; shift 2;;
    --source_label)      SOURCE_LABEL="$2"; shift 2;;
    --compression_level) COMPRESSION_LEVEL="$2"; shift 2;;
    --nodata)            NODATA="$2"; shift 2;;
    *) echo "WARN: ignoring unrecognized arg: $1"; shift;;
  esac
done

# --- input validation (fail fast with a clear message; nothing has run yet) ---
require_nonempty date "${DATE}" "'YYYY-MM-DD HH:MM:SS', to select an Umbra scene"
validate_regex date "${DATE}" '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$' "'YYYY-MM-DD HH:MM:SS'"
validate_activation_event "${ACTIVATION_EVENT}"
require_nonempty source_label "${SOURCE_LABEL}" "e.g. USGS, NASA, NOAA, Umbra"
validate_dst_crs "${DST_CRS}"
validate_int_range compression_level "${COMPRESSION_LEVEL}" 1 22
# --product (sigma|beta|gamma) is already enforced by argparse choices=.
# Speckle filtering is always on; only the kernel is tunable (3, 5, or 7).
validate_in_set filter_size "${FILTER_SIZE}" "3 5 7"
[[ -n "${NODATA}"  ]] && validate_number nodata  "${NODATA}"

OUT_HOME="${HOME}/drcs_outputs/${ACTIVATION_EVENT}"
mkdir -p "${OUT_HOME}"

# --- activation metadata embedded as GeoTIFF tags at COG creation ---
META_JSON="$(mktemp -d)/activation_metadata.json"
printf '{"ACTIVATION_EVENT": "%s", "SOURCE": "%s"}\n' \
  "${ACTIVATION_EVENT}" "${SOURCE_LABEL}" > "${META_JSON}"

# --- run the processor (writes the COG into OUT_HOME) ---
args=( --product "${PRODUCT}"
       --date "${DATE}"
       --bucket "${BUCKET}"
       --prefix "${PREFIX}"
       --output "${OUT_HOME}"
       --filter_size "${FILTER_SIZE}"
       -dst_crs "${DST_CRS}"
       -compression_level "${COMPRESSION_LEVEL}"
       --metadata-json "${META_JSON}" )
[[ -n "${NODATA}" ]]             && args+=( -nodata "${NODATA}" )

conda run --live-stream --name disasters_dps process_umbra "${args[@]}"

# --- shared output handling (png -> output/ -> S3 -> delete COG) ---
source "${basedir}/../_finalize.sh"
