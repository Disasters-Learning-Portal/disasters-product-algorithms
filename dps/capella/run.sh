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

# --- defaults (booleans default false; flag presence sets them true) ---
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
S3_BUCKET="nasa-disasters"
S3_DEST_BASE="drcs_activations_new"
SAVE_PNG="false"
PNG_MIN=""
PNG_MAX=""
DELETE_COG="false"

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
    --s3_bucket)         S3_BUCKET="$2"; shift 2;;
    --s3_dest_base)      S3_DEST_BASE="$2"; shift 2;;
    --png_min)           PNG_MIN="$2"; shift 2;;
    --png_max)           PNG_MAX="$2"; shift 2;;
    --apply_filter)      if [[ "${2:-}" =~ ^(true|false)$ ]]; then APPLY_FILTER="$2"; shift 2; else APPLY_FILTER="true"; shift; fi ;;
    --enable_s3_upload)  if [[ "${2:-}" =~ ^(true|false)$ ]]; then ENABLE_S3_UPLOAD="$2"; shift 2; else ENABLE_S3_UPLOAD="true"; shift; fi ;;
    --save_png)          if [[ "${2:-}" =~ ^(true|false)$ ]]; then SAVE_PNG="$2"; shift 2; else SAVE_PNG="true"; shift; fi ;;
    --delete_cog)        if [[ "${2:-}" =~ ^(true|false)$ ]]; then DELETE_COG="$2"; shift 2; else DELETE_COG="true"; shift; fi ;;
    *) echo "WARN: ignoring unrecognized arg: $1"; shift;;
  esac
done

# --- required-input / placeholder guards ---
if [[ -z "${DATE}" ]]; then
  echo "ERROR: --date is required (YYYYMMDDHHMMSS) to select a Capella scene" >&2; exit 1
fi
if [[ "${ACTIVATION_EVENT}" == "YYYYMM_Hazard_Location" ]]; then
  echo "ERROR: activation_event is still the placeholder 'YYYYMM_Hazard_Location'. Set a real event, e.g. 202511_Flood_TX." >&2; exit 1
fi
if [[ -z "${SOURCE_LABEL}" ]]; then
  echo "ERROR: source_label is required (e.g. USGS, NASA, NOAA, Capella Space)." >&2; exit 1
fi

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
