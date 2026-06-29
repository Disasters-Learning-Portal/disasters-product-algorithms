#!/usr/bin/env -S bash --login
set -euo pipefail
# MAAP DPS run script (Satellogic) -- OGC/CWL schema.
#
# Inputs arrive as NAMED flags via "$@". Boolean inputs may arrive as a bare
# "--flag" (presence) or "--flag true|false" (value), so the parser accepts both.
# NO file input: process_satellogic FETCHES source rasters from the CSDA vendor
# bucket s3://csda-data-vendor-satellogic (prefix 'disasters') at run time -- the
# bucket/prefix are hardcoded in the CLI (not flags). The DPS-worker role needs
# read access to that bucket (confirmed available).

basedir=$(dirname "$(readlink -f "$0")")
mkdir -p output

# --- defaults (booleans default false; flag presence sets them true) ---
DATE=""
PRODUCT="truecolor"
LEVEL="L1D"
BUCKET="csda-data-vendor-satellogic"   # informational; CLI hardcodes this
PREFIX="disasters"                      # informational; CLI hardcodes this
USE_MASK="false"
VISUALIZE="false"
GAMMA="0.7"
DST_CRS="native"
ACTIVATION_EVENT="YYYYMM_Example_Event"
SOURCE_LABEL=""
COMPRESSION_LEVEL="22"
NODATA=""
ENABLE_S3_UPLOAD="false"
S3_BUCKET="nasa-disasters"
S3_DEST_BASE="drcs_activations_new"

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
    --s3_bucket)         S3_BUCKET="$2"; shift 2;;
    --s3_dest_base)      S3_DEST_BASE="$2"; shift 2;;
    --use_mask)          if [[ "${2:-}" =~ ^(true|false)$ ]]; then USE_MASK="$2"; shift 2; else USE_MASK="true"; shift; fi ;;
    --visualize)         if [[ "${2:-}" =~ ^(true|false)$ ]]; then VISUALIZE="$2"; shift 2; else VISUALIZE="true"; shift; fi ;;
    --enable_s3_upload)  if [[ "${2:-}" =~ ^(true|false)$ ]]; then ENABLE_S3_UPLOAD="$2"; shift 2; else ENABLE_S3_UPLOAD="true"; shift; fi ;;
    *) echo "WARN: ignoring unrecognized arg: $1"; shift;;
  esac
done

# --- required-input / placeholder guards ---
if [[ -z "${DATE}" ]]; then
  echo "ERROR: --date is required ('YYYY-MM-DD HH:MM:SS') to select a Satellogic scene" >&2; exit 1
fi
if [[ "${ACTIVATION_EVENT}" == "YYYYMM_Example_Event" ]]; then
  echo "ERROR: activation_event is still the placeholder 'YYYYMM_Example_Event'. Set a real event, e.g. 202511_Flood_TX." >&2; exit 1
fi
if [[ -z "${SOURCE_LABEL}" ]]; then
  echo "ERROR: source_label is required (e.g. USGS, NASA, NOAA, Satellogic)." >&2; exit 1
fi

# bucket/prefix are NOT consumable by the CLI (no flags); surface them in the log.
echo "INFO: vendor source = s3://${BUCKET}/${PREFIX} (read by process_satellogic; AWS read access required)"

# --- activation metadata embedded as GeoTIFF tags at COG creation ---
WORKDIR="$(mktemp -d)"
META_JSON="${WORKDIR}/activation_metadata.json"
printf '{"ACTIVATION_EVENT": "%s", "SOURCE": "%s"}\n' \
  "${ACTIVATION_EVENT}" "${SOURCE_LABEL}" > "${META_JSON}"

# --- build the CLI argument list ---
# process_satellogic: --double-dash for product/date/level/output/gamma; -single-dash
# for the COG knobs (-dst_crs/-compression_level/-nodata). use_mask/visualize are bare.
args=( --product "${PRODUCT}"
       --date "${DATE}"
       --level "${LEVEL}"
       --output output
       -dst_crs "${DST_CRS}"
       -compression_level "${COMPRESSION_LEVEL}"
       --gamma "${GAMMA}"
       --metadata-json "${META_JSON}" )
[[ "${USE_MASK}"  == "true" ]] && args+=( --use_mask )
[[ "${VISUALIZE}" == "true" ]] && args+=( --visualize )
[[ -n "${NODATA}" ]]          && args+=( -nodata "${NODATA}" )

conda run --live-stream --name disasters_dps process_satellogic "${args[@]}"

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
