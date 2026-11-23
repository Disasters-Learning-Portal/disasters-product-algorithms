"""Pins notebooks/sentinel2_odr_workflow.ipynb against the defects that
shipped in the original draft (PR #134).

Notebooks are NOT linted or smoke-tested in CI, so every one of these was
invisible until someone ran the notebook and looked at the result -- or, worse,
did not look. Each test below corresponds to a real defect in the draft:

  * EVENT_NAME was free text ("Sentinel_test"), which resolve_metadata cannot
    split into YEAR_MONTH / HAZARD / LOCATION -- every product shipped with
    incomplete tags.
  * SOURCE was "CSDA", the commercial smallsat program. Sentinel-2 is ESA
    Copernicus; Earth Search only redistributes it.
  * ENABLE_S3_UPLOAD defaulted to True, so a first top-to-bottom run published
    to the production bucket under a placeholder prefix.
  * PRODUCT_FOLDERS had no `swir` entry, so that product was silently skipped
    by the upload; it also carried a `pan` key with no S2 equivalent.
  * The merge filter read globals().get("ENABLE_MERGE"), a name the config cell
    never defines (it defines MERGE) -- the filter was dead.
  * The upload globbed OUTPUT_DIR recursively and unscoped, so it would publish
    leftovers from a previous activation AND the water-extent product's
    CDL / WorldCover reference cache.

A second round (PR #190) hand-edited the generated notebook and shipped three
more, which the later classes pin:

  * The upload wrote every product to `Sentinel_Test/` -- the canonical call was
    wrapped in a string literal, so it did nothing -- while the closing message
    still printed the `ProgramData/` destination.
  * The water-extent product was dropped from PRODUCTS, though the CLI and the
    DPS job still produce it.
  * Re-running the process cell reported "0 COGs produced": NEW_COGS was a
    difference of file NAMES, and the CLI rewrites the same names.

The tests exec the notebook's real cells rather than re-describing them, in the
style of tests/unit/test_simple_disaster_naming.py.
"""
import ast
import json
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NB_PATH = os.path.join(REPO_ROOT, "notebooks", "sentinel2_odr_workflow.ipynb")


@pytest.fixture(scope="module")
def nb():
    with open(NB_PATH) as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def cells(nb):
    return ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]


def cell_containing(cells, needle):
    for src in cells:
        if needle in src:
            return src
    raise AssertionError(f"no code cell contains {needle!r}")


@pytest.fixture(scope="module")
def config_src(cells):
    return cell_containing(cells, "ACTIVATION OPTIONS")


@pytest.fixture(scope="module")
def validation_src(cells):
    return cell_containing(cells, "ACTIVATION METADATA")


@pytest.fixture(scope="module")
def upload_src(cells):
    return cell_containing(cells, "PRODUCT_FOLDERS")


@pytest.fixture(scope="module")
def process_src(cells):
    return cell_containing(cells, "PROCESS SELECTED PRODUCTS")


def exec_config(config_src, **overrides):
    """Run the config cell, then apply overrides. Returns its namespace."""
    ns = {}
    exec(compile(config_src, "config_cell", "exec"), ns)
    ns.update(overrides)
    return ns


# ---------------------------------------------------------------------
# Notebook integrity
# ---------------------------------------------------------------------

class TestNotebookIsWellFormed:

    def test_exists_and_parses(self, nb):
        assert nb["nbformat"] == 4
        assert nb["cells"]

    def test_every_code_cell_is_valid_python(self, cells):
        for i, src in enumerate(cells):
            ast.parse(src)  # raises SyntaxError with the offending cell

    def test_no_cell_outputs_are_committed(self, nb):
        for c in nb["cells"]:
            if c["cell_type"] == "code":
                assert c.get("outputs") == []
                assert c.get("execution_count") is None

    def test_it_drives_the_stac_cli_not_the_safe_one(self, cells):
        src = cell_containing(cells, "process_sentinel2_odr")
        assert '"process_sentinel2",' not in src, (
            "this notebook must drive process_sentinel2_odr; the .SAFE CLI "
            "is driven by sentinel2_workflow.ipynb"
        )


# ---------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------

class TestConfigDefaults:

    def test_s3_upload_is_off_by_default(self, config_src):
        """The draft shipped True, so a first run published to production."""
        assert exec_config(config_src)["ENABLE_S3_UPLOAD"] is False

    def test_source_is_copernicus_not_csda(self, config_src):
        """CSDA is the commercial smallsat program, not Sentinel-2."""
        assert exec_config(config_src)["SOURCE"] == "Copernicus"

    def test_event_name_matches_the_repo_convention(self, config_src):
        import re
        event = exec_config(config_src)["EVENT_NAME"]
        assert re.match(r"^\d{6}_[A-Za-z]+_.+$", event), event

    def test_bbox_is_well_formed(self, config_src):
        bbox = exec_config(config_src)["BBOX"]
        assert len(bbox) == 4
        assert bbox[0] < bbox[2] and bbox[1] < bbox[3]

    def test_at_least_one_product_is_enabled(self, config_src):
        assert any(exec_config(config_src)["PRODUCTS"].values())

    def test_merge_is_defined_so_the_upload_filter_is_live(self, config_src):
        """The draft's upload cell read ENABLE_MERGE, which nothing defined."""
        ns = exec_config(config_src)
        assert "MERGE" in ns
        assert "ENABLE_MERGE" not in ns

    def test_every_cli_product_is_offered(self, config_src):
        """The notebook is the operator's only route to a product, so PRODUCTS
        must list everything `process_sentinel2_odr --product` accepts. PR #190
        added `cloud_mask` and, in the same edit, dropped `we`."""
        pytest.importorskip("rasterio")
        from sentinel2 import process_sentinel2_odr as cli

        offered = (cli.INDEX_PRODUCTS | cli.COMPOSITE_PRODUCTS
                   | cli.WATER_EXTENT_PRODUCTS | cli.CLOUD_MASK_PRODUCTS)
        assert set(exec_config(config_src)["PRODUCTS"]) == offered

    def test_water_extent_thresholds_are_configurable(self, config_src, process_src):
        assert exec_config(config_src)["WE_NSTD"] == [1]
        assert '"--we-nstd"' in process_src


# ---------------------------------------------------------------------
# Validation cell
# ---------------------------------------------------------------------

class TestValidationCellFailsLoudly:

    def _run(self, config_src, validation_src, **overrides):
        ns = exec_config(config_src, **overrides)
        exec(compile(validation_src, "validation_cell", "exec"), ns)
        return ns

    def test_the_shipped_placeholder_is_rejected(self, config_src, validation_src):
        """A template that runs unedited is a template that publishes junk."""
        with pytest.raises(ValueError, match="placeholder"):
            self._run(config_src, validation_src)

    def test_free_text_event_name_is_rejected(self, config_src, validation_src):
        with pytest.raises(ValueError, match="YYYYMM_Hazard_Location"):
            self._run(config_src, validation_src, EVENT_NAME="Sentinel_test")

    def test_cloud_mask_on_l1c_is_rejected(self, config_src, validation_src):
        with pytest.raises(ValueError, match="Scene Classification Layer"):
            self._run(
                config_src, validation_src,
                EVENT_NAME="202601_Flood_TX", LEVEL="1", CLOUD_MASK=True,
            )

    def test_cloud_mask_product_on_l1c_is_rejected(self, config_src, validation_src):
        """The cloud-mask PRODUCT reads the same SCL layer the CLOUD_MASK toggle
        does, so it has the same L2A requirement -- with the toggle off."""
        products = dict(exec_config(config_src)["PRODUCTS"], cloud_mask=True)
        with pytest.raises(ValueError, match="cloud_mask product requires"):
            self._run(
                config_src, validation_src,
                EVENT_NAME="202601_Flood_TX", LEVEL="1", CLOUD_MASK=False,
                PRODUCTS=products,
            )

    def test_no_products_selected_is_rejected(self, config_src, validation_src):
        products = {k: False for k in exec_config(config_src)["PRODUCTS"]}
        with pytest.raises(ValueError, match="No products selected"):
            self._run(
                config_src, validation_src,
                EVENT_NAME="202601_Flood_TX", PRODUCTS=products,
            )

    def test_transposed_bbox_is_rejected(self, config_src, validation_src):
        with pytest.raises(ValueError, match="min < max"):
            self._run(
                config_src, validation_src,
                EVENT_NAME="202601_Flood_TX", BBOX=[-93.55, 42.10, -93.70, 42.00],
            )

    def test_a_valid_config_passes_and_writes_metadata(
        self, config_src, validation_src, tmp_path
    ):
        ns = self._run(
            config_src, validation_src,
            EVENT_NAME="202601_Flood_TX", OUTPUT_DIR=str(tmp_path / "out"),
        )
        meta = json.load(open(ns["ACTIVATION_METADATA_PATH"]))
        assert meta["ACTIVATION_EVENT"] == "202601_Flood_TX"
        assert meta["SOURCE"] == "Copernicus"
        assert meta["PROCESSOR"]
        assert os.path.isdir(ns["OUTPUT_DIR"])


# ---------------------------------------------------------------------
# Upload cell -- the product-folder mapping and the publish scope
# ---------------------------------------------------------------------

class TestUploadMapping:

    @staticmethod
    def _folders(upload_src):
        """Rebuild the token -> directory mapping without running the cell.

        PRODUCT_FOLDERS is no longer a literal: the notebook declares
        _PRODUCT_KEYS (token -> product key) and resolves each directory through
        shared_utils.product_paths.product_dir, so the published layout is
        defined in exactly one place. Resolve it the same way here, which also
        makes these tests fail if a token stops resolving.
        """
        from shared_utils.product_paths import product_dir

        tree = ast.parse(upload_src)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign)
                    and getattr(node.targets[0], "id", None) == "_PRODUCT_KEYS"):
                keys = ast.literal_eval(node.value)
                return {t: product_dir("sentinel2", k) for t, k in keys.items()}
        raise AssertionError("_PRODUCT_KEYS not found")

    def test_every_enabled_product_can_be_mapped(self, config_src, upload_src):
        """The draft had no `swir` entry, so those COGs were silently skipped."""
        pytest.importorskip("rasterio")
        from sentinel2.sentinel2_odr_functions import _product_token

        folders = self._folders(upload_src)
        for product in exec_config(config_src)["PRODUCTS"]:
            token = _product_token(product)
            assert token in folders, (
                f"product {product!r} produces filename token {token!r}, which "
                f"has no PRODUCT_FOLDERS entry -- its COGs would be silently "
                f"skipped by the upload"
            )

    def test_no_landsat_leftovers_in_the_mapping(self, upload_src):
        """The draft carried `pan`, which no Sentinel-2 pipeline produces.

        `cloudMask` used to be asserted absent here too. It was a leftover in
        the draft, but PR #190 gave the STAC pipeline a cloud-mask product
        (`--product cloud_mask`, filename token `cloudMask`), so the token now
        belongs in the mapping -- without it those COGs would be skipped at
        upload. test_every_enabled_product_can_be_mapped requires it."""
        folders = self._folders(upload_src)
        assert "pan" not in folders

    def test_the_mapping_holds_exactly_the_tokens_the_cli_writes(self, config_src, upload_src):
        """No missing token (a product silently skipped) and no extra one (a
        leftover from another sensor) -- the general form of the test above."""
        pytest.importorskip("rasterio")
        from sentinel2.sentinel2_odr_functions import _product_token

        written = {_product_token(p) for p in exec_config(config_src)["PRODUCTS"]}
        assert set(self._folders(upload_src)) == written

    def test_cloud_mask_publishes_to_its_own_folder(self, upload_src):
        assert self._folders(upload_src)["cloudMask"] == "CloudMask"

    def test_real_filenames_all_resolve_to_a_folder(self, config_src, upload_src):
        """Exercises the matcher against names _build_output_filename actually
        writes -- including merged, masked, and the water-extent NSTD variant."""
        pytest.importorskip("rasterio")
        import datetime as dt

        from sentinel2.sentinel2_odr_functions import (
            _build_output_filename, _nstd_variant_token,
        )

        class _Item:
            id = "S2C_T15TVG_20250813T170704_L2A"
            datetime = dt.datetime(2025, 8, 13, 17, 12, 26)
            properties = {
                "s2:datatake_id": "GS2C_20250813T170704_004999_N05.11",
                "s2:product_uri": "S2C_MSIL2A_20250813T170704_N0511_R069_T15TVG_X.SAFE",
            }

        folders = self._folders(upload_src)
        products = list(exec_config(config_src)["PRODUCTS"])

        names = []
        for product in products:
            names.append(_build_output_filename(_Item(), product))
            names.append(_build_output_filename(_Item(), product, masked=True))
            names.append(_build_output_filename(_Item(), product, merged=True))
        names.append(
            _build_output_filename(
                _Item(), "we", variant=_nstd_variant_token(1.5)
            )
        )

        for name in names:
            matched = [
                folders[t] for t in sorted(folders, key=len, reverse=True)
                if f"_{t}_" in name
            ]
            assert matched, f"{name} matched no PRODUCT_FOLDERS token"

    def test_longest_token_wins_so_substrings_do_not_shadow(self, upload_src):
        """`colorInfrared` must not be captured by a shorter key."""
        folders = self._folders(upload_src)
        name = "S2C_MSIL2A_colorInfrared_T15TVG_2025-08-13T17:12:26Z.tif"
        first = next(
            t for t in sorted(folders, key=len, reverse=True) if f"_{t}_" in name
        )
        assert first == "colorInfrared"

    def test_upload_is_scoped_to_this_run_not_the_whole_dir(self, upload_src):
        """The draft globbed OUTPUT_DIR recursively and unscoped, which would
        publish a previous activation's leftovers AND the water-extent
        product's CDL / WorldCover reference cache."""
        assert "NEW_COGS" in upload_src, (
            "the upload must iterate the COGs this run produced"
        )
        assert "recursive=True" not in upload_src, (
            "a recursive glob would sweep in OUTPUT_DIR/water_extent_reference/"
        )

    def test_an_unmappable_cog_raises_rather_than_being_skipped(self, upload_src):
        """A silent `continue` is how a whole product goes missing."""
        assert "raise RuntimeError" in upload_src

    def test_the_one_upload_call_goes_to_the_canonical_prefix(self, upload_src):
        """PR #190 left the canonical call inside a string literal and uploaded
        to `Sentinel_Test/` instead, while still printing `ProgramData/`."""
        calls = [
            node for node in ast.walk(ast.parse(upload_src))
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "upload_file_to_s3"
        ]
        assert len(calls) == 1, "exactly one upload call, so the message below it is true"
        destination = ast.unparse(calls[0].args[1])
        assert "prefix_for_product_dir(PRODUCT_SENSOR, subfolder)" in destination
        assert "_Test" not in upload_src


# ---------------------------------------------------------------------
# Process cell -- what "this run" means
# ---------------------------------------------------------------------

class _FakePopen:
    """Stands in for the CLI: writes (or REWRITES) one GeoTIFF per call."""

    def __init__(self, cmd, **_):
        out_dir = cmd[cmd.index("--output") + 1]
        product = cmd[cmd.index("--product") + 1]
        path = os.path.join(out_dir, f"S2C_MSIL2A_{product}_T15TVG_2025-08-13T17:12:26Z.tif")
        with open(path, "w") as fh:
            fh.write("x")
        # Make the rewrite visible even on a filesystem with coarse timestamps.
        stamp = os.stat(path).st_mtime_ns + 5_000_000_000 * _FakePopen.runs
        os.utime(path, ns=(stamp, stamp))
        self.stdout = iter(())

    runs = 0

    def wait(self):
        return 0


class TestProcessCellScopesToThisRun:

    def _namespace(self, config_src, tmp_path, monkeypatch):
        import subprocess

        monkeypatch.setattr(subprocess, "Popen", _FakePopen)
        out = tmp_path / "out"
        out.mkdir()
        ns = exec_config(config_src, OUTPUT_DIR=str(out))
        ns["ACTIVATION_METADATA_PATH"] = str(tmp_path / "meta.json")
        ns["os"] = os
        return ns, out

    def test_a_first_run_reports_what_it_wrote(self, config_src, process_src, tmp_path, monkeypatch):
        ns, out = self._namespace(config_src, tmp_path, monkeypatch)
        (out / "leftover_from_another_activation.tif").write_text("old")
        _FakePopen.runs = 1
        exec(compile(process_src, "process_cell", "exec"), ns)

        enabled = [p for p, on in ns["PRODUCTS"].items() if on]
        assert len(ns["NEW_COGS"]) == len(enabled) > 0
        assert not any("leftover" in f for f in ns["NEW_COGS"]), (
            "a file that was already there and was not touched is not this run's"
        )

    def test_a_rerun_still_reports_the_cogs_it_rewrote(self, config_src, process_src, tmp_path, monkeypatch):
        """The CLI writes deterministic names, so a second run REWRITES the same
        files. A name-only diff called that "0 COGs produced" and left the
        review and upload cells with nothing -- which is why the preview cell
        was hand-edited to glob the whole directory instead."""
        ns, _ = self._namespace(config_src, tmp_path, monkeypatch)
        _FakePopen.runs = 1
        exec(compile(process_src, "process_cell", "exec"), ns)
        first = list(ns["NEW_COGS"])

        _FakePopen.runs = 2
        exec(compile(process_src, "process_cell", "exec"), ns)
        assert ns["NEW_COGS"] == first and first

    def test_review_and_preview_use_new_cogs_not_a_directory_glob(self, cells):
        for marker in ("# REVIEW", "PREVIEW THE COGs"):
            src = cell_containing(cells, marker)
            assert "NEW_COGS" in src
            assert "rglob" not in src and "glob.glob" not in src

    def test_a_failed_product_stops_the_run(self, config_src, process_src, tmp_path, monkeypatch):
        """The cell used to print "FAILED" and carry on, so "Run All" went
        straight into the upload."""
        class _Failing(_FakePopen):
            def wait(self):
                return 2

        import subprocess

        ns, out = self._namespace(config_src, tmp_path, monkeypatch)
        monkeypatch.setattr(subprocess, "Popen", _Failing)
        _FakePopen.runs = 1
        with pytest.raises(RuntimeError, match="FAILED"):
            exec(compile(process_src, "process_cell", "exec"), ns)
        assert ns["NEW_COGS"] == []
        assert list(out.glob("*.tif")), "the failed products' files are on disk..."

    def test_only_finished_products_are_listed(self, config_src, process_src, tmp_path, monkeypatch):
        """...and must never reach the upload. A product that dies part-way
        leaves GeoTIFFs under product names that are not COGs and carry no
        activation tags; raising is not enough, because an operator can run
        the upload cell by hand afterwards. NEW_COGS, which is all the upload
        cell reads, holds only what a successful product wrote."""
        class _NdviFails(_FakePopen):
            def __init__(self, cmd, **kwargs):
                super().__init__(cmd, **kwargs)
                self.product = cmd[cmd.index("--product") + 1]

            def wait(self):
                return 1 if self.product == "ndvi" else 0

        import subprocess

        products = dict(exec_config(config_src)["PRODUCTS"], true_color=True, ndvi=True, evi=True)
        ns, _ = self._namespace(config_src, tmp_path, monkeypatch)
        ns["PRODUCTS"] = products
        monkeypatch.setattr(subprocess, "Popen", _NdviFails)
        _FakePopen.runs = 1
        with pytest.raises(RuntimeError, match="ndvi"):
            exec(compile(process_src, "process_cell", "exec"), ns)

        listed = sorted(os.path.basename(f).split("_")[2] for f in ns["NEW_COGS"])
        assert listed == ["evi", "true"], listed


class TestUploadCellRuns:
    """Exec the real upload cell with upload_file_to_s3 captured.

    Every static check on this cell once passed while it uploaded to
    `Sentinel_Test/`: the resolver was named, called, and its result ignored.
    Only running the cell shows where a file actually goes.
    """

    def _run(self, config_src, upload_src, monkeypatch, new_cogs, **overrides):
        import shared_utils

        sent = []
        monkeypatch.setattr(shared_utils, "upload_file_to_s3",
                            lambda local, uri: sent.append((local, uri)))
        overrides.setdefault("ENABLE_S3_UPLOAD", True)
        ns = exec_config(config_src, **overrides)
        ns["NEW_COGS"] = list(new_cogs)
        ns["os"] = os
        exec(compile(upload_src, "upload_cell", "exec"), ns)
        return sent

    def test_off_by_default_uploads_nothing(self, config_src, upload_src, monkeypatch):
        sent = self._run(
            config_src, upload_src, monkeypatch,
            ["/tmp/x/S2C_MSIL2A_ndvi_T15TVG_2025-08-13T17:12:26Z.tif"],
            ENABLE_S3_UPLOAD=False,
        )
        assert sent == []

    def test_every_product_lands_in_its_own_program_data_folder(
        self, config_src, upload_src, monkeypatch
    ):
        """One real filename per product the notebook offers, per tile and
        merged, against the canonical key from the shared table."""
        pytest.importorskip("rasterio")
        import datetime as dt

        from sentinel2.sentinel2_odr_functions import _build_output_filename
        from shared_utils.product_paths import STAGING_BUCKET, product_dir

        class _Item:
            id = "S2C_T15TVG_20250813T170704_L2A"
            datetime = dt.datetime(2025, 8, 13, 17, 12, 26)
            properties = {
                "s2:datatake_id": "GS2C_20250813T170704_004999_N05.11",
                "s2:product_uri": "S2C_MSIL2A_20250813T170704_N0511_R069_T15TVG_X.SAFE",
            }

        keys = TestUploadMapping._folders(upload_src)
        products = list(exec_config(config_src)["PRODUCTS"])
        for merged in (False, True):
            names = [_build_output_filename(_Item(), p, merged=merged) for p in products]
            sent = self._run(config_src, upload_src, monkeypatch,
                             [f"/tmp/x/{n}" for n in names], MERGE=merged)
            assert len(sent) == len(products)
            for (local, uri), name in zip(sent, names):
                token = next(t for t in sorted(keys, key=len, reverse=True) if f"_{t}_" in name)
                assert uri == (
                    f"s3://{STAGING_BUCKET}/ProgramData/Sentinel-2/{keys[token]}/{name}"
                )
        assert product_dir("sentinel2", "cloudMask") == keys["cloudMask"]

    def test_with_merge_on_only_the_mosaics_are_published(self, config_src, upload_src, monkeypatch):
        cogs = [
            "/tmp/x/S2C_MSIL2A_ndvi_T15TVG_2025-08-13T17:12:26Z.tif",
            "/tmp/x/S2C_MSIL2A_ndvi_R069_merged_2025-08-13_day.tif",
        ]
        sent = self._run(config_src, upload_src, monkeypatch, cogs, MERGE=True)
        assert [os.path.basename(local) for local, _ in sent] == [os.path.basename(cogs[1])]

    def test_a_cog_with_no_product_folder_raises(self, config_src, upload_src, monkeypatch):
        with pytest.raises(RuntimeError, match="NOT"):
            self._run(
                config_src, upload_src, monkeypatch,
                ["/tmp/x/S2C_MSIL2A_panchromatic_T15TVG_2025-08-13T17:12:26Z.tif"],
            )
