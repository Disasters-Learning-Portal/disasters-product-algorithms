# dps/_finalize.sh -- shared output handling, sourced by each sensor run.sh.
#
# Contract: the caller has already produced the product COG(s) in ${OUT_HOME}
# and set these variables:
#   OUT_HOME ACTIVATION_EVENT SAVE_PNG PNG_MIN PNG_MAX
#   ENABLE_S3_UPLOAD S3_BUCKET S3_DEST_BASE DELETE_COG
#
# Flow: optional PNG quicklook -> copy products into output/ (DPS uploads these,
# so the COG is never lost) -> optional publish to the operational S3 bucket ->
# optionally delete the COGs from ${OUT_HOME} to free home-dir space (the PNGs
# and the output/ copy are kept).

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

# 3) optional: publish products (COG + PNG) to the operational S3 bucket
if [[ "${ENABLE_S3_UPLOAD}" == "true" ]]; then
  S3_PREFIX="${S3_DEST_BASE}/${ACTIVATION_EVENT}"
  echo "Publishing products to s3://${S3_BUCKET}/${S3_PREFIX}/ ..."
  conda run --live-stream --name disasters_dps python - "${S3_BUCKET}" "${S3_PREFIX}" "${OUT_HOME}" <<'PY'
import os, sys, glob
from shared_utils import upload_file_to_s3
bucket, prefix, out_home = sys.argv[1], sys.argv[2], sys.argv[3]
files = sorted(glob.glob(os.path.join(out_home, "**", "*.tif"), recursive=True) +
               glob.glob(os.path.join(out_home, "**", "*.png"), recursive=True))
for f in files:
    upload_file_to_s3(f, f"s3://{bucket}/{prefix}/{os.path.basename(f)}")
print(f"Uploaded {len(files)} file(s) to s3://{bucket}/{prefix}/")
PY
fi

# 4) free home-dir space: delete the COGs from OUT_HOME (default true). The PNGs
#    stay as lightweight previews and the output/ copy is retained for DPS.
if [[ "${DELETE_COG}" == "true" ]]; then
  echo "Deleting COGs from ${OUT_HOME} (PNGs kept; output/ copy retained for DPS) ..."
  find "${OUT_HOME}" -type f -name '*.tif' -delete
fi
