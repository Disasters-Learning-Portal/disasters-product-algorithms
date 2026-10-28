#!/usr/bin/env bash
# Standalone assertions for dps/_validate.sh. Each validator calls `exit 1` on
# failure, so every case runs in a subshell. Run directly:
#     bash tests/integration/test_dps_validate.sh
# (A pytest wrapper, test_dps_validate.py, runs this same file in CI.)
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VALIDATE="${here}/../../dps/_validate.sh"

# Also source it HERE (not just inside the ok/no/eq subshells) so pure transforms
# like normalize_token can be used in command substitution when BUILDING a case's
# arguments -- that is how run.sh composes them, e.g.
#   validate_in_set products "$(normalize_token "$t" lower)" "$SET"
# _validate.sh is nothing but function definitions (no top-level side effects), and
# every validator still runs inside a subshell below, so its `exit 1` can't kill
# this harness.
source "$VALIDATE"

pass=0; fail=0

# ok DESC CALL... -- the call (in a subshell) must succeed.
ok() { local d="$1"; shift
  if ( source "$VALIDATE"; "$@" ) >/dev/null 2>&1; then pass=$((pass+1))
  else fail=$((fail+1)); echo "FAIL (expected pass): $d"; fi; }

# no DESC CALL... -- the call (in a subshell) must fail.
no() { local d="$1"; shift
  if ( source "$VALIDATE"; "$@" ) >/dev/null 2>&1; then
    fail=$((fail+1)); echo "FAIL (expected fail): $d"
  else pass=$((pass+1)); fi; }

# eq DESC WANT CALL... -- the call's stdout (in a subshell) must equal WANT.
# Used for pure transforms like normalize_token.
eq() { local d="$1" want="$2"; shift 2
  local got; got="$( source "$VALIDATE"; "$@" )"
  if [[ "$got" == "$want" ]]; then pass=$((pass+1))
  else fail=$((fail+1)); echo "FAIL (want '$want' got '$got'): $d"; fi; }

# --- activation_event -------------------------------------------------------
ok "event ok"            validate_activation_event 202511_Flood_TX
ok "event loc underscore" validate_activation_event 202501_Flood_CA_extra
no "event placeholder"   validate_activation_event YYYYMM_Hazard_Location
no "event no yearmonth"  validate_activation_event Flood_TX
no "event only ym"       validate_activation_event 202511
no "event month 13"      validate_activation_event 202513_Flood_TX
no "event 5-digit"       validate_activation_event 20251_Flood_TX
no "event empty loc"     validate_activation_event 202511_Flood_

# --- dst_crs ----------------------------------------------------------------
ok "crs native"          validate_dst_crs native
ok "crs epsg 3857"       validate_dst_crs EPSG:3857
ok "crs epsg 4326"       validate_dst_crs EPSG:4326
no "crs bare code"       validate_dst_crs 3857
no "crs empty epsg"      validate_dst_crs EPSG:
no "crs lowercase"       validate_dst_crs epsg:3857

# --- int_range (compression_level) ------------------------------------------
ok "cl 22"               validate_int_range compression_level 22 1 22
ok "cl 1"                validate_int_range compression_level 1 1 22
no "cl 0"                validate_int_range compression_level 0 1 22
no "cl 23"               validate_int_range compression_level 23 1 22
no "cl nonint"           validate_int_range compression_level x 1 22
no "cl decimal"          validate_int_range compression_level 1.5 1 22

# --- number -----------------------------------------------------------------
ok "num nodata"          validate_number nodata -9999
ok "num gamma"           validate_number gamma 0.7
no "num alpha"           validate_number nodata abc
no "num empty"           validate_number nodata ""

# --- in_set (level) ---------------------------------------------------------
ok "level L1D"           validate_in_set level L1D "L1D L1B"
ok "level L1B"           validate_in_set level L1B "L1D L1B"
no "level L1C"           validate_in_set level L1C "L1D L1B"

# --- in_set (sensor selector: list-dates) -----------------------------------
ok "sensor capella"      validate_in_set sensor capella "capella umbra satellogic"
ok "sensor umbra"        validate_in_set sensor umbra "capella umbra satellogic"
ok "sensor satellogic"   validate_in_set sensor satellogic "capella umbra satellogic"
no "sensor sentinel2"    validate_in_set sensor sentinel2 "capella umbra satellogic"
no "sensor landsat"      validate_in_set sensor landsat "capella umbra satellogic"
no "sensor typo"         validate_in_set sensor capel "capella umbra satellogic"
no "sensor empty"        validate_in_set sensor "" "capella umbra satellogic"
no "sensor raw Capella"  validate_in_set sensor Capella "capella umbra satellogic"

# --- normalize_token (trim + case-fold; feeds validate_in_set) --------------
eq "norm Capella->capella"  capella     normalize_token "Capella" lower
eq "norm CAPELLA->capella"  capella     normalize_token "CAPELLA" lower
eq "norm spaces trimmed"    capella     normalize_token "  capella  " lower
eq "norm ' Capella '"       capella     normalize_token " Capella " lower
eq "norm l1d->L1D"          L1D         normalize_token "l1d" upper
eq "norm ' L1b '->L1B"      L1B         normalize_token " L1b " upper
eq "norm default L1D"       L1D         normalize_token "L1D" upper
eq "norm satellogic keep"   satellogic  normalize_token "  SateLLogic " lower
eq "norm unknown untouched" sentinel2   normalize_token " Sentinel2 " lower

# --- optical products: case-folded membership -------------------------------
# run.sh (landsat + sentinel2) calls validate_in_set on the CASE-FOLDED token:
#   for t in ${PRODUCTS}; do validate_in_set products "$(normalize_token "$t" lower)" "$SET"
# The CLIs are case-INSENSITIVE (`p.lower() not in product_variants`), but
# validate_in_set is an exact compare -- so without the fold, `colorIR` (the exact
# token every algorithm_config/ogc doc string advertises) was rejected before the
# CLI ever ran. These cases pin the composition, not just validate_in_set alone.
S2_PRODUCTS="all true tc truecolor nat natural naturalcolor colorir cir colorinfrared swir shortwaveir shortwaveinfrared ndwi mndwi ndvi nbr we waterextent"
LS_PRODUCTS="all true tc truecolor pan panchromatic nat natural naturalcolor nc colorir colorinfrared cir mndwi ndvi evi ndwi nbr we waterextent"

ok "s2 colorIR folded"   validate_in_set products "$(normalize_token colorIR lower)"     "$S2_PRODUCTS"
ok "s2 colorir bare"     validate_in_set products "$(normalize_token colorir lower)"     "$S2_PRODUCTS"
ok "s2 COLORIR folded"   validate_in_set products "$(normalize_token COLORIR lower)"     "$S2_PRODUCTS"
ok "s2 SWIR folded"      validate_in_set products "$(normalize_token SWIR lower)"        "$S2_PRODUCTS"
ok "s2 NDVI folded"      validate_in_set products "$(normalize_token NDVI lower)"        "$S2_PRODUCTS"
ok "s2 waterExtent fold" validate_in_set products "$(normalize_token waterExtent lower)" "$S2_PRODUCTS"
ok "s2 true"             validate_in_set products "$(normalize_token true lower)"        "$S2_PRODUCTS"
ok "s2 all"              validate_in_set products "$(normalize_token all lower)"         "$S2_PRODUCTS"
no "s2 typo colorIRR"    validate_in_set products "$(normalize_token colorIRR lower)"    "$S2_PRODUCTS"
no "s2 pan not in s2"    validate_in_set products "$(normalize_token pan lower)"         "$S2_PRODUCTS"
no "s2 empty"            validate_in_set products "$(normalize_token '' lower)"          "$S2_PRODUCTS"

ok "ls colorIR folded"   validate_in_set products "$(normalize_token colorIR lower)"     "$LS_PRODUCTS"
ok "ls PAN folded"       validate_in_set products "$(normalize_token PAN lower)"         "$LS_PRODUCTS"
ok "ls EVI folded"       validate_in_set products "$(normalize_token EVI lower)"         "$LS_PRODUCTS"
ok "ls waterExtent fold" validate_in_set products "$(normalize_token waterExtent lower)" "$LS_PRODUCTS"
no "ls swir not in ls"   validate_in_set products "$(normalize_token swir lower)"        "$LS_PRODUCTS"
no "ls typo"             validate_in_set products "$(normalize_token evii lower)"        "$LS_PRODUCTS"

# --- date regexes -----------------------------------------------------------
ok "capella date"        validate_regex date 20231107120000 '^[0-9]{14}$' ts
no "capella date dashes" validate_regex date 2023-11-07 '^[0-9]{14}$' ts
ok "umbra date"          validate_regex date "2023-11-07 12:00:00" '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$' ts
no "umbra date compact"  validate_regex date 20231107 '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$' ts

# --- date_not_before (satellite mission-start floors) ------------------------
# Black Marble: VNP46A2 (Suomi-NPP) starts 2012-01-19, VJ146A2 (NOAA-20) 2018-01-19.
# A date before a product's CMR TemporalExtents.BeginningDateTime returns ZERO granules,
# which surfaces far downstream (after a successful Earthdata login) as an obscure
# failure -- so it is caught here instead.
ok "dnb after"           validate_date_not_before date 2023-06-15 2018-01-19
ok "dnb equal"           validate_date_not_before date 2018-01-19 2018-01-19
ok "dnb day after"       validate_date_not_before date 2018-01-20 2018-01-19
ok "dnb year after"      validate_date_not_before date 2019-01-01 2018-01-19
no "dnb day before"      validate_date_not_before date 2018-01-18 2018-01-19
no "dnb month before"    validate_date_not_before date 2017-12-31 2018-01-19
no "dnb years before"    validate_date_not_before date 2015-06-15 2018-01-19
# snpp floor accepts what the noaa20 floor rejects -- the two really are different gates
ok "dnb snpp 2015"       validate_date_not_before date 2015-06-15 2012-01-19
no "dnb snpp 2011"       validate_date_not_before date 2011-06-15 2012-01-19
# Shape is checked BEFORE the comparison: string ordering would otherwise let a
# malformed value like 9/9/2020 compare "greater" than 2018-01-19 and pass.
no "dnb slashes"         validate_date_not_before date 9/9/2020 2018-01-19
no "dnb compact"         validate_date_not_before date 20230615 2018-01-19
no "dnb month 13"        validate_date_not_before date 2023-13-01 2018-01-19
no "dnb day 32"          validate_date_not_before date 2023-06-32 2018-01-19
no "dnb day 00"          validate_date_not_before date 2023-06-00 2018-01-19
no "dnb empty"           validate_date_not_before date "" 2018-01-19
no "dnb text"            validate_date_not_before date yesterday 2018-01-19

# --- granule ----------------------------------------------------------------
tmp="$(mktemp -d)"
: > "${tmp}/g.tar"; : > "${tmp}/g.ZIP"; : > "${tmp}/g.txt"
ok "granule tar"         validate_granule file "${tmp}/g.tar" "tar zip"
ok "granule ZIP upper"   validate_granule file "${tmp}/g.ZIP" "tar zip"
no "granule wrong ext"   validate_granule file "${tmp}/g.txt" "tar zip"
no "granule missing"     validate_granule file "${tmp}/nope.tar" "tar zip"
no "granule dir"         validate_granule file "${tmp}" "tar zip"
rm -rf "${tmp}"

echo "dps/_validate.sh: ${pass} passed, ${fail} failed"
[[ "${fail}" -eq 0 ]]
