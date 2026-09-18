#!/usr/bin/env -S bash --login
set -euo pipefail
# MAAP DPS run script (Sentinel-2, STAC path) -- OGC/CWL schema.
#
# This is the SECOND Sentinel-2 algorithm. It coexists with
# dps/sentinel2/ (disasters-sentinel2-process) so both can run on the same
# activation and be compared before either is retired; see issue #144.
#
#   dps/sentinel2/      download .SAFE archives from Copernicus (CDSE) by MGRS
#                       tile + date, unzip with 7z, process locally.
#                       Needs COP_USER / COP_PASS from MAAP secrets.
#
#   dps/sentinel2_odr/ (this one) query a STAC API (Earth Search) by bbox +
#                       date range and read the cloud-optimized assets straight
#                       from S3. NO credentials, NO download step, NO 7z.
#
# Inputs arrive as NAMED flags via "$@". Boolean inputs may arrive as a bare
# "--flag" (presence) or "--flag true|false" (value), so the parser accepts both.
#
# Output flow handled by dps/_finalize.sh: ~/drcs_outputs -> output/ -> S3
# (nasa-disasters-staging, via MAAP workspace credentials) -> delete COG.

basedir=$(dirname "$(readlink -f "$0")")
mkdir -p output

# shared input validators (fail fast, before conda/network)
source "${basedir}/../_validate.sh"

# --- defaults (boolean defaults MIRROR algorithm_config.yaml so an input left
# at its form default round-trips correctly whether or not MAAP re-emits the
# flag; --flag or --flag true|false overrides) ---
# Submit-and-go defaults: a bare Submit runs a known-good test config over
# central Iowa. For a real activation, change bbox / dates / activation_event.
ACTIVATION_EVENT="202601_KyleWx_US"
PRODUCTS="true_color ndvi"
BBOX="-93.70,42.00,-93.55,42.10"
START_DATE="2025-08-12"
END_DATE="2025-08-14"
LEVEL="2"
CLOUD_COVER="50"
WE_NSTD="1"
MERGE="true"
MASK="false"

# HARDCODED (NOT job inputs, not parsed from flags) -- mirrors the Capella /
# Satellogic / Sentinel-2 pattern. To change any of these, publish a new
# algorithm_version.
#
#   SOURCE_LABEL: deliberately the SAME string the .SAFE job uses. Earth Search
#                 redistributes ESA's Sentinel-2 on AWS Open Data; the data
#                 provenance is Copernicus either way, and keeping the tag
#                 identical is what lets a Phase 4 side-by-side comparison line
#                 the two pipelines' products up.
#   STAC_API:     Earth Search v1. The CLI defaults to this too; pinned here so
#                 the deployed algorithm does not silently follow a CLI default
#                 change.
#
# dst_crs / compression / compression_level / nodata are NOT passed at all: the
# STAC CLI hardcodes them (native CRS, ZSTD 9, and a per-product nodata that is
# correct for each dtype -- uint8 composites, float32 indices, uint8 categorical
# water extent). A single blanket -nodata cannot be right for all three, which
# is why the .SAFE job omits it as well.
SOURCE_LABEL="Copernicus"
STAC_API="https://earth-search.aws.element84.com/v1"

# Publishing is ALWAYS ON and the S3 destination is LOCKED for this version --
# neither is a job input nor parsed from a flag, so operators cannot change where
# output goes. Same destination as every other sensor: the MAAP staging bucket
# nasa-disasters-staging (prefix dps_output/<event>/) using short-lived MAAP
# workspace credentials -- the DPS worker's own role can't write there; see
# shared_utils/staging_upload.py + dps/_finalize.sh step 3a.
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
    --activation_event)      ACTIVATION_EVENT="$2"; shift 2;;
    --products)              PRODUCTS="$2"; shift 2;;
    --bbox)                  BBOX="$2"; shift 2;;
    --start_date)            START_DATE="$2"; shift 2;;
    --end_date)              END_DATE="$2"; shift 2;;
    --level)                 LEVEL="$2"; shift 2;;
    --cloud_cover)           CLOUD_COVER="$2"; shift 2;;
    --we_nstd)               WE_NSTD="$2"; shift 2;;
    --merge)                 if [[ "${2:-}" =~ ^(true|false)$ ]]; then MERGE="$2"; shift 2; else MERGE="true"; shift; fi ;;
    --mask)                  if [[ "${2:-}" =~ ^(true|false)$ ]]; then MASK="$2"; shift 2; else MASK="true"; shift; fi ;;
    *) echo "WARN: ignoring unrecognized arg: $1"; shift;;
  esac
done

# --- normalize space-separated free-text list inputs ---
# MAAP form values sometimes arrive with LITERAL quote characters (operators copy
# an example verbatim including surrounding quotes). Quotes are never valid in a
# product token, a std-dev or a bbox, so strip every ' and " here.
PRODUCTS="${PRODUCTS//[\"\']/}"
WE_NSTD="${WE_NSTD//[\"\']/}"
BBOX="${BBOX//[\"\']/}"

# --- input validation (fail fast with a clear message; nothing has run yet) ---
validate_activation_event "${ACTIVATION_EVENT}"

# min_lat_span 0 -- unlike Black Marble, Sentinel-2 has no minimum AOI: a STAC
# search returns whole 110 km tiles however small the box is, so a tight AOI over
# one town is perfectly valid.
validate_bbox "${BBOX}" 0

# Sentinel-2A launched 2015-06-23; nothing exists before that, and an earlier
# date returns zero scenes and fails obscurely much later.
validate_date_not_before start_date "${START_DATE}" "2015-06-23"
validate_date_not_before end_date   "${END_DATE}"   "2015-06-23"
[[ "${START_DATE}" > "${END_DATE}" ]] && \
  die "start_date '${START_DATE}' is after end_date '${END_DATE}'."

validate_in_set level "${LEVEL}" "1 2"
validate_number cloud_cover "${CLOUD_COVER}"
validate_int_range cloud_cover "${CLOUD_COVER%%.*}" 0 100

require_nonempty products "${PRODUCTS}" \
  'one or more products, space-separated, e.g. true_color ndvi'
# Token set mirrors --product's `choices` in
# src/sentinel2/process_sentinel2_odr.py. Validated here because a bad token
# would otherwise only surface after the STAC search has already run.
# shellcheck disable=SC2086  # intentional word-split of the space-separated list
for t in ${PRODUCTS}; do validate_in_set products "$(normalize_token "$t" lower)" \
  "ndvi ndwi mndwi nbr evi we true_color natural_color color_infrared swir"; done

# shellcheck disable=SC2086
[[ -n "${WE_NSTD}" ]] && for n in ${WE_NSTD}; do validate_number we_nstd "$n"; done

# --cloud-mask needs the L2A Scene Classification Layer, which L1C has no
# equivalent of. The CLI rejects the combination too, but catching it here saves
# a STAC round-trip and gives the operator the reason up front.
if [[ "${MASK}" == "true" && "${LEVEL}" == "1" ]]; then
  die "mask=true requires level=2 (L2A): cloud masking uses the Scene Classification Layer, which does not exist for L1C."
fi

OUT_HOME="${HOME}/drcs_outputs/${ACTIVATION_EVENT}"
mkdir -p "${OUT_HOME}"

# --- activation metadata embedded as GeoTIFF tags at COG creation ---
# The STAC pipeline does NOT put the event in the filename (unlike the .SAFE
# job's -event flag). The event lives in the GeoTIFF tags and the S3 prefix --
# the convention bake_event_metadata.ipynb and simple_disaster_staging.ipynb use.
WORK_DIR="$(mktemp -d)/s2_stac"
mkdir -p "${WORK_DIR}"
META_JSON="${WORK_DIR}/activation_metadata.json"
printf '{"ACTIVATION_EVENT": "%s", "SOURCE": "%s"}\n' \
  "${ACTIVATION_EVENT}" "${SOURCE_LABEL}" > "${META_JSON}"

# --- process each requested product ---
# The STAC CLI takes ONE --product per invocation (unlike the .SAFE CLI's -p
# list), so loop. Each pass re-runs the STAC search, which is a cheap metadata
# query next to the raster reads.
#
# bbox reaches the CLI as four separate floats, so commas become spaces.
BBOX_ARGS="${BBOX//,/ }"

# shellcheck disable=SC2086  # intentional word-split of the space-separated list
for product in ${PRODUCTS}; do

  echo ""
  echo "=================================================================="
  echo "Product: ${product}"
  echo "=================================================================="

  # Normalized OUTSIDE the array on purpose. A command substitution inside the
  # `args=( ... )` block would put a literal ')' in it, and
  # tests/integration/test_dps_cli_flag_contract.py parses that block with a
  # non-greedy regex -- it would stop at that ')' and silently check only the
  # first flag instead of all of them.
  prod="$(normalize_token "${product}" lower)"

  # shellcheck disable=SC2206,SC2086
  args=( --product "${prod}"
         --level "${LEVEL}"
         --start-date "${START_DATE}"
         --end-date "${END_DATE}"
         --bbox ${BBOX_ARGS}
         --cloud-cover "${CLOUD_COVER}"
         --stac-api "${STAC_API}"
         --output "${WORK_DIR}"
         --metadata-json "${META_JSON}" )

  [[ "${MERGE}" == "true" ]] && args+=( --merge )
  [[ "${MASK}"  == "true" ]] && args+=( --cloud-mask )
  # shellcheck disable=SC2206
  [[ -n "${WE_NSTD}" ]]      && args+=( --we-nstd ${WE_NSTD} )

  conda run --live-stream --name disasters_dps process_sentinel2_odr "${args[@]}"

done

# --- move the produced COGs into OUT_HOME ---
# TOP-LEVEL *.tif ONLY, deliberately. The water-extent path caches its
# CDL / WorldCover reference rasters in ${WORK_DIR}/water_extent_reference/,
# and those are large, are not products, and must never be published. A
# recursive copy would ship them.
shopt -s nullglob
products_made=( "${WORK_DIR}"/*.tif )
(( ${#products_made[@]} )) || die "no COGs were produced for products='${PRODUCTS}' bbox='${BBOX}' ${START_DATE}..${END_DATE} (level ${LEVEL}, cloud cover < ${CLOUD_COVER}%). Widen the date range or raise cloud_cover."
cp "${products_made[@]}" "${OUT_HOME}/"

echo ""
echo "Produced ${#products_made[@]} COG(s):"
for f in "${products_made[@]}"; do echo "  $(basename "$f")"; done

# --- drop the scratch dir (raster cache + intermediates) ---
rm -rf "${WORK_DIR}"

# --- shared output handling (png -> output/ -> S3 -> delete COG) ---
source "${basedir}/../_finalize.sh"
