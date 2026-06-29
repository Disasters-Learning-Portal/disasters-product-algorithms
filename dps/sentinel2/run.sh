#!/usr/bin/env -S bash --login
set -euo pipefail
# MAAP DPS run script (Sentinel-2) -- OGC/CWL schema.
#
# Inputs arrive as NAMED flags via "$@". Boolean inputs may arrive as a bare
# "--flag" (presence) or "--flag true|false" (value), so the parser accepts both.
# The File input is localized by DPS to a path. Anything written to a relative
# output/ dir is uploaded to S3 by DPS.

basedir=$(dirname "$(readlink -f "$0")")
mkdir -p output

# --- defaults (booleans default false; flag presence sets them true) ---
FILE_PATH=""
ACTIVATION_EVENT="YYYYMM_Example_Event"
PRODUCTS="true swir"
SOURCE_LABEL=""
DST_CRS="native"
MERGE="false"
MASK="false"
PROCESS_DATE=""
PROCESS_TILE=""
WE_NSTD=""
COMPRESSION_LEVEL="22"
NODATA="0"
ENABLE_S3_UPLOAD="false"
S3_BUCKET="nasa-disasters"
S3_DEST_BASE="drcs_activations_new"

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
    --s3_bucket)             S3_BUCKET="$2"; shift 2;;
    --s3_dest_base)          S3_DEST_BASE="$2"; shift 2;;
    --merge)                 if [[ "${2:-}" =~ ^(true|false)$ ]]; then MERGE="$2"; shift 2; else MERGE="true"; shift; fi ;;
    --mask)                  if [[ "${2:-}" =~ ^(true|false)$ ]]; then MASK="$2"; shift 2; else MASK="true"; shift; fi ;;
    --enable_s3_upload)      if [[ "${2:-}" =~ ^(true|false)$ ]]; then ENABLE_S3_UPLOAD="$2"; shift 2; else ENABLE_S3_UPLOAD="true"; shift; fi ;;
    *) echo "WARN: ignoring unrecognized arg: $1"; shift;;
  esac
done

# --- required-input / placeholder guards ---
if [[ -z "${FILE_PATH}" ]]; then
  echo "ERROR: --file_path_of_raw_data is required (a Sentinel-2 L2A .zip granule)" >&2; exit 1
fi
if [[ "${ACTIVATION_EVENT}" == "YYYYMM_Example_Event" ]]; then
  echo "ERROR: activation_event is still the placeholder 'YYYYMM_Example_Event'. Set a real event, e.g. 202511_Flood_TX." >&2; exit 1
fi
if [[ -z "${SOURCE_LABEL}" ]]; then
  echo "ERROR: source_label is required (e.g. USGS, NASA, NOAA, Copernicus)." >&2; exit 1
fi

# --- stage the granule into an input dir (process_sentinel2 takes a directory) ---
INPUT_DIR="$(mktemp -d)/input"
mkdir -p "${INPUT_DIR}"
cp -rL "${FILE_PATH}" "${INPUT_DIR}/"

# --- activation metadata embedded as GeoTIFF tags at COG creation ---
META_JSON="${INPUT_DIR}/activation_metadata.json"
printf '{"ACTIVATION_EVENT": "%s", "SOURCE": "%s"}\n' \
  "${ACTIVATION_EVENT}" "${SOURCE_LABEL}" > "${META_JSON}"

# --- build the CLI argument list ---
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

conda run --live-stream --name disasters_dps process_sentinel2 "${args[@]}"

# --- promote products into the DPS output/ dir (CLI writes to <input>/output/) ---
cp -r "${INPUT_DIR}/output/." output/ 2>/dev/null || true

# --- optional: publish products to the operational S3 bucket ---
if [[ "${ENABLE_S3_UPLOAD}" == "true" ]]; then
  S3_PREFIX="${S3_DEST_BASE}/${ACTIVATION_EVENT}"
  echo "Publishing products to s3://${S3_BUCKET}/${S3_PREFIX}/ ..."
  conda run --live-stream --name disasters_dps python - "${S3_BUCKET}" "${S3_PREFIX}" <<'PY'
import os, sys, glob
from shared_utils import upload_file_to_s3
bucket, prefix = sys.argv[1], sys.argv[2]
cogs = sorted(glob.glob("output/**/*.tif", recursive=True))
for f in cogs:
    upload_file_to_s3(f, f"s3://{bucket}/{prefix}/{os.path.basename(f)}")
print(f"Uploaded {len(cogs)} file(s) to s3://{bucket}/{prefix}/")
PY
fi
