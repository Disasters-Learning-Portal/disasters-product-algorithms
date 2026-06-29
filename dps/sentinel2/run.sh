#!/usr/bin/env -S bash --login
set -euo pipefail
# MAAP DPS run script (Sentinel-2) -- OGC/CWL schema.
#
# Inputs arrive as NAMED flags (--name value) via "$@", in the order declared in
# algorithm_config.yaml. File/Directory inputs are localized by DPS to a path.
# Anything written to a relative output/ dir is uploaded to S3.

basedir=$(dirname "$(readlink -f "$0")")
mkdir -p output

# --- defaults (mirror algorithm_config.yaml) ---
GRANULE=""
ACTIVATION_EVENT="202601_Example_Event"
PRODUCTS="true swir"
SOURCE_LABEL="Copernicus"
DST_CRS="EPSG:3857"
MERGE="true"
MASK="false"
PROCESS_DATE=""
PROCESS_TILE=""
WE_NSTD=""
COMPRESSION_LEVEL="22"
NODATA="0"

# --- parse named flags ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --granule_archive)   GRANULE="$2"; shift 2;;
    --activation_event)  ACTIVATION_EVENT="$2"; shift 2;;
    --products)          PRODUCTS="$2"; shift 2;;
    --source_label)      SOURCE_LABEL="$2"; shift 2;;
    --dst_crs)           DST_CRS="$2"; shift 2;;
    --merge)             MERGE="$2"; shift 2;;
    --mask)              MASK="$2"; shift 2;;
    --process_date)      PROCESS_DATE="$2"; shift 2;;
    --process_tile)      PROCESS_TILE="$2"; shift 2;;
    --we_nstd)           WE_NSTD="$2"; shift 2;;
    --compression_level) COMPRESSION_LEVEL="$2"; shift 2;;
    --nodata)            NODATA="$2"; shift 2;;
    *) echo "WARN: ignoring unrecognized arg: $1"; shift;;
  esac
done

# --- stage the granule into an input dir (process_sentinel2 takes a directory) ---
INPUT_DIR="$(mktemp -d)/input"
mkdir -p "${INPUT_DIR}"
if [[ -n "${GRANULE}" ]]; then
  cp -rL "${GRANULE}" "${INPUT_DIR}/"
else
  echo "ERROR: no --granule_archive provided" >&2
  exit 1
fi

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

# --- promote products into the DPS output/ dir for S3 upload ---
# The CLI writes COGs to <input_dir>/output/ (process_sentinel2.py:257),
# nested by date/product; cp -r preserves that structure.
cp -r "${INPUT_DIR}/output/." output/ 2>/dev/null || true
