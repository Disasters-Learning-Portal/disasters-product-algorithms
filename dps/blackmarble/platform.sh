# Black Marble PLATFORM table -- SOURCED, never executed.
#
# Black Marble runs on a VIIRS nighttime-lights product, and there are two of them. The
# job picks one with its `viirs_product` input (VJ146A2 by default); run.sh maps that
# short name to a platform token with bm_platform_for_short_name, and everything below
# is keyed by the platform token:
#
#   snpp    Suomi-NPP  VNP46A2   the original. Suomi-NPP data product DELIVERY CEASES
#                                2026-11-01 (earthdata.nasa.gov alert). The archive stays
#                                readable, but there is no forward stream after that date.
#   noaa20  NOAA-20    VJ146A2   the JPSS-1 twin, NOT affected by that outage.
#
# The two products are structural twins -- same CMR version (2), same LAADS provider, same
# h5v/h5h tiling, same HDF-EOS5 grid path, same Gap_Filled_DNB_BRDF-Corrected_NTL layer,
# same -999.9 fill -- which is why one pipeline serves both and only this table changes.
# See disasters-portal#365, issue #186 and docs/DPS.md "Black Marble: choosing the VIIRS product".
#
# Kept out of run.sh for the same reason dps/_validate.sh and naming.sh are separate files:
# it is the only way the values can be asserted by the test suite. A product token or a
# date floor that silently drifts is a wrong-answer bug the job still "succeeds" through.
# Pinned by tests/unit/test_blackmarble_platform.py.
#
# Every accessor takes the platform as its first argument and dies on an unknown one, so a
# typo can never fall through to a default and quietly process the wrong satellite.

# bm_platforms -> the supported platform tokens, space separated (for validate_in_set).
bm_platforms() { printf 'snpp noaa20'; }

# bm_viirs_products -> the product short names the `viirs_product` job input accepts,
# space separated (for validate_in_set). Exactly upstream's --viirs-product choices.
bm_viirs_products() { printf 'VJ146A2 VNP46A2'; }

# bm_platform_for_short_name SHORT_NAME -> the platform token that downloads it; the
# inverse of bm_platform_short_name.
bm_platform_for_short_name() {
  case "$1" in
    VNP46A2) printf 'snpp' ;;
    VJ146A2) printf 'noaa20' ;;
    *)       die "unknown VIIRS product '$1' (expected: $(bm_viirs_products))." ;;
  esac
}

# bm_platform_product PLATFORM -> product/folder token for the main raster.
# Distinct per product so a VJ146A2 run can never overwrite a VNP46A2 run for the same
# activation event + date (_finalize.sh keys S3 objects by the OUT_HOME-relative path,
# which is <YYYYMMDD>/<product>/<file>).
bm_platform_product() {
  case "$1" in
    snpp)   printf 'hdnightlights' ;;
    noaa20) printf 'hdnightlightsnoaa20' ;;
    *)      die "unknown Black Marble platform '$1' (expected: $(bm_platforms))." ;;
  esac
}

# bm_platform_short_name PLATFORM -> the NASA product short name it downloads.
bm_platform_short_name() {
  case "$1" in
    snpp)   printf 'VNP46A2' ;;
    noaa20) printf 'VJ146A2' ;;
    *)      die "unknown Black Marble platform '$1' (expected: $(bm_platforms))." ;;
  esac
}

# bm_platform_min_date PLATFORM -> first date the product exists (CMR collection
# TemporalExtents.BeginningDateTime, verified live). A date before this returns zero
# granules, which upstream turns into an obscure failure well after the Earthdata login --
# validate it up front instead.
bm_platform_min_date() {
  case "$1" in
    snpp)   printf '2012-01-19' ;;
    noaa20) printf '2018-01-19' ;;
    *)      die "unknown Black Marble platform '$1' (expected: $(bm_platforms))." ;;
  esac
}

# bm_platform_source PLATFORM -> the SOURCE tag baked into the output COG.
# Mirrors the per-sensor SOURCE constants (cf. capella_v2.SOURCE): describes what the
# product is made of, not who ran it. Kept in step with dps/blackmarble/bake_event.py,
# which reads it through the same table.
bm_platform_source() {
  case "$1" in
    snpp)   printf 'NASA Black Marble VNP46A2 (VIIRS/snpp) + Landsat + OSM' ;;
    noaa20) printf 'NASA Black Marble VJ146A2 (VIIRS/noaa20) + Landsat + OSM' ;;
    *)      die "unknown Black Marble platform '$1' (expected: $(bm_platforms))." ;;
  esac
}

# Date after which Suomi-NPP product delivery ceases (earthdata.nasa.gov alert,
# disasters-portal#365). Not a hard failure -- the archive is still served, so a historical
# activation runs fine; run.sh only WARNS and suggests viirs_product=VJ146A2.
BM_SNPP_SUNSET_DATE="2026-11-01"
