#!/usr/bin/env -S bash --login
set -euo pipefail
# MAAP DPS run script (Capella) -- OGC/CWL schema.
#
# Inputs arrive as NAMED flags via "$@". Boolean inputs may arrive as a bare
# "--flag" (presence) or "--flag true|false" (value), so the parser accepts both.
# NO file input: process_capella FETCHES source rasters from the CSDA Capella
# vendor bucket at run time (DPS-worker read access required, confirmed available).
#
# Output flow: products are written to ~/drcs_outputs/<event>/, optionally given
# a PNG quicklook, copied to output/ (which DPS uploads -- the safety net),
# optionally published to s3://nasa-disasters, then the COGs are deleted from
# ~/drcs_outputs (default) to free space (PNGs + the output/ copy are kept).

basedir=$(dirname "$(readlink -f "$0")")
mkdir -p output

# shared input validators (fail fast, before conda/fetch)
source "${basedir}/../_validate.sh"

# --- defaults (boolean defaults MIRROR algorithm_config.yaml so an input left
# at its form default round-trips correctly whether or not MAAP re-emits the
# flag; --flag or --flag true|false overrides) ---
LIST_DATES="false"
DATE=""
PRODUCT="sigma"
BUCKET="csdap-capellaspace-delivery"
PREFIX="disasters"
APPLY_FILTER="false"
FILTER_SIZE="5"
DST_CRS="native"
ACTIVATION_EVENT="YYYYMM_Hazard_Location"
SOURCE_LABEL=""
COMPRESSION_LEVEL="22"
NODATA=""
ENABLE_S3_UPLOAD="false"
# S3 destination is LOCKED for this version: not exposed as a job input and not
# parsed from flags, so operators cannot change it. To target a different
# bucket/prefix, publish a new algorithm_version with these two values changed.
S3_BUCKET="nasa-disasters"
S3_DEST_BASE="drcs_activations_new"
SAVE_PNG="true"
PNG_MIN=""
PNG_MAX=""
DELETE_COG="true"

# --- parse named flags ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --list_dates)        if [[ "${2:-}" =~ ^(true|false)$ ]]; then LIST_DATES="$2"; shift 2; else LIST_DATES="true"; shift; fi ;;
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
    --png_min)           PNG_MIN="$2"; shift 2;;
    --png_max)           PNG_MAX="$2"; shift 2;;
    --apply_filter)      if [[ "${2:-}" =~ ^(true|false)$ ]]; then APPLY_FILTER="$2"; shift 2; else APPLY_FILTER="true"; shift; fi ;;
    --enable_s3_upload)  if [[ "${2:-}" =~ ^(true|false)$ ]]; then ENABLE_S3_UPLOAD="$2"; shift 2; else ENABLE_S3_UPLOAD="true"; shift; fi ;;
    --save_png)          if [[ "${2:-}" =~ ^(true|false)$ ]]; then SAVE_PNG="$2"; shift 2; else SAVE_PNG="true"; shift; fi ;;
    --delete_cog)        if [[ "${2:-}" =~ ^(true|false)$ ]]; then DELETE_COG="$2"; shift 2; else DELETE_COG="true"; shift; fi ;;
    *) echo "WARN: ignoring unrecognized arg: $1"; shift;;
  esac
done

# --- report mode: list available vendor scene dates (most recently added to S3
# first) and exit, WITHOUT processing. Runs before input validation because
# date/activation_event/source_label aren't needed just to discover scenes. ---
if [[ "${LIST_DATES}" == "true" ]]; then
  echo "Listing available Capella scenes in s3://${BUCKET}/${PREFIX} (most recently added to S3 first)..."
  conda run --live-stream --name disasters_dps process_capella \
    --list_dates --bucket "${BUCKET}" --prefix "${PREFIX}"
  exit 0
fi

# --- input validation (fail fast with a clear message; nothing has run yet) ---
require_nonempty date "${DATE}" "YYYYMMDDHHMMSS, to select a Capella scene"
validate_regex date "${DATE}" '^[0-9]{14}$' 'YYYYMMDDHHMMSS'
validate_activation_event "${ACTIVATION_EVENT}"
require_nonempty source_label "${SOURCE_LABEL}" "e.g. USGS, NASA, NOAA, Capella Space"
validate_dst_crs "${DST_CRS}"
validate_int_range compression_level "${COMPRESSION_LEVEL}" 1 22
# --product ('sigma') is already enforced by argparse choices= in the CLI.
[[ "${APPLY_FILTER}" == "true" ]] && validate_int_range filter_size "${FILTER_SIZE}" 1 101
[[ -n "${NODATA}"  ]] && validate_number nodata  "${NODATA}"
[[ -n "${PNG_MIN}" ]] && validate_number png_min "${PNG_MIN}"
[[ -n "${PNG_MAX}" ]] && validate_number png_max "${PNG_MAX}"

OUT_HOME="${HOME}/drcs_outputs/${ACTIVATION_EVENT}"
mkdir -p "${OUT_HOME}"

# --- activation metadata embedded as GeoTIFF tags at COG creation ---
META_JSON="$(mktemp -d)/activation_metadata.json"
printf '{"ACTIVATION_EVENT": "%s", "SOURCE": "%s"}\n' \
  "${ACTIVATION_EVENT}" "${SOURCE_LABEL}" > "${META_JSON}"

# --- run the processor (writes the COG into OUT_HOME) ---
args=( --date "${DATE}"
       --product "${PRODUCT}"
       --bucket "${BUCKET}"
       --prefix "${PREFIX}"
       --output "${OUT_HOME}"
       -dst_crs "${DST_CRS}"
       -compression_level "${COMPRESSION_LEVEL}"
       --metadata-json "${META_JSON}" )
[[ "${APPLY_FILTER}" == "true" ]] && args+=( --apply_filter --filter_size "${FILTER_SIZE}" )
[[ -n "${NODATA}" ]]              && args+=( -nodata "${NODATA}" )

conda run --live-stream --name disasters_dps process_capella "${args[@]}"

# --- shared output handling (png -> output/ -> S3 -> delete COG) ---
source "${basedir}/../_finalize.sh"
