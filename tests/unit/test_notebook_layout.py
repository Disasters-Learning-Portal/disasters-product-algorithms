"""The notebooks/ split, and the rule that a sensor notebook owns no paths.

Three things are pinned here:

1. `notebooks/` holds the CURRENT per-sensor workflows and nothing else.
   Everything an operator reaches for around them -- transfers, templates,
   one-off utilities -- lives in `notebooks/tools/`, and the fixtures under
   `notebooks/testing-notebooks/` are left alone.

2. A workflow that a newer pipeline has superseded is RETIRED, not deleted and
   not renamed: it moves to `notebooks/legacy/`, still runnable, behind a banner
   naming its successor. Deleting it would take away the fallback an operator
   needs while the new pipeline beds in; giving the new notebook the old name
   would put a different pipeline, data source and product list behind a name
   people already trust. Both registries below are hard-coded on purpose --
   adding, retiring or un-retiring a workflow should take a deliberate edit
   here.

3. A sensor notebook does not name a bucket or a product directory. Both are
   hard-coded once in `shared_utils.product_paths`; the notebook names its
   sensor and lets the table resolve the rest. Notebooks are not linted or
   executed in CI, so a hard-coded path that drifts from the bucket is invisible
   until an operator publishes to the wrong prefix -- which is what these assert
   against. A retired workflow can still publish to the live bucket, so it is
   held to exactly the same contract as a current one.
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
LEGACY_DIR = os.path.join(NB_DIR, "legacy")

# notebooks/<name> -> sensor key. The CURRENT operator workflows.
SENSOR_NOTEBOOKS = {
    "landsat_odr_workflow.ipynb": "landsat",
    "sentinel2_odr_workflow.ipynb": "sentinel2",
    "satellogic_workflow.ipynb": "satellogic",
    "skysat_workflow.ipynb": "skysat",
    "umbra_workflow.ipynb": "umbra",
    "capella_workflow.ipynb": "capella",
    "iceye_workflow.ipynb": "iceye",
}

# notebooks/legacy/<name> -> (sensor key, the current workflow that superseded it).
# RETIRED workflows: an older pipeline for a sensor that now has a newer one.
LEGACY_NOTEBOOKS = {
    "landsat_workflow.ipynb": ("landsat", "landsat_odr_workflow.ipynb"),
    "sentinel2_workflow.ipynb": ("sentinel2", "sentinel2_odr_workflow.ipynb"),
}

# Every notebook an operator can publish from, keyed by its path under
# notebooks/. The contract tests below run over all of them: retiring a
# workflow moves it, it does not excuse it.
WORKFLOW_NOTEBOOKS = {
    **SENSOR_NOTEBOOKS,
    **{f"legacy/{name}": sensor for name, (sensor, _) in LEGACY_NOTEBOOKS.items()},
}

LEGACY_BANNER = "LEGACY WORKFLOW"


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


def _fstring_skeleton(node):
    """An f-string with each replacement field shown as `{<expression>}`."""
    return "".join(
        part.value if isinstance(part, ast.Constant)
        else "{" + ast.unparse(part.value) + "}"
        for part in node.values
    )


def _docstrings(tree):
    """The string statements that really are docstrings."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                found.add(id(body[0]))
    return found


class TestNotebookSplit:
    def test_only_sensor_workflows_sit_at_the_top_level(self):
        top = {f for f in os.listdir(NB_DIR) if f.endswith(".ipynb")}
        assert top == set(SENSOR_NOTEBOOKS), (
            "notebooks/ is for the CURRENT per-sensor workflows. A tool belongs "
            "in notebooks/tools/; a workflow a newer pipeline has superseded "
            "belongs in notebooks/legacy/ (and in LEGACY_NOTEBOOKS). A new "
            "workflow needs an entry in SENSOR_NOTEBOOKS."
        )

    def test_tools_directory_exists_and_is_populated(self):
        assert os.path.isdir(TOOLS_DIR)
        assert [f for f in os.listdir(TOOLS_DIR) if f.endswith(".ipynb")]

    def test_testing_notebooks_are_untouched_by_the_split(self):
        # These are fixtures, exempt from the no-hard-coded-paths rule below.
        assert os.path.isdir(TESTING_DIR)
        assert [f for f in os.listdir(TESTING_DIR) if f.endswith(".ipynb")]


class TestRetiredWorkflows:
    """notebooks/legacy/ -- superseded workflows, kept runnable as a fallback."""

    def test_legacy_directory_holds_exactly_the_retired_workflows(self):
        assert os.path.isdir(LEGACY_DIR)
        legacy = {f for f in os.listdir(LEGACY_DIR) if f.endswith(".ipynb")}
        assert legacy == set(LEGACY_NOTEBOOKS), (
            "notebooks/legacy/ must hold exactly the workflows registered in "
            "LEGACY_NOTEBOOKS -- retiring (or un-retiring) one is a deliberate edit"
        )

    def test_a_workflow_is_never_both_current_and_retired(self):
        # Two copies under one name leave an operator guessing which is live.
        assert not set(SENSOR_NOTEBOOKS) & set(LEGACY_NOTEBOOKS)

    @pytest.mark.parametrize("nb", sorted(LEGACY_NOTEBOOKS))
    def test_retired_workflow_names_a_current_successor(self, nb):
        sensor, successor = LEGACY_NOTEBOOKS[nb]
        assert successor in SENSOR_NOTEBOOKS, (
            f"legacy/{nb} is superseded by {successor}, which is not a current "
            f"workflow -- a sensor must never be left with only a retired notebook"
        )
        assert SENSOR_NOTEBOOKS[successor] == sensor, (
            f"legacy/{nb} ({sensor}) cannot be superseded by a "
            f"{SENSOR_NOTEBOOKS[successor]} workflow"
        )

    @pytest.mark.parametrize("nb", sorted(LEGACY_NOTEBOOKS))
    def test_retired_workflow_says_so_at_the_top(self, nb):
        """An operator who opens a retired notebook must see that it is one,
        and where the current workflow lives, before the first code cell."""
        _, successor = LEGACY_NOTEBOOKS[nb]
        with open(os.path.join(LEGACY_DIR, nb), encoding="utf-8") as fh:
            cells = json.load(fh)["cells"]
        first_code = next(i for i, c in enumerate(cells) if c["cell_type"] == "code")
        banners = [
            "".join(c["source"]) for c in cells[:first_code]
            if c["cell_type"] == "markdown" and LEGACY_BANNER in "".join(c["source"])
        ]
        assert banners, f"legacy/{nb} has no '{LEGACY_BANNER}' banner above its first code cell"
        assert successor in banners[0], (
            f"legacy/{nb}: the banner must point at its successor, {successor}"
        )

    @pytest.mark.parametrize("nb", sorted(SENSOR_NOTEBOOKS))
    def test_a_current_workflow_carries_no_legacy_banner(self, nb):
        assert LEGACY_BANNER not in _all_source(os.path.join(NB_DIR, nb))

    @pytest.mark.parametrize("nb", sorted(WORKFLOW_NOTEBOOKS))
    def test_relative_src_path_matches_the_notebook_depth(self, nb):
        """`sys.path.append('../src')` is right one level under the repo root and
        wrong two levels under it. Moving a notebook into legacy/ without fixing
        the path leaves a local (non-installed) checkout importing nothing."""
        depth = nb.count("/") + 1                     # notebooks/ = 1, notebooks/legacy/ = 2
        expected = "/".join([".."] * depth + ["src"])
        found = re.findall(
            r"""sys\.path\.(?:append|insert)\((?:\s*\d+\s*,)?\s*["']((?:\.\./)+src)["']""",
            _code(os.path.join(NB_DIR, nb)),
        )
        assert all(path == expected for path in found), (
            f"{nb} adds {found} to sys.path; from its directory the package "
            f"source is at {expected!r}"
        )


class TestSensorNotebooksOwnNoPaths:
    @pytest.mark.parametrize("nb,sensor", sorted(WORKFLOW_NOTEBOOKS.items()))
    def test_declares_its_sensor_key(self, nb, sensor):
        src = _code(os.path.join(NB_DIR, nb))
        assert f'PRODUCT_SENSOR = "{sensor}"' in src, (
            f"{nb} must declare PRODUCT_SENSOR so the shared table can resolve "
            f"its destinations"
        )

    @pytest.mark.parametrize("nb", sorted(WORKFLOW_NOTEBOOKS))
    def test_takes_the_bucket_from_the_shared_table(self, nb):
        src = _code(os.path.join(NB_DIR, nb))
        assert "from shared_utils.product_paths import STAGING_BUCKET" in src
        assert "S3_BUCKET = STAGING_BUCKET" in src

    @pytest.mark.parametrize("nb", sorted(WORKFLOW_NOTEBOOKS))
    def test_names_no_bucket_literal(self, nb):
        # Three sensor notebooks had drifted onto two different buckets.
        src = _code(os.path.join(NB_DIR, nb))
        for bad in ('"nasa-disasters"', "'nasa-disasters'",
                    '"nasa-disasters-staging"', "'nasa-disasters-staging'"):
            assert bad not in src, f"{nb} hard-codes the bucket {bad}"

    @pytest.mark.parametrize("nb", sorted(WORKFLOW_NOTEBOOKS))
    def test_no_stale_prefix_variables(self, nb):
        # S3_PREFIX / S3_DEST_BASE are gone; a leftover reference is a NameError
        # at run time, and notebooks are not executed in CI to catch it. Any
        # S3_*PREFIX is refused, not just that one spelling: landsat_odr shipped
        # an S3_TEST_PREFIX that decided where every product was published.
        src = _all_source(os.path.join(NB_DIR, nb))
        stale = sorted(set(re.findall(r"\bS3_\w*PREFIX\b|\bS3_DEST_BASE\b", src)))
        assert not stale, (
            f"{nb} still refers to {stale}; the destination prefix comes from "
            f"shared_utils.product_paths"
        )

    @pytest.mark.parametrize("nb", sorted(WORKFLOW_NOTEBOOKS))
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

    @pytest.mark.parametrize("nb", sorted(WORKFLOW_NOTEBOOKS))
    def test_publishes_through_the_shared_resolver(self, nb):
        src = _code(os.path.join(NB_DIR, nb))
        assert ("prefix_for_product_dir" in src) or ("sensor=PRODUCT_SENSOR" in src), (
            f"{nb} must key its uploads by the canonical destination, either via "
            f"prefix_for_product_dir() or by passing sensor= to upload_dir_ambient()"
        )

    # What may follow "s3://{S3_BUCKET}/" in a notebook: a key that came out of
    # the shared table, or the "ProgramData/<Sensor>/<Product>/" placeholder a
    # notebook prints for the operator.
    _FROM_THE_TABLE = ("{_key}", "{prefix_for_product_dir(", "ProgramData/")

    @pytest.mark.parametrize("nb", sorted(WORKFLOW_NOTEBOOKS))
    def test_every_destination_in_the_bucket_comes_from_the_table(self, nb):
        """A notebook that names the resolver can still upload somewhere else.

        Both ODR notebooks did: `f"s3://{S3_BUCKET}/Sentinel_Test/{filename}"`
        and `f"s3://{S3_BUCKET}/{S3_TEST_PREFIX}/{filename}"` (default
        "Landsat_Test"), each next to a correct call that was not the one being
        made. test_publishes_through_the_shared_resolver passed on both,
        because the resolver's name was still in the cell.
        """
        marker = "s3://{S3_BUCKET}/"
        for cell in _code_cells(os.path.join(NB_DIR, nb)):
            try:
                tree = ast.parse(cell)
            except SyntaxError:
                continue  # covered by test_every_code_cell_parses
            for node in ast.walk(tree):
                # `_key` is only "from the table" if the table built it.
                if (isinstance(node, ast.Assign)
                        and getattr(node.targets[0], "id", None) == "_key"):
                    built = (_fstring_skeleton(node.value)
                             if isinstance(node.value, ast.JoinedStr) else ast.unparse(node.value))
                    assert built.startswith("{prefix_for_product_dir("), (
                        f"{nb} sets _key = {built!r}; the upload key must start "
                        f"with prefix_for_product_dir(...)"
                    )
                if not isinstance(node, ast.JoinedStr):
                    continue
                for tail in _fstring_skeleton(node).split(marker)[1:]:
                    assert tail.startswith(self._FROM_THE_TABLE), (
                        f"{nb} builds the destination {marker}{tail!r}; under the "
                        f"staging bucket only a key resolved through "
                        f"shared_utils.product_paths is allowed"
                    )

    @pytest.mark.parametrize("nb", sorted(WORKFLOW_NOTEBOOKS))
    def test_no_code_is_parked_in_a_string_literal(self, nb):
        """A statement that is only a string does nothing.

        sentinel2_odr_workflow.ipynb "commented out" its canonical upload call
        by wrapping it in triple quotes, then printed a message saying the
        files had gone there. It parses, it names every resolver, and it never
        runs -- so every other check in this file passed.
        """
        for i, cell in enumerate(_code_cells(os.path.join(NB_DIR, nb))):
            try:
                tree = ast.parse(cell)
            except SyntaxError:
                continue  # covered by test_every_code_cell_parses
            docstrings = _docstrings(tree)
            stray = [
                node.value.value for node in ast.walk(tree)
                if isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                and id(node) not in docstrings
            ]
            assert not stray, (
                f"{nb} code cell {i} has a bare string statement: {stray[0][:80]!r}. "
                f"Delete dead code; a string literal is not a comment."
            )

    @pytest.mark.parametrize("nb", sorted(WORKFLOW_NOTEBOOKS))
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

    @pytest.mark.parametrize("nb", sorted(WORKFLOW_NOTEBOOKS))
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

    @pytest.mark.parametrize("nb", sorted(WORKFLOW_NOTEBOOKS))
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

    @pytest.mark.parametrize("nb", sorted(LEGACY_NOTEBOOKS))
    def test_directory_publishers_use_the_ambient_helper(self, nb):
        """The two retired workflows publish a whole product tree, so they keep
        staging_upload's keying."""
        assert "upload_dir_ambient" in self._referenced(os.path.join(LEGACY_DIR, nb))

    @pytest.mark.parametrize("nb", sorted(WORKFLOW_NOTEBOOKS))
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

    def test_the_committed_notebook_is_what_the_generator_writes(self):
        """Editing the notebook in Jupyter is the natural thing to do, and wrong.

        PR #190 did exactly that: seven cells changed in the notebook and none
        in the generator, so the next regeneration would have silently undone
        the new cloud-mask product -- and until then the generator described a
        notebook that no longer existed. The check below (same wiring) passed
        throughout; only a cell-for-cell comparison notices.

        Cell type and source are compared, not ids or metadata: Jupyter adds
        those on save and they change nothing an operator runs.
        """
        gen_path = os.path.join(REPO_ROOT, "tools", "_build_s2_odr_notebook.py")
        spec = importlib.util.spec_from_file_location("_build_s2_odr_notebook", gen_path)
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)

        def cells_of(nb):
            return [(c["cell_type"], "".join(c["source"])) for c in nb["cells"]]

        with open(os.path.join(NB_DIR, "sentinel2_odr_workflow.ipynb"), encoding="utf-8") as fh:
            committed = cells_of(json.load(fh))
        generated = cells_of(gen.build())

        assert len(committed) == len(generated), (
            f"the committed notebook has {len(committed)} cells, the generator "
            f"writes {len(generated)}"
        )
        for i, (have, want) in enumerate(zip(committed, generated)):
            assert have == want, (
                f"notebooks/sentinel2_odr_workflow.ipynb cell {i} differs from "
                f"tools/_build_s2_odr_notebook.py. The notebook is generated: "
                f"make the change in the generator, then run "
                f"`python tools/_build_s2_odr_notebook.py`."
            )

    def test_generator_carries_the_same_wiring(self):
        gen = os.path.join(REPO_ROOT, "tools", "_build_s2_odr_notebook.py")
        with open(gen, encoding="utf-8") as fh:
            src = fh.read()
        assert 'PRODUCT_SENSOR = "sentinel2"' in src
        assert "from shared_utils.product_paths import STAGING_BUCKET" in src
        assert "_PRODUCT_KEYS" in src
        assert "S3_PREFIX" not in src and "S3_DEST_BASE" not in src


# The vendor workflows keep OUTPUT_DIR under /tmp and publish everything in it.
# Without a reset, every earlier run's COGs piled up there -- filling /tmp and
# being re-uploaded by the upload cell on every later run.
VENDOR_NOTEBOOKS = ("capella_workflow.ipynb", "umbra_workflow.ipynb",
                    "skysat_workflow.ipynb", "satellogic_workflow.ipynb",
                    "iceye_workflow.ipynb")
# landsat_odr keeps its OUTPUT_DIR under /tmp too, and reads no vendor bucket.
# (sentinel2_odr is deliberately absent: its water-extent reference cache --
# a 1.6 GB national CDL -- lives under OUTPUT_DIR and must survive a re-run, so
# it scopes to NEW_COGS by modification time instead of emptying the directory.)
RESET_NOTEBOOKS = VENDOR_NOTEBOOKS + ("landsat_odr_workflow.ipynb",)
_CLI_OUTPUT_RE = re.compile(r"""["']--output["']\s*,\s*OUTPUT_DIR""")


class TestTmpHygiene:
    @pytest.mark.parametrize("nb", RESET_NOTEBOOKS)
    def test_output_dir_reset_before_the_cli_writes_to_it(self, nb):
        reset_seen = False
        dispatched = False
        for src in _code_cells(os.path.join(NB_DIR, nb)):
            reset_at = src.find("reset_output_dir(OUTPUT_DIR)")
            m = _CLI_OUTPUT_RE.search(src)
            if m:
                dispatched = True
                assert reset_seen or (0 <= reset_at < m.start()), (
                    f"{nb}: the processing cell writes to OUTPUT_DIR without "
                    f"calling reset_output_dir(OUTPUT_DIR) first"
                )
            if reset_at >= 0:
                reset_seen = True
        assert dispatched, f"{nb}: no cell passes --output OUTPUT_DIR to the CLI"

    @pytest.mark.parametrize("nb", sorted(WORKFLOW_NOTEBOOKS))
    def test_no_mkstemp(self, nb):
        # mkstemp minted a new activation_meta_*.json in /tmp on every run of the
        # config cell; a fixed per-sensor name is overwritten instead.
        assert "mkstemp" not in _code(os.path.join(NB_DIR, nb))

    def test_s2_odr_generator_has_no_mkstemp(self):
        gen = os.path.join(REPO_ROOT, "tools", "_build_s2_odr_notebook.py")
        assert "mkstemp" not in open(gen, encoding="utf-8").read()
