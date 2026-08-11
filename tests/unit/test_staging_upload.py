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
