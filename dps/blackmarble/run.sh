#!/usr/bin/env -S bash --login
set -euo pipefail
# MAAP DPS run script (VEDA Black Marble) -- OGC/CWL schema.
#
# Black Marble is a DOWNLOAD-then-process pipeline (like Sentinel-2, unlike the
# file-input sensors): given a WGS84 bbox + date it downloads VIIRS VNP46A2
# nighttime lights (Earthdata), Landsat scenes (STAC), and OSM roads, then fuses
# them into an urban-focused Cloud Optimized GeoTIFF.
#
# The pipeline is NOT part of this repo. It is the upstream, NASA-IMPACT-maintained
# package `blackmarble` (github.com/NASA-IMPACT/veda-black-marble), pip-installed into
# the DPS worker env by dps/environment.yml and called here through its own
# `blackmarble` console script, UNMODIFIED. This job is a thin wrapper: validate the
# inputs -> fetch the Earthdata token -> run the upstream CLI -> publish the output to
# S3. It does NOT go through process_* / shared_utils.convert_to_cog -- blackmarble
# writes its own COG.
#
# Earthdata auth: the token is NEVER a job input (it would appear in the DPS job
# log). run.sh pulls it from MAAP's encrypted secret store at run time
# (maap.secrets.get_secret, via dps/_get_secret.py) and exports EARTHDATA_TOKEN,
# which blackmarble reads from the environment. Store it once from a MAAP notebook:
#   maap.secrets.add_secret("EARTHDATA_TOKEN", "<your-earthdata-token>")
# The earthdata_secret_name input only names WHICH secret to read (names are not
# sensitive) -- default EARTHDATA_TOKEN.
#
# Inputs arrive as NAMED flags via "$@". Boolean inputs may arrive as a bare
# "--flag" (presence) or "--flag true|false" (value), so the parser accepts both.
#
# Output flow handled by dps/_finalize.sh: ~/drcs_outputs -> output/ -> S3
# (nasa-disasters-staging, via MAAP workspace credentials) -> delete COG.

basedir=$(dirname "$(readlink -f "$0")")
mkdir -p output

# shared input validators (fail fast, before conda/download)
source "${basedir}/../_validate.sh"

# --- defaults (boolean defaults MIRROR algorithm_config.yaml so an input left at
# its form default round-trips correctly whether or not MAAP re-emits the flag;
# --flag or --flag true|false overrides) ---
# Submit-and-go defaults: a bare Submit runs a known-good San Francisco test box.
# For a real activation, change bbox / date / activation_event.
ACTIVATION_EVENT="202601_KyleWx_US"
BBOX="-122.55,37.69,-122.32,37.81"
DATE="2023-06-15"
CONFIG="fast"
OSM_SOURCE="overpass"
WGS84="false"
LOG_LEVEL="INFO"
# NAME of the MAAP secret holding the Earthdata token (not the value). Default
# EARTHDATA_TOKEN; run.sh fetches it by this name at run time.
EARTHDATA_SECRET_NAME="EARTHDATA_TOKEN"
# Publishing is ALWAYS ON and the S3 destination is LOCKED for this version --
# neither is a job input nor parsed from a flag, so operators cannot change where
# output goes. Black Marble publishes to the MAAP staging bucket nasa-disasters-
# staging (prefix dps_output/<event>/) using short-lived MAAP workspace credentials
# -- the DPS worker's own role can't write there; see shared_utils/staging_upload.py
# + dps/_finalize.sh step 3a. To target a different bucket/prefix, publish a new
# algorithm_version with these constants changed.
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
    --activation_event)       ACTIVATION_EVENT="$2"; shift 2;;
    --bbox)                   BBOX="$2"; shift 2;;
    --date)                   DATE="$2"; shift 2;;
    --config)                 CONFIG="$2"; shift 2;;
    --osm_source)             OSM_SOURCE="$2"; shift 2;;
    --earthdata_secret_name)  EARTHDATA_SECRET_NAME="$2"; shift 2;;
    --wgs84)                  if [[ "${2:-}" =~ ^(true|false)$ ]]; then WGS84="$2"; shift 2; else WGS84="true"; shift; fi ;;
    *) echo "WARN: ignoring unrecognized arg: $1"; shift;;
  esac
done

# --- normalize: strip stray quotes operators paste around the bbox/tokens ---
BBOX="${BBOX//[\"\']/}"
CONFIG="${CONFIG//[\"\']/}"
OSM_SOURCE="${OSM_SOURCE//[\"\']/}"

# --- input validation (fail fast with a clear message; nothing has run yet) ---
validate_activation_event "${ACTIVATION_EVENT}"
validate_bbox "${BBOX}"
validate_regex date "${DATE}" '^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$' 'YYYY-MM-DD'
validate_in_set config "${CONFIG}" "default high_quality fast"
validate_in_set osm_source "${OSM_SOURCE}" "overpass layercake"

OUT_HOME="${HOME}/drcs_outputs/${ACTIVATION_EVENT}"
mkdir -p "${OUT_HOME}"

# --- output naming + layout -------------------------------------------------
# Mirrors what every other sensor puts under OUT_HOME, because _finalize.sh keys each
# S3 object by its OUT_HOME-RELATIVE path: <YYYYMMDD>/<product>/<file> becomes
# s3://nasa-disasters-staging/dps_output/<event>/<YYYYMMDD>/<product>/<file>, the same
# shape process_sentinel2 produces (…/20251231/trueColor/S2B_MSIL1C_trueColor_…).
# The activation event is deliberately NOT in the filename -- it lives in the S3 prefix
# and in the COG's own tags (baked below), matching bake_event_metadata.ipynb, which
# STRIPS an event prefix out of names it finds.
#
# The filename convention itself (and why it is shaped that way) lives in the sourced
# naming.sh, so the test suite can assert it -- same split as dps/_validate.sh.
PRODUCT="hdnightlights"
PRODUCT_COLORED="hdnightlightscolored"

source "${basedir}/naming.sh"

DATE_DIR="${DATE//-/}"                       # 2023-06-15 -> 20230615, as Sentinel-2 does
OUT_DIR="${OUT_HOME}/${DATE_DIR}/${PRODUCT}"
mkdir -p "${OUT_DIR}"
STEM="$(bm_stem "${BBOX}" "${DATE}" "${PRODUCT}")"
STEM_COLORED="$(bm_stem "${BBOX}" "${DATE}" "${PRODUCT_COLORED}")"

# --- fetch the Earthdata token from MAAP secrets (NEVER a job input; never logged).
# _get_secret.py prints ONLY the secret value; capture it into EARTHDATA_TOKEN and
# never echo it. Auth is ambient in DPS (the wrapper injects MAAP_PGT). blackmarble
# reads EARTHDATA_TOKEN from the environment (no token on argv / in `ps` / the log). ---
EARTHDATA_TOKEN="$(conda run --name disasters_dps python "${basedir}/../_get_secret.py" "${EARTHDATA_SECRET_NAME}")" \
  || die "could not read Earthdata token from MAAP secret '${EARTHDATA_SECRET_NAME}' (store it with maap.secrets.add_secret('EARTHDATA_TOKEN', '<token>'); see docs/DPS.md)."
export EARTHDATA_TOKEN

# --- run the Black Marble pipeline ---
# Scratch dir for downloads/cache/temp lives OUTSIDE OUT_HOME so only the final
# COG(s) land there for _finalize.sh to publish. blackmarble writes --output-path
# (+ a `-colored` companion it derives from that path, + an extra EPSG:4326 file when
# --wgs84 is set) into OUT_DIR.
SCRATCH="$(mktemp -d)"
DATA_DIR="${SCRATCH}/data"
mkdir -p "${DATA_DIR}"
OUTFILE="${OUT_DIR}/${STEM}.tif"

echo "Running Black Marble pipeline"
echo "  activation_event=${ACTIVATION_EVENT}"
echo "  bbox=${BBOX}"
echo "  date=${DATE}"
echo "  config=${CONFIG}"
echo "  osm_source=${OSM_SOURCE}"
echo "  wgs84=${WGS84}"
echo "  output=${OUTFILE}"
echo "  earthdata_secret_name=${EARTHDATA_SECRET_NAME}"

# Upstream's own console script (`blackmarble = blackmarble.cli:app`), called as-is with
# the flags exactly as its README documents them -- a single command, no subcommand.
# Keep this in step with upstream's CLI when bumping the pin in dps/environment.yml.
bm_args=(
  --bbox "${BBOX}"
  --date "${DATE}"
  --config "${CONFIG}"
  --osm-source "${OSM_SOURCE}"
  --output-path "${OUTFILE}"
  --data-dir "${DATA_DIR}"
  --log-level "${LOG_LEVEL}"
)
[[ "${WGS84}" == "true" ]] && bm_args+=( --wgs84 )

conda run --live-stream --name disasters_dps blackmarble "${bm_args[@]}"

# Fail fast if the pipeline produced no COG (rather than silently "succeeding").
shopt -s nullglob
produced=( "${OUT_DIR}"/*.tif )
(( ${#produced[@]} )) || die "Black Marble produced no COG in ${OUT_DIR} for bbox='${BBOX}' date='${DATE}'. Check the date has VIIRS/Landsat coverage and the Earthdata token is valid."

# --- rename the colored companion onto its own product stem ---
# Upstream derives the second raster from OUR path, literally
# `output_path.replace(".tif", "-colored.tif")` (blackmarble/pipeline.py), which would
# leave `..._2023-06-15_day-colored.tif` -- a date token that is no longer last. Rename
# it to the colored product's own stem so both files obey the convention.
if [[ -f "${OUT_DIR}/${STEM}-colored.tif" ]]; then
  mv "${OUT_DIR}/${STEM}-colored.tif" "${OUT_DIR}/${STEM_COLORED}.tif"
fi

# --- bake the activation event into every produced COG ---
# Black Marble writes its own COG and never touches shared_utils.convert_to_cog, so it
# misses the --metadata-json path that gives every process_* sensor its event tags.
# bake_event.py RE-CREATES each COG with the tags applied at creation (GDAL 3.10+
# refuses an in-place COG update; see CLAUDE.md "Critical Constraints").
conda run --live-stream --name disasters_dps python "${basedir}/bake_event.py" \
  --out-home "${OUT_HOME}" --event "${ACTIVATION_EVENT}" \
  || die "failed to bake ACTIVATION_EVENT into the Black Marble COG(s) in ${OUT_DIR}."

# --- delete the raw downloads/cache now that the product is written ---
rm -rf "${SCRATCH}"

# --- shared output handling (output/ -> S3 staging -> delete COG) ---
source "${basedir}/../_finalize.sh"
