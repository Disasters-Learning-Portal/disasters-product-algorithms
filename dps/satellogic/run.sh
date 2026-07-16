#!/usr/bin/env -S bash --login
set -euo pipefail
# MAAP DPS run script (Satellogic) -- OGC/CWL schema.
#
# Inputs arrive as NAMED flags via "$@". Boolean inputs may arrive as a bare
# "--flag" (presence) or "--flag true|false" (value), so the parser accepts both.
# NO file input: process_satellogic FETCHES source rasters from the CSDA vendor
# bucket s3://csda-data-vendor-satellogic (prefix 'disasters') at run time -- the
# bucket/prefix are hardcoded in the CLI (not flags). DPS-worker read access
# required (confirmed available).
#
# Output flow handled by dps/_finalize.sh: ~/drcs_outputs -> PNG -> output/ -> S3
# -> delete COG.

basedir=$(dirname "$(readlink -f "$0")")
mkdir -p output

# --- defaults (boolean defaults MIRROR algorithm_config.yaml so an input left
# at its form default round-trips correctly whether or not MAAP re-emits the
# flag; --flag or --flag true|false overrides) ---
DATE=""
PRODUCT="truecolor"
LEVEL="L1D"
BUCKET="csda-data-vendor-satellogic"   # informational; CLI hardcodes this
PREFIX="disasters"                      # informational; CLI hardcodes this
USE_MASK="false"
VISUALIZE="false"
GAMMA="0.7"
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
    --date)              DATE="$2"; shift 2;;
    --product)           PRODUCT="$2"; shift 2;;
    --level)             LEVEL="$2"; shift 2;;
    --bucket)            BUCKET="$2"; shift 2;;
    --prefix)            PREFIX="$2"; shift 2;;
    --gamma)             GAMMA="$2"; shift 2;;
    --dst_crs)           DST_CRS="$2"; shift 2;;
    --activation_event)  ACTIVATION_EVENT="$2"; shift 2;;
    --source_label)      SOURCE_LABEL="$2"; shift 2;;
    --compression_level) COMPRESSION_LEVEL="$2"; shift 2;;
    --nodata)            NODATA="$2"; shift 2;;
    --png_min)           PNG_MIN="$2"; shift 2;;
    --png_max)           PNG_MAX="$2"; shift 2;;
    --use_mask)          if [[ "${2:-}" =~ ^(true|false)$ ]]; then USE_MASK="$2"; shift 2; else USE_MASK="true"; shift; fi ;;
    --visualize)         if [[ "${2:-}" =~ ^(true|false)$ ]]; then VISUALIZE="$2"; shift 2; else VISUALIZE="true"; shift; fi ;;
    --enable_s3_upload)  if [[ "${2:-}" =~ ^(true|false)$ ]]; then ENABLE_S3_UPLOAD="$2"; shift 2; else ENABLE_S3_UPLOAD="true"; shift; fi ;;
    --save_png)          if [[ "${2:-}" =~ ^(true|false)$ ]]; then SAVE_PNG="$2"; shift 2; else SAVE_PNG="true"; shift; fi ;;
    --delete_cog)        if [[ "${2:-}" =~ ^(true|false)$ ]]; then DELETE_COG="$2"; shift 2; else DELETE_COG="true"; shift; fi ;;
    *) echo "WARN: ignoring unrecognized arg: $1"; shift;;
  esac
done

# --- required-input / placeholder guards ---
if [[ -z "${DATE}" ]]; then
  echo "ERROR: --date is required ('YYYY-MM-DD HH:MM:SS') to select a Satellogic scene" >&2; exit 1
fi
if [[ "${ACTIVATION_EVENT}" == "YYYYMM_Hazard_Location" ]]; then
  echo "ERROR: activation_event is still the placeholder 'YYYYMM_Hazard_Location'. Set a real event, e.g. 202511_Flood_TX." >&2; exit 1
fi
if [[ -z "${SOURCE_LABEL}" ]]; then
  echo "ERROR: source_label is required (e.g. USGS, NASA, NOAA, Satellogic)." >&2; exit 1
fi

echo "INFO: vendor source = s3://${BUCKET}/${PREFIX} (read by process_satellogic; AWS read access required)"

OUT_HOME="${HOME}/drcs_outputs/${ACTIVATION_EVENT}"
mkdir -p "${OUT_HOME}"

# --- activation metadata embedded as GeoTIFF tags at COG creation ---
META_JSON="$(mktemp -d)/activation_metadata.json"
printf '{"ACTIVATION_EVENT": "%s", "SOURCE": "%s"}\n' \
  "${ACTIVATION_EVENT}" "${SOURCE_LABEL}" > "${META_JSON}"

# --- run the processor (writes the COG into OUT_HOME) ---
args=( --product "${PRODUCT}"
       --date "${DATE}"
       --level "${LEVEL}"
       --output "${OUT_HOME}"
       -dst_crs "${DST_CRS}"
       -compression_level "${COMPRESSION_LEVEL}"
       --gamma "${GAMMA}"
       --metadata-json "${META_JSON}" )
[[ "${USE_MASK}"  == "true" ]] && args+=( --use_mask )
[[ "${VISUALIZE}" == "true" ]] && args+=( --visualize )
[[ -n "${NODATA}" ]]          && args+=( -nodata "${NODATA}" )

conda run --live-stream --name disasters_dps process_satellogic "${args[@]}"

# --- shared output handling (png -> output/ -> S3 -> delete COG) ---
source "${basedir}/../_finalize.sh"
