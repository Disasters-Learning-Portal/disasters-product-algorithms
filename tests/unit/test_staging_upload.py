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


class TestAmbientUpload:
    """``upload_dir_ambient`` -- the notebook/hub path.

    Same keying as ``upload_dir_to_staging`` (both consume ``iter_upload_keys``),
    different credentials: the default boto3 chain instead of maap-py. The MAAP
    path needs ``MAAP_PGT`` in the environment, which the DPS wrapper injects and
    the Disasters hub never does, so a notebook calling it got HTTP 401 every
    time. These pin the split so it cannot quietly collapse back.
    """

    @staticmethod
    def _bucket(name="nasa-disasters-staging"):
        boto3 = pytest.importorskip("boto3")
        moto = pytest.importorskip("moto")
        mock = moto.mock_aws()
        mock.start()
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=name)
        return mock, name

    def test_keys_match_the_maap_path_exactly(self, tmp_path, monkeypatch):
        """The two entry points differ in credentials only -- never in destination."""
        out_home = _s2_tree(tmp_path)
        mock, bucket = self._bucket()
        try:
            n = staging_upload.upload_dir_ambient(
                str(out_home), bucket, "dps_output/202601_Flood_TX", preflight=False
            )
            import boto3
            listed = {
                o["Key"] for o in boto3.client("s3", region_name="us-east-1")
                .list_objects_v2(Bucket=bucket)["Contents"]
            }
        finally:
            mock.stop()

        expected = {
            k for _, k in iter_upload_keys(str(out_home), "dps_output/202601_Flood_TX")
        }
        assert listed == expected
        assert n == len(expected) == 5

    @staticmethod
    def _published_tree(tmp_path):
        """A tree whose product directories are named as product_paths spells them.

        Processors write into a directory named EXACTLY as the published
        <Product> segment -- that is the mechanism program_data_key() uses to
        derive the key by walking up the local path. Derive the names from the
        table rather than hardcoding them, so a rename cannot leave this stale.
        """
        from shared_utils import product_paths as pp

        out_home = tmp_path / "published"
        for key in ("trueColor", "waterExtent"):
            d = out_home / "20260117" / pp.product_dir("sentinel2", key)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"S2B_MSIL2A_{key}_T17RLN_x.tif").write_text("t")
        return out_home

    def test_sensor_keying_produces_the_canonical_program_data_destination(self, tmp_path):
        out_home = self._published_tree(tmp_path)
        mock, bucket = self._bucket()
        try:
            staging_upload.upload_dir_ambient(
                str(out_home), bucket, "", sensor="sentinel2", preflight=False
            )
            import boto3
            listed = {
                o["Key"] for o in boto3.client("s3", region_name="us-east-1")
                .list_objects_v2(Bucket=bucket)["Contents"]
            }
        finally:
            mock.stop()

        assert any(k.startswith("ProgramData/Sentinel-2/TrueColor/") for k in listed)
        # No event and no date level: the activation lives in the GeoTIFF tags.
        assert not any("202601" in k or "20260117" in k for k in listed)

    def test_never_touches_maap(self, tmp_path, monkeypatch):
        """A stray maap import would reintroduce the 401 on the hub."""
        out_home = _s2_tree(tmp_path)
        monkeypatch.setattr(
            staging_upload, "_workspace_s3_client",
            lambda: pytest.fail("upload_dir_ambient must not request MAAP credentials"),
        )
        mock, bucket = self._bucket()
        try:
            staging_upload.upload_dir_ambient(str(out_home), bucket, "p", preflight=False)
        finally:
            mock.stop()

    def test_failed_preflight_raises_before_uploading_anything(self, tmp_path):
        """A per-prefix grant is the thing that actually fails, so probe first.

        head_bucket succeeds for a read-only identity, so only a real PutObject
        is evidence. Failing up front beats dying part-way through the loop with
        some products published and some not.
        """
        out_home = _s2_tree(tmp_path)
        mock, bucket = self._bucket()
        try:
            import boto3
            from shared_utils import s3_operations

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(s3_operations, "can_write_to_bucket",
                           lambda *a, **k: (False, "AccessDenied: no grant here"))
                with pytest.raises(RuntimeError, match="AccessDenied"):
                    staging_upload.upload_dir_ambient(
                        str(out_home), bucket, "p", preflight=True
                    )

            resp = boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket=bucket)
            assert resp.get("KeyCount", 0) == 0, "nothing may be uploaded after a failed probe"
        finally:
            mock.stop()

    def test_preflight_probes_every_distinct_prefix(self, tmp_path):
        """Grants are per-prefix, so one probe on one prefix proves nothing."""
        out_home = self._published_tree(tmp_path)
        mock, bucket = self._bucket()
        seen = []
        try:
            from shared_utils import s3_operations

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(s3_operations, "can_write_to_bucket",
                           lambda s3, b, prefix, **k: (seen.append(prefix), (True, None))[1])
                staging_upload.upload_dir_ambient(
                    str(out_home), bucket, "", sensor="sentinel2", preflight=True
                )
        finally:
            mock.stop()

        assert len(seen) == len(set(seen)) >= 2, f"expected one probe per prefix, got {seen}"
        assert all(p.startswith("ProgramData/Sentinel-2/") for p in seen), seen
