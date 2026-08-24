#!/usr/bin/env -S bash --login
set -euo pipefail
# MAAP DPS run script (Sentinel-2) -- OGC/CWL schema.
#
# Sentinel-2 is DOWNLOAD-then-process (unlike Landsat, which takes a granule File
# input): it fetches L2A/L1C scenes from the Copernicus Data Space (CDSE) by MGRS
# tile(s) + date, then runs process_sentinel2 on them.
#
# Copernicus credentials are NEVER job inputs -- they are pulled from MAAP's
# encrypted secret store at run time (maap.secrets.get_secret, via
# dps/_get_secret.py) so they never appear in the job's parameters or log. Store
# them once from a MAAP notebook (default names COP_USER / COP_PASS):
#   maap.secrets.add_secret("COP_USER", "you@example.com")
#   maap.secrets.add_secret("COP_PASS", "your-copernicus-password")
# See docs/DPS.md "Sentinel-2 credentials via MAAP secrets".
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

# --- defaults (boolean defaults MIRROR algorithm_config.yaml so an input left
# at its form default round-trips correctly whether or not MAAP re-emits the
# flag; --flag or --flag true|false overrides) ---
# Submit-and-go defaults: a bare Submit runs a known-good test config (tile/date/
# event below). For a real activation, change tile / download_date / activation_event.
ACTIVATION_EVENT="202601_KyleWx_US"
PRODUCTS="true swir"
TILE="T17RLN T17RLM"
DOWNLOAD_DATE="20251231"
LEVEL="1"
WE_NSTD="1"
MERGE="true"
MASK="false"
# NAMES of the MAAP secrets holding the Copernicus credentials (not the values).
# BAKED IN (not job inputs): the operator stores COP_USER / COP_PASS once with
# maap.secrets.add_secret; run.sh fetches them by these fixed names at run time.
COP_USER_SECRET="COP_USER"
COP_PASS_SECRET="COP_PASS"
# HARDCODED (NOT job inputs, not parsed from flags) -- mirrors the Capella /
# Satellogic pattern. To change any of these, publish a new algorithm_version.
#   SOURCE_LABEL: a download-from-CDSE job always has Copernicus as its origin.
#   DST_CRS:      native = no warp, preserves the source UTM zone (fastest). Pass
#                 EPSG:3857 only when the COG is headed for veda-data-airflow's
#                 build_stac; native COGs still ingest + tile via titiler.
#   COMPRESSION_LEVEL: ZSTD 9 -- balanced size/runtime for 10980^2 S2 tiles.
#   LIMIT:        download_sentinel2's -limit, i.e. the OData $top on the CDSE
#                 catalogue query. It is a PER-TILE page size, NOT a total cap, and
#                 the download loop does not follow @odata.nextLink -- so 50 is a
#                 generous ceiling no realistic tile+date query reaches. Getting
#                 fewer scenes than expected is the S2 revisit (~5 days: a single
#                 -date catches one orbit swath -- widen download_date to a range)
#                 or a wrong level, never this number.
#   NODATA:       intentionally UNSET. The CLI's -nodata is a single value applied
#                 to EVERY product, but S2 products are mixed-dtype: uint8 color
#                 composites (fill 0) and float32 indices (dump_geotiff_float
#                 writes -9999.0, and 0 is a LEGITIMATE NDVI/NDWI value). Leaving
#                 it off lets cog_utils.set_nodata_value auto-detect per dtype and
#                 get both right; a blanket -nodata 0 mis-declared the indices.
SOURCE_LABEL="Copernicus"
DST_CRS="native"
COMPRESSION_LEVEL="9"
LIMIT="50"
# Publishing is ALWAYS ON and the S3 destination is LOCKED for this version --
# neither is a job input nor parsed from a flag, so operators cannot change where
# output goes. Sentinel-2 publishes to the MAAP staging bucket nasa-disasters-
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
    --activation_event)      ACTIVATION_EVENT="$2"; shift 2;;
    --products)              PRODUCTS="$2"; shift 2;;
    --tile)                  TILE="$2"; shift 2;;
    --download_date)         DOWNLOAD_DATE="$2"; shift 2;;
    --level)                 LEVEL="$2"; shift 2;;
    --we_nstd)               WE_NSTD="$2"; shift 2;;
    --merge)                 if [[ "${2:-}" =~ ^(true|false)$ ]]; then MERGE="$2"; shift 2; else MERGE="true"; shift; fi ;;
    --mask)                  if [[ "${2:-}" =~ ^(true|false)$ ]]; then MASK="$2"; shift 2; else MASK="true"; shift; fi ;;
    *) echo "WARN: ignoring unrecognized arg: $1"; shift;;
  esac
done

# --- normalize space-separated free-text list inputs ---
# MAAP form values sometimes arrive with LITERAL quote characters (operators copy
# an example like `T17RLN T17RLM` verbatim including surrounding quotes, giving
# `"T17RLN` after word-splitting). Quotes are never valid in a tile ID / std-dev /
# product token, so strip every ' and " here so a quoted entry validates the same
# as a bare one. Word-splitting on the remaining spaces still yields the tokens.
TILE="${TILE//[\"\']/}"
WE_NSTD="${WE_NSTD//[\"\']/}"
PRODUCTS="${PRODUCTS//[\"\']/}"

# --- input validation (fail fast with a clear message; nothing has run yet) ---
validate_activation_event "${ACTIVATION_EVENT}"
require_nonempty tile "${TILE}" 'one or more MGRS tiles, space-separated, e.g. T17RLN or: T17RLN T17RLM'
# shellcheck disable=SC2086  # intentional word-split of the space-separated list
for t in ${TILE}; do validate_regex tile "$t" '^T[0-9]{2}[A-Z]{3}$' 'MGRS tile e.g. T17RLN'; done
# download_date is optional: blank -> CLI default (recent scenes). One YYYYMMDD or
# a start/end pair; the CLI rejects >2, but we bound each token's shape here.
# shellcheck disable=SC2086
[[ -n "${DOWNLOAD_DATE}" ]] && for d in ${DOWNLOAD_DATE}; do validate_regex download_date "$d" '^[0-9]{8}$' 'YYYYMMDD'; done
validate_in_set level "${LEVEL}" "1 2"
# optical products validated here (the CLI's own check ends in quit() -> exit 0);
# token set mirrors the accepted list in src/sentinel2/process_sentinel2.py.
# CASE-FOLD FIRST: the CLI is case-INSENSITIVE (it tests `p.lower() not in
# product_variants` and every dispatch uses `p.lower()`), but validate_in_set is an
# exact string compare. Without normalize_token, the token `colorIR` -- which this
# algorithm's OWN input doc advertises, and which the CLI happily accepts -- died
# here with "products 'colorIR' is invalid" before process_sentinel2 ever ran. Only
# the validation is folded; ${PRODUCTS} is passed to the CLI unmodified.
# shellcheck disable=SC2086
for t in ${PRODUCTS}; do validate_in_set products "$(normalize_token "$t" lower)" \
  "all true tc truecolor nat natural naturalcolor colorir cir colorinfrared swir shortwaveir shortwaveinfrared ndwi mndwi ndvi nbr we waterextent"; done
# shellcheck disable=SC2086
[[ -n "${WE_NSTD}" ]] && for n in ${WE_NSTD}; do validate_number we_nstd "$n"; done

OUT_HOME="${HOME}/drcs_outputs/${ACTIVATION_EVENT}"
mkdir -p "${OUT_HOME}"

# --- fetch Copernicus credentials from MAAP secrets (NEVER a job input; never
# logged). _get_secret.py prints ONLY the secret value; we capture it into env
# vars and never echo them. Auth is ambient in DPS (the wrapper injects MAAP_PGT).
# download_sentinel2 reads COP_USER/COP_PASS from the environment (no -u/-p on the
# command line), so the password never lands in argv / `ps` / the job log. ---
COP_USER="$(conda run --name disasters_dps python "${basedir}/../_get_secret.py" "${COP_USER_SECRET}")" \
  || die "could not read Copernicus username from MAAP secret '${COP_USER_SECRET}' (store it with maap.secrets.add_secret; see docs/DPS.md)."
COP_PASS="$(conda run --name disasters_dps python "${basedir}/../_get_secret.py" "${COP_PASS_SECRET}")" \
  || die "could not read Copernicus password from MAAP secret '${COP_PASS_SECRET}' (store it with maap.secrets.add_secret; see docs/DPS.md)."
export COP_USER COP_PASS

# --- download the requested scenes from Copernicus into a temp dir
# (process_sentinel2 takes a directory of .zip granules) ---
DL_DIR="$(mktemp -d)/s2_download"
mkdir -p "${DL_DIR}"
# shellcheck disable=SC2206  # intentional word-split of the space-separated tile list
dl_args=( "${DL_DIR}" -tile ${TILE} -level "${LEVEL}" -limit "${LIMIT}" -y )
# shellcheck disable=SC2206
[[ -n "${DOWNLOAD_DATE}" ]] && dl_args+=( -date ${DOWNLOAD_DATE} )
conda run --live-stream --name disasters_dps download_sentinel2 "${dl_args[@]}"

# Fail fast if the tile/date matched no scenes on CDSE, rather than silently
# producing an empty output.
shopt -s nullglob
zips=( "${DL_DIR}"/*.zip )
(( ${#zips[@]} )) || die "no Sentinel-2 scenes downloaded for tile(s)='${TILE}' date='${DOWNLOAD_DATE:-recent}'. Check the tile ID / date window (discover scenes at https://dataspace.copernicus.eu/)."

# --- activation metadata embedded as GeoTIFF tags at COG creation ---
META_JSON="${DL_DIR}/activation_metadata.json"
printf '{"ACTIVATION_EVENT": "%s", "SOURCE": "%s"}\n' \
  "${ACTIVATION_EVENT}" "${SOURCE_LABEL}" > "${META_JSON}"

# --- process the downloaded granules (CLI writes products to <input_dir>/output/) ---
# shellcheck disable=SC2206  # intentional word-split of space-separated lists
args=( "${DL_DIR}"
       -p ${PRODUCTS}
       -dst_crs "${DST_CRS}"
       -event "${ACTIVATION_EVENT}"
       -compression_level "${COMPRESSION_LEVEL}"
       --metadata-json "${META_JSON}" )
[[ "${MERGE}" == "true" ]] && args+=( -merge )
[[ "${MASK}"  == "true" ]] && args+=( -mask )
[[ -n "${WE_NSTD}" ]]      && args+=( -we_nstd ${WE_NSTD} )
# NOTE: no -nodata on purpose -- see the NODATA comment in the constants block.

conda run --live-stream --name disasters_dps process_sentinel2 "${args[@]}"

# --- move the produced COGs into OUT_HOME ---
# process_sentinel2 above ran under `set -e`, so reaching here means processing
# fully completed; the products are now safely under OUT_HOME.
cp -r "${DL_DIR}/output/." "${OUT_HOME}/" 2>/dev/null || true

# --- delete the raw Copernicus download now that processing is complete ---
# Frees the large Sentinel-2 .SAFE.zip archives (~700 MB-1 GB each) AND the
# extracted .SAFE scenes from the worker's scratch; the products already live in
# OUT_HOME (and flow to output/ + S3 via _finalize.sh), so DL_DIR is disposable.
rm -rf "${DL_DIR}"

# --- shared output handling (png -> output/ -> S3 -> delete COG) ---
source "${basedir}/../_finalize.sh"
