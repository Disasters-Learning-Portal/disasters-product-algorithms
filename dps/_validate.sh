# dps/_validate.sh -- shared input validation helpers for DPS run scripts.
#
# WHY: the OGC/CWL algorithm_config.yaml schema supports only
# {name,label,doc,type,default} -- no enum / pattern / min-max -- so the MAAP
# form cannot enforce valid values. run.sh is the ONLY place a bad arg can be
# caught. These helpers let each run.sh fail FAST (before conda/staging/S3, ~0s)
# with one clear, actionable message instead of failing late in gdalwarp or,
# worse, "succeeding" with zero output.
#
# Contract: source this near the TOP of each run.sh (after `basedir=`), then call
# the validators AFTER flag parsing and BEFORE OUT_HOME/staging/conda. This file
# only DEFINES functions -- it runs nothing at source time. Every run.sh already
# sets `set -euo pipefail`, so a validator's `exit 1` aborts the job.

# die MESSAGE -- print an actionable error to stderr and abort the job.
die() { echo "ERROR: $*" >&2; exit 1; }

# require_nonempty NAME VALUE HINT -- fail if VALUE is empty.
require_nonempty() {
  [[ -n "$2" ]] || die "$1 is required ($3)."
}

# validate_activation_event VALUE -- reject the placeholder and enforce the
# YYYYMM_Hazard_Location shape. This string is the primary key for the S3 output
# path AND is split into YEAR_MONTH/HAZARD/LOCATION GeoTIFF tags by
# cog_metadata.parse_activation_event (split('_', 2)), which validates nothing.
# Require: 4-digit year + 2-digit month (01-12), '_', a hazard token with no
# underscore, '_', then a location (may itself contain underscores).
validate_activation_event() {
  local v="$1"
  [[ "$v" != "YYYYMM_Hazard_Location" ]] || \
    die "activation_event is still the placeholder 'YYYYMM_Hazard_Location'. Set a real event, e.g. 202511_Flood_TX."
  [[ "$v" =~ ^[0-9]{4}(0[1-9]|1[0-2])_[^_]+_.+$ ]] || \
    die "activation_event '$v' must be YYYYMM_Hazard_Location (4-digit year, 2-digit month 01-12, then _Hazard_Location), e.g. 202511_Flood_TX."
}

# validate_dst_crs VALUE -- 'native' or a well-formed 'EPSG:<code>'. Catches
# typos (3857, EPSG:, epsg:3857) that otherwise fail only inside gdalwarp.
validate_dst_crs() {
  [[ "$1" =~ ^(native|EPSG:[0-9]+)$ ]] || \
    die "dst_crs '$1' must be 'native' or 'EPSG:<code>' (e.g. EPSG:3857, EPSG:4326)."
}

# validate_int_range NAME VALUE MIN MAX -- integer within [MIN, MAX].
validate_int_range() {
  [[ "$2" =~ ^-?[0-9]+$ ]] || die "$1 '$2' must be an integer."
  (( $2 >= $3 && $2 <= $4 )) || die "$1 '$2' must be between $3 and $4."
}

# validate_number NAME VALUE -- integer or decimal (optionally signed).
validate_number() {
  [[ "$2" =~ ^-?[0-9]+([.][0-9]+)?$ ]] || die "$1 '$2' must be a number."
}

# validate_regex NAME VALUE REGEX HINT -- generic pattern check with a human hint
# (used for per-sensor date formats).
validate_regex() {
  [[ "$2" =~ $3 ]] || die "$1 '$2' is invalid: expected $4."
}

# validate_granule NAME PATH "EXT1 EXT2 ..." -- exists, is a regular file, and
# has one of the allowed extensions (case-insensitive).
validate_granule() {
  local name="$1" path="$2" exts="$3" lc ext ok=""
  [[ -n "$path" ]] || die "$name is required."
  [[ -f "$path" ]] || die "$name '$path' is not a file."
  lc="$(printf '%s' "$path" | tr '[:upper:]' '[:lower:]')"
  for ext in $exts; do
    [[ "$lc" == *".${ext}" ]] && { ok=1; break; }
  done
  [[ -n "$ok" ]] || die "$name '$path' must be one of: ${exts// /, } (case-insensitive)."
}

# normalize_token VALUE CASE("lower"|"upper") -- trim surrounding whitespace and
# fold case; echo the result. PURE transform (never dies), so it is safe to call
# in `X="$(normalize_token ...)"`: the actual accept/reject stays in
# validate_in_set, which runs in the caller's shell and dies there on a bad
# value. Use to make selectors (sensor/level) tolerant of the common copy-paste
# mistakes ("Capella", " L1d ") without silently accepting an unknown value.
# tr is used for case-folding (as validate_granule already does) so there is no
# bash-4 `${v,,}`/`${v^^}` dependency -- works on the CI Linux box and macOS 3.2.
normalize_token() {
  local v="$1" c="$2"
  v="${v#"${v%%[![:space:]]*}"}"        # ltrim
  v="${v%"${v##*[![:space:]]}"}"        # rtrim
  case "$c" in
    lower) printf '%s' "$v" | tr '[:upper:]' '[:lower:]' ;;
    upper) printf '%s' "$v" | tr '[:lower:]' '[:upper:]' ;;
    *)     printf '%s' "$v" ;;
  esac
}

# validate_in_set NAME VALUE "tok1 tok2 ..." -- membership check.
validate_in_set() {
  local name="$1" value="$2" allowed="$3" tok
  for tok in $allowed; do
    [[ "$value" == "$tok" ]] && return 0
  done
  die "$name '$value' is invalid. Allowed: ${allowed// /, }."
}
