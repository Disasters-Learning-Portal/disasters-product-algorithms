# Black Marble output-name helpers -- SOURCED, never executed.
#
# Kept out of run.sh (and out of awk-inline form) for the same reason dps/_validate.sh
# is a separate file: it is the only way the logic can be asserted by the test suite.
# Pinned by tests/unit/test_blackmarble_naming.py.
#
# Convention produced here:
#
#   hdnightlights_<NW><SE>_<YYYY-MM-DD>_day
#   e.g. hdnightlights_32_97N90_96W32_83N90_79W_2023-03-25_day
#
# <NW> is the north-west corner (max_lat, min_lon), <SE> the south-east corner
# (min_lat, max_lon). Each coordinate is |value| to 2 dp with "." rewritten as "_" and a
# hemisphere letter appended, concatenated with NO separator between the four.
#
# The trailing `_YYYY-MM-DD_day` is the repo-wide token for a TIME-LESS INDIVIDUAL
# product (cog_utils._relocate_datetime): Black Marble is a daily composite with no
# acquisition time, so `_day` is the correct form and the date stays LAST. That is also
# why the colored companion gets its own product token rather than a `-colored` suffix --
# a suffix would push the date out of final position.
#
# The activation event is deliberately absent: it lives in the S3 prefix and in the COG's
# own tags (see bake_event.py), matching bake_event_metadata.ipynb, which STRIPS an event
# prefix out of any filename carrying one.

# bm_fmt_coord <signed-decimal-degrees> <lat|lon> -> e.g. 32_97N / 90_96W
bm_fmt_coord() {
  awk -v v="$1" -v kind="$2" 'BEGIN{
    h = (kind == "lat") ? ((v >= 0) ? "N" : "S") : ((v >= 0) ? "E" : "W");
    a = (v < 0) ? -v : v;
    s = sprintf("%.2f", a);
    gsub(/\./, "_", s);
    printf "%s%s", s, h;
  }'
}

# bm_stem <bbox> <YYYY-MM-DD> <product> -> full filename stem (no extension)
# bbox is the CLI's own order: min_lon,min_lat,max_lon,max_lat
bm_stem() {
  local bbox="$1" date="$2" product="$3"
  local min_lon min_lat max_lon max_lat corners
  IFS=, read -r min_lon min_lat max_lon max_lat <<< "${bbox}"
  corners="$(bm_fmt_coord "${max_lat}" lat)$(bm_fmt_coord "${min_lon}" lon)"
  corners+="$(bm_fmt_coord "${min_lat}" lat)$(bm_fmt_coord "${max_lon}" lon)"
  printf '%s_%s_%s_day' "${product}" "${corners}" "${date}"
}
