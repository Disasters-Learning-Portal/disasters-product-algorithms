# dps/_finalize.sh -- shared output handling, sourced by each sensor run.sh.
#
# Contract: the caller has already produced the product COG(s) in ${OUT_HOME}
# and set these variables:
#   OUT_HOME ACTIVATION_EVENT ENABLE_S3_UPLOAD S3_BUCKET S3_DEST_BASE DELETE_COG
# Staging publish (ALL sensors set these today): when STAGING_UPLOAD=true the
#   publish step targets the MAAP org bucket STAGING_BUCKET under
#   STAGING_DEST_BASE/<event> using short-lived MAAP workspace credentials (the DPS
#   worker's own role can't write there) instead of the ambient S3_BUCKET upload.
#   See shared_utils/staging_upload.py.
#
# Flow: copy products into output/ (DPS uploads these, so the COG is never lost)
# -> publish to S3 (the MAAP staging bucket when STAGING_UPLOAD=true, else the
# ambient operational bucket) -> delete the COGs from ${OUT_HOME} to free home-dir
# space (the output/ copy is retained for DPS). No PNG quicklooks are produced.

# DPS_DRY_RUN=1 -- TEST HOOK. Copies products into output/ as usual, then SKIPS the
# S3 publish and the scratch delete, printing what it would have done. This is what
# lets tests/e2e run a real run.sh end to end (real download, real COG, real event
# bake) without writing to nasa-disasters-staging or destroying the products the test
# then inspects. It is an ENVIRONMENT variable on purpose: a DPS job receives named
# CWL flags and never sets env, so no job input can reach it and the "publishing is
# always on, destination is locked" invariant still holds for every real run.
mkdir -p output

# 1) copy products into the DPS output/ dir -- DPS uploads these
cp -r "${OUT_HOME}/." output/ 2>/dev/null || true

if [[ "${DPS_DRY_RUN:-0}" == "1" ]]; then
  echo "DPS_DRY_RUN=1: skipping S3 publish and scratch-COG delete (products left in ${OUT_HOME} and output/)."
  return 0 2>/dev/null || exit 0
fi

# 2) publish products (COG) to S3
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
files = sorted(glob.glob(os.path.join(out_home, "**", "*.tif"), recursive=True))
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

# 3) free home-dir space: delete the COGs from OUT_HOME (locked on). The output/
#    copy is retained for DPS's own upload, so nothing is lost.
if [[ "${DELETE_COG}" == "true" ]]; then
  echo "Deleting COGs from ${OUT_HOME} (output/ copy retained for DPS) ..."
  find "${OUT_HOME}" -type f -name '*.tif' -delete
fi
