"""The notebooks/ split, and the rule that a sensor notebook owns no paths.

Two things are pinned here:

1. `notebooks/` holds the per-sensor workflows and nothing else. Everything an
   operator reaches for around them -- transfers, templates, one-off utilities --
   lives in `notebooks/tools/`, and the fixtures under
   `notebooks/testing-notebooks/` are left alone.

2. A sensor notebook does not name a bucket or a product directory. Both are
   hard-coded once in `shared_utils.product_paths`; the notebook names its
   sensor and lets the table resolve the rest. Notebooks are not linted or
   executed in CI, so a hard-coded path that drifts from the bucket is invisible
   until an operator publishes to the wrong prefix -- which is what these assert
   against.
"""

import ast
import importlib.util
import json
import os
import re

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
NB_DIR = os.path.join(REPO_ROOT, "notebooks")
TOOLS_DIR = os.path.join(NB_DIR, "tools")
TESTING_DIR = os.path.join(NB_DIR, "testing-notebooks")

SENSOR_NOTEBOOKS = {
    "landsat_workflow.ipynb": "landsat",
    "sentinel2_workflow.ipynb": "sentinel2",
    "sentinel2_odr_workflow.ipynb": "sentinel2",
    "satellogic_workflow.ipynb": "satellogic",
    "skysat_workflow.ipynb": "skysat",
    "umbra_workflow.ipynb": "umbra",
    "capella_workflow.ipynb": "capella",
}


def _code(path):
    """Concatenated source of a notebook's code cells."""
    with open(path, encoding="utf-8") as fh:
        nb = json.load(fh)
    return "\n".join(
        "".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"
    )


def _code_cells(path):
    """Each code cell's source, with line magics stripped, as parseable Python.

    Cells are parsed individually: concatenating them is not valid Python in
    general, and a `!`/`%` magic is not Python at all.
    """
    with open(path, encoding="utf-8") as fh:
        nb = json.load(fh)
    out = []
    for c in nb["cells"]:
        if c["cell_type"] != "code":
            continue
        out.append("\n".join(
            l for l in "".join(c["source"]).split("\n")
            if not l.lstrip().startswith(("!", "%"))
        ))
    return out


def _undefined_names():
    """The symtable guard from tests/integration/test_dispatch_undefined_names.py.

    Loaded by path rather than re-implemented: that module already solved
    "every name referenced at module scope is bound", including the
    ``from X import *`` and builtins cases, for the sensor dispatch scripts.
    """
    path = os.path.join(REPO_ROOT, "tests", "integration",
                        "test_dispatch_undefined_names.py")
    spec = importlib.util.spec_from_file_location("_dispatch_undefined_names", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.undefined_names


# Names a notebook gets from the IPython kernel, not from its own source.
IPYTHON_GLOBALS = {"get_ipython", "display", "In", "Out", "exit", "quit"}

# `if "POLYGON" in globals() and POLYGON:` -- a name the notebook deliberately
# leaves unbound (an alternative the operator uncomments) and guards before use.
# Guarded like that it cannot raise NameError, so it is not the bug class here.
_GUARDED_RE = re.compile(r"""["'](\w+)["']\s+in\s+globals\(\)""")


def _guarded_names(source):
    return set(_GUARDED_RE.findall(source))


def _all_source(path):
    with open(path, encoding="utf-8") as fh:
        nb = json.load(fh)
    return "\n".join("".join(c["source"]) for c in nb["cells"])


class TestNotebookSplit:
    def test_only_sensor_workflows_sit_at_the_top_level(self):
        top = {f for f in os.listdir(NB_DIR) if f.endswith(".ipynb")}
        assert top == set(SENSOR_NOTEBOOKS), (
            "notebooks/ is for the per-sensor workflows; anything else belongs "
            "in notebooks/tools/"
        )

    def test_tools_directory_exists_and_is_populated(self):
        assert os.path.isdir(TOOLS_DIR)
        assert [f for f in os.listdir(TOOLS_DIR) if f.endswith(".ipynb")]

    def test_testing_notebooks_are_untouched_by_the_split(self):
        # These are fixtures, exempt from the no-hard-coded-paths rule below.
        assert os.path.isdir(TESTING_DIR)
        assert [f for f in os.listdir(TESTING_DIR) if f.endswith(".ipynb")]


class TestSensorNotebooksOwnNoPaths:
    @pytest.mark.parametrize("nb,sensor", sorted(SENSOR_NOTEBOOKS.items()))
    def test_declares_its_sensor_key(self, nb, sensor):
        src = _code(os.path.join(NB_DIR, nb))
        assert f'PRODUCT_SENSOR = "{sensor}"' in src, (
            f"{nb} must declare PRODUCT_SENSOR so the shared table can resolve "
            f"its destinations"
        )

    @pytest.mark.parametrize("nb", sorted(SENSOR_NOTEBOOKS))
    def test_takes_the_bucket_from_the_shared_table(self, nb):
        src = _code(os.path.join(NB_DIR, nb))
        assert "from shared_utils.product_paths import STAGING_BUCKET" in src
        assert "S3_BUCKET = STAGING_BUCKET" in src

    @pytest.mark.parametrize("nb", sorted(SENSOR_NOTEBOOKS))
    def test_names_no_bucket_literal(self, nb):
        # Three sensor notebooks had drifted onto two different buckets.
        src = _code(os.path.join(NB_DIR, nb))
        for bad in ('"nasa-disasters"', "'nasa-disasters'",
                    '"nasa-disasters-staging"', "'nasa-disasters-staging'"):
            assert bad not in src, f"{nb} hard-codes the bucket {bad}"

    @pytest.mark.parametrize("nb", sorted(SENSOR_NOTEBOOKS))
    def test_no_stale_prefix_variables(self, nb):
        # S3_PREFIX / S3_DEST_BASE are gone; a leftover reference is a NameError
        # at run time, and notebooks are not executed in CI to catch it.
        src = _all_source(os.path.join(NB_DIR, nb))
        for var in ("S3_PREFIX", "S3_DEST_BASE"):
            assert not re.search(r"\b" + var + r"\b", src), f"{nb} still refers to {var}"

    @pytest.mark.parametrize("nb", sorted(SENSOR_NOTEBOOKS))
    def test_no_literal_product_directory_map(self, nb):
        """A token -> directory dict in a notebook is a second source of truth.

        The ODR notebook's map had `colorIR`, `SWIR`, `MNDWI` and `waterExtent`
        in it -- none of which match the bucket.
        """
        nodes = []
        for cell in _code_cells(os.path.join(NB_DIR, nb)):
            try:
                nodes.extend(ast.walk(ast.parse(cell)))
            except SyntaxError:
                continue  # covered by test_every_code_cell_parses
        for node in nodes:
            if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)):
                continue
            name = getattr(node.targets[0], "id", "")
            if name == "_PRODUCT_KEYS":
                continue  # token -> product KEY; directories still come from the table
            values = [v.value for v in node.value.values
                      if isinstance(v, ast.Constant) and isinstance(v.value, str)]
            overlap = {"trueColor", "colorIR", "naturalColor", "shortwaveIR", "SWIR",
                       "cloudMask", "MNDWI", "mNDWI", "waterExtent"} & set(values)
            assert not overlap, (
                f"{nb}: {name} hard-codes product directories {sorted(overlap)}; "
                f"resolve them through shared_utils.product_paths.product_dir"
            )

    @pytest.mark.parametrize("nb", sorted(SENSOR_NOTEBOOKS))
    def test_publishes_through_the_shared_resolver(self, nb):
        src = _code(os.path.join(NB_DIR, nb))
        assert ("prefix_for_product_dir" in src) or ("sensor=PRODUCT_SENSOR" in src), (
            f"{nb} must key its uploads by the canonical destination, either via "
            f"prefix_for_product_dir() or by passing sensor= to upload_dir_ambient()"
        )

    @pytest.mark.parametrize("nb", sorted(SENSOR_NOTEBOOKS))
    def test_upload_cell_has_no_unbound_names(self, nb):
        """Every name a notebook uses must be bound by the notebook itself.

        sentinel2_odr_workflow.ipynb called product_dir() and
        prefix_for_product_dir() while importing neither -- shared_utils
        re-exports upload_file_to_s3 but not those two -- so the upload cell
        raised NameError for an operator on the hub. Notebooks are neither
        linted nor executed in CI, and the resolver check above passes on a
        name that is merely CALLED, so nothing else catches this.
        """
        path = os.path.join(NB_DIR, nb)
        # Cells run in order and share one namespace, so the notebook as a
        # whole is the scope a name has to be bound in.
        source = "\n".join(_code_cells(path))
        allowed = IPYTHON_GLOBALS | _guarded_names(source)
        missing = [n for n in _undefined_names()(source, path) if n not in allowed]
        assert missing == [], (
            f"{nb}: names referenced but never bound: {missing}. "
            f"Import them in the cell that uses them."
        )

    @pytest.mark.parametrize("nb", sorted(SENSOR_NOTEBOOKS))
    def test_every_code_cell_parses(self, nb):
        path = os.path.join(NB_DIR, nb)
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        for i, c in enumerate(d["cells"]):
            if c["cell_type"] != "code":
                continue
            src = "\n".join(
                l for l in "".join(c["source"]).split("\n")
                if not l.lstrip().startswith(("!", "%"))
            )
            try:
                ast.parse(src)
            except SyntaxError as e:  # pragma: no cover - the failure message is the point
                pytest.fail(f"{nb} cell {i} does not parse: {e}")


class TestNotebookUploadsUseAmbientCredentials:
    """No notebook may publish through the DPS MAAP-credential path.

    ``staging_upload.upload_dir_to_staging`` authenticates through maap-py,
    which reads ``MAAP_PGT`` from the process environment. The DPS wrapper
    injects it; the Disasters hub never does -- it is per-user and secret, so
    it cannot be baked into the image, and the ``maapToken`` in MAAP Settings
    lives in the JupyterLab frontend SettingRegistry where a kernel never sees
    it. landsat/sentinel2 called it anyway and returned HTTP 401 for every hub
    operator, deterministically, for six weeks.

    Assertions run over the AST, not the text, so the upload cells can name the
    banned helper in a comment explaining why not to use it.
    """

    MAAP_ONLY = {"upload_dir_to_staging", "workspace_bucket_credentials",
                 "_workspace_s3_client"}

    @staticmethod
    def _referenced(path):
        """Every name imported or called by a notebook's code cells."""
        names = set()
        for cell in _code_cells(path):
            try:
                tree = ast.parse(cell)
            except SyntaxError:  # pragma: no cover - test_every_code_cell_parses owns this
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    names.update(a.name for a in node.names)
                elif isinstance(node, ast.Import):
                    names.update(a.name for a in node.names)
                elif isinstance(node, ast.Call):
                    fn = node.func
                    names.add(getattr(fn, "id", None) or getattr(fn, "attr", ""))
        return names - {None, ""}

    @pytest.mark.parametrize("nb", sorted(SENSOR_NOTEBOOKS))
    def test_no_notebook_uses_the_maap_credential_path(self, nb):
        used = self._referenced(os.path.join(NB_DIR, nb)) & self.MAAP_ONLY
        assert not used, (
            f"{nb} reaches the MAAP-credential path ({sorted(used)}), which 401s "
            f"on the hub. Use staging_upload.upload_dir_ambient, or "
            f"upload_file_to_s3 with prefix_for_product_dir."
        )

    def test_the_tools_notebooks_stay_ambient_too(self):
        for name in sorted(os.listdir(TOOLS_DIR)):
            if not name.endswith(".ipynb"):
                continue
            used = self._referenced(os.path.join(TOOLS_DIR, name)) & self.MAAP_ONLY
            assert not used, f"{name} reaches the MAAP-credential path ({sorted(used)})"

    @pytest.mark.parametrize("nb", ["landsat_workflow.ipynb", "sentinel2_workflow.ipynb"])
    def test_directory_publishers_use_the_ambient_helper(self, nb):
        """These two publish a whole tree, so they keep staging_upload's keying."""
        assert "upload_dir_ambient" in self._referenced(os.path.join(NB_DIR, nb))

    @pytest.mark.parametrize("nb", sorted(SENSOR_NOTEBOOKS))
    def test_upload_is_opt_in(self, nb):
        """Publishing to the production bucket is never the default.

        sentinel2_workflow.ipynb shipped ENABLE_S3_UPLOAD = True, so a
        top-to-bottom run published without the operator choosing to.
        """
        src = _code(os.path.join(NB_DIR, nb))
        assert re.search(r"^ENABLE_S3_UPLOAD\s*=\s*False\s*$", src, re.MULTILINE), (
            f"{nb} must ship ENABLE_S3_UPLOAD = False"
        )


class TestOdrGeneratorStaysInSync:
    """sentinel2_odr_workflow.ipynb is generated; the generator must match it."""

    def test_generator_carries_the_same_wiring(self):
        gen = os.path.join(REPO_ROOT, "tools", "_build_s2_odr_notebook.py")
        with open(gen, encoding="utf-8") as fh:
            src = fh.read()
        assert 'PRODUCT_SENSOR = "sentinel2"' in src
        assert "from shared_utils.product_paths import STAGING_BUCKET" in src
        assert "_PRODUCT_KEYS" in src
        assert "S3_PREFIX" not in src and "S3_DEST_BASE" not in src
