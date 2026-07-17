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
