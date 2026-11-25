"""Pins notebooks/landsat_odr_workflow.ipynb against the defects it first shipped with.

Notebooks are NOT executed in CI, so every one of these was invisible until an
operator ran the notebook -- or published from it. Each test corresponds to a
real defect in the notebook as PR #190 added it:

  * The upload wrote every product to `Landsat_Test/` unless a variable inside
    the upload cell was emptied; the canonical `ProgramData/Landsat/<Product>/`
    key was the branch not taken.
  * EVENT_NAME shipped as a real-looking activation ("202610_Hurricane_Isaias")
    with nothing rejecting it, so a top-to-bottom run tagged every product with
    an event that never happened.
  * OUTPUT_DIR was /tmp/s3_temp, which other workflows share, and the upload
    fell back to globbing it whenever NEW_COGS was undefined -- which was
    always, because nothing defined NEW_COGS.
  * The processing cell ran `../src/landsat/process_landsat_odr.py` by relative
    path instead of the installed `process_landsat_odr` console script, so it
    only worked from one working directory and could run a checkout's script
    against a different installed package.
  * Nine configuration variables (TARGET_CRS, COMPRESSION, NODATA, ...) were
    read by nothing: the CLI fixes those settings.

The tests exec the notebook's real cells rather than re-describing them, in the
style of tests/unit/test_sentinel2_odr_notebook.py.
"""
import ast
import datetime as dt
import json
import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NB_PATH = os.path.join(REPO_ROOT, "notebooks", "landsat_odr_workflow.ipynb")

PLACEHOLDER = "202601_Flood_ExampleCity"

# Where each product the notebook offers must be published, spelled out here on
# purpose: an expectation read from the notebook's own mapping agrees with
# whatever that mapping says, including a product sent to another's folder.
PUBLISHED_FOLDERS = {
    "true_color": "TrueColor",
    "natural_color": "NaturalColor",
    "color_infrared": "ColorIR",
    "swir": "ShortwaveIR",
    "ndvi": "NDVI",
    "ndwi": "NDWI",
    "mndwi": "mNDWI",
    "nbr": "NBR",
    "evi": "EVI",
}


@pytest.fixture(scope="module")
def nb():
    with open(NB_PATH) as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def cells(nb):
    return ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]


def cell_containing(cells, needle):
    hits = [src for src in cells if needle in src]
    assert len(hits) == 1, f"expected 1 code cell containing {needle!r}, found {len(hits)}"
    return hits[0]


@pytest.fixture(scope="module")
def config_src(cells):
    return cell_containing(cells, "ACTIVATION OPTIONS BLOCK")


@pytest.fixture(scope="module")
def validation_src(cells):
    return cell_containing(cells, "ACTIVATION METADATA")


@pytest.fixture(scope="module")
def process_src(cells):
    return cell_containing(cells, "PROCESS SELECTED PRODUCTS")


@pytest.fixture(scope="module")
def upload_src(cells):
    return cell_containing(cells, "PRODUCT_FOLDERS")


def exec_config(config_src, **overrides):
    """Run the config cell, then apply overrides. Returns its namespace."""
    ns = {}
    exec(compile(config_src, "config_cell", "exec"), ns)
    ns.update(overrides)
    return ns


class _Item:
    """A Landsat STAC item as far as the filename builder is concerned."""

    def __init__(self, item_id="LC08_L2SP_021035_20261012_02_T1",
                 when=dt.datetime(2026, 10, 12, 16, 24, 22)):
        self.id = item_id
        self.datetime = when


# ---------------------------------------------------------------------
# Notebook integrity
# ---------------------------------------------------------------------

class TestNotebookIsWellFormed:

    def test_exists_and_parses(self, nb):
        assert nb["nbformat"] == 4
        assert nb["cells"]

    def test_no_cell_outputs_are_committed(self, nb):
        for c in nb["cells"]:
            if c["cell_type"] == "code":
                assert c.get("outputs") == []
                assert c.get("execution_count") is None

    def test_no_empty_cells(self, nb):
        assert all("".join(c["source"]).strip() for c in nb["cells"])

    def test_the_title_is_not_the_legacy_notebooks_title(self, nb):
        """Both notebooks were titled "Landsat 8/9 Processing Workflow"."""
        with open(os.path.join(REPO_ROOT, "notebooks", "legacy", "landsat_workflow.ipynb")) as fh:
            legacy_title = "".join(json.load(fh)["cells"][0]["source"]).splitlines()[0]
        title = "".join(nb["cells"][0]["source"]).splitlines()[0]
        assert title != legacy_title
        assert "STAC" in title

    def test_it_drives_the_installed_console_script(self, process_src):
        """Not a path into a checkout: that only resolves from one working
        directory, and pairs the checkout's script with whatever package
        happens to be installed."""
        tree = ast.parse(process_src)
        first_args = [
            node.value.elts[0].value for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and getattr(node.targets[0], "id", None) == "process_cmd"
            and isinstance(node.value, ast.List)
        ]
        assert first_args == ["process_landsat_odr"]
        assert "process_landsat_odr.py" not in process_src
        assert "sys.executable" not in process_src


# ---------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------

class TestConfigDefaults:

    def test_s3_upload_is_off_by_default(self, config_src):
        assert exec_config(config_src)["ENABLE_S3_UPLOAD"] is False

    def test_source_is_usgs(self, config_src):
        assert exec_config(config_src)["SOURCE"] == "USGS"

    def test_event_name_ships_as_the_placeholder(self, config_src):
        """A real-looking default event is published as if it were real."""
        assert exec_config(config_src)["EVENT_NAME"] == PLACEHOLDER

    def test_bbox_is_well_formed(self, config_src):
        bbox = exec_config(config_src)["BBOX"]
        assert len(bbox) == 4
        assert bbox[0] < bbox[2] and bbox[1] < bbox[3]

    def test_at_least_one_product_is_enabled(self, config_src):
        assert any(exec_config(config_src)["PRODUCTS"].values())

    def test_output_dir_is_not_shared_with_another_workflow(self, config_src):
        """/tmp/s3_temp is capella's OUTPUT_DIR and several CLIs' default."""
        out = exec_config(config_src)["OUTPUT_DIR"]
        assert out.startswith("/tmp/") and out != "/tmp/s3_temp"

    def test_every_cli_product_is_offered_and_nothing_else(self, config_src):
        """A PRODUCTS key the CLI rejects fails at run time; a CLI product
        missing here cannot be requested at all."""
        pytest.importorskip("rasterio")
        from landsat import process_landsat_odr as cli

        offered = cli.INDEX_PRODUCTS | cli.COMPOSITE_PRODUCTS
        assert set(exec_config(config_src)["PRODUCTS"]) == offered

    def test_every_config_variable_is_used(self, config_src, cells):
        """The first version declared TARGET_CRS, COMPRESSION, NODATA and six
        more that nothing read -- knobs an operator could turn to no effect."""
        declared = {
            node.targets[0].id for node in ast.parse(config_src).body
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        }
        rest = "\n".join(src for src in cells if src != config_src)
        unused = sorted(
            name for name in declared
            if not re.search(r"\b" + re.escape(name) + r"\b", rest)
        )
        assert unused == []


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
            self._run(config_src, validation_src, EVENT_NAME="Landsat_test")

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
                EVENT_NAME="202601_Flood_TX", BBOX=[-86.80, 35.60, -87.00, 35.40],
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
        assert meta["SOURCE"] == "USGS"
        assert meta["PROCESSOR"]
        assert os.path.isdir(ns["OUTPUT_DIR"])

    def test_metadata_file_is_not_the_legacy_notebooks(self, config_src, validation_src, tmp_path):
        """Both Landsat notebooks wrote activation_meta_landsat.json, so running
        one rewrote the event the other was about to embed."""
        ns = self._run(
            config_src, validation_src,
            EVENT_NAME="202601_Flood_TX", OUTPUT_DIR=str(tmp_path / "out"),
        )
        assert os.path.basename(ns["ACTIVATION_METADATA_PATH"]) == "activation_meta_landsat_odr.json"


# ---------------------------------------------------------------------
# Process cell -- flags, and what "this run" means
# ---------------------------------------------------------------------

class _FakePopen:
    """Stands in for the CLI: records the command, writes (or REWRITES) a GeoTIFF."""

    runs = 0
    commands = []
    environments = []

    def __init__(self, cmd, **kwargs):
        _FakePopen.commands.append(list(cmd))
        _FakePopen.environments.append(kwargs.get("env"))
        out_dir = cmd[cmd.index("--output") + 1]
        product = cmd[cmd.index("--product") + 1]
        path = os.path.join(out_dir, f"LC08_L2SP_{product}_021035_2026-10-12T16:24:22Z.tif")
        with open(path, "w") as fh:
            fh.write("x")
        # Make the rewrite visible even on a filesystem with coarse timestamps.
        stamp = os.stat(path).st_mtime_ns + 5_000_000_000 * _FakePopen.runs
        os.utime(path, ns=(stamp, stamp))
        self.stdout = iter(())

    def wait(self):
        return 0


class TestProcessCell:

    def _namespace(self, config_src, tmp_path, monkeypatch, **overrides):
        import subprocess

        monkeypatch.setattr(subprocess, "Popen", _FakePopen)
        _FakePopen.commands = []
        _FakePopen.environments = []
        _FakePopen.runs = 1
        out = tmp_path / "out"
        out.mkdir()
        ns = exec_config(config_src, OUTPUT_DIR=str(out), **overrides)
        ns["ACTIVATION_METADATA_PATH"] = str(tmp_path / "meta.json")
        return ns, out

    def test_every_flag_it_passes_is_one_the_cli_accepts(
        self, config_src, process_src, tmp_path, monkeypatch, capsys
    ):
        """A flag the CLI does not define is an argparse error on every run."""
        pytest.importorskip("rasterio")
        from landsat import process_landsat_odr as cli

        products = {k: True for k in exec_config(config_src)["PRODUCTS"]}
        ns, _ = self._namespace(
            config_src, tmp_path, monkeypatch,
            PRODUCTS=products, ENABLE_MASK=True, ENABLE_MERGE=True,
        )
        exec(compile(process_src, "process_cell", "exec"), ns)
        assert len(_FakePopen.commands) == len(products)

        monkeypatch.setattr("sys.argv", ["process_landsat_odr", "--help"])
        with pytest.raises(SystemExit):
            cli.main()
        accepted = set(re.findall(r"--[a-z][a-z-]+", capsys.readouterr().out))

        for cmd in _FakePopen.commands:
            assert cmd[0] == "process_landsat_odr"
            passed = {arg for arg in cmd if arg.startswith("--")}
            assert passed <= accepted, f"flags the CLI does not accept: {sorted(passed - accepted)}"
            assert {"--cloud-mask", "--merge", "--metadata-json"} <= passed

    def test_the_cli_output_streams_into_the_cell(
        self, config_src, process_src, tmp_path, monkeypatch
    ):
        """The markdown promises it. Through a pipe Python holds output back in
        blocks, so a stalled read looked the same as progress."""
        ns, _ = self._namespace(config_src, tmp_path, monkeypatch)
        exec(compile(process_src, "process_cell", "exec"), ns)
        assert _FakePopen.environments
        for env in _FakePopen.environments:
            assert env["PYTHONUNBUFFERED"] == "1" and env["PATH"] == os.environ["PATH"]

    def test_the_toggles_are_honoured(self, config_src, process_src, tmp_path, monkeypatch):
        ns, _ = self._namespace(
            config_src, tmp_path, monkeypatch, ENABLE_MASK=False, ENABLE_MERGE=False,
        )
        exec(compile(process_src, "process_cell", "exec"), ns)
        for cmd in _FakePopen.commands:
            assert "--cloud-mask" not in cmd and "--merge" not in cmd

    def test_an_earlier_runs_files_are_cleared_not_republished(
        self, config_src, process_src, tmp_path, monkeypatch
    ):
        """OUTPUT_DIR lives in /tmp and is never cleaned by anything else. A
        COG left by the previous activation would be uploaded again, tagged
        with the previous event (.clinerules.md rule 61)."""
        ns, out = self._namespace(config_src, tmp_path, monkeypatch)
        leftover = out / "LC08_L2SP_trueColor_P021_merged_2026-01-01_day.tif"
        leftover.write_text("old")
        exec(compile(process_src, "process_cell", "exec"), ns)

        enabled = [p for p, on in ns["PRODUCTS"].items() if on]
        assert not leftover.exists()
        assert len(ns["NEW_COGS"]) == len(enabled) > 0
        assert sorted(os.listdir(out)) == sorted(os.path.basename(f) for f in ns["NEW_COGS"])

    def test_a_rerun_still_reports_its_cogs(self, config_src, process_src, tmp_path, monkeypatch):
        """The CLI writes deterministic names, so a second run produces the
        same filenames; it must not read as "0 COGs produced"."""
        ns, _ = self._namespace(config_src, tmp_path, monkeypatch)
        exec(compile(process_src, "process_cell", "exec"), ns)
        first = list(ns["NEW_COGS"])

        _FakePopen.runs = 2
        exec(compile(process_src, "process_cell", "exec"), ns)
        assert ns["NEW_COGS"] == first and first

    def test_a_failed_product_raises_after_the_others_ran(
        self, config_src, process_src, tmp_path, monkeypatch
    ):
        class _Failing(_FakePopen):
            def wait(self):
                return 2

        import subprocess

        ns, out = self._namespace(config_src, tmp_path, monkeypatch)
        monkeypatch.setattr(subprocess, "Popen", _Failing)
        with pytest.raises(RuntimeError, match="FAILED"):
            exec(compile(process_src, "process_cell", "exec"), ns)
        assert ns["NEW_COGS"] == []
        assert list(out.glob("*.tif")), "the failed product's file is on disk..."

    def test_only_finished_products_are_listed(
        self, config_src, process_src, tmp_path, monkeypatch
    ):
        """...and must never reach the upload. The CLI delivers nothing for a
        run that fails or is killed, but the cell does not rely on that, and
        raising is not enough: the upload cell can be run by hand afterwards.
        NEW_COGS, which is all the upload cell reads, holds only what a
        successful product wrote."""
        class _NdviFails(_FakePopen):
            def __init__(self, cmd, **kwargs):
                super().__init__(cmd, **kwargs)
                self.product = cmd[cmd.index("--product") + 1]

            def wait(self):
                return -9 if self.product == "ndvi" else 0

        import subprocess

        products = dict(exec_config(config_src)["PRODUCTS"], true_color=True, ndvi=True, evi=True)
        ns, _ = self._namespace(config_src, tmp_path, monkeypatch, PRODUCTS=products)
        monkeypatch.setattr(subprocess, "Popen", _NdviFails)
        with pytest.raises(RuntimeError, match="ndvi"):
            exec(compile(process_src, "process_cell", "exec"), ns)

        listed = sorted(os.path.basename(f).split("_")[2] for f in ns["NEW_COGS"])
        assert listed == ["evi", "true"], listed

    def test_view_and_plot_use_new_cogs_not_a_directory_glob(self, cells):
        for marker in ("REVIEW THE COGs", "PREVIEW THE LANDSAT COGs"):
            src = cell_containing(cells, marker)
            assert "NEW_COGS" in src
            assert "glob(" not in src


# ---------------------------------------------------------------------
# Upload cell -- the product-folder mapping and the publish scope
# ---------------------------------------------------------------------

class TestUploadMapping:

    @staticmethod
    def _folders(upload_src):
        """token -> published directory, resolved the way the cell resolves it."""
        from shared_utils.product_paths import product_dir

        for node in ast.walk(ast.parse(upload_src)):
            if (isinstance(node, ast.Assign)
                    and getattr(node.targets[0], "id", None) == "_PRODUCT_KEYS"):
                keys = ast.literal_eval(node.value)
                return {t: product_dir("landsat", k) for t, k in keys.items()}
        raise AssertionError("_PRODUCT_KEYS not found")

    def test_the_mapping_holds_exactly_the_tokens_the_cli_writes(self, config_src, upload_src):
        pytest.importorskip("rasterio")
        from landsat.landsat_odr_functions import _product_token

        written = {_product_token(p) for p in exec_config(config_src)["PRODUCTS"]}
        assert set(self._folders(upload_src)) == written

    def test_real_filenames_all_resolve_to_a_folder(self, config_src, upload_src):
        """Against names _build_output_filename actually writes: per scene,
        masked, and the per-pass mosaic."""
        pytest.importorskip("rasterio")
        from landsat.landsat_odr_functions import _build_output_filename

        folders = self._folders(upload_src)
        for product in exec_config(config_src)["PRODUCTS"]:
            for kwargs in ({}, {"masked": True}, {"merged": True},
                           {"merged": True, "masked": True}):
                name = _build_output_filename(_Item(), product, **kwargs)
                matched = [
                    folders[t] for t in sorted(folders, key=len, reverse=True)
                    if f"_{t}_" in name
                ]
                assert matched, f"{name} matched no _PRODUCT_KEYS token"

    def test_longest_token_wins_so_substrings_do_not_shadow(self, upload_src):
        folders = self._folders(upload_src)
        name = "LC08_L2SP_colorInfrared_021035_2026-10-12T16:24:22Z.tif"
        first = next(
            t for t in sorted(folders, key=len, reverse=True) if f"_{t}_" in name
        )
        assert first == "colorInfrared"

    def test_the_one_upload_call_goes_to_the_canonical_prefix(self, upload_src):
        calls = [
            node for node in ast.walk(ast.parse(upload_src))
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "upload_file_to_s3"
        ]
        assert len(calls) == 1
        destination = ast.unparse(calls[0].args[1])
        assert "prefix_for_product_dir(PRODUCT_SENSOR, subfolder)" in destination
        assert "_Test" not in upload_src and "S3_TEST_PREFIX" not in upload_src

    def test_upload_is_scoped_to_this_run_with_no_directory_fallback(self, upload_src):
        """The first version globbed OUTPUT_DIR whenever NEW_COGS was undefined."""
        assert "NEW_COGS" in upload_src
        assert "glob(" not in upload_src          # glob.glob / Path.glob / rglob
        assert "globals()" not in upload_src

    def test_an_unmappable_cog_raises_rather_than_being_skipped(self, upload_src):
        assert "raise RuntimeError" in upload_src


class TestUploadCellRuns:
    """Exec the real upload cell with upload_file_to_s3 captured."""

    def _run(self, config_src, upload_src, monkeypatch, new_cogs, **overrides):
        import shared_utils

        sent = []
        monkeypatch.setattr(shared_utils, "upload_file_to_s3",
                            lambda local, uri: sent.append((local, uri)))
        overrides.setdefault("ENABLE_S3_UPLOAD", True)
        ns = exec_config(config_src, **overrides)
        ns["NEW_COGS"] = list(new_cogs)
        exec(compile(upload_src, "upload_cell", "exec"), ns)
        return sent, ns

    def test_off_by_default_uploads_nothing(self, config_src, upload_src, monkeypatch):
        sent, _ = self._run(
            config_src, upload_src, monkeypatch,
            ["/tmp/x/LC08_L2SP_trueColor_P021_merged_2026-10-12_day.tif"],
            ENABLE_S3_UPLOAD=False,
        )
        assert sent == []

    def test_each_product_lands_in_its_own_program_data_folder(
        self, config_src, upload_src, monkeypatch
    ):
        """One real filename per product the notebook offers, per scene and
        merged, against PUBLISHED_FOLDERS."""
        pytest.importorskip("rasterio")
        from landsat.landsat_odr_functions import _build_output_filename
        from shared_utils.product_paths import STAGING_BUCKET

        products = list(exec_config(config_src)["PRODUCTS"])
        assert set(products) == set(PUBLISHED_FOLDERS)
        for merged in (False, True):
            names = {p: _build_output_filename(_Item(), p, merged=merged) for p in products}
            sent, _ = self._run(config_src, upload_src, monkeypatch,
                                [f"/tmp/x/{n}" for n in names.values()], ENABLE_MERGE=merged)
            assert [uri for _, uri in sent] == [
                f"s3://{STAGING_BUCKET}/ProgramData/Landsat/{PUBLISHED_FOLDERS[p]}/{n}"
                for p, n in names.items()
            ]

    def test_with_merge_on_only_the_mosaics_are_published(
        self, config_src, upload_src, monkeypatch
    ):
        cogs = [
            "/tmp/x/LC08_L2SP_ndvi_021035_2026-10-12T16:24:22Z.tif",
            "/tmp/x/LC08_L2SP_ndvi_P021_merged_2026-10-12_day.tif",
        ]
        sent, _ = self._run(config_src, upload_src, monkeypatch, cogs, ENABLE_MERGE=True)
        assert [os.path.basename(local) for local, _ in sent] == [os.path.basename(cogs[1])]

    def test_a_cog_with_no_product_folder_raises(self, config_src, upload_src, monkeypatch):
        with pytest.raises(RuntimeError, match="NOT uploaded"):
            self._run(
                config_src, upload_src, monkeypatch,
                ["/tmp/x/LC08_L2SP_panchromatic_021035_2026-10-12T16:24:22Z.tif"],
                ENABLE_MERGE=False,
            )
