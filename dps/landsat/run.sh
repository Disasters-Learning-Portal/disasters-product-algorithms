#!/usr/bin/env -S bash --login
set -euo pipefail
# MAAP DPS run script (Landsat). Called by DPS per job.
#
# DPS contract:
#   - declared "file" inputs are downloaded into a relative input/ dir
#   - "positional" inputs arrive as $1 $2 ... in registration order
#   - everything written to a relative output/ dir is uploaded to S3
#   - stdout/stderr are captured automatically

basedir=$(dirname "$(readlink -f "$0")")
mkdir -p output
INPUT_DIR=input                 # DPS downloaded the granule archive(s) here

# --- Positional inputs (must match algorithm_config.yaml order) ---
ACTIVATION_EVENT="$1"           # e.g. 202512_Flood_WA
PRODUCTS="$2"                   # space-separated list, e.g. "true ndvi" (or "all")
DST_CRS="$3"                    # EPSG:3857 (VEDA) | EPSG:4326 | native
SOURCE_LABEL="$4"               # SOURCE GeoTIFF tag value

# --- Activation-event metadata embedded as GeoTIFF tags at COG creation ---
# Written inside input/ so DPS does NOT upload it as a product (only output/ is
# uploaded). PROCESSOR is auto-stamped from shared_utils.version when omitted.
META_JSON="${INPUT_DIR}/activation_metadata.json"
printf '{"ACTIVATION_EVENT": "%s", "SOURCE": "%s"}\n' \
  "${ACTIVATION_EVENT}" "${SOURCE_LABEL}" > "${META_JSON}"

# --- Run the processor ---
# process_landsat89 takes the input dir positionally, unpacks to
# input/unpacked/, and writes product COGs to input/output/.
# shellcheck disable=SC2086  # intentional word-split of PRODUCTS into nargs='*'
conda run --live-stream --name disasters_dps \
  process_landsat89 "${INPUT_DIR}" \
    -p ${PRODUCTS} \
    -dst_crs "${DST_CRS}" \
    -event "${ACTIVATION_EVENT}" \
    -merge \
    --metadata-json "${META_JSON}"

# --- Promote products into the DPS output/ dir for S3 upload ---
# The CLI wrote COGs to input/output/ (process_landsat89.py:178). Without this
# copy the job "succeeds" but uploads nothing.
cp -r "${INPUT_DIR}/output/." output/ 2>/dev/null || true
