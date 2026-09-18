"""
Regression tests for notebooks/tools/product_roundtrip_smoketest.ipynb.

That notebook is the fast "does every product still publish to the right place?"
check: for each (sensor, product) in shared_utils.product_paths.PRODUCT_DIRS it
converts a committed fixture to a tagged COG, uploads it to that product's
canonical ProgramData key, reads it back, deletes it and confirms it is gone.

Its real destination is the LIVE staging bucket, so the parts that matter most --
that the key is the canonical one, that an existing object is never overwritten,
and that every uploaded object is deleted again -- cannot be proved by running it
for real without writing into published prefixes. They are proved here instead,
against a moto bucket, by EXTRACTING and exec'ing the notebook's own cells (the
pattern in test_staging_notebooks.py). Nothing here reimplements the notebook.
"""
import os
import shutil

import pytest

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
pytest.importorskip("rasterio")
pytest.importorskip("rio_cogeo")
nbformat = pytest.importorskip("nbformat")
pytest.importorskip("shared_utils.product_paths")  # needs the package installed

from moto import mock_aws

from shared_utils import product_paths as pp

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
NB = os.path.join(REPO_ROOT, "notebooks", "tools", "product_roundtrip_smoketest.ipynb")
BUCKET = pp.STAGING_BUCKET

INPUTS = "# ---- INPUTS ----"
METADATA = "ACTIVATION_METADATA = {"
PLAN = "# ---- PLAN ----"
PREFLIGHT = "# ---- PREFLIGHT ----"
ROUNDTRIP = "# ---- ROUND TRIP ----"
RESULTS = "# ---- RESULTS ----"
SWEEP = "# ---- SWEEP ----"
NB_CHECK = "# ---- NOTEBOOK PATH CHECK ----"

# convert_to_cog shells out for two steps: `rio cogeo` on the default backend and
# `gdal_translate -of VRT -a_nodata none` for the nodata=False (composite) branch.
needs_gdal_cli = pytest.mark.skipif(
    shutil.which("rio") is None or shutil.which("gdal_translate") is None,
    reason="needs `rio` and `gdal_translate` on PATH (conda GDAL)",
)


def _cell(*markers):
    nb = nbformat.read(NB, as_version=4)
    hits = [c.source for c in nb.cells
            if c.cell_type == "code" and all(m in c.source for m in markers)]
    assert len(hits) == 1, f"expected 1 cell with {markers}, got {len(hits)}"
    return hits[0]


def _exec(ns, *markers, patch=None):
    src = _cell(*markers)
    if patch:
        old, new = patch
        assert old in src, f"{markers[0]}: {old!r} not in cell"
        src = src.replace(old, new)
    exec(compile(src, f"smoketest.ipynb:{markers[0]}", "exec"), ns)
    return ns


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    # The notebook finds the repo by walking up from cwd; pytest's cwd is not
    # guaranteed, so pin it.
    monkeypatch.setenv("DPA_REPO_ROOT", REPO_ROOT)


def _make_bucket():
    s3 = boto3.client("s3", region_name="us-west-2")
    s3.create_bucket(Bucket=BUCKET,
                     CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
    return s3


def _configured(tmp_path, **overrides):
    """Run the INPUTS + metadata cells, then apply test overrides."""
    ns = _exec({}, INPUTS)
    ns["WORK_DIR"] = str(tmp_path / "work")
    ns.update(overrides)
    return _exec(ns, METADATA)


def _keys():
    s3 = boto3.client("s3", region_name="us-west-2")
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
        out += [o["Key"] for o in page.get("Contents", [])]
    return sorted(out)


# ---------------------------------------------------------------------------
# The plan: every product is covered, and every key is the canonical destination
# ---------------------------------------------------------------------------

class TestPlan:
    def test_covers_every_product_in_the_table(self, tmp_path):
        ns = _exec(_configured(tmp_path), PLAN)
        rows = ns["ROWS"]
        assert len(rows) == len(pp.PRODUCT_DIRS)
        assert {(r["sensor"], r["product"]) for r in rows} == set(pp.PRODUCT_DIRS)

    def test_skips_exactly_the_undecided_products(self, tmp_path):
        ns = _exec(_configured(tmp_path), PLAN)
        skipped = {(r["sensor"], r["product"])
                   for r in ns["ROWS"] if r["status"] == "SKIP"}
        assert skipped == set(pp.undecided_products())
        # The reason is carried, not swallowed -- an undecided path must be
        # actionable, not a blank row.
        for r in ns["ROWS"]:
            if r["status"] == "SKIP":
                assert "undecided" in r["detail"]

    def test_every_key_is_the_canonical_destination(self, tmp_path):
        """The whole point: key == ProgramData/<Sensor>/<Product>/<filename>."""
        ns = _exec(_configured(tmp_path), PLAN)
        for r in ns["ROWS"]:
            if r["status"] != "PLANNED":
                continue
            expect = pp.program_data_prefix(r["sensor"], r["product"])
            assert r["key"] == f"{expect}/{os.path.basename(r['key'])}"
            # ...and the local directory the processor would write into is the
            # same <Product> segment, which is what makes the key derivable.
            assert os.path.basename(os.path.dirname(r["local"])) == \
                pp.product_dir(r["sensor"], r["product"])

    def test_every_name_carries_the_marker(self, tmp_path):
        """A leftover object must be identifiable by name alone -- that is the
        only thing that makes the SWEEP cell safe to run against a live bucket."""
        ns = _exec(_configured(tmp_path), PLAN)
        for r in ns["ROWS"]:
            if r["status"] == "PLANNED":
                assert ns["MARKER"] in os.path.basename(r["key"])

    def test_empty_plan_is_refused(self, tmp_path):
        with pytest.raises(AssertionError, match="no products planned"):
            _exec(_configured(tmp_path, SENSORS=["nosuchsensor"]), PLAN)

    def test_every_product_has_a_fixture_that_exists(self, tmp_path):
        ns = _exec(_configured(tmp_path), PLAN)
        for r in ns["ROWS"]:
            if r["status"] == "PLANNED":
                assert os.path.exists(r["fixture"]), r["fixture"]


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------

@needs_gdal_cli
class TestRoundTrip:
    # capella (SAR) + satellogic (8-bit composites and float indices) between
    # them cover every nodata branch the loop exercises, in six rows.
    SENSORS = ["capella", "satellogic"]

    def _run(self, tmp_path, **overrides):
        ns = _configured(tmp_path, DRY_RUN=False, SENSORS=self.SENSORS, **overrides)
        _exec(ns, PLAN)
        _exec(ns, PREFLIGHT)
        _exec(ns, ROUNDTRIP)
        return ns

    def test_uploads_then_deletes_every_product(self, tmp_path):
        with mock_aws():
            _make_bucket()
            ns = self._run(tmp_path)
            rows = [r for r in ns["ROWS"] if r["status"] != "SKIP"]
            assert rows, "no rows ran"
            assert all(r["status"] == "PASS" for r in rows), \
                [(r["product"], r["detail"]) for r in rows if r["status"] != "PASS"]
            assert all(r["deleted"] is True for r in rows)
            # Nothing left behind -- the reason this can point at a live bucket.
            assert _keys() == []

    def test_the_object_really_lands_at_the_canonical_key(self, tmp_path):
        """"Uploaded and then deleted" is also what "never uploaded" looks like
        from the outside. Suppress the delete so the objects survive, and pin
        that what is sitting in the bucket is exactly the canonical key set."""
        with mock_aws():
            _make_bucket()
            ns = _configured(tmp_path, DRY_RUN=False, SENSORS=self.SENSORS)
            _exec(ns, PLAN)
            _exec(ns, PREFLIGHT)
            _exec(ns, ROUNDTRIP,
                  patch=('s3.delete_object(Bucket=S3_BUCKET, Key=r["key"])',
                         "pass  # delete suppressed by the test"))

            expected = sorted(r["key"] for r in ns["ROWS"] if r["status"] != "SKIP")
            assert expected, "no rows ran"
            assert _keys() == expected

            # Each surviving key decomposes into ProgramData/<Sensor>/<Product>/.
            for key in expected:
                root, sensor_dir, product_dir, _name = key.split("/")
                assert root == pp.PROGRAM_DATA_ROOT
                sensor = next(s for s, d in pp.SENSOR_DIRS.items() if d == sensor_dir)
                assert product_dir in pp.product_dirs_for(sensor)

    def test_an_existing_key_is_never_overwritten(self, tmp_path):
        with mock_aws():
            s3 = _make_bucket()
            ns = _configured(tmp_path, DRY_RUN=False, SENSORS=["capella"])
            _exec(ns, PLAN)
            _exec(ns, PREFLIGHT)

            victim = ns["ROWS"][0]["key"]
            s3.put_object(Bucket=BUCKET, Key=victim, Body=b"a real product")

            _exec(ns, ROUNDTRIP)

            row = ns["ROWS"][0]
            assert row["status"] == "FAIL"
            assert "refusing to overwrite" in row["detail"]
            # The pre-existing object is byte-for-byte untouched.
            body = s3.get_object(Bucket=BUCKET, Key=victim)["Body"].read()
            assert body == b"a real product"

    def test_results_cell_raises_when_a_row_failed(self, tmp_path):
        with mock_aws():
            s3 = _make_bucket()
            ns = _configured(tmp_path, DRY_RUN=False, SENSORS=["capella"])
            _exec(ns, PLAN)
            _exec(ns, PREFLIGHT)
            s3.put_object(Bucket=BUCKET, Key=ns["ROWS"][0]["key"], Body=b"x")
            _exec(ns, ROUNDTRIP)
            with pytest.raises(RuntimeError, match="product.s. failed"):
                _exec(ns, RESULTS)

    def test_dry_run_makes_no_s3_calls(self, tmp_path):
        """No bucket is created at all: any S3 call would raise NoSuchBucket."""
        with mock_aws():
            ns = _configured(tmp_path, DRY_RUN=True, SENSORS=self.SENSORS)
            _exec(ns, PLAN)
            _exec(ns, PREFLIGHT)
            _exec(ns, ROUNDTRIP)
            rows = [r for r in ns["ROWS"] if r["status"] != "SKIP"]
            assert rows and all(r["status"] == "DRY" for r in rows), \
                [(r["product"], r["detail"]) for r in rows if r["status"] != "DRY"]
            assert all(r["deleted"] is None for r in rows)
            _exec(ns, RESULTS)   # must not raise

    def test_a_wrong_nodata_expectation_fails_the_row(self, tmp_path):
        """The loop's nodata assertion has to actually fire. It is the only thing
        pinning the three-way convert_to_cog(nodata=...) contract per product, and
        a check that never fails is worse than none -- it reads as coverage."""
        with mock_aws():
            _make_bucket()
            ns = _configured(tmp_path, DRY_RUN=False, SENSORS=["capella"])
            _exec(ns, PLAN)
            _exec(ns, PREFLIGHT)
            ns["ROWS"][0]["nodata_out"] = 42.0      # SAR dB is -9999.0
            _exec(ns, ROUNDTRIP)
            row = ns["ROWS"][0]
            assert row["status"] == "FAIL"
            assert "nodata is -9999.0, expected 42.0" in row["detail"]
            # A failed row must not leave its object behind.
            assert _keys() == []

    def test_a_missing_tag_fails_the_row(self, tmp_path):
        """Same for the tag check: prove _check_tags raises rather than passing
        whatever it is handed."""
        with mock_aws():
            _make_bucket()
            ns = _configured(tmp_path, DRY_RUN=False, SENSORS=["capella"])
            _exec(ns, PLAN)
            _exec(ns, PREFLIGHT)
            ns["REQUIRED_TAGS"] = ns["REQUIRED_TAGS"] + ("NO_SUCH_TAG",)
            _exec(ns, ROUNDTRIP)
            row = ns["ROWS"][0]
            assert row["status"] == "FAIL"
            assert "missing tag(s): NO_SUCH_TAG" in row["detail"]
            assert _keys() == []

    def test_the_cog_carries_every_activation_tag(self, tmp_path):
        import rasterio
        with mock_aws():
            _make_bucket()
            ns = self._run(tmp_path)
            for r in ns["ROWS"]:
                if r["status"] != "PASS":
                    continue
                with rasterio.open(r["local"]) as src:
                    tags = src.tags()
                    assert src.nodata == r["nodata_out"], r["product"]
                for tag in ns["REQUIRED_TAGS"]:
                    assert tags.get(tag), f"{r['product']} missing {tag}"
                assert tags["ACTIVATION_EVENT"] == ns["EVENT_NAME"]


# ---------------------------------------------------------------------------
# The sweep: only ever touches marked keys
# ---------------------------------------------------------------------------

class TestSweep:
    REAL = f"{pp.program_data_prefix('capella', 'sigma0')}/Capella-13_sigma0_filtered5_2026-03-21T05:29:08Z.tif"

    def _seed(self, marker):
        s3 = _make_bucket()
        orphan = f"{pp.program_data_prefix('umbra', 'sigma0')}/{marker}-Umbra-abc_sigma0_filtered5_1970-01-01T00:00:00Z.tif"
        s3.put_object(Bucket=BUCKET, Key=self.REAL, Body=b"real product")
        s3.put_object(Bucket=BUCKET, Key=orphan, Body=b"leftover")
        return s3, orphan

    def _ns(self, tmp_path):
        ns = _configured(tmp_path)
        ns["s3"] = boto3.client("s3", region_name="us-west-2")
        os.makedirs(ns["WORK_DIR"], exist_ok=True)
        return ns

    def test_selects_only_marked_keys(self, tmp_path):
        with mock_aws():
            ns = self._ns(tmp_path)
            _, orphan = self._seed(ns["MARKER"])
            _exec(ns, SWEEP)
            assert ns["orphans"] == [orphan]

    def test_confirm_deletes_the_orphan_and_spares_the_real_product(self, tmp_path):
        with mock_aws():
            ns = self._ns(tmp_path)
            s3, orphan = self._seed(ns["MARKER"])
            _exec(ns, SWEEP, patch=("SWEEP_CONFIRM = False", "SWEEP_CONFIRM = True"))
            assert _keys() == [self.REAL]
            assert s3.get_object(Bucket=BUCKET, Key=self.REAL)["Body"].read() == b"real product"

    def test_the_marker_filter_is_what_spares_real_products(self, tmp_path):
        """Defence in depth: the sweep filters on MARKER when listing AND again
        before deleting. Remove both and a real product would be swept -- which is
        what proves the filter, not the listing, is doing the work."""
        with mock_aws():
            ns = self._ns(tmp_path)
            self._seed(ns["MARKER"])
            _exec(ns, SWEEP,
                  patch=('if MARKER not in os.path.basename(key):\n        continue',
                         'if False:\n        continue'))
            unguarded = set(ns["orphans"])
            # With only the second guard gone the listing filter still holds.
            assert self.REAL not in unguarded

            ns2 = self._ns(tmp_path / "again")
            src_patched = _cell(SWEEP) \
                .replace('if MARKER in os.path.basename(obj["Key"]):', 'if True:') \
                .replace('if MARKER not in os.path.basename(key):\n        continue',
                         'if False:\n        continue')
            exec(compile(src_patched, "sweep-unguarded", "exec"), ns2)
            assert self.REAL in set(ns2["orphans"]), \
                "with both marker filters removed the real product should be selected"

    def test_without_confirmation_nothing_is_deleted(self, tmp_path):
        with mock_aws():
            ns = self._ns(tmp_path)
            _, orphan = self._seed(ns["MARKER"])
            _exec(ns, SWEEP)
            assert _keys() == sorted([self.REAL, orphan])


# ---------------------------------------------------------------------------
# The static check of the seven workflow notebooks
# ---------------------------------------------------------------------------

class TestWorkflowNotebookCheck:
    def test_all_workflow_notebooks_pass(self, tmp_path):
        ns = _exec(_configured(tmp_path), NB_CHECK)
        assert ns["problems"] == []
        assert len(ns["nb_paths"]) == 7, ns["nb_paths"]

    def test_a_hardcoded_program_data_path_is_caught(self, tmp_path):
        """The check must actually fire -- a regex this specific is easy to write
        so loosely that it never matches anything."""
        ns = _exec(_configured(tmp_path), NB_CHECK)
        sensor_dir = pp.SENSOR_DIRS["sentinel2"]
        assert ns["HARDCODED"].search(f'f"ProgramData/{sensor_dir}/NDVI/{{name}}"')
        # ...and must NOT fire on the placeholder every notebook prints, nor on a
        # path that appears only in a comment.
        assert not ns["HARDCODED"].search('f"ProgramData/<Sensor>/<Product>/"')
        assert not ns["HARDCODED"].search(f'f"ProgramData/{sensor_dir}/<Product>/"')
        assert not ns["HARDCODED"].search(
            ns["strip_comments"](f'x = 1  # writes to ProgramData/{sensor_dir}/NDVI/')
        )
