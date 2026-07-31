# dps/_finalize.sh -- shared output handling, sourced by each sensor run.sh.
#
# Contract: the caller has already produced the product COG(s) in ${OUT_HOME}
# and set these variables:
#   OUT_HOME ACTIVATION_EVENT SAVE_PNG PNG_MIN PNG_MAX
#   ENABLE_S3_UPLOAD S3_BUCKET S3_DEST_BASE DELETE_COG
# Optional staging override (default off; only Sentinel-2 sets these today): when
#   STAGING_UPLOAD=true the publish step targets the MAAP org bucket STAGING_BUCKET
#   under STAGING_DEST_BASE/<event> using short-lived MAAP workspace credentials
#   (the DPS worker's own role can't write there) instead of the ambient S3_BUCKET
#   upload. See shared_utils/staging_upload.py.
#
# Flow: optional PNG quicklook -> copy products into output/ (DPS uploads these,
# so the COG is never lost) -> optional publish to S3 (operational bucket via the
# worker role, or the MAAP staging bucket when STAGING_UPLOAD=true) -> optionally
# delete the COGs from ${OUT_HOME} to free home-dir space (PNGs + output/ kept).

mkdir -p output

# 1) PNG quicklooks (one .png next to each COG, in OUT_HOME)
if [[ "${SAVE_PNG}" == "true" ]]; then
  echo "Generating PNG quicklooks in ${OUT_HOME} ..."
  conda run --live-stream --name disasters_dps python - "${OUT_HOME}" "${PNG_MIN}" "${PNG_MAX}" <<'PY'
import os, sys, glob
from shared_utils.plotting import save_cog_png
out_home, pmin, pmax = sys.argv[1], sys.argv[2], sys.argv[3]
vmin = float(pmin) if pmin else None
vmax = float(pmax) if pmax else None
for cog in sorted(glob.glob(os.path.join(out_home, "**", "*.tif"), recursive=True)):
    try:
        save_cog_png(cog, os.path.splitext(cog)[0] + ".png", vmin=vmin, vmax=vmax)
    except Exception as e:
        print(f"  WARN: png failed for {cog}: {e}")
PY
fi

# 2) copy products (COG + PNG) into the DPS output/ dir -- DPS uploads these
cp -r "${OUT_HOME}/." output/ 2>/dev/null || true

# 3) optional: publish products (COG + PNG) to S3
if [[ "${ENABLE_S3_UPLOAD}" == "true" ]]; then
  if [[ "${STAGING_UPLOAD:-false}" == "true" ]]; then
    # 3a) MAAP staging path: publish to a MAAP org bucket (e.g. nasa-disasters-
    #     staging) using short-lived MAAP workspace credentials -- the DPS worker's
    #     ambient role can't write there. shared_utils.staging_upload keys objects
    #     by their OUT_HOME-relative path (same collision-safety as 3b).
    STAGING_PREFIX="${STAGING_DEST_BASE}/${ACTIVATION_EVENT}"
    echo "Publishing products to s3://${STAGING_BUCKET}/${STAGING_PREFIX}/ (MAAP workspace creds) ..."
    conda run --live-stream --name disasters_dps python - "${STAGING_BUCKET}" "${STAGING_PREFIX}" "${OUT_HOME}" <<'PY'
import sys
from shared_utils.staging_upload import upload_dir_to_staging
bucket, dest_prefix, out_home = sys.argv[1], sys.argv[2], sys.argv[3]
upload_dir_to_staging(out_home, bucket, dest_prefix)
PY
  else
    # 3b) operational path: publish to S3_BUCKET via the DPS worker's ambient role
    S3_PREFIX="${S3_DEST_BASE}/${ACTIVATION_EVENT}"
    echo "Publishing products to s3://${S3_BUCKET}/${S3_PREFIX}/ ..."
    conda run --live-stream --name disasters_dps python - "${S3_BUCKET}" "${S3_PREFIX}" "${OUT_HOME}" <<'PY'
import os, sys, glob
from shared_utils import upload_file_to_s3
bucket, prefix, out_home = sys.argv[1], sys.argv[2], sys.argv[3]
files = sorted(glob.glob(os.path.join(out_home, "**", "*.tif"), recursive=True) +
               glob.glob(os.path.join(out_home, "**", "*.png"), recursive=True))
for f in files:
    # Preserve the sub-path under OUT_HOME in the S3 key (mirrors the output/
    # cp -r tree) so same-named COGs in different scene/product subdirs don't
    # overwrite each other. For the flat single-file case relpath == basename.
    rel = os.path.relpath(f, out_home)
    upload_file_to_s3(f, f"s3://{bucket}/{prefix}/{rel}")
print(f"Uploaded {len(files)} file(s) to s3://{bucket}/{prefix}/")
PY
  fi
fi

# 4) free home-dir space: delete the COGs from OUT_HOME (default true). The PNGs
#    stay as lightweight previews and the output/ copy is retained for DPS.
if [[ "${DELETE_COG}" == "true" ]]; then
  echo "Deleting COGs from ${OUT_HOME} (PNGs kept; output/ copy retained for DPS) ..."
  find "${OUT_HOME}" -type f -name '*.tif' -delete
fi
