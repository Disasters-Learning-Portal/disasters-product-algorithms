#!/usr/bin/env bash
# Standalone assertions for dps/_validate.sh. Each validator calls `exit 1` on
# failure, so every case runs in a subshell. Run directly:
#     bash tests/integration/test_dps_validate.sh
# (A pytest wrapper, test_dps_validate.py, runs this same file in CI.)
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VALIDATE="${here}/../../dps/_validate.sh"

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

# --- date regexes -----------------------------------------------------------
ok "capella date"        validate_regex date 20231107120000 '^[0-9]{14}$' ts
no "capella date dashes" validate_regex date 2023-11-07 '^[0-9]{14}$' ts
ok "umbra date"          validate_regex date "2023-11-07 12:00:00" '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$' ts
no "umbra date compact"  validate_regex date 20231107 '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$' ts

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
