"""
Regression guard for the DPS S3-upload key in ``dps/_finalize.sh``.

The finalize step globs every product COG/PNG under ``OUT_HOME`` and uploads
each. It used to key objects by ``os.path.basename(f)``, so two products with the
same filename in different scene/product subdirs (e.g. Capella multi-scene runs,
which land in ``scene_1/``, ``scene_2/``) would silently overwrite each other in
the operational bucket. The fix keys by ``os.path.relpath(f, out_home)`` so the
sub-path is preserved. ``_finalize.sh`` is a bash-heredoc, not importable, so we
pin the intent by asserting the derivation in the script text -- and separately
check that the relpath rule actually produces distinct keys for a colliding case.
"""

import os
from pathlib import Path

_FINALIZE = Path(__file__).resolve().parents[2] / "dps" / "_finalize.sh"


def test_finalize_uses_relpath_not_basename_for_s3_key():
    text = _FINALIZE.read_text()
    # the S3-upload key must be the OUT_HOME-relative path...
    assert "os.path.relpath(f, out_home)" in text
    # ...and must NOT flatten to basename in the upload URI (the collision bug)
    assert "{os.path.basename(f)}" not in text


def test_relpath_keys_are_distinct_for_same_basename():
    # Two products with an identical basename under different subdirs must yield
    # distinct S3 keys -- the property the relpath fix guarantees.
    out_home = "/home/jovyan/drcs_outputs/202604_Flood_TX"
    a = os.path.join(out_home, "scene_1", "202604_Capella-18_sigma0.tif")
    b = os.path.join(out_home, "scene_2", "202604_Capella-18_sigma0.tif")

    key_a = os.path.relpath(a, out_home)
    key_b = os.path.relpath(b, out_home)

    assert key_a == os.path.join("scene_1", "202604_Capella-18_sigma0.tif")
    assert key_a != key_b  # no overwrite
    # the old basename scheme would have collided
    assert os.path.basename(a) == os.path.basename(b)
