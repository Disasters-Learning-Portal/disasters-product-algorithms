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
    by the upload; it also carried a `cloudMask` key with no S2 equivalent.
  * The merge filter read globals().get("ENABLE_MERGE"), a name the config cell
    never defines (it defines MERGE) -- the filter was dead.
  * The upload globbed OUTPUT_DIR recursively and unscoped, so it would publish
    leftovers from a previous activation AND the water-extent product's
    CDL / WorldCover reference cache.

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
        """Pull PRODUCT_FOLDERS out of the upload cell without running it."""
        tree = ast.parse(upload_src)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign)
                    and getattr(node.targets[0], "id", None) == "PRODUCT_FOLDERS"):
                return ast.literal_eval(node.value)
        raise AssertionError("PRODUCT_FOLDERS not found")

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
        """The draft carried `cloudMask` and `pan`, neither of which the STAC
        Sentinel-2 pipeline produces."""
        folders = self._folders(upload_src)
        assert "cloudMask" not in folders
        assert "pan" not in folders

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
