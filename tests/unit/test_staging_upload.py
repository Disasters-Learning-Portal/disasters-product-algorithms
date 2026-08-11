"""Unit tests for ``shared_utils.staging_upload`` (MAAP staging-bucket uploader).

The module publishes DPS products to a MAAP org bucket (nasa-disasters-staging)
using ``maap.aws.workspace_bucket_credentials()``. maap-py is a DPS-only dep, so:
  * the module must import with NO maap-py present (maap imported lazily), and
  * the credential-response parsing (which bucket is writable, what S3 keys to use)
    is factored into pure helpers so it's testable without maap-py or live AWS.
"""

import ast
import inspect
import os

import pytest

from shared_utils import staging_upload
from shared_utils.staging_upload import (
    iter_upload_keys,
    resolve_authorized_path,
)


def test_module_has_no_top_level_maap_import():
    # maap-py is a DPS-ONLY dependency; importing shared_utils.staging_upload must
    # not require it. Assert no module-level ``import maap...`` (it lives inside
    # _workspace_s3_client so only a live DPS job pulls it in).
    tree = ast.parse(inspect.getsource(staging_upload))
    top_modules = []
    for node in tree.body:  # module-level statements only
        if isinstance(node, ast.Import):
            top_modules += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            top_modules.append(node.module or "")
    assert not any("maap" in m for m in top_modules), top_modules


def test_resolve_authorized_path_read_write_empty_prefix():
    resp = {
        "credentials": {"aws_access_key_id": "AKIA...", "aws_secret_access_key": "x",
                        "aws_session_token": "y"},
        "authorized_s3_paths": [
            {"bucket": "maap-ops-workspace", "prefix": "shared/kyle/",
             "access": "read_write", "type": "workspace"},
            {"bucket": "nasa-disasters-staging", "prefix": "",
             "access": "read_write", "type": "org"},
        ],
    }
    assert resolve_authorized_path(resp, "nasa-disasters-staging") == ""


def test_resolve_authorized_path_returns_granted_prefix():
    resp = {"authorized_s3_paths": [
        {"bucket": "nasa-disasters-staging", "prefix": "team/", "access": "read_write"},
    ]}
    assert resolve_authorized_path(resp, "nasa-disasters-staging") == "team/"


def test_resolve_authorized_path_read_only_rejected():
    resp = {"authorized_s3_paths": [
        {"bucket": "nasa-disasters-staging", "prefix": "", "access": "read_only"},
    ]}
    with pytest.raises(RuntimeError) as excinfo:
        resolve_authorized_path(resp, "nasa-disasters-staging")
    msg = str(excinfo.value)
    assert "read_only" in msg
    assert "nasa-disasters-staging" in msg  # never-silent: names the bucket


def test_resolve_authorized_path_missing_bucket_lists_authorized():
    resp = {"authorized_s3_paths": [
        {"bucket": "maap-ops-workspace", "prefix": "", "access": "read_write"},
    ]}
    with pytest.raises(RuntimeError) as excinfo:
        resolve_authorized_path(resp, "nasa-disasters-staging")
    msg = str(excinfo.value)
    assert "nasa-disasters-staging" in msg       # what we wanted
    assert "maap-ops-workspace" in msg           # what WAS granted (debug aid)


def test_resolve_authorized_path_bad_shape():
    with pytest.raises(RuntimeError):
        resolve_authorized_path(["not", "a", "dict"], "nasa-disasters-staging")
    with pytest.raises(RuntimeError) as excinfo:
        resolve_authorized_path({"credentials": {}}, "nasa-disasters-staging")
    assert "authorized_s3_paths" in str(excinfo.value)


def test_iter_upload_keys_preserves_subpaths_and_filters(tmp_path):
    out_home = tmp_path / "202601_KyleWx_US"
    (out_home / "scene_1").mkdir(parents=True)
    (out_home / "a.tif").write_text("t")
    (out_home / "a.png").write_text("p")
    (out_home / "scene_1" / "b.tif").write_text("t")
    (out_home / "notes.txt").write_text("ignore me")  # must be excluded

    base = "dps_output/202601_KyleWx_US"
    keys = {key for _, key in iter_upload_keys(str(out_home), base)}

    assert keys == {
        "dps_output/202601_KyleWx_US/a.tif",
        "dps_output/202601_KyleWx_US/a.png",
        os.path.join("dps_output/202601_KyleWx_US", "scene_1", "b.tif"),
    }
    # only rasters/quicklooks, never the .txt
    assert not any(k.endswith(".txt") for k in keys)


def test_iter_upload_keys_empty_base_is_relpath(tmp_path):
    out_home = tmp_path / "evt"
    out_home.mkdir()
    (out_home / "only.tif").write_text("t")
    pairs = list(iter_upload_keys(str(out_home), ""))
    assert pairs == [(str(out_home / "only.tif"), "only.tif")]


def _s2_tree(tmp_path):
    """A Sentinel-2 output tree with the scratch files an operator run leaves behind."""
    out_home = tmp_path / "output"
    for rel in (
        "20260117/waterExtent/S2B_MSIL2A_waterExtent_NSTD_1_T17RLN_x.tif",
        "20260117/waterExtent/B8_merged.tif",
        "20260117/trueColor/S2B_MSIL2A_trueColor_T17RLN_x.tif",
        "20260117/trueColor/S2B_MSIL2A_trueColor_T17RLN_merged_x.tif",
        "20260117/trueColor/scratch.tmp.tif",
    ):
        p = out_home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("t")
    return out_home


def test_iter_upload_keys_include_none_publishes_everything(tmp_path):
    """Default stays None so every run.sh is unaffected -- a DPS OUT_HOME is all products."""
    out_home = _s2_tree(tmp_path)
    keys = {k for _, k in iter_upload_keys(str(out_home), "p")}
    assert len(keys) == 5
    assert any(k.endswith("B8_merged.tif") for k in keys)
    assert any(k.endswith("scratch.tmp.tif") for k in keys)


def test_iter_upload_keys_include_skips_scratch_but_keeps_water_extent(tmp_path):
    """The notebook predicate: waterExtent must publish; intermediates must not.

    waterExtent is the regression this guards -- the old PRODUCT_FOLDERS lookup
    keyed it as "WE" and matched "_WE", which never occurs in "_waterExtent",
    so every water-extent COG was silently skipped.
    """
    out_home = _s2_tree(tmp_path)

    def is_product(path):
        name = os.path.basename(path)
        return not name.endswith(".tmp.tif") and name != "B8_merged.tif"

    keys = {k for _, k in iter_upload_keys(str(out_home), "p", include=is_product)}

    assert any("waterExtent" in k for k in keys), "waterExtent must be published"
    assert not any(k.endswith("B8_merged.tif") for k in keys)
    assert not any(k.endswith(".tmp.tif") for k in keys)
    assert len(keys) == 3


def test_iter_upload_keys_include_merge_only(tmp_path):
    """With ENABLE_MERGE the per-tile inputs are plain rasters, not COGs -- skip them."""
    out_home = _s2_tree(tmp_path)

    def merged_products(path):
        name = os.path.basename(path)
        if name.endswith(".tmp.tif") or name == "B8_merged.tif":
            return False
        return "merged" in name

    keys = {k for _, k in iter_upload_keys(str(out_home), "p", include=merged_products)}
    assert keys == {"p/20260117/trueColor/S2B_MSIL2A_trueColor_T17RLN_merged_x.tif"}
